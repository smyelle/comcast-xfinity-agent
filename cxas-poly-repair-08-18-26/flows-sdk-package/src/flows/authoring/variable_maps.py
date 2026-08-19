"""Variable maps — fill slots from the session variables a call arrives with.

A conversation rarely starts from nothing. The account number came from the IVR, the
tracking number from a web form, the caller was identified before the transfer. Until
now the only way to spend that knowledge was `event_data`: re-author the slot as an
event slot with a matching `event_key` and route every value through the one OBJECT
variable CES reserves for it. Anything else a session carried — including everything
`cujs.yaml` seeds — sat in session state and the agent asked for it anyway.

A `variable_map` is a named description of ONE WAY a conversation can start:

    app = flows.App(
        root_flow=repair,
        variable_maps=[
            flows.variable_map("by_account", {
                "account_number": ["accountNumber", "account_id"],
            }),
        ],
    )

Read every binding as SLOT on the left, where its value comes from on the right. The
slot is the name the author chose and controls; the right-hand side is whatever the
upstream system happens to call it. Keying on the slot also makes two bindings fighting
over one slot impossible to write, rather than something to validate.

Four source forms, and that is the whole grammar::

    "tracking_number": "tracking_id"                     # one variable
    "account_number":  ["accountNumber", "account_id"]   # synonyms, first present wins
    "tracking_number": "parcel.tracking_id"              # a path into an OBJECT variable
    "outage_status":   flows.bind("outage_status",       # present but meaningless
                                  reject=["PENDING_BACKEND_RESULT"])

`reject` is not decoration. A real agent's upstream pre-sets its status variables to a
sentinel meaning "the backend has not answered yet"; treated as a value it fills a slot
with something no branch matches, and the flow falls through to the model. It is a
REJECT-LIST rather than a regex or an enum on purpose: "this is not an answer" is a much
smaller and safer claim than "this is the right answer", and the framework ships no
reject values of its own — a sentinel is a fact about one agent's upstream.

Three rules govern matching, chosen so a map can only ever SKIP a question and never
invent an answer:

* **all or nothing** — a map applies only if every binding resolves to a present,
  non-empty, correctly-shaped, non-rejected value. Half a shape is not a shape;
* **first declared wins** — when two maps both fit, the earlier one is used, so the most
  specific belongs first (`validate_app` rejects an ordering where a later map could
  never win);
* **no match is a clean no-op** — nothing is filled and the flow asks as it always would.

Ingress only. This reads session variables and writes slots, never the reverse.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence, Union

# What a slot's value may be drawn from. A bare string is one variable (optionally a
# dotted path into an OBJECT one); a list is synonyms tried in order; `Bind` adds the
# reject-list. The three normalize to the same emitted shape.
Source = Union[str, Sequence[str], "Bind"]


@dataclass(frozen=True)
class Bind:
  """One slot's sources plus the values that do not count as an answer."""

  var: tuple[str, ...]
  reject: tuple[str, ...] = ()


@dataclass(frozen=True)
class VariableMap:
  """One named way a conversation can start, and the slots it fills."""

  name: str
  bindings: dict[str, Bind]
  description: str = ""


def bind(var: Union[str, Sequence[str]], *,
         reject: Sequence[str] = ()) -> Bind:
  """A binding source with a reject-list — values that read as "not answered yet"."""
  if isinstance(var, str):
    names = (var,)
  elif isinstance(var, (list, tuple)):
    names = tuple(var)
  else:
    raise ValueError(
        f"bind(): a source is a variable name or a list of them, got"
        f" {type(var).__name__}. Anything else reaches `tuple()` and raises there,"
        " one frame from the authoring mistake rather than at it.")
  bad = [n for n in names if not isinstance(n, str)]
  if bad:
    raise ValueError(
        f"bind(): every source must be a variable NAME, got {bad!r}."
        " A value belongs on the slot's `default`, not in a binding.")
  return Bind(var=names, reject=tuple(reject))


def variable_map(name: str, bindings: Mapping[str, Source], *,
                 description: str = "") -> VariableMap:
  """Declare one variable shape and the slots it fills.

  Args:
    name: How this shape is referred to in diagnostics and `sm._variable_map`.
    bindings: `{slot_name: source}` — see the module docstring for the source forms.
    description: Optional prose for docs/diagnostics.

  Returns:
    A `VariableMap` to hand to `flows.App(variable_maps=[...])`.

  Raises:
    ValueError: on an empty map, an empty or malformed source, or the same variable
      bound to two different slots.
  """
  if not name or not isinstance(name, str):
    raise ValueError("variable_map(): needs a non-empty name — it is how the chosen"
                     " shape is reported in diagnostics and in sm._variable_map.")
  if not bindings:
    raise ValueError(
        f"variable_map({name!r}): needs at least one binding — an empty map matches"
        " every session vacuously and would shadow every map declared after it.")

  out: dict[str, Bind] = {}
  seen: dict[str, str] = {}
  for slot, source in bindings.items():
    if not slot or not isinstance(slot, str):
      raise ValueError(
          f"variable_map({name!r}): every key must be a slot name (the value's"
          f" destination), got {slot!r}.")
    b = source if isinstance(source, Bind) else bind(source)
    if not b.var:
      raise ValueError(
          f"variable_map({name!r}): slot {slot!r} names no variable to fill it from.")
    for expr in b.var:
      _check_expr(name, slot, expr)
      # The same variable feeding two slots is either a copy-paste error or a synonym
      # written on the wrong axis — synonyms belong in ONE binding's list, where they
      # are alternatives, not in two bindings, where they are both required.
      if expr in seen and seen[expr] != slot:
        raise ValueError(
            f"variable_map({name!r}): variable {expr!r} is bound to both"
            f" {seen[expr]!r} and {slot!r}. If these are two names for one fact, put"
            f" them in a single binding's list; two bindings means BOTH must arrive.")
      seen[expr] = slot
    out[slot] = b

  return VariableMap(name=name, bindings=out, description=description or "")


def _check_expr(map_name: str, slot: str, expr: Any) -> None:
  """A source expression is a variable name, optionally with a dotted path."""
  if not expr or not isinstance(expr, str):
    raise ValueError(
        f"variable_map({map_name!r}): slot {slot!r} has a non-string source {expr!r}.")
  if any(not part for part in expr.split(".")):
    raise ValueError(
        f"variable_map({map_name!r}): slot {slot!r} has a malformed source {expr!r} —"
        " a dotted path reaches into an OBJECT variable, so every segment must be"
        " named (no leading, trailing or doubled dots).")


# ---------------------------------------------------------------------------
# Build-time lowering. Everything the runtime needs is resolved HERE — the shape a
# value must have, whether the slot confirms before it is accepted, and the reject
# list — so the ingress callback does no config lookup and no string parsing per turn.
# ---------------------------------------------------------------------------


def variable_names(maps: Sequence[VariableMap]) -> set[str]:
  """Every top-level variable any map reads (path roots only)."""
  return {expr.split(".", 1)[0]
          for m in maps for b in m.bindings.values() for expr in b.var}


def _alt(expr: str) -> dict[str, Any]:
  head, _, rest = expr.partition(".")
  return {"var": head, "path": rest.split(".") if rest else []}


def _shape_of(slot_def: Mapping[str, Any]) -> str:
  """A slot is list-valued if it collects repeatedly or reads its value back as one."""
  if slot_def.get("repeated"):
    return "list"
  fmt = slot_def.get("readback_fmt")
  if isinstance(fmt, Mapping) and fmt.get("type") in ("count", "join"):
    return "list"
  return "scalar"


def project(maps: Sequence[VariableMap],
            slots: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
  """Lower `maps` for ONE config, dropping bindings whose slot it does not hold.

  A map that keeps some of its bindings rides along reduced — a flow holding one of
  the two slots a shape names should still be seeded with that one. A map that keeps
  none is dropped from this config entirely.
  """
  by_name = {s["name"]: s for s in slots if isinstance(s, Mapping) and s.get("name")}
  out = []
  for m in maps:
    # Lowering runs BEFORE validation, so anything that is not a VariableMap has to
    # be skipped rather than crashed on — otherwise the AttributeError from here
    # pre-empts the validator's actionable message about it.
    if not isinstance(m, VariableMap):
      continue
    lowered = []
    for slot, b in m.bindings.items():
      slot_def = by_name.get(slot)
      if slot_def is None:
        continue
      lowered.append({
          "slot": slot,
          "shape": _shape_of(slot_def),
          "readback": bool(slot_def.get("requires_readback")),
          "reject": list(b.reject),
          "alts": [_alt(e) for e in b.var],
      })
    if lowered:
      out.append({"name": m.name, "bindings": lowered})
  return out


# ---------------------------------------------------------------------------
# The ingress callback. GENERATED per app rather than vendored into the blessed
# bundle: it is framework-authored but app-scoped, exactly like the generated setters
# and `set_active_flow`, and the blessed 4 are the callbacks EVERY slot-filling agent
# carries and the deploy gate byte-verifies. This one only exists when an app declares
# maps, so an app that declares none emits precisely what it did before.
#
# It is registered FIRST in beforeAgentCallbacks, ahead of any author hook, so a
# session that arrived knowing something has already spent that knowledge by the time
# author code runs. That ordering is the whole point: an author `before_agent` hook
# that sweeps a backend off the account number cannot do so on turn 0 if the account
# only reaches the slot machine afterwards.
# ---------------------------------------------------------------------------

INGRESS_SUBDIR = "before_agent_callbacks/before_agent_callbacks_00pre"

_INGRESS_SOURCE = '''# pylint: disable=invalid-name,undefined-variable,unused-argument,broad-exception-caught,line-too-long
"""Session-variable ingress: pre-fill slots from the variables a call arrived with.

Generated by flows (see flows.authoring.variable_maps). Registered ahead of every
other before_agent callback so author hooks observe an already-seeded slot machine.
"""
from typing import Any, Optional


def before_agent_callback(callback_context) -> None:
  """Fill slots from the FIRST declared variable shape that fits this session."""
  import json as json_lib

  state = callback_context.state
  raw = state.get("variable_maps_by_config")
  if not raw:
    return None
  try:
    table = json_lib.loads(raw) if isinstance(raw, str) else raw
  except Exception:
    return None
  if not isinstance(table, dict):
    return None
  # Resolution has not run yet (this callback is deliberately ahead of it), so fall
  # back to the cold-turn default. For a router app turn 0 is the host, which holds
  # no user slots, so nothing is seeded there and the map applies once a flow is live.
  config_id = state.get("_active_config_id") or state.get("default_config_id")
  maps = table.get(config_id)
  # Session variables are writable by whoever opens the session, so neither the table
  # nor `sm` is guaranteed to be the shape this was emitted with. A wrong shape must
  # skip ingress, never crash the turn — a callback that raises takes the whole
  # conversation down, and the flow would still have run fine without any seeding.
  if not isinstance(maps, list) or not maps:
    return None

  sm = state.get("sm")
  if not isinstance(sm, dict):
    sm = {}
  marker = sm.setdefault("_variable_map", {})
  if marker.get("done"):
    return None
  # setdefault, never a fresh dict: `sm` is not initialized yet (that is the NEXT
  # callback) and clobbering here would drop a transfer's carried values. Deliberately
  # does NOT set `_initialized` — that would suppress the real initializer.
  filled = sm.setdefault("filled", {})
  pending = sm.setdefault("pending", {})

  def resolve(binding):
    """First alternative that yields a present, correctly-shaped, unrejected value."""
    reject = binding.get("reject") or []
    want_list = binding.get("shape") == "list"
    for alt in binding.get("alts") or []:
      value = state.get(alt.get("var"))
      for segment in alt.get("path") or []:
        if not isinstance(value, dict):
          value = None
          break
        value = value.get(segment)
      if value is None:
        continue
      if isinstance(value, str):
        value = value.strip()
      # An unseeded CES variable arrives as its declared default, so "" / {} / [] mean
      # "not supplied" and cannot be told apart from absent. Rejects are the same
      # judgement one step further out: a sentinel the upstream writes for "no answer
      # yet" is not an answer either.
      if value == "" or value == {} or value == []:
        continue
      if isinstance(value, str) and value in reject:
        continue
      # A tuple is a list once it has been through JSON, so treat the two alike.
      # Accepting a tuple as a SCALAR would fill the slot on the turn it arrived and
      # then reject the identical value on the next one, after the round trip.
      if want_list:
        if not isinstance(value, (list, tuple)):
          continue
      elif isinstance(value, (dict, list, tuple)):
        continue
      return value
    return None

  chosen = None
  values = None
  for candidate in maps:
    if not isinstance(candidate, dict):
      continue
    resolved = {}
    for binding in candidate.get("bindings") or []:
      if not isinstance(binding, dict) or not binding.get("slot"):
        resolved = None
        break
      value = resolve(binding)
      if value is None:
        resolved = None
        break
      resolved[binding["slot"]] = (value, bool(binding.get("readback")))
    if resolved is not None:
      chosen, values = candidate, resolved
      break
  if chosen is None:
    return None

  written = []
  for slot, (value, readback) in values.items():
    if slot in filled or slot in pending:
      continue
    # A slot that confirms before it is believed must be STAGED, not filled: accepting
    # a seeded value as verified is worse than asking for it cold.
    if readback:
      pending[slot] = value
    else:
      filled[slot] = value
    written.append(slot)

  # Only a map that actually WROTE something has spent this flow's one attempt. A map
  # can resolve and write nothing (every slot already filled), and burning the marker
  # there would strand a later, larger shape that has not arrived yet.
  if written:
    marker["done"] = True
    marker["name"] = chosen.get("name")
    marker["written"] = written
  state["sm"] = sm
  return None
'''


def ingress_source() -> str:
  """The generated ingress callback body (identical for every app)."""
  return _INGRESS_SOURCE


def shadowed(lowered: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
  """Maps that can never win here: an earlier map's slots are a subset of theirs.

  Whenever the later map's bindings all resolve, the earlier one's do too, so the
  earlier one is always chosen. Computed on the LOWERED maps rather than the authored
  ones because projection drops bindings, and dropping a binding drops a conjunct —
  a map's discriminating condition can vanish in one flow while the map stays
  selectable there.
  """
  pairs = []
  for i, later in enumerate(lowered):
    later_slots = {b["slot"] for b in later["bindings"]}
    for earlier in lowered[:i]:
      if {b["slot"] for b in earlier["bindings"]} <= later_slots:
        pairs.append((later["name"], earlier["name"]))
        break
  return pairs
