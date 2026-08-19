"""Assemble an `App` into a deployable CXAS app dir.

Generalizes the proven per-build recipe from the first production agents into
the library: collect
the setters + stub executors a flow references, merge in the hand-authored
`@flows.tool` bodies, validate every config (single + cross) against the packaged
framework, emit via `scaffold.build`, then scope the agent's tools, inject the
author's business `variableDeclarations`, and write instructions.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from typing import Any, Optional

from ..config import validation as _sv
from ..emit import fanout as _fanout
from ..emit import scaffold as _scaffold
from ..emit.models import (
    ChildAgentSpec,
    HostAgentSpec,
    MultiAgentScaffoldRequest,
    ScaffoldRequest,
    ScaffoldResult,
)
from .. import surfaces as _surfaces
from ..engine import blessed_source as _bs
from ..engine import loader as _loader
from . import a2a as _a2a
from . import customization as _customization
from . import guardrails as _gr
from . import integrity as _integrity
from . import language as _language
from . import autofill as _autofill
from . import mcp as _mcp
from . import openapi as _openapi
from . import search as _search
from . import setters as _setters
from . import steering as _steering
from . import toolset_common as _toolset_common
from . import tools as _tools
from . import variable_maps as _variable_maps
from .dsl import Agent, App, HostRouter

# The packaged framework root the validator + engine load from.
FRAMEWORK_ROOT = str(_loader.default_framework_root())


def framework_tool_names() -> set[str]:
  """Names of the blessed framework control tools (never author-generated)."""
  names = {"end_session"}
  for f in _bs.framework_tool_files():
    if f["path"].startswith("tools/"):
      names.add(f["path"].split("/")[1])
  return names


def collect(
    all_configs: list[dict[str, Any]],
    tool_bodies: dict[str, str],
    classifiers: Optional[dict[str, tuple[dict, Any]]] = None,
    bodyless: Optional[set[str]] = None,
) -> tuple[dict[str, str], list[str]]:
  """Walk every config -> `{tool_name: body}` for setters + executors, merged with
  the hand-authored `@flows.tool` bodies (which win).

  `bodyless` names tools that exist on the platform but have no python body — a
  remote A2A agent is one. They are available to reference but must never get a
  generated executor stub: emitting one would put a `pythonFunction` body next to the
  `remoteAgentTool` resource of the same name, and the stub would answer instead of
  the remote agent.

  Returns `(bodies, available_tool_names)`.
  """
  no_body = framework_tool_names() | set(bodyless or ())
  classifiers = classifiers or {}
  authored = set(tool_bodies)
  setter_params: dict[str, str] = {}       # single-field: name -> slot param
  setter_enums: dict[str, list] = {}       # single-field enum slots: name -> options
  setter_enum_owner: dict[str, str] = {}   # ... and which slot declared them (errors)
  setter_rules: dict[str, list] = {}       # single-field slots needing a validating setter (e.g. luhn)
  # Multi-field (`setter_group`): a set_<op>_inputs setter shared by N slots that
  # each carry a `setter_field`. name -> ordered [{"name": field, "validation_rules"}]
  # (the group's fields), deduped by field so cross-config sharing merges cleanly.
  setter_groups: dict[str, list[dict[str, Any]]] = {}
  execs: dict[str, dict[str, set]] = {}    # name -> {params, out_keys}
  for c in all_configs:
    for s in c.get("slots", []):
      setter = s.get("setter")
      if s.get("source") == "user" and setter and setter not in no_body and setter not in authored:
        field = s.get("setter_field")
        if field:
          grp = setter_groups.setdefault(setter, [])
          if not any(f["name"] == field for f in grp):
            grp.append({"name": field,
                        "validation_rules": s.get("validation_rules") or []})
        else:
          setter_params[setter] = s["name"]
          # A standalone (non-grouped) slot normally uses the plain value-recording
          # setter, which ignores validation_rules. A `luhn` rule (card number) must
          # still be enforced, so route such a slot to a single-field VALIDATING
          # setter that applies its rules (length_digits/date_format/luhn) and returns
          # the single-setter contract. Gated on `luhn` to leave existing single-slot
          # behavior for other rule kinds unchanged.
          _rules = s.get("validation_rules") or []
          if any((r.get("kind") or r.get("type")) == "luhn" for r in _rules):
            setter_rules[setter] = _rules
          # An enum-bearing slot (an intent slot, or any slot with option_cues) gets a
          # setter that enforces the enum — otherwise the rules are decorative.
          opts = [o for r in (s.get("validation_rules") or [])
                  if r.get("kind") == "enum"
                  for o in str(r.get("detail", "")).split("|") if o.strip()]
          if opts:
            # One setter, one enum. Two slots sharing a setter name with DIFFERENT
            # options (easy to do by accident — `intent_slot("topic", ...)` in two flows
            # both generate `set_topic`) would otherwise let the last one win, and the
            # other slot's perfectly valid answers would be rejected at runtime as
            # `not_in_enum`. Fail the build instead of shipping that.
            prior = setter_enums.get(setter)
            if prior is not None and set(prior) != set(opts):
              raise ValueError(
                  f"setter {setter!r} is shared by slots with different enum options: "
                  f"{sorted(set(prior))} (from {setter_enum_owner[setter]!r}) vs "
                  f"{sorted(set(opts))} (from {s['name']!r}). Give one of them its own "
                  "setter=, or make the options match."
              )
            setter_enums[setter] = opts
            setter_enum_owner[setter] = s["name"]
    for t in c.get("tasks", []):
      tool = t.get("tool")
      if not tool or tool in no_body or tool in authored:
        continue
      e = execs.setdefault(tool, {"params": set(), "out_keys": set()})
      e["params"].update(t.get("inputs", []))
      e["out_keys"].update((t.get("outputs") or {}).keys())

  bodies: dict[str, str] = {}
  # Multi-field setters first: a grouped `set_<op>_inputs` shadows any stray single-
  # field default for the same name (a slot missing setter_field on a group setter).
  for name, fields in setter_groups.items():
    bodies[name] = _setters.gen_multi_setter(name, fields)
  for name, param in setter_params.items():
    if name in setter_groups:
      continue  # already emitted as a multi-field setter
    if name.startswith("set_wrap_up"):
      bodies[name] = _setters.gen_wrap_up_setter(name, param)
    elif name in classifiers:
      mapping, default = classifiers[name]
      bodies[name] = _setters.gen_classifier_setter(name, param, mapping, default)
    elif name in setter_rules:
      bodies[name] = _setters.gen_validating_setter(name, param, setter_rules[name])
    elif name in setter_enums:
      bodies[name] = _setters.gen_enum_setter(name, param, setter_enums[name])
    else:
      bodies[name] = _setters.gen_setter(name, param)
  for name, e in execs.items():
    bodies[name] = _setters.gen_executor(
        name, sorted(e["params"]), sorted(e["out_keys"]) or ["result"]
    )
  # A router's bootstrap tool writes the gate slot, but it is named only in `bootstrap`
  # — never as a slot setter — so the slot scan above never generates it. It is also not
  # a blessed framework tool. Generate it here from the gate slot it fills. Declaring the
  # gate in `slots` instead would make the engine try to COLLECT it; the blessed pattern
  # is a bootstrap tool filling an undeclared gate (validator `_check_bootstrap`).
  for c in all_configs:
    boot_tool = (c.get("bootstrap") or {}).get("tool")
    gate = (c.get("bootstrap") or {}).get("slot") or c.get("gate_slot")
    if boot_tool and gate and boot_tool not in no_body and boot_tool not in bodies:
      # A ROUTER's bootstrap tool is also called BY THE ENGINE (route_backstop /
      # default_flow_backstop / resume_complete_inject), which passes the destination as
      # `flow=`. Naming the parameter after the gate slot instead makes every engine-side
      # call a no-op: the gate never fills, so the backstop re-fires each pass until CES
      # caps the turn. The value still lands on `bootstrap.slot`.
      param = "flow" if c.get("router") else gate
      # A router's gate IS an enum — `flow_types` is the closed set of routable flow
      # ids — so it gets the enum setter, which rejects anything else. The plain setter
      # stores any non-empty string, and for a gate that is not a cosmetic problem: the
      # gate reads as FILLED, so the router considers itself done, while the value maps
      # to no config, so `flow_config_map` misses and no child DAG ever drives. The call
      # then runs to its end with the model improvising and nothing logged anywhere.
      #
      # Measured on a two-flow router with no route_cues: the model answered
      # `set_active_flow(flow="Troubleshoot Internet")` — a label it invented rather than
      # the `triage` key — and every later turn fired no tool at all, ending in a
      # transfer. The identical flow as a standalone app was correct throughout.
      #
      # Rejecting produces the `not_in_enum` error the No-Match ladder already handles,
      # so the gate stays unfilled and the route / default backstops get their turn -
      # which is the recovery that was missing, not merely a better error message.
      flow_types = c.get("flow_types") if c.get("router") else None
      bodies[boot_tool] = (
          _setters.gen_enum_setter(boot_tool, param, list(flow_types)) if flow_types
          else _setters.gen_setter(boot_tool, param))
  # Hand-authored @flows.tool bodies override any generated stub.
  bodies.update(tool_bodies)

  dag_tools = {f"{c.get('_config_id', '')}_dag" for c in all_configs if c.get("_config_id")}
  available = sorted(set(bodies) | no_body | dag_tools)
  return bodies, available


def setter_descriptions(all_configs: list[dict[str, Any]]) -> dict[str, str]:
  """`{setter_name: description}` for every slot that declared `description=`.

  `intent_slot`/`passive_slot` store it as `tool_description`; this collects it so the
  scaffold can emit it as the setter's model-facing `pythonFunction.description` instead of
  the SDK default. A slot with no `description=` contributes nothing (the default stands).
  """
  out: dict[str, str] = {}
  for c in all_configs:
    for s in c.get("slots", []):
      desc = s.get("tool_description")
      setter = s.get("setter")
      if desc and setter:
        out[setter] = desc
  return out


def scoped_agent_tools(
    config_id: str,
    all_configs: list[dict[str, Any]],
    extra_config_ids: list[str],
    extra_agent_tools: Optional[list[str]] = None,
) -> list[str]:
  """The correctly-scoped `tools[]` for a plain (non-router) agent = ONLY what its
  flow uses, mirroring the proven scoping that avoids the "tools everywhere" routing
  failure (model calling set_active_flow / classify_turn_intent unprompted)."""
  # `settle_guard` is fired BY THE ENGINE, in the same preempt as a deferred dispatch, to
  # hold the turn open while the launch lands (ces-probes 129/130). CES only lets an agent
  # call a tool it lists, so an unlisted one is dropped on dispatch — and the engine, still
  # seeing the task un-fired, re-enters until the turn hits the ten-reasoning-loop cap with
  # nothing said. The resource ships in every app (it is in the blessed framework set);
  # this is the line that makes it callable.
  t = {"slot_filling_engine", "slot_intake", f"{config_id}_dag", "end_session",
       "settle_guard"}
  t |= {f"{cid}_dag" for cid in extra_config_ids}
  for c in all_configs:
    for s in c.get("slots", []):
      if s.get("setter"):
        t.add(s["setter"])
    for tk in c.get("tasks", []):
      if tk.get("tool"):
        t.add(tk["tool"])
  if any(s.get("requires_readback") for c in all_configs for s in c.get("slots", [])):
    t |= {"confirm_pending", "reject_pending"}
  for c in all_configs:
    if c.get("correction_tool"):
      t.add(c["correction_tool"])
      t.add("set_slot_change")
    # A router's bootstrap tool is the ONE thing the model must be able to call on a
    # routing turn; it is named only in `bootstrap`, never as a slot setter, so the
    # scan above misses it and the agent ships unable to route.
    boot_tool = (c.get("bootstrap") or {}).get("tool")
    if boot_tool:
      t.add(boot_tool)
  t |= {"cancel_flow", "transfer_to_human"}
  # A progressive fan-out's peek/watch are named by no task and no slot, so the scan
  # above cannot find them — and an agent that does not LIST the watcher cannot have it
  # dispatched. That failure has no symptom at all: a leg name resolving to no
  # registered tool survives neither a daemon thread nor a join, nothing surfaces
  # anywhere, and the turn simply dies (ces-probes 69). They are hidden from the model
  # on every turn by the engine's hiding policy; listing them only makes them
  # dispatchable by the engine.
  t |= _fanout.synthetic_tool_names(dict(enumerate(all_configs)))
  # A remote tool's STATUS wrapper, for the same reason and with the same symptom. No
  # flow names it — the engine owns the poll — so the task scan above cannot see it, and
  # CES drops a dispatch to a tool the agent does not list WITHOUT an error. Measured
  # live before this line: the start call reached the service and came back with a real
  # job handle, `remote_poll` logged a poll on every turn for six minutes, and not one
  # of them ever became an HTTP request or an `after_tool`. The job finished; the agent
  # never found out. Hidden from the model on every turn by the engine's hiding policy,
  # so listing it only makes it dispatchable by the engine.
  for _cfg in all_configs:
    for _remote in (_cfg.get("remote_tools") or {}).values():
      if _remote.get("status_tool"):
        t.add(_remote["status_tool"])
  t |= set(extra_agent_tools or [])
  return sorted(t)


_AUTO_GATE_SLOT = "active_flow"


def _apply_single_flow_gate(cfg: dict) -> dict:
  """Well-form a standalone (host-less) flow as a self-seeding, re-enterable gate.

  A single-flow app has no host to fill the flow gate, so without `single_flow` +
  a `gate_slot` the engine can't self-seed the flow on turn 1, and without
  `bootstrap.reset_on_complete` it can't re-arm after a terminal task (so a
  follow-up like "do that again" reads as a cancel). Author-supplied `bootstrap`
  keys (e.g. `welcome_slot`) are preserved. No-op if the author already set
  `single_flow` / `gate_slot` / `router` — i.e. opted in or authored a router root
  that manages its own gate.
  """
  if cfg.get("single_flow") or cfg.get("gate_slot") or cfg.get("router"):
    return cfg
  out = dict(cfg)
  out["single_flow"] = True
  out["gate_slot"] = _AUTO_GATE_SLOT
  boot = dict(out.get("bootstrap") or {})
  boot.setdefault("slot", _AUTO_GATE_SLOT)
  boot.setdefault("reset_on_complete", True)
  out["bootstrap"] = boot
  return out


def _apply_surfaces(cfg: dict, app: App) -> dict:
  """Stamp the app's delivery surfaces onto every flow config.

  Surfaces are an app-level property but resolution happens per turn inside the
  engine, which only ever sees one flow's config — so the table rides along on
  each. Emitted only when the app actually declares something: the engine's
  built-in `voice`/`chat` cover almost every app, and an absent key keeps the
  config identical to what it was before this feature existed.
  """
  if not app.surfaces and not app.default_surface:
    return cfg
  out = dict(cfg)
  if app.surfaces:
    out["surfaces"] = _surfaces.surfaces_to_config(app.surfaces)
  if app.default_surface:
    out["default_surface"] = app.default_surface
  return out


def _apply_variable_maps(cfg: dict, app: App) -> dict:
  """Lower the app's variable maps onto every flow config that can use them.

  Same shape of problem as `_apply_surfaces`: an app-level property resolved per turn
  by code that only ever sees one flow's config, so the table rides along on each.
  Lowering is per config because it drops bindings for slots this flow does not hold
  and reads the surviving slot definitions for the value shape and readback split.
  Emitted only when the app declares maps, so an app without them is unchanged.
  """
  if not app.variable_maps:
    return cfg
  lowered = _variable_maps.project(app.variable_maps, cfg.get("slots") or [])
  if not lowered:
    return cfg
  out = dict(cfg)
  out["variable_maps"] = lowered
  return out


def _apply_sensitive_readback(cfg: dict) -> dict:
  """Strip build-time `sensitive` slot markers + derive terminal `readback_inputs`.

  Two things read the marker before it goes: the terminal readback gate below, and the
  `verbatim` pin that keeps a PHI/PCI task's arguments off the model-fired filler path.

  `sensitive` (from `user_slot(sensitive=True)`) is a BUILD-TIME PHI/PCI marker, not
  a runtime slot key — it is NOT in the validator's `_VALID_SLOT_KEYS`, so it would
  be rejected by the unknown-key check. It must never reach the validator or disk.

  Before stripping it, mirror the CES backend's coarse WHOLE-TERMINAL readback gate
  (ces/backend.py `_terminal_config`/`_sequenced_config` + flows_sdk/lower.py:
  `readback_inputs = bool(user_slots) and not any(s.do_not_speak ...)`): a terminal
  tool task confirms its collected inputs before firing UNLESS any input slot is
  sensitive (PHI/PCI must not be spoken back). So per terminal tool task:
  `readback_inputs = bool(user_inputs) and not any(sensitive among task.inputs)`.
  Only set when the author didn't set `readback_inputs` explicitly (their call wins).
  Component-task inputs are a mapping (not a slot-name list) and CES never gates them
  here, so only `tool` tasks get a derived value.
  """
  slots = cfg.get("slots", [])
  sensitive = {s["name"] for s in slots if s.get("sensitive")}
  out = dict(cfg)

  new_tasks: list[dict] = []
  changed = False
  for t in cfg.get("tasks", []):
    # `sensitive` only ever SUPPRESSES readback (never read PHI/PCI back). If a terminal
    # tool task collects a sensitive input and the author didn't set readback_inputs,
    # pin it False. We do NOT force readback_inputs=True on non-sensitive terminals —
    # that would be a no-op unless the input slots also carry requires_readback (the
    # validator warns), and forcing it here is the author's call, not ours.
    nt = None
    if (t.get("terminal") and t.get("tool") and "readback_inputs" not in t
        and any(i in sensitive for i in (t.get("inputs") or []))):
      nt = dict(t)
      nt["readback_inputs"] = False
    # Same marker, second consumer. A `speech` policy that improvises the filler hands
    # the whole fire turn to the model, INCLUDING the tool call — so the arguments are
    # retyped through the model's output. That is fine for an order number and wrong
    # for PHI/PCI, and the engine cannot tell the difference because `sensitive` is
    # stripped below. Pin the task here, where the marker is still visible, so the
    # policy can be set flow-wide without leaking a sensitive value.
    if (t.get("tool") and "verbatim" not in t
        and any(i in sensitive for i in (t.get("inputs") or []))):
      if nt is None:
        nt = dict(t)
      nt["verbatim"] = True
    if nt is not None:
      new_tasks.append(nt)
      changed = True
    else:
      new_tasks.append(t)
  if changed:
    out["tasks"] = new_tasks

  # Strip the build-time marker from every slot (validator rejects it) — done after
  # the readback derivation above, which is the only consumer of `sensitive`.
  if sensitive:
    out["slots"] = [{k: v for k, v in s.items() if k != "sensitive"} for s in slots]
  return out


# Build-time opt-out marker on a slot/task. Never reaches the validator or disk.
_AUTOFILL_MARKER = "automatic_fillers"


def _apply_automatic_fillers(cfg: dict, app: App, report: Optional[list] = None) -> dict:
  """Hoist a contentless opener off `then_say`/`ask` into the node's `filler_say`.

  Authors write the filler in the wrong place — at the front of the line spoken AFTER
  the tool returns. Moving that first sentence to `filler_say` makes it ride the
  dispatching preempt instead, so the round trip is speech rather than dead air, using
  copy the author already wrote. See `authoring/autofill.py` for what qualifies (a
  closed list of acknowledgement phrases) and `hoist_blocked_by` for what disqualifies a node.

  Only the two text fields move. In particular this does NOT set `preempt_then_say`:
  an earlier draft did, on the theory that a non-terminal `then_say` is relayed for the
  model to re-render. It is not — `preempt` is already `bool(task_msg)` (engine ~7631),
  so a turn carrying a then_say never reaches the model. What the ordinary path adds is
  a FOLD: the line is spoken together with whatever comes next, the following question
  (~7561) or the next task's fire message (~7384). Preempting returns before that fold
  (~4330), which drops the next question, strands an `escalate` chain mid-walk and
  leaves an `awaits` completion undispatched. Driven both ways: with the flag the
  result turn is "Your balance is 42 dollars." and the agent then goes silent; without
  it, "Your balance is 42 dollars. Would you like that emailed to you?".

  Runs AFTER `_apply_sensitive_readback`, which is what pins `verbatim=True` on a
  PHI/PCI task — and `verbatim` blocks the hoist.

  The `automatic_fillers` marker is stripped unconditionally, including when the app never
  opted in: it is not in the validator's key whitelist, so leaving it would turn
  annotating a node into a build failure for everyone.
  """
  enabled, extra_ack = _autofill.filler_policy(app)
  out = dict(cfg)

  for key, is_task in (("slots", False), ("tasks", True)):
    nodes = cfg.get(key) or []
    if not nodes:
      continue
    field = _autofill.hoist_field(is_task)
    new_nodes: list[dict] = []
    changed = False
    for node in nodes:
      opted_out = node.get(_AUTOFILL_MARKER) is False
      hoist = None
      if enabled and not opted_out and not _autofill.hoist_blocked_by(
          node, cfg, is_task=is_task):
        hoist = _autofill.split_leading_filler(node.get(field), extra_ack=extra_ack)

      if hoist is None and _AUTOFILL_MARKER not in node:
        new_nodes.append(node)
        continue

      nn = {k: v for k, v in node.items() if k != _AUTOFILL_MARKER}
      if hoist is not None:
        nn[field] = hoist.remainder
        nn["filler_say"] = hoist.filler
        if report is not None:
          report.append((node.get("name", "?"), hoist.filler))
      new_nodes.append(nn)
      changed = True
    if changed:
      out[key] = new_nodes
  return out


def _report_filler_hoists(report: list) -> None:
  """Print what the automatic-filler pass moved. Silent when it moved nothing."""
  if not report:
    return
  print(f"automatic fillers: hoisted {len(report)}")
  for name, filler in report:
    print(f"  {name} -> {filler!r}")


def _apply_router_child_gate(
    cfg: dict,
    flow_keys: Optional[list[str]] = None,
    route_cues: Optional[dict[str, list[str]]] = None,
    intent_first: bool = True,
    flow_descriptions: Optional[dict[str, str]] = None,
) -> dict:
  """Well-form a sub-agent flow as a router child: a `set_active_flow` gate.

  Unlike a standalone (`single_flow`) app, a multi-agent sub-agent IS reached by
  cross-agent routing — the host pre-fills `active_flow` on transfer-in (mirrors
  `bella_notte_dag`). So it needs `gate_slot="active_flow"` + a `set_active_flow`
  bootstrap, but NOT `single_flow`.

  Mid-call sibling switching:
    * `intent_first` (default): the specialist classifies each in-flow turn against
      the available flows, so a switch is recognized by MEANING (robust switching).
      `flow_types` is the option menu shown to that classifier.
    * When `intent_first` is off, switching falls back to the model calling
      `set_active_flow` opportunistically plus keyword rails: `flow_types` (needs a
      lead-in + flow name) and `route_cues` (sibling synonyms, no lead-in).
  No-op on fields the author already set.
  """
  if cfg.get("gate_slot") or cfg.get("router") or cfg.get("single_flow"):
    return cfg
  out = dict(cfg)
  out["gate_slot"] = _AUTO_GATE_SLOT
  boot = dict(out.get("bootstrap") or {})
  boot.setdefault("tool", "set_active_flow")
  boot.setdefault("slot", _AUTO_GATE_SLOT)
  boot.setdefault("reset_on_complete", True)
  if intent_first:
    boot.setdefault("intent_first", True)
  out["bootstrap"] = boot
  if flow_keys and not out.get("flow_types"):
    # Order-preserving dedup, NOT sorted — see _ROUTE_ORDER_NOTE. flow_types is
    # threaded verbatim to CES; the runtime only reads it as a same-offset tiebreak,
    # so a re-sort here would yield a different config from the same input.
    out["flow_types"] = list(dict.fromkeys(k.lower().strip() for k in flow_keys))
  if route_cues and not out.get("route_cues"):
    out["route_cues"] = {k: list(v) for k, v in route_cues.items()}
  # Per-flow routing descriptions so the intent-first mid-flow classifier judges a switch
  # by what each flow is FOR (not just its key/cues) — the same descriptions the router
  # turn uses. Absent -> not emitted -> classifier byte-identical.
  if flow_descriptions and not out.get("flow_descriptions"):
    out["flow_descriptions"] = {k: v for k, v in flow_descriptions.items() if v}
  return out


def _slug(name: str) -> str:
  """A config-id-safe slug from an agent display name (lowercase, `_`-joined)."""
  return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "host"


# ---------------------------------------------------------------------------
# Route-cue / flow_types ORDERING contract (why nothing here is sorted).
#
# _ROUTE_ORDER_NOTE: route_cues and flow_types are emitted in AUTHOR/SFIR order,
# verbatim. This is a cross-backend DETERMINISM requirement, not a mis-routing fix:
# CES threads the source order straight into the deployed config, so a re-sort in
# this authoring layer would produce a DIFFERENT config from the SAME input (the
# Python DSL and the migration/SFIR emitter would then diverge byte-for-byte). It is
# NOT how routing is decided at runtime: `_route_intent` breaks ties on the earliest
# utterance position, and the dict/list order here is only the same-offset tiebreak.
# Hence every dedup below preserves insertion order (dict.fromkeys), never sorted().
# ---------------------------------------------------------------------------
_ROUTE_ORDER_NOTE = "route_cues/flow_types are author-ordered; see _ROUTE_ORDER_NOTE"


def _derived_route_cues(host: HostRouter) -> dict[str, list[str]]:
  """Alias-derived per-route cues: each flow key + the target Agent's aliases.

  Deduped ORDER-PRESERVING (flow key first, then aliases in author order) — never
  sorted (see _ROUTE_ORDER_NOTE)."""
  return {
      k.lower().strip(): list(dict.fromkeys(
          [k.lower().strip(), *(a.lower().strip() for a in ag.aliases)]))
      for k, ag in host.routes.items()
  }


def _merged_route_cues(host: HostRouter) -> dict[str, list[str]]:
  """The host's effective route cues: alias-derived cues with any explicit
  `HostRouter.route_cues` layered ON TOP, verbatim + order-preserving.

  Explicit cues take PRECEDENCE: an author-supplied entry REPLACES the alias-derived
  list for that flow key (override) and a key not among the routes is kept as-is
  (extend). Derived keys keep their author position; new explicit keys append in
  author order. Nothing is sorted (see _ROUTE_ORDER_NOTE)."""
  merged = _derived_route_cues(host)
  for k, cues in (host.route_cues or {}).items():
    merged[k] = list(cues)
  return merged


def _router_config_for(host: HostRouter) -> tuple[str, dict]:
  """The engine-strategy host's synthesized router DAG (id, config).

  Mirrors `slotfill_migration.engine.build_router_parent`: a `router` config with a
  `welcome` announce + an intent-first `set_active_flow` bootstrap that routes to a
  specialist via the shared `set_active_flow` (which returns `target_agent`).

  Threads the host's effective `route_cues` (explicit `HostRouter.route_cues` over
  alias-derived, see `_merged_route_cues`) into the emitted router config so the
  engine host has the same deterministic keyword backstop the sub-agent DAGs get —
  previously the engine host emitted NO route_cues at all.
  """
  cid = f"{_slug(host.name)}_router"
  # Order-preserving dedup, NOT sorted (see _ROUTE_ORDER_NOTE).
  keys = list(dict.fromkeys(k.lower().strip() for k in host.routes))
  welcome = host.welcome_message or "How can I help you today?"
  cfg = {
      "router": True,
      "bootstrap": {
          "tool": "set_active_flow", "slot": _AUTO_GATE_SLOT,
          "reset_on_complete": True, "welcome_slot": "welcome", "intent_first": True,
      },
      "gate_slot": _AUTO_GATE_SLOT,
      "flow_types": keys,
      "slots": [{
          "name": "welcome", "source": "announce", "shared": True, "preempt": False,
          "message": welcome,
          "response": [{"type": "text", "text": welcome}],
      }],
      "tasks": [],
  }
  cues = _merged_route_cues(host)
  if cues:
    cfg["route_cues"] = cues
  return cid, cfg


def _check_route_phrasings(host: HostRouter) -> None:
  """Reject a phrasing (flow key or alias) claimed by more than one sub-agent.

  Overlapping route cues make a mid-call switch non-deterministic, so this fails
  at build time with a clear message rather than mis-routing at runtime.
  """
  claim: dict[str, str] = {}
  collisions: list[str] = []
  for key, ag in host.routes.items():
    phrasings = {key.lower().strip(), *(a.lower().strip() for a in ag.aliases)}
    for p in sorted(phrasings):
      if not p:
        continue
      owner = claim.get(p)
      if owner is not None and owner != ag.name:
        collisions.append(f"{p!r} (claimed by {owner} and {ag.name})")
      else:
        claim.setdefault(p, ag.name)
  if collisions:
    raise ValueError(
        "overlapping route phrasing/aliases across sub-agents: "
        + "; ".join(sorted(set(collisions)))
    )

  # Validate an explicit `HostRouter.route_cues` map (order-preserving, threaded
  # verbatim by _merged_route_cues). Reject a cue mapped to an unknown flow key, an
  # empty cue list, and a duplicate cue phrase mapped to two different flows.
  valid_keys = {k.lower().strip() for k in host.routes}
  cue_errors: list[str] = []
  cue_owner: dict[str, str] = {}
  for flow_key, cues in (host.route_cues or {}).items():
    fk = flow_key.lower().strip() if isinstance(flow_key, str) else flow_key
    if fk not in valid_keys:
      cue_errors.append(
          f"route_cues maps to unknown flow key {flow_key!r} "
          f"(valid: {sorted(valid_keys)})"
      )
    if not cues:
      cue_errors.append(f"route_cues[{flow_key!r}] is an empty cue list")
      continue
    for cue in cues:
      c = cue.lower().strip()
      if not c:
        continue
      owner = cue_owner.get(c)
      if owner is not None and owner != fk:
        cue_errors.append(
            f"cue {cue!r} maps to two different flows ({owner} and {fk})"
        )
      else:
        cue_owner.setdefault(c, fk)
  if cue_errors:
    raise ValueError(
        "invalid HostRouter.route_cues: " + "; ".join(cue_errors)
    )


def _check_multi_agent_wiring(app: App) -> None:
  """Validate host/agents wiring — shared by `validate_app` and emit so `flows
  validate` catches the same errors emission would (missing/duplicate agents,
  overlapping route phrasings)."""
  host = app.host
  names = [a.name for a in app.agents]
  declared = set(names)
  missing = {a.name for a in host.routes.values()} - declared
  if missing:
    raise ValueError(
        f"host routes reference agents not in agents=[...]: {sorted(missing)}"
    )
  dupes = sorted({n for n in names if names.count(n) > 1})
  if dupes:
    raise ValueError(f"duplicate agent names: {dupes}")
  _check_route_phrasings(host)


_INTENT_EQ_RE_CACHE: dict[str, "re.Pattern[str]"] = {}


def _intent_eq_value_dict(spec: Any, intent: str) -> Optional[str]:
  """The `<value>` a DECLARATIVE gate pins `intent` to, or None.

  Only `eq` leaves — reachable through `all` conjunctions — pin a single value; `any`/`not`
  express a set or a complement, so they do not identify one operation."""
  if not isinstance(spec, dict):
    return None
  if isinstance(spec.get("all"), list):
    for sub in spec["all"]:
      val = _intent_eq_value_dict(sub, intent)
      if val is not None:
        return val
    return None
  if spec.get("slot") == intent and isinstance(spec.get("eq"), str):
    return spec["eq"]
  return None


def _intent_eq_value(cond: Any, intent: str) -> Optional[str]:
  """Parse the value out of an `eq(...)`-style gate on the intent slot.

  Matches an `f.get('<intent>') == '<value>'` clause anywhere in the lambda-source
  string `cond` (tolerating additional `and`/`has(...)` gates around it) and returns
  `<value>`, or None if the condition doesn't gate on `intent`. Quote style (' or ")
  is tolerated for both the key and the value (Python `repr` picks either).

  A declarative-dict gate is read structurally instead — without this the journey oracle
  would report every dict-gated terminal as ungated."""
  if isinstance(cond, dict):
    return _intent_eq_value_dict(cond, intent)
  if not isinstance(cond, str) or not cond:
    return None
  pat = _INTENT_EQ_RE_CACHE.get(intent)
  if pat is None:
    pat = re.compile(
        r"""f\.get\(\s*['"]""" + re.escape(intent)
        + r"""['"]\s*\)\s*==\s*['"]([^'"]*)['"]"""
    )
    _INTENT_EQ_RE_CACHE[intent] = pat
  m = pat.search(cond)
  return m.group(1) if m else None


def _check_journey_gates(cfg: dict) -> list[str]:
  """Design-time oracle for a Shape-B journey's intent gating — the check CES lacks.

  A "journey-shaped" flow fans ONE intent slot out to N operation terminals, each
  gated on `intent == <op value>` (the migration's Shape B: intent -> shared spine ->
  intent-gated op terminals). The blessed framework validator has NO notion of this
  invariant, so CES will happily emit a journey whose op terminals are mis-gated,
  duplicated, or missing — a failure that only surfaces at RUNTIME (the wrong op
  fires, or the flow dead-ends). This is that missing compile-time oracle, done at the
  AUTHORING level exactly like `_check_route_phrasings`.

  STRUCTURAL definition (no naming convention): a flow is journey-shaped iff it has a
  slot with `kind == "intent"` carrying `option_cues`. For such a flow it verifies:

    * every terminal task is gated on `<intent> == <value>` for a `<value>` that IS a
      key in the intent slot's `option_cues` (unknown value -> error; ungated terminal
      -> error when the journey has >1 operation);
    * there is EXACTLY ONE terminal per intent value (missing / duplicate -> error).

  Non-terminal tasks may carry any gate (`has(...)`, other eq) and are ignored. A
  non-journey flow (no intent slot) yields `[]`. Returns human-readable error strings;
  `[]` means the journey's gating is sound.
  """
  slots = cfg.get("slots") or []
  intent = next(
      (s for s in slots if s.get("kind") == "intent" and s.get("option_cues")),
      None,
  )
  if intent is None:
    return []  # not journey-shaped
  # A Shape-B journey FANS OUT one intent to N (>=2) operation terminals. A flow with an intent slot but a
  # single terminal is a classification pattern ("classify the intent, then do one thing") — NOT a journey,
  # so the per-value-terminal invariant doesn't apply and we must not flag it.
  if sum(1 for t in (cfg.get("tasks") or []) if t.get("terminal")) < 2:
    return []
  intent_name = intent.get("name")
  values = list((intent.get("option_cues") or {}).keys())
  multi_op = len(values) > 1
  errors: list[str] = []

  by_value: dict[str, list[str]] = {}
  for t in cfg.get("tasks") or []:
    if not t.get("terminal"):
      continue
    tname = t.get("name", "<unnamed>")
    cond = t.get("condition")
    val = _intent_eq_value(cond, intent_name)
    if val is None:
      if multi_op:
        errors.append(
            f"terminal task {tname!r} is not gated on intent slot {intent_name!r} "
            f"(condition={cond!r}); a Shape-B journey with {len(values)} operations "
            "must gate every operation terminal on its intent value"
        )
        continue
      val = values[0]  # single-op journey: the lone terminal serves the one op
    elif val not in values:
      errors.append(
          f"terminal task {tname!r} gates on {intent_name}=={val!r}, which is not an "
          f"option of intent slot {intent_name!r} (valid: {values})"
      )
      continue
    by_value.setdefault(val, []).append(tname)

  for val in values:
    hits = by_value.get(val, [])
    if not hits:
      errors.append(
          f"intent value {val!r} has no operation terminal "
          f"(missing gated terminal for {intent_name}=={val!r})"
      )
    elif len(hits) > 1:
      errors.append(
          f"intent value {val!r} has {len(hits)} operation terminals ({hits}); "
          "expected exactly one gated terminal per intent value"
      )
  return errors


def _apply_remote_registry(all_map: dict[str, dict]) -> None:
  """Tell each config which of its tasks fire a REMOTE tool, and how to follow them up.

  The engine cannot infer this. A remote tool's start wrapper is an ordinary generated
  `api_tool`, indistinguishable from any other at runtime — and it answers with a job
  handle rather than the task's outputs, so ingesting that answer would complete the
  task on the turn it started, with none of its slots filled. The registry is what says
  "this one is not finished yet, and here is the tool that will tell you when it is".

  Scoped per config, so a flow carries no entry for a tool it never fires.
  """
  for cfg in all_map.values():
    fired = [t.get("tool") for t in (cfg.get("tasks") or []) if t.get("tool")]
    registry = _openapi.remote_registry(fired)
    if not registry:
      continue
    cfg["remote_tools"] = registry
    # The handle needs somewhere to land, and the author must never have to know that.
    # Declared here rather than by the author: naming it in the flow would put an
    # implementation detail of the transport into the conversation's vocabulary, and
    # an undeclared output slot is silently dropped by intake.
    have = {s.get("name") for s in (cfg.get("slots") or [])}
    for task in cfg.get("tasks") or []:
      entry = registry.get(task.get("tool"))
      if not entry:
        continue
      job = entry["job_slot"]
      # Mapped as one of the task's own outputs, so ordinary intake fills it from the
      # start call. Without this the handle is dropped and there is nothing to poll on.
      task.setdefault("outputs", {}).setdefault(job, job)
      if job not in have:
        cfg.setdefault("slots", []).append(
            {"name": job, "source": f"task:{task['name']}"})
        have.add(job)


def _assemble(
    app: App,
    filler_report: Optional[list] = None,
) -> tuple[dict[str, dict], dict[str, str], list[str]]:
  """Shared assembly: `(all_configs_map, tool_bodies, available_tools)`.

  Tool bodies = generated setters/executors + the @flows.tool registry + the app's
  raw `tool_bodies` (raw wins). Classifiers steer which setters are classifying.
  """
  root_cfg = app.root_flow.to_config()
  if not app.extra_flows:
    root_cfg = _apply_single_flow_gate(root_cfg)
  all_map = {app.config_id: root_cfg}
  for f in app.extra_flows:
    all_map[f.config_id] = f.to_config()
  # Steering (router_flow with route objects): add the handled route flows + the ONE
  # shared deferral flow, and register the generated deferral-recorder tool, so the rest
  # of assembly (surfaces, tool collection, validation) treats them like any other flow.
  spec = _steering_spec(app)
  if spec is not None:
    if spec.has_deferred:
      # FLAT mode (A2): the deferral recorder also derives detected_path from the baked-in
      # leaf->path map (the flat pick never had the model name the category).
      if spec.route_mode == "flat" and spec.leaf_paths:
        _name, _src, _keys = _steering.record_flat_intent_tool(
            spec.record_intent_name, spec.leaf_paths)
      else:
        _name, _src, _keys = _steering.record_intent_tool(spec.record_intent_name)
      _tools.register_source_tool(
          _name, _src, flows=[spec.defer_config_id], output_keys=_keys)
    # Multi-level steering: each INTERNAL route opens a classification flow (config id ==
    # the route name) carrying the sub_intent slot chain + a shared path-recorder tool.
    if spec.has_internal:
      _pname, _psrc, _pkeys = _steering.record_path_tool(spec.record_path_name)
      _tools.register_source_tool(
          _pname, _psrc,
          flows=[r.name for r in spec.routes if r.is_internal], output_keys=_pkeys)
    for cf in spec.child_flows(_router_no_input(app)):
      if cf.config_id not in all_map:
        all_map[cf.config_id] = cf.to_config()
  if app.language_switching == "select":
    all_map[app.config_id] = _apply_language_select(all_map[app.config_id], app)
  # Single pre-processing pass shared by validate + emit: derive terminal
  # readback_inputs from `sensitive` markers, then strip them (validator rejects
  # `sensitive`). Idempotent, so re-running (emit also calls validate_app) is safe.
  # Latency hiding runs LAST of these: it must see the `verbatim` that
  # `_apply_sensitive_readback` pins on a PHI/PCI task, because that blocks the hoist.
  all_map = {cid: _apply_automatic_fillers(
                 _apply_variable_maps(_apply_surfaces(
                     _apply_sensitive_readback(c), app), app), app, filler_report)
             for cid, c in all_map.items()}
  # Generate/re-render the toolset wrappers BEFORE collecting them: which operations
  # a flow uses, which response fields to lift, and the mock default are all only
  # knowable here (see each kind's `prepare_for_build`).
  _prepare_toolset_wrappers(app, all_map)
  _apply_remote_registry(all_map)
  # Agent-scoped extras are pulled in BY NAME and then checked, exactly as
  # `_assemble_multi` does: such a tool is named by no task and no slot, so flow
  # attachment alone leaves its body unemitted. This path used to do neither, so the
  # single-agent half of `extra_agent_tools` only worked by accident — a `@flows.tool`
  # with no `flows=` attaches to every flow and so came along anyway. One declared
  # against another app's flow was dropped, and a name with nothing behind it sailed
  # through validate, both surfacing as the post-emit `agent lists a tool the app does
  # not contain` after the whole tree had been built and thrown away.
  # `_remote_agent_names` first: it funnels through `_remote_agents`, which is where a
  # malformed A2A declaration gets its own error. `_extra_agent_tools` reads `.name`
  # off the same objects, so asking it first would report that as an AttributeError.
  bodyless = (_remote_agent_names(app) | _search_tool_names(app)
              | _agent_tool_names(app))
  extra = _extra_agent_tools(app)
  authored = {**_tools.collect_tools(all_map.keys(), names=extra),
              **(app.tool_bodies or {})}
  _add_language_tool(app, authored)
  # Multi-level steering registers one classifier setter per internal node (the by-meaning
  # child pick), merged UNDER any author classifiers of the same name (generated names are
  # namespaced `set_sub_intent__<node>`, so a collision would be intentional).
  _classifiers = dict(spec.head_classifiers() if spec is not None else {})
  _classifiers.update(app.classifiers or {})
  bodies, available = collect(
      [dict(c, _config_id=cid) for cid, c in all_map.items()],
      authored,
      classifiers=_classifiers,
      bodyless=bodyless,
  )
  _check_extra_tools(app.root_agent, extra, available)
  return all_map, bodies, available


def _assemble_multi(
    app: App,
    filler_report: Optional[list] = None,
) -> tuple[dict[str, dict], dict[str, str], list[str], dict[str, str], Optional[str]]:
  """Multi-agent assembly: `(all_configs, bodies, available, routes_by_name, host_cid)`.

  Each sub-agent's root flow gets the router-child gate; the engine strategy adds a
  synthesized host router config. The shared `set_active_flow` (flow key -> target
  agent) is passed through the authored bodies so `collect` scopes it in everywhere.
  """
  if app.language_switching == "select":
    raise ValueError(
        "language_switching='select' (turn-1 menu + lock) is currently single-agent "
        "only; use a root_flow App. explicit/auto switching work with multi-agent."
    )
  host = app.host
  routes_by_name = {k.lower().strip(): v.name for k, v in host.routes.items()}
  flow_keys = list(routes_by_name.keys())
  # Per-flow route cues (the flow key + the target Agent's aliases), so any
  # sub-agent can detect a lead-in-free switch to any sibling. Author-ordered,
  # never sorted (see _ROUTE_ORDER_NOTE).
  route_cues = _derived_route_cues(host)
  # Per-flow routing descriptions (flow key -> Agent.description), so the mid-flow
  # intent-first classifier judges a sibling switch by meaning — the SAME descriptions the
  # host routing SI uses. Empty when no agent declares one (classifier byte-identical).
  flow_descriptions = {k.lower().strip(): a.description
                       for k, a in host.routes.items() if a.description}
  all_map: dict[str, dict] = {}
  for ag in app.agents:
    all_map[ag.config_id] = _apply_router_child_gate(
        ag.flow.to_config(), flow_keys, route_cues,
        intent_first=host.robust_switching, flow_descriptions=flow_descriptions)
    for xf in ag.extra_flows:
      all_map[xf.config_id] = xf.to_config()
  host_cid: Optional[str] = None
  if host.strategy == "engine":
    host_cid, hc = _router_config_for(host)
    all_map[host_cid] = hc
  # Same sensitive-strip + readback + latency pass as the single-agent path (see
  # _assemble), so `flows validate` and emission agree and no stray `sensitive` or
  # `automatic_fillers` key reaches disk.
  all_map = {cid: _apply_automatic_fillers(
                 _apply_variable_maps(_apply_surfaces(
                     _apply_sensitive_readback(c), app), app), app, filler_report)
             for cid, c in all_map.items()}
  saf_body = _setters.gen_active_flow_router(routes_by_name)
  # Agent-scoped extras are pulled in BY NAME: such a tool is named by no task and no
  # slot, so flow attachment alone would leave its body unemitted (see collect_tools).
  extra_host = _host_extra_tools(app)
  extra_agents = _agent_extra_tools(app)
  _prepare_toolset_wrappers(app, all_map)  # see _assemble
  authored = {
      **_tools.collect_tools(all_map.keys(), names=[*extra_host, *extra_agents]),
      **(app.tool_bodies or {}),
      "set_active_flow": saf_body,
  }
  _add_language_tool(app, authored)
  bodies, available = collect(
      [dict(c, _config_id=cid) for cid, c in all_map.items()],
      authored,
      classifiers=app.classifiers or {},
      bodyless=(_remote_agent_names(app) | _search_tool_names(app)
                | _agent_tool_names(app)),
  )
  _check_extra_tools("host", extra_host, available)
  for ag in app.agents:
    _check_extra_tools(ag.name, list(ag.extra_tools or []), available)
  return all_map, bodies, available, routes_by_name, host_cid


def _host_extra_tools(app: App) -> list[str]:
  """Author-scoped extra tools for the HOST, order-preserving and deduped.

  Two sources, unioned: `HostRouter.extra_tools` (explicit, colocated with the host) and
  `App.extra_agent_tools` (app-level extras are the router's, the same rule
  `App.remote_agents` already follows — the host is the agent that talks to the caller
  between transfers). Language/remote-agent extras are added by the emit path itself, so
  this deliberately does NOT go through `_extra_agent_tools`."""
  if not app.is_multi_agent:
    return []
  out: list[str] = []
  for name in [*(app.host.extra_tools or []), *(app.extra_agent_tools or [])]:
    if name and name not in out:
      out.append(name)
  return out


def _agent_extra_tools(app: App) -> list[str]:
  """Every SUB-AGENT's `extra_tools`, flattened, order-preserving and deduped.

  Only used to pull the BODIES in by name (which agent scopes which name is decided
  per-agent in `_emit_multi_agent`); a body is emitted once per app either way."""
  if not app.is_multi_agent:
    return []
  out: list[str] = []
  for ag in app.agents or []:
    for name in ag.extra_tools or []:
      if name and name not in out:
        out.append(name)
  return out


def _check_extra_tools(owner: str, extra: list[str], available: list[str]) -> None:
  """An author-scoped extra tool must resolve to something REAL — else the agent ships
  listing a tool that does not exist, and the only symptom is a failed tool call
  mid-call.

  `available` already spans framework tools, every `<config_id>_dag`, declared remote
  agents (body-less by design) and every tool with a body, so the check is one lookup."""
  have = set(available)
  missing = [n for n in extra if n not in have]
  if missing:
    raise ValueError(
        f"{owner} extra tools have no tool to call: {sorted(missing)}. Give each one a "
        "body (@flows.tool, or App.tool_bodies={'name': source}) or declare it as a "
        "remote agent — scoping a name alone emits an agent that calls a tool the app "
        "does not contain."
    )


def _check_task_success_keys(
    cfg: dict, tool_keys: dict[str, list[str]]
) -> list[str]:
  """ERROR a task whose tool CANNOT report success — the silent hang.

  Intake reads `success = bool(response_data.get(task["success_check"]))` and applies
  the task's `outputs` only when that is truthy AND every declared key is present
  (`slot_intake._intake_executor`). A tool whose return model has no `success_check`
  field therefore looks FAILED on every call, and fills nothing at all: the result slot
  never arrives, and whatever waits on it — an announce, a downstream task, a verdict
  rung's run-flag — waits for the rest of the call.

  The blessed validator's `_check_task_output_keys` only covers keys named in `outputs`.
  That does catch a verdict spine task, whose run-flag deliberately rides the
  `success_check` key (so the key IS in `outputs` there) — but for every ordinary task
  the success key is nowhere in the config, and nothing checks it.

  Only tools with a pydantic return model are checked: their field set is closed and
  statically known, so this cannot false-positive. A tool returning a plain dict
  declares its keys nowhere, and is skipped.
  """
  errors: list[str] = []
  for task in cfg.get("tasks") or []:
    keys = tool_keys.get(task.get("tool") or "")
    if not keys:
      continue
    success_key = task.get("success_check", "success")
    if success_key not in keys:
      errors.append(
          f"Task '{task.get('name', '<unnamed>')}' checks success on key"
          f" '{success_key}' but tool '{task['tool']}' returns {sorted(keys)} — intake"
          " reads that key to decide the call worked, so the task would look failed"
          " every time and fill none of its outputs"
      )
  return errors


def _run_validation(
    all_map: dict[str, dict], bodies: dict[str, str], available: list[str]
) -> tuple[list[str], list[str]]:
  """Single + cross validation over a config map (shared by both paths)."""
  # Scoped to the tools actually collected for THIS app, so a same-named tool left in
  # the process-wide registry by another build cannot be checked against. Scoped by NAME
  # is not enough: the registry holds one entry per name for the whole process, so the
  # keys behind a shared name came from whichever module imported last. Resolving
  # against this app's flows is what makes them the right function's keys.
  tool_keys = {n: list(s.output_keys)
               for n, s in _tools.resolve_specs(all_map.keys(), names=bodies).items()
               if n in bodies}
  errors: list[str] = []
  warnings: list[str] = []
  # A tool body that reads a name nothing carries into the emitted file. `render_tool`
  # inlines the referenced pydantic models and the function, and nothing else, so a
  # module-level constant or helper the body closes over is left behind and the tool
  # dies on its first call with `name 'X' is not defined`. App-scoped rather than
  # per-config: a tool is emitted once and can be fired from several flows.
  for tool_name, names in sorted(_tools.registered_unresolved_globals().items()):
    if tool_name not in bodies:
      continue
    errors.append(
        f"Tool '{tool_name}' reads {names} but nothing defines them in the emitted"
        " file — only the referenced pydantic models and the function itself are"
        " inlined, so a module-level constant or helper is left behind and the tool"
        " fails on its first call with \"name '"
        f"{names[0]}' is not defined\". Inline the value, or move it inside the"
        " function."
    )
  for cid, c in all_map.items():
    _ok, e, w = _sv.raw_validate_single(
        c,
        available_tools=available,
        setter_sources=bodies,
        task_tool_sources=bodies,
        framework_root=FRAMEWORK_ROOT,
    )
    errors += [f"[{cid}] {m}" for m in e]
    warnings += [f"[{cid}] {m}" for m in w]
    # Authoring-level journey-gate oracle (Shape B), not in the blessed validator.
    errors += [f"[{cid}] journey-gate: {m}" for m in _check_journey_gates(c)]
    errors += [f"[{cid}] {m}" for m in _check_task_success_keys(c, tool_keys)]
  if len(all_map) > 1:
    _ok, ce, cw = _sv.raw_validate_cross(all_map, framework_root=FRAMEWORK_ROOT)
    errors += [f"[cross] {m}" for m in ce]
    warnings += [f"[cross] {m}" for m in cw]
  return errors, warnings


# ---------------------------------------------------------------------------
# Remote A2A agents (see flows.authoring.a2a).
# ---------------------------------------------------------------------------


def _steering_spec(app: App) -> "Optional[_steering.SteeringSpec]":
  """The `SteeringSpec` a `router_flow([...routes])` stashed on the root flow, or None.

  Present only for a single-agent router built from `flows.route(...)` objects; every
  other app (bare-key router, plain flow, multi-agent) returns None ⇒ no behaviour
  change."""
  root = getattr(app, "root_flow", None)
  return getattr(root, "_steering", None) if root is not None else None


def _router_no_input(app: App) -> "Optional[dict[str, Any]]":
  """The router root flow's `no_input`, for the flows the steering tree generates."""
  root = getattr(app, "root_flow", None)
  if root is None:
    return None
  return (root.to_config() or {}).get("no_input")


def _all_flows(app: App) -> list[Any]:
  """Every Flow the app builds, single-agent or multi-agent."""
  flows_: list[Any] = []
  if app.root_flow is not None:
    flows_.append(app.root_flow)
  flows_.extend(app.extra_flows or [])
  spec = _steering_spec(app)
  if spec is not None:
    # The handled route flows + the generated shared deferral flow — carried on the
    # routes, not in `extra_flows`, so collection (remote agents, search tools) sees them.
    flows_.extend(spec.child_flows(_router_no_input(app)))
  for ag in (app.agents or []):
    flows_.extend(ag.all_flows)
  return flows_


def _remote_agents(app: App) -> list[Any]:
  """Every declared remote A2A agent across the app, in declaration order, resolved.

  App-level agents come first, then each sub-agent's own. Deduplicated by name,
  because one tool resource is emitted per name and the host and a sub-agent
  legitimately share one. Two DIFFERENT agents under one name is a build error: the
  emitted resources would collide and which card survived would depend on ordering.

  Resolving here is what lets `ces_agent(...)` omit project/location: this is the
  first point at which the app they default to is known, and it is the single funnel
  every other A2A path goes through.
  """
  out: list[Any] = []
  seen: dict[str, Any] = {}
  for ra in [*(app.remote_agents or []),
             *(r for ag in (app.agents or []) for r in (ag.remote_agents or [])),
             *(r for fl in _all_flows(app) for r in getattr(fl, "_remote_agents", ()))]:
    if not isinstance(ra, _a2a.RemoteAgent):
      raise ValueError(
          "remote_agents must be built with flows.remote_agent(...) or "
          f"flows.ces_agent(...), got {type(ra).__name__}"
      )
    ra = ra.resolved_for(app.gcp_project, app.location)
    prior = seen.get(ra.name)
    if prior is None:
      seen[ra.name] = ra
      out.append(ra)
    elif prior != ra:
      raise ValueError(
          f"two different remote agents are both named {ra.name!r} — one tool "
          "resource is emitted per name, so give one of them a different name"
      )
  return out


def _agent_tools(app: App) -> list[Any]:
  """Every declared agent tool across the app, in declaration order, deduplicated.

  App-level first, then the ones a task carried in by being handed the declaration
  itself. One tool resource is emitted per name, so two DIFFERENT declarations sharing a
  name is a build error rather than a coin toss over which survives.
  """
  from . import agent_tool as _at  # noqa: PLC0415 (import cycle at module scope)
  out: list[Any] = []
  seen: dict[str, Any] = {}
  for at in [*(getattr(app, "agent_tools", None) or []),
             *(t for fl in _all_flows(app) for t in getattr(fl, "_agent_tools", ()))]:
    if not isinstance(at, _at.AgentTool):
      raise ValueError(
          "agent_tools must be built with flows.agent_tool(...), got "
          f"{type(at).__name__}")
    prior = seen.get(at.name)
    if prior is None:
      seen[at.name] = at
      out.append(at)
    elif prior != at:
      raise ValueError(
          f"two different agent tools are both named {at.name!r} — one tool resource is "
          "emitted per name, so give one of them a different name")
  return out


def _agent_tool_names(app: App) -> set[str]:
  """Tool names of every declared agent tool, emitted or carried."""
  return {at.name for at in _agent_tools(app)}


def _agent_tool_payloads(app: App) -> Optional[list[dict[str, Any]]]:
  """`agentTool` payloads for the scaffold request — the ones this app EMITS."""
  specs = [dict(at.tool_payload(), asynchronous=at.asynchronous)
           for at in _agent_tools(app) if at.emit]
  return specs or None


def _carried_agent_tool_names(app: App) -> Optional[list[str]]:
  """Names of agent tools declared `emit=False` — real, but not ours to write."""
  names = sorted(at.name for at in _agent_tools(app) if not at.emit)
  return names or None


def _helper_agent_payloads(app: App) -> Optional[list[dict[str, Any]]]:
  """`agents/<name>/` payloads for every declared helper agent."""
  helpers = getattr(app, "helper_agents", None) or []
  specs = [dict(h.agent_json(), instruction=h.instruction) for h in helpers]
  return specs or None


def _remote_agent_names(app: App) -> set[str]:
  """Tool names of every declared remote A2A agent."""
  return {ra.name for ra in _remote_agents(app)}


def _search_tools(app: App) -> list[Any]:
  """Every declared Google Search tool across the app, in declaration order.

  App-level first, then each sub-agent's own, then any carried in by a spliced
  `research()` task. Deduplicated by name, because one resource is emitted per name and
  a host and a specialist legitimately share one search. Two DIFFERENT tools under one
  name is a build error: the emitted resources would collide and which survived would
  depend on ordering.
  """
  out: list[Any] = []
  seen: dict[str, Any] = {}
  for st in [*(app.search_tools or []),
             *(s for ag in (app.agents or []) for s in (ag.search_tools or [])),
             *(s for fl in _all_flows(app) for s in getattr(fl, "_search_tools", ()))]:
    if not isinstance(st, _search.SearchTool):
      raise ValueError(
          "search_tools must be built with flows.search_tool(...), "
          f"got {type(st).__name__}"
      )
    prior = seen.get(st.name)
    if prior is None:
      seen[st.name] = st
      out.append(st)
    elif prior != st:
      raise ValueError(
          f"two different search tools are both named {st.name!r} — one tool resource "
          "is emitted per name, so give one of them a different name"
      )
  return out


def _search_tool_names(app: App) -> set[str]:
  """Tool names of every declared Google Search tool."""
  return {st.name for st in _search_tools(app)}


def _search_tool_payloads(app: App) -> Optional[list[dict[str, Any]]]:
  """The `googleSearchTool` payloads for the scaffold request (None when there are
  none, so an app without search emits exactly as before)."""
  specs = [st.tool_payload() for st in _search_tools(app)]
  return specs or None


def _check_search_tasks(
    all_map: dict[str, dict], search_names: set[str]
) -> tuple[list[str], list[str]]:
  """Reject a task that fires a search tool on terms intake cannot satisfy.

  A search response is `{search_query, snippets, instructions}` with no `success` key, and
  intake reads `success = bool(response_data.get(success_check))` while mapping `outputs`
  by FLAT top-level key. So the default `success_check="success"` reads every search as
  failed and escalates on the first fire, and mapping anything nested (a snippet's `text`,
  say) silently maps nothing. Passing the `SearchTool` object to `task()` gets both right;
  this catches the hand-written task that names the tool as a bare string.
  """
  errors: list[str] = []
  keys = ", ".join(repr(k) for k in _search.SEARCH_RESPONSE_KEYS)
  for cid, cfg in all_map.items():
    for task in cfg.get("tasks") or []:
      tool_name = task.get("tool")
      if tool_name not in search_names:
        continue
      name = task.get("name", "<unnamed>")
      success_check = task.get("success_check", "success")
      if success_check not in _search.SEARCH_RESPONSE_KEYS:
        errors.append(
            f"[{cid}] task {name!r} fires search tool {tool_name!r} with "
            f"success_check={success_check!r}, but a search response carries no such key "
            f"— it has only {keys}. Intake would read every search as failed and escalate "
            f"on the first fire. Pass the flows.search_tool(...) OBJECT as the task's tool "
            f"and this is set for you, or set success_check to "
            f"{_search.SEARCH_SNIPPETS_KEY!r} (empty exactly when the search found nothing)"
        )
      bad_outputs = sorted(set(task.get("outputs") or {}) - set(_search.SEARCH_RESPONSE_KEYS))
      if bad_outputs:
        errors.append(
            f"[{cid}] task {name!r} maps output key(s) {bad_outputs} from search tool "
            f"{tool_name!r}, but intake maps by flat top-level key and a search response "
            f"has only {keys}. The text is nested inside {_search.SEARCH_SNIPPETS_KEY!r} — "
            "map the snippets into a slot and let the model read them with then_directive"
        )
  return errors, []


def _remote_agent_payloads(app: App) -> Optional[list[dict[str, Any]]]:
  """The `remoteAgentTool` payloads for the scaffold request (None when there are
  none, so an app without A2A emits exactly as before)."""
  specs = [ra.tool_payload() for ra in _remote_agents(app)]
  return specs or None


# Toolset kinds share the same emit contract (a `{name, resource, spec?}` payload, one
# resource per name, no agent scoping) and differ only in their builder and their
# validation guards. `_TOOLSET_TYPES` is the whole set the app may declare.
_TOOLSET_TYPES = (_openapi.OpenApiToolset, _mcp.McpToolset)


def _toolsets(app: App) -> list[Any]:
  """Every declared toolset across the app (OpenAPI + MCP), in declaration order.

  App-level first, then each sub-agent's own. Deduplicated by name, because one
  resource is emitted per name and a host and a specialist legitimately share an API.
  Two DIFFERENT toolsets under one name is a build error: they would collide on disk
  and which one survived would depend on ordering.
  """
  out: list[Any] = []
  seen: dict[str, Any] = {}
  for ts in [*(app.toolsets or []),
             *(t for ag in (app.agents or []) for t in (ag.toolsets or []))]:
    if not isinstance(ts, _TOOLSET_TYPES):
      raise ValueError(
          "toolsets must be built with flows.openapi_toolset(...) or "
          f"flows.mcp_toolset(...), got {type(ts).__name__}"
      )
    prior = seen.get(ts.name)
    if prior is None:
      seen[ts.name] = ts
      out.append(ts)
    elif prior != ts:
      raise ValueError(
          f"two different toolsets are both named {ts.name!r} — one resource is "
          "emitted per name, so give one of them a different name"
      )
  return out


def _toolset_names(app: App) -> set[str]:
  """Display names of every declared toolset."""
  return {ts.name for ts in _toolsets(app)}


def _toolset_payloads(app: App) -> Optional[list[dict[str, Any]]]:
  """Scaffold payloads for the declared toolsets (None when there are none, so an
  app without toolsets emits exactly as before)."""
  return [ts.payload() for ts in _toolsets(app)] or None


def _guardrail_entries(app: App) -> list[Any]:
  """Every guardrail declared on the app or on any sub-agent, in declaration order."""
  host_gr = (app.host.guardrails or []) if app.host is not None else []
  return [*(app.guardrails or []), *host_gr,
          *(g for ag in (app.agents or []) for g in (ag.guardrails or []))]


def _guardrails(app: App) -> list[Any]:
  """The guardrail RESOURCES to emit, deduplicated by display name.

  One resource is emitted per name, so an app-level guardrail that a specialist also
  names is emitted once and referenced twice — that is the normal case, not an error.
  Two DIFFERENT resources under one name IS an error: they collide on disk and which
  one survived would depend on ordering.
  """
  out: list[Any] = []
  seen: dict[str, Any] = {}
  stems: dict[str, str] = {}
  for g in _gr.resources(_guardrail_entries(app)):
    prior = seen.get(g.name)
    if prior is None:
      # Two names differing only by space-vs-underscore land on the SAME
      # `guardrails/<stem>/<stem>.json`, so one would silently overwrite the other.
      owner = stems.get(g.dir_name)
      if owner is not None:
        raise ValueError(
            f"guardrails {owner!r} and {g.name!r} both emit to "
            f"guardrails/{g.dir_name}/ — the on-disk name is the display name with "
            "spaces replaced by underscores, so these two collide")
      stems[g.dir_name] = g.name
      seen[g.name] = g
      out.append(g)
    elif prior != g:
      raise ValueError(
          f"two different guardrails are both named {g.name!r} — one resource is "
          "emitted per name, so give one of them a different name")
  return out


def _guardrail_payloads(app: App) -> Optional[list[dict[str, Any]]]:
  """Scaffold payloads for the declared guardrails (None when there are none, so an
  app without guardrail resources emits exactly as before)."""
  return [g.payload_entry() for g in _guardrails(app)] or None


def _agent_guardrail_names(app: App) -> dict[str, list[str]]:
  """`{agent display name: [guardrail display name]}` for agents that declare any."""
  return {ag.name: [_gr.display_name(g) for g in ag.guardrails]
          for ag in (app.agents or []) if ag.guardrails}


def _check_guardrails(app: App) -> tuple[list[str], list[str]]:
  """Guardrail wiring: transfer targets exist, and the risky scope/action pairing.

  The scope check applies to an `llmPolicy` ONLY. A judged rule at `scope="agent"` cannot
  run until there is a response to judge, which on `gemini-3.1-flash-live` is after the
  words have streamed — so pairing it with `respond`/`generate` produces an agent that
  says the wrong thing and then corrects itself out loud (ces-probes `102`). A warning,
  not an error: it is the right choice on `gemini-composite-v1`, where the line really is
  suppressed.

  A `blocklist` is exempt, and that exemption is measured rather than assumed: a
  deterministic `contentFilter` at `scope="agent"` PREVENTS on both models (`108`).
  Warning about it would steer authors away from the one response-side control that works
  on the live model — which is what the first version of this check did.
  """
  errors: list[str] = []
  warnings: list[str] = []
  known = {ag.name for ag in (app.agents or [])}
  if app.host is not None:
    known.add(app.host.name)
  if not app.is_multi_agent:
    known.add(app.root_agent)

  for g in _guardrails(app):
    action = getattr(g, "action", None)
    target = getattr(action, "target", None) if action is not None else None
    if target is not None:
      name = getattr(target, "name", target)
      if name not in known:
        errors.append(
            f"guardrail {g.name!r} transfers to {name!r}, which is not an agent in "
            f"this app (have: {sorted(known)}) — CES resolves the target by name at "
            "deploy and a name that matches nothing fails silently")
    judged = "llmPolicy" in g.payload
    if (judged and g.scope == "agent" and action is not None
        and "transferAgent" not in action.body):
      warnings.append(
          f"guardrail {g.name!r} is scope='agent' with "
          f"{next(iter(action.body))} — on gemini-3.1-flash-live the caller hears the "
          "offending line BEFORE this action (ces-probes 102), so it corrects itself "
          "out loud. Use flows.transfer_to(...), or scope='user' if the rule can be "
          "judged from the caller's turn")

  declared = {g.name for g in _guardrails(app)}
  host_names = ([_gr.display_name(g) for g in (app.host.guardrails or [])]
                if app.host is not None else [])
  for owner, names in [("App", [_gr.display_name(g) for g in (app.guardrails or [])]),
                       *([(app.host.name, host_names)] if host_names else []),
                       *_agent_guardrail_names(app).items()]:
    for name in names:
      if name not in declared:
        warnings.append(
            f"{owner} names guardrail {name!r} but nothing here emits it — it must "
            "already exist on the target, or it is a name with no resource behind it "
            "and will never apply")
  return errors, warnings


def _prepare_toolset_wrappers(app: App, all_map: dict[str, dict]) -> None:
  """Generate/re-render every toolset's `pythonFunction` wrappers before they are
  collected. Each kind knows how to fill its own outputs and bake the mock default."""
  toolsets = _toolsets(app)
  _openapi.prepare_for_build(
      [t for t in toolsets if isinstance(t, _openapi.OpenApiToolset)],
      all_map, app.mock_apis)
  _mcp.prepare_for_build(
      [t for t in toolsets if isinstance(t, _mcp.McpToolset)],
      all_map, app.mock_apis)


def _environment_json(app: App) -> Optional[str]:
  """`environment.json` for env-scoped toolsets of any kind, or None when none are."""
  return _toolset_common.environment_json(_toolsets(app))


def _check_toolset_tasks(
    all_map: dict[str, dict], app: App
) -> tuple[list[str], list[str]]:
  """Cross-check tasks against the one thing a toolset cannot do: be called.

  A toolset is not a tool. It is a separate CES resource kind, it lands under
  `toolsets/` rather than `tools/`, and its operations exist only as sandbox symbols
  (`tools.<toolset>_<name>`) — never as entries in an agent's `tools[]`. The reference
  app bears this out: ten toolsets, and not one agent naming any of them.

  So a task that fires a toolset by name references a tool that does not exist. The
  generic availability check would say exactly that and leave the author looking for a
  missing tool, when the real answer is that they need a wrapper (`api_tool` for
  OpenAPI, `mcp_tool` for MCP). The same goes for naming the in-sandbox symbol
  `<toolset>_<name>` directly, which looks like it ought to work precisely because it is
  the symbol the wrapper calls.
  """
  errors: list[str] = []
  warnings: list[str] = []
  toolsets = _toolsets(app)
  if not toolsets:
    return errors, warnings
  by_name = {ts.name: ts for ts in toolsets}
  # What the author actually declared. NOT the `available` list: `collect` invents an
  # executor stub for any task tool it does not recognise, so both mistakes below are
  # "available" — as a `pythonFunction` emitted under the very name whose real
  # implementation lives elsewhere. Availability is the wrong thing to gate on here.
  declared = set(_tools.registered_output_keys()) | set(app.tool_bodies or {})
  for cid, cfg in all_map.items():
    for task in cfg.get("tasks") or []:
      tool_name = task.get("tool")
      if not tool_name:
        continue
      name = task.get("name", "<unnamed>")
      if tool_name in by_name:
        ts = by_name[tool_name]
        if isinstance(ts, _mcp.McpToolset):
          errors.append(
              f"[{cid}] task {name!r} fires {tool_name!r}, which is an MCP TOOLSET, "
              "not a tool — CES exposes its tools only inside the sandbox, and no agent "
              "can call a toolset. Declare the tool you want with "
              f"flows.mcp_tool(<name>, {tool_name}, <toolName>) and fire that instead"
          )
        else:
          errors.append(
              f"[{cid}] task {name!r} fires {tool_name!r}, which is an OpenAPI TOOLSET, "
              "not a tool — CES exposes its operations only inside the sandbox, and no "
              "agent can call a toolset. Declare the operation you want with "
              f"flows.api_tool(<name>, {tool_name}, <operationId>) and fire that instead"
          )
        continue
      if tool_name in declared:
        continue  # a real tool, whatever it happens to be named
      for owner, ts in sorted(by_name.items()):
        if not tool_name.startswith(f"{owner}_"):
          continue
        op = tool_name[len(owner) + 1:]
        if isinstance(ts, _mcp.McpToolset):
          # No spec to confirm the tool exists — but an undeclared name carrying the
          # toolset's own prefix is the in-sandbox MCP symbol (or a typo of it), and
          # either way the fix is a wrapper. CES discovers the tool at runtime.
          errors.append(
              f"[{cid}] task {name!r} fires {tool_name!r}, which looks like the "
              f"in-sandbox symbol for tool {op!r} of MCP toolset {owner!r} — it exists "
              "for a tool BODY to call, not as a tool an agent can be given. Wrap it "
              f"with flows.mcp_tool({tool_name!r}, {owner}, {op!r}) and fire the wrapper"
          )
          break
        if op in ts.operations:
          errors.append(
              f"[{cid}] task {name!r} fires {tool_name!r}, which is the in-sandbox "
              f"symbol for operation {op!r} of toolset {owner!r} — it exists for a tool "
              "BODY to call, not as a tool an agent can be given. Wrap it with "
              f"flows.api_tool({tool_name!r}, {owner}, {op!r}) and fire the wrapper"
          )
          break
  return errors, warnings


def _check_api_mocks(all_map: dict[str, dict], app: App) -> list[str]:
  """WARN when `mock_apis=True` leaves some toolset call still hitting the network.

  The flag reads as "this app is mocked", so a tool that never declared a mock and
  quietly goes live is the failure worth naming: an offline demo half-works, and the
  half that reaches out fails for reasons that look like the flow's fault.
  """
  if not app.mock_apis:
    return []
  meta = _tools.registered_meta()
  fired = {t.get("tool") for cfg in all_map.values() for t in (cfg.get("tasks") or [])}
  unmocked = sorted(
      name for name in fired
      if name in meta and ("operation" in meta[name] or "mcp_tool" in meta[name])
      and not meta[name].get("has_mock"))
  if not unmocked:
    return []
  return [
      f"App(mock_apis=True) but {', '.join(repr(n) for n in unmocked)} declared no "
      "mock, so those calls still go to the real API. Add them to the toolset's "
      "mocks={...}, pass mock= to the flows.api_tool(...) / flows.mcp_tool(...), or set "
      "a 'mock_<tool>' variable per session"
  ]


def _check_a2a_tasks(
    all_map: dict[str, dict], a2a_names: set[str]
) -> tuple[list[str], list[str]]:
  """Cross-check tasks that fire a remote A2A agent against how intake reads a reply.

  A remote agent does not answer in the shape an executor task expects. Its reply is
  the A2A `SendMessageResponse` oneof — `{"message": ...}` when it answers now,
  `{"task": ...}` when it has only accepted the work — and CES substitutes its own
  `{"result": "pending"}` when it defers the call. None of those carry a `success`
  key, and intake computes `success = bool(response_data.get(success_check))`, so a
  task left on the default `success_check` reads as failed on EVERY fire. With
  `on_failure.max_retries` defaulting to zero that escalates the flow the first time
  it runs, with nothing actually wrong — the same silent failure `awaits` exists to
  prevent for asynchronous python tools.

  `outputs` has the matching problem: intake maps by flat top-level key, and the
  reply text lives at `message.parts[].text`, so a key that is not one of the two
  kinds never resolves and the slot never fills.

  The `task`-reply warning is about a subtler trap. That reply is what an agent returns
  when it ACCEPTS work, so a task checking it passes on a receipt rather than an
  answer — and `awaits` cannot bridge the gap, because the engine enters a wait only
  for CES's `{"result": "pending"}` placeholder (`_is_async_pending`) and a `task` reply
  is a real response it never sees as pending.
  """
  errors: list[str] = []
  warnings: list[str] = []
  kinds = ", ".join(repr(a) for a in _a2a.A2A_REPLY_KINDS)
  for cid, cfg in all_map.items():
    for task in cfg.get("tasks") or []:
      tool_name = task.get("tool")
      if tool_name not in a2a_names:
        continue
      name = task.get("name", "<unnamed>")
      success_check = task.get("success_check", "success")
      if success_check not in _a2a.A2A_REPLY_KINDS:
        errors.append(
            f"[{cid}] task {name!r} fires remote agent {tool_name!r} with "
            f"success_check={success_check!r}, but an A2A reply carries no such key — "
            f"it is one of {kinds}. Intake would read every call as failed and escalate "
            "on the first fire. Use flows.delegate(), or set success_check to one of them"
        )
      bad_outputs = sorted(set(task.get("outputs") or {}) - set(_a2a.A2A_REPLY_KINDS))
      if bad_outputs:
        errors.append(
            f"[{cid}] task {name!r} maps output key(s) {bad_outputs} from remote agent "
            f"{tool_name!r}, but intake maps by flat top-level key and an A2A reply has "
            f"only {kinds}. The reply text is nested inside it — map the whole reply into a "
            "slot and read it with flows.delegate()"
        )
      if success_check == _a2a.A2A_TASK_REPLY:
        warnings.append(
            f"[{cid}] task {name!r} succeeds on the {_a2a.A2A_TASK_REPLY!r} reply, which an "
            "agent returns the moment it ACCEPTS the work — so the task passes on a "
            "receipt whose state may still be SUBMITTED, not on an answer. `awaits` "
            "does not bridge that: the engine enters a wait only for CES's "
            "'{\"result\": \"pending\"}' placeholder, and a `task` reply is a real response "
            f"it never sees as pending. Use the {_a2a.A2A_MESSAGE_REPLY!r} reply for a "
            "finished answer, or keep this one only if the agent returns COMPLETED "
            "`task` replies and let the reply task reject the unfinished ones"
        )
  return errors, warnings


def _async_tool_names(
    app: App, all_map: Optional[dict[str, dict]] = None,
) -> set[str]:
  """Every tool CES will run ASYNCHRONOUSLY — the decorator registry, the escape hatch,
  and every progressive fan-out leg wrapper. The same union `emit` writes
  `executionType` from, so validation and emission can never disagree about which tools
  are asynchronous.

  The leg wrappers are asynchronous by LOWERING, not by declaration: that is the whole
  mechanism. A synchronous leg blocks its dispatch, so the runtime could not hand the
  framework back a pass to narrate on until every leg had finished.
  """
  names = _tools.registered_async_tools() | set(app.async_tools or ())
  if all_map:
    names |= _fanout.leg_tool_names(all_map)
  return names


def _tool_timeout_map(all_map: Optional[dict[str, dict]] = None,
                      app: Optional["App"] = None) -> dict[str, int]:
  """Seconds per tool for `timeout: "<n>s"`, INCLUDING the generated leg wrappers.

  A leg is rewritten into a `<group>_<leg>_leg` tool, and the wrapper is the resource
  CES enforces a timeout against, so an author's `@tool(timeout=…)` has to be carried
  onto it. Without this the declaration is accepted, written onto a tool nothing
  dispatches, and the wrapper silently takes the 60s default — indistinguishable from
  the platform ignoring the setting.

  Args:
    all_map: Every config in the app, keyed by config id.

  Returns:
    {tool_name: seconds} for the author's tools plus each wrapper that inherits one.
  """
  declared = dict(_tools.registered_tool_timeouts())
  # `App.tool_timeouts` first, then the decorator registry on top, so a decorated tool
  # keeps saying what it always said and a raw-source body finally has a way to speak.
  if app is not None:
    declared = {**(getattr(app, "tool_timeouts", None) or {}), **declared}
  out = dict(declared)
  if all_map:
    for wrapper, source in _fanout.leg_tool_sources(all_map).items():
      if declared.get(source):
        out[wrapper] = declared[source]
  return out


def _check_async_pairing(
    all_map: dict[str, dict], async_tools: set[str],
    skip_tools: Optional[set[str]] = None,
) -> tuple[list[str], list[str]]:
  """Cross-check `executionType: ASYNCHRONOUS` against the tasks that fire those tools.

  The declaration lives in two places that the config validator cannot see at once: the
  tool resource carries `executionType`, and the task carries `awaits`. Neither half is
  wrong on its own, so nothing caught a mismatch until the flow ran.

  A task firing an asynchronous tool WITHOUT `awaits` is the failure the primitive
  exists to prevent: the platform's `{"result": "pending"}` placeholder is falsy under
  `success_check`, so it routes into `on_failure`, where `max_retries` defaults to 0 —
  the flow escalates on the very first fire, with nothing actually failed. That is an
  error. The mirror case is merely dead config: a synchronous tool answers with its real
  result, `awaits` never engages, and the author has a timeout they think is protecting
  them. That is a warning.

  `skip_tools` opts a tool out of BOTH halves. A remote A2A agent is neither
  synchronous nor declared asynchronous — the platform picks per call, which is why
  `awaits` on one is prudent rather than the dead config this would warn about.
  `_check_a2a_tasks` covers those tasks instead.

  A leg of a PROGRESSIVE group is the other exemption, and it has to be read off the
  group rather than the registry. Lowering replaces the leg's tool with a generated
  wrapper emitted `executionType: ASYNCHRONOUS`, so the leg genuinely does defer no
  matter what the author's own tool was declared as — and `parallel()` merges
  `deadline`/`waiting_say`/`while_waiting` onto exactly those legs. Judged by the
  registry the pair reads as dead config and warns on a correct group, which is worse
  than silence: it tells an author to delete the block that is working.
  """
  skip_tools = set(skip_tools or ())
  for cfg in all_map.values():
    for legs in _fanout.progressive_groups(cfg).values():
      skip_tools.update(leg["tool"] for leg in legs if leg.get("tool"))
  errors: list[str] = []
  warnings: list[str] = []
  for cid, cfg in all_map.items():
    for task in cfg.get("tasks") or []:
      tool_name = task.get("tool")
      if tool_name in skip_tools:
        continue
      if not tool_name:
        continue
      name = task.get("name", "<unnamed>")
      # A REMOTE tool is a third case, and both branches below would be wrong about it.
      # It is not `ASYNCHRONOUS` — every call it makes is a fast synchronous one — yet
      # its task genuinely waits, across turns, while the engine polls the job. So
      # `awaits` is right here and must not be warned about, and its absence is not the
      # error the first branch describes either.
      if (cfg.get("remote_tools") or {}).get(tool_name):
        continue
      is_async = tool_name in async_tools
      if is_async and not task.get("awaits"):
        errors.append(
            f"[{cid}] task {name!r} fires {tool_name!r}, declared ASYNCHRONOUS, but has "
            "no `awaits` block — CES answers that call with a 'pending' placeholder, "
            "which reads as a failure and escalates the flow on the first fire. Add "
            "awaits=flows.awaits(max_turns=...)"
        )
      elif task.get("awaits") and not is_async:
        warnings.append(
            f"[{cid}] task {name!r} declares `awaits` but {tool_name!r} is not "
            "asynchronous, so the wait never engages — mark the tool "
            f"@flows.tool(asynchronous=True) or drop the awaits block"
        )
  return errors, warnings


def assemble_for_lint(
    app: App,
) -> tuple[dict[str, dict], dict[str, str], list[str], Optional[str]]:
  """Assemble an App into `(all_configs, tool_bodies, available_tools, host_cid)`.

  The public entry the `flows.lint` package builds its `LintContext` from — the
  same assembled configs `validate_app` sees (openapi wrappers rendered, sensitive
  stripped, router gates applied), so the linter lints exactly what will emit.
  Raises `ValueError` on wiring the App cannot even assemble (the linter turns that
  into a single blocking finding).
  """
  if app.is_multi_agent:
    _check_multi_agent_wiring(app)
    all_map, bodies, available, _routes, host_cid = _assemble_multi(app)
    return all_map, bodies, available, host_cid
  all_map, bodies, available = _assemble(app)
  return all_map, bodies, available, None



def validate_app(app: App) -> tuple[list[str], list[str]]:
  """Validate every config single + cross. Returns `(errors, warnings)`."""
  if app.is_multi_agent:
    _check_multi_agent_wiring(app)
    all_map, bodies, available, _routes, _host_cid = _assemble_multi(app)
  else:
    all_map, bodies, available = _assemble(app)
  errors, warnings = _run_validation(all_map, bodies, available)
  a2a_names = _remote_agent_names(app)
  # A fan-out leg wrapper is asynchronous by lowering and its wait is owned by the
  # group's watcher, not by an `awaits` block — so it is exempt from BOTH halves of the
  # pairing check, exactly as a remote A2A agent is.
  _legs = _fanout.leg_tool_names(all_map)
  a_err, a_warn = _check_async_pairing(
      all_map, _async_tool_names(app, all_map), skip_tools=a2a_names | _legs)
  r_err, r_warn = _check_a2a_tasks(all_map, a2a_names)
  o_err, o_warn = _check_toolset_tasks(all_map, app)
  s_err, s_warn = _check_search_tasks(all_map, _search_tool_names(app))
  # NOT added to `skip_tools` above: unlike a remote A2A agent, an agent tool
  # genuinely defers, so a task firing an async one with no `awaits` earns the
  # ordinary pairing warning.
  t_err, t_warn = _check_agent_tasks(all_map, app)
  v_err, v_warn = _check_variable_maps(all_map, app)
  g_err, g_warn = _check_guardrails(app)
  return (errors + a_err + r_err + o_err + s_err + t_err + v_err + g_err,
          warnings + a_warn + r_warn + o_warn + s_warn + t_warn
          + _check_api_mocks(all_map, app)
          + _check_fanout_lowering(all_map, bodies) + v_warn + g_warn)


def _check_agent_tasks(
    all_map: dict[str, dict], app: App
) -> tuple[list[str], list[str]]:
  """Cross-check tasks that fire an agent tool against how the platform and intake read
  one, and check that the agent it names is somewhere findable.

  An agent's wire contract is entirely its own: the argument is `request`, the reply is
  `{"response": "<text>"}`, and nothing in it says whether it succeeded. Every one of
  those is a silent failure when it is wrong. A call made with the wrong argument name is
  rejected by the platform ("Missing request parameter in the agent tool call"); a task
  left on the default `success_check` reads a perfectly good answer as failed and, with
  `on_failure.max_retries` defaulting to zero, escalates on the first fire.

  Handing `task()` the declaration sets all three, so an app that does that never sees
  these. They exist for the app that names the tool as a bare string — a converted agent
  wiring a grafted specialist, which is exactly where the contract is least visible.
  """
  from . import agent_tool as _at  # noqa: PLC0415 (import cycle at module scope)
  errors: list[str] = []
  warnings: list[str] = []
  declared = {t.name: t for t in _agent_tools(app)}
  if not declared:
    return errors, warnings
  # Everything that could satisfy an `agent=` reference. A converted app grafts its
  # agents in AFTER emit, so an unresolvable name is a warning here and an error at
  # `flows check --app-dir`, which reads the built directory once the graft has run.
  known = {h.name for h in (getattr(app, "helper_agents", None) or [])}
  known |= {a.config_id for a in (app.agents or [])}
  known |= {getattr(a, "name", "") for a in (app.agents or [])}
  if app.root_agent:
    known.add(app.root_agent)
  # A timeout on a platform-executed call is decorative. Measured for an A2A agent
  # (ces-probes 136): the field is accepted, persisted, and IGNORED — a one-second budget
  # let a call answer after 3665ms while a python tool carrying the same budget in the
  # same app died at 1242ms. Whether an `agentTool` differs has not been measured, and
  # accepted-and-ignored is the worst of the options because the config reads back as
  # though the call were bounded. Say so rather than emit it quietly.
  _timeouts = _tool_timeout_map()
  for tool in declared.values():
    if tool.name in _timeouts:
      warnings.append(
          f"agent tool {tool.name!r} declares a timeout, which does not bound it — the "
          "platform makes this call, and a timeout on a platform-executed tool is "
          "accepted and ignored. Bound it with `asynchronous=True` plus an `awaits` "
          "policy on the task, which is the only budget that holds")
  for tool in declared.values():
    if tool.agent not in known:
      warnings.append(
          f"agent tool {tool.name!r} calls agent {tool.agent!r}, which this app does not "
          "declare. Fine if it arrives with a graft — `flows check --app-dir` will "
          "confirm it against the built app — but a typo here fails at the platform")
  for cid, cfg in all_map.items():
    for task in cfg.get("tasks") or []:
      tool = declared.get(task.get("tool"))
      if tool is None:
        continue
      name = task.get("name", "<unnamed>")
      inputs = task.get("inputs") or {}
      params = (list(inputs.values()) if isinstance(inputs, dict) else list(inputs))
      bad = [p for p in params if p != _at.AGENT_REQUEST_PARAM]
      if bad:
        errors.append(
            f"[{cid}] task {name!r} fires agent tool {tool.name!r} with "
            f"{sorted(bad)!r}, but an agent takes exactly one argument, "
            f"{_at.AGENT_REQUEST_PARAM!r}. The platform rejects any other name. Pass the "
            "declaration to flows.task(...) and it is mapped for you")
      if len(params) > 1:
        errors.append(
            f"[{cid}] task {name!r} fires agent tool {tool.name!r} with {len(params)} "
            f"inputs — an agent takes one. Compose them into a single slot")
      success_check = task.get("success_check", "success")
      if success_check != _at.AGENT_REPLY_KEY:
        errors.append(
            f"[{cid}] task {name!r} fires agent tool {tool.name!r} with "
            f"success_check={success_check!r}, but an agent's reply carries no such key "
            f"— it answers under {_at.AGENT_REPLY_KEY!r} and says nothing about success. "
            "Intake would read every answer as failed and escalate on the first fire")
      outputs = task.get("outputs") or {}
      if outputs and _at.AGENT_REPLY_KEY not in outputs:
        errors.append(
            f"[{cid}] task {name!r} maps outputs {sorted(outputs)!r} from agent tool "
            f"{tool.name!r}, which answers under {_at.AGENT_REPLY_KEY!r} — intake maps by "
            "flat top-level key, so the slot would never fill")
  return errors, warnings


def _check_variable_maps(
    all_map: dict[str, dict[str, Any]], app: App,
) -> tuple[list[str], list[str]]:
  """Variable maps against the app that declares them: reachable, and real."""
  if not app.variable_maps:
    return [], []
  errors, warnings = [], []

  # CES only puts DECLARED variables in session state, so an undeclared name is an
  # alternative that can never resolve. Worse than a dead map: the binding still
  # matches through its sibling spellings, so nothing looks wrong and the one session
  # that arrives under the missing name silently asks a question it should have skipped.
  # A raw dict here reaches `.bindings` and raises AttributeError from inside the
  # validator, which reads as a framework bug rather than an authoring one.
  wrong = [m for m in app.variable_maps
           if not isinstance(m, _variable_maps.VariableMap)]
  if wrong:
    return ([f"App(variable_maps=[...]) takes VariableMap objects, got"
             f" {type(wrong[0]).__name__}. Build them with flows.variable_map(...) —"
             " it is what normalizes the source forms and checks them."], [])

  # A slot name that exists in NO flow is a typo, and lowering hides it: the binding
  # is dropped per config, and if the map has another binding that does land, the
  # map-level check below still passes. The question that should have been skipped is
  # then asked, with nothing anywhere saying why.
  all_slots = {s["name"] for cfg in all_map.values() for s in cfg.get("slots") or []
               if isinstance(s, dict) and s.get("name")}
  for m in app.variable_maps:
    for slot in m.bindings:
      if slot not in all_slots:
        errors.append(
            f"variable_map({m.name!r}): slot {slot!r} exists in no flow. Check the"
            " name — the KEYS are slots and the values are the variables they are"
            " filled from, which is the easy way round to get backwards.")

  declared = {v.get("name") for v in _all_variables(app)}
  for m in app.variable_maps:
    for slot, b in m.bindings.items():
      missing = [e for e in b.var if e.split(".", 1)[0] not in declared]
      if missing:
        errors.append(
            f"variable_map({m.name!r}): slot {slot!r} reads"
            f" {', '.join(repr(x) for x in missing)}, which the app does not declare."
            " Add it to App(variables=[...]) — CES only surfaces declared variables,"
            " so this source can never resolve.")

  # A conditionally-active slot cannot be seeded. `fill_slots` skips a slot whose
  # condition is false, but ingress runs before `sm` is initialized and before the
  # config is compiled, so it cannot evaluate one — it would fill a slot the engine
  # itself would have declined. Refusing keeps the two paths honest; the alternative
  # is a slot whose meaning depends on whether it arrived by variable or by voice.
  for cid, cfg in all_map.items():
    conditional = {s["name"] for s in cfg.get("slots") or []
                   if isinstance(s, dict) and s.get("name") and s.get("condition")}
    for m in cfg.get("variable_maps") or []:
      for b in m["bindings"]:
        if b["slot"] in conditional:
          errors.append(
              f"[{cid}] variable_map({m['name']!r}): slot {b['slot']!r} has a"
              " `condition`, so whether it may be filled at all depends on the"
              " conversation. A variable map runs before the flow does and cannot"
              " evaluate that. Seed a slot the condition READS instead.")

  # A map naming no slot any flow holds is dead config that reads as working.
  landed = {m["name"] for cfg in all_map.values() for m in cfg.get("variable_maps", [])}
  for m in app.variable_maps:
    if m.name not in landed:
      errors.append(
          f"variable_map({m.name!r}): none of its slots exist in any flow, so it can"
          " never fill anything. Check the slot names — the KEYS are slots, the values"
          " are the variables they are filled from.")

  # Shadowing is judged AFTER lowering, per config: dropping a binding drops a
  # conjunct, so a map's discriminating condition can vanish in one flow while the map
  # stays selectable there. A warning, not an error — a map legitimately reachable in
  # one flow and shadowed in another is not an authoring mistake.
  for cid, cfg in all_map.items():
    for later, earlier in _variable_maps.shadowed(cfg.get("variable_maps") or []):
      warnings.append(
          f"[{cid}] variable_map({later!r}) can never be chosen here:"
          f" {earlier!r} is declared earlier and its slots are a subset, so whenever"
          f" {later!r} resolves {earlier!r} does too. Declare the most specific first.")
  return errors, warnings


def _check_fanout_lowering(
    all_map: dict[str, dict], bodies: dict[str, str],
) -> list[str]:
  """WARN for every fan-out group that will keep the old batch shape.

  The difference is entirely caller-visible and entirely invisible in the source: a
  lowered group speaks each finding the moment it lands, an un-lowered one says nothing
  until its slowest leg is back and then says everything. Nothing offline can tell them
  apart, so if this does not name the group nothing ever will.
  """
  out: list[str] = []
  for cid, cfg in all_map.items():
    for group, reason in sorted(_fanout.unlowered_groups(cfg, bodies).items()):
      out.append(
          f"[{cid}] parallel group {group!r} will NOT narrate progressively because "
          f"{reason}. The legs still fire together, but the caller hears nothing until "
          "the slowest one is back and then hears every line at once. Give every leg a "
          "real tool with a body (@flows.tool, or App.tool_bodies={'name': source}) to "
          "get a line per result as it arrives.")
  return out


# ---------------------------------------------------------------------------
# Emit failure handling.
#
# `scaffold.build` obeys a crash-envelope rule — it never raises, every failure
# comes back as `ScaffoldResult(ok=False, ...)`. That contract only holds up if
# the CALLER honours `ok=False`, and this one did not, twice over:
#
#   * it read the reason out of `validation.errors` ALONE, so the two failures
#     that are not dag-validation failures — a framework bundle that drifted from
#     its manifest, and duplicate UUIDs — printed `scaffold failed:` and nothing
#     else, and
#   * it raised AFTER `scaffold.build` had already written the tree to disk, so a
#     failed emit left a complete-looking app dir behind. That dir (framework
#     variableDeclarations only — the post-emit variable injection never ran) was
#     pushed to a live CES app and scored 24/43 before anyone read the emit log.
#
# So: say the real reason, and leave nothing deployable behind.
#
# Both halves live in `authoring/integrity.py` because the studio's `framework
# new` CLI made the SAME two mistakes at its own `scaffold.build` call site, and
# it cannot import this module (the emit stack) to fix them.
# ---------------------------------------------------------------------------

_FAILED_MARKER = _integrity.FAILED_MARKER
_scaffold_failure = _integrity.scaffold_failure_reason
_discard_tree = _integrity.discard_tree


class ScaffoldFailed(ValueError):
  """`scaffold.build` returned `ok=False`; the emit was aborted.

  A `ValueError` subclass so every existing `except ValueError` around the emit
  path (the CLI, authors' scripts) keeps catching it.
  """


def _abort_scaffold(
    res: ScaffoldResult, keep_failed: bool, exclude: tuple[str, ...] = (),
) -> None:
  """Raise on `ok=False`, taking the half-written tree with it."""
  reason = _scaffold_failure(res, exclude)
  note = _discard_tree(res.written_to, reason, keep_failed)
  raise ScaffoldFailed(f"scaffold failed: {reason}{note}")


def _asked_variables(res: ScaffoldResult, injected: list[dict[str, Any]]) -> list[str]:
  """Every variable this emit meant app.json to declare: the scaffold's framework
  set plus the ones the post-emit injection was asked to add."""
  scaffolded: list[str] = []
  for f in res.files:
    if f.path == "app.json":
      try:
        decls = json.loads(f.content).get("variableDeclarations") or []
      except ValueError:
        decls = []
      scaffolded = _integrity.variable_names(decls)
      break
  return list(dict.fromkeys([*scaffolded, *_integrity.variable_names(injected)]))


def _verify_emitted(
    out_dir: str,
    res: ScaffoldResult,
    *,
    agents: list[str],
    variables: list[str],
    all_map: dict[str, dict],
    keep_failed: bool,
    settings: Optional[dict[str, Any]] = None,
    carried_tools: Optional[set[str]] = None,
) -> None:
  """Post-emit self-check: fail the emit if the tree isn't what was asked for.

  Cheap by construction (one read of app.json + the agent jsons, a stat per tool,
  the framework hashes) because it runs on every emit.
  """
  tools = _integrity.emitted_tool_names(res.files) | _integrity.config_tool_names(
      all_map.values())
  # Minus the ones declared `emit=False`: a task names them, so config scanning finds
  # them, but this build deliberately wrote no resource for them.
  tools -= set(carried_tools or ())
  report = _integrity.verify_emitted(
      out_dir, agents=agents, variables=variables, tools=sorted(tools),
      settings=settings)
  if report.ok:
    return
  reason = report.summary()
  note = _discard_tree(res.written_to or out_dir, reason, keep_failed)
  raise _integrity.EmitIntegrityError(
      f"emit produced an INCOMPLETE app: {reason}{note}")


def emit(
    app: App, out_dir: str, *, overwrite: bool = True, keep_failed: bool = False,
) -> ScaffoldResult:
  """Emit `app` as a deployable CXAS app dir at `out_dir`.

  Single-agent: scaffolds the framework + dag + tools, scopes the agent's tools,
  injects vars, and writes instructions. Multi-agent (`host`+`agents`): emits a host
  router + N sub-agents (see `_emit_multi_agent`). Raises `ValueError` on validation
  errors, on a failed scaffold (`ScaffoldFailed`) and on a tree that does not match
  what was asked for (`EmitIntegrityError`) — in the last two cases the incomplete
  tree is removed, unless `keep_failed` marks and keeps it for debugging.
  """
  if app.is_multi_agent:
    return _emit_multi_agent(
        app, out_dir, overwrite=overwrite, keep_failed=keep_failed)

  filler_report: list = []
  all_map, bodies, _available = _assemble(app, filler_report)
  _report_filler_hoists(filler_report)
  # Progressive fan-out lowering is an EMIT step, deliberately not an assembly one.
  # `validate_app` has to see the program the AUTHOR wrote — two legs sharing a tool is
  # still the ambiguity it always was up there, even though the lowering happens to
  # resolve it by giving each leg its own wrapper — and the scaffold re-validates the
  # lowered config it actually writes, which is where the leg/peek/watch tools have to
  # resolve. Runs after `collect` (inside `_assemble`) because the wrapper inlines the
  # tool's own source, and a stub executor only exists once collect has generated it.
  # A no-op returning the same objects for an app with no fan-out group.
  # Captured BEFORE lowering: it repoints each leg's `tool` at its wrapper, so
  # afterwards there is nothing left that maps a wrapper back to the tool whose
  # `@tool(timeout=…)` it inherits.
  _pre_lowering = all_map
  all_map, bodies, _available = _fanout.apply(all_map, bodies, _available)
  root_cfg = all_map[app.config_id]
  extra = {cid: c for cid, c in all_map.items() if cid != app.config_id}

  errors, _warnings = validate_app(app)
  if errors:
    raise ValueError("flow validation failed:\n  " + "\n  ".join(errors))

  if overwrite and os.path.isdir(out_dir):
    shutil.rmtree(out_dir)
  req = ScaffoldRequest(
      app_display_name=app.app_display_name,
      config_id=app.config_id,
      root_agent=app.root_agent,
      model=app.model,
      gcp_project=app.gcp_project,
      location=app.location,
      mode="local",
      target_path=out_dir,
      config_override=root_cfg,
      tools_override=bodies,
      async_tools=sorted(_async_tool_names(app, all_map)),
      tool_timeouts=_tool_timeout_map(_pre_lowering, app),
      tool_descriptions=setter_descriptions(list(all_map.values())),
      remote_agent_tools=_remote_agent_payloads(app),
      agent_tools=_agent_tool_payloads(app),
      carried_agent_tools=_carried_agent_tool_names(app),
      helper_agents=_helper_agent_payloads(app),
      search_tools=_search_tool_payloads(app),
      toolsets=_toolset_payloads(app),
      guardrails=_guardrail_payloads(app),
      environment_json=_environment_json(app),
      extra_configs=extra,
      app_uuid=app.app_uuid,
      agent_uuid=app.agent_uuid,
  )
  res = _scaffold.build(req)
  if not res.ok:
    _abort_scaffold(res, keep_failed)

  # Post-emit: scope the agent tools, inject business vars, write instructions.
  os.makedirs(out_dir, exist_ok=True)
  agent_tools = _scope_agent_tools(out_dir, app, all_map)
  variables = (_all_variables(app) + _router_runtime_vars(app, all_map, agent_tools)
               + _engine_task_tools_var(all_map)
               + _variable_map_var(all_map))
  if variables:
    _inject_variables(out_dir, variables)
  if app.global_instruction is not None:
    with open(os.path.join(out_dir, "global_instruction.txt"), "w") as f:
      f.write(app.global_instruction.rstrip() + "\n")
  # Steering: append the generated <routing> block (built from the routes' descriptions)
  # to the author's persona, so a Route-based router needs no hand-written routing text.
  instr_text = app.agent_instruction
  _spec = _steering_spec(app)
  if _spec is not None:
    _block = _steering.routing_instruction(_spec)
    instr_text = (instr_text.rstrip() + "\n\n" + _block) if instr_text else _block
  if instr_text is not None:
    instr = os.path.join(out_dir, "agents", app.root_agent, "instruction.txt")
    if os.path.isfile(instr):
      with open(instr, "w") as f:
        f.write(instr_text.rstrip() + "\n")
  # Language: app.json languageSettings + the <language_detection> instruction block.
  _emit_language(out_dir, app, [app.root_agent])
  # App-LEVEL settings (timezone, guardrails, the app_settings long tail).
  _emit_app_settings(out_dir, app)
  # Author customization (steering + lifecycle hooks) on the single root agent.
  _emit_author_callbacks(out_dir, app.root_agent, app.steering, app.hooks)
  # After the author merge, so the ingress entry lands ahead of the author bucket.
  _emit_variable_map_ingress(out_dir, app, [app.root_agent])
  _verify_emitted(
      out_dir, res, agents=[app.root_agent],
      variables=_asked_variables(res, variables), all_map=all_map,
      settings=app.declared_settings, keep_failed=keep_failed,
      carried_tools={at.name for at in _agent_tools(app) if not at.emit})
  return res


# ---------------------------------------------------------------------------
# Multi-agent emit — host/steering router + N slot-filling sub-agents.
# ---------------------------------------------------------------------------


def _default_child_instruction(agent: Agent, host: Optional[HostRouter] = None) -> str:
  """A sub-agent's starter instruction (overridable via `Agent.instruction`).

  When `host` is given, appends a `<switching>` block naming each SIBLING flow and
  telling the model to hand off via `set_active_flow` (which the sub-agent carries)
  rather than cancel — so "actually I'd like <sibling> instead" mid-flow transfers
  to the sibling instead of ending the call.
  """
  domain = agent.flow.config_id.replace("_", " ")
  instr = (
      f"<role>\nYou are {agent.name}, the specialist that handles {domain}. "
      "Collect the information the system asks for, one thing at a time, and confirm "
      "details before finalizing.\n</role>\n"
  )
  if host is not None:
    siblings = [(k, a) for k, a in host.routes.items() if a.name != agent.name]
    if siblings:
      lines = "\n".join(
          f'  - If the caller wants {a.description or a.flow.config_id.replace("_", " ")}, '
          f'call set_active_flow with flow="{k.lower().strip()}" to hand off '
          "(do NOT cancel or end the call)."
          for k, a in siblings
      )
      instr += (
          f"\n<switching>\nIf the caller asks for a DIFFERENT service than {domain}, "
          "switch to the right specialist instead of cancelling:\n"
          f"{lines}\n</switching>\n"
      )
  return instr


def _default_host_instruction(host: HostRouter, agents: list[Agent]) -> str:
  """A steering host's greet + silent-routing instruction (bella_notte-modeled).

  Lists each route -> `set_active_flow(flow=<key>)` and, when `entry_var` is set,
  tells the model to honor an upstream intent tag. Silent routing: emit no text on
  a route so the specialist owns the first spoken turn.
  """
  by_name = {a.name: a for a in agents}
  greet = (
      f'Open the conversation with this greeting: "{host.welcome_message}"'
      if host.welcome_message
      else "Greet the caller and ask how you can help."
  )
  # Description-driven path: when EVERY route's agent carries a routing `description`, emit
  # the SAME shared <routing> block the single-agent router uses (one routing-SI style).
  _descs = [(k.lower().strip(),
             (by_name.get(ag.name).description if by_name.get(ag.name) else None))
            for k, ag in host.routes.items()]
  if all(d for _k, d in _descs):
    _entry = (f"\nIf an upstream {host.entry_var} tag is present, route to the matching "
              "flow immediately without asking." if host.entry_var else "")
    _block = _steering.routing_block([(k, d) for k, d in _descs], silent=True)
    return (
        "<role>\n"
        f"You are {host.name}, the entry point that routes each caller to the right "
        "specialist.\n</role>\n\n"
        "<persona>\n- Warm, brief, professional.\n"
        "- Never reveal internal system details, variable names, or tool names.\n"
        "</persona>\n\n"
        f"Greeting: {greet}\n\n"
        f"{_block}{_entry}\n"
    )
  # Fallback: config_id-derived taskflow (unchanged; back-compat for hosts without
  # per-route descriptions).
  lines = []
  for key, ag in host.routes.items():
    target = by_name.get(ag.name)
    hint = (target.flow.config_id if target else ag.name).replace("_", " ")
    lines.append(
        f"        - If the user wants {hint}: SILENT ROUTING — say nothing, and call "
        f'set_active_flow with flow="{key.lower().strip()}".'
    )
  entry = ""
  if host.entry_var:
    entry = (
        f"\n      If an upstream {host.entry_var} tag is present, route to the "
        "matching flow immediately without asking.\n"
    )
  routes_block = "\n".join(lines)
  return (
      "<role>\n"
      f"You are {host.name}, the entry point that routes each caller to the right "
      "specialist.\n"
      "</role>\n\n"
      "<persona>\n"
      "- Warm, brief, professional.\n"
      "- Never reveal internal system details, variable names, or tool names.\n"
      "</persona>\n\n"
      "<taskflow>\n"
      "  <subtask name=\"Greeting\">\n"
      "    <step name=\"Welcome\">\n"
      "      <trigger>Conversation begins.</trigger>\n"
      f"      <action>{greet}</action>\n"
      "    </step>\n"
      "  </subtask>\n"
      "  <subtask name=\"Route\">\n"
      "    <step name=\"Route_To_Specialist\">\n"
      "      <trigger>Caller states their request.</trigger>\n"
      "      <action>\n"
      f"{routes_block}\n"
      f"{entry}"
      "      </action>\n"
      "    </step>\n"
      "  </subtask>\n"
      "</taskflow>\n"
  )


def _host_tools(
    host: HostRouter, host_cid: Optional[str], extra: Optional[list[str]] = None
) -> list[str]:
  """The host agent's `tools[]` per strategy, plus any author-scoped `extra`."""
  if host.strategy == "transfer":
    base = {"end_session", "set_active_flow"}
  else:
    base = set(scoped_agent_tools(host_cid, [], []))  # engine + intake + control
    base |= {f"{host_cid}_dag", "set_active_flow", "slot_filling_engine", "slot_intake"}
    # The engine-strategy host IS a router; declare classify_turn_intent so the
    # re-entry forced-classify (router-scoped Pass A after a defer) has a tool to compel.
    base |= {"classify_turn_intent"}
  return sorted(base | set(extra or ()))


def _emit_variable_map_ingress(out_dir: str, app: App, agent_names: list[str]) -> None:
  """Write the ingress callback and register it FIRST on every slot-filling agent.

  Runs after `_emit_author_callbacks`, and inserts at index 0, so the entry lands
  ahead of the author `_00` bucket that `merge_registrations` prepends. That ordering
  is the contract: an author `before_agent` hook sees an already-seeded slot machine,
  so a hook whose job is to act on what the session arrived with can do it on turn 0.

  Author entries keep their position relative to the framework `_01`; nothing else
  about an app changes, and an app with no maps never reaches here.
  """
  if not app.variable_maps:
    return
  entry = {
      "pythonCode": None,  # per-agent, filled in below
      "description": "Session-variable ingress (pre-fill slots from the call)",
  }
  for agent_name in agent_names:
    rel = f"agents/{agent_name}/{_variable_maps.INGRESS_SUBDIR}/python_code.py"
    dest = os.path.join(out_dir, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w") as f:
      f.write(_variable_maps.ingress_source())
    aj_path = os.path.join(out_dir, "agents", agent_name, f"{agent_name}.json")
    if not os.path.isfile(aj_path):
      continue
    with open(aj_path) as f:
      aj = json.load(f)
    cbs = aj.setdefault("beforeAgentCallbacks", [])
    if any(c.get("pythonCode") == rel for c in cbs):
      continue
    cbs.insert(0, dict(entry, pythonCode=rel))
    with open(aj_path, "w") as f:
      json.dump(aj, f, indent=2)


def _emit_author_callbacks(out_dir, agent_name, steering, hooks) -> None:
  """Write an agent's author steering/hook files + merge their JSON registrations."""
  if steering is None and (hooks is None or not hooks.any()):
    return
  emitted = _customization.author_callbacks(agent_name, steering=steering, hooks=hooks)
  for rel, content in emitted.files:
    dest = os.path.join(out_dir, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w") as f:
      f.write(content)
  aj_path = os.path.join(out_dir, "agents", agent_name, f"{agent_name}.json")
  if os.path.isfile(aj_path):
    with open(aj_path) as f:
      aj = json.load(f)
    _customization.merge_registrations(aj, emitted)
    with open(aj_path, "w") as f:
      json.dump(aj, f, indent=2)


def _emit_multi_agent(
    app: App, out_dir: str, *, overwrite: bool = True, keep_failed: bool = False,
) -> ScaffoldResult:
  """Emit a host router + N slot-filling sub-agents as a deployable app dir."""
  host = app.host
  agents = app.agents

  _check_multi_agent_wiring(app)

  filler_report: list = []
  all_map, bodies, _available, _routes, host_cid = _assemble_multi(
      app, filler_report)
  _report_filler_hoists(filler_report)
  # Captured BEFORE lowering: it repoints each leg's `tool` at its wrapper, so
  # afterwards there is nothing left that maps a wrapper back to the tool whose
  # `@tool(timeout=…)` it inherits.
  _pre_lowering = all_map
  all_map, bodies, _available = _fanout.apply(all_map, bodies, _available)

  errors, _warnings = validate_app(app)
  if errors:
    raise ValueError("flow validation failed:\n  " + "\n  ".join(errors))

  # Per-agent scoped tools (sub-agents also carry set_active_flow for sibling hops).
  child_specs: list[ChildAgentSpec] = []
  agent_config_map: dict[str, str] = {}
  for ag in agents:
    agent_config_map[ag.name] = ag.config_id
    cfgs = [all_map[ag.config_id]] + [all_map[xf.config_id] for xf in ag.extra_flows]
    tools = scoped_agent_tools(
        ag.config_id, cfgs, [xf.config_id for xf in ag.extra_flows]
    )
    # set_active_flow lets the specialist hand off to a sibling mid-call; with
    # robust switching the specialist also classifies each turn (classify_turn_intent).
    tools = set(tools) | {"set_active_flow"}
    if host.robust_switching:
      tools.add("classify_turn_intent")
    if app.language_switching != "off":
      tools.add("update_language")
    tools |= {ra.name for ra in (ag.remote_agents or [])}
    tools |= {st.name for st in (ag.search_tools or [])}
    # Author-scoped extras for THIS specialist (the mirror of HostRouter.extra_tools).
    # Scoping is per-agent for the same reason it is everywhere else here: a knowledge
    # tool the caller may reach mid-journey belongs to the journeys that offer it, and
    # to no other. The bodies were pulled in by name in _assemble_multi, and an
    # unresolvable name already failed there.
    tools |= set(ag.extra_tools or [])
    tools = sorted(tools)
    child_specs.append(ChildAgentSpec(
        name=ag.name,
        instruction=(ag.instruction or _default_child_instruction(ag, host)),
        tools=tools,
        guardrails=[_gr.display_name(g) for g in (ag.guardrails or [])],
    ))

  default_cid: Optional[str] = None
  if host.strategy == "engine":
    agent_config_map[host.name] = host_cid
    default_cid = host_cid

  host_tools = _host_tools(host, host_cid, _host_extra_tools(app))
  if app.language_switching != "off":
    host_tools = sorted(set(host_tools) | {"update_language"})
  # App-level remote agents are the ROUTER's to call: it is the agent that talks to
  # the caller between transfers. A sub-agent's own are scoped in above.
  if app.remote_agents:
    host_tools = sorted(set(host_tools) | {ra.name for ra in app.remote_agents})
  # App-level search is the router's too, on the same reasoning.
  if app.search_tools:
    host_tools = sorted(set(host_tools) | {st.name for st in app.search_tools})
  host_spec = HostAgentSpec(
      name=host.name,
      instruction=(host.instruction or _default_host_instruction(host, agents)),
      strategy=host.strategy,
      child_agents=[a.name for a in agents],
      tools=host_tools,
      guardrails=[_gr.display_name(g) for g in (host.guardrails or [])],
  )

  if overwrite and os.path.isdir(out_dir):
    shutil.rmtree(out_dir)
  req = MultiAgentScaffoldRequest(
      app_display_name=app.app_display_name,
      gcp_project=app.gcp_project,
      location=app.location,
      model=app.model,
      mode="local",
      target_path=out_dir,
      host=host_spec,
      agents=child_specs,
      all_configs=all_map,
      tools_override=bodies,
      async_tools=sorted(_async_tool_names(app, all_map)),
      tool_timeouts=_tool_timeout_map(_pre_lowering, app),
      tool_descriptions=setter_descriptions(list(all_map.values())),
      remote_agent_tools=_remote_agent_payloads(app),
      agent_tools=_agent_tool_payloads(app),
      carried_agent_tools=_carried_agent_tool_names(app),
      helper_agents=_helper_agent_payloads(app),
      search_tools=_search_tool_payloads(app),
      toolsets=_toolset_payloads(app),
      guardrails=_guardrail_payloads(app),
      environment_json=_environment_json(app),
      agent_config_map=agent_config_map,
      default_config_id=default_cid,
      intent_config_map=None,
      app_uuid=app.app_uuid,
  )
  res = _scaffold.build_multi_agent(req)
  if not res.ok:
    # A transfer host carries its OWN callbacks by design; the scaffold's drift
    # gate skips that dir, so the reason must skip it too (see build_multi_agent).
    _abort_scaffold(
        res, keep_failed,
        exclude=(f"agents/{host.name}/",) if host.strategy == "transfer" else ())

  # Post-emit: business vars, global instruction, author customization per agent.
  os.makedirs(out_dir, exist_ok=True)
  variables = _all_variables(app) + _variable_map_var(all_map)
  if variables:
    _inject_variables(out_dir, variables)
  if app.global_instruction is not None:
    with open(os.path.join(out_dir, "global_instruction.txt"), "w") as f:
      f.write(app.global_instruction.rstrip() + "\n")
  # Language: app.json languageSettings + the <language_detection> block on the host
  # and every sub-agent (so the switch holds across a transfer).
  _emit_language(out_dir, app, [host.name, *[a.name for a in agents]])
  # App-LEVEL settings (timezone, guardrails, the app_settings long tail).
  _emit_app_settings(out_dir, app)
  _emit_author_callbacks(out_dir, host.name, host.steering, host.hooks)
  for ag in agents:
    _emit_author_callbacks(out_dir, ag.name, ag.steering, ag.hooks)
  # Sub-agents only: the host holds no user slots, so it has nothing to seed.
  _emit_variable_map_ingress(out_dir, app, [a.name for a in agents])
  _verify_emitted(
      out_dir, res, agents=[host.name, *[a.name for a in agents]],
      variables=_asked_variables(res, variables), all_map=all_map,
      settings=app.declared_settings, keep_failed=keep_failed,
      carried_tools={at.name for at in _agent_tools(app) if not at.emit})
  return res


def _scope_agent_tools(out_dir: str, app: App, all_map: dict[str, dict]) -> list[str]:
  """Write the scoped `tools[]` onto the root agent; returns the list (the router
  runtime vars need the same set to decide what to hide)."""
  tools = scoped_agent_tools(
      app.config_id,
      list(all_map.values()),
      [f.config_id for f in app.extra_flows],
      extra_agent_tools=_extra_agent_tools(app),
  )
  # A CARRIED agent tool (`emit=False`) is real, but its resource is not in what this
  # build writes — so listing it here fails the emit integrity check, which holds an
  # agent to the tools the emitted tree actually contains. Whatever grafts the resource
  # in declares it on the agent at the same time, which is where that wiring belongs.
  carried = {at.name for at in _agent_tools(app) if not at.emit}
  if carried:
    tools = [t for t in tools if t not in carried]
  # A single-agent router must DECLARE classify_turn_intent so the engine's re-entry
  # forced-classify (a router-scoped Pass A on the clean turn after a defer handed off)
  # has a tool to compel — otherwise hide_tools leaves it "the only option" while it was
  # never on the agent, and the forced-classify silently degrades to a blind re-route.
  # It is already in `_ROUTER_KEEP_TOOLS`, so `router_hide_tools` keeps it visible.
  if (all_map.get(app.config_id) or {}).get("router"):
    tools = sorted(set(tools) | {"classify_turn_intent"})
  aj_path = os.path.join(out_dir, "agents", app.root_agent, f"{app.root_agent}.json")
  if not os.path.isfile(aj_path):
    return tools
  with open(aj_path) as f:
    aj = json.load(f)
  aj["tools"] = tools
  with open(aj_path, "w") as f:
    json.dump(aj, f, indent=2)
  return tools


def _inject_variables(out_dir: str, variables: list[dict[str, Any]]) -> None:
  app_path = os.path.join(out_dir, "app.json")
  if not os.path.isfile(app_path):
    raise RuntimeError(
        f"cannot inject variables: {app_path} is missing "
        "(scaffold.build must run and succeed before post-emit steps)")
  with open(app_path) as f:
    app_json = json.load(f)
  vds = app_json.setdefault("variableDeclarations", [])
  have = {v.get("name") for v in vds}
  for v in variables:
    if v.get("name") not in have:
      vds.append(v)
  with open(app_path, "w") as f:
    json.dump(app_json, f, indent=2)


# ---------------------------------------------------------------------------
# App-LEVEL CES settings: app.json's top level (timeZoneSettings, guardrails, and
# the `app_settings` long tail). Absent from an App that declares none, so its
# emitted tree is byte-for-byte what it was before these fields existed.
#
# The interesting half is the DEPLOY, not the emit: `cxas push --overwrite` replaces
# the whole app, so `deploy/prep.py` merges these back from the live target — and
# always won, which is how a fresh app's platform-default timezone silently beat the
# source. Emit therefore also records WHICH of these keys the author owns
# (`declared-settings.json`), because the deploy is handed a path and cannot ask the
# `App`. Declared beats preserved; undeclared still falls back to the target.
# ---------------------------------------------------------------------------


def _emit_app_settings(out_dir: str, app: App) -> None:
  """Post-emit: write the author's app-LEVEL settings + record what they own."""
  keys = app.declared_setting_keys
  if not keys:
    return
  settings = app.declared_settings
  if settings:
    app_path = os.path.join(out_dir, "app.json")
    if not os.path.isfile(app_path):
      raise RuntimeError(
          f"cannot inject app settings: {app_path} is missing "
          "(scaffold.build must run and succeed before post-emit steps)")
    with open(app_path) as f:
      app_json = json.load(f)
    app_json.update(settings)
    with open(app_path, "w") as f:
      json.dump(app_json, f, indent=2)
  _integrity.write_declared_settings(out_dir, keys)


# ---------------------------------------------------------------------------
# Language: languageSettings emit + the update_language tool + <language_detection>
# instruction block. Off by default; enabled by App.languages / language_switching.
# ---------------------------------------------------------------------------


def _add_language_tool(app: App, authored: dict[str, str]) -> None:
  """Add the `update_language` body for caller-initiated switch modes. In `select` mode
  the choice slot uses a generic setter (auto-generated); the language is enforced by the
  before_model hook injecting a concrete directive, not by a tool write."""
  if app.language_switching in ("explicit", "auto"):
    authored.setdefault(
        "update_language",
        _setters.gen_update_language(_language.display_names(app.languages)),
    )


def _extra_agent_tools(app: App) -> list[str]:
  """Extra agent tools per language mode: `update_language` for explicit/auto switching;
  `try_again` for `select` (the drift-nudge hook re-invokes the model through it).

  Declared remote A2A agents are scoped in too. Unlike an ordinary tool, a remote agent
  is not necessarily named by any task — the reference pattern is a model-callable
  tool the instruction points at — so flow scanning alone would emit its resource and
  leave the agent unable to call it.
  """
  extra = list(app.extra_agent_tools or [])
  if app.language_switching in ("explicit", "auto") and "update_language" not in extra:
    extra.append("update_language")
  if app.language_switching == "select" and "try_again" not in extra:
    extra.append("try_again")
  for ra in app.remote_agents or []:
    if ra.name not in extra:
      extra.append(ra.name)
  # An agent tool is scoped in on the same terms: a task that fires one names it, but a
  # specialist the model is meant to reach on its own initiative is named by nothing, and
  # flow scanning alone would emit the resource and leave the agent unable to call it.
  #
  # Only the ones this app WRITES. A carried tool's resource is not in what we emit, so
  # listing it on the agent fails the integrity check ("agent lists a tool the app does
  # not contain") — and whatever grafts the resource in is what scopes it, on the same
  # pass. `emit=False` means neither the resource nor the wiring is ours.
  for at in _agent_tools(app):
    if at.emit and at.name not in extra:
      extra.append(at.name)
  # Search tools are scoped in on the same terms and for the same reason: one declared
  # for the model to reach on its own initiative is named by no task at all.
  for st in _search_tools(app):
    if st.name not in extra:
      extra.append(st.name)
  return extra


def _second_language_code(app: App) -> str:
  """The first non-default language code (the DTMF `press 9` target in select mode)."""
  default = app.resolved_default_language
  for c in app.languages:
    if c != default:
      return c
  return app.languages[-1] if app.languages else "es-US"


def _default_language_prompt(app: App) -> str:
  """A generated bilingual turn-1 menu when the author doesn't supply one."""
  second = _language.display_name(_second_language_code(app))
  base = _language.display_name(app.resolved_default_language) if app.resolved_default_language else "English"
  return (
      f"Welcome. For {second}, press 9 or say {second}. Otherwise, I'll continue in "
      f"{base}."
  )


def _apply_language_select(cfg: dict, app: App) -> dict:
  """Prepend the turn-1 language menu as the first USER slot (DTMF fills the first user
  slot, not the gate). The generic `set_language_choice` setter records the choice; the
  before_model lock hook derives `active_language` from the filled value."""
  out = dict(cfg)
  slots = list(out.get("slots", []))
  sel = _language.language_select(
      app.language_prompt or _default_language_prompt(app),
      second_code=_second_language_code(app),
  )
  insert_at = len(slots)
  for i, s in enumerate(slots):
    if s.get("source") == "user":
      insert_at = i
      break
  slots.insert(insert_at, sel)
  out["slots"] = slots
  return out


def _language_var(app: App) -> dict[str, Any]:
  """The `active_language` variableDeclaration (default = the default display name)."""
  default = app.resolved_default_language
  default_name = _language.display_name(default) if default else ""
  return {
      "name": "active_language",
      "description": (
          "The active conversation language (display name). Set by update_language; "
          "read by the <language_detection> instruction to stay in-language."
      ),
      "schema": {"type": "STRING", "default": default_name},
  }


# Tools that stay visible on a router turn: the framework's own set plus the routing
# tool itself. Everything else on the agent is flow-specific and gets hidden, so the
# model's only move on a router turn is to route. Mirrors `scoped_agent_tools`.
_ROUTER_KEEP_TOOLS = frozenset({
    "slot_filling_engine", "slot_intake", "end_session", "cancel_flow",
    "transfer_to_human", "confirm_pending", "reject_pending", "set_slot_change",
    "try_again", "classify_turn_intent",
})


def _is_silent_config(cfg: dict[str, Any]) -> bool:
  """A SILENT flow has no setter for the model to call — nothing it can do to fill a
  slot or to enter the flow. Structural, not a new author knob: a collection flow always
  has at least one setter; a diagnostic fan-out (slots written by `task:` outputs) has
  none. Such a flow can only be reached as the router's `default_flow`."""
  return not any(s.get("setter") for s in cfg.get("slots", []))


def _engine_task_tools_var(all_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
  """`engine_task_tools` — every task executor in the app, for the model never to see.

  A task's `tool` is dispatched BY THE ENGINE, with the task's inputs. The model must
  never call it: a stray empty model call satisfies the task with no outputs and strands
  the flow. CES registers every tool on the agent as model-callable, and the engine's
  per-turn hide covers only the ACTIVE config's tasks — so in a single-agent multi-flow
  app every OTHER flow's tasks leak, and in ANY app every COMPONENT's tasks do.

  The blessed `before_model` already hides this set on every turn. It has been reading a
  variable that only `slotfill_migration` emitted, so a MIGRATED app had its executors
  hidden and a hand-authored SDK app silently did not — consumer and producer split
  across two layers, with the consumer in the engine every app runs.

  Component-reference tasks (a `component` key and no `tool`) carry no executor and are
  naturally excluded. Empty when the app has no tasks at all ⇒ no variable, and no
  behavior change for an app that never had one.
  """
  tools = sorted({t["tool"] for cfg in all_map.values()
                  for t in (cfg.get("tasks") or [])
                  if isinstance(t, dict) and t.get("tool")})
  if not tools:
    return []
  return [{
      "name": "engine_task_tools",
      "description": ("Engine-fired task executors — dispatched by the framework, never "
                      "model-callable; the blessed before_model hides them every turn."),
      "schema": {"type": "STRING", "default": json.dumps(tools)},
  }]


def _router_runtime_vars(
    app: App, all_map: dict[str, dict[str, Any]], agent_tools: list[str],
) -> list[dict[str, Any]]:
  """Runtime state vars for a single-agent router-over-flows app.

  A router root emits every flow's DAG onto ONE agent, so config switching and tool
  scoping have to happen per-turn rather than statically:

  * `flow_config_map` — the blessed `before_agent` resolver keys config off the
    `active_flow` gate through this map. WITHOUT it the resolver's active-flow branch
    never fires, the router config stays pinned for the whole call, and no child DAG
    ever drives. Identity, by the `{config_id}_dag` load convention.
  * `router_hide_tools` — on a router turn, hide every flow-specific tool so the model
    routes instead of "doing" the request by calling a flow's setter directly.
  * `default_flow` — the home-base flow `before_agent` seeds the gate to on a cold turn.
  * `silent_flow_configs` — flows with no model-callable setter; keeps the flow-specific
    tools hidden while one is active so the engine drives it instead of the model
    calling a sibling's setter.

  Empty for a non-router root ⇒ no behavior change for every existing SDK app."""
  root = all_map.get(app.config_id) or {}
  if not root.get("router"):
    return []
  spec = _steering_spec(app)
  if spec is not None:
    # Steering: every route key is a valid gate value; deferred keys resolve to the ONE
    # shared deferral config (a NON-identity map), so the chosen route label survives on
    # the gate for `detected_intent` while N routes share one flow.
    cmap = spec.config_map()
    flow_types = [r.name for r in spec.routes]
  else:
    flow_types = [f for f in (root.get("flow_types") or []) if f in all_map]
    cmap = {f: f for f in flow_types}
  if not flow_types:
    return []
  out: list[dict[str, Any]] = [{
      "name": "flow_config_map",
      "description": (
          "Maps each router flow (an active_flow gate value) to its in-agent DAG config "
          "id so the before_agent resolver activates that flow's DAG when the gate is set"
      ),
      "schema": {"type": "STRING", "default": json.dumps(cmap)},
  }]
  boot_tool = (root.get("bootstrap") or {}).get("tool") or "set_active_flow"
  hide = sorted(set(agent_tools) - _ROUTER_KEEP_TOOLS - {boot_tool})
  if hide:
    out.append({
        "name": "router_hide_tools",
        "description": (
            "Flow-specific tools (per-flow setters, engine-fired task executors, "
            "{flow}_dag config loaders) hidden from the model on a router turn so it "
            "routes via the bootstrap tool instead of calling a flow tool directly"
        ),
        "schema": {"type": "STRING", "default": json.dumps(hide)},
    })
  default_flow = (root.get("bootstrap") or {}).get("default_flow")
  if default_flow:
    out.append({
        "name": "default_flow",
        "description": (
            "Home-base flow: before_agent seeds the active_flow gate to it on a cold "
            "turn, so the flow's DAG runs on the turn carrying the caller's opening "
            "utterance rather than costing a separate routing turn"
        ),
        "schema": {"type": "STRING", "default": str(default_flow)},
    })
  # Resolve each gate key through the config map (deferred steering keys point at the
  # shared deferral config, not a same-named one) before asking whether it is silent.
  silent = [f for f in flow_types
            if _is_silent_config(all_map.get(cmap.get(f, f), {}))]
  if silent:
    out.append({
        "name": "silent_flow_configs",
        "description": (
            "Flows with no model-callable setter; the router's flow-specific tools stay "
            "hidden while one is active so the engine drives it end-to-end instead of "
            "the model calling a sibling flow's setter"
        ),
        "schema": {"type": "STRING", "default": json.dumps(silent)},
    })
  # Steering post-model capability (Phase B): the after_model callback reads these state
  # vars. Emitted ONLY when a Route-based router declares them, so the callback branch is
  # a strict no-op for every other app.
  if spec is not None:
    _backstop = spec.backstop_cues()
    if _backstop:
      out.append({
          "name": "steering_backstop",
          "description": (
              "Per-route POST-model keyword net: when the model declines to route, the "
              "after_model callback matches the caller's utterance against these and "
              "routes on a hit (a deterministic safety net UNDER the model)"),
          "schema": {"type": "STRING", "default": json.dumps(_backstop)},
      })
    _disambig = spec.disambiguate_config()
    if _disambig:
      out.append({
          "name": "steering_disambiguate",
          "description": (
              "Disambiguation budget {max_turns, on_exhaust}: after max_turns turns where "
              "the model still has not routed, the after_model callback routes the caller "
              "to the on_exhaust route (a hand-off), bounding the clarifying loop"),
          "schema": {"type": "STRING", "default": json.dumps(_disambig)},
      })
  return out


def _variable_map_var(all_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
  """The lowered variable-map table, keyed by config, for the ingress callback.

  The callback runs ahead of `sm` initialization and config resolution, so it cannot
  fetch a DAG; everything it needs is carried here instead. Keyed by config because a
  map is lowered per flow (see `_apply_variable_maps`). Absent when no flow kept a
  map, which keeps an app that declares none byte-identical.
  """
  table = {cid: cfg["variable_maps"] for cid, cfg in all_map.items()
           if cfg.get("variable_maps")}
  if not table:
    return []
  # A ROUTER gets the shapes its flows declare. Projection keeps nothing for it — a host
  # holds no user slots — so it would have no entry, and the ingress runs BEFORE routing
  # and can see no other id. Keyed strictly, a routed app was therefore never seeded on
  # the turn the variables actually arrive: the maps only became reachable once
  # `_active_config_id` named a real flow, a turn or more after routing, by which point
  # that flow has already asked for the value the map exists to supply.
  #
  # One entry per authored map, at its authored position, carrying the WIDEST projection
  # any flow gave it. Widest because the host cannot know which flow will run, and a
  # binding for a slot that flow turns out not to hold is inert; ordering is preserved
  # because "first declared wins" is the rule the author wrote against.
  widest: dict[str, dict[str, Any]] = {}
  order: list[str] = []
  for cid, cfg in all_map.items():
    if cfg.get("router"):
      continue
    for m in (cfg.get("variable_maps") or []):
      name = m.get("name")
      if name not in widest:
        order.append(name)
        widest[name] = m
      elif len(m.get("bindings") or []) > len(widest[name].get("bindings") or []):
        widest[name] = m
  if order:
    for cid, cfg in all_map.items():
      if cfg.get("router") and cid not in table:
        table[cid] = [widest[n] for n in order]
  return [{
      "name": "variable_maps_by_config",
      "description": (
          "Session-variable ingress: per config, the ordered variable shapes that "
          "pre-fill slots before the conversation starts."),
      "schema": {"type": "STRING", "default": json.dumps(table)},
  }]


def _all_variables(app: App) -> list[dict[str, Any]]:
  """Business variables, the `active_language` var, and the API mock flag."""
  variables = list(app.variables or [])
  if app.language_switching != "off":
    variables.append(_language_var(app))
  # Declared whenever the app has an API to mock, so the flag can be flipped on a
  # deployed app that was emitted live — a variable that only exists when
  # `mock_apis=True` could not be turned ON without a rebuild, which is the case that
  # matters (mocking a live app to reproduce something).
  if _toolsets(app):
    variables.append(_toolset_common.mock_flag_variable(app.mock_apis))
  return variables


def _emit_language(out_dir: str, app: App, agent_names: list[str]) -> None:
  """Post-emit: write app.json languageSettings + append the instruction block, and in
  `select` mode also emit the language-lock nudge hooks per agent.

  `languageSettings` is emitted whenever `languages` is set (even single-language); the
  instruction block + hooks only when `language_switching` is on. `select` uses the
  `<language_lock>` block (fixed language) instead of the switch-friendly
  `<language_detection>` block.
  """
  if not app.languages:
    return
  _inject_language_settings(out_dir, app)
  if app.language_switching == "off":
    return
  if app.language_switching == "select":
    block = (
        _language.language_menu_block(
            app.languages, app.resolved_default_language, _second_language_code(app))
        + "\n"
        + _language.language_lock_block(app.languages, app.resolved_default_language)
    )
  else:
    block = _language.language_detection_block(
        app.languages, app.resolved_default_language, app.language_switching
    )
  for name in agent_names:
    path = os.path.join(out_dir, "agents", name, "instruction.txt")
    if not os.path.isfile(path):
      continue
    with open(path) as f:
      existing = f.read()
    with open(path, "w") as f:
      f.write(existing.rstrip() + "\n\n" + block)
    if app.language_switching == "select":
      _emit_language_nudge(out_dir, name, app)


def _emit_language_nudge(out_dir: str, agent_name: str, app: App) -> None:
  """Emit the select-mode language-lock hooks (before_model sync/escalate at `_03`,
  after_model drift-nudge at `_03`) and register them in the agent JSON. `_03` keeps
  them off the framework's `_01` path (drift verify ignores them, like author hooks)."""
  before_rel = (f"agents/{agent_name}/before_model_callbacks/"
                "before_model_callbacks_03/python_code.py")
  after_rel = (f"agents/{agent_name}/after_model_callbacks/"
               "after_model_callbacks_03/python_code.py")
  files = {
      before_rel: _language.gen_language_before_model(
          app.languages, app.resolved_default_language),
      after_rel: _language.gen_language_after_model(),
  }
  for rel, content in files.items():
    dest = os.path.join(out_dir, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w") as f:
      f.write(content)
  aj_path = os.path.join(out_dir, "agents", agent_name, f"{agent_name}.json")
  if not os.path.isfile(aj_path):
    return
  with open(aj_path) as f:
    aj = json.load(f)
  aj.setdefault("beforeModelCallbacks", []).append(
      {"pythonCode": before_rel,
       "description": "Language lock: sync active_language + escalate on drift"})
  aj.setdefault("afterModelCallbacks", []).append(
      {"pythonCode": after_rel,
       "description": "Language lock: nudge back into active_language on drift"})
  with open(aj_path, "w") as f:
    json.dump(aj, f, indent=2)


def _inject_language_settings(out_dir: str, app: App) -> None:
  """Patch app.json `languageSettings` from the App's language config (flows-owned)."""
  app_path = os.path.join(out_dir, "app.json")
  if not os.path.isfile(app_path):
    raise RuntimeError(
        f"cannot inject languageSettings: {app_path} is missing "
        "(scaffold.build must run and succeed before post-emit steps)")
  with open(app_path) as f:
    app_json = json.load(f)
  default = app.resolved_default_language
  app_json["languageSettings"] = {
      "defaultLanguageCode": default,
      "supportedLanguageCodes": [c for c in app.languages if c != default],
      "enableMultilingualSupport": (
          app.language_switching != "off" or len(app.languages) > 1
      ),
  }
  with open(app_path, "w") as f:
    json.dump(app_json, f, indent=2)
