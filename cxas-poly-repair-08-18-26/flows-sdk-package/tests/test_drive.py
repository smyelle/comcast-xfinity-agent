"""Driving a CUJ: session seeding + the tool-fakes flag, with a stub session.

No network and no Slot Studio import — `session_factory` is the seam for that.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests
"""

from __future__ import annotations

import pytest
import yaml

import flows
from flows import drive

FILE = {
    "variable_aliases": {"account": ["accountNumber", "account_id"]},
    "cujs": {"reboot": {"description": "Reboot offered.",
                        "variables": {"account": "8069100230361003"}}},
}


class FakeSessions:
  def __init__(self):
    self.calls = []

  def run(self, **kw):
    self.calls.append(kw)
    return {}


class FakeTurn:
  def __init__(self, text):
    self.agent_text = text
    self.tool_calls = [{"action": "reboot"}]


class FakeSession:
  def __init__(self, app_name, initial_variable_state=None, **kw):
    self.app_name = app_name
    self.seeded = initial_variable_state
    self.sent = []
    self.is_ended = False
    self._sessions = FakeSessions()

  def send(self, text):
    self.sent.append(text)
    self._sessions.run(text=text)
    return FakeTurn("Would you like us to reboot your device now?")


@pytest.fixture
def cujs_file(tmp_path):
  path = tmp_path / "cujs.yaml"
  path.write_text(yaml.safe_dump(FILE))
  return str(path)


def _factory(captured):
  def make(app_name, initial_variable_state=None, **kw):
    s = FakeSession(app_name, initial_variable_state, **kw)
    captured.append(s)
    return s
  return make


def test_open_session_seeds_the_cuj_variables(cujs_file):
  made = []
  cuj = flows.load_cujs(cujs_file)["reboot"]
  drive.open_session(cuj, "abc-123", session_factory=_factory(made))

  assert made[0].seeded == {"accountNumber": "8069100230361003",
                            "account_id": "8069100230361003"}
  assert made[0].app_name == "projects/ces-deployment-dev/locations/us/apps/abc-123"


def test_tool_fakes_flag_reaches_the_transport(cujs_file):
  made = []
  s = drive.open_session({"a": "1"}, "abc-123", session_factory=_factory(made))
  s.send("hi")
  assert s._sessions.calls[0]["use_tool_fakes"] is True


def test_tool_fakes_can_be_turned_off(cujs_file):
  s = drive.open_session({"a": "1"}, "abc-123", session_factory=_factory([]),
                         use_tool_fakes=False)
  s.send("hi")
  assert "use_tool_fakes" not in s._sessions.calls[0]


def test_a_full_resource_name_is_passed_through():
  made = []
  full = "projects/p/locations/l/apps/x"
  drive.open_session({"a": "1"}, full, session_factory=_factory(made))
  assert made[0].app_name == full


def test_run_steps_returns_what_the_agent_said(cujs_file):
  results = drive.run_steps("reboot", "abc-123", ["my internet is down"],
                            session=FakeSession("app"))
  assert results[0].utterance == "my internet is down"
  assert "reboot your device" in results[0].text
  assert results[0].tool_calls == ["reboot"]


def test_collapse_mirror_undoubles_text():
  doubled = "Your parcel arrives Thursday. Your parcel arrives Thursday."
  assert drive.collapse_mirror(doubled) == "Your parcel arrives Thursday."
  assert drive.collapse_mirror("Hello there.") == "Hello there."
  assert drive.collapse_mirror("") == ""


def test_collapse_mirror_leaves_short_repeats_alone():
  # A real agent turn can be "bye bye"; halving it is a mangling, not a fix.
  for short in ("bye bye", "yes yes", "no no"):
    assert drive.collapse_mirror(short) == short


def test_chat_accepts_a_plain_dict(cujs_file, capsys):
  made = []
  # Regression: chat() used to hand open_session the *resolved* CUJ, which is None
  # for a bare dict, so this path raised instead of driving.
  assert drive.chat({"accountNumber": "1"}, "abc-123", say=["hello"],
                    session_factory=_factory(made)) == 0
  assert made[0].seeded == {"accountNumber": "1"}
  assert "reboot your device" in capsys.readouterr().out


def test_chat_scripted_with_a_named_cuj(cujs_file, monkeypatch, capsys):
  monkeypatch.setenv("FLOWS_CUJS", cujs_file)
  made = []
  assert drive.chat("reboot", "abc-123", say=["hello"],
                    session_factory=_factory(made)) == 0
  out = capsys.readouterr().out
  assert "CUJ: reboot — Reboot offered." in out
  assert made[0].seeded["accountNumber"] == "8069100230361003"


def test_run_steps_stops_at_an_ended_session():
  session = FakeSession("app")
  session.is_ended = True
  results = drive.run_steps({"a": "1"}, "abc", ["one", "two"], session=session)
  assert len(results) == 1 and session.sent == []
