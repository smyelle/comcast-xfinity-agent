"""Python DSL for authoring slot-filling flows.

Every helper returns a plain slot/task/config **dict** — the exact shape the
framework validator and engine consume — so a flow authored in Python and the same
flow authored in YAML converge on an identical `Config`. Author with `Flow`:

    from flows import Flow, user_slot, announce, task, has

    tracking = Flow("acme_tracking", root_agent="Acme_Tracking_Agent",
                    bootstrap={"welcome_slot": "welcome"})
    tracking.add(
        announce("welcome", ["Sure, I can help you track that."], shared=True),
        user_slot("tracking_number", "What's your tracking number?"),
        result_slot("status_msg", "lookup_task"),
        announce("status", ["{status_msg}"], requires=["status_msg"], end=True),
    )
    tracking.task("lookup_task", "lookup_shipment", ["tracking_number"], "status_msg",
                  condition=has("tracking_number"))

`Flow.to_config()` returns the dict; `App` bundles flows + app-level settings for
emission (see flows.authoring.build).
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Literal, NoReturn, Optional, Sequence, Union

from . import agent_tool as _agent_tool
from . import guardrails as _guardrails
from . import search as _search
from .handoff import Handoff, HandoffPayload, as_handoff
from .say import Say, TextLike, floor_of, payloads_of, variants_of

# What a `handoff=` argument accepts at every site that takes one.
HandoffLike = Union[Handoff, HandoffPayload]

# ---------------------------------------------------------------------------
# Conditions — two interchangeable forms, both native to the engine:
#
#   * a `lambda f: ...` SOURCE STRING (what eq/ne/has/unset build), and
#   * a DECLARATIVE DICT — `{"slot": s, <op>: v}` leaves combined by `all`/`any`/`not`.
#
# The engine evaluates either (`_eval_part_condition` dispatches on the type), so every
# `condition=` site here accepts either. The dict form matters because a GENERATOR — a
# compiler lowering an IR, a config editor, a diff — already holds the gate as structure;
# flattening it into lambda source only for the engine to re-derive the meaning throws away
# exactly what every downstream reader wants, and there is no total inverse.
#
# The grammar below MIRRORS the blessed evaluator + framework validator
# (engine/framework/tools/{slot_filling_engine,validate_dag_config}) key for key. It is
# enforced at AUTHOR time so a typo'd operator fails next to the literal, not as a gate that
# quietly never opens in production.
# ---------------------------------------------------------------------------

# A condition is a lambda-source string or a declarative dict.
ConditionSpec = Union[str, dict[str, Any]]

# A latency filler: one line, or a pool to pick from at random. `None` INSIDE the pool
# means silence — an ordinary member, so "sometimes say nothing" is written the same way
# as "sometimes say this", and either is weighted by repeating it. Lines may reference
# filled slots (`"Let me pull up order {order_id}."`); one whose slot is not filled yet
# is skipped rather than spoken with its braces.
FillerLike = Union[str, list[Optional[str]]]

_CONDITION_LEAF_KEYS = frozenset({
    "slot", "capability", "surface", "eq", "neq", "in", "not_in", "filled",
    "gte", "lte", "gt", "lt", "upper", "default",
    # Turn-relative: filled, AND filled on an earlier turn than this one.
    "since_turns",
})
# Capabilities a `{"capability": ...}` leaf may name. Mirrors flows.surfaces —
# spelled out rather than imported so the DSL keeps its light import graph.
_SURFACE_CAPABILITIES = frozenset({
    "payloads", "brevity", "links", "filler", "keypad", "max_options",
})
_CONDITION_COMPARISON_OPS = frozenset({"gte", "lte", "gt", "lt"})
_CONDITION_VALUE_OPS = frozenset({"eq", "neq", "in", "not_in", "filled", "since_turns"})
_CONDITION_OPS = _CONDITION_COMPARISON_OPS | _CONDITION_VALUE_OPS


def _bad_condition(path: str, problem: str, fragment: Any) -> NoReturn:
  raise ValueError(f"condition {path}: {problem} — offending fragment: {fragment!r}")


def _check_condition(spec: Any, path: str = "<root>") -> None:
  """Raise `ValueError` unless `spec` is a well-formed declarative condition dict.

  `path` names the position of the failing fragment inside a nested gate
  (`<root>.all[1].not`), so the message points at the sub-dict to fix rather than the
  whole tree.
  """
  if not isinstance(spec, dict):
    _bad_condition(path, f"expected a dict, got {type(spec).__name__}", spec)
  keys = set(spec)

  for comb in ("all", "any"):
    if comb in keys:
      extra = sorted(keys - {comb})
      if extra:
        _bad_condition(path, f"{comb!r} combinator takes no other keys, found {extra}", spec)
      items = spec[comb]
      if not isinstance(items, list):
        _bad_condition(path, f"{comb!r} must be a list, got {type(items).__name__}", spec)
      if len(items) < 2:
        _bad_condition(
            path, f"{comb!r} needs at least 2 sub-conditions, got {len(items)}", spec)
      for i, sub in enumerate(items):
        _check_condition(sub, f"{path}.{comb}[{i}]")
      return

  if "not" in keys:
    extra = sorted(keys - {"not"})
    if extra:
      _bad_condition(path, f"'not' combinator takes no other keys, found {extra}", spec)
    _check_condition(spec["not"], f"{path}.not")
    return

  # A leaf reads one of three sources: a slot value, a capability of the delivery
  # surface, or the surface's name. The surface leaves make an operator OPTIONAL —
  # {"capability": "payloads"} is a truthiness test and {"surface": "voice"} is an
  # equality test, which is how they read at the call site and how authors expect
  # them to behave.
  if "capability" in keys or "surface" in keys:
    if "capability" in keys and "surface" in keys:
      _bad_condition(path, "a leaf reads 'capability' or 'surface', not both", spec)
    source = "capability" if "capability" in keys else "surface"
    if "slot" in keys:
      _bad_condition(
          path, f"a leaf reads {source!r} or 'slot', not both", spec)
    if not isinstance(spec[source], str):
      _bad_condition(
          path,
          f"{source!r} must be a string, got {type(spec[source]).__name__}", spec)
    if source == "capability" and spec[source] not in _SURFACE_CAPABILITIES:
      _bad_condition(
          path,
          f"unknown capability {spec[source]!r}; valid:"
          f" {sorted(_SURFACE_CAPABILITIES)}", spec)
    unknown = sorted(keys - _CONDITION_LEAF_KEYS)
    if unknown:
      _bad_condition(
          path,
          f"unknown condition key(s) {unknown}; valid:"
          f" {sorted(_CONDITION_LEAF_KEYS)}", spec)
    ops = keys & _CONDITION_OPS
    if len(ops) > 1:
      _bad_condition(
          path, f"leaf condition has {len(ops)} operators {sorted(ops)}; expected 1",
          spec)
    return

  if "slot" not in keys:
    _bad_condition(
        path,
        "leaf condition has no 'slot' key (a leaf gates exactly one slot, or reads"
        " 'capability'/'surface')", spec)
  if not isinstance(spec["slot"], str):
    _bad_condition(path, f"'slot' must be a string, got {type(spec['slot']).__name__}", spec)

  unknown = sorted(keys - _CONDITION_LEAF_KEYS)
  if unknown:
    _bad_condition(
        path,
        f"unknown condition key(s) {unknown}; valid: {sorted(_CONDITION_LEAF_KEYS)}", spec)

  ops = keys & _CONDITION_OPS
  if not ops:
    _bad_condition(
        path, f"leaf condition has no operator; expected one of {sorted(_CONDITION_OPS)}",
        spec)
  if len(ops) > 1:
    _bad_condition(
        path,
        f"leaf condition has multiple operators {sorted(ops)}; combine them under 'all'",
        spec)
  op = next(iter(ops))

  if "upper" in keys:
    if not isinstance(spec["upper"], bool):
      _bad_condition(path, f"'upper' must be a bool, got {type(spec['upper']).__name__}", spec)
    if op in _CONDITION_COMPARISON_OPS or op == "filled":
      _bad_condition(path, f"'upper' does not apply to the {op!r} operator", spec)

  # `default` is the value a numeric comparison reads when the slot is UNFILLED —
  # `int(filled.get(slot, spec.get("default", 0)))` in the engine's `_eval_condition`.
  # Every other operator takes the `filled.get(slot, "")` path a few lines below it and
  # never looks at `default` at all, so a `default` there is not a weaker gate, it is a
  # DEAD key: `{"slot": "x", "neq": "v", "default": "v"}` reads an absent x as "" and
  # fires, which is the opposite of what the author wrote.
  if "default" in keys and op not in _CONDITION_COMPARISON_OPS:
    _bad_condition(
        path,
        f"'default' only applies to a numeric comparison "
        f"{sorted(_CONDITION_COMPARISON_OPS)} — the {op!r} operator reads an unfilled "
        "slot as \"\" and IGNORES 'default' entirely; drop it, or say what you mean "
        "with an explicit {'any': [{...'filled': False}, {...}]}",
        spec)

  if op == "filled" and not isinstance(spec["filled"], bool):
    _bad_condition(path, f"'filled' must be a bool, got {type(spec['filled']).__name__}", spec)
  elif op in ("in", "not_in") and not isinstance(spec[op], list):
    _bad_condition(path, f"{op!r} must be a list, got {type(spec[op]).__name__}", spec)
  elif op in _CONDITION_COMPARISON_OPS:
    # INT, not "a number" — stricter than the framework validator (which takes
    # int|float) on purpose. The engine truncates the LEFT side to an integer
    # (`int(filled.get(...))`), so a fractional threshold or default cannot mean what
    # it says: `{"gt": 12.8}` is decided against an already-truncated value, and
    # `{"default": 12.8}` enters the comparison as 12. Both fail silently at runtime,
    # so they fail loudly here instead. A `bool` is an `int` to isinstance and is
    # never a threshold anyone meant.
    for key in ("default", op) if "default" in keys else (op,):
      val = spec[key]
      if not isinstance(val, int) or isinstance(val, bool):
        _bad_condition(
            path,
            f"{key!r} must be an int alongside {op!r} (the engine does int(value) on "
            f"the slot, so a fractional bound is decided against a truncated value), "
            f"got {type(val).__name__} {val!r}",
            spec)


def gate(spec: dict[str, Any]) -> dict[str, Any]:
  """Validate a DECLARATIVE condition dict and return it unchanged — the escape hatch.

  `eq`/`ne`/`has`/`unset` cover the single-slot cases and only ever compose by AND. When a
  gate is genuinely structural — nested any/all, a numeric threshold, a negation — author it
  as the dict the engine evaluates natively:

      condition=gate({"all": [{"slot": "verified", "eq": "yes"},
                              {"slot": "balance_due", "gt": 0}]})

  Passing the dict straight to `condition=` validates it identically; `gate()` earns its
  keep when a gate is BUILT (assembled in a loop, read from a spec) far from the slot it
  ends up on — the error then names the fragment at the point of construction.
  """
  _check_condition(spec)
  return spec


def _lambda_body(src: str, side: str = "condition") -> str:
  """The expression after `lambda f:` — raises unless `src` IS that form.

  Only a source STRING that is actually a lambda can be ANDed with another one (the
  body is spliced into a new lambda). A near-miss — `f.get("x") == 1`, an expression
  the author meant to wrap — has no `:` to split on, and the bare
  `src.split(":", 1)[1]` this replaces raised `IndexError` with no hint of which
  argument was wrong or what the format should have been.
  """
  head, sep, body = src.partition(":")
  if not sep or not head.strip().startswith("lambda"):
    raise ValueError(
        f"{side} string must be a `lambda f: ...` source string (what eq/ne/has/unset "
        f"build), e.g. \"lambda f: f.get('x') == 1\" — got {src!r}")
  return body.strip()


def _condition(cond: Optional[ConditionSpec]) -> Optional[ConditionSpec]:
  """Normalize a `condition=` argument for a builder: `None` when absent, else validated.

  A lambda-source string is passed through once its FORM is checked (the engine `eval`s
  it and the framework validator lints the body; a string that is not a lambda at all
  would `eval` to a value the engine then calls, i.e. a TypeError swallowed by
  `_is_slot_active`'s fail-open, leaving the gate permanently ACTIVE). A dict is checked
  against the declarative grammar HERE so a bad gate can never reach the Config — where
  a `filled`/`eq` typo yields a gate that silently never opens and an op that never fires.
  """
  if cond is None or cond == "":
    return None
  if isinstance(cond, str):
    _lambda_body(cond)
    return cond
  if isinstance(cond, dict):
    _check_condition(cond)
    return cond
  raise TypeError(
      "condition must be a `lambda f: ...` source string (see eq/ne/has/unset) or a "
      f"declarative dict (see gate()), got {type(cond).__name__}: {cond!r}")


def eq(slot: str, val: Any) -> str:
  """Active only when `slot` equals `val`."""
  return f"lambda f: f.get({slot!r}) == {val!r}"


def ne(slot: str, val: Any) -> str:
  """Active only when `slot` does not equal `val`."""
  return f"lambda f: f.get({slot!r}) != {val!r}"


def has(slot: str) -> str:
  """Active only when `slot` is set (truthy)."""
  return f"lambda f: bool(f.get({slot!r}))"


def unset(slot: str) -> str:
  """Active only while `slot` is falsy/absent."""
  return f"lambda f: not f.get({slot!r})"


def since(slot: str, *, turns: int = 1) -> dict[str, Any]:
  """Active once `slot` has been filled for at least `turns` caller turns.

  The gap this closes is an agent answering its own question. A branch that OFFERS
  something latches a slot as it speaks — so a branch reading that latch is satisfiable
  on the very same turn, and the model, holding both the question and the tool to
  answer it, supplies the answer itself and the caller never gets asked. `filled` alone
  cannot express "and not on the turn it appeared"; this can.

      flows.since("reboot_offered")          # from the NEXT turn onwards
      flows.since("offer_made", turns=2)     # two turns later
  """
  if turns < 1:
    raise ValueError(
        f"since({slot!r}): turns must be at least 1 — `turns=0` is just"
        " flows.has(), and spelling it this way hides that the guard does nothing.")
  return {"slot": slot, "since_turns": turns}


def escalated() -> str:
  """Active only once the caller has asked for a human.

  The engine's escalate rail fills the synthesized `escalate` slot before it runs an
  `escalate(tasks=[...])` chain, so a chain member gated on this is inert in the
  ordinary spine walk and eligible only on the hand-off path.
  """
  return "lambda f: bool(f.get('escalate'))"


def _check_and_operand(spec: Any, side: str) -> None:
  """Raise a fragment-naming error unless `spec` is a well-formed condition."""
  if isinstance(spec, dict):
    _check_condition(spec, side)
  elif isinstance(spec, str):
    _lambda_body(spec, side)
  else:
    raise TypeError(
        f"{side} must be a `lambda f: ...` source string or a declarative condition "
        f"dict, got {type(spec).__name__}: {spec!r}")


def _and_conditions(
    existing: Optional[ConditionSpec], extra: ConditionSpec
) -> ConditionSpec:
  """AND two conditions of the SAME form (or return `extra` if there is no existing one).

  The two forms cannot nest — a lambda string is opaque inside an `all`, and a dict is not
  Python source — so mixing them raises rather than silently keeping one gate and dropping
  the other.

  BOTH operands are validated here even though most callers hand over an already-checked
  gate, because the interesting ones do not: a `VerdictBranch.condition` and an
  `Operation.slots` gate are only checked much later, when the announce/slot they end up
  on is built. Taking a malformed one apart — `set(spec)` on a non-dict, splitting a
  string that is not a lambda — raises a bare TypeError/IndexError from the middle of the
  combinator, naming neither the offending fragment nor which side it came from.
  """
  _check_and_operand(extra, "<and:right>")
  if not existing:
    return extra
  _check_and_operand(existing, "<and:left>")
  if isinstance(existing, dict) != isinstance(extra, dict):
    raise ValueError(
        "cannot AND a declarative-dict condition with a lambda-source one — the forms do "
        f"not nest; express both the same way (got {existing!r} and {extra!r})")
  if isinstance(existing, dict):
    # Flatten a nested `all` so a twice-gated node reads as one conjunction, not a tree.
    left = existing["all"] if set(existing) == {"all"} else [existing]
    right = extra["all"] if set(extra) == {"all"} else [extra]
    return {"all": [*left, *right]}
  a = _lambda_body(existing, "<and:left>")
  b = _lambda_body(extra, "<and:right>")
  return f"lambda f: ({a}) and ({b})"


# ---------------------------------------------------------------------------
# Slot builders.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fallback:
  """One default value and the state it applies in."""

  value: Any
  when: Optional[Any] = None


def fallback(value: Any, *, when: Optional[ConditionSpec] = None) -> Fallback:
  """A conditional default: `value`, but only while `when` holds.

  Hand a list of these to a slot's `default=`; the first whose condition holds wins,
  and a bare value among them is the last resort. Conditions are the ordinary
  `has`/`eq`/`in_`/... builders, evaluated against filled slots, so a default can
  depend on what the rest of the flow found.
  """
  return Fallback(value=value, when=when)


def _defaults_to_config(default: Any) -> list[dict[str, Any]]:
  """Normalize `default=` into the emitted list-of-fallbacks form."""
  items = default if isinstance(default, list) else [default]
  out = []
  for item in items:
    if isinstance(item, Fallback):
      entry: dict[str, Any] = {"value": item.value}
      when = _condition(item.when)
      if when is not None:
        entry["when"] = when
      out.append(entry)
    else:
      out.append({"value": item})
  return out


def _value_policy(
    slot: dict[str, Any], *,
    default: Any = None,
    reject: Optional[Sequence[str]] = None,
    publish: Optional[Union[str, Sequence[str]]] = None,
    shared: bool = False,
    relatch: bool = False,
) -> dict[str, Any]:
  """Attach the value-handling keys every slot builder shares.

  Kept in one place because the four compose, and the composition is the point:
  `reject` decides a value never counted, which is what lets `default` then apply.
  Each key is omitted when unset, so a slot that uses none is byte-identical to
  what it was before these existed.
  """
  if reject:
    slot["reject"] = list(reject)
  if default is not None:
    slot["default"] = _defaults_to_config(default)
  if publish:
    slot["publish"] = [publish] if isinstance(publish, str) else list(publish)
  if shared:
    slot["shared"] = True
  if relatch:
    slot["relatch"] = True
  return slot


def event_slot(
    name: str,
    key: Optional[str] = None,
    *,
    default: Any = None,
    reject: Optional[Sequence[str]] = None,
    publish: Optional[Union[str, Sequence[str]]] = None,
    shared: bool = False,
    relatch: bool = False,
) -> dict[str, Any]:
  """A slot prefilled by an upstream system event (e.g. ANI).

  `default` covers the case the event did not: a slot the flow reads but no producer
  always supplies. Without one an absent value is indistinguishable from a benign
  one, which is how a low-priority branch wins a comparison it should have lost.
  `reject` names values that are present but mean "not answered yet" — an upstream
  sentinel — so they fall through to the default rather than filling the slot with
  something no branch matches. `publish` mirrors the value back out to session
  variables for tools that read state rather than parameters. `shared` keeps the
  value across a flow teardown or a `reset_on_complete` re-arm.

  `relatch` is for the event slot used as a LATCH -- a rung that fills it as it
  speaks, so a later rung can wait a caller turn on it with `flows.since(...)`.
  Ordinarily a rung's `sets` will not overwrite a slot that is already filled, which
  is right for a verdict but wrong for a latch that must re-arm: the second rung's
  write is skipped, the `since` stamp stays on the FIRST one, and every rung after it
  reads a gate that opened turns ago. With `relatch` the write lands and the stamp
  moves, so "wait until the caller has replied" keeps meaning that on the tenth step
  as much as the first.
  """
  slot = {"name": name, "source": "event", "event_key": key or name}
  return _value_policy(slot, default=default, reject=reject, publish=publish,
                       shared=shared, relatch=relatch)


def user_slot(
    name: str,
    ask: "TextLike",
    *,
    setter: Optional[str] = None,
    reprompts: Optional[list[str]] = None,
    max_retries: int = 3,
    on_exhaust: str = "I'm still having trouble hearing you.",
    on_exhaust_then: Optional[dict[str, Any]] = None,
    on_exhaust_fill: Optional[str] = None,
    on_exhaust_handoff: Optional["HandoffLike"] = None,
    dtmf: Optional[dict[str, str]] = None,
    requires: Optional[list[str]] = None,
    condition: Optional[ConditionSpec] = None,
    readback: bool = False,
    skip_readback_if_matches: Optional[list[str]] = None,
    hint: Optional[str] = None,
    validation: Optional[dict[str, Any]] = None,
    validation_rules: Optional[list[dict[str, Any]]] = None,
    sensitive: bool = False,
    repeated: Optional[dict[str, Any]] = None,
    verbatim: bool = False,
    reject: Optional[Sequence[str]] = None,
    publish: Optional[Union[str, Sequence[str]]] = None,
    shared: bool = False,
    filler_say: Optional["FillerLike"] = None,
    automatic_fillers: bool = True,
) -> dict[str, Any]:
  """A user-filled slot with a `set_<name>` setter + a No-Match reprompt ladder.

  The setter defaults to `set_<name>` (auto-generated at build unless you author a
  `@flows.tool` of that name). The No-Match exhaust escalates to a human by default.

  `reprompts` is the ladder, one rung per failed attempt (the engine clamps to the
  last rung); fewer than two rungs are padded with the generated defaults. It is
  bounded by `max_retries`, not by the DSL — rung N plays on attempt N+1 and
  `on_exhaust` fires on attempt `max_retries`, so only the first `max_retries - 1`
  rungs can ever be heard. A longer list WARNS rather than dropping its tail.

  `validation` — the full No-Match ladder verbatim, e.g. an error-code map:
  `{"max_retries": 3, "errors": {"invalid_length": "That should be 5 digits."},
  "on_exhaust": {"say": "...", "then": {"tool": "transfer_to_human"}}}`. When given it REPLACES the
  reprompt ladder built from `reprompts`/`max_retries`/`on_exhaust` (use one or the
  other). This is the shape a migration mines from a source agent's error handling.

  `validation_rules` — field-level checks (e.g. `{"kind":"length_digits","detail":"9"}`,
  `{"kind":"enum","detail":"a|b"}`) that the SETTER enforces on the captured value. This
  is orthogonal to the No-Match `validation` ladder above (which handles the
  caller-didn't-answer case): both are attached when given.

  `filler_say` — a line to cover the wait on the turn this slot is collected. The same
  kwarg a task takes, and it means the same thing: "there is a wait here, cover it".
  The delivery differs because the turn does — a task rides its line on the tool call,
  while here the line is spoken as a partial preempt and the model's own reply follows
  in the same turn. Pass a list to rotate, including `None` to sometimes stay silent:
  `["Let me take a look.", "One sec.", None]`. Keep it short and free of any claim the
  model has not made yet; a deterministic prefix can steer what it says next.

  `automatic_fillers=False` opts this slot out of the build-time hoist that would
  otherwise move a contentless opener off the front of `ask` into `filler_say`. Only
  meaningful on an app that turned the pass on with `App(automatic_fillers=True)`.

  `sensitive` — a BUILD-TIME marker (PHI/PCI slots like SSN). It is NOT a wire key: the
  build layer strips `sensitive` before validation/emit and uses it to gate terminal
  readback (so a sensitive value is never spoken back). It never reaches the Config the
  framework validator sees.

  `skip_readback_if_matches` — names EARLIER slots whose confirmed value makes this
  slot's readback redundant. When the captured value is digit-identical to any one of
  them, the readback is skipped and the value is accepted outright; a value matching
  none of them is read back as usual. Use it where a slot re-offers a number the caller
  already confirmed (a confirmation-SMS number offered against the verified mobile), so
  accepting the offer does not ask them to confirm the same digits a second time.
  Requires `readback=True` — there is nothing to skip otherwise.

  `on_exhaust_handoff` replaces the default `then: transfer_to_human` marker with a real
  hand-off: the exhaust turn speaks `on_exhaust`, emits the vendor payload and ends the
  leg (see `flows.handoff`). The marker tool only RECORDS the request — on a
  contact-center platform it is the payload that puts a person on the line.
  """
  _exhausts = [k for k, v in (("on_exhaust_fill", on_exhaust_fill),
                              ("on_exhaust_then", on_exhaust_then),
                              ("on_exhaust_handoff", on_exhaust_handoff))
               if v is not None]
  if len(_exhausts) > 1:
    raise ValueError(
        f"user_slot(): pass ONE of {_exhausts} — `fill` resolves the slot and"
        " continues, `then` ends the attempt via a control tool, and a hand-off ends"
        " the leg itself. They are competing dispositions for the same rung.")
  if skip_readback_if_matches and not readback:
    raise ValueError(
        "user_slot(): skip_readback_if_matches= only suppresses a readback this slot"
        " actually has — pass readback=True, or drop it. Without one it is inert, and"
        " inert in the worst way: the author believes the second confirmation is gone.")
  if on_exhaust_handoff is not None and validation is not None:
    raise ValueError(
        "user_slot(): validation= replaces the whole No-Match ladder, so"
        " on_exhaust_handoff would be silently dropped. Put the hand-off inside it:"
        ' validation={..., "on_exhaust": h.on_exhaust("...")}.')
  # The derived reprompts quote the question back, so they need its plain-string
  # form. A polymorphic ask still has one — the floor — and using it keeps the
  # reprompt ladder identical to what a plain-string ask would have produced.
  ask_text = floor_of(ask) or ""
  # The WHOLE ladder is kept. The engine plays `reprompts[min(retries-1, len-1)]`,
  # so rung N is heard on attempt N+1 and it clamps to the last rung — there is no
  # two-rung limit anywhere in it. Truncating to two here silently threw away an
  # author's third rung. The real bound is `max_retries` (on_exhaust fires on
  # attempt `max_retries`), so a list longer than that warns instead.
  ladder = list(reprompts or [])
  if not ladder:
    ladder.append(f"Sorry, I didn't catch that. {ask_text}")
  if len(ladder) < 2:
    ladder.append("One more time. " + ask_text)
  reachable = max(max_retries - 1, 0)
  if reprompts and validation is None and len(ladder) > reachable:
    warnings.warn(
        f"user_slot({name!r}): {len(ladder)} reprompts but max_retries="
        f"{max_retries} lets only the first {reachable} play before on_exhaust"
        " fires — raise max_retries or drop the extra rung(s).",
        UserWarning,
        stacklevel=2,
    )
  s: dict[str, Any] = {
      "name": name,
      "source": "user",
      "setter": setter or f"set_{name}",
      "hint": hint or name.replace("_", " "),
      # A LADDER is emitted whole; a plain ask emits its floored string exactly as
      # before. The engine walks the rungs on each re-ask (see `_ask_rung`).
      "ask": list(ask) if isinstance(ask, (list, tuple)) else ask_text,
      "validation": validation if validation is not None else {
          "max_retries": max_retries,
          "reprompts": ladder,
          "on_exhaust": (
              {"say": on_exhaust, "fill": on_exhaust_fill}
              if on_exhaust_fill is not None else
              as_handoff(on_exhaust_handoff, "user_slot()").on_exhaust(on_exhaust)
              if on_exhaust_handoff is not None else
              {"say": on_exhaust,
               "then": on_exhaust_then or {"tool": "transfer_to_human"}}
          ),
      },
  }
  # A polymorphic ask splits by semantics: alternative WORDINGS replace the ask
  # (`ask_variants`), structured content accompanies it (`response`, the field the
  # engine already resolves for a question). A plain-string ask emits neither and
  # so produces exactly the config it always did.
  ask_variants = variants_of(ask)
  if ask_variants:
    s["ask_variants"] = ask_variants
  ask_payloads = payloads_of(ask)
  if ask_payloads:
    s["response"] = ask_payloads
  if dtmf:
    s["dtmf_map"] = dtmf
  if requires:
    s["requires"] = requires
  cond = _condition(condition)
  if cond is not None:
    s["condition"] = cond
  if readback:
    s["requires_readback"] = True
  if skip_readback_if_matches:
    s["skip_readback_if_matches"] = list(skip_readback_if_matches)
  if validation_rules:
    s["validation_rules"] = validation_rules
  if sensitive:
    s["sensitive"] = True
  if repeated:
    s["repeated"] = repeated
  if verbatim:
    s["verbatim"] = True
  if filler_say is not None:
    s["filler_say"] = filler_say
  if not automatic_fillers:
    # Build-time marker, stripped before validation (see build._apply_automatic_fillers).
    s["automatic_fillers"] = False
  # No `default` here, deliberately: a defaulted question is a question never asked.
  # A user slot that should resolve without the caller has `validation.on_exhaust.fill`,
  # which fires only after they have actually been given the chance.
  return _value_policy(s, reject=reject, publish=publish, shared=shared)


def intent_slot(
    name: str,
    options: dict[str, list[str]],
    *,
    ask: Optional[str] = None,
    passive: Optional[bool] = None,
    setter: Optional[str] = None,
    dtmf: Optional[dict[str, str]] = None,
    requires: Optional[list[str]] = None,
    condition: Optional[ConditionSpec] = None,
    cue_priority: Optional[str] = None,
    multi_fill: bool = False,
    switchable: Union[bool, str] = False,
    max_retries: int = 2,
    reprompts: Optional[list[str]] = None,
    on_exhaust: Optional[str] = None,
    on_exhaust_fill: Optional[str] = None,
    cue_only: bool = False,
    push_back: Optional[dict[str, Any]] = None,
    verbatim: bool = False,
    description: Optional[str] = None,
) -> dict[str, Any]:
  """A first-class INTENT slot — a `kind:"intent"` enum whose value SELECTS one operation.

  `options` maps each enum VALUE -> its spoken cue phrases (the text twin of `dtmf_map`
  used for deterministic cue->value fill). The slot is valid-by-construction: it always
  carries `option_cues`, an enum `validation_rules` entry, and a `setter` (a
  model-classified intent slot NEEDS a setter to be captured — the engine drops a falsy
  setter, leaving the slot uncapturable).

  The enum `validation_rules` `detail` is a PIPE-JOINED STRING of the option values
  (e.g. `"pay|refund"`), and the rule discriminator key is `kind` (NOT `type`) — this is
  what the framework `_check_intent_slots` requires (`any(r.get("kind") == "enum")`) and
  what the setter generator splits on `"|"`.

  `switchable="defer"` PARKS the journey the caller stepped away from instead of
  discarding it, and restores it if they come back. "Hold on, what's my balance" then
  "okay, back to the activation" is ordinary customer behaviour, and re-asking for a
  number already given reads as the agent having lost the thread. Use `True` when a
  switch means the caller changed their mind and the old journey is genuinely finished.

  `switchable=True` lets a caller CHANGE the subject mid-flow: an already-filled value is
  re-decided when a later utterance matches exactly one option unambiguously, and
  everything derived from the old value is cleared. Off by default, because the cost of a
  false positive is discarding a journey the caller was halfway through — only turn it on
  for a slot whose cue sets are disjoint enough to survive incidental mentions.

  `passive=True` marks a model-classified intent (never asked): no `ask`/reprompt ladder
  and no enum-listing `not_in_enum` retry (the model would speak the internal categories
  verbatim). Otherwise the slot is ASKED with a humanized `ask`.

  `description` is the model-facing tool description of the generated setter -- what the
  model reads when deciding whether/how to call it. Without it the SDK stamps the useless
  default "Record the value for <setter>."; a real one (purpose + when-to-call) is what
  keeps a model-classified slot from being a wrong-tool/wrong-value guess. Emitted onto the
  setter's `pythonFunction.description`; omitted -> the default, unchanged.
  """
  if not options:
    raise ValueError("intent_slot(): options must be non-empty")
  for val, cues in options.items():
    if not cues:
      raise ValueError(
          f"intent_slot(): option {val!r} maps to an empty cue list"
      )
  s: dict[str, Any] = {
      "name": name,
      "source": "user",
      "kind": "intent",
      "option_cues": options,
      # cue_only → no model setter: only the deterministic option_cues fill it, so the
      # model can never classify an off-cue answer onto a value (it rides push_back).
      "setter": ("" if cue_only else (setter or f"set_{name}")),
      "hint": name.replace("_", " "),  # gate mode lists the hint, not the raw slot name
      "validation_rules": [{"kind": "enum", "detail": "|".join(options.keys())}],
  }
  if passive:
    s["passive"] = True
  else:
    s["ask"] = (list(ask) if isinstance(ask, (list, tuple))
                else (ask or "Which would you like?"))
  if dtmf:
    s["dtmf_map"] = dtmf
  if cue_priority:
    s["cue_priority"] = cue_priority
  if multi_fill:
    # Opt in to filling from an utterance that ALSO fills another intent slot. Off by
    # default: one utterance usually expresses one intent, and letting overlapping
    # vocabulary set several slots at once makes silent decisions the author did not ask
    # for. Cue sets must be disjoint from the other slot's, or one phrase means two
    # things and both are recorded.
    s["multi_fill"] = True
  if switchable:
    if switchable not in (True, "defer"):
      raise ValueError(
          f"intent_slot(): switchable must be True or 'defer', got {switchable!r}")
    s["switchable"] = switchable
  # An ASKED intent slot with no ladder re-asks forever when the caller answers something
  # the cues cannot resolve — there is no setter error, so the retry counter never moves.
  # `on_exhaust_fill` gives it somewhere to land.
  if not passive and on_exhaust_fill is not None:
    if on_exhaust_fill not in options:
      raise ValueError(
          f"intent_slot(): on_exhaust_fill {on_exhaust_fill!r} is not one of"
          f" {sorted(options)}")
    s["validation"] = {
        "max_retries": max_retries,
        "on_exhaust": {"say": on_exhaust or "", "fill": on_exhaust_fill},
    }
    if reprompts:
      s["validation"]["reprompts"] = list(reprompts)
  if verbatim:
    s["verbatim"] = True
  if push_back is not None:
    # The re-offer ladder for a caller who keeps declining/pushing back (see push_back()).
    # Pairs naturally with cue_only: a non-accept fills nothing and rides this ladder.
    s["push_back"] = push_back
  if cue_only and setter:
    raise ValueError(
        "intent_slot(): cue_only=True means no model setter — do not also pass setter=."
        " A non-accept then fills nothing and rides push_back / on_exhaust_fill.")
  if requires:
    s["requires"] = requires
  if description:
    s["tool_description"] = description
  cond = _condition(condition)
  if cond is not None:
    s["condition"] = cond
  return s


def passive_slot(
    name: str,
    *,
    setter: Optional[str] = None,
    option_cues: Optional[dict[str, list[str]]] = None,
    kind: Optional[str] = None,
    requires: Optional[list[str]] = None,
    condition: Optional[ConditionSpec] = None,
    cue_priority: Optional[str] = None,
    multi_fill: bool = False,
    description: Optional[str] = None,
) -> dict[str, Any]:
  """A never-asked, model/cue-filled but still-CAPTURABLE user slot.

  `passive` slots are skipped by the engine's `_find_next_question` (the model or
  `option_cues` fill them silently), yet they still need a `setter` to record what was
  captured — so the setter defaults to `set_<name>`: the engine DROPS a falsy passive
  setter, which would leave the slot permanently uncapturable.

  This is deliberately NOT `user_slot(..., )`: `user_slot`'s `ask`/reprompt/`on_exhaust`
  ladder is the caller-didn't-answer contract, which is dead weight (and a jargon-leak
  risk) for a slot that is never asked. `passive_slot` emits none of it.

  `kind="intent"` + `option_cues` produces a valid model-classified intent slot (adds the
  enum `validation_rules` like `intent_slot`). `option_cues` alone (no `kind`) is a plain
  cue-filled passive slot.

  `description` sets the generated setter's model-facing tool description (see
  `intent_slot`); omitted -> the SDK default. Only meaningful for a model-filled passive
  slot, since a cue-only one (`setter=""`) is never offered to the model.
  """
  s: dict[str, Any] = {
      "name": name,
      "source": "user",
      "passive": True,
      "setter": setter or f"set_{name}",
  }
  if option_cues:
    s["option_cues"] = option_cues
  if cue_priority:
    s["cue_priority"] = cue_priority
  if multi_fill:
    # Opt in to filling from an utterance that ALSO fills another intent slot. Off by
    # default: one utterance usually expresses one intent, and letting overlapping
    # vocabulary set several slots at once makes silent decisions the author did not ask
    # for. Cue sets must be disjoint from the other slot's, or one phrase means two
    # things and both are recorded.
    s["multi_fill"] = True
  if kind:
    s["kind"] = kind
  if kind == "intent" and option_cues:
    s["validation_rules"] = [
        {"kind": "enum", "detail": "|".join(option_cues.keys())}
    ]
  if requires:
    s["requires"] = requires
  if description:
    s["tool_description"] = description
  cond = _condition(condition)
  if cond is not None:
    s["condition"] = cond
  return s


def setter_group(op: str, slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
  """Re-point a list of user slots at ONE shared multi-field validating setter.

  Returns COPIES of `slots` (inputs are not mutated) with each slot's `setter` set to
  `set_<op>_inputs` and its `setter_field` set to its own `name` (derived from the name
  so field and name can never desync). This opts an author into CES's one-setter-per-flow
  shape — the engine routes each field's captured value back through the field name. The
  setter BODY (which validates all fields together) is generated in `setters.py`.
  """
  out: list[dict[str, Any]] = []
  for slot in slots:
    s = dict(slot)
    s["setter"] = f"set_{op}_inputs"
    s["setter_field"] = s["name"]
    out.append(s)
  return out


def announce(
    name: str,
    texts: list[str],
    *,
    message: Optional[str] = None,
    requires: Optional[list[str]] = None,
    condition: Optional[ConditionSpec] = None,
    end: bool = False,
    escalated: bool = False,
    reason: str = "completed",
    barge_in: bool = True,
    shared: bool = False,
    sets: Optional[dict[str, Any]] = None,
    preempt: bool = False,
    transfer_to: Optional[str] = None,
    end_conversation: bool = False,
    handoff: Optional["HandoffLike"] = None,
    repair: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
  """A spoken announce slot. `texts` -> response text parts; `{slot}` interpolates.

  Two delivery channels, and they are NOT interchangeable:

  * `texts` -> `response` parts, delivered VERBATIM (the caller hears exactly this).
  * `message` -> the engine's turn message, which the MODEL renders in its own
    words. Use it for content the agent should deliver conversationally — a canned
    offer folded in after an operation completes — rather than read out.

  Pass `texts`, `message`, or both; neither raises (an announce that says nothing
  just silently fills its own slot). `end=True` appends an `end_session` part
  terminating the conversation; `transfer_to` appends the structured `transfer`
  part that moves control to another AGENT in the same app (it routes on `agent`,
  not on the spoken text, so a hand-off cannot be expressed as prose).

  `handoff` is the other kind of hand-off: out of the app entirely, to a live agent
  on the contact-center platform. It appends the vendor payload AND the `end_session`
  that has to accompany it (see `flows.handoff`). Mutually exclusive with `end` and
  `transfer_to`, which would each add a second, competing disposition.

  `preempt` is ALWAYS emitted, never left to the config default. The engine reads
  it as `slot_def.get("preempt", True)`, so omitting the key from a `preempt=False`
  announce silently inverts it into a preempting one that cuts off the turn in
  progress — the opposite of what the author asked for.

  `sets` writes extra slots when the announce fires, and it is what lets a LADDER of
  mutually exclusive announces exist. An announce normally latches only its own name,
  so a ladder of them all fire on the same turn and the caller hears every one at
  once — the cascade leaves as a single preempt. Give each rung
  `sets={"verdict_delivered": "true"}` and the first to fire closes the gate the rest
  are conditioned on: the cascade recomputes after every announce, so exactly one
  speaks.

  Reach for it when a rung's whole effect is speech plus a latch. Written as a TASK
  that shape costs a tool dispatch and then a second engine invocation to process the
  result — on a real agent, ~230ms to write one constant — while an announce does it
  in-process with no round trip at all.

  The values land in `filled` alongside the announce's own name, so conditions,
  `requires` and later rungs read them exactly as they read any other slot.

  They must be TRUTHY, and the build rejects them otherwise. A condition reads a slot
  as filled by its truthiness, so a latch written as `""` or `False` still reads as
  unfilled: the gate stays open, every lower rung speaks anyway, and nothing about the
  config looks wrong. For the same reason a latch already holding a falsy value is
  treated as unset and written over, while one holding a real value is left alone.
  """
  if not texts and message is None and handoff is None:
    raise ValueError(
        "announce(): give texts (verbatim response parts) and/or message"
        " (model-rendered) — an announce with neither says nothing")
  if handoff is not None:
    if end:
      raise ValueError(
          "announce(): pass handoff= or end=True, not both — a hand-off already"
          " emits the end_session that ends the leg, and a second one would"
          " duplicate it. Set the reason/escalated on flows.handoff(...) instead.")
    if transfer_to is not None:
      raise ValueError(
          "announce(): pass handoff= or transfer_to=, not both. transfer_to hands"
          " control to another agent in this app; a hand-off ends the leg and gives"
          " the caller to the contact-center platform. Only one of them can happen.")
    if escalated or reason != "completed":
      raise ValueError(
          "announce(): reason=/escalated= describe the end=True end_session and are"
          " not read when handoff= is given. Set them on the hand-off itself:"
          " flows.handoff(payload, reason=..., escalated=...).")
  parts: list[dict[str, Any]] = []
  for t in texts:
    p: dict[str, Any] = {"type": "text", "text": t}
    if not barge_in:
      p["interruptable"] = False
    parts.append(p)
  if transfer_to is not None:
    parts.append({"type": "transfer", "agent": transfer_to})
  if end:
    ep: dict[str, Any] = {"type": "end_session", "reason": reason}
    if escalated:
      ep["escalated"] = True
    parts.append(ep)
  if handoff is not None:
    parts.extend(as_handoff(handoff, "announce()").parts())
  a: dict[str, Any] = {"name": name, "source": "announce"}
  if message is not None:
    a["message"] = message
  if parts:
    a["response"] = parts
  if requires:
    a["requires"] = requires
  cond = _condition(condition)
  if cond is not None:
    a["condition"] = cond
  if shared:
    a["shared"] = True
  if sets:
    a["sets"] = dict(sets)
  a["preempt"] = preempt
  if end_conversation:
    a["end_conversation"] = True
  if repair is not None:
    a["repair"] = repair
  return a


def content_announce(
    name: str,
    message: str,
    *,
    after: str,
    condition: Optional[ConditionSpec] = None,
) -> dict[str, Any]:
  """A canned-content pitch delivered AFTER an operation completes.

  `after` is the operation's result slot: the announce only becomes eligible once
  that slot is filled, and it never preempts — so the pitch lands behind the
  operation's confirmation instead of cutting across it. The content rides as a
  model-rendered `message` rather than verbatim `response` parts, because an offer
  read out word for word sounds like an ad break spliced into the call.

  `after` must therefore name a slot that fills on a turn which does NOT otherwise
  preempt — so NOT the result slot of a task with a `then_say`, and not one gated
  behind a preempting announce. A message is only handed to the model on a
  non-preempting turn; when the turn preempts for any other reason the engine folds
  the message into the canned directive text, and the caller hears the pitch's
  INSTRUCTIONS read out ("Offer to set up autopay ... in your own words"). Put a
  turn boundary between the confirmation and the offer — `after` a slot the caller's
  next answer fills — and the pitch renders as prose. See examples/content_offer.py.
  """
  return announce(name, [], message=message, requires=[after],
                  condition=condition, preempt=False)


def result_slot(
    name: str,
    task_name: str,
    *,
    default: Any = None,
    reject: Optional[Sequence[str]] = None,
    publish: Optional[Union[str, Sequence[str]]] = None,
    shared: bool = False,
) -> dict[str, Any]:
  """A slot filled by a task's result (`source: task:<task_name>`).

  `default` is what the slot holds when the task ran but its response did not carry
  this key — intake skips an absent key, so without a default the slot stays empty
  and every downstream branch reading it silently never fires. See `event_slot` for
  `reject` / `publish` / `shared`.
  """
  slot = {"name": name, "source": f"task:{task_name}"}
  return _value_policy(slot, default=default, reject=reject, publish=publish,
                       shared=shared)


# ---------------------------------------------------------------------------
# Repeated collection loops — collect N items into one list slot (Mode A) or
# repeat a child component per element (Mode B). Previously only expressible as
# a raw `repeated` dict; this builder makes the termination affordance explicit
# (the framework REQUIRES one of until_max/done_setter, floored by min_count).
# ---------------------------------------------------------------------------


def repeated(
    *,
    until_max: Optional[int] = None,
    done_setter: Optional[str] = None,
    min_count: Optional[int] = None,
    ask_more: Optional[str] = None,
    over: Optional[str] = None,
    each: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
  """Build a `repeated` block for a `user_slot` (Mode A) or `component` (Mode B).

  Termination REQUIRES `until_max` (a hard cap) and/or `done_setter` (a caller-said-
  "that's all" signal), optionally floored by `min_count`. Mode B binds each element:
  `over` is the parent list slot, `each` maps `{child_slot: element_field}`. Raises if no
  termination affordance is given (the framework rejects an unbounded loop).
  """
  if until_max is None and done_setter is None:
    raise ValueError(
        "repeated(): give until_max and/or done_setter — an unbounded loop is rejected"
    )
  until: dict[str, Any] = {}
  if until_max is not None:
    until["max_count"] = until_max
  if done_setter is not None:
    until["done_setter"] = done_setter
  block: dict[str, Any] = {"until": until}
  if min_count is not None:
    block["min_count"] = min_count
  if ask_more is not None:
    block["ask_more"] = ask_more
  if over is not None:
    block["over"] = over
  if each is not None:
    block["each"] = each
  return block


# ---------------------------------------------------------------------------
# readback_fmt — how a value is spoken back at confirmation. Previously a raw
# dict; these builders inherit the framework's required-field validation.
# ---------------------------------------------------------------------------


def readback(fmt_type: str, **fields: Any) -> dict[str, Any]:
  """Build a `readback_fmt` dict (attach to a slot's `readback_fmt`).

  `fmt_type` ∈ {"date","time","digits","prefix","plural","none_sub","join","count"}
  and `fields` carry that type's required/optional fields (matching the framework
  validator + formatter, which key on `"type"`):
    * prefix   → `text`         e.g. `readback("prefix", text="ending in")`;
                 optional `values` ({stored: spoken}) speaks an enum key as a
                 sentence instead of leaking "remove_lift" to TTS
    * plural   → `one`, `other` e.g. `readback("plural", one="item", other="items")`
    * count    → `one`, `other` (len()-based plural of a list slot)
    * none_sub → `default`      e.g. `readback("none_sub", default="none on file")`
    * join     → `each` (element template), `sep` optional (defaults ", ")
    * digits   → optional `text` label; speaks the value one digit at a time, which
                 is what a `readback_verbatim` phone/ZIP/SSN needs (an unspaced
                 "2124561234" is read by TTS as one enormous number)
    * date / time → no required fields; `date` takes an optional `text` lead-in
                 (replacing the default "on") and parses MMDDYYYY as well as ISO

  Raises `ValueError` on an unknown `fmt_type` or a missing required field, so a bad
  format is caught at author time rather than by the framework validator.
  """
  required = {
      "prefix": ["text"], "plural": ["one", "other"], "none_sub": ["default"],
      "join": ["each"], "count": ["one", "other"], "date": [], "time": [],
      "digits": [],
  }
  if fmt_type not in required:
    raise ValueError(
        f"readback(): unknown fmt_type {fmt_type!r}; valid: {sorted(required)}")
  missing = [f for f in required[fmt_type] if f not in fields]
  if missing:
    raise ValueError(f"readback({fmt_type!r}): missing required field(s) {missing}")
  return {"type": fmt_type, **fields}


# ---------------------------------------------------------------------------
# Control blocks — cancel / escalate dispositions + a plain no_input ladder.
# Previously only settable as raw dicts via Flow.set(); these builders default
# the outcome correctly (the engine's default is the block NAME, which breaks a
# `== "cancelled"` resume gate) and keep transfer_to optional.
# ---------------------------------------------------------------------------


def cancel(
    *,
    say: str,
    transfer_to: Optional[str] = None,
    outcome: str = "cancelled",
    requires_readback: bool = False,
    end_conversation: Optional[bool] = None,
    clear_slots: Optional[list[str]] = None,
    confirm_say: Optional[str] = None,
    verbatim: bool = False,
) -> dict[str, Any]:
  """A cancel disposition (flow policy `cancel`): stop, optionally return to a parent.

  `end_conversation=False` turns cancel from a teardown into a step BACK. Instead of
  ending the session, the slots named in `clear_slots` — normally the intent slot that
  selected the journey — are un-decided along with everything derived from them, `say` is
  spoken, and the flow asks its next open question. For a single-agent app that is the
  difference between "never mind" returning the caller to the menu and "never mind"
  hanging up on them.

  `clear_slots` is only read when `end_conversation` is False. Leaving it empty means the
  caller is acknowledged and then asked the same question again, which is rarely what
  anyone wants.
  """
  block: dict[str, Any] = {
      "requires_readback": requires_readback,
      "say": say,
      "outcome": outcome,
  }
  if transfer_to is not None:
    block["transfer_to"] = transfer_to
  if end_conversation is False:
    block["end_conversation"] = False
    block["clear_slots"] = list(clear_slots or [])
  if confirm_say is not None:
    block["confirm_say"] = confirm_say
  if verbatim:
    block["verbatim"] = True
  return block


def _check_declined_say(fn, declined_say):
  """Reject a refusal line that can never be heard, at author time.

  Every rule here is dead config that reads as though it does something, which is the
  failure mode worth raising on: an empty ladder that says nothing, a reason with no
  words behind it, and a reason placed after a catch-all — which always matches, so
  everything below it is unreachable and the author's most specific wording is the one
  that never speaks.
  """
  if declined_say is None:
    return
  if isinstance(declined_say, str):
    return
  if not isinstance(declined_say, list):
    raise ValueError(
        f"{fn}(): declined_say must be a line, a ladder of lines, or a list of"
        f" {{'when': ..., 'say': ...}} reasons — got {type(declined_say).__name__}")
  if not declined_say:
    raise ValueError(
        f"{fn}(): declined_say=[] says nothing on a refusal — omit it entirely to"
        " make the block silent, or give at least one line")
  if not any(isinstance(x, dict) for x in declined_say):
    # A plain LADDER: one reason, a line per refusal. Every line is unconditional, so
    # the reachability rule below would reject the second one — that rule applies only
    # to a list that actually carries a reason.
    if not all(isinstance(x, str) and x for x in declined_say):
      raise ValueError(f"{fn}(): declined_say ladder must hold non-empty lines")
    return
  catch_all = None
  for i, entry in enumerate(declined_say):
    if isinstance(entry, str):
      unconditional, say_ = True, entry
    elif isinstance(entry, dict):
      unconditional, say_ = entry.get("when") is None, entry.get("say")
      if not say_:
        raise ValueError(
            f"{fn}(): declined_say[{i}] is a reason with no `say`, so matching it"
            " would refuse the caller in silence")
      if isinstance(say_, list) and not all(
          isinstance(x, str) and x for x in say_):
        raise ValueError(
            f"{fn}(): declined_say[{i}]['say'] must be a line or a ladder of"
            " non-empty lines")
      elif not isinstance(say_, (str, list)):
        raise ValueError(
            f"{fn}(): declined_say[{i}]['say'] must be a line or a ladder of lines")
    else:
      raise ValueError(
          f"{fn}(): declined_say[{i}] must be a line or a"
          " {'when': ..., 'say': ...} reason")
    if catch_all is not None:
      raise ValueError(
          f"{fn}(): declined_say[{i}] can never be reached — declined_say"
          f"[{catch_all}] has no `when`, so it matches every refusal. Put the"
          " catch-all last.")
    if unconditional:
      catch_all = i


def escalate(
    *,
    say: Optional[str] = None,
    transfer_to: Optional[str] = None,
    outcome: str = "escalated",
    reason: Optional[str] = None,
    requires_readback: bool = False,
    tasks: Optional[list[str]] = None,
    component: Optional[str] = None,
    inputs: Optional[dict[str, str]] = None,
    outputs: Optional[dict[str, str]] = None,
    on_abort: str = "skip",
    condition: Optional[dict[str, Any]] = None,
    declined_say: Optional[Union[str, list[Union[str, dict[str, Any]]]]] = None,
    handoff: Optional["HandoffLike"] = None,
    confirm_say: Optional[str] = None,
    verbatim: bool = False,
) -> dict[str, Any]:
  """An escalate disposition (flow policy `escalate`): hand off to a human/team.

  `tasks` names an ordered pre-terminal chain run BEFORE the disposition — the way
  to build the summary the receiving human reads. While it is in flight the DAG walk
  sees only those tasks; when it is exhausted the disposition above runs unchanged.
  Gate each member on `escalated()` so it stays inert in the ordinary spine walk.

  `component` routes the detected human request into an INTERACTIVE, in-DAG,
  returnable deflection sub-flow (a child config) instead of the fixed
  chain-then-terminate disposition. Use it when reaching a human is not a one-shot
  hand-off but a mini-conversation — e.g. deflect to self-service, offer an SMS
  assistant, branch on the caller's reason, then either resolve/hand off/hang up. The
  child is entered every time a human request is detected (re-entrant across repeated
  asks); a child terminal task with `end_conversation` ends/transfers the call, while
  an ordinary child completion RETURNS to this flow (the caller was deflected).
  `inputs`/`outputs` seed/read the child scope like a `component(...)` task; `on_abort`
  ("skip"|"fail_flow") governs a child that aborts. Mutually exclusive with `tasks`,
  `transfer_to`, and `handoff` — the child owns the disposition. When `component` is
  set, do NOT pass `say`: the child speaks its own prelude (its first announce/task),
  and a `say` here would be silently dropped, so it is rejected. Set `escalatable=False`
  on the CHILD flow so a repeat ask inside the deflection is not itself re-preempted.

  `condition` gates whether the hand-off is ALLOWED AT ALL, and is checked before any
  of that: some dispositions are only valid in some states — a repair flow that has
  just found a fault no hand-off can fix must not offer a live agent, since nothing on
  the other end brings the service back. When the condition is false the request is
  dropped (the caller may ask again later, when it may well have changed), the `tasks`
  chain never arms, and `declined_say` explains why; without one the block simply
  never fires.

  `declined_say` takes three shapes, because a flow may refuse for more than one
  reason and the caller has to hear the right one:

    "a line"                      one reason, one answer.
    ["first", "second"]           one reason, a LADDER indexed by how many times the
                                  request has been refused, clamped to the last line
                                  (the same sentence twice reads as not listening).
    [{"when": <cond>, "say": …},  REASONS, evaluated in order against the filled
     …, {"say": …}]               state; the first match supplies the line and its
                                  `say` may itself be a ladder. An entry with no
                                  `when` — or a bare string — always matches, so it
                                  is the catch-all and must come last.

  Without the third shape a two-reason refusal has to be worded so vaguely that it
  covers both, on the one turn where the caller has asked a direct question.

  `handoff` is what makes this rail actually reach a person on a contact-center
  platform. Without it the disposition speaks `say` and emits a bare `end_session` —
  the caller is told someone is coming and is then disconnected, because nothing told
  the platform to route them. Pass `flows.handoff(flows.ujet(...))` (or another
  vendor's payload) and the vendor payload rides the disposition turn ahead of the
  end. Not for a MULTI-agent `transfer_to`, which hands to a sibling agent inside the
  app rather than leaving it.
  """
  _check_declined_say("escalate", declined_say)
  if handoff is not None and transfer_to is not None:
    raise ValueError(
        "escalate(): pass handoff= or transfer_to=, not both. transfer_to returns"
        " control to a parent agent inside this app (no session end); a hand-off ends"
        " the leg and gives the caller to the contact-center platform.")
  if component is not None:
    if any(x is not None for x in (tasks, transfer_to, handoff)):
      raise ValueError(
          "escalate(): component= owns the whole disposition, so it cannot be combined"
          " with tasks=/transfer_to=/handoff= (a chain-then-terminate and a route-into-"
          "a-sub-flow are mutually exclusive). Model those inside the child DAG instead.")
    if say is not None:
      raise ValueError(
          "escalate(): say= is dropped at runtime when component= is set — the child"
          " speaks its own prelude (its first announce/task). Omit say=, or move that"
          " line into the child DAG's opening announce.")
  elif say is None:
    raise ValueError(
        "escalate(): say= is required (unless component= is set, where the child speaks).")
  block: dict[str, Any] = {
      "requires_readback": requires_readback,
      "outcome": outcome,
  }
  if say is not None:
    block["say"] = say
  # `outcome` is the flow disposition (lands in exit_status.flow_outcome); `reason` is the
  # value on the terminal end_session PART a downstream contract reads. They differ:
  # `escalate(outcome="escalate")` still emits `reason="transfer"` unless `reason` is set.
  # Omitted -> the engine keeps its "transfer" default, so existing configs are unchanged.
  if reason is not None:
    block["reason"] = reason
  if transfer_to is not None:
    block["transfer_to"] = transfer_to
  if tasks is not None:
    block["tasks"] = list(tasks)
  if component is not None:
    block["component"] = component
    block["inputs"] = dict(inputs or {})
    block["outputs"] = dict(outputs or {})
    block["on_abort"] = on_abort
  if condition is not None:
    block["condition"] = condition
  if declined_say is not None:
    block["declined_say"] = declined_say
  if handoff is not None:
    block["response"] = as_handoff(handoff, "escalate()").parts()
  if confirm_say is not None:
    block["confirm_say"] = confirm_say
  if verbatim:
    block["verbatim"] = True
  return block


def continue_cues(
    *,
    phrases: Optional[list[str]] = None,
    extra: Optional[list[str]] = None,
    ack: str = "",
    enabled: bool = True,
) -> dict[str, Any]:
  """Following-along cues (flow policy `continue_cues`) — "mhmm", "got it", "go on".

  A caller making these noises is agreeing, not answering and not taking the floor. Left
  unrecognized the turn fills no slot, so the flow counts it as a stall and enough of them
  escalate the call — and if the caller made the noise OVER a line, the platform cut that
  line short and the rest is never spoken. Recognized, the turn is absorbed: no stall, no
  re-ask, and any unheard remainder is resumed.

  On by default with `DEFAULT_CONTINUER_PHRASES`; set a policy only to change it.

    `phrases`  replaces the default vocabulary outright.
    `extra`    adds to it — the usual choice, since only the author knows whether "fine"
               means agreement in their domain.
    `ack`      a short line to say before resuming ("Sure —"). Empty resumes silently.
    `enabled`  False turns the behavior off for this flow.

  Safety is at the engine, not here: a pending slot wins, so a caller answering "okay" to
  "shall I book it?" is still answering. Matching is whole-utterance, so "mhm but what
  about the fee" is a question, not agreement.
  """
  policy: dict[str, Any] = {"enabled": bool(enabled)}
  if phrases is not None:
    policy["phrases"] = [p for p in (phrases or []) if str(p).strip()]
  if extra:
    policy["extra"] = [p for p in extra if str(p).strip()]
  if ack:
    policy["ack"] = ack
  return policy


_REPAIR_MODES = ("parts", "remainder", "full")


def repair(
    *,
    mode: str = "parts",
    lead_in: str = "",
    max_repairs: int = 2,
    min_unheard_chars: int = 15,
) -> dict[str, Any]:
  """How an announce recovers when the caller talked over it (`announce(repair=...)`).

  The cascade speaks every announce it can reach as ONE response, so a caller who barges
  during the first never hears the rest — and all of them are recorded as delivered
  regardless. This replays what was missed.

    `mode="parts"`      re-speak every part the caller never reached, verbatim, plus the
                        whole part that was cut mid-way. Exact: part boundaries are known,
                        so nothing is reconstructed. The default.
    `mode="remainder"`  as `parts`, but the cut part resumes mid-sentence instead of
                        restarting. Falls back to `parts` when the boundary cannot be
                        located confidently.
    `mode="full"`       re-speak the whole announce from the top.

  `lead_in` prefixes the replay ("As I was saying —"). `max_repairs` caps how many times
  one announce will be re-attempted, so a caller who interrupts every attempt is not
  looped. `min_unheard_chars` skips a repair when only a tail fragment was lost.

  Replay reads a recording of what was spoken; it never re-runs the DAG and never clears
  the announce's latch, so no condition is re-evaluated and no downstream gate re-fires.
  """
  if mode not in _REPAIR_MODES:
    raise ValueError(
        f"repair(mode={mode!r}) — valid modes: {', '.join(_REPAIR_MODES)}")
  block: dict[str, Any] = {"mode": mode, "max_repairs": int(max_repairs),
                           "min_unheard_chars": int(min_unheard_chars)}
  if lead_in:
    block["lead_in"] = lead_in
  return block


def on_interrupted(
    *,
    say: str = "",
    say_unknown: str = "",
    min_unheard_chars: int = 15,
    repair_announces: Optional[dict[str, Any]] = None,
    resume_on_continuer: bool = True,
    then: str = "",
    open_slot: str = "",
    component: str = "",
    end_conversation: bool = False,
) -> dict[str, Any]:
  """What this flow does when the caller talks over the agent (flow policy).

  Fires on a turn the platform reports as an interruption. `say` may reference `{heard}`
  and `{unheard}`; `{unheard}` is what the caller missed.

  **`say_unknown` is not an edge case.** The heard prefix arrives through the platform's
  own transcription of what it played, so it does not always line up with the string we
  sent. When it does not, `{unheard}` is withheld rather than guessed and `say_unknown` is
  spoken instead. A policy that references `{unheard}` without a `say_unknown` simply says
  nothing on those turns.

  `then` / `open_slot` / `component` / `end_conversation` are the same action arms as
  `no_input(on_exhaust=...)`. Use `open_slot` to gate later logic on the interruption —
  conditions read filled slots, so a real slot is how an author sees this in the DAG.

  `repair_announces` applies a `flows.repair(...)` to every announce in the flow without
  tagging each one; a `repair=` on the announce itself wins. `resume_on_continuer` decides
  whether a following-along cue resumes the unheard remainder or merely absorbs the turn.
  """
  policy: dict[str, Any] = {
      "min_unheard_chars": int(min_unheard_chars),
      "resume_on_continuer": bool(resume_on_continuer),
  }
  if say:
    policy["say"] = say
  if say_unknown:
    policy["say_unknown"] = say_unknown
  if repair_announces is not None:
    policy["repair_announces"] = repair_announces
  if then:
    policy["then"] = then
  if open_slot:
    policy["open_slot"] = open_slot
  if component:
    policy["component"] = component
  if end_conversation:
    policy["end_conversation"] = True
  return policy


def no_input(
    *,
    reprompts: list[str],
    on_exhaust: dict[str, Any],
    hold_phrases: Optional[list[str]] = None,
    hold_reprompts: Optional[list[str]] = None,
    hold_ack: Optional[str] = None,
    hold_vetoes: Optional[list[str]] = None,
    verbatim: bool = False,
) -> dict[str, Any]:
  """A flow-level plain-silence ladder (flow policy `no_input`).

  `reprompts` are spoken one-per-silent-turn; `on_exhaust` is the terminal action
  (`{"say": ..., "then": {"tool": "transfer_to_human"}}` / `{"open_slot": ...}` / `{"component": ...}` /
  `{"end_conversation": True}`). For the full caller-asked-to-hold pattern — with an
  offer slot or child component on exhaust — use `hold_and_wait(...)`.

  The hold arguments handle a caller who ASKS for time rather than falling silent.
  `hold_phrases` is what the engine matches; `hold_reprompts` replaces the silence
  ladder once holding (an empty entry is a silent tick); `hold_ack` is spoken in
  place of the pending question on the turn the request is heard, so the caller who
  said "hold on, let me find it" is not immediately asked the same thing again.
  Passing `hold_ack` without `hold_phrases` defaults the phrases, since an ack that
  nothing can trigger is dead config. `hold_vetoes` names what disqualifies an utterance
  that carries a marker ("hold on, why do you need that?"); leave it unset for the
  engine's own defaults, or pass `[]` to match on markers alone.
  """
  policy: dict[str, Any] = {
      "reprompts": list(reprompts), "on_exhaust": on_exhaust}
  if hold_phrases is not None:
    policy["hold_phrases"] = list(hold_phrases)
  elif hold_ack or hold_reprompts is not None:
    policy["hold_phrases"] = list(DEFAULT_HOLD_PHRASES)
  if hold_reprompts is not None:
    policy["hold_reprompts"] = list(hold_reprompts)
  if hold_ack:
    policy["hold_ack"] = hold_ack
  if hold_vetoes is not None:
    policy["hold_vetoes"] = list(hold_vetoes)
  if verbatim:
    policy["verbatim"] = True
  return policy


def push_back(
    *,
    reprompts: list[str],
    max: int = 1,
    say: str = "",
    then: Optional[dict[str, Any]] = None,
    fill: Optional[str] = None,
    end_conversation: bool = False,
    verbatim: bool = False,
) -> dict[str, Any]:
  """A slot re-offer ladder for a caller who keeps DECLINING / pushing back.

  When the caller answers the awaited slot with something that does not fill it — declines
  an offer, insists, or says something off-cue — `push_back` RE-OFFERS `reprompts[k]` for
  the first `max` pushes (spoken as a PREEMPT, so the model cannot improvise the turn), then
  on the next push DISPOSES via on_exhaust: `fill` resolves the slot with an authored value
  and lets the flow continue (the dispose task/announce keyed on it fires this turn), and/or
  `then` fires a control tool (`{"tool": ..., "args": {...}}`), optionally ending the leg
  with `end_conversation=True`. `say` is the line spoken on the disposition turn.

  This is the counterpart — for an OFFER the caller keeps declining — of the other bounded
  ladders: `no_input` (the caller went SILENT), a slot's `validation` (the caller gave a BAD
  value), and `steer_back` (the caller STALLED). Each owns its own kind of turn. It is in
  particular what a CUE-ONLY intent slot needs (one with `option_cues` and no model setter):
  a non-accept matches no cue and, without this, the model answers the turn itself and
  drifts off-script — `push_back` re-offers cleanly instead. Attach with
  `intent_slot(..., push_back=flows.push_back(...))`.
  """
  if not reprompts and fill is None and then is None and not end_conversation:
    raise ValueError(
        "push_back(): give at least `reprompts` (to re-offer) or a disposition"
        " (`fill`, `then`, and/or `end_conversation`) — an empty ladder does"
        " nothing.")
  block: dict[str, Any] = {"reprompts": list(reprompts), "max": max}
  if say:
    block["say"] = say
  if then is not None:
    block["then"] = then
  if fill is not None:
    block["fill"] = fill
  if end_conversation:
    block["end_conversation"] = True
  if verbatim:
    block["verbatim"] = True
  return block


def answer(
    name: str,
    *,
    scope: str,
    instruction: str,
    grounds: Optional[list[str]] = None,
    tools: Optional[list[str]] = None,
    condition: Optional[ConditionSpec] = None,
    requires: Optional[list[str]] = None,
    max_turns: int = 8,
    allow_math: bool = True,
) -> dict[str, Any]:
  """A grounded, intent-scoped free-response fallback (an entry in the flow `answer` policy).

  When the caller, while `condition` holds, asks something no cue or open slot matches — the
  turn that would otherwise STEER BACK — the engine hands the model a grounded directive built
  from `scope` + `instruction` + the data in `grounds` (session vars/slots already in state)
  + the caller's question, and lets it COMPOSE the reply. The model may call ONLY the tools in
  `tools` (a read/compute WHITELIST); every other tool is hidden that turn, so no commitment
  is possible — structured actions (waiver, transfer, due-date change, …) stay deterministic
  DAG cues that match FIRST. The turn is NON-ADVANCING (fills no slot) and does not accrue
  steer-back strikes; after `max_turns` caller questions it yields to the steer-back ladder.

  This is the free-form counterpart of the bounded ladders (`no_input` = SILENCE, a slot's
  `validation` = a BAD value, `steer_back` = a STALL, `push_back` = a DECLINE): those recover a
  broken turn, `answer` fields an engaged, on-topic QUESTION. It is what lets a slot-filling
  DAG hold a short reasoning conversation (arithmetic, projection, comparison, plain-language
  explanation) grounded on the account data already looked up, without handing the model a
  free turn on which it could improvise a commitment. Attach a LIST as the flow `answer`
  policy: `flow.set("answer", [flows.answer(...)])`.

  Args:
    name: names this answer node (telemetry + its per-node caller-turn counter).
    scope: one phrase bounding what it may discuss ("billing questions about this account").
    instruction: the grounded system-instruction template — read-only, spoken, 1-3 sentences,
      "use the compute tool for arithmetic; if the caller asks for a change, acknowledge and
      stop — the system handles it".
    grounds: session vars/slots holding the data to answer FROM (already in state; no
      round-trip).
    tools: WHITELIST of tool names the model may call this turn (read/compute only). Every
      other tool is hidden. Keep COMMIT tools off this list — they remain DAG cues.
    condition: the intent gate; the node is eligible only while this holds.
    requires: slots that must be filled before the node is eligible (e.g. the lookup result).
    max_turns: bounded caller-question budget before yielding to steer-back.
    allow_math: let the model do low-stakes arithmetic itself; prefer a compute tool in
      `tools` for numbers that matter.
  """
  if not grounds and not tools:
    raise ValueError(
        f"answer({name!r}): give at least one of `grounds` (data to answer from) or `tools`"
        " (a read/compute whitelist to fetch it) — with neither there is nothing to answer"
        " from.")
  if max_turns <= 0:
    raise ValueError(
        f"answer({name!r}): max_turns must be positive, got {max_turns}.")
  block: dict[str, Any] = {
      "name": name,
      "scope": scope,
      "instruction": instruction,
      "max_turns": int(max_turns),
      "allow_math": bool(allow_math),
  }
  if grounds:
    block["grounds"] = list(grounds)
  if tools:
    block["tools"] = list(tools)
  if condition is not None:
    block["condition"] = _condition(condition)
  if requires:
    block["requires"] = list(requires)
  return block


IMPROVISE_CLASSES = ("reprompt", "no_input", "exhaust", "retry", "control",
                     "await", "filler")


def speech(
    *,
    improvise: list[str],
    improvise_style: Optional[str] = None,
) -> dict[str, Any]:
  """Which canned utterances the model may reword (flow policy `speech`).

  The framework speaks its recovery lines verbatim, preempting the model so the
  caller hears exactly the authored sentence — which is why the third no-match in a
  row is word-for-word the second. Naming a class here moves that family onto the
  directive channel instead: the authored line becomes an instruction the model
  rewords, the way a slot `ask` already works. Nothing is improvised by default.

  Classes: `reprompt` (validation no-match ladder), `no_input` (silence reprompts
  and hold_ack), `exhaust` (the give-up lines), `retry` (on_failure.retry_say),
  `control` (a control block's `declined_say`), `await` (async waiting lines),
  `filler` (a task's `filler_say` — see below, it works differently).

  Opting a class in is a REQUEST, not an override. A line stays literal whenever its
  turn also carries a tool call, a non-text response part (chips, audio, a transfer,
  an end_session), a status that has already terminated, or a pending value awaiting
  readback — the model's turn has no way to deliver any of those. Two consequences
  worth knowing before you author around them, both confirmed by driving them:

  * `cancel.say` / `escalate.say` land on an already-terminating turn, so `control`
    never reaches the final disposition line — which is usually the contractual one,
    so this is the right way round.
  * `confirm_say` is asked with the control slot PENDING, which routes the turn down
    the readback protocol rather than the directive fold. So `control` in practice
    means `declined_say`: the line a caller hears when a request is deflected, and
    the one that most needs to stop repeating.

  `filler` is the exception to the rule above, and the exception is worth
  understanding before you switch it on. A filler rides the same turn as the tool call
  it covers, so it cannot cross to the directive channel the way the others do — the
  call would be dropped. Instead the engine hands over the WHOLE turn: the model is
  asked for a reply containing both its own holding line and the call. That buys a line
  which fits the request ("I'm looking into your delivery status right now") rather
  than a fixed one, but it moves the dispatch to the model, so:

  * A task whose inputs include a `sensitive` slot is pinned verbatim automatically at
    build time — under this shape the arguments pass through the model's output.
  * Non-scalar or long arguments keep the engine's own dispatch, untested territory.
  * If the model answers without calling, the engine takes the turn back and fires it
    the ordinary way on the next pass. That costs the caller a turn, so it is a
    backstop, not a plan.

  `improvise_style` shapes the rewording and is appended to the directive on
  improvised turns only. Without it the model varies wording with no guidance, which
  is usually worse than the canned line — treat it as part of the feature, not a
  decoration.

  Per-site opt-out: `verbatim=True` on `user_slot`, `task`, `cancel`, `escalate`,
  `no_input` or `awaits` pins that one site literal against the policy.
  """
  if not improvise:
    raise ValueError(
        "speech(): improvise names no classes, so nothing would be improvised —"
        " omit the speech policy entirely instead")
  unknown = [c for c in improvise if c not in IMPROVISE_CLASSES]
  if unknown:
    raise ValueError(
        f"speech(): unknown improvise classes {unknown} — expected any of"
        f" {list(IMPROVISE_CLASSES)}.")
  policy: dict[str, Any] = {"improvise": list(improvise)}
  if improvise_style:
    policy["improvise_style"] = improvise_style
  return policy


# ---------------------------------------------------------------------------
# Tasks.
# ---------------------------------------------------------------------------


def task(
    name: str,
    tool: str,
    inputs: "list[str] | dict[str, str]",
    out_slot: str,
    *,
    out_key: Optional[str] = None,
    extra_outputs: Optional[dict[str, str]] = None,
    requires: Optional[list[str]] = None,
    condition: Optional[ConditionSpec] = None,
    success_check: str = "success",
    terminal: bool = False,
    then_say: "TextLike" = None,
    on_failure: Optional[dict[str, Any]] = None,
    clear_slots_on_success: Optional[list[str]] = None,
    transfer_to: Optional[str] = None,
    on_complete: Optional[dict[str, Any]] = None,
    readback_inputs: Optional[bool] = None,
    awaits: Optional[dict[str, Any]] = None,
    then_directive: Optional[str] = None,
    filler_say: Optional["FillerLike"] = None,
    while_running: Optional[dict[str, Any]] = None,
    parallel: Optional[str] = None,
    verbatim: bool = False,
    automatic_fillers: bool = True,
    count_into: Optional[str] = None,
) -> dict[str, Any]:
  """A tool-calling task: `tool(inputs)` -> `{out_key: ...}` mapped to `out_slot`.

  `inputs` is normally a list of slot names, which are passed to the tool as
  same-named parameters. Pass a `{slot: parameter}` dict when the two differ — a tool
  whose parameter names are not yours to choose, such as an A2A remote agent (its
  `task`/`contextId` are the platform's). `requires` still defaults to the slots.

  Terminal disposition (the most common carve node transfers back + gates readback):

  * `transfer_to` — sugar for `on_complete={"transfer_to": <agent>}`; merged into an
    explicit `on_complete` dict when one is also given WITHOUT its own `transfer_to`.
  * `on_complete` — general passthrough (e.g. `clear_slots`/`auto_resume_deferred`),
    rendered only when non-None. Passing BOTH `transfer_to` and an `on_complete` that
    already carries a `transfer_to` raises `ValueError` (no silent-override footgun).
  * `readback_inputs` — passthrough, rendered only when not None. Never auto-derived
    (PHI/PCI terminals must be able to set it False — that's the caller's decision).

  Feedback after the tool returns, in order of how much the model may vary it:

  * `then_say` — the literal sentence, spoken verbatim.
  * `then_directive` — an INSTRUCTION the model composes its reply from, given the
    tool result ("Tell the caller where the package is"). Use it when the reply
    depends on data whose shape you cannot template. Ignored when `then_say` is also
    set: a task speaks one way or the other, and the literal wins.

    ACROSS tasks the same precedence bites, and there it is silent. A `then_say` is
    delivered as a PREEMPT, which skips the model entirely — so a `then_say` task that
    becomes eligible on the same turn as a `then_directive` task cancels the composed
    answer before it is generated, and the caller hears a reply to a question they did
    not ask. The engine logs `directive_cancelled_by_preempt` at WARNING when this
    happens; nothing reaches the transcript, so that log is the only signal. Give the
    two mutually exclusive conditions, or gate the verbatim one on the directive's
    result slot being unfilled.

  Latency masking, both riding the same turn as the tool call:

  * `filler_say` — "one moment while I check". On a TASK it cannot be improvised: the
    model only gets a turn when the framework does not preempt, and not preempting
    drops the tool call the filler exists to cover. Author the wording you want. Pass
    a list to rotate across several, including `None` to sometimes say nothing —
    `["One moment.", "Let me check that.", None]` — because the same line on every
    wait is what makes an agent sound scripted. Lines may reference filled slots.
  * `automatic_fillers=False` — opt this task out of the build-time hoist that would
    otherwise move a contentless opener off the front of `then_say` into `filler_say`.
    Only meaningful under `App(automatic_fillers=True)`.
  * `while_running` — `{"audioUri": ...}` hold music.

  `verbatim` pins this task's `on_failure` retry/exhaust lines literal even when the
  flow's `speech` policy opts their class into improvisation.
  """
  # A search tool passed as the OBJECT fires through this ordinary path — it just needs
  # the three things about it that are not the author's to know: its parameter is the
  # platform's `query`, its response carries no `success` key (`snippets` is empty
  # exactly when the search found nothing, so it is the honest check), and the tool has
  # to be declared. See flows.authoring.search.
  searcher = None
  if isinstance(tool, _search.SearchTool):
    searcher, tool = tool, tool.name
    if not isinstance(inputs, dict):
      seq = list(inputs)
      if len(seq) != 1:
        raise ValueError(
            f"task {name!r} fires search tool {tool!r} with {len(seq)} inputs — a search "
            "takes exactly one `query`. Pass one slot, or compose several into one "
            "with a tool of your own and pass that slot"
        )
      inputs = {seq[0]: _search.SEARCH_QUERY_PARAM}
    if success_check == "success":
      success_check = _search.SEARCH_SNIPPETS_KEY
    if out_key is None:
      out_key = _search.SEARCH_SNIPPETS_KEY
  # An agent called as a tool has a wire contract of its own — `request` in,
  # `{"response": ...}` out, and no `success` key anywhere in it. All three follow from
  # the tool's type, so the author states none of them; each stays overridable.
  agent_caller = None
  if isinstance(tool, _agent_tool.AgentTool):
    agent_caller, tool = tool, tool.name
    if not isinstance(inputs, dict):
      seq = list(inputs)
      if len(seq) > 1:
        raise ValueError(
            f"task {name!r} fires agent tool {tool!r} with {len(seq)} inputs — an agent "
            f"takes exactly one, `{_agent_tool.AGENT_REQUEST_PARAM}`. Pass one slot, or "
            "compose several into one with a tool of your own and pass that slot")
      inputs = {seq[0]: _agent_tool.AGENT_REQUEST_PARAM} if seq else {}
    if success_check == "success":
      success_check = _agent_tool.AGENT_REPLY_KEY
    if out_key is None:
      out_key = _agent_tool.AGENT_REPLY_KEY
  # Every tool object in the SDK returns its name from `__str__` precisely so it can be
  # handed to `task()` directly — but the name was never taken. The object itself went
  # into the task dict, where downstream code uses `task["tool"]` as a dict key and
  # emits it as JSON, and a frozen dataclass holding a Mapping is neither hashable nor
  # serializable. The documented convenience therefore crashed for `api_tool` too.
  if not isinstance(tool, str):
    tool = str(tool)
  key = out_key or out_slot
  outputs = {key: out_slot}
  if extra_outputs:
    outputs.update(extra_outputs)
  # A dict is preserved as a dict: it is the engine's {slot: param} mapping form, and
  # `list()`-ing it would silently drop the parameter names and send the slot names.
  t: dict[str, Any] = {
      "name": name,
      "tool": tool,
      "inputs": dict(inputs) if isinstance(inputs, dict) else list(inputs),
      "outputs": outputs,
      "success_check": success_check,
      "terminal": terminal,
      "requires": requires if requires is not None else list(inputs),
  }
  if searcher is not None:
    # Ride the declaration in on the task, so `f.task(..., support, ...)` is enough and
    # the tool need not ALSO be named on the App. Lifted off by `Flow.task`.
    t[_SEARCH_TOOL_KEY] = searcher
  if agent_caller is not None:
    t[_AGENT_TOOL_KEY] = agent_caller
  cond = _condition(condition)
  if cond is not None:
    t["condition"] = cond
  if then_say:
    # Mirrors the slot ask: floor on `then_say`, alternative wordings in
    # `then_say_variants` (they replace it), structured content in `then_response`
    # (it accompanies, which is the framework's existing meaning for that key).
    t["then_say"] = floor_of(then_say)
    then_variants = variants_of(then_say)
    if then_variants:
      t["then_say_variants"] = then_variants
    then_payloads = payloads_of(then_say)
    if then_payloads:
      t["then_response"] = then_payloads
  if on_failure:
    t["on_failure"] = on_failure
  if clear_slots_on_success:
    t["clear_slots_on_success"] = clear_slots_on_success
  if transfer_to is not None:
    if on_complete is not None and "transfer_to" in on_complete:
      raise ValueError(
          "task(): pass transfer_to OR on_complete['transfer_to'], not both"
      )
    on_complete = {**on_complete, "transfer_to": transfer_to} if on_complete else {
        "transfer_to": transfer_to
    }
  if on_complete is not None:
    t["on_complete"] = on_complete
  if readback_inputs is not None:
    t["readback_inputs"] = readback_inputs
  if awaits is not None:
    t["awaits"] = awaits
  if count_into:
    # An integer slot the engine bumps on each fire, so "N times" can be a condition:
    # `{"slot": count_into, "gte": 3}`. The grammar already compares numbers, but
    # nothing produced one -- a latch holds "true", not a count -- so capping anything
    # meant deriving it in a callback.
    t["count_into"] = count_into
  if parallel:
    # Usually stamped by `parallel(...)`, which also checks the group's shape. Set
    # directly only when hand-building a group without the builder.
    t["parallel"] = parallel
  if then_directive is not None:
    t["then_directive"] = then_directive
  if filler_say is not None:
    t["filler_say"] = filler_say
  if while_running is not None:
    t["while_running"] = while_running
  if verbatim:
    t["verbatim"] = True
  if not automatic_fillers:
    # Build-time marker, stripped before validation (see build._apply_automatic_fillers).
    t["automatic_fillers"] = False
  return t


def awaits(
    *,
    max_turns: int,
    say: Optional[str] = None,
    while_waiting: Optional[list[str]] = None,
    answer_first: Optional[int] = None,
    on_timeout: Optional[dict[str, Any]] = None,
    verbatim: bool = False,
) -> dict[str, Any]:
  """Wait policy for a task whose tool is declared ASYNCHRONOUS.

  Such a tool answers twice: the call returns a platform-substituted
  `{"result": "pending"}`, and the real payload arrives one or more turns later as a
  synthetic user turn. Without this block the placeholder is read as a failure and — with
  `on_failure.max_retries` defaulting to 0 — escalates the flow on the very first fire.

  `max_turns` is required and must be positive, and it is a bound on the WAIT rather
  than on the tool. CES does cap the body — 60 seconds unless the tool resource says
  otherwise, which `@tool(timeout=…)` sets — but overrunning that cap is silent: the
  body never reports, so no completion ever arrives and the deadline that fires is this
  one. Turns are the unit because the engine has no clock, the same way the no_input
  ladder counts reprompts.

  The two spoken knobs cover different moments. `say` is the turn the wait STARTS.
  `while_waiting` is the turns after it, and only the ones with nothing else to do —
  when the flow is busy collecting an unrelated slot it asks the question rather than
  talking over it. The ladder drains and does not cycle: reassurance on a loop reads
  worse than silence, and `max_turns` is what actually ends the wait.

  Args:
    max_turns: Give up after this many turns without a completion.
    say: Spoken once when the wait starts. Omit for a silent wait.
    while_waiting: Lines for successive idle turns of the wait, one per turn. Once
      spent the hold falls back to silence. `{slot}` placeholders are interpolated.
    answer_first: Turns to spend answering the caller when their speech arrives on the
      SAME turn as the completion, before the downstream terminal is allowed to fire.
      Omit (the default) and that turn is treated purely as a delivery: the utterance is
      discarded and the terminal fires at once. Set it and the utterance is kept and
      handled normally, with the terminal held for at most this many turns. Capped by
      the engine, because a caller must not be able to hold a completed transaction open
      indefinitely by continuing to talk.
    on_timeout: Standard `on_exhaust` disposition (`say` / `then`) when the bound is hit.

  Returns:
    The `awaits` block for `task(awaits=...)`.
  """
  # An integral float is accepted and normalized, because a config round-tripped
  # through JSON carries 3 as 3.0 and this builder is also how a migration re-emits
  # one. Fractional values are still rejected: the unit is turns, which are discrete.
  if (not isinstance(max_turns, (int, float)) or isinstance(max_turns, bool)
      or max_turns != int(max_turns) or max_turns <= 0):
    raise ValueError(
        f"awaits(): max_turns must be a positive whole number, got {max_turns!r} —"
        " a body that overruns its timeout never reports at all, so nothing would end"
        " the wait")
  max_turns = int(max_turns)
  if while_waiting is not None and (
      not isinstance(while_waiting, (list, tuple))
      or not all(isinstance(x, str) for x in while_waiting)):
    raise ValueError(
        f"awaits(): while_waiting must be a list of strings, got {while_waiting!r}")
  if while_waiting and len(while_waiting) > max_turns:
    raise ValueError(
        f"awaits(): while_waiting has {len(while_waiting)} lines but max_turns is"
        f" {max_turns} — the wait ends before the last line could be spoken")
  block: dict[str, Any] = {"max_turns": max_turns}
  if say:
    block["say"] = say
  if while_waiting:
    block["while_waiting"] = list(while_waiting)
  if answer_first is not None:
    if (not isinstance(answer_first, (int, float))
        or isinstance(answer_first, bool)
        or answer_first != int(answer_first) or answer_first <= 0):
      raise ValueError(
          f"awaits(): answer_first must be a positive whole number of turns, got"
          f" {answer_first!r}")
    block["answer_first"] = int(answer_first)
  if on_timeout:
    block["on_timeout"] = on_timeout
  if verbatim:
    block["verbatim"] = True
  return block


def component(
    name: str,
    child_id: str,
    *,
    inputs: Optional[dict[str, str]] = None,
    outputs: Optional[dict[str, str]] = None,
    on_abort: str = "skip",
    requires: Optional[list[str]] = None,
    condition: Optional[ConditionSpec] = None,
) -> dict[str, Any]:
  """A component task: descend into the reusable child DAG `child_id`."""
  t: dict[str, Any] = {
      "name": name,
      "component": child_id,
      "inputs": inputs or {},
      "outputs": outputs or {},
      "on_abort": on_abort,
  }
  if requires is not None:
    t["requires"] = requires
  cond = _condition(condition)
  if cond is not None:
    t["condition"] = cond
  return t


# ---------------------------------------------------------------------------
# hold_and_wait — the reusable "caller asked to hold / went silent" pattern.
# A flow-level `no_input` policy: plain silence reprompts out loud; a "hold on"
# phrase waits quietly through a silent-tick ladder; on exhaust it either arms an
# in-flow offer slot (open_slot — keeps a number read AT the offer in scope) or
# descends into a reusable offer/help component.
# ---------------------------------------------------------------------------

# Speech matching one of these enters HOLD mode (silence, then waits quietly).
# Matched on WORD BOUNDARIES, so the bare interruption markers are safe to carry: "hold"
# does not match "household" and "sec" does not match "second opinion". A marker alone
# does not make a hold -- see DEFAULT_HOLD_VETOES for what disqualifies one.
DEFAULT_HOLD_PHRASES = [
    # The bare interruption markers, which are the commonest thing a caller says.
    # The bare time nouns ("a second", "a moment", "a minute") are deliberately NOT
    # here even on word boundaries: "I want a second opinion" is not a request for time,
    # and every real phrasing reaches one of the markers below anyway.
    "hold on", "hold up", "hold", "hang on", "wait", "one moment", "moment please",
    "just a moment", "just a sec", "just a second", "one sec", "a sec",
    "one second",
    "sec", "give me a", "gimme a", "let me have a",
    # Politeness frames. A caller being careful with a machine gets MORE formal.
    "can you hold", "could you hold", "mind holding", "please hold", "please wait",
    "bear with me", "stay with me", "be right back", "don t hang up",
    # Going to look something up.
    "let me find", "let me check", "let me look", "let me grab", "let me get",
    "let me pull", "let me open", "let me see", "let s see", "let me think",
    "looking for", "find it", "find my", "grab my", "get my number",
    "still looking", "trying to find", "look that up", "look it up",
    "need to find", "have to find", "in front of me",
    # Going somewhere to do it. This is the longest wait a caller asks for.
    "let me go", "let me walk", "let me put", "let me turn", "let me try",
    "let me make sure", "let me deal", "let me test", "let me unplug",
    "i need to go", "i have to go", "i m going", "i m heading", "going downstairs",
    "in the middle of",
]

# A marker is necessary and not sufficient. These are the things a caller says that carry
# a marker and are not a request for time: a question about the ask, a request to repeat
# it, a request for a person, a statement that they cannot answer, or a correction of
# what the call is about. Answering any of them with "take your time" is worse than
# re-asking, because the caller asked once and got patience instead of an answer.
#
# The engine carries this same list as its runtime default, so a config that sets no
# `hold_vetoes` still gets it; this export is for extending it. An explicit empty list
# restores marker-only matching.
DEFAULT_HOLD_VETOES = [
    "why do you", "why are you", "why would you", "why do i", "why should i",
    "what do you need", "what for", "what is that for", "what s that for",
    "what do you mean",
    "what did you say", "what was that", "say that again", "repeat that",
    "can you repeat", "come again", "didn t catch", "didn t hear", "pardon",
    "a person", "real person", "a human", "an agent", "representative",
    "supervisor", "operator", "transfer me", "talk to someone", "talk to a",
    "speak to someone", "speak to a", "get me someone",
    "already gave", "already told", "already said", "told you",
    "don t have an", "don t have a", "do not have an", "do not have a",
    "can t find", "cannot find", "couldn t find", "can t remember",
    "don t remember", "don t know it", "no idea",
    "this is about", "calling about", "not about", "i thought this was",
    "wrong department",
    # Calling the whole thing off. Without these a marker swallows the request: the
    # keyword backstops are suppressed on a hold turn, so "hold on, cancel that" enters
    # hold mode and the cancellation never reaches `cancel_flow`.
    "cancel", "stop", "never mind", "nevermind", "forget it", "forget about it",
]
# HOLD-mode ladder: empty entries = silent wait ticks; one gentle check-in.
DEFAULT_SILENT_TICKS = [
    "", "", "Take your time. I'm still here whenever you're ready.", "", "",
]


def hold_and_wait(
    *,
    reprompts: list[str],
    offer_slot: Optional[str] = None,
    offer_component: Optional[str] = None,
    say: Optional[str] = None,
    hold_reprompts: Optional[list[str]] = None,
    hold_phrases: Optional[list[str]] = None,
    hold_ack: Optional[str] = None,
    hold_vetoes: Optional[list[str]] = None,
    offer_inputs: Optional[dict[str, str]] = None,
    offer_outputs: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
  """Build a flow-level `no_input` policy for the hold-and-wait pattern.

  `reprompts` — spoken ladder for PLAIN silence (caller didn't ask to hold). Give
  at most one of `offer_slot` (arm an in-flow offer via open_slot) or
  `offer_component` (descend into a reusable offer child DAG) for the exhaust path.
  `say` is the graceful close spoken if the caller is silent through the offer too,
  and on its own is a complete exhaust path — closing the call gracefully with no
  escalation offer is a legitimate ladder. Defaults supply the standard hold phrases
  + silent-tick ladder.

  `hold_ack` is spoken IN PLACE OF the pending question on the turn the caller
  asks for time ("hold on, let me find it"), instead of putting the same question
  again — which is the one reply that request rules out. The question is not
  consumed: the silence that follows picks up the `hold_reprompts` ladder as
  usual, and a turn that also supplies the value fills it and moves on.

  Raises:
    ValueError: If BOTH offers are given (which used to drop `offer_component` on
      the floor), or if none of `offer_slot`/`offer_component`/`say` is (which used
      to emit `{"component": None}` — an exhaust path leading nowhere). Both were
      previously silent.
  """
  if offer_slot and offer_component:
    raise ValueError(
        "hold_and_wait(): give at most ONE of offer_slot= (arm an in-flow offer)"
        " or offer_component= (descend into an offer child DAG) — they are two"
        f" ways to spend the same exhaust turn; got offer_slot={offer_slot!r} and"
        f" offer_component={offer_component!r}.")
  if not (offer_slot or offer_component or say):
    raise ValueError(
        "hold_and_wait(): the silence ladder needs somewhere to go when it"
        " exhausts — give offer_slot=, offer_component=, or at least say= to close"
        " the call gracefully. Without one the exhaust leads nowhere.")
  on_exhaust: dict[str, Any] = {}
  if offer_slot:
    on_exhaust["open_slot"] = offer_slot
  elif offer_component:
    on_exhaust["component"] = offer_component
  if say:
    on_exhaust["say"] = say
  if offer_inputs:
    on_exhaust["inputs"] = offer_inputs
  if offer_outputs:
    on_exhaust["outputs"] = offer_outputs
  policy = {
      "reprompts": reprompts,
      "hold_reprompts": (
          hold_reprompts if hold_reprompts is not None else list(DEFAULT_SILENT_TICKS)
      ),
      "hold_phrases": (
          hold_phrases if hold_phrases is not None else list(DEFAULT_HOLD_PHRASES)
      ),
      "on_exhaust": on_exhaust,
  }
  if hold_ack:
    policy["hold_ack"] = hold_ack
  if hold_vetoes is not None:
    policy["hold_vetoes"] = list(hold_vetoes)
  return policy


# ---------------------------------------------------------------------------
# Flow + App builders.
# ---------------------------------------------------------------------------

# Private key a `delegate()` call task carries its RemoteAgent on, so splicing the task
# into a flow is enough to declare the agent. Stripped by `Flow.task` (and again by
# `to_config`) — it is an authoring-time object reference and must never reach config.
_REMOTE_AGENT_KEY = "_remote_agent"

# The same, for a `research()` search task and its SearchTool.
_SEARCH_TOOL_KEY = "_search_tool"
_AGENT_TOOL_KEY = "_agent_tool"

# Top-level Config keys that carry flow policy (mirrors the validator whitelist).
_POLICY_KEYS = frozenset({
    # Following-along cues ("mhmm", "got it") and what to do about an interruption. Both
    # are flow-level for the same reason `no_input` is: they describe how this flow reacts
    # to a conversational event, not to any one slot.
    "continue_cues", "on_interrupted",
    "gate_slot", "correction_tool", "bootstrap", "no_input", "steer_back",
    "cancel", "escalate", "intent_change", "confirm_transition_prefix",
    "exit_status", "event_mappings", "readback_response",
    "channel_readback_response", "shared_slots", "flow_types", "router",
    "route_cues", "flow_descriptions", "single_flow", "speech",
    # Flow-level default latency filler, for any model turn whose slot does not carry
    # its own. A pool is the usual shape here — one line across a whole flow is what
    # makes an agent sound scripted (see FillerLike).
    "filler_say",
    # A LIST of grounded, intent-scoped free-response fallbacks (flows.answer). Each fields
    # an off-menu, on-intent QUESTION with a grounded, tool-whitelisted, non-advancing turn
    # instead of steering back. Read/compute only — commit tools stay DAG cues.
    "answer",
    # False suppresses the SYNTHESIZED cancel/escalate control slot entirely, so its
    # framework tool is never advertised to the model. The escalation flow ITSELF sets
    # `escalatable: False` — otherwise transfer_to_human is offered on turn one and the
    # flow terminates before its own containment ask runs. Not the same as
    # `escalate.condition`, which still synthesizes the slot and merely declines after.
    "cancelable", "escalatable",
    # A flow-level terminal return delivered on the end_session tool call's params (see
    # flows.end_params_handoff / Flow.on_end). The engine folds it onto every terminal end.
    "on_end",
})


@dataclass
class ParallelGroup:
  """The tasks and slots a `parallel()` call splices into a flow.

  Hand it straight to `Flow.task(...)`, which takes both halves in one call.
  """

  name: str
  tasks: list[dict[str, Any]]
  slots: list[dict[str, Any]]


def parallel(
    name: str,
    *,
    tasks: list[dict[str, Any]],
    all_done_say: "TextLike" = None,
    deadline: Optional[int] = None,
    waiting_say: Optional[str] = None,
    while_waiting: Optional[list[str]] = None,
    on_timeout: Optional[dict[str, Any]] = None,
    filler_say: Optional["FillerLike"] = None,
    while_running: Optional[dict[str, Any]] = None,
    progressive: bool = True,
) -> ParallelGroup:
  """Dispatch several independent tasks TOGETHER instead of one per pass.

  The engine fires one task per pass, so three independent lookups cost three
  re-invocations and — because each one blocks its turn — three lookups' worth of the
  caller's time. Grouping them dispatches all the eligible ones in a single action, and
  the runtime runs them concurrently: **three four-second legs cost the caller four
  seconds, not twelve** (measured against CES; `span=4.0` against `sum=12.0`, with a
  single-leg baseline of 4.0).

  Each leg carries its own `then_say`, so the line lives next to the task that produces
  it. The synchronous legs' lines are concatenated into ONE utterance in DECLARATION
  order — never arrival order, which is neither stable nor observable. A leg that fails
  simply contributes no line, so one flaky backend cannot silence its siblings.

  A group is a **batching hint, not a barrier**. A leg gated off by its `condition`,
  already complete, or awaiting an asynchronous result is not in that pass's set and
  fires later, possibly alone. Holding the group until every leg was ready would let one
  never-ready leg wedge the rest forever.

  `deadline` / `waiting_say` / `on_timeout` are declared once here and land only on the
  legs whose tool is `@tool(asynchronous=True)` — the author never says which those are.

  **A synchronous group cannot narrate progress.** The runtime hands back the whole batch
  after the slowest leg; it does not re-enter the framework when the fastest one lands
  (measured with legs of 1s/5s/9s). If you want a line per result as it arrives, the slow
  legs must be asynchronous — their completions arrive one turn each.

  Args:
    name: The group's name. Also names the `<name>_done` flag an all-done line waits on.
    tasks: The legs, built with `task(...)`. Two or more, and no two may share a tool.
    all_done_say: Spoken once every leg has REPORTED — not necessarily succeeded. In an
      all-synchronous group it lands in the same utterance, after the per-leg lines.
    deadline: Turns to allow an asynchronous leg (the engine has no clock).
    waiting_say: Spoken once, when the wait starts. Only the first asynchronous leg may
      carry one, or the caller hears several holding lines on one turn.
    while_waiting: Reassurance for the idle turns AFTER the wait starts, one line per
      turn, draining rather than cycling. The alternative to a per-leg `then_say`: that
      narrates each finding as it lands, which is right when the caller wants progress
      and wrong when the legs are internal plumbing whose names mean nothing to them.
      Carried by the same first asynchronous leg as `waiting_say`, for the same reason.
    on_timeout: Disposition when `deadline` runs out, in the `on_exhaust` vocabulary.
    filler_say: A spoken filler on the firing turn. Kept verbatim for a group: improvised
      filler hands the CALL to the model, a shape validated for exactly one call.
    while_running: Hold music on the firing turn.

  Returns:
    A `ParallelGroup` to splice in with `flow.task(group)`.
  """
  if len(tasks) < 2:
    raise ValueError(
        f"parallel({name!r}): a group needs at least two legs, got {len(tasks)} — a"
        " group of one is a plain task carrying extra machinery.")
  seen: dict[str, str] = {}
  for leg in tasks:
    tool_name = leg.get("tool")
    if not tool_name:
      raise ValueError(
          f"parallel({name!r}): leg {leg.get('name')!r} has no tool. A component"
          " descends into a child DAG and ends the pass, so it cannot ride a shared"
          " dispatch — fire it before or after the group.")
    if leg.get("terminal"):
      raise ValueError(
          f"parallel({name!r}): leg {leg['name']!r} is terminal. A terminal fire tears"
          " the flow down, so its siblings' results would land on a flow that has"
          " already ended. Put the terminal downstream of the group.")
    if tool_name in seen:
      raise ValueError(
          f"parallel({name!r}): legs {seen[tool_name]!r} and {leg['name']!r} both call"
          f" {tool_name!r}. The results come back keyed by tool name, so two calls to"
          " one tool cannot be told apart.")
    seen[tool_name] = leg["name"]
    leg["parallel"] = name
    if not progressive:
      # Opt out of per-leg narration and take the batch shape instead: the legs stay
      # SYNCHRONOUS, go out in one action, and CES runs them concurrently and hands the
      # whole batch back on the same pass (ces-probes 33 and 65).
      #
      # It costs one reasoning pass rather than one plus a watch pass per window, and
      # the ten-pass ceiling never resets (ces-probes 72/73) — so on a DAG that already
      # spends most of its budget, progressive narration is what makes the group
      # unaffordable, not the concurrency. It also keeps each leg's own tool name, so
      # name-keyed config (a `toolFakeConfig`, a golden) still attaches.
      #
      # What it gives up is real: a synchronous fan-out has exactly ONE observation
      # point, after the slowest leg (ces-probes 40). No line lands as each result
      # arrives. Choose per group.
      leg["parallel_batch"] = True

  # Which legs are deferred is INFERRED from the tool decorator, so the author never
  # names a mechanism. Imported here rather than at module scope: `tools` imports this
  # module for its own type surface, and the registry is only populated once the
  # decorators have run, which is after import either way.
  from . import tools as _tools  # noqa: PLC0415
  async_legs = [leg for leg in tasks
                if leg["tool"] in _tools.registered_async_tools()]
  if progressive:
    # A progressive group lowers EVERY leg to an asynchronous publishing wrapper, so its
    # legs are deferred by CONSTRUCTION and the decorator registry has nothing to say
    # about them. Consulting only the registry made these knobs unusable on the case that
    # needs them most — a converted agent, whose legs are grafted tool resources no
    # `@tool` decorator ever saw — and it did not degrade, it refused the group outright.
    async_legs = list(tasks)
  if (deadline or waiting_say or while_waiting or on_timeout) and not async_legs:
    raise ValueError(
        f"parallel({name!r}): deadline/waiting_say/while_waiting/on_timeout apply to a"
        " deferred leg. This group is progressive=False and no leg's tool is declared"
        " @tool(asynchronous=True), so no leg ever waits.")
  for i, leg in enumerate(async_legs):
    existing = leg.get("awaits") or {}
    if deadline and existing.get("max_turns") not in (None, deadline):
      raise ValueError(
          f"parallel({name!r}): leg {leg['name']!r} sets max_turns"
          f" {existing['max_turns']} but the group's deadline is {deadline}.")
    merged = dict(existing)
    if deadline:
      merged.setdefault("max_turns", deadline)
    if on_timeout:
      merged.setdefault("on_timeout", on_timeout)
    # Only the FIRST deferred leg speaks a holding line; several would all be produced
    # on the same turn, so the caller would hear the same reassurance N times.
    if waiting_say and i == 0:
      merged.setdefault("say", waiting_say)
    if while_waiting and i == 0:
      merged.setdefault("while_waiting", list(while_waiting))
    if merged:
      leg["awaits"] = merged
  if filler_say:
    tasks[0]["filler_say"] = filler_say
  if while_running:
    tasks[0]["while_running"] = while_running

  slots: list[dict[str, Any]] = []
  if all_done_say:
    # `<name>_done` is filled by the engine once every leg has reported. Declared as an
    # event slot because nothing in the DAG produces it as an output.
    slots.append(event_slot(f"{name}_done"))
    slots.append(announce(
        f"{name}_all_done",
        [all_done_say] if isinstance(all_done_say, str) else list(all_done_say),
        requires=[f"{name}_done"],
        preempt=True,
    ))
  return ParallelGroup(name=name, tasks=list(tasks), slots=slots)


class Flow:
  """A slot-filling flow — accumulates slots/tasks/policies into a `Config` dict.

  `config_id` names the emitted `<config_id>_dag` tool; `root_agent` is the agent
  that runs it. Policy kwargs (bootstrap/cancel/escalate/no_input/router/...) are
  validated against the framework's config-key whitelist.
  """

  def __init__(self, config_id: str, *, root_agent: str = "", **policy: Any):
    self.config_id = config_id
    self.root_agent = root_agent
    self._slots: list[dict[str, Any]] = []
    self._tasks: list[dict[str, Any]] = []
    self._policy: dict[str, Any] = {}
    # Remote A2A agents carried in by a spliced `delegate()` task (see `task`). The
    # build unions these with `App(remote_agents=...)`, so a delegation reaches its
    # agent from one declaration instead of two.
    self._remote_agents: list[Any] = []
    # Search tools carried in by a spliced `research()` task, on the same terms.
    self._search_tools: list[Any] = []
    # Agent tools carried in on a task that was handed the declaration itself.
    self._agent_tools: list[Any] = []
    for k, v in policy.items():
      self.set(k, v)

  def add(self, *slots: dict[str, Any]) -> "Flow":
    """Append one or more slot dicts (order = ask order)."""
    self._slots.extend(slots)
    return self

  # Alias so `flow.slots(...)` reads naturally.
  slots = add

  def task(self, *args: Any, **kwargs: Any) -> "Flow":
    """Append task(s). Accepts prebuilt task dicts, or the `task(...)` args.

    Several dicts at once so a multi-task primitive splices in one call
    (`flow.task(*delegation.tasks)`), mirroring `add(*slots)`. A `task(...)` call
    always leads with the task NAME, so a leading dict is unambiguous.
    """
    # A parallel group brings both halves — its legs and the slots an all-done line
    # needs — so it splices in ONE call rather than making the author remember to add
    # the slots separately and in the right order.
    if len(args) == 1 and not kwargs and isinstance(args[0], ParallelGroup):
      group = args[0]
      self._slots.extend(group.slots)
      self._tasks.extend(group.tasks)
      return self
    if args and not kwargs and all(isinstance(a, dict) for a in args):
      # A `delegate()` call task rides its RemoteAgent in on a private key. Lift it off
      # here: it is an authoring-time reference, not config, and leaving it on the dict
      # would put an object in the emitted JSON.
      for a in args:
        agent = a.pop(_REMOTE_AGENT_KEY, None)
        if agent is not None:
          self._remote_agents.append(agent)
        searcher = a.pop(_SEARCH_TOOL_KEY, None)
        if searcher is not None:
          self._search_tools.append(searcher)
        caller = a.pop(_AGENT_TOOL_KEY, None)
        if caller is not None:
          self._agent_tools.append(caller)
      self._tasks.extend(args)
    else:
      built = task(*args, **kwargs)
      searcher = built.pop(_SEARCH_TOOL_KEY, None)
      if searcher is not None:
        self._search_tools.append(searcher)
      caller = built.pop(_AGENT_TOOL_KEY, None)
      if caller is not None:
        self._agent_tools.append(caller)
      self._tasks.append(built)
    return self

  def set(self, key: str, value: Any) -> "Flow":
    """Set a flow-level policy key (bootstrap/cancel/escalate/no_input/router/...)."""
    if key not in _POLICY_KEYS:
      raise ValueError(
          f"unknown flow policy key {key!r}; valid: {sorted(_POLICY_KEYS)}"
      )
    self._policy[key] = value
    return self

  def on_end(self, handoff: Any) -> "Flow":
    """Attach a flow-level terminal return, applied to EVERY terminal end (any reason).

    Takes a `flows.end_params_handoff(...)`: the framework's deterministic terminal emit
    folds its `{envelope: state[from_state]}` onto the end_session tool call's params at the
    termination choke, so a native channel gets its return without a per-exit hook and
    without the model dropping the end. See docs/end-params-handoff.md.
    """
    if not hasattr(handoff, "to_config"):
      raise TypeError(
          "Flow.on_end() takes a flows.end_params_handoff(...) (an EndParamsHandoff); got "
          f"{type(handoff).__name__}")
    return self.set("on_end", handoff.to_config())

  def to_config(self) -> dict[str, Any]:
    """Render the plain `Config` dict (the shape YAML also produces)."""
    cfg: dict[str, Any] = dict(self._policy)
    cfg["slots"] = list(self._slots)
    # Belt and braces: `Flow.task` already lifts the key off, but a task dict reaching
    # `_tasks` another way must not carry an object into the emitted JSON.
    cfg["tasks"] = [{k: v for k, v in t.items()
                     if k not in (_REMOTE_AGENT_KEY, _SEARCH_TOOL_KEY,
                                  _AGENT_TOOL_KEY)}
                    for t in self._tasks]
    _check_counters(cfg)
    return cfg


def _check_counters(cfg: dict[str, Any]) -> None:
  """A `count_into` slot has to be the engine's alone to write.

  The engine reads the running total with `int(...)`, so a value from any other source —
  a spoken account number, another task's output — raises mid-dispatch and the caller
  hears the platform's failure line instead of their turn. Refused here rather than in
  `task()` because only the assembled config knows every slot and every other task.

  Declaring the counter is still allowed, and is how you give it a default or share it
  across flows; what is refused is a second writer.
  """
  produced: dict[str, str] = {}
  for t in cfg["tasks"]:
    for out in (t.get("outputs") or {}).values():
      # One out_key can map to SEVERAL slots, so a value is a name or a list of them.
      for slot in (out if isinstance(out, list) else [out]):
        if isinstance(slot, str):
          produced.setdefault(slot, t.get("name", "?"))
  # A slot with several producers carries a LIST of sources, so this cannot read the
  # field as a string — stringifying one would quietly match nothing.
  spoken = set()
  for s in cfg["slots"]:
    src = s.get("source")
    for one in (src if isinstance(src, list) else [src]):
      if isinstance(one, str) and one.startswith("user"):
        spoken.add(s.get("name"))
  for t in cfg["tasks"]:
    counter = t.get("count_into")
    if not counter:
      continue
    if counter in spoken:
      raise ValueError(
          f"task {t.get('name')!r}: count_into={counter!r} names a slot the CALLER "
          "fills, so the count would be overwritten with speech and the engine would "
          "raise reading it back. Count into a name of its own.")
    if counter in produced:
      raise ValueError(
          f"task {t.get('name')!r}: count_into={counter!r} is also the output slot of "
          f"task {produced[counter]!r}. A counter may have only one writer — count into "
          "a name of its own.")


# ---------------------------------------------------------------------------
# Routing (G6) — a single-agent router ROOT flow (1:1 with CES's router.json).
# ---------------------------------------------------------------------------


def router_flow(
    config_id: str,
    flows: "list[str] | list[Any]",
    *,
    route_cues: Optional[dict[str, list[str]]] = None,
    intent_slot: Optional[dict[str, Any]] = None,
    default_flow: str = "",
    root_agent: str = "",
    engine_only_tools: "list[str] | tuple[str, ...]" = (),
    disambiguate: Any = None,
    default_route: str = "",
    catch_all_route: str = "",
    tie_break: str = "primary",
    routing_notes: "list[str] | tuple[str, ...]" = (),
    classifier_style: str = "enum",
    route_mode: str = "hierarchical",
    filler_say: Optional["FillerLike"] = None,
) -> Flow:
  """A SINGLE-AGENT router root flow (1:1 with CES's `router.json` shape).

  Two ways to declare the routable destinations:

  * **Bare flow keys** (`flows=["diagnostics", "reboot", ...]`): the low-level form. You
    supply the `<routing>` instruction, the child flows (`App.extra_flows`), and any
    `route_cues` separately. Unchanged, backward-compatible behaviour.
  * **Route objects** (`flows=[flows.route("diagnostics", "...", flow=diag), ...]`): the
    first-class steering form. Each `route(...)` carries its name, a semantic
    `description`, its child `flow` (or None to defer), and deterministic `cues`. From
    them `build.py` GENERATES the `<routing>` instruction (from the descriptions), adds
    the handled child flows plus ONE shared deferral flow, folds the `cues` into
    `route_cues`, and emits the non-identity `flow_config_map` that lets every deferred
    route share the one deferral flow while keeping its own `detected_intent` label.
    `disambiguate` is the single low-confidence path: when set, the model asks a brief
    clarifying question for ANY case it cannot confidently route (ambiguous, unclear, or
    off-topic) and, with `disambiguation(max_turns, on_exhaust)`, hands off after a bounded
    number of turns. A deferred route (`flow=None`) uses ONE generated hand-off flow
    (records `detected_intent`, speaks a default line, ends); for a custom line or a real
    transfer, give routes a shared `flow` instead. `engine_only_tools` names tools kept off
    the model's list (a cold-start guard). `backstop` (per-route) is recognised by the
    post-model engine capability.

  Returns a `Flow` whose `to_config()` is the router config: `router=True`,
  `gate_slot="active_flow"`, a `set_active_flow` bootstrap onto `active_flow`, and
  `flow_types` (the routable child flow keys). Order is PRESERVED (never sorted) so the
  runtime's earliest-utterance-position tiebreak matches authored order.

  `default_flow` names the HOME-BASE flow: the one an utterance matching no cue lands
  on, and — because `build.py` also emits it as a state var — the one a cold turn
  activates before the caller has said anything routable. Required for a SILENT flow
  (one with no setter for the model to call, e.g. a diagnostic fan-out), since nothing
  can route INTO such a flow; see the home-base activation docs. Must be one of `flows`.
  It is a pre-model preempt, so it is NOT how a Route-based router handles low confidence
  — use `disambiguate=` for that.

  `filler_say` covers the silence the caller sits through while the router decides. A
  routing turn is the slowest turn of a call — it spends several serialized round trips
  where an in-flow turn spends one — and until this existed it was also the only turn
  that could not speak early, because the engine returns here long before a filler is
  normally armed. The line goes out as a `partial` preempt, so the routing decision
  still lands in the SAME turn behind it. Takes a string or a pool; a `None` in the pool
  is silence that turn. Write it intent-neutral: it is spoken BEFORE the caller's intent
  is known, so "Let me get you to the right place" is a promise it cannot keep — prefer
  "One moment.".

  `route_mode` controls how a multi-level tree (routes with `subroutes`) is classified.
  `"hierarchical"` (the default) resolves the intent path in one model pass PER LEVEL: the L1
  router picks the top-level route, then each internal node runs its own scoped classifier.
  `"flat"` folds the whole tree into a SINGLE classification straight to the leaf — the leaves
  become the gate enum, the category is DERIVED from the chosen leaf (recorded as
  `detected_path`), and the `<routing>` block keeps the category grouping with contrastive
  rule-out notes so the model still reasons coarse-to-fine but commits in one call. One
  inference instead of N per turn, measured as accurate-or-better on the corpora tried (see
  the Steering doc for the tradeoff). Requires the route-object (tree) form — the bare-key
  form has no tree to fold and raises.
  """
  if route_mode not in ("hierarchical", "flat"):
    raise ValueError(
        f"router_flow({config_id!r}): route_mode={route_mode!r} must be 'hierarchical' or "
        "'flat'")
  routes = _as_routes(flows)
  spec = None
  _groups: tuple = ()
  _leaf_paths: "dict[str, str]" = {}
  if routes is not None:
    from . import steering as _steering  # noqa: PLC0415 (avoid an import cycle)
    # A name is emitted as a flow key, a config id, a sub_intent slot value AND a classifier
    # key, so it must be unique across the WHOLE tree (any depth), not just the level-1 set.
    # Validate on the AUTHORED tree first (catches cross-category leaf-name collisions that
    # would then also collide once flattened).
    _all = [n.name for n in _steering.all_nodes(routes)]
    _dupes = sorted({k for k in _all if _all.count(k) > 1})
    if _dupes:
      raise ValueError(f"router_flow({config_id!r}): duplicate route names {_dupes}")
    # FLAT single-pass (A2): FOLD the category tree into a flat leaf set for classification
    # (the gate enum becomes the leaves; `set_active_flow` picks a leaf in one inference) while
    # retaining the grouping for the <routing> block + a leaf->path map for detected_path.
    # After this, `routes` are plain deferred leaves, so every branch below (config_map, child
    # flows, deferral recorder) reuses the proven plain-flat path.
    if route_mode == "flat":
      routes, _groups, _leaf_paths = _steering.flatten_tree(routes)
    keys = [r.name for r in routes]
    _on_exhaust = getattr(disambiguate, "on_exhaust", "")
    if _on_exhaust and _on_exhaust not in keys:
      raise ValueError(
          f"router_flow({config_id!r}): disambiguation on_exhaust={_on_exhaust!r} is not "
          f"one of the routes {keys}")
    # v1: a DEEP level's disambiguation cannot hand off cross-tree on exhaust (that needs a
    # flow transition the multi-level classifier does not yet do) — it falls to `default=`.
    # Reject an explicit per-node on_exhaust so the limitation is loud, not silent.
    for _n in _steering.all_nodes(routes):
      if getattr(_n.disambiguate, "on_exhaust", ""):
        raise ValueError(
            f"router_flow({config_id!r}): route {_n.name!r} sets a per-level disambiguate= "
            f"with on_exhaust={_n.disambiguate.on_exhaust!r}, which is not yet supported "
            "(v1) — use default= to name the fallback child, or set on_exhaust on the "
            "router-level disambiguate= (level-1 routing).")
    # v1: a handled leaf (flow=) is only supported at the TOP level. A DEEPER handled leaf
    # would need a flow transition the multi-level classifier does not do yet — a deep route
    # classifies + defers (records the detected path). Reject a deep flow= so it is loud.
    _top = {r.name for r in routes}
    for _n in _steering.all_nodes(routes):
      if _n.handled and _n.name not in _top:
        raise ValueError(
            f"router_flow({config_id!r}): route {_n.name!r} has flow= but is not a top-level "
            "route — a handled leaf is only supported at the top level (v1); a deeper route "
            "classifies and defers. Keep the handled flow as a top-level route.")
    if default_flow:
      raise ValueError(
          "router_flow(): default_flow is a pre-model preempt and defeats a Route-based "
          "router — use disambiguate= for model-driven low-confidence handling")
    # Structured routing policies must name real top-level routes, and neither the home
    # nor the catch-all can be an explicit_only route (the model would be steered to it by
    # inference, defeating the guard).
    _explicit = {r.name for r in routes if r.explicit_only}
    _role = {"default_route": "the home / low-confidence fallback",
             "catch_all_route": "the catch-all"}
    for _knob, _val in (("default_route", default_route),
                        ("catch_all_route", catch_all_route)):
      if _val and _val not in keys:
        raise ValueError(
            f"router_flow({config_id!r}): {_knob}={_val!r} is not one of the routes {keys}")
      if _val and _val in _explicit:
        raise ValueError(
            f"router_flow({config_id!r}): {_knob}={_val!r} is marked explicit_only — a route "
            f"the model must reach only by an explicit request cannot be {_role[_knob]} "
            "(the model would be steered to it by inference)")
    if tie_break not in ("primary", "none"):
      raise ValueError(
          f"router_flow({config_id!r}): tie_break={tie_break!r} must be 'primary' or 'none'")
    if classifier_style not in ("fuzzy", "enum"):
      raise ValueError(
          f"router_flow({config_id!r}): classifier_style={classifier_style!r} must be "
          "'fuzzy' or 'enum'")
    spec = _steering.SteeringSpec(
        routes=tuple(routes),
        config_id=config_id,
        engine_only_tools=tuple(engine_only_tools),
        disambiguate=disambiguate,
        default_route=default_route,
        catch_all_route=catch_all_route,
        tie_break=tie_break,
        routing_notes=tuple(routing_notes),
        classifier_style=classifier_style,
        route_mode=route_mode,
        groups=_groups,
        leaf_paths=_leaf_paths,
    )
    flow_keys = keys
    merged_cues = spec.route_cues()
    if route_cues:
      merged_cues.update({k: list(v) for k, v in route_cues.items()})
    route_cues = merged_cues or None
  else:
    flow_keys = list(flows)
    if route_mode == "flat":
      raise ValueError(
          "router_flow(): route_mode='flat' needs the route-object form (flows=["
          "flows.route(...), ...]) — it folds a category tree into one leaf classification, "
          "which the bare-key form has no tree to fold")
    if default_route or catch_all_route or routing_notes:
      raise ValueError(
          "router_flow(): default_route / catch_all_route / routing_notes need the "
          "route-object form (flows=[flows.route(...), ...]) — they generate the <routing> "
          "block, which the bare-key form does not")
    if default_flow and default_flow not in flow_keys:
      raise ValueError(
          f"router_flow(): default_flow {default_flow!r} is not one of flows {flow_keys!r}")

  f = Flow(config_id, root_agent=root_agent)
  f.set("router", True)
  f.set("gate_slot", "active_flow")
  # `intent_first` makes routing DETERMINISTIC: the engine classifies the turn and its
  # ordered backstops run — `route_backstop` activates the cue-matched flow, and only if
  # nothing matched does `default_flow_backstop` fall back to the home base. Without it
  # both backstops are skipped and routing rests entirely on the model calling
  # `set_active_flow` with a valid flow name, which it does not reliably do.
  boot: dict[str, Any] = {"tool": "set_active_flow", "slot": "active_flow",
                          "intent_first": True}
  if default_flow:
    boot["default_flow"] = default_flow
  f.set("bootstrap", boot)
  # PRESERVE order — never sort (runtime same-offset tiebreak == authored order).
  f.set("flow_types", flow_keys)
  if route_cues is not None:
    f.set("route_cues", route_cues)
  if filler_say is not None:
    f.set("filler_say", filler_say)
  if intent_slot is not None:
    f.add(intent_slot)
  # Authoring-time reference (never emitted as config, like `Flow._remote_agents`):
  # build.py reads it to add child flows, emit the flow_config_map, and generate the
  # <routing> instruction.
  if spec is not None:
    f._steering = spec  # type: ignore[attr-defined]
  return f


def _as_routes(flows: Any) -> "Optional[list[Any]]":
  """`flows` as a list of Route objects, or None when it is the bare-key form.

  Duck-typed (a Route has `name`/`description`/`handled`) so `router_flow` does not have
  to import `steering` unless it is actually given routes, keeping the low-level path
  import-free. Mixing routes and bare strings is an error.
  """
  seq = list(flows or [])
  if not seq:
    return None
  is_route = [hasattr(x, "name") and hasattr(x, "description") and hasattr(x, "handled")
              for x in seq]
  if all(is_route):
    return seq
  if any(is_route):
    raise ValueError(
        "router_flow(): mix of flows.route(...) objects and bare flow-key strings — "
        "use one form or the other")
  return None


# ---------------------------------------------------------------------------
# Connected journey (G5) — the "Shape B" builder: intent -> shared spine ->
# intent-gated operation terminals, emitted as a plain slot-filling flow. The
# backend flattens the shared spine in legalize(); no engine change is needed.
# ---------------------------------------------------------------------------


@dataclass
class Operation:
  """One operation (intent branch) of a connected journey.

  `value` is the intent enum value that selects this op (e.g. ``"pay_bill"``); `cues`
  are the spoken phrases that map to it (feed the journey's intent slot `option_cues`).
  `slots` are this op's own user slots (asked after the shared spine); `tasks` are its
  tasks, the LAST of which is the terminal. `journey()` DERIVES each task's gate from
  `value`, so the gate can never desync from the intent enum.
  """

  value: str
  cues: list[str]
  slots: list[dict[str, Any]] = field(default_factory=list)
  tasks: list[dict[str, Any]] = field(default_factory=list)


def journey(
    config_id: str,
    *,
    spine: Any,
    operations: list[Operation],
    parent: str,
    intent_name: str = "journey_intent",
    root_agent: str = "",
    welcome: Optional[str] = None,
) -> Flow:
  """Build a connected-journey (Shape B) flow: intent -> shared spine -> op terminals.

  Layout of the returned plain `Flow`:

  * optional `welcome` announce first (shared).
  * a first-class INTENT slot (`intent_slot(intent_name, {op.value: op.cues})`) — its
    `option_cues` + enum rule derive from `operations`, the single source of truth.
  * the shared SPINE (prerequisite slots asked ONCE for every op): `spine` is a list of
    slot dicts added after the intent; if `spine` is a component-id string, a
    `component(...)` is added instead.
  * per operation: its op-specific `slots`, then its `tasks` — each task GATED on the
    op via `condition = eq(intent_name, op.value)`, and the TERMINAL task additionally
    given `on_complete={"transfer_to": parent}` + `terminal=True`.

  The terminal gate value is DERIVED from `op.value` (a hand-authored gate is
  overwritten — it cannot desync from the intent enum). There must be exactly ONE
  terminal per operation.

  Raises `ValueError` if two operations share a value, or an operation has no tasks or
  more than one terminal task.

  This is ordinary DSL output (no engine change): the backend flattens the shared spine
  in `legalize()`; the intent enum + per-terminal gates are generated so they cannot
  desync.
  """
  seen: set[str] = set()
  for op in operations:
    if op.value in seen:
      raise ValueError(f"journey(): duplicate operation value {op.value!r}")
    seen.add(op.value)
    if not op.tasks:
      raise ValueError(f"journey(): operation {op.value!r} has no tasks")
    n_terminal = sum(1 for t in op.tasks if t.get("terminal"))
    if n_terminal != 1:
      raise ValueError(
          f"journey(): operation {op.value!r} must have exactly ONE terminal task, "
          f"found {n_terminal}"
      )

  f = Flow(config_id, root_agent=root_agent)
  if welcome is not None:
    f.add(announce(f"{config_id}_welcome", [welcome], shared=True))

  # The intent enum + its cues derive from operations (single source of truth).
  f.add(intent_slot(intent_name, {op.value: list(op.cues) for op in operations}))

  # Shared spine — asked once, after the intent, before any op-specific slots.
  if isinstance(spine, str):
    f.task(component(f"{config_id}_spine", spine))
  else:
    f.add(*spine)

  for op in operations:
    g = eq(intent_name, op.value)  # gate DERIVED from op.value — cannot desync
    # Op-specific SLOTS must be gated on the intent too — otherwise the engine collects
    # every op's slots regardless of the chosen intent (e.g. asks the pay amount on a
    # balance request). The task gate alone is NOT enough; a live LLM will happily fill an
    # ungated slot. AND with any author-supplied condition.
    for s in op.slots:
      s = dict(s)
      own = s.get("condition")
      # Match the author's form: the two condition forms cannot nest, so a declaratively
      # gated op slot gets the declarative twin of the same intent gate.
      s["condition"] = _and_conditions(
          own, {"slot": intent_name, "eq": op.value} if isinstance(own, dict) else g)
      f.add(s)
    for t in op.tasks:
      t = dict(t)  # copy — never mutate the caller's task dict
      t["condition"] = g  # terminal/op-task gate is DERIVED from op.value (clean eq for the lint)
      if t.get("terminal"):
        t["terminal"] = True
        oc = dict(t.get("on_complete") or {})
        oc["transfer_to"] = parent  # terminal transfers back to the parent
        t["on_complete"] = oc
      f.task(t)
  return f


# ---------------------------------------------------------------------------
# Verdict ladder — a SILENT diagnostic spine followed by an ORDERED priority
# ladder of condition-gated outcomes, exactly one of which speaks. The shape a
# fan-out orchestrator needs: run every check first, then arbitrate once.
# ---------------------------------------------------------------------------


@dataclass
class VerdictBranch:
  """One rule of a priority ladder: a condition-gated outcome.

  List order IS the priority — the first branch whose `condition` holds is the one
  the caller hears. `say` is delivered VERBATIM unless `generative`, which hands it
  to the model as a directive to render in its own words (use it for a summary
  rather than a fixed sentence). `reads` names the diagnostic slots the condition
  looks at, so `verdict()` can declare the ones no spine task produces.
  `transfer_to` hands the caller off once the outcome is spoken.
  """

  condition: ConditionSpec
  say: str
  generative: bool = False
  reads: list[str] = field(default_factory=list)
  transfer_to: Optional[str] = None


def _absent(name: str, like: Optional[ConditionSpec]) -> ConditionSpec:
  """"`name` is not filled", in the same condition FORM as `like`.

  The two forms do not nest, so a leaf that is going to be ANDed with an authored
  condition has to match however that condition was written.
  """
  return {"slot": name, "filled": False} if isinstance(like, dict) else unset(name)


def verdict(
    config_id: str,
    *,
    spine: list[dict[str, Any]],
    branches: list[VerdictBranch],
    root_agent: str = "",
) -> Flow:
  """Build a verdict flow: a silent diagnostic spine, then a priority ladder.

  `spine` is the diagnostics, as `task(...)` dicts that take NO inputs — they fire
  with no user turn, so the flow drives itself from entry and the first thing the
  caller hears is the verdict. `branches` is the ordered ladder.

  Three correctness properties are DERIVED here rather than left to the author,
  because each one fails silently when hand-wired:

  * **Run once.** Each spine task gets a hidden `<task>_ran` output and is gated on
    it being unset. Gating on a diagnostic's own status output instead would
    re-fire forever whenever that status is legitimately empty (a healthy check).
    The flag is mapped off the task's `success_check` key rather than a synthetic
    one, because intake applies a task's outputs only when EVERY declared key is
    present in the tool's response — a made-up `<task>_ran` key no real tool
    returns would silently discard the diagnostic's real outputs along with it,
    leaving the whole ladder waiting on run-flags that never arrive.
  * **Arbitrate over the COMPLETE picture.** Every branch `requires` every spine
    run-flag. Without that the engine announces the first branch that happens to
    match once the FIRST diagnostic returns, speaking a verdict formed from half
    the evidence and skipping a higher-priority rule whose input had not landed.
  * **Halt at the first match.** The engine does not stop at the first eligible
    announce — it walks the cascade and speaks EVERY announce whose condition
    holds, so two matching rules arrive concatenated and contradictory ("Your line
    is down. Everything looks healthy."). Each branch is therefore additionally
    gated on every higher-priority branch being unfired.

  A spine task MAY carry its own `condition` (run this check only for a VIP, only
  on the mobile channel). Its output slots then carry that same gate, so a skipped
  diagnostic's run-flag is waived rather than waited on — without that the ladder
  hangs outright, since every rung requires every run-flag and the skipped task's
  never fills. A `requires` on a spine task is REFUSED: unlike a condition it can
  never be satisfied on the entry turn, so it is a hang with no waiver.

  A `reads` slot no spine task produces is declared as an event slot (filled from
  session state upstream) but is NOT required: a gate that can never fill would
  make its branch permanently unreachable, and a high-priority rule silently losing
  to a lower one is worse than evaluating it against an absent value, which simply
  reads False.

  A VERBATIM rung PREEMPTS; a `generative` one does not. The two are not a style
  choice here. The spine's raw diagnostics are in the model's context by the time
  the ladder resolves, so a non-preempting verdict is merely QUEUED for the next
  turn while the model, holding every check's output, narrates its own summary of
  them — and it recites all of them ("First, there's a hold. Second, you were
  $12.40 short."), which is the exact contradiction the halt gating just prevented,
  relocated from the config into the LLM. Preempting rides the rung inline and
  skips the model, so the one arbitrated outcome is what the caller hears, on the
  turn the diagnostics land. A `generative` rung cannot preempt — its `say` is a
  directive that only means anything if the model runs — so it accepts that
  exposure by construction; use it for a summary, not for arbitration.

  Deliberately NO welcome announce: a welcome preempts the entry turn, which
  suppresses the no-input spine and leaves the flow announcing and re-entering
  forever. The rungs are safe to preempt precisely because they cannot be eligible
  that early: every one of them requires the whole spine first. Flow-level gating
  is left to the caller — a verdict is usually a router's `default_flow` (see the
  silent-flow docs), since nothing can route into a flow with no setter.
  """
  if not spine:
    raise ValueError("verdict(): spine needs at least one diagnostic task")
  if not branches:
    raise ValueError("verdict(): branches needs at least one rule")

  f = Flow(config_id, root_agent=root_agent)
  produced: list[str] = []
  ran_flags: list[str] = []
  spine_tasks: list[dict[str, Any]] = []
  for t in spine:
    t = dict(t)  # copy — never mutate the caller's task dict
    name = t.get("name")
    if t.get("inputs"):
      raise ValueError(
          f"verdict(): spine task {name!r} declares inputs {t['inputs']!r} — a"
          " diagnostic must fire with no user turn, so it can take none")
    if t.get("terminal"):
      raise ValueError(
          f"verdict(): spine task {name!r} is terminal — the ladder speaks the"
          " outcome, so no diagnostic ends the flow")
    # A prerequisite here is not merely ignored, it is unsatisfiable — and it used to be
    # cleared silently a few lines down. The spine is a fan-out that fires on the entry
    # turn with nothing filled, no question is asked before the verdict, and a diagnostic
    # takes no inputs, so there is nothing an earlier one could hand a later one. A task
    # left waiting never reports, and every rung requires its run-flag, so one stray
    # `requires` wedges the entire ladder. Refusing it says the shape cannot express what
    # the author meant; dropping it pretended otherwise.
    if t.get("requires"):
      raise ValueError(
          f"verdict(): spine task {name!r} declares requires {t['requires']!r} — the"
          " spine is a no-prerequisite fan-out that fires on the entry turn with"
          " nothing filled yet, so the gate could never open and every rung, which"
          " waits on this diagnostic's run-flag, would hang with it")
    ran = f"{name}_ran"
    success_key = t.get("success_check", "success")
    outputs = dict(t.get("outputs") or {})
    if success_key in outputs:
      raise ValueError(
          f"verdict(): spine task {name!r} already maps its {success_key!r} key to"
          f" slot {outputs[success_key]!r} — that key carries the derived run-once"
          " flag, so map the diagnostic's real outputs off its other fields")
    outputs[success_key] = ran
    t["outputs"] = outputs
    t["requires"] = []
    gate_cond = t.get("condition")
    t["condition"] = _and_conditions(gate_cond, _absent(ran, gate_cond))
    for out in t["outputs"].values():
      if out not in produced:
        produced.append(out)
        slot = result_slot(out, name)
        # A CONDITIONAL diagnostic never reports when its gate is false, so its
        # run-flag never fills — and every rung `requires` every run-flag, so the
        # ladder would hang with no branch able to fire and no question left to ask
        # (verified end to end: the turn dead-ends at next_question). The engine
        # already has the escape: a `requires` entry is satisfied when the required
        # slot is filled OR its own condition is INACTIVE (`_compute_dag_state` /
        # `_find_next_slot_action`: `req in filled or not _is_slot_active(...)`). So
        # carry the diagnostic's gate onto the slots it produces and the same gate
        # that skips the task waives the wait for it. Note this is the AUTHOR's gate,
        # not the run-once one derived above — that one is "flag not yet filled",
        # which would waive the wait exactly when the flag arrives, i.e. backwards.
        if gate_cond is not None:
          slot["condition"] = gate_cond
        f.add(slot)
    ran_flags.append(ran)
    spine_tasks.append(t)

  declared = set(produced)
  for b in branches:
    for slot in b.reads:
      if slot not in declared:
        declared.add(slot)
        f.add(event_slot(slot))

  # Ladder slots go in priority order: the engine scans the slot list and returns
  # the FIRST eligible announce, so authored order is the tiebreak between two
  # branches that both match on the same turn.
  ladder = [f"{config_id}_branch_{i}" for i in range(len(branches))]
  for i, b in enumerate(branches):
    cond = b.condition
    # An announce fills its own slot when it speaks, so "every higher rung still
    # unfilled" is exactly "nothing above me won".
    for higher in ladder[:i]:
      cond = _and_conditions(cond, _absent(higher, b.condition))
    requires = list(dict.fromkeys(
        [*ran_flags, *(s for s in b.reads if s in produced)]))
    f.add(announce(
        ladder[i],
        [] if b.generative else [b.say],
        message=b.say if b.generative else None,
        requires=requires,
        condition=cond,
        # Verbatim -> ride inline and skip the model; generative -> the model IS
        # the renderer, so preempting would speak the directive at the caller.
        preempt=not b.generative,
        transfer_to=b.transfer_to,
    ))

  for t in spine_tasks:
    f.task(t)
  return f


# ---------------------------------------------------------------------------
# Author customization — the general "write your own logic" surface.
#
# CES exposes exactly four lifecycle callbacks per agent. `flows` emits the
# canonical slot-filling ones; `AgentHooks`/`steering` let an author inject their
# OWN Python alongside them (steering runs BEFORE the framework's before_agent to
# resolve the active config; the raw hooks run before/after their framework peer).
# Each is a plain function rendered self-contained at emit (inspect.getsource) with
# the same sandbox isolation rules as `@flows.tool` — typing/pydantic/stdlib only.
# ---------------------------------------------------------------------------


@dataclass
class AgentHooks:
  """Author lifecycle callbacks emitted around the framework's (escape hatch).

  Each is a function `fn(callback_context, ...)` matching its CES callback
  signature; `before_*` run before the framework callback, `after_*` after. Use
  for bespoke logic the declarative surface can't express (segment routing, CRM
  lookups, custom notices). Prefer `steering=` for the common turn-1 config case.
  """

  before_agent: Optional[Callable[..., Any]] = None
  before_model: Optional[Callable[..., Any]] = None
  after_model: Optional[Callable[..., Any]] = None
  after_tool: Optional[Callable[..., Any]] = None

  def any(self) -> bool:
    return any((self.before_agent, self.before_model, self.after_model, self.after_tool))


@dataclass
class Agent:
  """A named slot-filling sub-agent: one primary flow (+ optional extra DAGs).

  `name` is the CES agent display name; `flow.config_id` is its active DAG on
  transfer-in. `instruction` overrides the generated default. `steering`/`hooks`
  attach author customization to THIS agent (see `AgentHooks`).

  `extra_tools` scopes ADDITIONAL model-callable tools onto THIS specialist, beyond the
  ones its flow references — the per-agent mirror of `HostRouter.extra_tools`. It is how
  a caller reaches something mid-journey that the journey itself never fires: an FAQ
  knowledge tool they can ask at any point, say. Scoping is per-agent for the same
  reason it is everywhere else in this framework — a tool listed on an agent that has no
  business calling it is a tool the model will eventually call — so put it on the two
  specialists that offer it, not on all of them and not on the router.

  Each name must resolve to a real tool: a framework tool, a declared remote agent, or
  one with a body (`@flows.tool`, `App.tool_bodies`). The body is emitted whatever flow
  it is attached to; a name that resolves to nothing is a build error, because it would
  only surface as a failed tool call at run time.
  """

  name: str
  flow: Flow
  instruction: Optional[str] = None
  # A by-MEANING routing description of what this specialist is FOR (e.g. "the caller
  # wants to book or change a reservation"). When every route on a `HostRouter` has one,
  # the host + sibling-switching instructions are generated from these descriptions via
  # the shared `<routing>` generator (the same one `router_flow([route(...)])` uses),
  # instead of the config_id-derived fallback. Keep it free of verbatim caller phrases.
  description: Optional[str] = None
  extra_flows: list[Flow] = field(default_factory=list)
  # Spoken phrasings that mean "I want THIS agent" — used as route_cues so the
  # engine can detect a mid-call switch to this specialist WITHOUT a switch lead-in
  # ("takeout order", "take out", "to go"), which is what real (noisy) voice needs.
  # The flow key is included automatically; add domain synonyms here.
  aliases: list[str] = field(default_factory=list)
  steering: Optional[Callable[..., Any]] = None
  hooks: Optional[AgentHooks] = None
  # Remote A2A agents scoped onto THIS sub-agent (see `App.remote_agents`, which
  # scopes onto the host). Declare them here to keep a specialist's remote agent off
  # the router and its siblings — tool scoping is per-agent for the same reason it is
  # everywhere else in this framework.
  remote_agents: list[Any] = field(default_factory=list)
  # Google Search tools scoped onto THIS sub-agent (see `App.search_tools`). This is the
  # ONLY gate there is: search visibility cannot be narrowed per turn, because
  # `hide_tool()` breaks the turn for a managed tool (ces-probes 26). A specialist that
  # should answer open questions gets one here; its siblings then cannot search at all.
  search_tools: list[Any] = field(default_factory=list)
  # Extra model-callable tools scoped onto THIS sub-agent (the mirror of
  # `HostRouter.extra_tools`; see the class docstring).
  extra_tools: list[str] = field(default_factory=list)
  # OpenAPI toolsets this sub-agent needs (see `App.toolsets`). Unlike remote agents
  # there is nothing per-agent to scope — a toolset is app-wide and only its `api_tool`
  # wrappers are scoped — so declaring one here is a convenience for keeping a
  # specialist's dependency next to the specialist. All of them are emitted once.
  toolsets: list[Any] = field(default_factory=list)
  # Guardrails scoped to THIS sub-agent only. The CES agent resource carries its own
  # `guardrails` array, so a specialist can run a rule its siblings do not — the host
  # router has no business being judged against a payments policy. Resources are
  # emitted once app-wide and referenced by name from each agent that names them, so
  # the same guardrail can sit on `App` and on an `Agent` without being duplicated.
  guardrails: Optional[list[Any]] = None

  def __post_init__(self) -> None:
    _check_guardrail_entries(self.guardrails, f"Agent({self.name!r}).guardrails")

  @property
  def config_id(self) -> str:
    return self.flow.config_id

  @property
  def all_flows(self) -> list[Flow]:
    """This agent's flows (primary first) — the DAGs it references."""
    return [self.flow, *self.extra_flows]


@dataclass
class HostRouter:
  """A steering agent that routes callers to sub-agents.

  `routes` maps a flow key (the value `set_active_flow` receives) -> the target
  `Agent`. Two shapes via `strategy`:

  * ``"transfer"`` (default, "receptionist"): a non-slot-filling router with custom
    before_model/after_tool callbacks + `childAgents`; the model silently calls
    `set_active_flow` and the framework transfers to the specialist. Each specialist
    is a full agent with its own instruction + scoped tools, and can hand off to a
    sibling mid-call.
  * ``"engine"`` ("config-swap"): one agent runs the engine with a synthesized
    router DAG and switches configs by intent (mirrors the migration host router).

  `entry_var` (e.g. ``"ENTRY_INTENT"``) is woven into the host instruction so an
  upstream intent tag routes silently. `steering`/`hooks` attach customization.

  `robust_switching` (default True) makes each specialist recognize a mid-call switch
  to another sub-agent by MEANING ("actually, I'd rather order takeout") rather than
  by fixed keywords, so callers can move between sub-agents naturally. It adds one
  brief classification step per turn inside a specialist; set it False for the lowest
  latency (switching then relies on the caller using a sub-agent's name or `aliases`).

  `welcome_message` sets the caller-facing greeting the host opens with (woven into
  the generated host instruction, so it applies to both strategies). When unset the
  host uses a generic greeting; ignored if you supply your own `instruction`.

  `route_cues` maps a flow key -> its spoken cue phrases (the phrasings that mean "I
  want THIS route"). It is threaded VERBATIM and ORDER-PRESERVING by build.py and takes
  PRECEDENCE over alias-derived cues. Runtime matching is earliest-utterance-position
  wins; the dict/list order is only the same-offset tiebreak, so it is NEVER sorted —
  authored order is the tiebreak contract. When unset, cues derive from routes' aliases.

  `extra_tools` scopes ADDITIONAL model-callable tools onto the host itself, beyond the
  routing pair it always carries. The host is the agent that talks to the caller before
  (and between) transfers, so it is the one that answers the FAQ question or records the
  classification the specialists read back — and neither of those is a flow's tool, so
  nothing else can scope them. Each name must resolve to a real tool: a framework tool,
  a declared remote agent, or one with a body (`@flows.tool`, `App.tool_bodies`). The
  body is emitted whatever flow it is attached to; a name that resolves to nothing is a
  build error, because it would only surface as a failed tool call at run time.
  `App.extra_agent_tools` is unioned in here as well (app-level extras are the router's,
  exactly like `App.remote_agents`).
  """

  name: str
  routes: dict[str, Agent]
  instruction: Optional[str] = None
  strategy: Literal["transfer", "engine"] = "transfer"
  entry_var: Optional[str] = None
  robust_switching: bool = True
  welcome_message: Optional[str] = None
  route_cues: Optional[dict[str, list[str]]] = None
  steering: Optional[Callable[..., Any]] = None
  hooks: Optional[AgentHooks] = None
  extra_tools: list[str] = field(default_factory=list)
  # Guardrails scoped to the HOST agent only — the mirror of `Agent.guardrails`. Use it
  # for a rule that belongs to routing rather than to any specialist; anything that
  # should hold everywhere belongs on `App.guardrails` instead.
  guardrails: Optional[list[Any]] = None

  def __post_init__(self) -> None:
    if self.strategy not in ("transfer", "engine"):
      raise ValueError(
          f"HostRouter.strategy must be 'transfer' or 'engine', got {self.strategy!r}"
      )
    if not self.routes:
      raise ValueError("HostRouter requires at least one route")
    _check_guardrail_entries(self.guardrails, f"HostRouter({self.name!r}).guardrails")


def _check_guardrail_entries(entries: Any, where: str) -> None:
  """A guardrails list holds display-name strings, `flows.safety(...)`-style resources,
  or a mix.

  A bare string references a resource the TARGET is expected to already have (the only
  thing this field could mean before the emitter could produce one); a `Guardrail`
  carries the resource with it. Mixing is how an app half-migrated from the console
  stays expressible.
  """
  if entries is None:
    return
  if isinstance(entries, str) or not isinstance(entries, (list, tuple)):
    raise ValueError(
        f"{where} must be a list of guardrail names or flows.safety(...)-style "
        f"resources; use [] to declare that it runs with none (got {entries!r})")
  for entry in entries:
    if isinstance(entry, _guardrails.Guardrail):
      continue
    if not isinstance(entry, str) or not entry.strip():
      raise ValueError(
          f"{where} entries must be a non-empty resource name or a guardrail built "
          f"with flows.safety/blocklist/policy/prompt_guard, got {entry!r}")


# app.json TOP-LEVEL keys the emitter already owns -> the `App` field that sets each.
# `app_settings` rejects them by name rather than accepting a write the post-emit steps
# would silently overwrite (or, worse, that would overwrite the emitter's own).
_OWNED_APP_SETTINGS = {
    "name": "app_uuid=",
    "displayName": "app_display_name=",
    "rootAgent": "root_flow= / host=",
    "variableDeclarations": "variables=",
    "modelSettings": "model=",
    "languageSettings": "languages= / default_language= / language_switching=",
    "timeZoneSettings": "time_zone=",
    "guardrails": "guardrails=",
}


@lru_cache(maxsize=1)
def _tz_names() -> frozenset:
  """Every IANA zone name this machine knows, or empty when it knows none.

  `available_timezones()` rather than `ZoneInfo(name)`: the lookup goes through the
  filesystem, and on a case-INSENSITIVE one (macOS, by default) `America/New_york`
  loads happily — which is exactly the typo worth catching. Cached because the scan
  walks the whole zoneinfo tree (~25ms) and an `App` is cheap to construct.
  """
  try:
    from zoneinfo import available_timezones
  except ImportError:  # pragma: no cover - py<3.9
    return frozenset()
  try:
    return frozenset(available_timezones())
  except Exception:  # pragma: no cover - no tz database installed
    return frozenset()


def _known_time_zone(name: str) -> Optional[bool]:
  """Is `name` a zone in the IANA database? `None` when there is no database to ask.

  A mistyped zone is exactly as silent as an undeclared one — it raises nothing and
  shifts every derived date — so it is worth one lookup at authoring time. Systems
  with no tz database (a bare Windows interpreter without `tzdata`) answer `None`
  and are not failed for it.
  """
  names = _tz_names()
  if not names:
    return None
  return name in names


@dataclass
class App:
  """App-level settings + the flows to emit (the emission unit for build).

  Single-agent: pass `root_flow` (its `config_id`/`root_agent` seed the app) plus
  optional `extra_flows` (additional DAGs on the one agent). Multi-agent: pass a
  `host` (HostRouter) + `agents` (the sub-agents). `variables` are custom business
  `variableDeclarations` (the framework state vars are auto-added at emit).
  `steering`/`hooks` attach author customization to the (single) root agent.
  """

  root_flow: Optional[Flow] = None
  app_display_name: str = ""
  gcp_project: str = "ces-deployment-dev"
  location: str = "us"
  model: str = "gemini-3.1-flash-live"
  extra_flows: list[Flow] = field(default_factory=list)
  variables: list[dict[str, Any]] = field(default_factory=list)
  agent_instruction: Optional[str] = None
  global_instruction: Optional[str] = None
  app_uuid: Optional[str] = None
  agent_uuid: Optional[str] = None
  # Hand-authored tool bodies {tool_name: python_source} — an escape hatch for tools
  # written as raw source (e.g. mock/real dispatch) instead of @flows.tool. Merged
  # with (and overridden by) the @flows.tool registry at build.
  tool_bodies: dict[str, str] = field(default_factory=dict)
  # Tool names to emit with `executionType: ASYNCHRONOUS`. The escape hatch matching
  # `tool_bodies` — `@flows.tool(asynchronous=True)` is the route for decorated tools,
  # and the two are unioned at build.
  async_tools: set[str] = field(default_factory=set)
  # Seconds per tool, emitted as `timeout: "<n>s"`. The escape hatch matching
  # `tool_bodies`, exactly as `async_tools` is for `asynchronous`: `@flows.tool(
  # timeout=...)` is the route for a DECORATED tool, and a raw-source body had no route
  # at all. CES kills a tool at 60 seconds unless the resource says otherwise, and on an
  # ASYNCHRONOUS body that failure is quiet — it simply never reports, so the wait runs
  # to `awaits.max_turns` and the caller hears a timeout line for a backend that was
  # merely slow. An author who writes the body as source is exactly the author most
  # likely to be wrapping something slow.
  tool_timeouts: dict[str, int] = field(default_factory=dict)
  # Classifying setters {setter_name: (mapping, default)} — free-form speech mapped
  # to a canonical menu key. `default=None` re-prompts on no match.
  classifiers: dict[str, Any] = field(default_factory=dict)
  # Extra model-callable tools to scope into the agent beyond flow-referenced ones.
  # Multi-agent: they scope onto the HOST — it is the agent that talks to the caller
  # between transfers — exactly like `remote_agents` below. Use `HostRouter.extra_tools`
  # to say that explicitly, or `Agent.remote_agents`-style per-specialist scoping to
  # keep a tool off the router; the two host sources are unioned.
  extra_agent_tools: list[str] = field(default_factory=list)
  # Remote A2A agents (see flows.authoring.a2a). Each is emitted as one body-less
  # `remoteAgentTool` resource and scoped onto the agent, so the model can call it —
  # reference it from the instruction as `{@TOOL: name}`. A remote agent named by a
  # task's `tool` is wired by the ordinary task path as well.
  remote_agents: list[Any] = field(default_factory=list)
  # Google Search grounding tools (see flows.authoring.search). Each is emitted as one
  # body-less `googleSearchTool` resource and scoped onto the agent, so the model can
  # search on its own initiative and summarise what it finds. A search tool named by a
  # task's `tool` — what `research()` builds — is wired by the ordinary task path too.
  # Scoping is per-agent and cannot be narrowed per turn (ces-probes 26): to keep search
  # away from a collection flow, give it to a different `Agent`, not to the `App`.
  search_tools: list[Any] = field(default_factory=list)
  # Agents callable as tools (see flows.authoring.agent_tool). Each is emitted as one
  # body-less `agentTool` resource naming an agent in THIS app — unless it was declared
  # `emit=False`, which says the app already carries the resource and this is only the
  # declaration that teaches tasks its contract. A tool handed straight to `task()` is
  # picked up by the ordinary task path and need not be listed here.
  agent_tools: list[Any] = field(default_factory=list)
  # OpenAPI toolsets (see flows.authoring.openapi). Each is emitted as one
  # `openApiToolset` resource under `toolsets/` — a different resource kind from a
  # tool, and one no agent can call directly. What the flow fires is the generated
  # `flows.api_tool(...)` wrapper, scoped by the ordinary task path.
  toolsets: list[Any] = field(default_factory=list)
  # Default for the `mock_apis` variable: when true, every `flows.api_tool` that
  # declared a mock answers from it instead of calling the API. Only the DEFAULT — the
  # flag is a session variable, so a deployed app can be flipped either way without a
  # rebuild, and a per-tool `mock_<tool>` payload overrides it for one call.
  mock_apis: bool = False
  # Build-time automatic fillers (see authoring/autofill.py). Off unless asked for: `True`
  # runs the pass with the framework acknowledgement phrases, and a dict turns it on
  # while widening them — `{"extra_ack": ["righto"]}`. The pass hoists a
  # contentless opener off `then_say`/`ask` into `filler_say`, so the wait is covered
  # by copy the author already wrote. Individual nodes opt out with
  # `task(automatic_fillers=False)` / `user_slot(automatic_fillers=False)`.
  automatic_fillers: Union[bool, dict[str, Any]] = False
  # --- multi-agent (host + sub-agents). Mutually exclusive with root_flow. ---
  host: Optional[HostRouter] = None
  agents: list[Agent] = field(default_factory=list)
  # --- author customization for the single-agent root (see AgentHooks). ---
  steering: Optional[Callable[..., Any]] = None
  hooks: Optional[AgentHooks] = None
  # --- language (see flows.authoring.language). `languages` are the supported
  # BCP-47 locales emitted into app.json languageSettings; `default_language`
  # (defaults to languages[0]) is the one the session opens in. `language_switching`
  # off = single-language app (just sets languageSettings); "explicit" | "auto"
  # emit the `update_language` tool + a `<language_detection>` block so a caller can
  # switch mid-call and stay there (explicit-only is the production default: the live
  # model does not reliably auto-detect switches); "select" offers a turn-1 language
  # menu (`language_prompt` + DTMF) and then HARD-LOCKS the chosen language for the
  # rest of the call (a `<language_lock>` block + a progressive-nudge hook). ---
  languages: list[str] = field(default_factory=list)
  default_language: Optional[str] = None
  language_switching: Literal["off", "explicit", "auto", "select"] = "off"
  # "select" mode only: the bilingual turn-1 menu the caller hears (voice copy, no
  # dashes). Defaults to a generated menu when unset.
  language_prompt: Optional[str] = None
  # --- delivery surfaces (see flows.surfaces). One agent, many surfaces: each
  # declares what it CAN DO (render structure, carry a link, offer N choices) and
  # authored content is projected onto it. Leave empty and the built-in `voice` and
  # `chat` surfaces apply, which is right for almost every app — declare surfaces
  # only to add one (SMS, an in-car head unit) or to retune a built-in.
  # `default_surface` is used when the inbound channel is absent or unrecognized;
  # unset, it falls back to voice, because a voice failure is unrecoverable and a
  # chat failure is merely plain. ---
  surfaces: list[Any] = field(default_factory=list)
  default_surface: Optional[str] = None
  # --- variable maps (see flows.authoring.variable_maps). Each names ONE WAY a
  # conversation can start — "arrive with these session variables and here is what we
  # already know" — and the slots that knowledge fills. The framework seeds them before
  # any author `before_agent` hook runs, so a session that was handed an account number
  # is not asked for one. Ordered: the first map whose every binding resolves wins, so
  # the most specific belongs first. App-level because session variables are app-wide;
  # projected into each flow's config at build, keeping only the slots that flow holds.
  variable_maps: list[Any] = field(default_factory=list)
  # --- app-LEVEL CES settings: app.json's TOP LEVEL, alongside modelSettings. ---
  #
  # Leave them unset and `flows` has no opinion: a freshly created CES app comes up on
  # the PLATFORM's defaults, and `flows deploy` back-fills whatever the live target
  # happens to carry (deploy/prep.PRESERVE). That is how a migrated app came up on
  # `America/Los_Angeles` against a source that ran `America/New_York`, while four of
  # its tools computed temporary-lift windows off `current_date` — a silently wrong
  # date range on a caller's credit file, with nothing in the source to review. Declare
  # them and they are EMITTED into app.json, and a deploy keeps YOUR value.
  #
  # `time_zone` is the IANA zone name (`"America/New_York"`) that `current_date` and
  # every date derived from it resolve against.
  #
  # `guardrails` names the app's guardrail resources (`["Default Safety Guardrail",
  # "Default Prompt Guardrail"]`). `[]` is a DECLARATION that the app runs with none;
  # `None` (the default) means "not mine — take the target's".
  #
  # `app_settings` is the raw passthrough for the long tail (`loggingSettings`,
  # `toolExecutionMode`, `dataStoreSettings`, `errorHandlingSettings`, ...): each key is
  # written VERBATIM to app.json's top level. Keys the emitter owns are rejected with a
  # pointer to the field that sets them (see `_OWNED_APP_SETTINGS`), so there is exactly
  # one way to say each thing.
  time_zone: Optional[str] = None
  # App-wide guardrails: display-name strings (a resource the target already has),
  # `flows.safety(...)`-style resources the emitter writes, or a mix. `[]` means "this
  # app runs with none", which is different from `None` (say nothing, keep the
  # target's).
  guardrails: Optional[list[Any]] = None
  # Ask the platform to REPORT interruptions, and to quote what the caller actually heard.
  # On by default because the alternative is losing the report, not avoiding the cut: the
  # platform truncates the agent's speech whether or not this is set. Turn it off only for
  # a surface where nobody can talk over the agent at all. See `declared_settings`.
  barge_in_awareness: bool = True
  app_settings: dict[str, Any] = field(default_factory=dict)

  def __post_init__(self) -> None:
    # A timeout is emitted as the STRING `"<n>s"`, so a wrong type does not fail here —
    # it deploys as `"-5s"` or `"True s"` and the tool quietly keeps the default. Catch
    # it where the author wrote it. `bool` is excluded explicitly because it is an int
    # in Python and `timeout=True` would otherwise emit a one-second budget.
    for _name, _secs in (self.tool_timeouts or {}).items():
      if not isinstance(_name, str) or not _name:
        raise ValueError(
            f"App.tool_timeouts keys are tool names; got {_name!r}")
      if isinstance(_secs, bool) or not isinstance(_secs, int) or _secs <= 0:
        raise ValueError(
            f"App.tool_timeouts[{_name!r}] is seconds as a positive whole number; got "
            f"{_secs!r}")
    # `agents` carries two unrelated things. A HelperAgent is a plain agent an
    # `agent_tool` calls, and it is perfectly at home in a single-agent app — that is
    # the whole point of it, since the multi-agent form needs a Flow per agent. A
    # sub-agent (`flows.Agent`) is the multi-agent form. Split them before deciding
    # which shape this app is.
    from . import agent_tool as _at  # noqa: PLC0415 (import cycle at module scope)
    self.helper_agents = [a for a in self.agents if isinstance(a, _at.HelperAgent)]
    self.agents = [a for a in self.agents if not isinstance(a, _at.HelperAgent)]
    multi = self.host is not None or bool(self.agents)
    single = self.root_flow is not None
    if multi and single:
      raise ValueError(
          "App: provide either root_flow (single-agent) OR host+agents "
          "(multi-agent), not both"
      )
    if multi:
      if self.host is None or not self.agents:
        raise ValueError("App: multi-agent apps require both host= and agents=[...]")
    elif not single:
      raise ValueError(
          "App: provide root_flow (single-agent) or host+agents (multi-agent)"
      )
    if not self.app_display_name:
      raise ValueError("App: app_display_name is required")
    self._validate_languages()
    self._validate_app_settings()

  def _validate_app_settings(self) -> None:
    """App-level CES settings invariants (see field docs)."""
    if self.time_zone is not None:
      if not isinstance(self.time_zone, str) or not self.time_zone.strip():
        raise ValueError(
            "App.time_zone must be a non-empty IANA zone name, e.g. 'America/New_York'"
        )
      if _known_time_zone(self.time_zone) is False:
        raise ValueError(
            f"App.time_zone {self.time_zone!r} is not a zone in the IANA tz database "
            "— it is case- and spelling-sensitive ('America/New_York', not "
            "'America/New_york'), and a wrong zone shifts every date the agent derives"
        )
    _check_guardrail_entries(self.guardrails, "App.guardrails")
    for key in self.app_settings or {}:
      if not isinstance(key, str) or not key:
        raise ValueError(
            f"App.app_settings keys must be app.json top-level names, got {key!r}")
      if key in _OWNED_APP_SETTINGS:
        raise ValueError(
            f"App.app_settings[{key!r}] is emitted by flows — set it with "
            f"`{_OWNED_APP_SETTINGS[key]}` instead, so one field owns it and "
            "`flows deploy` knows it is yours"
        )

  def _validate_languages(self) -> None:
    """Language config invariants (see field docs)."""
    if self.language_switching not in ("off", "explicit", "auto", "select"):
      raise ValueError(
          "App.language_switching must be 'off', 'explicit', 'auto', or 'select', "
          f"got {self.language_switching!r}"
      )
    if self.default_language and self.default_language not in self.languages:
      raise ValueError(
          f"App.default_language {self.default_language!r} is not in languages="
          f"{self.languages}"
      )
    if self.language_switching != "off" and len(self.languages) < 2:
      raise ValueError(
          "App.language_switching requires at least 2 languages= to switch between "
          f"(got {self.languages})"
      )

  @property
  def resolved_default_language(self) -> Optional[str]:
    """The session's opening locale: `default_language` or the first `languages`."""
    return self.default_language or (self.languages[0] if self.languages else None)

  @property
  def declared_settings(self) -> dict[str, Any]:
    """The app-LEVEL app.json settings this App declares, in emit order.

    `{app.json top-level key: value}` — what the post-emit step writes and what the
    integrity check holds the emitted app.json to.
    """
    out: dict[str, Any] = {}
    if self.time_zone:
      out["timeZoneSettings"] = {"timeZone": self.time_zone}
    if self.guardrails is not None:
      # app.json carries NAMES; the resources travel separately as `guardrails/` dirs.
      out["guardrails"] = [_guardrails.display_name(g) for g in self.guardrails]
    # Barge-in awareness, ON unless the author turns it off. This does NOT decide whether
    # the caller can interrupt -- `disableBargeIn` does, and the platform cuts the agent
    # off either way. It decides whether the agent is TOLD, and told what the caller
    # actually heard (ces-probes 161 for the report, 162 for the gating). Leaving it unset
    # is the dangerous default: the line is cut and the model context still asserts the
    # whole thing was delivered.
    #
    # Written BEFORE the app_settings loop and merged key-wise below, so an author who
    # hand-writes `app_settings={"audioProcessingConfig": {"inactivityTimeout": "8s"}}`
    # keeps their setting and still gets the flag.
    if self.barge_in_awareness:
      out["audioProcessingConfig"] = {"bargeInConfig": {"bargeInAwareness": True}}
    for key, value in (self.app_settings or {}).items():
      if key == "audioProcessingConfig" and isinstance(value, dict) and key in out:
        merged = dict(out[key])
        for sub_key, sub_value in value.items():
          if (sub_key == "bargeInConfig" and isinstance(sub_value, dict)
              and isinstance(merged.get(sub_key), dict)):
            merged[sub_key] = {**merged[sub_key], **sub_value}
          else:
            merged[sub_key] = sub_value
        out[key] = merged
        continue
      out[key] = value
    return out

  @property
  def declared_setting_keys(self) -> list[str]:
    """Every app.json top-level setting this App OWNS.

    `declared_settings` plus `languageSettings` when `languages` is set (that one is
    written by the language step, not by the app-settings step, but it is just as
    author-declared). This is the list a deploy will NOT overwrite from the live
    target; emit records it in `declared-settings.json` for `flows deploy` to read.
    """
    keys = list(self.declared_settings)
    if self.languages and "languageSettings" not in keys:
      keys.append("languageSettings")
    return keys

  @property
  def is_multi_agent(self) -> bool:
    return self.host is not None

  @property
  def config_id(self) -> str:
    if self.is_multi_agent:
      raise AttributeError(
          "multi-agent App has no single config_id — use each Agent.config_id"
      )
    return self.root_flow.config_id

  @property
  def root_agent(self) -> str:
    if self.is_multi_agent:
      return self.host.name
    return self.root_flow.root_agent or f"{self.config_id}_agent"
