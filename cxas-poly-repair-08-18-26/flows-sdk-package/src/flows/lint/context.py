"""`LintContext`: build the read-model once, hand it to every rule.

Rules never re-parse or re-read; they read indices off this context. The two
expensive computations — the slot **fillability fixpoint** (mirrors the blessed
validator's `_check_reachability`) and the **tool reference graph** (via the one
canonical `config.tool_refs.referenced_tools` walk) — are computed lazily and
cached, so ~40 rules share one pass (see DESIGN.md section 4.2, 11).

The context operates on the ASSEMBLED raw config dicts — the same
`(all_map, bodies, available)` `validate_app` produces — so the linter sees
exactly what will be emitted (openapi wrappers rendered, sensitive stripped,
router gates applied).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, NamedTuple, Optional

from ..config import tool_refs as _tool_refs

# Spoken-text vocabulary, for the voice rules: every entry is text a caller HEARS.
# `then_directive` is excluded on purpose — it is an instruction the model composes its
# reply from, not verbatim TTS, so dash/format rules do not apply to it.
class SpokenItem(NamedTuple):
  """One caller-heard string. Four fields, in the order they have always been
  yielded, so `for kind, node, path, text in ctx.iter_spoken(cid)` keeps working —
  `LintContext` is exported, so out-of-tree rules unpack this. Delivery flags live on
  the response descriptor, which `iter_spoken_parts` yields alongside."""

  node_kind: str
  node: str
  json_path: str
  text: str


def normalize_sources(src: Any) -> list[str]:
  """A slot `source` as a list; legally a bare string, a list, or absent (->user)."""
  if isinstance(src, list):
    return [x for x in src if isinstance(x, str)]
  if isinstance(src, str):
    return [src]
  return ["user"]


def _slot_is_user_askable(slot: dict) -> bool:
  """A slot the caller is asked a question for (has an `ask`, user-sourced)."""
  sources = normalize_sources(slot.get("source"))
  return bool(slot.get("ask")) and "user" in sources


@dataclass
class LintContext:
  """Everything a rule needs, computed once."""

  app: Any
  configs: dict[str, dict]        # {config_id: raw config dict}
  bodies: dict[str, str]          # {tool_name: python source}
  available: list[str]            # tool names available to reference
  host_cid: Optional[str] = None  # the synthesized host router config, if any
  assembly_error: Optional[str] = None  # set when the App would not assemble

  # caches
  _fillable: dict[str, set[str]] = field(default_factory=dict)
  _refs: Optional[dict[str, dict]] = None

  # -- basic per-config accessors ---------------------------------------------

  def config_ids(self) -> list[str]:
    return list(self.configs.keys())

  def slots(self, cid: str) -> list[dict]:
    return [s for s in (self.configs[cid].get("slots") or []) if isinstance(s, dict)]

  def tasks(self, cid: str) -> list[dict]:
    return [t for t in (self.configs[cid].get("tasks") or []) if isinstance(t, dict)]

  def slot_map(self, cid: str) -> dict[str, dict]:
    return {s["name"]: s for s in self.slots(cid) if "name" in s}

  def task_map(self, cid: str) -> dict[str, dict]:
    return {t["name"]: t for t in self.tasks(cid) if "name" in t}

  def terminals(self, cid: str) -> list[dict]:
    return [t for t in self.tasks(cid) if t.get("terminal")]

  # -- tool reference graph ----------------------------------------------------

  def referenced_tools(self) -> dict[str, dict]:
    """Union across all configs of `{tool_name: {kind, ...}}` it is referenced by.

    The single canonical walk (slot setters, task tools, bootstrap, correction,
    intent_change, cancel/escalate, and every exhaust `then`), excluding reserved
    framework/control tools.
    """
    if self._refs is None:
      merged: dict[str, dict] = {}
      for cfg in self.configs.values():
        for name, how in _tool_refs.referenced_tools(cfg).items():
          merged.setdefault(name, how)
      self._refs = merged
    return self._refs

  def referenced_tool_names(self) -> set[str]:
    return set(self.referenced_tools().keys())

  def reserved_tool_names(self) -> set[str]:
    return _tool_refs.reserved_tool_names()

  # -- reachability (fillability fixpoint; mirrors _check_reachability) --------

  def fillable_slots(self, cid: str) -> set[str]:
    """Slots that CAN be filled from the fillable roots, transitively.

    Mirrors the blessed validator's `_check_reachability` so the linter's
    reachability verdict matches what the framework computes at emit time.
    """
    if cid in self._fillable:
      return self._fillable[cid]
    cfg = self.configs[cid]
    slots = self.slots(cid)
    boot = cfg.get("bootstrap") if isinstance(cfg.get("bootstrap"), dict) else {}
    boot = boot or {}
    gate_slot = cfg.get("gate_slot")

    fillable: set[str] = set()
    for slot in slots:
      name = slot.get("name", "<unnamed>")
      sources = normalize_sources(slot.get("source"))
      if slot.get("setter"):
        fillable.add(name)
      if "announce" in sources:
        fillable.add(name)
      if "event" in sources and slot.get("event_key"):
        fillable.add(name)
    for key in ("slot", "welcome_slot"):
      if boot.get(key):
        fillable.add(boot[key])
    if gate_slot:
      fillable.add(gate_slot)

    changed = True
    while changed:
      changed = False
      for slot in slots:
        name = slot.get("name", "<unnamed>")
        if name in fillable:
          continue
        reqs = slot.get("requires") or []
        if not reqs or not all(r in fillable for r in reqs):
          continue
        sources = normalize_sources(slot.get("source"))
        if (slot.get("setter") or "announce" in sources
                or ("event" in sources and slot.get("event_key"))):
          fillable.add(name)
          changed = True
      for task in self.tasks(cid):
        if task.get("component"):
          continue
        inputs = _task_input_slots(task.get("inputs"))
        if all(s in fillable for s in inputs) and all(
                s in fillable for s in (task.get("requires") or [])):
          for slot_name in (task.get("outputs") or {}).values():
            if slot_name not in fillable:
              fillable.add(slot_name)
              changed = True
      # Component tasks run a child DAG and merge its outputs into parent slots;
      # the canonical validator seeds those outputs (and a repeated component's
      # `collect` slot) as fillable once the component's inputs/requires are, so a
      # downstream slot fed by a component is not treated as unreachable.
      for task in self.tasks(cid):
        if not task.get("component"):
          continue
        inputs = _task_input_slots(task.get("inputs"))
        if all(s in fillable for s in inputs) and all(
                s in fillable for s in (task.get("requires") or [])):
          for slot_name in (task.get("outputs") or {}).values():
            if slot_name not in fillable:
              fillable.add(slot_name)
              changed = True
          collect = task.get("collect")
          if collect and collect not in fillable:
            fillable.add(collect)
            changed = True
    self._fillable[cid] = fillable
    return fillable

  def user_askable_slots(self, cid: str) -> list[dict]:
    return [s for s in self.slots(cid) if _slot_is_user_askable(s)]

  # -- spoken-text walker (voice rules) ---------------------------------------

  def iter_spoken(self, cid: str) -> Iterator[SpokenItem]:
    """Yield `(node_kind, node_name, json_path, text)` for every caller-heard string."""
    for item, _part in self.iter_spoken_parts(cid):
      yield item

  def iter_spoken_parts(self, cid: str) -> Iterator[tuple[SpokenItem, Optional[dict]]]:
    """`iter_spoken`, paired with the response descriptor the text came from.

    A rule that cares about HOW a line is delivered — `partial`, `interruptable` —
    needs the descriptor, not just the string. Kept as a separate method rather than a
    fifth tuple field because `LintContext` is exported public API, so widening
    `iter_spoken` would break any out-of-tree rule that unpacks it.

    The descriptor is None for text that is not part-shaped (an `ask`, a reprompt
    rung, a `filler_say`), which is most of them.
    """
    cfg = self.configs[cid]
    for i, slot in enumerate(self.slots(cid)):
      name = slot.get("name", f"<slot {i}>")
      base = f"slots[{i}]"
      for f_name in ("ask", "hint", "message"):
        val = slot.get(f_name)
        if isinstance(val, str) and val:
          yield SpokenItem("slot", name, f"{base}.{f_name}", val), None
      yield from _parts_text("slot", name, f"{base}.response", slot.get("response"))
      yield from _variants_text("slot", name, f"{base}.ask_variants",
                                slot.get("ask_variants"))
      yield from _filler_text("slot", name, base, slot.get("filler_say"))
      val = slot.get("validation")
      if isinstance(val, dict):
        yield from _ladder_text("slot", name, f"{base}.validation.reprompts",
                                val.get("reprompts"))
        yield from _exhaust_say("slot", name, f"{base}.validation", val.get("on_exhaust"))
        yield from _map_text("slot", name, f"{base}.validation.errors",
                             val.get("errors"))
      pb = slot.get("push_back")
      if isinstance(pb, dict):
        yield from _ladder_text("slot", name, f"{base}.push_back.reprompts",
                                pb.get("reprompts"))
        if isinstance(pb.get("say"), str) and pb["say"]:
          yield SpokenItem("slot", name, f"{base}.push_back.say", pb["say"]), None
    for i, task in enumerate(self.tasks(cid)):
      name = task.get("name", f"<task {i}>")
      base = f"tasks[{i}]"
      val = task.get("then_say")
      if isinstance(val, str) and val:
        yield SpokenItem("task", name, f"{base}.then_say", val), None
      yield from _filler_text("task", name, base, task.get("filler_say"))
      yield from _parts_text("task", name, f"{base}.then_response", task.get("then_response"))
      yield from _variants_text("task", name, f"{base}.then_say_variants",
                                task.get("then_say_variants"))
      of = task.get("on_failure")
      if isinstance(of, dict):
        if isinstance(of.get("retry_say"), str) and of["retry_say"]:
          yield SpokenItem("task", name, f"{base}.on_failure.retry_say",
                           of["retry_say"]), None
        yield from _exhaust_say("task", name, f"{base}.on_failure", of.get("on_exhaust"))
      aw = task.get("awaits")
      if isinstance(aw, dict):
        for f_name in ("say", "hold_say", "hold_ack"):
          if isinstance(aw.get(f_name), str) and aw[f_name]:
            yield SpokenItem("task", name, f"{base}.awaits.{f_name}", aw[f_name]), None
        for f_name in ("while_waiting", "hold_reprompts"):
          yield from _ladder_text("task", name, f"{base}.awaits.{f_name}",
                                  aw.get(f_name))
    for block in ("cancel", "escalate"):
      b = cfg.get(block)
      if isinstance(b, dict):
        for f_name in ("say", "confirm_say"):
          if isinstance(b.get(f_name), str) and b[f_name]:
            yield SpokenItem("field", block, f"{block}.{f_name}", b[f_name]), None
        dec = b.get("declined_say")
        if isinstance(dec, str) and dec:
          yield SpokenItem("field", block, f"{block}.declined_say", dec), None
        elif isinstance(dec, list):
          for j, entry in enumerate(dec):
            if isinstance(entry, str) and entry:
              yield SpokenItem("field", block, f"{block}.declined_say[{j}]",
                               entry), None
            elif isinstance(entry, dict):
              # A refusal REASON: `{"when": <condition>, "say": line | ladder}`. Its
              # lines reach a caller exactly as a plain ladder's do, so they are
              # linted the same way — a reason is not a place to hide unreviewed copy.
              say_ = entry.get("say")
              lines = [say_] if isinstance(say_, str) else (
                  say_ if isinstance(say_, list) else [])
              for k, line in enumerate(lines):
                if isinstance(line, str) and line:
                  suffix = "" if isinstance(say_, str) else f"[{k}]"
                  yield SpokenItem(
                      "field", block,
                      f"{block}.declined_say[{j}].say{suffix}", line), None
    ni = cfg.get("no_input")
    if isinstance(ni, dict):
      yield from _ladder_text("field", "no_input", "no_input.reprompts",
                              ni.get("reprompts"))
      yield from _exhaust_say("field", "no_input", "no_input", ni.get("on_exhaust"))
    if isinstance(cfg.get("all_done_say"), str) and cfg["all_done_say"]:
      yield SpokenItem("field", "all_done_say", "all_done_say",
                       cfg["all_done_say"]), None
    yield from _filler_text("field", "filler_say", "", cfg.get("filler_say"))
    yield from _parts_text("field", "readback_response", "readback_response",
                           cfg.get("readback_response"))


def relative_field(json_path: str) -> str:
  """A `NodeAnchor.field` relative to its node: drop the leading node locator.

  The anchor's `ref` already names the node, so `slots[3].ask` -> `ask` and
  `tasks[1].then_response[0]` -> `then_response[0]` keeps the field consistent with
  the DAG-level rules (which anchor `on_failure.on_exhaust.open_slot`, not
  `tasks[1].on_failure...`) and with what a Studio UI highlights.
  """
  return json_path.split(".", 1)[1] if "." in json_path else json_path


def _task_input_slots(inputs: Any) -> list[str]:
  """Task `inputs` is a list[str] OR a {slot: param} dict — return the slot names."""
  if isinstance(inputs, dict):
    return list(inputs.keys())
  if isinstance(inputs, list):
    return [s for s in inputs if isinstance(s, str)]
  return []


_Walk = Iterator[tuple[SpokenItem, Optional[dict]]]


def _parts_text(kind: str, node: str, base: str, parts: Any) -> _Walk:
  """Response descriptors — the only spoken text that carries delivery flags."""
  if not isinstance(parts, list):
    return
  for j, part in enumerate(parts):
    if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
      yield SpokenItem(kind, node, f"{base}[{j}].text", part["text"]), part


def _variants_text(kind: str, node: str, base: str, variants: Any) -> _Walk:
  """Surface-gated alternative wordings. A variant REPLACES the line it belongs to,
  so it is heard exactly as written and every voice rule applies to it."""
  yield from _parts_text(kind, node, base, variants)


def _ladder_text(kind: str, node: str, base: str, ladder: Any) -> _Walk:
  if not isinstance(ladder, list):
    return
  for j, line in enumerate(ladder):
    if isinstance(line, str) and line:
      yield SpokenItem(kind, node, f"{base}[{j}]", line), None


def _map_text(kind: str, node: str, base: str, mapping: Any) -> _Walk:
  """A {key: line} map, e.g. `validation.errors` keyed by error code."""
  if not isinstance(mapping, dict):
    return
  for key, line in mapping.items():
    if isinstance(line, str) and line:
      yield SpokenItem(kind, node, f"{base}.{key}", line), None


def _filler_text(kind: str, node: str, base: str, spec: Any) -> _Walk:
  """A `filler_say`, which is one line or a pool. `None` entries are silence, not
  text, so they are skipped rather than walked."""
  path = f"{base}.filler_say" if base else "filler_say"
  if isinstance(spec, str) and spec:
    yield SpokenItem(kind, node, path, spec), None
  elif isinstance(spec, list):
    for j, line in enumerate(spec):
      if isinstance(line, str) and line:
        yield SpokenItem(kind, node, f"{path}[{j}]", line), None


def _exhaust_say(kind: str, node: str, base: str, exhaust: Any) -> _Walk:
  if isinstance(exhaust, dict) and isinstance(exhaust.get("say"), str) and exhaust["say"]:
    yield SpokenItem(kind, node, f"{base}.on_exhaust.say", exhaust["say"]), None


def build_context(app: Any) -> LintContext:
  """Assemble an `App` and construct the shared `LintContext`.

  Assembly can raise (bad multi-agent wiring, an unresolvable extra tool); the
  linter turns that into a single blocking finding rather than crashing, since a
  config that will not assemble cannot be linted further.
  """
  from ..authoring import build as _build

  try:
    all_map, bodies, available, host_cid = _build.assemble_for_lint(app)
  except Exception as exc:  # noqa: BLE001 — a malformed App becomes a finding, not a crash
    # assemble delegates to wiring/normalization helpers that can raise more than
    # ValueError (a TypeError/AttributeError on malformed input), and a linter must
    # never crash on the very input it exists to critique.
    return LintContext(app=app, configs={}, bodies={}, available=[],
                       assembly_error=f"{type(exc).__name__}: {exc}")
  return LintContext(app=app, configs=all_map, bodies=bodies, available=available,
                     host_cid=host_cid)
