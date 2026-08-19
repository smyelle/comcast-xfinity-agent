#!/usr/bin/env python3
"""Offline proof that every build switch is a flag, and that a flag reaches the artifact.

THE INCIDENT THIS EXISTS FOR. `--demo` promised a console-demoable build and rewrote one
tool. Everything else that makes a demo work -- the account -> scenario bindings, the
fixture bake, the faked sweep legs -- hung off a separate `SPIKE_DEMO` environment
variable that never appeared in `--help`. So `--demo` alone emitted a build that
recognised a test account and then routed it to whatever the live hub returned, and it
stayed that way for months, because a build dir said nothing at all about how it was
built. Nothing could have caught it by reading the artifact.

Three gates, offline, no model and no network:

  A. RESOLUTION. Defaults, the implications `--demo` carries, and the combinations that
     are refused because they would silently do nothing. Pure argument parsing.

  B. NO AMBIENT SWITCHES. Every retired environment variable has a flag, and no module
     the build imports reads the environment for a mode any more. This is what stops the
     next switch from being added the old way.

  C. THE MANIFEST IS TRUE. Six real builds. Each one's `build_manifest.json` must match
     what was asked for, AND the emitted app must carry the evidence the manifest claims
     -- the baked account map, the inlined leg fixtures, the missing proxy toolset, the
     injected delays, the blanked credentials. A flag that parses, records itself and
     then fails to take effect is precisely the original defect, so recording is not
     enough on its own.

     Packing is the one property graded against an OUTCOME rather than a flag, because
     `--pack-engine` is a request that a build host without python 3.12 cannot grant. See
     `packing_failures`.

  D. THE CALLER NAMES ITS APP. `--ces-app` is the one switch whose effect is a runtime
     decision rather than a file, so the emitted specialist caller is EXECUTED here: it
     must send the app id a session carries, and send no `app` field at all when there is
     none. Silence is what leaves the multi-tenant proxy on its own default app, which is
     the failure this switch exists to end.

  E. THE PACKING GATE CAN FAIL. C's packing assertions are the only thing standing between
     "the manifest says bytecode" and an app that ships source, and an assertion nobody has
     seen fail is worth nothing -- this repo has already shipped a mutation harness whose
     mutations had stopped applying. So each way the emitter could silently skip packing is
     APPLIED to a real build dir here, and C's own checker must catch every one.

    python tests/config_check.py [--verbose]
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import build_config  # noqa: E402
import engine_packing  # noqa: E402
import labs_paths  # noqa: E402

labs_paths.add_sdk_paths()
from flows.engine import packing  # noqa: E402

BUILD = os.path.join(ROOT, "build.py")

#: A syntactically real CES app, and not one that exists. Nothing here calls it: what is
#: under test is that the id reaches the artifact and leaves it again in the request.
APP_RESOURCE = "projects/config-check/locations/us/apps/00000000-0000-0000-0000-config"


def _parse(argv):
  """The resolved config for a command line, exactly as `build.py` resolves it."""
  parser = argparse.ArgumentParser()
  build_config.add_arguments(parser)
  return build_config.resolve(parser.parse_args(argv))


# --- A. resolution -----------------------------------------------------------

def check_resolution(verbose: bool) -> list:
  """Defaults, implications, and the combinations that are refused."""
  failures = []

  def expect(label, argv, **fields):
    try:
      config = _parse(argv)
    except SystemExit as exc:
      failures.append(f"{label}: refused ({exc})")
      return
    for key, want in fields.items():
      got = getattr(config, key)
      if got != want:
        failures.append(f"{label}: {key} is {got!r}, expected {want!r}")
      elif verbose:
        print(f"  ok   {label:34} {key}={got!r}")

  def refuse(label, argv, needle):
    try:
      _parse(argv)
    except SystemExit as exc:
      if needle not in str(exc):
        failures.append(f"{label}: refused for the wrong reason ({exc})")
      elif verbose:
        print(f"  ok   {label:34} refused")
      return
    failures.append(f"{label}: accepted a combination that would silently do nothing")

  # THE DEFAULTS ARE THE CONTRACT. Cloud Run is where the specialists are meant to run;
  # in-sandbox is the fallback, and a default that quietly picked it would ship a demo
  # nobody could reproduce on the deployed shape.
  #
  # `pack_engine` is the one default that is ON, and it belongs in that list for the same
  # reason: the engine ships as bytecode unless somebody says otherwise, because the
  # alternative costs ~95ms of re-parsing on every engine call.
  expect("no flags", [], demo=False, legs="live", specialists="cloudrun",
         specialist_delay="on", specialist_diag=False, leg_delay=0.0,
         sweep_delay=0.0, scrub_secrets=False, pack_engine=True, cuj=None, ces_app="")

  # `--demo` ON ITS OWN has to be the whole demo. This is the assertion the incident was
  # missing: the legs come with it, and nothing else needs knowing.
  expect("--demo", ["--demo"], demo=True, legs="fake", specialists="cloudrun")
  expect("--demo keeps cloudrun", ["--demo"], specialists="cloudrun")
  expect("--demo --legs live", ["--demo", "--legs", "live"], demo=True, legs="live")
  expect("--legs fake alone", ["--legs", "fake"], demo=False, legs="fake")
  expect("--specialists local", ["--demo", "--specialists", "local"],
         specialists="local", local_specialists=True)
  expect("--leg-delay", ["--leg-delay", "3"], leg_delay=3.0)
  expect("--sweep-delay", ["--demo", "--sweep-delay", "5"], sweep_delay=5.0)
  expect("--specialist-delay", ["--demo", "--specialist-delay", "30"],
         specialist_delay="30")
  expect("--skip-greeting", ["--skip-greeting"], skip_greeting=True)
  expect("no --skip-greeting", [], skip_greeting=False)
  expect("--scrub-secrets", ["--scrub-secrets"], scrub_secrets=True)
  expect("--no-pack-engine", ["--no-pack-engine"], pack_engine=False)
  expect("--pack-engine", ["--pack-engine"], pack_engine=True)
  expect("--cuj", ["--demo", "--cuj", "gateway_reboot"], cuj="gateway_reboot")
  expect("--ces-app", ["--ces-app", APP_RESOURCE], ces_app=APP_RESOURCE)

  # A no-op flag is the failure this file is about, so it is an ERROR rather than a
  # warning. Each of these parsed happily before and changed nothing.
  refuse("--specialist-diag on cloudrun", ["--demo", "--specialist-diag"],
         "--specialist-diag needs --specialists=local")
  refuse("--sweep-delay without --demo", ["--sweep-delay", "5"],
         "--sweep-delay needs --demo")
  refuse("--specialist-delay without --demo", ["--specialist-delay", "30"],
         "--specialist-delay needs --demo")
  # Not a no-op but a SILENT one: the proxy refuses an app it cannot parse and answers
  # from its own instead, so a typo here is a build whose specialists quietly belong to
  # somebody else's app.
  refuse("--ces-app that is not a resource", ["--ces-app", "cce8467e-7a51"],
         "is not a CES app resource")

  # A leg's delay is rendered into the emitted body, and `3` has to stay `3`: this is
  # what made the flag byte-identical to the environment variable it replaced.
  for value, want in ((3.0, "3"), (2.5, "2.5"), (20.0, "20")):
    got = build_config.format_seconds(value)
    if got != want:
      failures.append(f"format_seconds({value}) is {got!r}, expected {want!r}")

  # THE ORDERING GUARD. Activating after something has read the config means half the
  # build was composed against the defaults -- which is exactly how `--demo` set its
  # switch four lines after the import that had already consumed it.
  saved = (build_config._active, build_config._observed)  # noqa: SLF001
  try:
    build_config.reset_for_test()
    build_config.current()
    try:
      build_config.activate(build_config.BuildConfig(demo=True))
      failures.append("a config activated AFTER it had been read was accepted")
    except SystemExit:
      if verbose:
        print("  ok   late activation                 refused")
  finally:
    build_config.reset_for_test()
    build_config._active, build_config._observed = saved  # noqa: SLF001

  return failures


# --- B. no ambient switches --------------------------------------------------

#: Every switch that used to be an environment variable, and the flag that replaced it.
#: A row that cannot be resolved is a switch that lost its only way in.
RETIRED = {
    "SPIKE_DEMO": (["--demo"], "demo", True),
    "SPIKE_FAKE_LEGS": (["--legs", "fake"], "legs", "fake"),
    "SPIKE_SLOW_LEGS": (["--leg-delay", "3"], "leg_delay", 3.0),
    "SPIKE_LOCAL_SPECIALISTS": (["--specialists", "local"], "specialists", "local"),
    "SPIKE_LOCAL_DIAG": (["--demo", "--specialists", "local", "--specialist-diag"],
                         "specialist_diag", True),
    "DEMO_SWEEP_SECONDS": (["--demo", "--specialist-delay", "30"],
                           "specialist_delay", "30"),
    "SCRUB_SECRETS": (["--scrub-secrets"], "scrub_secrets", True),
    # Read in the AGENT module and by nothing at all: defined, never consulted. Deleted
    # rather than given a flag, and the empty entry says so.
    "SPIKE_PY_ONLY": (None, None, None),
}

#: Env keys a build module may still read. Both are LOCATORS -- where the substrate is,
#: where the SDK is -- rather than modes, and neither changes what is emitted.
ALLOWED_ENV = {"COMCAST_SOURCE"}

#: Not imported by the build. `labs_paths` finds the SDK before anything else can run,
#: and `derive_head_intents` is a standalone analysis script.
NOT_BUILD_MODULES = {"labs_paths.py", "derive_head_intents.py"}

_ENV_READ = re.compile(
    r"""environ\.get\(\s*["'](\w+)["']|environ\[\s*["'](\w+)["']"""
    r"""|getenv\(\s*["'](\w+)["']""")


def _build_modules():
  """Every module the build imports, by path."""
  paths = [os.path.join(ROOT, n) for n in sorted(os.listdir(ROOT))
           if n.endswith(".py") and n not in NOT_BUILD_MODULES]
  journeys = os.path.join(ROOT, "journeys")
  for dirpath, _dirnames, filenames in os.walk(journeys):
    paths += [os.path.join(dirpath, n) for n in sorted(filenames) if n.endswith(".py")]
  return paths


def check_no_ambient(verbose: bool) -> list:
  """Every retired variable has a flag, and no build module reads a mode from the env."""
  failures = []
  for name, row in sorted(RETIRED.items()):
    argv, field, want = row
    if argv is None:
      if verbose:
        print(f"  ok   {name:26} deleted; nothing read it")
      continue
    try:
      got = getattr(_parse(argv), field)
    except SystemExit as exc:
      failures.append(f"{name}: its replacement {' '.join(argv)} is refused ({exc})")
      continue
    if got != want:
      failures.append(f"{name}: {' '.join(argv)} resolved {field}={got!r}, not {want!r}")
    elif verbose:
      print(f"  ok   {name:26} -> {' '.join(argv)}")

  # A retired variable must be REFUSED, not ignored. Every scenario record in `tests/`
  # still opens with `SPIKE_DEMO=1 SPIKE_LOCAL_SPECIALISTS=1 python build.py ...`, and
  # emitting a live-backend build for that command line would be the original incident
  # again with the artifact and the intent swapped.
  for name in sorted(RETIRED):
    if RETIRED[name][0] is None:
      continue
    try:
      build_config.reject_retired_env({name: "1"})
      failures.append(f"{name}=1 was ignored rather than refused; the caller asked for a "
                      "mode and got a build that is not it")
    except SystemExit as exc:
      if name not in str(exc):
        failures.append(f"{name}=1 was refused without naming it: {exc}")
      elif verbose:
        print(f"  ok   {name:26} refused when set in the environment")
  if build_config.reject_retired_env({}) is not None:
    failures.append("reject_retired_env objected to a clean environment")

  for path in _build_modules():
    with open(path) as fh:
      source = fh.read()
    for match in _ENV_READ.finditer(source):
      key = next(g for g in match.groups() if g)
      if key in ALLOWED_ENV:
        continue
      line = source[:match.start()].count("\n") + 1
      failures.append(
          f"{os.path.relpath(path, ROOT)}:{line} reads the environment for {key!r}. "
          "A build switch is a flag: add it to `build_config`, where --help can see it.")
  if verbose and not failures:
    print(f"  ok   {'ambient reads':26} none in {len(_build_modules())} build modules")
  return failures


# --- C. the manifest is true -------------------------------------------------

def _leg_bodies(app_dir):
  return [os.path.join(app_dir, "tools", leg, "python_function", "python_code.py")
          for leg in ("SweepLegs_leg_outage_leg", "SweepLegs_leg_convoy_leg")]


def _read(path):
  with open(path) as fh:
    return fh.read()


def _credential_defaults(app_dir):
  """The two variable defaults that are live credentials, as `{name: is_blank}`.

  Only ever the EMPTINESS. The values are real credentials and this file prints its
  findings.
  """
  with open(os.path.join(app_dir, "app.json")) as fh:
    decls = json.load(fh).get("variableDeclarations", [])
  return {d["name"]: not (d.get("schema") or {}).get("default")
          for d in decls if d["name"] in ("RDK_TOKEN", "RDK_MCP_CLIENT_SECRET")}


def _ces_app_default(app_dir):
  """The baked default of the variable the specialist caller reads its app id from."""
  with open(os.path.join(app_dir, "app.json")) as fh:
    decls = json.load(fh).get("variableDeclarations", [])
  for decl in decls:
    if decl["name"] == build_config.CES_APP_VARIABLE:
      return (decl.get("schema") or {}).get("default", "")
  return None


def _spec_declares_app(app_dir):
  """Whether the toolset's spec has an `app` field for the caller to fill.

  Read separately from the caller's own source because the two fail apart, and only one
  of them fails visibly: CES builds the request from the SPEC, so a body field the spec
  omits is stripped inside the sandbox and the call goes out looking exactly like the
  one-tenant call it replaced.
  """
  path = os.path.join(app_dir, "toolsets", "specialist_proxy", "open_api_toolset",
                      "open_api_schema.yaml")
  if not os.path.exists(path):
    return False
  # A regex rather than a YAML parse, so this reads the emitted BYTES: the build writes
  # this file and a round-trip through the same library could agree with itself.
  body = _read(path).split("/resolveSpecialists/", 1)[0]
  return bool(re.search(r"^\s+app:\n\s+type: string$", body, re.M))


def evidence(app_dir) -> dict:
  """What the emitted app actually IS, read back off the files a caller would run.

  Deliberately not read from the manifest: the manifest is the claim under test.
  """
  legs = [_read(p) for p in _leg_bodies(app_dir)]
  gate = _read(os.path.join(app_dir, "tools", "resolve_account_context",
                            "python_function", "python_code.py"))
  sweep = _read(os.path.join(app_dir, "tools", "run_comcast_diagnostics",
                             "python_function", "python_code.py"))
  status_dir = os.path.join(app_dir, "tools", "resolve_specialists_remote__status")
  status = _read(os.path.join(status_dir, "python_function", "python_code.py"))
  start = _read(os.path.join(app_dir, "tools", "resolve_specialists_remote",
                             "python_function", "python_code.py"))
  with open(os.path.join(status_dir,
                         "resolve_specialists_remote__status.json")) as fh:
    status_meta = json.load(fh)
  delay = re.search(r"_time\.sleep\((\d+(?:\.\d+)?)\)\n  import inspect", legs[0])
  sweep_delay = re.search(r"_SWEEP_DELAY_S = (\d+(?:\.\d+)?)", sweep)
  scenario = re.search(r"demo_delay=([^'&\"]*)", gate)
  return {
      # The account -> scenario map, which is what makes an account number pick a journey.
      "accounts_baked": "DEMO_ACCOUNTS" in gate,
      # The stub that resolves the sweep from `mock_config_string`.
      "sweep_stubbed": "_SWEEP_DELAY_S" in sweep,
      "legs_inlined": all("_LEG_FIXTURE_INLINED" in body for body in legs),
      "leg_delay": float(delay.group(1)) if delay else 0.0,
      "sweep_delay": float(sweep_delay.group(1)) if sweep_delay else 0.0,
      "specialist_delay": scenario.group(1) if scenario else None,
      "specialists_local": "_local_specialists" in status,
      "proxy_toolset": os.path.isdir(os.path.join(app_dir, "toolsets",
                                                  "specialist_proxy")),
      "poll_async": status_meta.get("executionType") == "ASYNCHRONOUS",
      "specialist_diag": "_net_text" in status,
      "credentials_blank": _credential_defaults(app_dir),
      # Multi-tenancy, at both ends of the one contract: the caller reads the app id out
      # of the session, the spec gives it somewhere to go, and `--ces-app` decides what
      # the session says when nobody seeds it.
      "caller_sends_app": "_place(request, 'app'" in start,
      "spec_declares_app": _spec_declares_app(app_dir),
      "ces_app_default": _ces_app_default(app_dir),
  }


def expected(config) -> dict:
  """What the manifest's claims MEAN for the emitted app."""
  return {
      "accounts_baked": config.demo,
      "sweep_stubbed": config.demo,
      "legs_inlined": config.fake_legs,
      "leg_delay": config.leg_delay,
      "sweep_delay": config.sweep_delay,
      "specialist_delay": config.specialist_delay if config.demo else None,
      "specialists_local": config.local_specialists,
      # The proxy is the Cloud Run dependency: local mode drops it so a stray edit cannot
      # reach it, and cloudrun mode must still have it.
      "proxy_toolset": not config.local_specialists,
      "poll_async": config.local_specialists,
      "specialist_diag": config.specialist_diag,
      "credentials_blank": {"RDK_TOKEN": config.scrub_secrets,
                            "RDK_MCP_CLIENT_SECRET": config.scrub_secrets},
      # There is no proxy in local mode, so there is nothing to tell which app to use.
      "caller_sends_app": not config.local_specialists,
      "spec_declares_app": not config.local_specialists,
      # DECLARED in every build, whatever it is set to: a build with no --ces-app still
      # has to leave the variable there for a session to seed.
      "ces_app_default": config.ces_app,
  }


def _engine_path(app_dir):
  return os.path.join(app_dir, engine_packing.ENGINE_TOOL)


def _blessed_engine():
  """The framework's own engine source, which every build emits verbatim.

  What makes the round-trip assertion exact rather than a heuristic: a packed module must
  unpack to THESE bytes. `flows.engine.blessed_source` hashes the same copy out of the
  deployed artifact for the framework drift gate, so a blob that lost them would break that
  gate and the deployed module's source fallback together, and neither would say so.
  """
  return _read(os.path.join(labs_paths.framework_root(), engine_packing.ENGINE_ENTRY,
                            "python_function", "python_code.py"))


def packing_failures(label, app_dir) -> list:
  """Hold the emitted engine tool to what the manifest says it IS.

  Every other property in this file is read off a file and compared to a flag. Packing
  cannot be, because the flag is a REQUEST: it needs a python 3.12 interpreter (CES's), and
  the venv this repo builds in is 3.13, so a build host without one emits source. The
  manifest therefore records an outcome as well -- packed / skipped / off -- and it is the
  OUTCOME that is graded here.

  The reason this is not merely "does the marker string appear": an app was deployed named
  `comcast-DEMO-packed` while serving source, and nothing in the artifact contradicted the
  name. So a `packed` claim has to survive being taken apart -- the module unpacks to the
  source it was built from, and that source is the engine. And a `skipped` claim has to be
  UNAVAILABILITY rather than a shrug: on a host that can pack, skipping is a defect, and a
  build that both could pack and did not is exactly the silence this gate exists to break.
  """
  failures = []
  recorded = build_config.read_packing(app_dir)
  config = build_config.read_manifest(app_dir)
  text = _read(_engine_path(app_dir))
  packed = packing.is_packed(text)

  if recorded not in build_config.PACKING_STATES:
    return [f"{label}: the manifest records {build_config.PACKING_KEY}={recorded!r}, "
            f"which is not one of {build_config.PACKING_STATES}"]
  if config.pack_engine != (recorded != build_config.PACK_OFF):
    failures.append(
        f"{label}: the manifest says pack_engine={config.pack_engine} and "
        f"{build_config.PACKING_KEY}={recorded!r}. A build that asked for packing records "
        f"packed or skipped; one that did not records off.")

  if recorded == build_config.PACKED:
    if not packed:
      failures.append(
          f"{label}: the manifest says the engine is packed and the emitted tool is plain "
          "source. That is the deploy that was named for being packed while serving "
          "source, reproduced.")
    else:
      recovered = packing.unpack(text)
      if recovered != _blessed_engine():
        failures.append(
            f"{label}: the packed engine unpacks to {len(recovered or ''):,} characters "
            f"that are not the framework's engine ({len(_blessed_engine()):,}). The drift "
            "gate hashes that copy and the deployed module falls back to it, so a blob "
            "that lost it breaks both silently.")
      # The shape CES needs: it resolves a tool's schema by PARSING, and a module whose
      # first top-level def is not the annotated entry point does not degrade to a missing
      # tool -- it takes the whole app down (probe 165).
      defs = [n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef)]
      if not defs or defs[0].name != engine_packing.ENGINE_ENTRY or defs[0].returns is None:
        failures.append(
            f"{label}: the packed engine's first top-level def is not an annotated "
            f"`{engine_packing.ENGINE_ENTRY}`; CES could not resolve its schema.")
  else:
    if packed:
      failures.append(
          f"{label}: the manifest says {recorded!r} and the emitted engine IS packed. The "
          "artifact has to be readable off the manifest in both directions.")
    elif text != _blessed_engine():
      failures.append(f"{label}: the manifest says {recorded!r}, so the emitted engine "
                      "should be the framework's source verbatim, and it is not")

  # THE SILENT SKIP. `skipped` is legitimate only on a host with no 3.12 interpreter. On
  # this one there is (uv fetches a managed one), so a build that emitted source while
  # asking for bytecode is a defect in the emitter, not in the machine.
  if recorded == build_config.PACK_SKIPPED and engine_packing.interpreter():
    failures.append(
        f"{label}: packing was skipped on a host that CAN pack "
        f"({' '.join(engine_packing.interpreter())}). A skip is for a machine with no "
        "python 3.12; here it means the step silently did nothing.")
  return failures


#: One row per build. Between them they exercise every switch, in both states.
BUILDS = [
    ("default", []),
    ("no packing", ["--no-pack-engine"]),
    ("demo", ["--demo"]),
    ("demo, local, every delay",
     ["--demo", "--specialists", "local", "--specialist-diag",
      "--leg-delay", "3", "--sweep-delay", "5", "--specialist-delay", "30"]),
    ("fake legs, scrubbed, no demo", ["--legs", "fake", "--scrub-secrets"]),
    ("demo, app pinned", ["--demo", "--ces-app", APP_RESOURCE]),
]


def check_manifest(verbose: bool) -> list:
  """Build for real, then hold each build to what its manifest says it is."""
  failures = []
  work = tempfile.mkdtemp(prefix="config_check_")
  try:
    for label, argv in BUILDS:
      out = os.path.join(work, re.sub(r"\W+", "_", label))
      result = subprocess.run(
          [sys.executable, BUILD, "--out", out] + argv,
          cwd=ROOT, capture_output=True, text=True, check=False)
      if result.returncode != 0:
        failures.append(f"{label}: build failed\n{result.stdout[-1500:]}"
                        f"{result.stderr[-1500:]}")
        continue

      wanted = _parse(argv)
      stamped = build_config.read_manifest(out)
      if stamped != wanted:
        failures.append(f"{label}: the manifest records {stamped}, but the flags "
                        f"resolve to {wanted}")
        continue

      failures += packing_failures(label, out)
      got, want = evidence(out), expected(stamped)
      for key in sorted(want):
        if got[key] != want[key]:
          failures.append(
              f"{label}: the manifest says {key} should be {want[key]!r}, and the "
              f"emitted app has {got[key]!r}. The flag was recorded and did not take "
              "effect -- which is the defect this file exists for.")
        elif verbose:
          print(f"  ok   {label[:24]:26} {key}={got[key]!r}")
      if not verbose:
        print(f"  ok   {label:30} manifest matches {len(want)} emitted properties, "
              f"engine {build_config.read_packing(out)}")
  finally:
    shutil.rmtree(work, ignore_errors=True)
  return failures


# --- D. the caller tells the proxy which app ---------------------------------

def _run_caller(app_dir, variables):
  """Call the emitted specialist caller for real, and return the request it posted.

  The body is EXECUTED rather than read, because what matters is a runtime decision:
  `_session_value` consults `context.variables` and `context.state`, and reading the
  source only shows that the lines are present. CES supplies `context` and `tools` as
  globals, so they are injected the same way.
  """
  import types as pytypes

  source = _read(os.path.join(app_dir, "tools", "resolve_specialists_remote",
                              "python_function", "python_code.py"))
  sent = {}

  def _start(request):
    sent.update(request)
    return {"jobId": "config-check"}

  namespace = {
      "context": pytypes.SimpleNamespace(variables=dict(variables), state={}),
      "tools": pytypes.SimpleNamespace(specialist_proxy_resolveSpecialists=_start),
  }
  exec(compile(source, "resolve_specialists_remote", "exec"), namespace)  # noqa: S102
  out = namespace["resolve_specialists_remote"](
      accountNumber="8069100230361003", cable_modem_mac="aa:bb:cc:dd:ee:ff",
      mock_config_string="")
  return sent, out


def check_caller_sends_app(verbose: bool) -> list:
  """The proxy is multi-tenant only if the caller says who it is — and only then.

  Both halves are load-bearing and they fail in opposite directions. Without the first,
  every build's specialists are opened in whichever app the proxy was deployed pinned to
  and every other build's fail silently. Without the second, a build with no `--ces-app`
  and a session that seeds nothing would send an EMPTY app, and an empty app is not the
  same as no app -- the proxy's fallback is what keeps that build working.
  """
  failures = []
  work = tempfile.mkdtemp(prefix="config_check_caller_")
  try:
    out = os.path.join(work, "pinned")
    result = subprocess.run([sys.executable, BUILD, "--out", out], cwd=ROOT,
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
      return [f"caller: build failed\n{result.stdout[-1500:]}{result.stderr[-1500:]}"]

    sent, answered = _run_caller(out, {build_config.CES_APP_VARIABLE: APP_RESOURCE})
    if sent.get("app") != APP_RESOURCE:
      failures.append(f"the caller sent app={sent.get('app')!r} for a session holding "
                      f"{APP_RESOURCE!r}; the proxy would answer from its own app")
    elif verbose:
      print(f"  ok   {'variable set':26} app={sent['app']!r}")
    if not answered.get("success"):
      failures.append(f"the caller failed the call it did make: {answered.get('error')!r}")

    sent, answered = _run_caller(out, {})
    if "app" in sent:
      failures.append(f"the caller sent app={sent['app']!r} with the variable unset; "
                      "empty is not the same as absent, and the proxy's fallback is "
                      "what makes an unpinned build keep working")
    elif verbose:
      print(f"  ok   {'variable unset':26} no app field")
    if not answered.get("success"):
      failures.append("the caller stopped working when the variable is unset: "
                      f"{answered.get('error')!r}")
    if not failures:
      print(f"  ok   {'the caller':30} sends the app it is given, and nothing when "
            "it is given none")
  finally:
    shutil.rmtree(work, ignore_errors=True)
  return failures


# --- E. the packing gate can fail --------------------------------------------

def _write(path, text):
  with open(path, "w") as fh:
    fh.write(text)


def _decoy_src(packed_text):
  """The same packed module with a DIFFERENT source embedded, still well-formed.

  Re-encoded the way `packing` encodes, so nothing about the file looks wrong: it parses,
  it carries the marker, its entry stub is intact, and the bytecode still runs. Only the
  recovered source is not the engine -- which is invisible to every check except the
  round-trip one, and is what would quietly break the drift gate and the fallback.
  """
  import base64  # noqa: PLC0415
  import zlib  # noqa: PLC0415

  decoy = base64.b64encode(zlib.compress(b"def slot_filling_engine():\n  pass\n", 9))
  mutated, count = re.subn(r"^_SRC = b'[^']*'$", f"_SRC = {decoy!r}", packed_text,
                           count=1, flags=re.M)
  if count != 1:
    raise AssertionError("the packed module has no single-line `_SRC = b'...'` to swap; "
                         "this mutation has rotted and is testing nothing")
  return mutated


def _set_packing(app_dir, state):
  """Rewrite the manifest's outcome key in place, leaving the flags alone."""
  path = os.path.join(app_dir, build_config.MANIFEST_NAME)
  with open(path) as fh:
    data = json.load(fh)
  data[build_config.PACKING_KEY] = state
  _write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def check_packing_can_fail(verbose: bool) -> list:
  """Break a build the ways the emitter could break it, and require C to notice.

  Two builds, one packed and one not, then each mutation applied to the emitted files and
  reverted. Nothing is rebuilt per mutation: what is under test is the CHECKER, and every
  mutation here is a state a real emitter bug would leave the artifact in.
  """
  failures = []
  work = tempfile.mkdtemp(prefix="config_check_packing_")
  try:
    dirs = {}
    for name, argv in (("packed", []), ("source", ["--no-pack-engine"])):
      out = os.path.join(work, name)
      result = subprocess.run([sys.executable, BUILD, "--out", out] + argv, cwd=ROOT,
                              capture_output=True, text=True, check=False)
      if result.returncode != 0:
        return [f"packing selftest: the {name} build failed\n"
                f"{result.stdout[-1500:]}{result.stderr[-1500:]}"]
      dirs[name] = out

    # POSITIVE CONTROL. A mutation harness that mutates a build the checker already
    # rejects proves nothing, and a harness whose anchors have rotted reports success --
    # which is how two of `hook_diff_selftest`'s seven mutations silently stopped applying.
    for name, app_dir in dirs.items():
      clean = packing_failures(name, app_dir)
      if clean:
        return [f"packing selftest: the untouched {name} build already fails: {clean}"]
    if verbose:
      print("  ok   positive control            both untouched builds pass")

    packed_text = _read(_engine_path(dirs["packed"]))
    source_text = packing.unpack(packed_text)

    # (label, which build, what a broken emitter would have left behind)
    mutations = [
        # THE ONE THIS GATE IS FOR: the packing step did nothing and the manifest still
        # claims bytecode. Exactly the deploy named `comcast-DEMO-packed` that served
        # source, and nothing in the artifact contradicted it.
        ("emitter silently skipped packing", "packed",
         lambda d: _write(_engine_path(d), source_text)),
        # Packed, but the embedded source is not the engine: the framework drift gate
        # hashes that copy, and the deployed module falls back to it whenever the CES
        # interpreter stops matching the one that built the blob.
        ("the blob stops carrying its source", "packed",
         lambda d: _write(_engine_path(d), _decoy_src(packed_text))),
        # The stub CES parses for the tool's schema, renamed. Offline this changes nothing
        # -- the blob rebinds the name at load time either way -- and deployed it takes the
        # whole app down rather than degrading to a missing tool (probe 165).
        ("the packed module loses its entry def", "packed",
         lambda d: _write(_engine_path(d),
                          packed_text.replace("def slot_filling_engine(", "def gone(", 1))),
        # Recorded honestly, on a host that can pack. A skip is a missing interpreter, not
        # a shrug -- without this rule, "skipped" would be a way to make any build pass.
        ("skipped on a host that can pack", "packed",
         lambda d: (_write(_engine_path(d), source_text),
                    _set_packing(d, build_config.PACK_SKIPPED))),
        # The request and the outcome pulled apart. `write_manifest` refuses to emit this
        # pairing; the checker must refuse to accept one it is handed.
        ("the outcome contradicts the flag", "packed",
         lambda d: _set_packing(d, build_config.PACK_OFF)),
        # The other direction: `--no-pack-engine` asked for readable source and got a blob.
        # A flag that is recorded and does not take effect is the defect this file is about,
        # and it is no less one when it errs towards the fast path.
        ("--no-pack-engine packed anyway", "source",
         lambda d: _write(_engine_path(d), packed_text)),
    ]

    for label, which, mutate in mutations:
      app_dir = dirs[which]
      # `skipped` is legitimate where there is no python 3.12, so on such a host that
      # mutation is not a defect and there is nothing to catch. Reported rather than
      # skipped silently -- the whole point of this file.
      if label.startswith("skipped on a host") and not engine_packing.interpreter():
        print(f"  --   {label:30} not applicable: this host cannot pack")
        continue
      saved = (_read(_engine_path(app_dir)),
               _read(os.path.join(app_dir, build_config.MANIFEST_NAME)))
      try:
        mutate(app_dir)
        caught = packing_failures(label, app_dir)
      finally:
        _write(_engine_path(app_dir), saved[0])
        _write(os.path.join(app_dir, build_config.MANIFEST_NAME), saved[1])
      if not caught:
        failures.append(f"{label}: mutated the {which} build and the manifest check "
                        "still passed; the packing assertions have no teeth")
        print(f"  FAIL {label:30} NOT CAUGHT")
      else:
        print(f"  ok   {label:30} caught")
        if verbose:
          print(f"         {caught[0]}")
  finally:
    shutil.rmtree(work, ignore_errors=True)
  return failures


def run(verbose: bool) -> int:
  print("A. the flags resolve, and a no-op combination is refused")
  failures = check_resolution(verbose)

  print("\nB. every retired environment variable has a flag, and none is still read")
  failures += check_no_ambient(verbose)

  print("\nC. the manifest matches the app that was emitted")
  failures += check_manifest(verbose)

  print("\nD. the specialist caller names the app whose specialists should answer")
  failures += check_caller_sends_app(verbose)

  print("\nE. the packing assertions in C can fail")
  failures += check_packing_can_fail(verbose)

  if failures:
    print(f"\n{len(failures)} failure(s):")
    for line in failures:
      print(f"  FAIL {line}")
    return 1
  print("\nevery switch is a flag, and every flag reaches the artifact")
  return 0


if __name__ == "__main__":
  parser = argparse.ArgumentParser()
  parser.add_argument("--verbose", action="store_true")
  raise SystemExit(run(parser.parse_args().verbose))
