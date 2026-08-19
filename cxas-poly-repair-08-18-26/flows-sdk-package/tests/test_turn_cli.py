"""`flows turn` and `flows chat --json` — the machine-readable drive surface.

A coding agent drives an agent from a shell, where every invocation is its own
process. That needs two things nothing here had: a turn you can RESUME by session id,
and one JSON object on stdout with nothing else mixed into it.

Everything goes through the `session_factory` seam with a local fake, so no test here
reaches a network.
"""

from __future__ import annotations

import json

import pytest

from flows import cli, drive

APP = "a3497427-3072-4047-9068-144dc9d212d6"
RESOURCE = f"projects/ces-deployment-dev/locations/us/apps/{APP}"


class FakeRecord:
  def __init__(self, label, reply, tools):
    self.user_text = label
    self.agent_text = reply
    self.tool_calls = [{"action": t, "args": {}} for t in tools]
    self.tool_responses: list = []
    self.agent_transfer = None
    self.session_ended = False
    self.payloads: list = []


class FakeTransport:
  """Stands in for the scrapi client `open_session` reaches through to set fakes."""

  def run(self, **kw):
    return {}


class FakeSession:
  """Records the send_input kwargs so the CLI's request shaping is assertable."""

  instances: list["FakeSession"] = []

  def __init__(self, app_name, session_id=None, **kw):
    self.app_name = app_name
    self.session_id = session_id or "minted-session"
    self.sent: list[dict] = []
    self.is_ended = False
    self._sessions = FakeTransport()
    FakeSession.instances.append(self)

  def send_input(self, **kw):
    self.sent.append(kw)
    label = kw.get("label") or kw.get("text") or (
        f"[dtmf: {kw['dtmf']}]" if kw.get("dtmf") else f"[event: {kw.get('event')}]")
    return FakeRecord(label, "Sure thing.", ["set_topic"])

  def send(self, text):
    return self.send_input(text=text)

  def get_state(self):
    return {"filled_slots": {"topic": "billing"}}


@pytest.fixture(autouse=True)
def _fake_driver(monkeypatch):
  FakeSession.instances = []
  monkeypatch.setattr(drive, "_default_session_factory", lambda: FakeSession)
  yield


def run(argv: list[str]) -> int:
  return cli.main(argv)


# --- drive.turn ---------------------------------------------------------------
def test_turn_expands_a_bare_uuid_and_returns_the_session_to_resume():
  out = drive.turn(APP, text="hello")
  assert out["app"] == RESOURCE
  assert out["session_id"] == "minted-session"
  assert out["agent_text"] == "Sure thing."
  assert out["tool_calls"] == [{"action": "set_topic", "args": {}}]
  assert out["filled_slots"] == {"topic": "billing"}


def test_turn_resumes_the_session_it_is_given():
  drive.turn(APP, text="hello", session_id="carried-over")
  assert FakeSession.instances[0].session_id == "carried-over"


def test_turn_collapses_a_mirrored_reply(monkeypatch):
  """Some apps emit the agent line doubled; a driver that passes it through makes
  two runs incomparable.

  The doubling only collapses past a length floor, so a genuinely repeated short
  line ("bye bye") is left alone — hence a realistic sentence here rather than a
  two-word one.
  """
  line = "Your appointment is confirmed for Tuesday."

  class Doubled(FakeSession):
    def send_input(self, **kw):
      r = super().send_input(**kw)
      r.agent_text = f"{line} {line}"
      return r

  monkeypatch.setattr(drive, "_default_session_factory", lambda: Doubled)
  assert drive.turn(APP, text="x")["agent_text"] == line


def test_turn_forwards_only_what_was_asked_for():
  drive.turn(APP, dtmf="1", variables={"zip": "94110"})
  sent = FakeSession.instances[0].sent[0]
  assert sent["dtmf"] == "1"
  assert sent["variables"] == {"zip": "94110"}
  assert sent["text"] is None and sent["event"] is None


# --- flows turn ---------------------------------------------------------------
def test_json_mode_puts_exactly_one_object_on_stdout(capsys):
  assert run(["turn", "--app", APP, "--text", "hello", "--json"]) == 0
  captured = capsys.readouterr()
  payload = json.loads(captured.out)
  assert payload["agent_text"] == "Sure thing."
  assert payload["session_id"] == "minted-session"
  assert captured.out.count("{") >= 1


def test_human_mode_keeps_stdout_to_the_reply_and_the_rest_on_stderr(capsys):
  """A shell pipeline reads stdout. Session ids and tool lists are commentary."""
  assert run(["turn", "--app", APP, "--text", "hello"]) == 0
  captured = capsys.readouterr()
  assert captured.out.strip() == "agent > Sure thing."
  assert "session: minted-session" in captured.err
  assert "set_topic" in captured.err


def test_dtmf_and_event_are_each_a_valid_input(capsys):
  assert run(["turn", "--app", APP, "--dtmf", "1", "--json"]) == 0
  assert json.loads(capsys.readouterr().out)["input"] == "[dtmf: 1]"

  assert run(["turn", "--app", APP, "--event", "WELCOME", "--json"]) == 0
  assert json.loads(capsys.readouterr().out)["input"] == "[event: WELCOME]"


@pytest.mark.parametrize("argv", [
    ["turn", "--app", APP],
    ["turn", "--app", APP, "--text", "hi", "--dtmf", "1"],
])
def test_exactly_one_input_is_required(argv):
  with pytest.raises(SystemExit, match="exactly one"):
    run(argv)


def test_an_empty_text_is_a_real_input(capsys):
  """The turn a SILENT caller produces. Rejecting it made the one behavior a
  no_input ladder exists for the one behavior you could not rehearse."""
  assert run(["turn", "--app", APP, "--text", "", "--json"]) == 0
  sent = FakeSession.instances[0].sent[0]
  assert sent["text"] == ""


def test_variables_decode_json_and_fall_back_to_a_plain_string():
  run(["turn", "--app", APP, "--text", "hi",
       "--var", "zip=94110", "--var", 'opts={"a": 1}', "--var", "name=Ada", "--json"])
  sent = FakeSession.instances[0].sent[0]
  assert sent["variables"] == {"zip": 94110, "opts": {"a": 1}, "name": "Ada"}


def test_event_vars_ride_on_their_own_channel():
  run(["turn", "--app", APP, "--event", "WELCOME", "--event-var", "lang=en", "--json"])
  sent = FakeSession.instances[0].sent[0]
  assert sent["event_vars"] == {"lang": "en"}
  assert sent.get("variables") is None


def test_a_malformed_key_value_is_a_message_not_a_traceback():
  with pytest.raises(SystemExit, match="expected k=v"):
    run(["turn", "--app", APP, "--text", "hi", "--var", "novalue"])


def test_fakes_are_on_unless_refused():
  run(["turn", "--app", APP, "--text", "hi", "--json"])
  assert FakeSession.instances[0].sent[0]["use_tool_fakes"] is True

  FakeSession.instances.clear()
  run(["turn", "--app", APP, "--text", "hi", "--no-fakes", "--json"])
  assert FakeSession.instances[0].sent[0]["use_tool_fakes"] is None


def test_a_missing_runtime_dependency_exits_three(monkeypatch, capsys):
  """Exit 3 is the environment tier — distinct from a usage error or a bad turn."""
  def explode():
    raise ImportError("needs cxas-scrapi")

  monkeypatch.setattr(drive, "_default_session_factory", explode)
  assert run(["turn", "--app", APP, "--text", "hi", "--json"]) == 3
  assert "cxas-scrapi" in capsys.readouterr().err


# --- flows chat ---------------------------------------------------------------
def test_chat_no_longer_demands_a_cuj(capsys):
  """An app with no cujs.yaml was previously undrivable from the CLI."""
  assert run(["chat", "--app", APP, "--say", "hello", "--json"]) == 0
  payload = json.loads(capsys.readouterr().out)
  assert [t["input"] for t in payload["turns"]] == ["hello"]
  assert payload["session_id"] == "minted-session"


def test_chat_json_reports_every_turn_and_the_end_state(capsys):
  assert run(["chat", "--app", APP, "--say", "one", "--say", "two", "--json"]) == 0
  payload = json.loads(capsys.readouterr().out)
  assert [t["input"] for t in payload["turns"]] == ["one", "two"]
  assert payload["turns"][0]["tool_calls"] == ["set_topic"]
  assert payload["filled_slots"] == {"topic": "billing"}
  assert payload["session_ended"] is False


def test_chat_json_refuses_a_repl():
  """A REPL has no result to serialize; failing loudly beats emitting `{}`."""
  with pytest.raises(SystemExit, match="needs at least one --say"):
    run(["chat", "--app", APP, "--json"])
