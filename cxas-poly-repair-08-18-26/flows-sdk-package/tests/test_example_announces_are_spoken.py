"""A line the docs quote must actually reach the caller.

Every example here shipped a sentence that the module's own prose — or a docs page
transcript — presents as something the caller hears, and that the engine silently threw
away. The mechanism is always the same: an `announce`'s `texts` are only rendered on a
turn that PREEMPTS the model, and `flows.announce()` defaults `preempt=False`. On a turn
that does not otherwise preempt (a plain bootstrap welcome, a terminal reached by a task
result, a collection finishing) the engine fills the announce slot, marks it done, and
emits `response: null`. The slot inspector says the announce ran. The caller heard
nothing.

That failure is invisible to every other test in this suite. Validation passes — the
config is well-formed. The build passes — the app deploys. `filled["welcome"] is True`,
so a state assertion passes too. Only reading the SPOKEN output catches it, which is
what these do: drive the real engine and assert on the words that leave the turn.

`_spoken` deliberately reads both channels — `action["message"]` (the model's directive,
reworded before the caller hears it) and the `response` text parts (verbatim). Asserting
only on `response` would pass an example that had been "fixed" by moving the copy to
`message=`, which is a different behavior: the model rewords it, so a compliance line or
a localized string would no longer survive the trip.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_example_announces_are_spoken.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from typing import Any

import pytest

from flows.authoring import build
from flows.engine import loader as fb

_EXAMPLES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")


def _load(name: str) -> types.ModuleType:
  path = os.path.join(_EXAMPLES, f"{name}.py")
  spec = importlib.util.spec_from_file_location(f"_spoken_example_{name}", path)
  assert spec and spec.loader, path
  mod = importlib.util.module_from_spec(spec)
  # Registered BEFORE exec so `inspect.getsource` can resolve the @flows.tool return
  # models this module defines (assembly reads them to generate tool bodies).
  sys.modules[spec.name] = mod
  spec.loader.exec_module(mod)
  return mod


def _spoken(action: dict[str, Any]) -> str:
  """Everything the caller hears on one engine pass, both channels joined."""
  parts = [action.get("message") or ""]
  for part in action.get("response") or []:
    if isinstance(part, dict) and part.get("type") == "text":
      parts.append(part.get("text") or "")
  return " ".join(p for p in parts if p).strip()


class Call:
  """One offline call against an example, driven the way the platform drives it.

  A caller turn is NOT one engine pass. When a task fires the platform runs the tool
  and re-invokes the engine with the result inside the SAME turn, so what the caller
  hears is everything said across those passes. `turn()` returns the join, and
  `heard` accumulates the whole call — which is what lets a test assert that a line
  was said exactly ONCE rather than merely that it was said.
  """

  def __init__(self, example: str, *, seed: dict[str, Any] | None = None,
               tool_results: dict[str, dict] | None = None) -> None:
    mod = _load(example)
    # Assembled, not `root_flow.to_config()`: build-time layers (the single-flow gate,
    # the language-select slot) add slots the engine reads, and an example whose whole
    # subject is one of those layers cannot be driven from the authored config alone.
    self.config = build._assemble(mod.app)[0][mod.app.config_id]  # noqa: SLF001
    self.results = tool_results or {}
    self.sm = fb.seed_sm(self.config)
    self.sm["filled"], self.sm["pending"] = {}, {}
    gate = self.sm.get("_gate_slot") or self.config.get("gate_slot")
    if gate:
      self.sm[gate] = "j"
      self.sm["filled"][gate] = "j"
    # There is no model in this harness, so a value the LLM would have captured has to
    # be seeded. Otherwise the call could never reach the line under test.
    self.sm["filled"].update(seed or {})
    self.heard: list[str] = []

  def turn(self, text: str = "") -> str:
    said = []
    for pass_n in range(8):
      out = fb.run_engine(self.config, self.sm, config_id="j",
                          last_user_text=text if pass_n == 0 else "")
      self.sm = out["sm"]
      line = _spoken(out["action"])
      if line:
        said.append(line)
      fired = (out["action"].get("function_call") or {}).get("name") or ""
      if not fired:
        break
      assert fired in self.results, f"no stubbed result for {fired}"
      self.sm = fb.run_intake(fired, self.results[fired], self.sm)["sm"]
    spoken = " ".join(said)
    self.heard.append(spoken)
    return spoken

  def setter(self, tool: str, **args: Any) -> str:
    """A setter call the model would have made, then the turn it triggers."""
    self.sm = fb.run_intake(tool, fb.call_setter(tool, args), self.sm)["sm"]
    return self.turn()


# ── escalate_chain ───────────────────────────────────────────────────────────

_DIAGNOSIS = "the gateway has been offline since 9:14 this morning"
_VERDICT = f"Here's what I found: {_DIAGNOSIS}."
_HOLDING = "One moment while I pull your details together."
_ESCALATE_SAY = "Let me get you to someone who can help."


def _escalate_chain_call() -> Call:
  return Call("escalate_chain", seed={"account": "8069100230361049"},
              tool_results={
                  "diagnose": {"diagnosis": _DIAGNOSIS, "success": True},
                  "prepare_handoff": {
                      "summary": f"Account 8069100230361049 asked for a person."
                                 f" Diagnostics found {_DIAGNOSIS}.",
                      "success": True},
                  "hand_off": {"handoff_ack": "filed", "success": True}})


def test_escalate_chain_reads_the_diagnosis_out():
  """The verdict announce must speak, not just fill.

  The module's own comment calls the wrap-up slot the thing that "holds the turn open
  after the verdict is read out", and the docs page opens both its transcripts with
  this line. Authored without `preempt=True` the engine set `filled["verdict"] = True`
  and returned `response: null`, so the caller was asked "does that answer it?" about
  an answer they were never given.
  """
  call = _escalate_chain_call()
  assert _VERDICT in call.turn(), (
      "the diagnosis never reached the caller — an announce only renders its own "
      "`texts` when `preempt=True`")


def test_escalate_chain_queues_the_follow_up_behind_the_verdict():
  """A preempting announce carries the pending ask out with it, in that order.

  Pinned because the fix has a failure mode of its own: if the ask were left to a
  later turn the caller would hear a diagnosis and then silence.
  """
  spoken = _escalate_chain_call().turn()
  assert spoken.index(_VERDICT) < spoken.index("Does that answer it"), spoken


def test_escalate_chain_says_the_holding_line_exactly_once():
  """`then_say` belongs on the LAST chain member, or the caller hears it twice.

  `_escalate_path_turn` stashes a member's `then_say` in `_escalate_pending_msg` for
  the disposition to speak AND passes the same string as the fire action's message.
  On the final member only one of those two paths can run, so it is spoken once. On a
  NON-final member both run: once alongside the next member's tool call, and again
  folded ahead of `escalate.say`. This drives the whole hand-off and counts.
  """
  call = _escalate_chain_call()
  call.turn()                       # diagnostics land, verdict is read out
  call.setter("transfer_to_human", reason="wants a person")
  whole_call = " ".join(call.heard)

  assert whole_call.count(_HOLDING) == 1, (
      f"the holding line was spoken {whole_call.count(_HOLDING)} times — move "
      f"`then_say` to the final chain member:\n{whole_call}")
  assert _ESCALATE_SAY in whole_call, "the caller was never handed off"


def test_escalate_chain_holding_line_lands_ahead_of_the_hand_off():
  """Both spoken lines arrive on ONE caller turn, holding line first."""
  call = _escalate_chain_call()
  call.turn()
  last = call.setter("transfer_to_human", reason="wants a person")
  assert last == f"{_HOLDING} {_ESCALATE_SAY}", last


# ── language_switching ───────────────────────────────────────────────────────

_ES_STATUS = "El pedido A1042 esta en camino y llega hoy."


def test_language_switching_speaks_the_localized_status():
  """The one sentence the whole example exists to deliver.

  `lookup_order_status` localizes at the tool boundary precisely because the language
  lock governs MODEL-generated text — the tool's string has to survive verbatim. The
  terminal announce that carries it was authored non-preempting, so the tool returned
  Spanish, the announce filled, `end_session` never even rendered, and the call ended
  without a word. A caller who chose Spanish got silence.
  """
  call = Call("language_switching",
              seed={"language_choice": "Spanish", "order_number": "A1042"},
              tool_results={"lookup_order_status": {
                  "status_message": _ES_STATUS, "success": True}})
  assert _ES_STATUS in call.turn(), (
      "the localized status was dropped — a terminal announce still needs "
      "`preempt=True` to render its own `texts`")


# ── openapi_toolsets ─────────────────────────────────────────────────────────

_ORDER_RESULT = {"getOrder": {"status": "out for delivery",
                              "estimatedDate": "Thursday", "success": True}}


def test_openapi_toolset_opens_with_the_welcome_and_the_ask():
  """The documented opening turn: welcome verbatim, then the queued question.

  A bootstrap welcome is the worst case for this bug — it is the FIRST thing the
  caller does not hear, and the ask still goes out, so the call looks fine in a log.
  """
  spoken = Call("openapi_toolsets", tool_results=_ORDER_RESULT).turn()
  assert "I can check on an order for you." in spoken, spoken
  assert "What's your order number?" in spoken, spoken


def test_openapi_toolset_reads_the_order_status_out():
  """The API answer is the payload of the call; dropping it drops the whole point.

  The lifted `status`/`estimatedDate` values reached the slots — a state assertion
  passed — while the sentence interpolating them was discarded, so the caller was
  asked for a phone number to text a tracking link they had not been given.
  """
  spoken = Call("openapi_toolsets", seed={"order_id": "A-1042"},
                tool_results=_ORDER_RESULT).turn()
  assert "Your order is out for delivery, arriving Thursday." in spoken, spoken


# ── multi_shipment_tracking ──────────────────────────────────────────────────


def test_multi_shipment_signs_off_before_it_hangs_up():
  """A terminal announce that says nothing hangs up on the caller mid-air.

  The collection completing does not preempt the turn, so this one lost both its
  sign-off AND its `end_session` part: the documented last line of the call was an
  empty response.
  """
  call = Call("multi_shipment_tracking",
              seed={"shipments": [{"status": "out for delivery"}]})
  assert "All set. Have a great day!" in call.turn()


@pytest.mark.parametrize("example,slot", [
    ("escalate_chain", "verdict"),
    ("language_switching", "status"),
    ("openapi_toolsets", "welcome"),
    ("openapi_toolsets", "status"),
    ("multi_shipment_tracking", "done"),
])
def test_the_spoken_announces_are_authored_preempting(example, slot):
  """The static half of the guard, so a regression names itself.

  The behavioral tests above are the real check, but they fail with a diff of
  transcripts. This one fails with the slot that lost the flag, which is the fix.
  """
  mod = _load(example)
  config = build._assemble(mod.app)[0][mod.app.config_id]  # noqa: SLF001
  slot_def = next(s for s in config["slots"] if s["name"] == slot)
  assert slot_def.get("preempt") is True, (
      f"{example}.{slot} is authored non-preempting again — its `texts` are dropped "
      f"and the line the docs quote is never spoken")
