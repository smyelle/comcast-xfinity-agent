"""Emit the authored flows app, then graft the source app's diagnostic substrate onto it.

`flows.build_app` produces a complete CES app for the orchestration we authored. It
cannot produce the parts that are not ours to invent: the OpenAPI toolsets, the
`toolFakeConfig` mocks every evaluation scenario drives through `mock_config_string`,
and the specialist sub-agents that `run_comcast_diagnostics` fans out to. Those are
copied across verbatim so the only thing that differs from the source app is the
orchestration layer.

    python build.py [--out DIR]
    python build.py --help          every mode this build has, and its default

Every switch is a flag. There are no environment variables left to know about: what a
build composed is decided once, in `build_config.resolve`, and stamped into the emitted
app dir as `build_manifest.json`. See `build_config` for why that matters.
"""

import argparse
import json
import re
import os
import shutil
import uuid
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import labs_paths  # noqa: E402

# Must run BEFORE `import flows`: the SDK lives in another repo, and an unrelated
# `flows` may already be installed in this environment — which is not a hypothetical,
# it is what an editable install of some other checkout does. This puts the intended
# one first, and says so loudly if it cannot find one.
labs_paths.add_sdk_paths()
labs_paths.require_features()

import flows  # noqa: E402

import build_config  # noqa: E402
import engine_packing  # noqa: E402
import source_tools  # noqa: E402

# `app` is imported by `main()`, NOT here. Importing it constructs the whole `App`, tool
# bodies and all, and those bodies are emitted from the build config — so the config has
# to be resolved first. A module-level import here is how `--demo` came to set its switch
# too late to have any effect on what it emitted; `build_config.activate` now refuses that
# ordering outright.

SOURCE = source_tools.SOURCE


def _copy_tree(src, dst):
  if not os.path.isdir(src):
    return False
  if os.path.isdir(dst):
    shutil.rmtree(dst)
  shutil.copytree(src, dst)
  return True


def graft_substrate(out_dir: str, config: build_config.BuildConfig) -> dict:
  """Copy the source's toolsets, tool metadata (incl. fakes) and specialist agents."""
  if not os.path.isdir(os.path.join(SOURCE, "toolsets")):
    raise SystemExit(
        f"Cannot find the tool substrate at {SOURCE!r}.\n\n"
        "This build grafts the carried toolsets, tool fake configs and specialist\n"
        "sub-agents onto the authored flow. They are vendored in `flows-sdk/substrate/`,\n"
        "so run this from `flows-sdk/`, or point COMCAST_SOURCE at another app root:\n\n"
        "    COMCAST_SOURCE=/path/to/app python build.py --out ./built")
  report = {"toolsets": [], "tool_meta": [], "agents": [], "missing": []}

  # 1. Toolsets — the OpenAPI backends the carried tools call.
  src_toolsets = os.path.join(SOURCE, "toolsets")
  for name in sorted(os.listdir(src_toolsets)):
    if _copy_tree(os.path.join(src_toolsets, name),
                  os.path.join(out_dir, "toolsets", name)):
      report["toolsets"].append(name)

  # 2. Tool METADATA. build_app already wrote each carried tool's python body from
  #    `tool_bodies`, but not its JSON — which is where `toolFakeConfig` and, for the
  #    agent-as-a-tool wrappers, the `agentTool` binding live. Overwrite the generated
  #    JSON with the source's, preserving the generated name/uuid so the push does not
  #    collide with the source app's tool ids.
  for name in source_tools.CARRIED_TOOL_META:
    src_json = os.path.join(SOURCE, "tools", name, f"{name}.json")
    dst_dir = os.path.join(out_dir, "tools", name)
    dst_json = os.path.join(dst_dir, f"{name}.json")
    if not os.path.exists(src_json):
      report["missing"].append(f"tool json: {name}")
      continue
    os.makedirs(dst_dir, exist_ok=True)
    with open(src_json) as fh:
      meta = json.load(fh)
    if os.path.exists(dst_json):
      with open(dst_json) as fh:
        meta["name"] = json.load(fh).get("name", meta.get("name"))
    else:
      meta.pop("name", None)
    # Drop `executionType: ASYNCHRONOUS` from a carried tool whose body CALLS another
    # tool. `ces-probes/112` proves that shape cannot work: once CES actually defers the
    # body, the nested call fails, and nothing reports it -- the caller gets a placeholder
    # or an error where it expected a result.
    #
    # Four of the source's tools are in exactly that shape, and one of them is what took
    # a live call down. `perform_connect_network_analysis` is asynchronous and calls
    # `connect_agent_rest_call_sendA`; deferred, it never answers, so the network
    # specialist re-asked and CES killed the turn at its 10-reasoning-loop limit --
    # "Agent has reached the limit of 10 reasoning loops", measured against real
    # backends. `check_outage`, `rdk_client_wifi_analysis` and
    # `perform_rdk_device_diagnostics` are the same shape.
    #
    # Synchronous is the correct shape for all four: a synchronous body may call another
    # tool, which is precisely what these exist to do. Conditioned on the body rather
    # than hardcoded, so a source tool that is legitimately asynchronous -- self-contained,
    # no nested call -- keeps its declaration.
    body_path = os.path.join(dst_dir, "python_function", "python_code.py")
    if meta.get("executionType") == "ASYNCHRONOUS" and os.path.exists(body_path):
      with open(body_path) as fh:
        body = fh.read()
      if re.search(r"\btools\.[A-Za-z_]", body):
        meta.pop("executionType", None)
        report.setdefault("desynced", []).append(name)
    with open(dst_json, "w") as fh:
      json.dump(meta, fh, indent=2)
    # Fake configs are referenced by path from the tool JSON, so bring the directory.
    src_fake = os.path.join(SOURCE, "tools", name, "tool_fake_config")
    if os.path.isdir(src_fake):
      _copy_tree(src_fake, os.path.join(dst_dir, "tool_fake_config"))
    report["tool_meta"].append(name)

  # 2b. Fake configs for the LOWERED legs of a progressive group. The generated
  #     `<group>_<leg>_leg` wrapper inlines the author's body instead of calling the tool
  #     through `tools` -- deliberately (ces-probes 147: a nested call from a deferred
  #     body is aborted once it outlives the turn) -- and the original tool's
  #     `toolFakeConfig` is bypassed along with it. So every mocked scenario for a lowered
  #     leg goes silently live: `outage_status=active` in the fixture came back "none" and
  #     11 of 13 ladder scenarios resolved against the real backend. Copy the source
  #     tool's fake onto the wrapper, code block and all, so a fixture keeps meaning what
  #     it says.
  #     Imported hard, not behind `except ImportError: {}`. Swallowing the error made
  #     moving this constant a SILENT downgrade to exactly the live-backend behaviour the
  #     paragraph above describes -- the build still succeeded and the fixtures still read
  #     as if they meant something. A missing name should stop the build.
  from journeys.diagnostics_sweep import SPIKE_LEG_FAKES  # noqa: PLC0415
  for leg_tool, src_tool in sorted(SPIKE_LEG_FAKES.items()):
    dst_json = os.path.join(out_dir, "tools", leg_tool, f"{leg_tool}.json")
    src_json = os.path.join(SOURCE, "tools", src_tool, f"{src_tool}.json")
    if not (os.path.exists(dst_json) and os.path.exists(src_json)):
      continue  # the group was not lowered this build (or the leg is gated out)
    with open(src_json) as fh:
      fake = json.load(fh).get("toolFakeConfig")
    if not fake:
      continue
    src_code = os.path.join(SOURCE, "tools", src_tool, "tool_fake_config")
    if os.path.isdir(src_code):
      _copy_tree(src_code, os.path.join(out_dir, "tools", leg_tool, "tool_fake_config"))
    # Repoint the code path at the wrapper's own copy; the source path names the tool it
    # came from and would not resolve inside this app.
    code = (fake.get("codeBlock") or {}).get("pythonCode")
    if code:
      fake["codeBlock"]["pythonCode"] = code.replace(
          f"tools/{src_tool}/", f"tools/{leg_tool}/")
    with open(dst_json) as fh:
      meta = json.load(fh)
    meta["toolFakeConfig"] = fake
    with open(dst_json, "w") as fh:
      json.dump(meta, fh, indent=2)
    # The fixture reaches a lowered leg as an ARGUMENT, because a deferred body's view
    # of session state is not the caller's -- `mock_config_string` simply is not there,
    # so every faked leg fell through to its "nothing found" default and 11 of 13 ladder
    # scenarios resolved against the wrong data while looking healthy. The source fake
    # reads ambient state, so its copy is taught to prefer the argument.
    fake_code = os.path.join(out_dir, "tools", leg_tool, "tool_fake_config",
                             "code_block", "python_code.py")
    if os.path.exists(fake_code):
      with open(fake_code) as fh:
        body = fh.read()
      patched = body.replace(
          'state.get("mock_config_string") or ""',
          '(input or {}).get("mock_config_string")'
          ' or state.get("mock_config_string") or ""')
      # Normalise the fake's verdict key. The real body returns `"success": True`; its
      # fixture returns `"status": "success"` and no `success` at all. That difference is
      # invisible while the tool is only ever called NESTED -- the sweep wrapper supplies
      # its own `success` -- and fatal the moment a LEG fires the tool directly, because
      # the task's success_check reads the fixture's payload and finds nothing. Measured:
      # all four legs reported failure under fakes, the group closed with no statuses, and
      # the caller heard the platform's "I'm having trouble with that".
      #
      # `status` cannot stand in for it: it carries "success" AND "error", both truthy.
      patched += (
          "\n\n_fake_tool_call_inner = fake_tool_call\n\n\n"
          "def fake_tool_call(tool, input, callback_context):\n"
          "  out = _fake_tool_call_inner(tool, input, callback_context)\n"
          "  if isinstance(out, dict) and \"success\" not in out:\n"
          "    out[\"success\"] = out.get(\"status\") != \"error\"\n"
          "  return out\n")
      if patched != body:
        with open(fake_code, "w") as fh:
          fh.write(patched)
    # ...and, when asked, INLINE it.
    #
    # CORRECTION. This block first claimed a `toolFakeConfig` is not honoured on an
    # ASYNCHRONOUS tool. That claim was WRONG, and ces-probes 150 disproves it with a
    # cleaner instrument than the one that produced it: two resources differing only in
    # `executionType`, the fake answering 4/4 on BOTH arms across two sessions, against a
    # no-fakes control on the same deploy reporting the real body 4/4. Fakes work fine on
    # asynchronous tools. What went wrong on THIS agent's lowered legs is still open --
    # the legs stayed deferred under `use_tool_fakes`, and 150 also found that a honoured
    # fake CANCELS deferral, so deferral surviving is itself evidence the fake never
    # applied here.
    #
    # The inlining stays, because it is useful whatever the cause: the fixture goes into
    # the wrapper, which IS the body that runs, so a mocked scenario exercises the leg
    # regardless. It is a workaround for an unexplained gap, not for a platform rule.
    #
    # Opt-in, and never for a build that will serve real callers: this hard-wires recorded
    # answers into the tool.
    # Make a leg genuinely SLOW, so the group actually has idle turns to reassure over.
    # Every backend reachable from this desk answers in well under a second, so without
    # this the reassurance path cannot be observed at all -- the legs land inside their
    # firing turn and there is nothing to wait through. The sleep is in the wrapper's OWN
    # body, which ces-probes 147 measured as unbounded (24/24 at 3s); it is a nested call
    # that cannot outlive the turn, not the body itself.
    if config.leg_delay:
      secs = build_config.format_seconds(config.leg_delay)
      body_path = os.path.join(out_dir, "tools", leg_tool, "python_function",
                               "python_code.py")
      if os.path.exists(body_path):
        with open(body_path) as fh:
          body = fh.read()
        if "_LEG_SLOWED" not in body:
          body = body.replace(
              "  import inspect as _inspect\n",
              "  _LEG_SLOWED = True\n"
              "  import time as _time\n"
              f"  _time.sleep({secs})\n"
              "  import inspect as _inspect\n", 1)
          with open(body_path, "w") as fh:
            fh.write(body)
          report["tool_meta"].append(f"{leg_tool} (slowed {secs}s)")

    # `--demo` IMPLIES THIS. It did not once -- the inlining hung off a separate
    # `SPIKE_FAKE_LEGS` environment variable that nothing in `--help` mentioned -- and
    # that is what made the demo build lie. `--demo` resolves `legs=fake` in
    # `build_config.resolve` now, and `--legs=live` is how a demo build opts back out.
    #
    # `bake_demo_fixtures` walks `CARRIED_TOOL_META` and inlines each carried tool's
    # fixture into its own body. A lowered leg is not a carried tool -- it is a wrapper
    # this build EMITS, with the author's body copied into it -- so that walk never
    # reaches `SweepLegs_leg_outage_leg` or `SweepLegs_leg_convoy_leg`, and the demo
    # promise ("every carried tool answers from its recorded fixture") held for the
    # account gate and the specialists but not for the two legs.
    #
    # MEASURED on the UX build, cold and seeded, before this line changed: seeding
    # `outage_status=active&convoy_status=predictive_offline` produced
    # `outage_status="none"` and `convoy_status="clear"` -- and `settle_diagnostics`
    # defaults an unfilled status to `"skipped"`, so those are not defaults, they are the
    # EDE and Convoy APIs answering. Cold on 8069100230359928, whose binding is
    # `convoy_status=predictive_offline`, live Convoy said `technician` and the caller was
    # told a technician was needed instead of being offered the reboot.
    #
    # The inlining below is exactly the right instrument and was already written; it was
    # simply behind its own opt-in. Still never for a build a real caller reaches -- both
    # this and the delay above hard-wire recorded answers, and the default build is
    # `--legs=live`.
    if config.fake_legs:
      body_path = os.path.join(out_dir, "tools", leg_tool, "python_function",
                               "python_code.py")
      # `fake_code` -- the BUILT copy -- not the source one. The two patches above are
      # written there and both exist because a LEG needs them: preferring the
      # `mock_config_string` ARGUMENT over ambient state (a deferred body cannot see the
      # caller's session), and normalising the fixture's missing `success` key (the leg's
      # own success_check reads the fixture's payload directly). Inlining the unpatched
      # source silently undid both -- which is precisely the failure the patches were
      # added for: every faked leg fell through to its "nothing found" default and then
      # reported failure, and 11 of 13 ladder scenarios resolved against the wrong data
      # while looking healthy.
      if os.path.exists(body_path) and os.path.exists(fake_code):
        with open(body_path) as fh:
          body = fh.read()
        with open(fake_code) as fh:
          fixture = fh.read()
        state_key = leg_tool[:-4] if leg_tool.endswith("_leg") else leg_tool
        if "_LEG_FIXTURE_INLINED" not in body:
          shim = (
              "\n\n# --- fixture, inlined because a fake is ignored on an ASYNCHRONOUS"
              " tool ---\n_LEG_FIXTURE_INLINED = True\n"
              # A fake's signature is annotated with names CES injects into a
              # tool_fake_config module and NOT into a python_function one. Annotations
              # evaluate at def time, so without these the inlined fixture raises
              # NameError on import and the leg silently runs for real -- which is the
              # very failure this inlining exists to remove.
              "from typing import Any, Optional  # noqa: F401\n"
              "Tool = globals().get('Tool', object)\n"
              "CallbackContext = globals().get('CallbackContext', object)\n"
              # EVERY occurrence, on a word boundary. The patched fixture defines
              # `fake_tool_call` twice -- the original and the success-normalising
              # wrapper that calls it through `_fake_tool_call_inner` -- so renaming only
              # the first `def` would leave `_fake_tool_call_inner = fake_tool_call`
              # pointing at a name that no longer exists, and the whole inlined module
              # would raise NameError on import. Word-bounded so
              # `_fake_tool_call_inner` itself is left alone.
              + re.sub(r"\bfake_tool_call\b", "_leg_fixture", fixture)
              + "\n\ndef _fixture_answer(_args):\n"
              "  \"\"\"The recorded answer for these arguments, or None to run for real.\"\"\"\n"
              "  try:\n"
              "    return _leg_fixture(None, _args, context)  # noqa: F821\n"
              "  except Exception:\n"
              "    return None\n")
          body = body + shim
          # Consult it before doing any real work.
          publish = ('      context.state["' + state_key
                     + '"] = _json.dumps(_fx, default=str)\n')
          probe = ("  _fx = _fixture_answer(_args)\n"
                   "  if isinstance(_fx, dict):\n"
                   "    try:\n"
                   + publish
                   + "    except Exception:\n"
                     "      pass\n"
                     "    return _fx\n")
          body = body.replace("  try:\n    _sig = _inspect.signature(",
                              probe + "  try:\n    _sig = _inspect.signature(", 1)
          # The PATCHED fixture must be what was inlined. `_fake_tool_call_inner` is the
          # marker because the success-normalising wrapper above is appended
          # unconditionally, so its absence means the SOURCE copy was read instead --
          # which produced a leg that compiled, deployed and ran, and quietly ignored
          # every scenario (`mock_config_string` is not in a deferred body's session
          # state) while reporting failure (the fixture has no `success` key). Asserted
          # rather than trusted: that failure has no symptom except a ladder resolving
          # against the wrong data and looking healthy doing it.
          if "_fake_tool_call_inner" not in body:
            raise SystemExit(
                f"build: {leg_tool}'s inlined fixture is the unpatched SOURCE copy, so "
                "faked legs will silently answer for the wrong scenario and then report "
                "failure. Read the fixture from `fake_code`, not from SOURCE.")
          compile(body, body_path, "exec")
          with open(body_path, "w") as fh:
            fh.write(body)
          report["tool_meta"].append(f"{leg_tool} (fixture INLINED)")
    report["tool_meta"].append(f"{leg_tool} (fake <- {src_tool})")

  # 2c. The DEMO build's fixture baking USED to run here, and could not work from here.
  #     It is `bake_demo_fixtures()` now, called from `main()` after the three patches
  #     that write the fixtures it bakes. See its docstring.

  # 3. environment.json — the toolsets' endpoints and credentials resolve `$env_var`
  #    defaults through it, and CES rejects the import outright if a referenced key is
  #    missing.
  #
  #    MERGED, not copied over. Every toolset in this app used to be the source's, so a
  #    straight copy was right. `specialist_proxy` is the first one this conversion
  #    AUTHORS: the SDK writes its URL here under `env_scoped`, and the source's file --
  #    which has never heard of it -- overwrote that with nothing. The toolset then
  #    deploys with `servers: $env_var` unresolved and every specialist call fails at
  #    runtime, while the app looks perfectly well-formed on disk. Authored entries are
  #    kept for keys the source does not define; the source wins everywhere else, so the
  #    grafted endpoints and credentials are untouched. Same hazard, and the same rule,
  #    as `patch_app_json` merging the authored audio keys over the grafted ones.
  for name in ("environment.json", "pythonEnvFiles"):
    src = os.path.join(SOURCE, name)
    dst = os.path.join(out_dir, name)
    if os.path.isfile(src):
      if name == "environment.json":
        authored = {}
        if os.path.isfile(dst):
          with open(dst) as fh:
            authored = json.load(fh)
        with open(src) as fh:
          merged = json.load(fh)
        kept = []
        for key, value in (authored.get("toolsets") or {}).items():
          if key not in (merged.get("toolsets") or {}):
            merged.setdefault("toolsets", {})[key] = value
            kept.append(key)
        with open(dst, "w") as fh:
          json.dump(merged, fh, indent=2)
        if kept:
          report["tool_meta"].append(
              f"environment.json: kept authored toolset(s) {sorted(kept)}")
      else:
        shutil.copy2(src, dst)
      report.setdefault("files", []).append(name)
    elif _copy_tree(src, dst):
      report.setdefault("files", []).append(name)

  # The source `guardrails/` directory is deliberately NOT copied. It used to be, and the
  # copy was worse than useless: `patch_app_json` never carried the source's guardrails
  # ARRAY, so four resource dirs shipped bound to nothing and the app deployed unguarded
  # while looking guarded on disk. `guardrails.py` now authors them through the SDK, which
  # emits the resources AND the names together and puts both in front of `validate_app`.
  # Copying as well would stand unmanaged dirs beside managed ones and let ordering decide.

  # 4. Specialist sub-agents, with their own instructions/callbacks/toolsets.
  for name in source_tools.SPECIALIST_AGENTS:
    if _copy_tree(os.path.join(SOURCE, "agents", name),
                  os.path.join(out_dir, "agents", name)):
      report["agents"].append(name)
    else:
      report["missing"].append(f"agent: {name}")
  return report


def patch_app_json(out_dir: str) -> None:
  """Carry over the ONE app-level setting the SDK cannot express.

  Everything else that used to be patched here is now DECLARED on the `App`
  (`app_settings=`, `time_zone=`, `guardrails=`). That matters beyond tidiness: a
  declared setting is recorded in `declared-settings.json`, held by the emit integrity
  check, and survives a `flows deploy` merge — whereas a key in a tuple here is invisible
  to all three. Dropping a key from that tuple is exactly how the app lost its guardrail
  attachment, its DLP redaction and its XML strip without a single warning.

  `temperature` is what is left: `App` has a `model=` field but no `temperature=`, so
  `modelSettings.temperature: 0.0` can only arrive by patch.

  `model` is deliberately NOT taken from the source. `app.py` owns it, and a blanket
  `update(src["modelSettings"])` would silently overwrite the emitted model with the
  source's — deploying the app on a different model than it was authored for, which for
  guardrails is the difference between preventing and merely detecting.
  """
  path = os.path.join(out_dir, "app.json")
  with open(path) as fh:
    emitted = json.load(fh)
  with open(os.path.join(SOURCE, "app.json")) as fh:
    src = json.load(fh)

  # `audioProcessingConfig` is the one that makes this a VOICE agent at all. Without
  # it CES rejects a bidi/voice session outright — every attempt came back
  # `generic::invalid_argument` and the websocket closed, so the app could be driven
  # over text and not over the phone. Every check in this directory is a text drive,
  # so nothing here noticed; it took pointing an audio caller at the app.
  #
  # `loggingSettings` is the same class of silent loss: it carries the DLP redaction that
  # keeps the caller's account and phone numbers out of the logs, and the conversion had
  # been dropping it.
  # Carried WHOLESALE, so anything the app authored under one of these keys is
  # overwritten. That is right for the settings this agent does not own, and wrong for
  # `audioProcessingConfig`, which the app now writes into: `inactivityTimeout` was
  # declared, emitted, recorded in `declared-settings.json` -- and then replaced here by
  # the source's copy, which has no timeout. The reassurance had no clock to run on and
  # the fan-out held the floor in silence. Same shape as the `modelSettings` hazard
  # below, and handled the same way: the authored keys are re-applied on top.
  authored_audio = dict(emitted.get("audioProcessingConfig") or {})
  for key in ("toolExecutionMode", "evaluationSettings", "timeZoneSettings",
              "languageSettings", "evaluationMetricsThresholds",
              "audioProcessingConfig", "loggingSettings"):
    if key in src:
      emitted[key] = src[key]
  if authored_audio:
    emitted.setdefault("audioProcessingConfig", {}).update(authored_audio)

  v_decls = emitted.setdefault("variableDeclarations", [])
  if not any(v.get("name") == "mock_config_dict" for v in v_decls):
    v_decls.append({
        "name": "mock_config_dict",
        "description": "Unified mock configuration dictionary",
        "schema": {"type": "OBJECT"}
    })
  # The source carries modelSettings (temperature etc.), but the AUTHORED app owns the
  # model choice — keep the emitted model so app.py `model=` wins over the source's.
  # Without this the app deploys on a model it was not authored for, which for guardrails
  # is the difference between preventing and merely detecting.
  authored_model = emitted.get("modelSettings", {}).get("model")
  emitted.setdefault("modelSettings", {}).update(src.get("modelSettings", {}))
  # A live-measured hazard worth keeping next to the model choice: on composite-v1 the
  # voice `en-US-Standard-A` emits ZERO audio and closes the session while still
  # answering normally in TEXT, so a text drive cannot see it. If this app ever pins a
  # voice it must not be that one; `synthesizeSpeechConfigs` here is `{"en-US": {}}`,
  # i.e. no voice pinned, which is the safe shape.
  if authored_model:
    emitted["modelSettings"]["model"] = authored_model

  # Barge-in. Without this key the caller CANNOT interrupt: measured in ces-probes 79,
  # a caller talking over the agent changed the call length not at all (59.1s vs 61.2s),
  # while the same app with `bargeInAwareness` went 54.6s -> 36.4s.
  #
  # This agent has lines a caller would want to escape — the outage advisory is ~290
  # characters, about twenty seconds they currently have to sit through even once they
  # have heard the part they rang about. Nested under the carried config rather than
  # replacing it, so the source's `synthesizeSpeechConfigs` survives.
  emitted.setdefault("audioProcessingConfig", {}).setdefault(
      "bargeInConfig", {})["bargeInAwareness"] = True

  with open(path, "w") as fh:
    json.dump(emitted, fh, indent=2)


def patch_root_agent(out_dir: str, app) -> list:
  """Declare the carried tools + toolsets on the root agent.

  CES only exposes a tool to an agent that lists it, and a tool the engine fires must
  still be declared even though the model never sees it.
  """
  agent_dir = os.path.join(out_dir, "agents", app.root_agent)
  path = os.path.join(agent_dir, f"{app.root_agent}.json")
  with open(path) as fh:
    agent = json.load(fh)

  declared = set(agent.get("tools", []))
  declared.update(source_tools.CARRIED_TOOL_META)
  declared.update(source_tools.RUNG_TOOLS)
  # Declaring a tool here is what OFFERS it to the model, so the fan-out probes and
  # the rungs' delegate targets come straight back off: they are reached from inside
  # another tool's body, which resolves against the app rather than this list. See
  # source_tools.ENGINE_ONLY_TOOLS for what that prevents.
  declared.difference_update(source_tools.ENGINE_ONLY_TOOLS)

  # The per-flow `{config_id}_dag` tools come off too. They are pure config fetches the
  # ENGINE calls - `getattr(tools, f"{config_id}_dag")({})` in slot_filling_engine - so
  # like everything else above they resolve against the app, not this list.
  #
  # Offering them is actively harmful, because it hands the model a way to enter a flow
  # the router did not choose. Driven over AUDIO on a free turn, having correctly routed
  # to `repair`, the model went on to call `steering_dag` and then a SIBLING flow's DAG:
  # `reboot_dag` -> `verdict_steering_reboot` -> "Alright, I am sending a signal to
  # reboot your gateway now" on a healthy account, and `technical_phone_dag` ->
  # `verdict_defer` -> a transfer, for a caller reporting an internet fault. 2 of 3 runs,
  # against 0 of 3 in text - the model is likelier to fill a spoken turn with a tool.
  #
  # `router_hide_tools` already hides these on the ROUTER turn, which is why the routing
  # decision itself is correct. Nothing hides them on the turns AFTER it.
  declared = {t for t in declared if not t.endswith("_dag")}
  agent["tools"] = sorted(declared)

  # TOOLSETS get the same treatment as tools, and until now they did not. `tools` above
  # is filtered by ENGINE_ONLY_TOOLS so the model is never offered a transfer or a
  # reboot; `toolsets` was carried across verbatim, and it re-opened every door that
  # filtering closes:
  #
  #     gateway_restart_api        -> restartDevice
  #     xa_customer_notification_api -> postNotification
  #     device_status_api          -> checkDeviceStatus
  #     convoy_recs_account        -> getRecommendationsByAccount
  #
  # Driven over AUDIO, that is not theoretical. Given a free turn the model called
  # `device_status_api.checkDeviceStatus` and then `gateway_restart_api.restartDevice`
  # TWICE, and told the caller "I have sent a signal to restart your gateway" — an
  # unrequested restart of a healthy gateway, reached without any rung firing.
  # Reproduced 3 times out of 3.
  #
  # None of these need to be on the ROOT agent. Every one is invoked from inside a tool
  # body — `reboot` calls `tools.gateway_restart_api_restartDevice(...)` — and the
  # `tools` namespace resolves against the APP registry, not the calling agent's list.
  # That is the same mechanism ENGINE_ONLY_TOOLS already relies on. The specialist
  # sub-agents carry their own toolsets in their own JSON and are untouched.
  agent["toolsets"] = []

  with open(path, "w") as fh:
    json.dump(agent, fh, indent=2)
  return agent["tools"]


# Anchored on the read, not on the branch below it, so the carry lands OUTSIDE any
# conditional and at the same indent as the line it follows. Anchoring on the `if`
# produced a block with no body — valid text, uncompilable Python, and invisible to
# both the build and the offline oracles because neither executes this file.
_TECH_TYPE_ANCHOR = (
    '            tech_type = report.get("recommendation", {}).get("technician_type", "")\n')

_TECH_TYPE_CARRY = (
    '            if tech_type:\n'
    '              result_data["technician_type"] = str(tech_type)\n')


# The specialist's mock has four canned modes, and `impaired` always reports
# "Network Tech". So the OTHER side of the P6 split — "install and repair tech", the one
# that warns about a service charge — could not be produced by the harness at all. The
# CUJ asserting it was therefore testing something the fixture cannot generate: it
# failed for that reason, not because the ladder was wrong.
#
# The real specialist reports either value — its own instruction lists both — so this is
# a fixture gap. Patched into the BUILT copy for the same reason the carry below is:
# `flows-sdk/substrate` is the source of truth and stays byte-for-byte untouched.
_TECH_MOCK_HELPER = '''def _apply_tech_override(report, state):
  """Let the harness name the technician type, so both sides of P6 are drivable."""
  wanted = _parse_query_param(str(state.get("mock_config_string") or ""),
                              "technician_type")
  if not wanted:
    return report
  head, sep, tail = report.partition('"technician_type":')
  if not sep:
    return report
  _old, close, rest = tail.partition('",')
  if not close:
    return report
  return head + sep + ' "' + wanted.replace('"', "") + close + rest


'''
_TECH_MOCK_SITES = (
    ('return {"response": _IMPAIRED_REPORT}',
     'return {"response": _apply_tech_override(_IMPAIRED_REPORT, state)}'),
    ('return {"response": report}',
     'return {"response": _apply_tech_override(report, state)}'),
)
_TECH_MOCK_ANCHOR = "def fake_tool_call("


def allow_mocking_technician_type(out_dir: str) -> None:
  """Teach the built network-specialist fake to honour a `technician_type` mock key."""
  path = os.path.join(out_dir, "tools", "network_specialist_agent_as_a_tool",
                      "tool_fake_config", "code_block", "python_code.py")
  if not os.path.exists(path):
    raise SystemExit(
        "build: the network specialist's fake is not where it was grafted to. Without "
        "it the install-and-repair half of the P6 split cannot be driven at all.")
  with open(path) as fh:
    src = fh.read()
  if "_apply_tech_override" in src:
    return
  for old, _new in _TECH_MOCK_SITES + ((_TECH_MOCK_ANCHOR, ""),):
    if src.count(old) != 1:
      raise SystemExit(
          f"build: expected exactly one {old!r} in the network specialist's fake, found "
          f"{src.count(old)}. Re-anchor rather than dropping the patch, or the "
          "install-and-repair CUJ silently goes back to testing nothing.")
  for old, new in _TECH_MOCK_SITES:
    src = src.replace(old, new)
  src = src.replace(_TECH_MOCK_ANCHOR, _TECH_MOCK_HELPER + _TECH_MOCK_ANCHOR, 1)
  try:
    compile(src, path, "exec")
  except SyntaxError as exc:
    raise SystemExit(
        f"build: the technician-type mock patch produced uncompilable Python at line "
        f"{exc.lineno} — {exc.msg}.") from exc
  with open(path, "w") as fh:
    fh.write(src)
  print("  technician mock  : honours technician_type=")


# `reboot` is the ONE carried tool the source ships with no `toolFakeConfig`. Two
# consequences, both bad: a drive with `use_tool_fakes=True` still reaches the real
# Convoy API, and of the tool's three outcomes only whichever one the live backend
# happens to give is reachable. D4's whole point is that the other two exist, so without
# this the failure ladder could never be driven — the same trap the technician split was
# in, where a CUJ asserted something the fixture could not produce.
#
# Written into the BUILT app only; `flows-sdk/substrate` stays untouched.
_REBOOT_FAKE = '''from typing import Any

_VALID = ("success", "timeline_blocked", "error")


def _parse_query_param(query_string: str, key: str) -> Optional[str]:
  """Last value for `key` in an `a=b&c=d` string, or None. No imports: the host bans them."""
  value = None
  for pair in str(query_string).split("&"):
    if not pair:
      continue
    name, sep, raw = pair.partition("=")
    if sep and name.strip() == key:
      value = raw.strip()
  return value


def fake_tool_call(tool: Tool, input: dict[str, Any], callback_context: CallbackContext) -> Optional[dict[str, Any]]:
    # pylint: disable=missing-function-docstring,undefined-variable,line-too-long
  """Mock `reboot`. Mode from `reboot_status=` in mock_config_string; default success.

  Mirrors the real tool's three documented shapes so the rung's `success_check` is
  reading the same key live as it would in production.
  """
  state = callback_context.state
  mode = _parse_query_param(str(state.get("mock_config_string") or ""), "reboot_status")
  mode = (mode or "success").strip().lower()
  if mode not in _VALID:
    mode = "success"
  if mode == "success":
    return {"status": "success", "success": True,
            "message": "Reboot command successfully sent to the gateway."}
  if mode == "timeline_blocked":
    return {"status": "timeline_blocked", "success": False,
            "message": "A recent restart was found on the provided mac address."}
  return {"status": "error", "success": False,
          "message": "Convoy could not be reached."}
'''


def install_reboot_fake(out_dir: str) -> None:
  """Give the built `reboot` tool a fake, so all three of its outcomes are drivable."""
  tool_dir = os.path.join(out_dir, "tools", "reboot")
  meta = os.path.join(tool_dir, "reboot.json")
  if not os.path.exists(meta):
    raise SystemExit(
        "build: `reboot` was not grafted, so its fake cannot be installed and the D4 "
        "failure ladder cannot be driven. Check CARRIED_TOOLS.")
  rel = "tools/reboot/tool_fake_config/code_block/python_code.py"
  code = os.path.join(out_dir, rel)
  os.makedirs(os.path.dirname(code), exist_ok=True)
  with open(code, "w") as fh:
    fh.write(_REBOOT_FAKE)
  with open(meta) as fh:
    spec = json.load(fh)
  if "toolFakeConfig" in spec:
    raise SystemExit(
        "build: `reboot` now ships its own toolFakeConfig upstream. Drop this patch and "
        "use theirs rather than silently overwriting it.")
  spec["toolFakeConfig"] = {"codeBlock": {"pythonCode": rel}, "enableFakeMode": True}
  with open(meta, "w") as fh:
    json.dump(spec, fh, indent=2)
  print("  reboot mock      : honours reboot_status=")


def teach_context_fake_the_accounts(out_dir: str) -> None:
  """DEMO build: let `fetch_customer_context`'s fixture read the scenario off the ACCOUNT.

  The fixture already keys the MAC it hands back on the account number (`_MOCK_MACS`),
  which is why a console caller can now reach the missing-hardware journey just by saying
  8344200010126021. What it keys on `mock_config_string` is the account STANDING -- and a
  console session has no way to set that variable, so `context_status=suspended` was the
  one recorded outcome the console could not reach.

  The obvious fix was for `resolve_account_context` to write the scenario into state
  before calling this tool. It does write it, and it does not work: MEASURED on
  9a503448, account 8069100020078787 (the `account_suspended` binding) swept normally and
  the engine log shows the gate completing with the restricted branch's outputs absent.
  A state write inside a tool body is not visible to a NESTED tool call in the same turn.
  So the scenario has to be somewhere the callee can see by itself, and the account number
  is the only thing that reaches it.

  Patched onto the BUILT copy of the fixture, before `bake_demo_fixtures` inlines it -- the
  ordering `allow_mocking_technician_type` already relies on, and for the same reason: bake
  first and the unpatched copy is what ends up in the body, so the patch looks applied on
  disk and does nothing at runtime.

  The seeded value still wins, so `--cuj`, `--var` and the eval harness are unaffected;
  this is only the default a console caller cannot otherwise supply.
  """
  path = os.path.join(out_dir, "tools", "fetch_customer_context", "tool_fake_config",
                      "code_block", "python_code.py")
  if not os.path.exists(path):
    raise SystemExit(
        "build: `fetch_customer_context` has no fixture in the built app, so the demo "
        "account gate has nothing to answer from. It is grafted with the tool; check "
        "CARRIED_TOOLS.")
  with open(path) as fh:
    src = fh.read()
  if "_DEMO_ACCOUNT_SCENARIOS" in src:
    return
  anchor = '  mock_config_string = state.get("mock_config_string") or ""\n'
  if src.count(anchor) != 1:
    raise SystemExit(
        "build: cannot teach the context fixture the account map — its "
        "`mock_config_string` read no longer matches. Re-anchor rather than dropping "
        "this, or every console account silently reads as an ordinary clear one and the "
        "restricted-account journey becomes undrivable again.")
  # Generated from `cujs.yaml` by the same function the gate's own copy comes from, so the
  # two cannot disagree about which account means what.
  scenarios = source_tools._demo_account_scenarios()  # noqa: SLF001
  patched = src.replace(
      anchor,
      "  # The scenario a console caller cannot seed, chosen by the account they gave.\n"
      "  _account_digits = \"\".join(_c for _c in account_number if _c.isdigit())\n"
      "  mock_config_string = (state.get(\"mock_config_string\")\n"
      "                        or _DEMO_ACCOUNT_SCENARIOS.get(_account_digits) or \"\")\n")
  patched = f"_DEMO_ACCOUNT_SCENARIOS = {scenarios!r}\n\n" + patched
  compile(patched, path, "exec")  # a bad patch must not deploy
  with open(path, "w") as fh:
    fh.write(patched)
  print(f"  context mock     : honours {len(scenarios)} account -> scenario bindings")


def bake_demo_fixtures(out_dir: str) -> int:
  """DEMO build: bake every carried tool's fixture into its BODY.

  A tool fake is a SESSION setting -- `use_tool_fakes` rides on SessionConfig and is
  asked for per call -- so the CES console never fires one, and an app opened there talks
  to the live backends. Setting `goldenEvaluationToolCallBehaviour: FAKE` on the app does
  not change that; measured, the console path still reached the real hub. The only way to
  get fixture behaviour with no cooperation from the caller is for the tool ITSELF to
  answer, so this inlines the fixture as the first statement of the body.

  Opt-in and never for anything a real caller reaches: it hard-wires recorded answers.
  Push it under its own display name.

  CALLED FROM `main()`, AND IT HAS TO BE. This was a block inside `graft_substrate`, and
  from there it ran BEFORE the three functions that produce the fixtures it bakes. It
  also read every fixture out of `SOURCE`, so nothing written at build time could reach
  it. Two demo defects fell out of that, both silent:

    * `reboot` is the one carried tool the source ships with NO `toolFakeConfig` --
      `install_reboot_fake` writes it, into `out_dir`. Baking first, from SOURCE, found
      nothing to bake, so a console demo that reached the reboot action called the real
      Convoy API and failed.
    * `allow_mocking_technician_type` teaches the network specialist's fake to honour
      `technician_type=`, patching the BUILT copy. Baking from SOURCE inlined the
      unpatched one, so the install-and-repair half of the P6 split was unreachable in
      the demo -- the exact gap that patch exists to close.

  So: fixtures come from `out_dir` when they are there (SOURCE is the fallback for a tool
  whose fake was only ever grafted), and this runs last.
  """
  import ast as _ast  # noqa: PLC0415
  baked = 0
  done: set[str] = set()
  for name in source_tools.CARRIED_TOOL_META:
    body_p = os.path.join(out_dir, "tools", name, "python_function", "python_code.py")
    rel = os.path.join("tools", name, "tool_fake_config", "code_block", "python_code.py")
    fake_p = os.path.join(out_dir, rel)
    if not os.path.exists(fake_p):
      fake_p = os.path.join(SOURCE, rel)
    if not (os.path.exists(body_p) and os.path.exists(fake_p)):
      continue
    with open(body_p) as fh:
      body = fh.read()
    if "_DEMO_FIXTURE" in body:
      done.add(name)  # already baked (a re-run over the same dir); still covered
      continue
    try:
      tree = _ast.parse(body)
    except SyntaxError:
      continue
    fn = next((n for n in tree.body
               if isinstance(n, _ast.FunctionDef) and n.name == name), None)
    if not fn:
      continue
    # Insert AFTER the docstring, so the fixture runs before any real work but the
    # signature CES derives its schema from is untouched.
    first = fn.body[0]
    is_doc = (isinstance(first, _ast.Expr)
              and isinstance(getattr(first, "value", None), _ast.Constant)
              and isinstance(first.value.value, str))
    at = (fn.body[1].lineno - 1) if (is_doc and len(fn.body) > 1) else (first.lineno - 1)
    with open(fake_p) as fh:
      fixture = fh.read()
    lines = body.splitlines(keepends=True)
    probe = ("  _fx = _demo_fixture(locals())\n"
             "  if isinstance(_fx, dict):\n"
             "    return _fx\n")
    lines.insert(at, probe)
    tail = (
        "\n\n# --- fixture, inlined so the tool answers without a session flag ---\n"
        "_DEMO_FIXTURE = True\n"
        "from typing import Any, Optional  # noqa: F401,E402\n"
        "Tool = globals().get('Tool', object)\n"
        "CallbackContext = globals().get('CallbackContext', object)\n"
        # Word-bounded and global, for the reason the leg inlining is: a BUILT fixture
        # may already carry a wrapper that calls the original through
        # `_fake_tool_call_inner`, and renaming only the `def` would leave that pointing
        # at a name that no longer exists.
        + re.sub(r"\bfake_tool_call\b", "_demo_fixture_inner", fixture)
        + "\n\ndef _demo_fixture(args):\n"
          "  try:\n"
          "    out = _demo_fixture_inner(None, dict(args), context)  # noqa: F821\n"
          "  except Exception:\n"
          "    return None\n"
          "  if isinstance(out, dict) and \"success\" not in out:\n"
          "    out[\"success\"] = out.get(\"status\") != \"error\"\n"
          "  return out\n")
    compile("".join(lines) + tail, body_p, "exec")  # a bad bake must not deploy
    with open(body_p, "w") as fh:
      fh.write("".join(lines) + tail)
    baked += 1
    done.add(name)
  # `reboot` is a canary, because it is the tool this ordering got wrong: its fake
  # exists only in `out_dir` (the source ships none), so a bake that runs too early or
  # reads from SOURCE finds nothing and the console demo calls the real Convoy API.
  if "reboot" not in done:
    raise SystemExit(
        "build: the demo bake did not reach `reboot`. Its fake is written by "
        "`install_reboot_fake` into the BUILT app, so this has to run after it and read "
        "fixtures from `out_dir` — otherwise a console reboot hits the live backend.")
  # `fetch_customer_context` is the other one, and it is load-bearing in a way `reboot` is
  # not: the demo build's `resolve_account_context` no longer answers the account step from
  # a canned constant, it falls through to its real body and lets THIS promotion answer.
  # So a bake that misses it does not degrade one branch, it takes the whole demo down at
  # the first step -- the gate reaches the context hub, which errors for every account in
  # dev, and the caller is handed off before a single check runs. That is the failure this
  # promotion exists to remove, and it would return silently: the build succeeds, the app
  # deploys, and only a live drive shows it.
  if "fetch_customer_context" not in done:
    raise SystemExit(
        "build: the demo bake did not reach `fetch_customer_context`, which the demo "
        "account gate now depends on. Without it `resolve_account_context` calls the real "
        "context hub and every console conversation dies on the account step.")
  print(f"  demo fixtures    : {baked} baked into bodies")
  return baked


def carry_technician_type(out_dir: str) -> None:
  """Make the grafted fan-out RETURN the technician type it already reads.

  The network specialist reports which kind of technician is needed. The source's
  fan-out reads that value, uses it only to decide that the line is impaired, and then
  drops it — so the P6 split ("network tech" gets no-charge wording, "install and
  repair tech" gets the service-charge heads-up) had nothing to split on, and the
  dispatch codes sent to the appointment queue were a hard-coded default.

  Patched here rather than in the vendored export, which is the source of truth and
  stays untouched, and rather than in an override body, which would fork a 250-line
  tool to add one line and then rot against it.

  The anchor is asserted. A silent no-op here would leave the split looking wired while
  the value it depends on never arrives, which is the failure this whole change is
  about.
  """
  path = os.path.join(out_dir, "tools", "run_comcast_diagnostics",
                      "python_function", "python_code.py")
  with open(path) as fh:
    src = fh.read()
  if _TECH_TYPE_CARRY in src:
    return
  if src.count(_TECH_TYPE_ANCHOR) != 1:
    raise SystemExit(
        "build: cannot carry technician_type — the fan-out's network branch no longer "
        "matches the expected line. The source tool has changed; re-anchor the patch "
        "rather than dropping it, or the P6 technician split silently stops working.")
  patched = src.replace(_TECH_TYPE_ANCHOR, _TECH_TYPE_ANCHOR + _TECH_TYPE_CARRY)
  # Compile it. A text patch to a tool body is not checked by anything else: the build
  # only writes the file, and the offline oracles read the DAG config rather than
  # executing this. The first version of this patch emitted an `if` with no body, which
  # every gate passed and which would have crashed on the first live sweep.
  try:
    compile(patched, path, "exec")
  except SyntaxError as exc:
    raise SystemExit(
        f"build: carrying technician_type produced uncompilable Python at line "
        f"{exc.lineno} — {exc.msg}. The patch is wrong, not the source.") from exc
  with open(path, "w") as fh:
    fh.write(patched)
  print("  technician_type  : carried out of the fan-out")


# ---------------------------------------------------------------------------
# UX DEMO MODE: the specialists, in this sandbox, behind the SAME contract
# ---------------------------------------------------------------------------
#
# `--specialists=local` (with `--demo`) makes the pair run as ordinary in-sandbox tool
# calls instead of through the Cloud Run proxy -- no Cloud Run, no Firestore, no IAM --
# while the agent stays byte-for-byte the app it already was. Cloud Run is the DEFAULT and
# the deployed shape; this is the fallback for a machine that has none of that.
#
# WHY IT IS A POST-EMIT PATCH RATHER THAN A SWITCH IN `app.py`. The obvious shape is an
# env branch in `_specialists_task()` picking the local synchronous tool instead of
# `resolve_specialists_remote`. It was tried and it fails, twice over:
#
#   * The synchronous body at `source_tools._specialists_source()` had SILENTLY DIVERGED
#     from the remote contract -- it derives three of the seven declared outputs and
#     knows nothing of `activityType` / `activityCode` / `jobType` / `wifi_status`, which
#     the remote path added when the specialists' side effects were audited. Two
#     implementations of one contract drift, and nothing was watching.
#   * Hand-writing the missing keys back is how the drift happened in the first place. A
#     literal list is a second copy of the contract; the fix has to DERIVE it.
#
# So this changes neither the flow, the task, the tool names, nor the declaration. It
# changes ONE LINE inside each of the two generated wrappers: where `data` comes from.
# Everything that expresses the contract -- the `out[k] = _dig(data, 'result.k')`
# projection and the return dict, both GENERATED from `remote_tool(outputs={...})` -- is
# left exactly as emitted. Add an output to the declaration and both paths pick it up
# with nothing here to update. Nothing downstream can tell the difference either: the
# validator ran on the same App, the DAG is the same DAG, and the engine still marks the
# job pending, polls `..._status` once per turn and speaks the same `while_waiting`
# ladder, because it is still the same remote tool.
#
# The DERIVATION is lifted verbatim out of `specialist_proxy/main.py`. The proxy is the
# reference implementation of this contract, so the local path runs its actual source
# rather than a paraphrase of it -- one body of rules, two places it can run.
_LOCAL_MODE = "--specialists=local"

# THE WAIT, MADE AUDIBLE. How long a RECORDED answer pretends to take in this build, and
# nowhere else.
#
# It exists for one reason, the same one `specialist_proxy.DEMO_SWEEP_SECONDS` exists for:
# a fixture answers in microseconds, so the job lands on the first poll and a UX demo shows
# a correct verdict with none of the waiting the copy was written for -- no tick, no
# reassurance line, nothing to hear. The reassurance ladder is unreachable, which for a UX
# tool is the wrong gap to have.
#
# It reverses the note this file used to carry, and only because the shape changed under
# it. That note was right: a delay spent inside a SYNCHRONOUS poll blocks that poll rather
# than spreading across several, which is worse than the wait it imitates. What makes it
# affordable now is that the poll is
# `executionType: ASYNCHRONOUS` (see `local_specialists`), so CES defers the body, answers
# the poll at once with a `pending` placeholder and delivers the real result later as a
# completion envelope. The turn returns immediately, the inactivity ticks keep coming, and
# the sleep is spent OFF the turn instead of on it.
#
# TUNED against the app's 5s `inactivityTimeout` and the demo ladder, which is two silent
# ticks (`app._QUIET_TICKS`) and then exactly one reassurance line. The completion has to
# outlast all three draws and land on the tick after the reassurance: shorter and the
# caller never hears the line, longer and they hear silence after it. Measured over voice
# rather than computed -- see the drive recorded in the commit message.
#
# Bounded well under the CES tool-execution kill (60s by default, and this tool declares no
# `timeout`), so the body reports rather than being killed at ~19s (ces-probes 116).
_DEMO_ASYNC_SLEEP_SECONDS = 20.0

# Lifted from the proxy, in dependency order. `_derive` and `_fixture` are the two that
# produce the contract; the rest are what they call.
_PROXY_LIFTED = ("GATEWAY_VOCAB", "_NET_ALIASES", "_first_json_object",
                 "_tech_from_analysis", "_tech_from_activity", "_derive",
                 "_query", "_fixture")

_PROXY_CALL = ("      raw = getattr(tools, 'specialist_proxy_resolveSpecialists')"
               "(request)")
_PROXY_STATUS_CALL = ("      raw = getattr(tools, "
                      "'specialist_proxy_resolveSpecialistsStatus')(request)")


def _proxy_definitions(names) -> str:
  """The named top-level definitions of `specialist_proxy/main.py`, verbatim.

  Read by AST rather than copied, so the local path cannot fall behind the service: an
  edit to the proxy's rules is an edit to this build's output. A name that has moved or
  been renamed stops the build instead of quietly emitting a shorter contract.
  """
  import ast as _ast  # noqa: PLC0415
  path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "specialist_proxy", "main.py")
  with open(path) as fh:
    src = fh.read()
  found, tree = {}, _ast.parse(src)
  for node in tree.body:
    if isinstance(node, _ast.FunctionDef) and node.name in names:
      found[node.name] = _ast.get_source_segment(src, node)
    elif isinstance(node, _ast.Assign):
      for target in node.targets:
        if isinstance(target, _ast.Name) and target.id in names:
          found[target.id] = _ast.get_source_segment(src, node)
  missing = [n for n in names if n not in found]
  if missing:
    raise SystemExit(
        f"build: {_LOCAL_MODE} cannot lift {', '.join(missing)} from "
        "specialist_proxy/main.py — the proxy's derivation has been renamed or moved. "
        "Re-point the lift rather than reimplementing it locally: two copies of these "
        "rules is exactly the drift this build exists to prevent.")
  return "\n\n\n".join(found[n] for n in names)


def _keys_assigned(source: str, func: str) -> set:
  """The `out` string keys a lifted function can produce. The contract it can honour.

  Three shapes, because the two lifted functions use all three and a key this cannot see
  is a key the gate below believes nothing produces:

    out["k"] = v                       plain subscript store
    out["a"], out["b"] = code          tuple target -- the fixture's dispatch triple
    out = {"k": v, ...}                dict literal -- how `_fixture` opens

  The dict literal is the one that was missing, and missing it is not harmless. It made
  `_fixture` look as though it produced four of its seven keys, so the union at the gate
  leaned entirely on `_derive`; move a key from `_derive` into that literal and the build
  would have died claiming nothing in the proxy's rules produces it. It cannot cause a
  false PASS -- an unseen key only ever shrinks `produced` -- but a false failure on a
  correct proxy is the same amount of stopped work.

  Deliberately conservative: `ast.Constant` string keys only. A `**spread` (whose key is
  None) and a computed key are both invisible statically, and a key this cannot READ must
  not be a key it CREDITS -- silently crediting one is how a genuinely undeliverable
  output would reach the caller as a missing slot instead of a build error.
  """
  import ast as _ast  # noqa: PLC0415
  fn = next(n for n in _ast.parse(source).body
            if isinstance(n, _ast.FunctionDef) and n.name == func)
  keys = set()

  def _is_out(node) -> bool:
    """Is this the name `out`?"""
    return isinstance(node, _ast.Name) and node.id == "out"

  def _subscript(node) -> None:
    """`out["k"]`, in any context. A non-constant or non-string key is ignored."""
    if (isinstance(node, _ast.Subscript) and _is_out(node.value)
        and isinstance(node.slice, _ast.Constant)
        and isinstance(node.slice.value, str)):
      keys.add(node.slice.value)

  def _literal(node) -> None:
    """Constant string keys of a dict literal; `**spread` and computed keys skipped."""
    if isinstance(node, _ast.Dict):
      for k in node.keys:
        if isinstance(k, _ast.Constant) and isinstance(k.value, str):
          keys.add(k.value)

  for node in _ast.walk(fn):
    # `out["k"] = v`. Store context only, so `if out["k"] == ...` is not a production.
    if isinstance(node, _ast.Subscript) and isinstance(node.ctx, _ast.Store):
      _subscript(node)
    if isinstance(node, _ast.Assign):
      for target in node.targets:
        # `out["a"], out["b"], out["c"] = code` — the fixture's dispatch triple.
        if isinstance(target, _ast.Tuple):
          for el in target.elts:
            _subscript(el)
        # `out = {...}` — the whole contract in one expression, of which the two rules
        # above can see no key at all.
        elif _is_out(target):
          _literal(node.value)
    # `out: dict[str, str] = {...}` — the same statement, annotated.
    elif isinstance(node, _ast.AnnAssign) and _is_out(node.target):
      _literal(node.value)
    # `out.update({...})` — not used today, and counted so that switching to it is not
    # a silent contraction of the contract.
    elif (isinstance(node, _ast.Call) and isinstance(node.func, _ast.Attribute)
          and node.func.attr == "update" and _is_out(node.func.value) and node.args):
      _literal(node.args[0])
  return keys


# The only NEW code here: what replaces the HTTP round trip. It produces the two
# `{report, state, tool_results}` records `_derive` consumes, from an in-sandbox call
# rather than from a CES session the proxy drove — and then hands off to the proxy's own
# rules. It enumerates nothing the contract contains.
# The handle helpers, injected into BOTH wrappers: the start call packs, the poll
# unpacks. Everything below the split is the poll's alone.
_LOCAL_HANDLE = '''

def _pack(request: dict) -> str:
  """The job handle: the start call's own arguments, carried to the poll.

  A poll receives the HANDLE and nothing else — which is fine for the proxy, because it
  stored the request body against the job id, and is not fine here, because there is no
  store. Reading the session instead is NOT equivalent and the difference is not
  cosmetic: `mock_config_string` is a declared PARAM lifted from a slot, and in a demo
  build `resolve_account_context` writes one
  (`...network_status=clear...&demo_delay=on`) that never reaches `context.variables`.
  Measured: reading the session missed it, so a demo drive took the LIVE branch and
  spent 36s where the deployed app spent 8s on fixtures. The handle is the store.
  """
  import base64
  return 'local:' + base64.urlsafe_b64encode(
      _json.dumps(request, sort_keys=True).encode()).decode()


def _unpack(job_id: str) -> dict:
  """The start call's arguments, back out of the handle."""
  import base64
  head, sep, tail = str(job_id or '').partition('local:')
  if not sep:
    return {}
  try:
    got = _json.loads(base64.urlsafe_b64decode(tail.encode()).decode())
    return got if isinstance(got, dict) else {}
  except Exception:
    return {}


'''


_LOCAL_RUNNER = '''

def _local_state() -> dict:
  """This session's variables and state, flattened, as one snapshot."""
  snap: dict = {}
  for attr in ('variables', 'state'):
    box = getattr(context, attr, None)
    if box is None:
      continue
    try:
      snap.update(dict(box))
    except Exception:
      try:
        snap.update({k: box.get(k) for k in list(box.keys())})
      except Exception:
        pass
  return snap


def _agent_text(res: Any) -> str:
  """An agentTool answers under `response`, as a JSON string."""
  data = _unwrap(res)
  inner = data.get('result') if isinstance(data.get('result'), dict) else data
  raw = (inner or {}).get('response', '')
  if isinstance(raw, str):
    return raw
  try:
    return _json.dumps(raw)
  except Exception:
    return ''


def _local_specialists(job_id: str) -> dict:
  """Both specialists, IN THIS SANDBOX, answered in the poll's own envelope.

  Mirrors the proxy's `_work` branch for branch, off the SAME inputs: the start call's
  arguments, carried here in the handle (`_pack`). `mock_config_string` is the harness
  signal there and here.

  The one thing this path gets for free that the remote one had to declare: the
  specialists' `after_tool` callbacks write `activityType` / `activityCode` / `jobType`
  / `wifi_status` into THIS session, so the side effects the proxy had to carry as
  outputs simply land. They are read back as a DELTA against a snapshot taken before
  the pair ran, which is the closest thing here to the fresh session the proxy drives —
  the orchestrator's own slots are already in the snapshot and so cannot be mistaken
  for a specialist's answer.

  The proxy's recorded `demo_delay` is not spent as the proxy spends it -- as a number of
  seconds read off the request. It is spent as `DEMO_SLEEP_SECONDS` below, on the FIXTURE
  branch only, and only because this poll is deferred: `executionType: ASYNCHRONOUS` means
  the sleep is spent off the turn rather than inside it, which is the objection the earlier
  note here raised and the only thing that ever made it valid.
  """
  import concurrent.futures

  started = _unpack(job_id)
  mock_config_string = str(started.get('mock_config_string')
                           or _session_value('mock_config_string') or '')
  if _query(mock_config_string, 'network_status') or _query(
      mock_config_string, 'gateway_status'):
    # THE WAIT, MADE AUDIBLE. A recorded answer costs microseconds, so without this the
    # verdict lands on the FIRST poll and the reassurance ladder this build exists to
    # show is never reached. See `build._DEMO_ASYNC_SLEEP_SECONDS` for the tuning.
    #
    # Gated on `demo_delay`, and read exactly as the proxy reads it: the demo build's
    # scenario writes `demo_delay=on` into `mock_config_string`, `DEMO_SWEEP_SECONDS=<n>`
    # writes a number there instead, and no eval harness writes either -- so `diag_check`
    # against this build is as fast as it ever was. Honouring the number matters because
    # `source_tools.DEMO_SCENARIO` already offers it as the knob for a LONG wait, and a
    # knob that silently does nothing is worse than no knob.
    #
    # NOTHING IS CALLED HERE. This branch answers from `_fixture` and returns; ces-probes
    # 147 is the reason that matters -- a deferred body's nested `tools.` call is aborted
    # once it outlives its turn, which is exactly what deferring the LIVE branch below
    # would do to the two specialist agent calls. The fixture branch has nothing to lose.
    asked = _query(mock_config_string, 'demo_delay')
    if asked:
      import time as _time
      try:
        _time.sleep(float(asked))
      except ValueError:
        _time.sleep(DEMO_SLEEP_SECONDS)
    return {'status': 'done', 'result': _fixture(mock_config_string)}

  before = _local_state()
  pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
  try:
    net_f = pool.submit(tools.network_specialist_agent_as_a_tool,
                        {'request': 'measure line signals'})
    gw_f = pool.submit(tools.gateway_specialist_agent_as_a_tool,
                       {'request': 'triage gateway logs'})
    try:
      net_raw = net_f.result(timeout=60.0)
    except Exception:
      net_raw = None
    try:
      gw_raw = gw_f.result(timeout=60.0)
    except Exception:
      gw_raw = None
  finally:
    # `wait=False`, for the reason the proxy gives: a wedged leg must not hold the turn
    # open at the closing brace, past the timeout that was supposed to end it.
    pool.shutdown(wait=False)

  after = _local_state()
  wrote = {k: v for k, v in after.items() if before.get(k) != v}
  # The specialists' callbacks stash each inner tool call under `<tool>_response`, which
  # is where `_tech_from_analysis` looks for the recommendation.
  results = [{'response': v} for k, v in after.items()
             if str(k).endswith('_response')]
  net = {'report': _first_json_object([_agent_text(net_raw)]), 'state': wrote,
         'tool_results': results}
  gw = {'report': _first_json_object([_agent_text(gw_raw)]), 'state': wrote,
        'tool_results': results}
  return {'status': 'done', 'result': _derive(net, gw)}
'''


# The operation the specialist fan-out starts, as declared in `journeys/diagnostics_sweep`
# and generated into the toolset's spec. Named here because two files below have to find
# the same operation: the spec it is a path in, and the wrapper that calls it.
_PROXY_OP = "resolveSpecialists"

# The three lines added to the generated start wrapper, and where they go. The wrapper
# fills `request` from its own parameters and then calls the toolset, so the app id has to
# be placed in between -- it is not one of the tool's parameters and must not become one:
# the engine fills those from slots, and this value belongs to the DEPLOYMENT rather than
# to the conversation.
_APP_ANCHOR = "  data = None\n"
_APP_SEND = f'''\
  # WHOSE specialists to open. The proxy opens a CES session at each specialist and a
  # session belongs to an APP, so a request naming none is answered out of whichever app
  # the service itself was deployed against. Nothing downstream can tell: a pair that
  # never opened derives a HEALTHY line out of two empty legs. The id cannot be known
  # here -- CES assigns it at push time and a tool body cannot ask which app is running
  # it -- so it is read from the session (`build.py --ces-app` bakes the default). Unset
  # means send nothing, which is exactly the fallback every build had before.
  ces_app = _session_value({build_config.CES_APP_VARIABLE!r})
  if ces_app:
    _place(request, 'app', str(ces_app))
'''


def tell_the_proxy_which_app(out_dir: str) -> None:
  """Make the specialist caller name the app whose specialists should answer.

  The proxy is MULTI-TENANT and the agent is what makes that true: the service reads an
  optional `app` off the request and only falls back to its own default when none is
  sent. Nothing else can supply it. The value is a deployment fact rather than a
  conversational one, so it rides a session variable instead of a tool parameter, and an
  unset variable sends nothing -- an older proxy, or a build with no `--ces-app`, behaves
  precisely as it did.

  Both ends are patched here because the generated pair has to agree: CES builds the HTTP
  request from the toolset's spec, so a body field the spec does not declare is dropped
  before it ever leaves the sandbox.
  """
  spec_p = os.path.join(out_dir, "toolsets", "specialist_proxy", "open_api_toolset",
                        "open_api_schema.yaml")
  start_p = os.path.join(out_dir, "tools", "resolve_specialists_remote",
                         "python_function", "python_code.py")
  for path in (spec_p, start_p):
    if not os.path.exists(path):
      raise SystemExit(
          f"build: cannot tell the proxy which app to use — {path} is missing. The "
          "specialist toolset and its generated caller are what carry the app id; "
          "without them every specialist call lands on the proxy's own default app.")

  import yaml  # noqa: PLC0415
  with open(spec_p) as fh:
    spec = yaml.safe_load(fh)
  try:
    props = (spec["paths"][f"/{_PROXY_OP}"]["post"]["requestBody"]["content"]
             ["application/json"]["schema"]["properties"])
  except (KeyError, TypeError) as exc:
    raise SystemExit(
        f"build: the generated spec has no {_PROXY_OP} request body to add `app` to "
        f"({exc}). The SDK's remote-tool spec has changed shape; re-anchor this rather "
        "than dropping it, or the caller sends a field CES will strip.") from exc
  props["app"] = {"type": "string"}
  with open(spec_p, "w") as fh:
    yaml.safe_dump(spec, fh, sort_keys=False)

  with open(start_p) as fh:
    start = fh.read()
  for helper in ("def _session_value(", "def _place("):
    if helper not in start:
      raise SystemExit(
          f"build: the generated caller no longer defines `{helper.split()[1]}`, which "
          "is what reads the app id out of the session and places it in the request. "
          "Re-anchor this against the SDK's current wrapper template.")
  if start.count(_APP_ANCHOR) != 1:
    raise SystemExit(
        "build: cannot place the app id in the generated caller; the anchor "
        f"{_APP_ANCHOR.strip()!r} appears {start.count(_APP_ANCHOR)} times, not once.")
  start = start.replace(_APP_ANCHOR, _APP_SEND + _APP_ANCHOR, 1)
  try:
    compile(start, start_p, "exec")
  except SyntaxError as exc:
    raise SystemExit(
        f"build: telling the proxy which app produced uncompilable Python at line "
        f"{exc.lineno} — {exc.msg}.") from exc
  with open(start_p, "w") as fh:
    fh.write(start)


def local_specialists(out_dir: str, config: build_config.BuildConfig) -> None:
  """UX DEMO MODE: run the specialist pair in-sandbox, behind the same remote contract.

  See the block comment above. Patches the two generated wrappers and nothing else, so
  the emitted flow, DAG and declaration are identical to the default build's.
  """
  import ast as _ast  # noqa: PLC0415

  def _wrapper(name):
    return os.path.join(out_dir, "tools", name, "python_function", "python_code.py")

  start_p, status_p = (_wrapper("resolve_specialists_remote"),
                       _wrapper("resolve_specialists_remote__status"))
  for path in (start_p, status_p):
    if not os.path.exists(path):
      raise SystemExit(
          f"build: {_LOCAL_MODE} found no wrapper at {path}. The remote tool is what "
          "this mode re-points; without it there is nothing to make local.")
  with open(start_p) as fh:
    start = fh.read()
  with open(status_p) as fh:
    status = fh.read()

  # THE CONTRACT, read off the emitted wrapper rather than restated. These lines are
  # generated from `remote_tool(outputs={...})`, so they are the declaration itself.
  declared = set(re.findall(r"out\['(\w+)'\] = _dig\(data, 'result\.\w+'\)", status))
  if not declared:
    raise SystemExit(
        f"build: {_LOCAL_MODE} could not read the declared outputs out of the status "
        "wrapper. The generated projection has changed shape; re-anchor it rather than "
        "hand-listing the outputs, which is how the local and remote paths drifted "
        "apart the last time.")

  lifted = _proxy_definitions(_PROXY_LIFTED)
  # THE ANTI-DRIFT GATE. Every declared output must be one the lifted rules can produce.
  # This is the check that was missing: the old local body derived three of seven and
  # the build was perfectly happy.
  #
  # A UNION over both rules, because either one can be the branch a call takes: `_derive`
  # on a live account, `_fixture` when the harness seeds `mock_config_string`. It is not
  # an intersection, so this does not assert that both cover the contract -- it asserts
  # that the contract is not asking for something neither has heard of. `_keys_assigned`
  # has to see dict literals for the `_fixture` half to carry its weight; see its
  # docstring for what happened when it did not.
  produced = _keys_assigned(lifted, "_derive") | _keys_assigned(lifted, "_fixture")
  short = sorted(declared - produced)
  if short:
    raise SystemExit(
        f"build: {_LOCAL_MODE} cannot honour the remote contract — "
        f"{', '.join(short)} is declared by resolve_specialists_remote but nothing in "
        "the proxy's derivation produces it. The service and its rules have diverged; "
        "fix specialist_proxy/main.py, which both paths run.")

  if _PROXY_CALL not in start or _PROXY_STATUS_CALL not in status:
    raise SystemExit(
        f"build: {_LOCAL_MODE} cannot find the proxy call in the generated wrappers. "
        "The SDK's wrapper template has changed; re-anchor the one line this mode "
        "replaces rather than rewriting the body, which would fork the contract.")

  # The START call. Sub-second and it must stay that way: the engine's whole remote
  # shape rests on it, and `ContextGate` speaks the bridge line on this same turn.
  start = start.replace(
      _PROXY_CALL,
      "      # UX DEMO MODE: no proxy. The work happens on the POLL, so this turn is\n"
      "      # as cheap as the real start call (measured 0.15-0.21s) and the caller\n"
      "      # still hears ContextGate's line before any waiting begins. The handle\n"
      "      # carries this call's arguments, which is the proxy's job store's job.\n"
      "      raw = {'jobId': _pack(request)}")
  start_anchor = "def resolve_specialists_remote("
  if start.count(start_anchor) != 1:
    raise SystemExit(f"build: {_LOCAL_MODE} cannot place the handle helpers; anchor moved.")
  start = start.replace("from typing import Any",
                        "import json as _json\nfrom typing import Any", 1)
  start = start.replace(start_anchor,
                        _LOCAL_HANDLE.strip() + "\n\n\n" + start_anchor, 1)

  # The POLL. `_json` and the lifted rules go in above the entrypoint; CES derives the
  # tool's schema from the `def`, which is untouched.
  status = status.replace("from typing import Any",
                          "import json as _json\nfrom typing import Any", 1)
  status = status.replace(
      _PROXY_STATUS_CALL,
      "      # UX DEMO MODE: no proxy. Same rules, same inputs, run here — see build.py.\n"
      "      raw = _local_specialists(jobId)")
  # `--specialist-diag`: carry the specialists' raw answers back in the result, under
  # underscore keys. Undeclared, so `_dig` never reads them and no slot can fill from
  # one — they ride out in the wrapper's `response`, which is where the proxy already
  # puts `_net_seconds` / `_gw_seconds`. For diagnosing a status you did not expect;
  # raw agent text is not something to ship.
  #
  # PATCHED INTO THE RUNNER, before the runner is inserted below. It used to patch
  # `status` here instead, and `status` does not contain the runner yet at this point --
  # so `SPIKE_LOCAL_DIAG=1` matched nothing, changed nothing, and reported success. The
  # count assertion is the part that would have caught it: a text patch that silently
  # applies to nothing is the same failure as a flag that silently does nothing.
  runner = _LOCAL_RUNNER.strip()
  if config.specialist_diag:
    diag_anchor = "  return {'status': 'done', 'result': _derive(net, gw)}"
    if runner.count(diag_anchor) != 1:
      raise SystemExit(
          "build: --specialist-diag cannot find the local runner's return; re-anchor it "
          "rather than dropping the patch, or the flag goes back to doing nothing.")
    runner = runner.replace(
        diag_anchor,
        "  _out = _derive(net, gw)\n"
        "  _out['_net_text'] = _agent_text(net_raw)[:600]\n"
        "  _out['_gw_text'] = _agent_text(gw_raw)[:600]\n"
        "  _out['_net_report'] = str(net['report'])[:300]\n"
        "  _out['_gw_report'] = str(gw['report'])[:300]\n"
        "  _out['_wrote'] = str(sorted(wrote))[:400]\n"
        "  return {'status': 'done', 'result': _out}")

  anchor = "def resolve_specialists_remote__status("
  if status.count(anchor) != 1:
    raise SystemExit(f"build: {_LOCAL_MODE} cannot place the local body; anchor moved.")
  status = status.replace(
      anchor,
      "# " + "-" * 72 + "\n"
      "# Lifted VERBATIM from specialist_proxy/main.py — the reference implementation\n"
      "# of this contract. Not a copy to maintain: build.py re-reads it every build.\n"
      "# " + "-" * 72 + "\n"
      + lifted + "\n\n"
      + _LOCAL_HANDLE.strip() + "\n\n\n"
      + f"DEMO_SLEEP_SECONDS = {_DEMO_ASYNC_SLEEP_SECONDS!r}\n\n\n"
      + runner + "\n\n\n" + anchor, 1)

  for path, src in ((start_p, start), (status_p, status)):
    try:
      compile(src, path, "exec")
    except SyntaxError as exc:
      raise SystemExit(
          f"build: {_LOCAL_MODE} produced uncompilable Python in {os.path.basename(path)} "
          f"at line {exc.lineno} — {exc.msg}.") from exc
    with open(path, "w") as fh:
      fh.write(src)

  # THE POLL IS DEFERRED. `executionType: ASYNCHRONOUS` on the STATUS resource, and only
  # in this build -- the default one never reaches this function at all, and its two
  # wrappers are emitted with no `executionType` key, which is SYNCHRONOUS.
  #
  # This is what buys the sleep above. CES answers a deferred call at once with
  # `{"result": "pending"}` and delivers the body's real return later, as a
  # `<context>function [...] completed with response {...}</context>` envelope on a
  # subsequent turn (ces-probes 116); on voice an inactivity tick supplies that turn with
  # no caller input at all (ces-probes 117). So the poll's turn ends immediately, the
  # ticks keep arriving, `while_waiting` drains a line per tick, and the verdict lands a
  # few turns in -- the real cadence, off a fixture, with no Cloud Run anywhere.
  #
  # THE ENGINE NEEDS NO TELLING, and that is the property that makes this a one-key patch
  # rather than a config change. The placeholder is what it keys on, twice over:
  # `slot_intake._is_async_placeholder` refuses to map outputs from it, and the engine's
  # post-executor `_is_async_pending` opens the wait the `Specialists` task already
  # declares. The remote mark set by the START call is REPLACED by that wait, so
  # `_remote_turn` finds nothing in flight and the job is polled exactly ONCE. When the
  # envelope lands, `before_model` routes it back through intake under the status tool's
  # own name -- which is registered against this same task (`executor_tasks[
  # remote["status_tool"]]`), so the `status: done` payload completes it by the ordinary
  # path, with the ordinary output mapping.
  #
  # It cannot be done on the START wrapper instead. `_intake_remote` reads the job handle
  # out of the start's return, and a placeholder carries none: the task fails at once with
  # `remote_bad_handle` and the caller hears the `on_exhaust` line.
  status_json = os.path.join(out_dir, "tools", "resolve_specialists_remote__status",
                             "resolve_specialists_remote__status.json")
  if not os.path.exists(status_json):
    raise SystemExit(
        f"build: {_LOCAL_MODE} found no status tool resource at {status_json}; the poll "
        "cannot be made asynchronous and the demo would answer instantly.")
  with open(status_json) as fh:
    status_res = json.load(fh)
  status_res["executionType"] = "ASYNCHRONOUS"
  with open(status_json, "w") as fh:
    json.dump(status_res, fh, indent=2)

  # Nothing calls the proxy now, so the resources that point at it come out. Belt and
  # braces for "no Cloud Run dependency": an unreferenced toolset is inert, but an
  # ABSENT one cannot be reached by a stray edit either.
  dropped = []
  toolset_dir = os.path.join(out_dir, "toolsets", "specialist_proxy")
  leftover = _grep_toolset(out_dir, "specialist_proxy")
  if leftover:
    raise SystemExit(
        f"build: {_LOCAL_MODE} still has callers of the specialist_proxy toolset "
        f"({', '.join(leftover)}); it cannot be dropped and the build would ship a "
        "Cloud Run dependency it claims not to have.")
  if os.path.isdir(toolset_dir):
    shutil.rmtree(toolset_dir)
    dropped.append("toolset")
  env_p = os.path.join(out_dir, "environment.json")
  if os.path.exists(env_p):
    with open(env_p) as fh:
      env = json.load(fh)
    for section in env.values():
      if isinstance(section, dict) and section.pop("specialist_proxy", None) is not None:
        dropped.append("environment url")
    with open(env_p, "w") as fh:
      json.dump(env, fh, indent=2)

  print(f"  UX demo mode     : specialists run in-sandbox behind the same contract "
        f"({len(declared)} declared outputs, all derived)")
  print(f"  poll             : ASYNCHRONOUS, {_DEMO_ASYNC_SLEEP_SECONDS:g}s recorded "
        f"latency on the fixture path (demo_delay), so the wait is audible")
  if dropped:
    print(f"  proxy resources  : dropped ({', '.join(sorted(set(dropped)))})")


def _grep_toolset(out_dir: str, toolset: str) -> list:
  """Tool bodies that still call `tools.<toolset>_*`."""
  hits = []
  root = os.path.join(out_dir, "tools")
  for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
    path = os.path.join(root, name, "python_function", "python_code.py")
    if not os.path.exists(path):
      continue
    with open(path) as fh:
      if re.search(rf"tools[,\s]*['\"]?{re.escape(toolset)}_\w+", fh.read()):
        hits.append(name)
  return hits


def demo_sweep(out_dir: str, config: build_config.BuildConfig) -> None:
  """Swap the real fan-out for a mock_config_string-driven stub (DEMO builds only).

  The per-tool `toolFakeConfig` mocks only engage when the caller sets a session-level
  fakes flag, which a console session does not — so on the shipped build every
  interactive conversation reaches for unreachable backends and lands on the
  "couldn't get all the info I need" rung. This makes every scenario reachable by
  editing one session variable.
  """
  import demo_stub
  path = os.path.join(out_dir, "tools", "run_comcast_diagnostics",
                      "python_function", "python_code.py")
  body = demo_stub.DEMO_SWEEP
  # The real fan-out reaches Comcast through an auth proxy that is not routable from dev,
  # so a demo build always measures ~4s on the sweep turn and every timing in this repo
  # rests on that number. `--sweep-delay` makes the stub take as long as the real sweep is
  # believed to, which is the only way to see what a caller actually sits through - and
  # the only way to prove that a change meant to HIDE that wait really hides it.
  #
  # Injected as a constant rather than read from a session variable: in a demo build only
  # BAKED defaults reach the tool (measured - a session-level mock_config_string does
  # not), so a session knob would look wired and silently do nothing.
  if config.sweep_delay:
    marker = "_SWEEP_DELAY_S = 0.0"
    if marker not in body:
      raise SystemExit("build: cannot inject --sweep-delay; the stub constant moved.")
    body = body.replace(marker, f"_SWEEP_DELAY_S = {float(config.sweep_delay)}")
  with open(path, "w") as fh:
    fh.write(body)
  print("  demo build       : diagnostics resolved from mock_config_string"
        + (f" (+{build_config.format_seconds(config.sweep_delay)}s simulated sweep)"
           if config.sweep_delay else ""))


def check_cujs(out_dir: str, cujs) -> list:
  """Every CUJ variable must be one the emitted app declares.

  Without this the mismatch is only found by whoever runs `--cuj <that one>`, which
  is how an undeclared variable sat in the file unnoticed. Read-only: the defaults
  are written later, and only for the CUJ actually asked for.
  """
  with open(os.path.join(out_dir, "app.json")) as fh:
    declared = {v["name"] for v in json.load(fh).get("variableDeclarations", [])}
  problems = []
  for cuj in cujs:
    missing = sorted(set(cuj.variables) - declared)
    if missing:
      problems.append(f"cuj {cuj.name}: undeclared {', '.join(missing)}")
  return problems


def main() -> int:
  ap = argparse.ArgumentParser(
      formatter_class=argparse.RawDescriptionHelpFormatter,
      description=__doc__.split("\n\n")[0])
  build_config.add_arguments(ap)
  args = ap.parse_args()

  # RESOLVED, FROZEN, AND ONLY THEN IMPORTED. `import app` constructs the whole `App`,
  # which emits every tool body from this config, so a switch decided after this line
  # cannot reach the artifact. That is not hypothetical: `--demo` used to set `SPIKE_DEMO`
  # here, four lines below the import, and the account -> scenario bindings it was
  # supposed to bake were emitted before it ever ran -- a build that recognised a test
  # account and then routed it to whatever the live hub said. `activate` refuses a late
  # config now, so the ordering cannot rot back.
  build_config.reject_retired_env()
  config = build_config.activate(build_config.resolve(args))
  from app import app  # noqa: PLC0415

  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  if errors:
    print(f"\n{len(errors)} validation error(s) — not emitting.")
    return 1

  out = os.path.abspath(args.out)
  if os.path.isdir(out):
    shutil.rmtree(out)
  flows.build_app(app, out)
  print(f"emitted: {out}")

  report = graft_substrate(out, config)
  carry_technician_type(out)
  allow_mocking_technician_type(out)
  install_reboot_fake(out)
  # AFTER the three above: it bakes the fixtures they write, and it used to run before
  # them. See `bake_demo_fixtures`. Before `demo_sweep`, which replaces the sweep's whole
  # body -- baking one into it and then overwriting it is wasted work either way.
  if config.demo:
    # Before the bake, which inlines what this writes. See its docstring.
    teach_context_fake_the_accounts(out)
    bake_demo_fixtures(out)
  # AFTER the graft (it needs the emitted wrappers) and after the fixture bake, whose
  # `CARRIED_TOOL_META` walk does not touch these two. Independent of `--demo`, which
  # replaces the SWEEP; this replaces where the SPECIALISTS run.
  if config.local_specialists:
    local_specialists(out, config)
  else:
    # Only where there IS a proxy: local mode drops the toolset entirely, so there would
    # be no spec to declare the field in and nothing to send it to.
    tell_the_proxy_which_app(out)
    print(f"  proxy app        : {config.ces_app or 'unset (the proxy falls back)'}")
  if config.demo:
    demo_sweep(out, config)
  patch_app_json(out)
  tools = patch_root_agent(out, app)

  cujs = flows.load_cujs(start=os.path.dirname(os.path.abspath(__file__)))
  undeclared = check_cujs(out, cujs)
  if undeclared:
    for line in undeclared:
      print(f"  ERROR            : {line}")
    return 1

  if config.cuj:
    if not config.demo:
      print("  WARNING          : --cuj without --demo; a console session does not "
            "fire the tool fakes, so the seeded variables will have no effect.")
    cuj = cujs[config.cuj]
    written = flows.apply_to_app_dir(out, cuj)
    print(f"  cuj baked in     : {cuj.name} ({', '.join(written)})")
  print(f"  cujs checked     : {len(cujs.names())}")

  print(f"  toolsets grafted : {len(report['toolsets'])}")
  print(f"  tool metadata    : {len(report['tool_meta'])}")
  if report.get("desynced"):
    print(f"  async -> sync    : {sorted(report['desynced'])}  "
          f"(nested tool call; see ces-probes/112)")
  print(f"  specialist agents: {report['agents']}")
  print(f"  root agent tools : {len(tools)}")
  # AFTER every step that rewrites a tool body, because it replaces the engine's module
  # with a base64 blob: a later text patch would either miss it or corrupt it. Returns what
  # it actually DID, which is not the same as what was asked for -- see `engine_packing`.
  packing_state = engine_packing.pack_app_dir(out, config)
  # LAST, and unconditional. A built app dir used to say nothing about how it was made,
  # which is why `--demo` could promise a demo and emit a live one for months without
  # anybody being able to tell by looking. `tests/config_check.py` reads this back.
  build_config.write_manifest(out, config, packing_state)
  print(f"  build manifest   : {config.summary()}")
  if report["missing"]:
    print("  MISSING:", report["missing"])
    return 1
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
