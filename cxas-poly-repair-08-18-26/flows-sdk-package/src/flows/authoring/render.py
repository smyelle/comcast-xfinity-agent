"""Deterministic Config -> Flows-DSL source renderer (the migration deliverable).

`render_config_source(config, config_id=..., root_agent=...)` turns a plain `Config`
dict (the shape `Flow.to_config()` produces and the engine consumes) back into an
importable, HUMAN-READABLE `flows` authoring module. The rendered module binds a
module-level `flow` to a `flows.Flow`, so the ROUND-TRIP CONTRACT holds:

    exec(source)                 # runs the builders
    namespace["flow"].to_config() == config      # byte-for-byte, order preserved

This is the second migration backend's authoring layer: instead of an opaque config
blob, each migrated agent gets a checked-in `.py` a human can read and edit.

BUILDER-MATCH-OR-RAW: every slot/task is emitted with a HIGH-LEVEL builder
(`user_slot`/`announce`/`event_slot`/`result_slot`/`task`/`component`) ONLY when that
builder reproduces the target dict byte-for-byte *including key order* (verified by
actually calling the real builder and comparing `list(d.items())`). Anything a builder
cannot reproduce exactly is emitted as `raw({...})` — a clean dict literal — so nothing
is ever silently downgraded. Flow-level policy keys become `.set(key, value)` calls.

DETERMINISM: no uuid/time/randomness; source order of slots/tasks/keys is preserved
(never sorted); string/list/dict literals use a stable formatter. Two calls with the
same config return identical bytes.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from .dsl import (
    announce,
    component,
    eq,
    event_slot,
    has,
    intent_slot,
    ne,
    passive_slot,
    result_slot,
    task,
    unset,
    user_slot,
)
from .handoff import (
    CXAS_KEY,
    DIALOGFLOW_KEY,
    HANDOFF_REASON,
    UJET_ESCALATION_ACTION,
    UJET_KEY,
    cxas,
    dialogflow_cx,
    handoff,
    ujet,
)

# task() gains transfer_to / on_complete / readback_inputs kwargs in a concurrent
# dsl.py change; detect what the installed builder actually accepts so the renderer
# uses those kwargs when present and falls back to raw({...}) when they are not.
import inspect as _inspect

_TASK_PARAMS = set(_inspect.signature(task).parameters)

_WIDTH = 88


# ---------------------------------------------------------------------------
# Nested builder calls as argument values.
#
# Every kwarg the renderer emitted used to be a LITERAL, so a slot whose shape is
# only expressible through a second builder — `announce(..., handoff=flows.handoff(
# flows.ujet(menu_id="90")))` — had nowhere to go and fell back to `raw({...})`.
#
# `_Expr` carries both halves of what such a value needs: `value`, the object the
# real builder returns (so the byte-for-byte verification can call `announce()`
# for real), and enough structure to RENDER the call the same way `_Renderer`
# renders a top-level one. Nothing about the builder-match-or-raw contract
# changes: the candidate is still only adopted when `announce(...)` reproduces the
# target dict exactly, key order included, and `_expr()` returns None the moment a
# nested builder refuses its arguments — which drops the slot to `raw({...})` just
# as an unreproducible shape always has.
# ---------------------------------------------------------------------------

_EXPR_BUILDERS = {
    "handoff": handoff,
    "ujet": ujet,
    "dialogflow_cx": dialogflow_cx,
    "cxas": cxas,
}


@dataclass(frozen=True)
class _Expr:
  """A builder CALL used as an argument value: its source and its real object."""

  name: str
  args: tuple
  kwargs: tuple  # ((key, value), ...) in the order they render
  value: Any  # what the builder returns — for the byte-for-byte check
  used: frozenset  # builder names the emitted module has to import

  def inline(self) -> str:
    parts = [_inline(a) for a in self.args]
    parts += [f"{k}={_inline(v)}" for k, v in self.kwargs]
    return f"{self.name}(" + ", ".join(parts) + ")"

  def render(self, indent: int = 0, prefix: int = 0) -> str:
    """Inline when it fits, else one argument per line — same rule as `_Renderer`."""
    inline = self.inline()
    if len(inline) + indent + prefix <= _WIDTH:
      return inline
    child = indent + 4
    pad = " " * child
    close = " " * indent
    lines = [_lit(a, child) for a in self.args]
    lines += [f"{k}={_lit(v, child, prefix=len(k) + 1)}" for k, v in self.kwargs]
    if len(lines) == 1:  # no magic trailing comma on a single element
      return f"{self.name}(\n{pad}{lines[0]}\n{close})"
    return (f"{self.name}(\n" + "".join(f"{pad}{ln},\n" for ln in lines)
            + close + ")")


def _expr(name: str, *args: Any, **kwargs: Any) -> Optional[_Expr]:
  """Build one `_Expr`, or None if the builder rejects these arguments."""
  used = {name}
  real_args = []
  for a in args:
    used |= a.used if isinstance(a, _Expr) else frozenset()
    real_args.append(a.value if isinstance(a, _Expr) else a)
  real_kwargs = {}
  for k, v in kwargs.items():
    used |= v.used if isinstance(v, _Expr) else frozenset()
    real_kwargs[k] = v.value if isinstance(v, _Expr) else v
  builder = _EXPR_BUILDERS[name]  # outside the try: an unknown name is OUR bug
  try:
    value = builder(*real_args, **real_kwargs)
  except Exception:  # noqa: BLE001 — a refusal is a refusal, see _build
    return None
  return _Expr(name, tuple(args), tuple(kwargs.items()), value, frozenset(used))


def _unwrap(v: Any) -> Any:
  """The value a builder is actually called with (an `_Expr` stands for its object)."""
  return v.value if isinstance(v, _Expr) else v


def _candidate(reverser, d: dict[str, Any]) -> Optional[tuple[str, list, dict]]:
  """Run a candidate REVERSER on `d`, or None if it cannot read this dict.

  The same rule as `_build`, one step earlier. A reverser reads the keys a
  well-formed config carries and a MINED one need not: an `on_exhaust` that is the
  string `"escalate"` rather than the `{"say": ...}` dict makes
  `_candidate_user_slot` do `"escalate".get("say")` and raise `AttributeError`. That
  slot was headed for `raw({...})` regardless — a shape no builder reproduces — so
  losing the whole render over it is the one outcome the fallback exists to prevent.
  """
  try:
    return reverser(d)
  except Exception:  # noqa: BLE001 — unreadable dict == no candidate == raw({...})
    return None


def _build(name: str, args: list[Any], kwargs: dict[str, Any]) -> Optional[dict]:
  """Run the real builder for a candidate, or None if it REFUSES the arguments.

  A candidate is a guess at how a dict was built and the byte-for-byte comparison
  is what confirms it, so a builder that raises has simply rejected the guess —
  and the answer is the same one a mismatch gets: `raw({...})`. This used to
  propagate: a mined `{"slot": x, "neq": y, "default": ""}` condition (dead on a
  non-numeric comparison, and the DSL says so) took the WHOLE render down over one
  slot that was always going to fall back anyway.

  The refusal is not always a deliberate `raise`. A builder that INDEXES an argument
  it was promised is a list refuses a mined dict with a `KeyError`
  (`user_slot(reprompts={"0": "..."})` -> `reprompts[0]`), and a `TypeError`/
  `ValueError`-only guard let that one through — the same dead render, from the same
  cause. So every exception the builder raises means the same thing here. Nothing is
  silently downgraded by widening it: the byte-for-byte gate still has to pass for a
  builder form to be emitted at all, so the worst case remains `raw({...})`. The
  registry LOOKUP stays outside the guard, because an unknown builder name is a bug
  in this module rather than a config the builder declined.
  """
  builder = _BUILDERS[name]
  try:
    return builder(*[_unwrap(a) for a in args],
                   **{k: _unwrap(v) for k, v in kwargs.items()})
  except Exception:  # noqa: BLE001 — see above: any raise is a refusal
    return None


# ---------------------------------------------------------------------------
# raw — the identity marker so raw-dict fallbacks are greppable + round-trip.
# ---------------------------------------------------------------------------


def raw(d: dict) -> dict:
  """Identity passthrough for a config sub-dict a high-level builder can't reproduce.

  Rendered as `raw({...})` in emitted source and re-imported from `flows`, so a
  fallback to a raw dict literal is always greppable and round-trips exactly (the
  dict is appended to the Flow unchanged).
  """
  return d


# ---------------------------------------------------------------------------
# Literal formatting — deterministic, order-preserving, double-quoted strings.
# ---------------------------------------------------------------------------


def _str_lit(s: str) -> str:
  # Match ruff/pyink quote normalization: prefer double quotes, but switch to single
  # quotes when the body contains a double quote and NO single quote (so switching
  # avoids an escape). Deterministic; keeps the emitted source byte-identical to
  # `ruff format` without shelling out to it.
  if '"' in s and "'" not in s:
    body = (
        s.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return "'" + body + "'"
  # json.dumps yields a double-quoted, fully-escaped literal (\n, \t, \", \\, \uXXXX).
  return json.dumps(s, ensure_ascii=False)


def _inline(v: Any) -> str:
  """Single-line literal for `v` (deterministic; preserves dict/list order)."""
  if isinstance(v, _Expr):  # a nested builder CALL, not a literal
    return v.inline()
  if v is None:
    return "None"
  if v is True:
    return "True"
  if v is False:
    return "False"
  if isinstance(v, (int, float)):
    return repr(v)
  if isinstance(v, str):
    return _str_lit(v)
  if isinstance(v, (list, tuple)):
    return "[" + ", ".join(_inline(x) for x in v) + "]"
  if isinstance(v, dict):
    return (
        "{"
        + ", ".join(f"{_str_lit(str(k))}: {_inline(val)}" for k, val in v.items())
        + "}"
    )
  raise TypeError(f"cannot render literal for {type(v)!r}")


def _lit(v: Any, indent: int, prefix: int = 0) -> str:
  """Pretty literal for `v`, wrapping to multiline when the inline form is too wide.

  `indent` is the column the CLOSING bracket aligns to; children sit at `indent + 4`.
  `prefix` is extra width consumed before `v` on its first line (e.g. a ``key=`` label),
  so the wrap decision matches ruff/pyink even though the close still aligns to `indent`.
  """
  if isinstance(v, _Expr):
    return v.render(indent, prefix)
  inline = _inline(v)
  if (len(inline) + indent + prefix <= _WIDTH
      or not isinstance(v, (list, tuple, dict)) or not v):
    return inline
  pad = " " * (indent + 4)
  close = " " * indent
  if isinstance(v, (list, tuple)):
    body = ",\n".join(pad + _lit(x, indent + 4) for x in v)
    return "[\n" + body + ",\n" + close + "]"
  body = ",\n".join(
      # charge the ``"key": `` label against the budget so a nested value wraps exactly
      # when ruff/pyink would (matches its width cascade at every depth).
      f"{pad}{_str_lit(str(k))}: "
      f"{_lit(val, indent + 4, prefix=len(_str_lit(str(k))) + 2)}"
      for k, val in v.items()
  )
  return "{\n" + body + ",\n" + close + "}"


# ---------------------------------------------------------------------------
# Condition helpers — emit has()/unset()/eq()/ne() when they reproduce the string.
# ---------------------------------------------------------------------------

_HAS_RE = re.compile(r"lambda f: bool\(f\.get\((.+)\)\)")
_UNSET_RE = re.compile(r"lambda f: not f\.get\((.+)\)")
_EQ_RE = re.compile(r"lambda f: f\.get\((.+)\) == (.+)")
_NE_RE = re.compile(r"lambda f: f\.get\((.+)\) != (.+)")


def _reverse_condition(s: str) -> Optional[tuple[str, list[Any]]]:
  """If `s` is a helper-produced lambda source, return (helper_name, args)."""
  m = _HAS_RE.fullmatch(s)
  if m:
    slot = _try_literal(m.group(1))
    if isinstance(slot, str) and has(slot) == s:
      return ("has", [slot])
  m = _UNSET_RE.fullmatch(s)
  if m:
    slot = _try_literal(m.group(1))
    if isinstance(slot, str) and unset(slot) == s:
      return ("unset", [slot])
  m = _EQ_RE.fullmatch(s)
  if m:
    slot = _try_literal(m.group(1))
    val = _try_literal(m.group(2))
    if isinstance(slot, str) and eq(slot, val) == s:
      return ("eq", [slot, val])
  m = _NE_RE.fullmatch(s)
  if m:
    slot = _try_literal(m.group(1))
    val = _try_literal(m.group(2))
    if isinstance(slot, str) and ne(slot, val) == s:
      return ("ne", [slot, val])
  return None


_SENTINEL = object()


def _try_literal(src: str) -> Any:
  try:
    return ast.literal_eval(src)
  except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
    return _SENTINEL


# ---------------------------------------------------------------------------
# The renderer — tracks which builders are used so the import line stays minimal.
# ---------------------------------------------------------------------------


class _Renderer:

  def __init__(self) -> None:
    self.used: set[str] = set()

  # --- call rendering -----------------------------------------------------

  def _render_condition(self, s: str) -> str:
    rev = _reverse_condition(s)
    if rev is not None:
      name, args = rev
      self.used.add(name)
      return f"{name}(" + ", ".join(_inline(a) for a in args) + ")"
    return _str_lit(s)

  def _kw_str(self, k: str, v: Any, *, inline: bool, indent: int = 0) -> str:
    """Render one `key=value` kwarg; condition kwargs re-emit has()/eq()/... helpers.

    When wrapping, the ``key=`` label width is charged against the line budget (via
    ``_lit(prefix=...)``) so a value wraps exactly when ruff/pyink would.
    """
    if k == "condition" and isinstance(v, str):
      return f"{k}={self._render_condition(v)}"
    self._note(v)
    if inline:
      return f"{k}={_inline(v)}"
    return f"{k}={_lit(v, indent, prefix=len(k) + 1)}"

  def _note(self, v: Any) -> Any:
    """Record the builders a nested call needs, so the import line stays right.

    Only when the value is actually RENDERED — a candidate the byte-for-byte check
    rejects must not leave a stray import behind for a call nothing emitted.
    """
    if isinstance(v, _Expr):
      self.used |= set(v.used)
    return v

  def _call_inline(self, name: str, args: list[Any], kwargs: dict[str, Any]) -> str:
    """Single-line `name(a, b, k=v)` — used when it fits in `_WIDTH`."""
    self.used.add(name)
    parts = [_inline(self._note(a)) for a in args]
    parts += [self._kw_str(k, v, inline=True) for k, v in kwargs.items()]
    return f"{name}(" + ", ".join(parts) + ")"

  def _call_wrapped(
      self, name: str, args: list[Any], kwargs: dict[str, Any], indent: int
  ) -> str:
    """Multi-line call: one arg/kwarg per line at `indent + 4`, close at `indent`.

    Matches ruff/pyink deterministically (no external formatter, so INV-2 byte-stability
    holds): nested values pretty-print via `_lit`; a call with a SINGLE element gets no
    magic trailing comma (e.g. ``raw(\\n    {...}\\n)``), a multi-element call does.
    """
    self.used.add(name)
    child = indent + 4
    pad = " " * child
    lines = [_lit(self._note(a), child) for a in args]
    lines += [self._kw_str(k, v, inline=False, indent=child) for k, v in kwargs.items()]
    close = " " * indent
    if len(lines) == 1:
      return f"{name}(\n{pad}{lines[0]}\n{close})"
    return f"{name}(\n" + "".join(f"{pad}{ln},\n" for ln in lines) + close + ")"

  # --- slots --------------------------------------------------------------

  def render_slot(self, d: dict[str, Any]) -> str:
    """Render one slot expression (goes inside `flow.add(...)`, base column 4)."""
    cand = _candidate(_candidate_slot, d)
    if cand is not None:
      name, args, kwargs = cand
      built = _build(name, args, kwargs)
      if built is not None and list(built.items()) == list(d.items()):
        inline = self._call_inline(name, args, kwargs)
        # +4 flow.add indent, +1 for the trailing comma the caller appends.
        if len(inline) + 5 <= _WIDTH:
          return inline
        return self._call_wrapped(name, args, kwargs, indent=4)
    self.used.add("raw")
    inline = "raw(" + _inline(d) + ")"
    if len(inline) + 5 <= _WIDTH:
      return inline
    return self._call_wrapped("raw", [d], {}, indent=4)

  # --- tasks --------------------------------------------------------------

  def render_task(self, d: dict[str, Any]) -> str:
    """Render one full `flow.task(...)` statement (module level, base column 0)."""
    cand = _candidate(_candidate_task, d)
    if cand is not None:
      name, args, kwargs = cand
      built = _build(name, args, kwargs)
      if built is not None and list(built.items()) == list(d.items()):
        single = "flow.task(" + self._call_inline(name, args, kwargs) + ")"
        if len(single) <= _WIDTH:
          return single
        inner = self._call_wrapped(name, args, kwargs, indent=4)
        return "flow.task(\n    " + inner + "\n)"
    self.used.add("raw")
    single = "flow.task(raw(" + _inline(d) + "))"
    if len(single) <= _WIDTH:
      return single
    inner = self._call_wrapped("raw", [d], {}, indent=4)
    return "flow.task(\n    " + inner + "\n)"

  # --- flow-level policy --------------------------------------------------

  def render_set(self, key: str, value: Any) -> str:
    inline = f"flow.set({_str_lit(key)}, {_inline(value)})"
    if len(inline) <= _WIDTH:
      return inline
    return f"flow.set(\n    {_str_lit(key)},\n    {_lit(value, 4)},\n)"


# ---------------------------------------------------------------------------
# Candidate builders — reverse a dict into (builder_name, args, kwargs). The
# caller then CALLS the real builder and compares order-sensitively; a candidate
# that fails to reproduce the dict byte-for-byte falls back to raw({...}).
# ---------------------------------------------------------------------------

_BUILDERS = {
    "user_slot": user_slot,
    "intent_slot": intent_slot,
    "passive_slot": passive_slot,
    "announce": announce,
    "event_slot": event_slot,
    "result_slot": result_slot,
    "task": task,
    "component": component,
}


def _candidate_slot(d: dict[str, Any]) -> Optional[tuple[str, list, dict]]:
  src = d.get("source")
  if src == "user":
    # An intent (enum) slot and a passive (never-asked) slot both live under source=user but carry
    # extra semantics user_slot can't reproduce — dispatch to their dedicated builders first.
    if d.get("kind") == "intent":
      return _candidate_intent_slot(d)
    if d.get("passive"):
      return _candidate_passive_slot(d)
    return _candidate_user_slot(d)
  if src == "announce":
    return _candidate_announce(d)
  if src == "event":
    return _candidate_event_slot(d)
  if isinstance(src, str) and src.startswith("task:"):
    return _candidate_result_slot(d)
  return None


def _candidate_intent_slot(d: dict[str, Any]) -> Optional[tuple[str, list, dict]]:
  name = d.get("name")
  options = d.get("option_cues")
  if not isinstance(name, str) or not isinstance(options, dict) or not options:
    return None
  kw: dict[str, Any] = {}
  if d.get("passive"):
    kw["passive"] = True
  if d.get("ask") is not None:
    kw["ask"] = d["ask"]
  setter = d.get("setter")
  if setter is not None and setter != f"set_{name}":
    kw["setter"] = setter
  if d.get("dtmf_map") is not None:
    kw["dtmf"] = d["dtmf_map"]
  if d.get("requires") is not None:
    kw["requires"] = d["requires"]
  if d.get("condition") is not None:
    kw["condition"] = d["condition"]
  return ("intent_slot", [name, options], _order_intent_kwargs(kw))


def _order_intent_kwargs(kw: dict[str, Any]) -> dict[str, Any]:
  order = ["ask", "passive", "setter", "dtmf", "requires", "condition"]
  return {k: kw[k] for k in order if k in kw}


def _candidate_passive_slot(d: dict[str, Any]) -> Optional[tuple[str, list, dict]]:
  name = d.get("name")
  if not isinstance(name, str) or not d.get("passive"):
    return None
  kw: dict[str, Any] = {}
  setter = d.get("setter")
  if setter is not None and setter != f"set_{name}":
    kw["setter"] = setter
  if d.get("option_cues") is not None:
    kw["option_cues"] = d["option_cues"]
  if d.get("kind") is not None:
    kw["kind"] = d["kind"]
  if d.get("requires") is not None:
    kw["requires"] = d["requires"]
  if d.get("condition") is not None:
    kw["condition"] = d["condition"]
  return ("passive_slot", [name], kw)


def _candidate_user_slot(d: dict[str, Any]) -> Optional[tuple[str, list, dict]]:
  name = d.get("name")
  ask = d.get("ask")
  if not isinstance(name, str) or not isinstance(ask, str):
    return None
  kw: dict[str, Any] = {}
  setter = d.get("setter")
  if setter is not None and setter != f"set_{name}":
    kw["setter"] = setter
  val = d.get("validation") or {}
  # The reprompt-ladder shape (built from reprompts/max_retries/on_exhaust params) is
  # {max_retries, reprompts, on_exhaust}. Anything else — e.g. a mined error-code map
  # ({max_retries, errors, on_exhaust}) — can't be rebuilt from those params, so pass the
  # whole ladder verbatim via `validation=` (keeps the slot idiomatic instead of raw).
  is_ladder_shape = "reprompts" in val and set(val) <= {"max_retries", "reprompts", "on_exhaust"}
  if val and not is_ladder_shape:
    kw["validation"] = val
  else:
    reprompts = val.get("reprompts")
    default_reprompts = [f"Sorry, I didn't catch that. {ask}", "One more time. " + ask]
    if reprompts is not None and reprompts != default_reprompts:
      kw["reprompts"] = reprompts
    mr = val.get("max_retries")
    if mr is not None and mr != 3:
      kw["max_retries"] = mr
    oe = val.get("on_exhaust") or {}
    say = oe.get("say")
    if say is not None and say != "I'm still having trouble hearing you.":
      kw["on_exhaust"] = say
    then = oe.get("then")
    if then is not None and then != {"tool": "transfer_to_human"}:
      kw["on_exhaust_then"] = then
  if d.get("dtmf_map") is not None:
    kw["dtmf"] = d["dtmf_map"]
  if d.get("requires") is not None:
    kw["requires"] = d["requires"]
  if d.get("condition") is not None:
    kw["condition"] = d["condition"]
  if d.get("requires_readback"):
    kw["readback"] = True
  # Emitted right after `readback`, which is where user_slot() inserts the key it
  # produces — render_slot compares `list(built.items()) == list(d.items())`, so a
  # candidate that rebuilds the same pairs in a different ORDER is still a mismatch
  # and would drop the whole slot to raw({...}).
  if d.get("skip_readback_if_matches") is not None:
    kw["skip_readback_if_matches"] = d["skip_readback_if_matches"]
  hint = d.get("hint")
  if hint is not None and hint != name.replace("_", " "):
    kw["hint"] = hint
  # Last, matching the position user_slot() inserts it at — the round-trip
  # comparison is key-ORDER sensitive.
  if d.get("verbatim"):
    kw["verbatim"] = True
  # After `verbatim`, matching user_slot()'s own insertion order.
  if d.get("filler_say") is not None:
    kw["filler_say"] = d["filler_say"]
  if d.get("automatic_fillers") is False:
    kw["automatic_fillers"] = False
  return ("user_slot", [name, ask], kw)


def _candidate_announce(d: dict[str, Any]) -> Optional[tuple[str, list, dict]]:
  name = d.get("name")
  message = d.get("message")
  # A model-rendered announce carries its content in `message` and has no response
  # parts at all, so an absent `response` is a valid shape rather than a mismatch.
  resp = d.get("response", [] if isinstance(message, str) else None)
  if not isinstance(name, str) or not isinstance(resp, list):
    return None
  # A hand-off is the LAST thing an announce emits and it is a PAIR — the vendor
  # payload plus the `end_session` that has to travel with it. Split it off the tail
  # and reverse it into a `handoff=` call; everything before it is ordinary parts.
  head, tail = resp, []
  for i, p in enumerate(resp):
    if isinstance(p, dict) and p.get("type") == "payload":
      head, tail = resp[:i], resp[i:]
      break
  handoff_expr = _candidate_handoff(tail) if tail else None
  if tail and handoff_expr is None:
    return None  # a payload announce() cannot rebuild -> raw({...}), as before
  texts: list[Any] = []
  barge_in = True
  end = False
  escalated = False
  reason = "completed"
  transfer_to = None
  for p in head:
    if not isinstance(p, dict):
      return None
    t = p.get("type")
    if t == "text":
      if set(p) - {"type", "text", "interruptable"}:
        return None
      texts.append(p.get("text"))
      if p.get("interruptable") is False:
        barge_in = False
    elif t == "transfer":
      if set(p) - {"type", "agent"}:
        return None
      transfer_to = p.get("agent")
    elif t == "end_session":
      if set(p) - {"type", "reason", "escalated"}:
        return None
      end = True
      reason = p.get("reason", "completed")
      if p.get("escalated"):
        escalated = True
    else:
      return None
  if handoff_expr is not None and (end or transfer_to is not None):
    # announce() rejects both combinations (two competing dispositions), so this
    # shape has no builder form at all.
    return None
  kw: dict[str, Any] = {}
  if message is not None:
    kw["message"] = message
  if d.get("requires") is not None:
    kw["requires"] = d["requires"]
  if d.get("condition") is not None:
    kw["condition"] = d["condition"]
  if end:
    kw["end"] = True
  if escalated:
    kw["escalated"] = True
  if reason != "completed":
    kw["reason"] = reason
  if not barge_in:
    kw["barge_in"] = False
  if d.get("shared"):
    kw["shared"] = True
  if d.get("preempt"):
    kw["preempt"] = True
  if transfer_to is not None:
    kw["transfer_to"] = transfer_to
  if d.get("end_conversation"):
    kw["end_conversation"] = True
  if handoff_expr is not None:
    kw["handoff"] = handoff_expr  # last, matching announce()'s signature order
  return ("announce", [name, texts], kw)


# --- Telephony hand-off -----------------------------------------------------
#
# `flows.handoff(flows.ujet(menu_id="90"))` emits a vendor payload part AND the
# `end_session` that has to accompany it. Reversing that pair back out of a config
# is what lets a migrated hand-off read as the thing it IS rather than as an
# anonymous blob of vendor JSON — the difference between an author seeing
# `handoff=handoff(ujet(menu_id="90"))` and seeing forty lines of `raw({...})` they
# have to decode before they dare touch it.
#
# Every rule below is a REFUSAL to guess. An unrecognized vendor still renders as a
# raw payload wrapped in handoff() (the pair is the invariant, not the vendor); a
# pair whose two parts carry different conditions has no builder form at all, since
# `handoff(surface=/condition=)` gates both together on purpose; and anything else
# in the tail drops the whole slot to raw({...}). The byte-for-byte gate in
# `render_slot` is the backstop for all of it.


def _candidate_handoff(parts: list[Any]) -> Optional[_Expr]:
  """Reverse a trailing `[payload, end_session]` into a `flows.handoff(...)` call."""
  if len(parts) != 2:
    return None
  payload, end = parts
  if not (isinstance(payload, dict) and isinstance(end, dict)):
    return None
  if payload.get("type") != "payload" or end.get("type") != "end_session":
    return None
  if set(payload) - {"type", "data", "condition"}:
    return None
  if set(end) - {"type", "reason", "escalated", "condition"}:
    return None
  data = payload.get("data")
  if not isinstance(data, dict) or not data:
    return None
  condition = payload.get("condition")
  if condition != end.get("condition"):
    return None  # a SPLIT pair — handoff() cannot express one, by design
  reason = end.get("reason")
  if not isinstance(reason, str) or not reason:
    return None
  escalated = bool(end.get("escalated"))

  vendor = _candidate_vendor(data)
  kw: dict[str, Any] = {}
  if reason != HANDOFF_REASON:
    kw["reason"] = reason
  # With a builder payload `escalated` defaults to what the vendor MEANS, so it is
  # only spelled out when the config disagrees. With a raw dict there is no default
  # — handoff() insists on being told — so it is always passed.
  if vendor is None or escalated != bool(vendor.value.escalation):
    kw["escalated"] = escalated
  if condition is not None:
    if set(condition) == {"surface"} and isinstance(condition["surface"], str):
      kw["surface"] = condition["surface"]
    else:
      kw["condition"] = condition
  return _expr("handoff", vendor if vendor is not None else data, **kw)


def _candidate_vendor(data: dict[str, Any]) -> Optional[_Expr]:
  """`ujet(...)` / `dialogflow_cx(...)` for a recognized payload, else None."""
  keys = list(data)
  if keys == [UJET_KEY] and isinstance(data[UJET_KEY], dict):
    return _candidate_ujet(data[UJET_KEY])
  if DIALOGFLOW_KEY in keys and set(keys) <= {DIALOGFLOW_KEY, "parameters"}:
    return _candidate_dialogflow(data)
  if CXAS_KEY in keys and set(keys) <= {CXAS_KEY, "variables"}:
    return _candidate_cxas(data)
  return None


def _candidate_ujet(obj: dict[str, Any]) -> Optional[_Expr]:
  """`ujet()` builds its object in a fixed order; anything else can't be reproduced."""
  named = ["menu_id", "escalation_reason", "type", "action", "language"]
  if list(obj)[:len(named)] != named:
    return None
  extra = {k: obj[k] for k in list(obj)[len(named):]}
  kw: dict[str, Any] = {"menu_id": obj["menu_id"]}
  # Signature order, defaults omitted — `ujet(menu_id="90")` for the common shape.
  if obj["escalation_reason"] != "by_virtual_agent":
    kw["escalation_reason"] = obj["escalation_reason"]
  if obj["language"] != "en":
    kw["language"] = obj["language"]
  if obj["action"] != UJET_ESCALATION_ACTION:
    kw["action"] = obj["action"]
  if obj["type"] != "action":
    kw["message_type"] = obj["type"]
  if extra:
    kw["extra"] = extra
  return _expr("ujet", **kw)


def _candidate_dialogflow(data: dict[str, Any]) -> Optional[_Expr]:
  kw: dict[str, Any] = {"agent": data[DIALOGFLOW_KEY]}
  params = data.get("parameters")
  if params:
    kw["parameters"] = params
  return _expr("dialogflow_cx", **kw)


def _candidate_cxas(data: dict[str, Any]) -> Optional[_Expr]:
  kw: dict[str, Any] = {"app": data[CXAS_KEY]}
  variables = data.get("variables")
  if variables:
    kw["variables"] = variables
  return _expr("cxas", **kw)


def _candidate_event_slot(d: dict[str, Any]) -> Optional[tuple[str, list, dict]]:
  if set(d.keys()) != {"name", "source", "event_key"} or d.get("source") != "event":
    return None
  name = d["name"]
  key = d["event_key"]
  args = [name] if key == name else [name, key]
  return ("event_slot", args, {})


def _candidate_result_slot(d: dict[str, Any]) -> Optional[tuple[str, list, dict]]:
  if set(d.keys()) != {"name", "source"}:
    return None
  src = d["source"]
  if not (isinstance(src, str) and src.startswith("task:")):
    return None
  return ("result_slot", [d["name"], src.split(":", 1)[1]], {})


def _candidate_task(d: dict[str, Any]) -> Optional[tuple[str, list, dict]]:
  if "component" in d:
    return _candidate_component(d)
  name = d.get("name")
  tool = d.get("tool")
  inputs = d.get("inputs")
  outputs = d.get("outputs")
  if not isinstance(name, str) or not isinstance(tool, str):
    return None
  if not isinstance(inputs, list) or not isinstance(outputs, dict) or not outputs:
    return None
  items = list(outputs.items())
  first_key, first_val = items[0]
  out_slot = first_val
  if not isinstance(out_slot, str):
    return None
  kw: dict[str, Any] = {}
  if first_key != out_slot:
    kw["out_key"] = first_key
  if items[1:]:
    kw["extra_outputs"] = dict(items[1:])
  requires = d.get("requires")
  if requires is not None and requires != list(inputs):
    kw["requires"] = requires
  condition = d.get("condition")
  if condition is not None:
    kw["condition"] = condition
  success_check = d.get("success_check", "success")
  if success_check != "success":
    kw["success_check"] = success_check
  if d.get("terminal"):
    kw["terminal"] = True
  if d.get("then_say") is not None:
    kw["then_say"] = d["then_say"]
  if d.get("on_failure") is not None:
    kw["on_failure"] = d["on_failure"]
  if d.get("clear_slots_on_success") is not None:
    kw["clear_slots_on_success"] = d["clear_slots_on_success"]
  # Coordinate with the concurrent dsl.py task() kwargs (transfer_to/on_complete/
  # readback_inputs). Use them only if the installed builder accepts them; else the
  # dict can't be reproduced by task() -> fall back to raw({...}).
  oc = d.get("on_complete")
  if oc is not None:
    if "transfer_to" in _TASK_PARAMS and set(oc.keys()) == {"transfer_to"}:
      kw["transfer_to"] = oc["transfer_to"]
    elif "on_complete" in _TASK_PARAMS:
      kw["on_complete"] = oc
    else:
      return None
  rbi = d.get("readback_inputs")
  if rbi is not None:
    if "readback_inputs" in _TASK_PARAMS:
      kw["readback_inputs"] = rbi
    else:
      return None
  # Same forward-compat hedge as the block above: reproduce these only if the
  # installed task() accepts them, else fall back to raw({...}).
  for _k in ("awaits", "then_directive", "filler_say", "while_running",
             "parallel"):
    if d.get(_k) is not None:
      if _k not in _TASK_PARAMS:
        return None
      kw[_k] = d[_k]
  if d.get("verbatim"):
    if "verbatim" not in _TASK_PARAMS:
      return None
    kw["verbatim"] = True
  if d.get("automatic_fillers") is False:
    if "automatic_fillers" not in _TASK_PARAMS:
      return None
    kw["automatic_fillers"] = False
  return ("task", [name, tool, list(inputs), out_slot], _order_task_kwargs(kw))


def _order_task_kwargs(kw: dict[str, Any]) -> dict[str, Any]:
  """Fixed, signature-following kwarg order for deterministic rendering."""
  order = [
      "out_key", "extra_outputs", "requires", "condition", "success_check",
      "terminal", "then_say", "on_failure", "clear_slots_on_success",
      "parallel",
      "transfer_to", "on_complete", "readback_inputs", "awaits",
      "then_directive", "filler_say", "while_running", "verbatim",
      "automatic_fillers",
  ]
  return {k: kw[k] for k in order if k in kw}


def _candidate_component(d: dict[str, Any]) -> Optional[tuple[str, list, dict]]:
  name = d.get("name")
  child = d.get("component")
  if not isinstance(name, str) or not isinstance(child, str):
    return None
  kw: dict[str, Any] = {}
  inputs = d.get("inputs")
  if inputs not in (None, {}):
    kw["inputs"] = inputs
  outputs = d.get("outputs")
  if outputs not in (None, {}):
    kw["outputs"] = outputs
  on_abort = d.get("on_abort", "skip")
  if on_abort != "skip":
    kw["on_abort"] = on_abort
  if "requires" in d:
    kw["requires"] = d["requires"]
  if d.get("condition") is not None:
    kw["condition"] = d["condition"]
  return ("component", [name, child], kw)


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def render_config_source(
    config: dict, *, config_id: str, root_agent: str = ""
) -> str:
  """Render `config` as an importable `flows` authoring module (round-trips).

  The returned source binds module-level `flow` to a `flows.Flow` such that
  `flow.to_config() == config` byte-for-byte (order preserved) — the round-trip
  contract the migration backend asserts after `exec`.
  """
  r = _Renderer()
  policy_lines = [
      r.render_set(k, v)
      for k, v in config.items()
      if k not in ("slots", "tasks")
  ]
  slot_lines = [r.render_slot(s) for s in config.get("slots", []) or []]
  task_lines = [r.render_task(t) for t in config.get("tasks", []) or []]

  # Minimal, deterministic import line: Flow first, then used builders sorted.
  used = {n for n in r.used}
  imports = ["Flow"] + sorted(used)
  import_block = "from flows import (\n" + "".join(
      f"    {n},\n" for n in imports
  ) + ")"

  ctor_args = _str_lit(config_id)
  if root_agent:
    ctor_args += f", root_agent={_str_lit(root_agent)}"
  ctor = f"Flow({ctor_args})"
  if len(f"flow = {ctor}") > _WIDTH:
    # Wrap like ruff/pyink: the (atomic) args go on one indented continuation line.
    ctor = f"Flow(\n    {ctor_args}\n)"

  # The module docstring is the ONE place this renderer interpolates a value outside a Python
  # literal, so it is the one place `_str_lit`'s escaping does not protect. `repr` does not escape
  # `"""`, so a `config_id` carrying one would CLOSE the docstring early and everything after it
  # would become top-level code — code that the round-trip gate and `materialize` then run through
  # `exec(compile(...))`. config_id is not a compiler-invented constant (the migration backend
  # derives it from `<op>_dag`, where the op name is mined from the SCANNED agent), so escape every
  # double quote: inside a `"""` docstring `\"` is a plain quote, and a breakout becomes impossible.
  # Ids without quotes render byte-for-byte as before.
  doc_id = repr(config_id).replace('"', '\\"')
  out: list[str] = []
  out.append(
      f'"""Flows-SDK authoring source for {doc_id} '
      "(generated by flows.authoring.render).\n\n"
      "Deterministically rendered from the emitted Config. Every slot/task is a\n"
      "high-level builder when it reproduces the Config byte-for-byte, else raw({...}).\n"
      "Round-trip: flow.to_config() reproduces the exact Config (order preserved).\n"
      '"""'
  )
  out.append("")  # ruff/pyink: a blank line after the module docstring
  out.append(import_block)
  out.append("")
  out.append(f"flow = {ctor}")
  for line in policy_lines:
    out.append(line)
  if slot_lines:
    out.append("flow.add(")
    for sl in slot_lines:
      out.append(_indent_first(sl, 4) + ",")
    out.append(")")
  for tl in task_lines:
    out.append(tl)
  out.append("")
  out.append("")
  out.append('if __name__ == "__main__":')
  out.append("    from flows.config.validation import raw_validate_single")
  out.append("")
  out.append("    valid, errors, _warnings = raw_validate_single(flow.to_config())")
  out.append('    print("valid" if valid else errors)')
  return "\n".join(out) + "\n"


def _indent_first(expr: str, indent: int) -> str:
  """Prefix only the FIRST line of a (possibly multiline) expression with spaces.

  A multiline `raw({...})` already carries its own internal indentation (rendered
  for the given base column), so only the opening line needs the leading pad.
  """
  pad = " " * indent
  first, sep, rest = expr.partition("\n")
  return pad + first + sep + rest


def render_app_source(app_spec: dict) -> str:
  """Render a thin single-flow App wrapper module (minimal).

  Expects `app_spec` with at least `config` and `config_id`; optional `root_agent`
  and `app_display_name`. Emits the flow source plus a `flows.App(root_flow=flow,
  ...)` binding. Multi-agent apps are out of scope for now.
  """
  config = app_spec["config"]
  config_id = app_spec["config_id"]
  root_agent = app_spec.get("root_agent", "")
  display = app_spec.get("app_display_name") or config_id

  flow_src = render_config_source(config, config_id=config_id, root_agent=root_agent)
  # Splice the App wrapper before the __main__ footer of the flow module.
  marker = '\nif __name__ == "__main__":'
  head, sep, tail = flow_src.partition(marker)
  app_block = (
      "import flows\n\n"
      f"app = flows.App(root_flow=flow, app_display_name={_str_lit(display)})\n"
  )
  return head + app_block + sep + tail
