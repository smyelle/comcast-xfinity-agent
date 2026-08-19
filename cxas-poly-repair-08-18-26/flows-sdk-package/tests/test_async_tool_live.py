"""Turn + scenario tests for `examples/async_tool.py`, driven against a DEPLOYED app.

Everything in `test_async_tools.py` runs offline against the engine, which means it has
to hand the engine the platform's `{"result": "pending"}` placeholder itself. That is
the one thing worth not taking on trust: `poll_activation` is declared
`executionType: ASYNCHRONOUS`, and what CES actually does with that — defer the body,
substitute a placeholder, and deliver the real payload later as a synthetic user turn —
is a platform behaviour no offline harness can produce.

So these drive a real app over the real session API.

    export FLOWS_ASYNC_APP=projects/ces-deployment-dev/locations/us/apps/<id>
    pytest packages/flows/tests/test_async_tool_live.py -v

Skipped entirely when that variable is unset, so the offline suite stays offline and
CI does not need credentials.

Two shapes, and the split is not cosmetic. `poll_activation` sleeps 25 seconds on
purpose, so the concurrency is real rather than a race the backend happens to win —
which means the completion lands WHEN IT LANDS. The turn tests therefore stop before
it and assert only the deterministic half of the wait. Convergence needs the clock to
run, so it belongs to the scenario tests, which pace their turns and tolerate the
number of turns the payload takes to arrive.
"""

from __future__ import annotations

import os
import time

import pytest

APP = os.environ.get("FLOWS_ASYNC_APP", "")

# Per-app gates rather than one module-level `pytestmark`: this file drives TWO
# deployed apps (see the stale-consumer section at the bottom), and a module-level skip
# on FLOWS_ASYNC_APP would silently skip the other app's tests too — which it did.
async_only = pytest.mark.skipif(
    not APP, reason="set FLOWS_ASYNC_APP to a deployed app resource to run live tests")


# The tool's own sleep. Anything shorter and "did the flow keep working WHILE the
# backend worked" is not actually being tested.
POLL_SECONDS = 25


@pytest.fixture(scope="module")
def sessions():
  from cxas_scrapi.core.sessions import Sessions
  return Sessions(APP)


class Driver:
  """One conversation. Records every turn so assertions can look back over it."""

  def __init__(self, sessions):
    self._s = sessions
    self._id = sessions.create_session_id()
    self.turns: list[dict] = []

  def say(self, text: str, then_wait: float = 0.0) -> dict:
    out = self._s.get_structured_response(
        self._s.run(session_id=self._id, text=text, modality="text"))
    self.turns.append(out)
    if then_wait:
      # Real seconds on purpose: the backend is genuinely slow, and the point of the
      # primitive is what the flow does with that time.
      time.sleep(then_wait)
    return out

  def tools_called(self, name: str) -> int:
    return sum(1 for t in self.turns
               for c in (t.get("tool_calls") or []) if c.get("action") == name)

  def args_for(self, name: str) -> dict:
    for t in self.turns:
      for c in (t.get("tool_calls") or []):
        if c.get("action") == name:
          return dict(c.get("args") or {})
    return {}

  @property
  def transcript(self) -> str:
    return "\n".join((t.get("agent_text") or "") for t in self.turns)

  def ended(self) -> bool:
    return any(t.get("session_ended") for t in self.turns)


@pytest.fixture
def d(sessions):
  return Driver(sessions)


def _to_the_wait(d: Driver) -> dict:
  """Drive to the point where the poll is dispatched and outstanding."""
  d.say("I want to activate my phone")
  return d.say("9413")


# --------------------------------------------------------------------------- #
# Turn tests — the deterministic half of the wait
# --------------------------------------------------------------------------- #

@async_only
def test_the_placeholder_does_not_read_as_a_failure(d):
  """Without `awaits` this is the whole bug: the placeholder is falsy under
  success_check, routes into on_failure, and — max_retries defaulting to 0 — escalates
  the flow on the very first fire, with nothing actually failed."""
  fired = _to_the_wait(d)

  assert d.tools_called("poll_activation") == 1
  assert not d.ended(), "the flow ended on the turn the placeholder came back"
  assert "transfer_to_human" not in [
      c["action"] for t in d.turns for c in (t.get("tool_calls") or [])]
  assert fired["agent_text"].strip(), "the wait spoke nothing at all"


@async_only
def test_awaits_say_is_spoken_when_the_wait_starts(d):
  fired = _to_the_wait(d)
  assert "in progress" in fired["agent_text"].lower()


@async_only
def test_collection_continues_while_the_backend_works(d):
  """The headline. The wait blocks its own task, not the conversation — so the very
  next turn asks the one question that has no dependency on the poll."""
  _to_the_wait(d)
  asked = d.say("ok")

  assert "number" in asked["agent_text"].lower()
  assert d.tools_called("poll_activation") == 1, "the poll was re-dispatched"


@async_only
def test_while_waiting_speaks_once_there_is_nothing_left_to_ask(d):
  """With the callback number in and the poll still outstanding, this turn used to be
  dead air. The ladder covers exactly these turns."""
  _to_the_wait(d)
  d.say("ok")
  idle = d.say("555 0101")

  assert d.args_for("set_callback_number").get("callback_number", "").endswith("0101")
  assert idle["agent_text"].strip(), "the idle wait turn said nothing"
  assert "555" not in idle["agent_text"], "it re-asked instead of acknowledging the wait"


@async_only
def test_the_ladder_does_not_talk_over_the_question(d):
  """`while_waiting` is for dead air only. The turn that HAS a question to ask must
  ask it rather than speaking a reassurance line over it."""
  _to_the_wait(d)
  asked = d.say("ok")
  assert "number" in asked["agent_text"].lower()
  assert "still working" not in asked["agent_text"].lower()


@async_only
def test_the_poll_is_never_redispatched_while_outstanding(d):
  """The regression guard. A pending result is falsy under success_check, so the DAG's
  already-succeeded skip does NOT catch it — without the in-flight marker the engine
  re-fires the tool every single turn for as long as the backend takes."""
  _to_the_wait(d)
  for text in ("ok", "are you still there", "hello"):
    d.say(text)

  assert d.tools_called("poll_activation") == 1
  assert d.tools_called("start_activation") == 1


# --------------------------------------------------------------------------- #
# Scenario tests — paced, so the completion actually arrives
# --------------------------------------------------------------------------- #

def _drive_until(d: Driver, nudges: list[str], predicate, pace: float) -> bool:
  """Keep the call alive one nudge per turn until `predicate` holds."""
  for text in nudges:
    if predicate(d):
      return True
    d.say(text, then_wait=pace)
  return predicate(d)


@async_only
def test_the_flow_converges_when_the_backend_answers(d):
  """End to end: the completion lands as a synthetic user turn, its outputs map, and
  the terminal task fires with BOTH the polled result and the number collected during
  the wait."""
  _to_the_wait(d)
  d.say("ok")
  d.say("555 0101", then_wait=POLL_SECONDS * 0.5)

  converged = _drive_until(
      d,
      ["sure, I'll wait", "still there?", "any update?", "hello?", "how's it going?"],
      lambda dd: dd.tools_called("finish_activation") >= 1,
      pace=8.0)

  assert converged, (
      "finish_activation never fired; the completion either was not ingested or the "
      f"terminal stayed deferred.\ntranscript:\n{d.transcript}")

  args = d.args_for("finish_activation")
  assert args.get("status_msg"), "the polled result was not mapped into the terminal"
  assert args.get("callback_number", "").endswith("0101"), (
      "the slot collected DURING the wait did not reach the terminal")
  assert "activated" in d.transcript.lower()


@async_only
def test_the_terminal_line_comes_from_the_task_not_the_model(d):
  """The completion envelope is visible to the model in the transcript, so a passing
  transcript alone proves nothing — the model can simply read the payload back. The
  tool call is what distinguishes the framework doing the work from a paraphrase."""
  _to_the_wait(d)
  d.say("ok")
  d.say("555 0101", then_wait=POLL_SECONDS * 0.5)
  _drive_until(d, ["sure", "still there?", "any update?", "hello?", "and now?"],
               lambda dd: dd.tools_called("finish_activation") >= 1, pace=8.0)

  assert d.tools_called("finish_activation") >= 1, d.transcript
  # The terminal's then_say is "{closing}", which the tool builds — so the number has
  # to appear in the spoken line, not merely in the tool arguments.
  assert "0101" in d.transcript


@async_only
def test_a_waiting_caller_is_not_scored_off_topic(d):
  """"hello? are you there" during a wait is not off-topic. Left to the steer-back
  counter it would be, and six such turns escalate the flow — handing the caller to a
  human because the backend was slow."""
  _to_the_wait(d)
  d.say("ok")
  d.say("555 0101", then_wait=4.0)

  for text in ("hello?", "are you there?", "is this thing on?", "hello??", "anyone?"):
    out = d.say(text, then_wait=4.0)
    assert not out.get("agent_transfer"), f"transferred on {text!r}\n{d.transcript}"

  assert d.tools_called("transfer_to_human") == 0, d.transcript
  assert not d.ended(), "the flow gave up on a caller who was only checking in"


# --------------------------------------------------------------------------- #
# The stale-consumer guard, against its own deployed app
# --------------------------------------------------------------------------- #
#
#   export FLOWS_STALE_APP=projects/<project>/locations/us/apps/<id>
#
# built from examples/async_stale_consumer.py. Separate from FLOWS_ASYNC_APP because
# the scenario needs a slot that is ALREADY filled with a placeholder when the wait
# starts — the activation demo has no such slot, which is why this guard had no live
# coverage until now.

STALE_APP = os.environ.get("FLOWS_STALE_APP", "")

stale_only = pytest.mark.skipif(
    not STALE_APP, reason="set FLOWS_STALE_APP to the async_stale_consumer app")


@pytest.fixture(scope="module")
def stale_sessions():
  from cxas_scrapi.core.sessions import Sessions
  return Sessions(STALE_APP)


@pytest.fixture
def sd(stale_sessions):
  return Driver(stale_sessions)


@stale_only
def test_a_consumer_does_not_fire_on_the_placeholder_verdict(sd):
  """`quick_check` writes verdict="still checking" so the field is never empty, and
  `deep_scan` will replace it. `advise` reads verdict — and once contact_pref is in,
  BOTH its inputs are present, so nothing but the stale-output guard is stopping it
  from firing on the placeholder and reading it out as though it were the finding."""
  sd.say("my internet keeps dropping")
  sd.say("8069100230361049", then_wait=2.0)
  sd.say("text please", then_wait=2.0)

  assert sd.tools_called("quick_check") == 1
  assert sd.tools_called("deep_scan") == 1
  assert sd.tools_called("advise") == 0, (
      "advise fired while the scan was outstanding — on the placeholder verdict."
      f"\n{sd.transcript}")
  assert "still checking" not in sd.transcript.lower()


@stale_only
def test_collection_is_not_stalled_by_the_guard(sd):
  """The guard is scoped to task eligibility, not slot collection: an unrelated
  question must still be asked while the scan runs."""
  sd.say("my internet keeps dropping")
  sd.say("8069100230361049", then_wait=2.0)
  sd.say("text please", then_wait=2.0)
  assert sd.args_for("set_contact_pref").get("contact_pref"), sd.transcript


@stale_only
def test_the_consumer_fires_on_the_REAL_verdict_once_it_lands(sd):
  """End to end: the guard must hold, and then release on the real value."""
  sd.say("my internet keeps dropping")
  sd.say("8069100230361049", then_wait=2.0)
  sd.say("text please", then_wait=POLL_SECONDS * 0.5)

  _drive_until(sd, ["you there?", "any news?", "hello?", "and now?", "still there?"],
               lambda d: d.tools_called("advise") >= 1, pace=8.0)

  assert sd.tools_called("advise") == 1, sd.transcript
  verdict = sd.args_for("advise").get("verdict", "")
  assert "splitter" in verdict, f"advise ran on the placeholder: {verdict!r}"
  assert "still checking" not in sd.transcript.lower()
