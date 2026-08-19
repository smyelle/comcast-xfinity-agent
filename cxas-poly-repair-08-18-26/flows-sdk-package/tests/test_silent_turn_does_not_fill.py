"""A turn the caller did not take must not fill a slot from the LAST turn's words.

`scanned_user_text` is the newest real utterance in the history. On most turns that is
this turn's own, and the engine leans on it: it re-invokes itself several times within
one turn (after a setter, after a terminal fires) with `last_user_text` empty, and
falling back to the scan is what keeps a deterministic `option_cues` match seeing the
words the caller just said.

Two turns break that assumption, and neither is distinguishable from a within-turn
re-invoke by the text alone: an INACTIVITY tick, and an async COMPLETION DELIVERY the
caller put no utterance on. On both, the scan is a PREVIOUS turn's words.

MEASURED, on an agent that offers something during an async wait. The caller was asked
whether they wanted to try it; they said nothing at all on the turn the completion
landed on; and their previous turn -- a description of the fault, which is what the
offer was about -- contained a phrase in the offer slot's DECLINE cue list. The slot
filled with the declining value, the rung gated on that value ends the session, and the
call closed on a refusal nobody made.

What the fix must NOT cost, pinned below just as hard:

  * the within-turn re-invoke of a SPOKEN turn still fills, because the scan there is
    this turn's text and that is the case the fallback exists for;
  * a caller who DOES answer on the completion-delivery turn is still captured, which is
    the whole point of asking a question during a slow call.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

import flows  # noqa: E402
from flows.engine import loader as fb  # noqa: E402

# The caller's LAST real utterance, one turn before the ones under test. It is about the
# fault, not about the offer -- and it contains "not", which the offer slot's plainly
# authored "no" cue matches. That collision is the point: a stale utterance is scored
# against a question it was never a reply to, so any cue set can lose.
STALE = "my laptop is not working and my tv is not working either"

OFFER_CUES = {"ACCEPT": ["yes", "sure", "go ahead"],
              "DECLINE": ["no", "not now", "rather not"]}


def _config():
  """A flow that asks something while a slow check runs behind it."""
  f = flows.Flow("support", root_agent="agent")
  f.add(flows.user_slot("order_id", ask="What is your order number?"))
  f.add(flows.intent_slot("try_it", OFFER_CUES,
                          ask="Want to give it a go?", cue_priority="first"))
  f.add(flows.result_slot("check", "slow_check"))
  f.task(flows.task("slow_check", tool="run_check", inputs=["order_id"],
                    out_slot="check", awaits={"max_turns": 20}))
  return flows.App(root_flow=f, app_display_name="t").root_flow.to_config()


def _sm(config):
  sm = fb.seed_sm(config)
  sm["filled"] = {"order_id": "A-1042"}
  sm["pending"] = {}
  # A session already under way: without this the engine treats the call as its FIRST
  # run and reads the scan as the routing utterance, which is a different (and
  # legitimate) path through the cue fill.
  sm["_config_id"] = "support"
  gate = sm.get("_gate_slot") or config.get("gate_slot")
  if gate:
    sm[gate] = "support"
    sm["filled"][gate] = "support"
  return sm


def _drive(config, sm, *, spoke="", scanned=STALE, inactivity=False,
           completion=False):
  return fb.load_engine().slot_filling_engine({
      "raw_config": config,
      "sm": sm,
      "last_user_text": spoke,
      "scanned_user_text": scanned,
      "is_inactivity": inactivity,
      "event_data": {},
      "config_id": "support",
      "n_user_turns": 2,
      "async_completion_landed": completion,
  })


def _value(out):
  return out["sm"]["filled"].get("try_it")


def test_a_completion_delivery_the_caller_said_nothing_on_does_not_fill():
  """THE REGRESSION. The turn belongs to the backend, not to the caller."""
  config, sm = _config(), None
  sm = _sm(config)
  assert _value(_drive(config, sm, completion=True)) is None


def test_the_guard_survives_the_within_turn_re_invoke():
  """The half that decides whether the fix is real.

  Neither flag outlives the turn's FIRST pass -- `is_inactivity` is recomputed per
  invocation and `async_completion_landed` counts what was ingested on THIS pass, which
  is nothing the second time. The engine re-invokes itself within the turn and runs the
  cue match again on every pass, so an unlatched guard holds once and the next pass
  fills the slot anyway. That later pass is where the measured hang-up came from: the
  first spoke the completion's own line and the one behind it read the stale words.
  """
  config = _config()
  sm = _sm(config)
  _drive(config, sm, completion=True)
  # Same turn, one pass later: the platform no longer reports a completion and the
  # caller has still not spoken.
  assert _value(_drive(config, sm)) is None


def test_an_inactivity_tick_does_not_fill():
  """Silence, by definition. `effective_user_text` already refuses the same fallback
  one stage later; the cue pass runs before it and was never given the guard."""
  config = _config()
  sm = _sm(config)
  assert _value(_drive(config, sm, inactivity=True)) is None


def test_an_answer_given_on_the_delivery_turn_is_still_captured():
  """The cost side. Asking a question during a slow call is the point of the primitive,
  and the caller who answers on the very turn the result lands must not be dropped."""
  config = _config()
  sm = _sm(config)
  assert _value(_drive(config, sm, spoke="yes", scanned="yes",
                       completion=True)) == "ACCEPT"


def test_a_spoken_turn_still_fills_from_the_scan_on_its_own_re_invoke():
  """Unchanged, and the reason the guard is not simply "never use the scan": on the
  re-invoke of a turn the caller DID speak on, the scan is that turn's own text."""
  config = _config()
  sm = _sm(config)
  _drive(config, sm, spoke="no thanks", scanned="no thanks")
  sm["filled"].pop("try_it", None)
  sm["pending"].pop("try_it", None)
  assert _value(_drive(config, sm, scanned="no thanks")) == "DECLINE"


def test_the_caller_speaking_again_releases_the_latch():
  """A held guard that never lets go would cost every later re-invoke its fallback. The
  next real utterance is what releases it."""
  config = _config()
  sm = _sm(config)
  _drive(config, sm, inactivity=True)
  assert _value(_drive(config, sm, spoke="sure", scanned="sure")) == "ACCEPT"
