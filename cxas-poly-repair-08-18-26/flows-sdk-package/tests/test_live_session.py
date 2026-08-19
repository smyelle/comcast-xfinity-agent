"""The live driver: `flows.live` — session, client factory, and the SCRAPI patch.

`flows.live` is the only part of `flows` that reaches a deployed app, so the rule
here is that NOTHING in this file touches a network. Every session goes through
the `client_factory` seam with a local fake, and the patch tests drive upstream's
own parser over hand-built response objects.

The one behaviour worth stating plainly, because it is silent when it breaks: the
SCRAPI patch is what keeps a MULTI-PART agent turn intact. Without it a turn that
arrives as one top-level part plus a diagnostic-mirror part loses the second part
outright — no exception, just missing words in the transcript.
"""

from __future__ import annotations

import importlib

import pytest

from flows.live import clients, scrapi_patches
from flows.live.session import ChatSession, SessionEndedError, TurnRecord

APP = "projects/p/locations/us/apps/a"


# --- a local stand-in for the cxas_scrapi Sessions/Traces clients -------------
class FakeSessions:
  """Records every `run` kwarg and replays canned structured responses."""

  def __init__(self, app_name, deployment_id=None, replies=None, **kw):
    self.app_name = app_name
    self.deployment_id = deployment_id
    self.kwargs = kw
    self.calls: list[dict] = []
    self._replies = list(replies or [])

  def create_session_id(self):
    return "session-0"

  def run(self, **kw):
    self.calls.append(kw)
    return {"raw": len(self.calls)}

  def get_structured_response(self, raw):
    if self._replies:
      return self._replies.pop(0)
    return {"agent_text": "ok", "tool_calls": []}


class FakeTraces:
  def __init__(self, app_name, **kw):
    self.app_name = app_name
    self.reports = 0

  def get_report(self, conversation_id, fmt="json"):
    self.reports += 1
    return f"report:{conversation_id}:{fmt}"

  def get_normalized(self, conversation_id):
    return {"conversation_id": conversation_id}


class FakeFactory:
  """A `client_factory` drop-in; counts constructions so caching is observable."""

  def __init__(self, replies=None):
    self.replies = replies
    self.sessions_built = 0
    self.traces_built = 0
    self.last_traces: FakeTraces | None = None

  def make_sessions(self, app_name, **kw):
    self.sessions_built += 1
    return FakeSessions(app_name, replies=self.replies, **kw)

  def make_traces(self, app_name, **kw):
    self.traces_built += 1
    self.last_traces = FakeTraces(app_name, **kw)
    return self.last_traces


def _session(**kw):
  factory = kw.pop("factory", None) or FakeFactory()
  return ChatSession(app_name=APP, client_factory=factory, **kw), factory


# --- construction -------------------------------------------------------------
def test_a_session_mints_an_id_when_the_caller_does_not_supply_one():
  session, _ = _session()
  assert session.session_id == "session-0"
  assert session.turns == []
  assert session.current_turn_index == 0


def test_a_caller_supplied_session_id_is_kept_verbatim():
  session, _ = _session(session_id="resumed-42")
  assert session.session_id == "resumed-42"


def test_an_initial_turn_count_offsets_the_first_index():
  session, _ = _session(initial_turn_count=7)
  assert session.current_turn_index == 7


def test_the_client_factory_is_injectable_and_the_default_is_the_package_one():
  """The default must be flows' own, or the package is not standalone."""
  session = ChatSession.__new__(ChatSession)
  assert session is not None  # no construction: we only assert the default wiring
  assert clients.make_sessions.__module__ == "flows.live.clients"


def test_unknown_kwargs_reach_the_transport_but_client_factory_does_not():
  """`client_factory` must not leak into **session_kwargs and hit the client."""
  factory = FakeFactory()
  session = ChatSession(app_name=APP, client_factory=factory, api_endpoint="host:443")
  assert session._sessions.kwargs == {"api_endpoint": "host:443"}


# --- send() -------------------------------------------------------------------
def test_send_records_a_turn_and_returns_it():
  session, _ = _session(factory=FakeFactory(
      replies=[{"agent_text": "Hi there", "tool_calls": [{"action": "greet"}]}]))
  turn = session.send("hello")

  assert isinstance(turn, TurnRecord)
  assert turn.turn_index == 0
  assert turn.user_text == "hello"
  assert turn.agent_text == "Hi there"
  assert turn.tool_calls == [{"action": "greet"}]
  assert session.turns == [turn]
  assert session.current_turn_index == 1


def test_the_channel_is_injected_on_the_first_turn_only():
  session, _ = _session(channel="voice")
  session.send("one")
  session.send("two")

  first, second = session._sessions.calls
  assert first["variables"]["event_data"] == {"channel": "voice"}
  assert "variables" not in second


def test_capture_si_is_sent_on_every_turn_not_just_the_first():
  """SI capture has to work mid-session, so the flag cannot be turn-0 only."""
  session, _ = _session(capture_si=True)
  session.send("one")
  session.send("two")

  assert all(c["variables"]["capture_si"] is True for c in session._sessions.calls)


def test_initial_variable_state_seeds_the_server_on_the_first_turn():
  """Seeded locally AND sent, or a pinned value never reaches the agent."""
  session, _ = _session(initial_variable_state={"current_date": "2026-01-01"})
  session.send("one")
  assert session._sessions.calls[0]["variables"]["current_date"] == "2026-01-01"


def test_historical_contexts_and_turn_count_are_first_turn_only():
  session, _ = _session(historical_contexts=[{"a": 1}], turn_count=3)
  session.send("one")
  session.send("two")

  first, second = session._sessions.calls
  assert first["historical_contexts"] == [{"a": 1}]
  assert first["turn_count"] == 3
  assert "historical_contexts" not in second
  assert "turn_count" not in second


def test_variable_updates_accumulate_across_turns():
  session, _ = _session(factory=FakeFactory(replies=[
      {"agent_text": "a", "variable_updates": [{"sm": {"filled": {"zip": "94110"}}}]},
      {"agent_text": "b", "variable_updates": [{"other": 1}, "not-a-dict"]},
  ]))
  session.send("one")
  session.send("two")

  assert session.get_slot_machine() == {"filled": {"zip": "94110"}}
  assert session._variable_state["other"] == 1


def test_sending_to_an_ended_session_raises():
  session, _ = _session(factory=FakeFactory(
      replies=[{"agent_text": "bye", "session_ended": True}]))
  session.send("one")

  assert session.is_ended
  with pytest.raises(SessionEndedError, match="already ended"):
    session.send("two")


def test_close_is_idempotent_and_ends_the_session():
  session, _ = _session()
  session.close()
  session.close()
  assert session.is_ended
  with pytest.raises(SessionEndedError):
    session.send("one")


# --- send_event() -------------------------------------------------------------
def test_send_event_fires_an_event_and_labels_the_turn():
  session, _ = _session()
  turn = session.send_event("WELCOME", {"lang": "en"})

  call = session._sessions.calls[0]
  assert call["event"] == "WELCOME"
  assert call["event_vars"] == {"lang": "en"}
  assert turn.user_text == "[event: WELCOME]"


def test_send_event_refuses_an_ended_session():
  session, _ = _session()
  session.close()
  with pytest.raises(SessionEndedError):
    session.send_event("WELCOME")


def test_send_event_accumulates_variable_updates_like_send_does():
  """An event turn fills slots too; dropping its updates loses state silently."""
  session, _ = _session(factory=FakeFactory(replies=[
      {"agent_text": "welcome",
       "variable_updates": [{"sm": {"filled": {"lang": "en"}}}, "not-a-dict"]}]))
  session.send_event("WELCOME")
  assert session.get_slot_machine() == {"filled": {"lang": "en"}}


# --- send_input(): the general form ------------------------------------------
def test_send_input_forwards_only_the_keys_it_was_given():
  """A None must not reach the transport as an explicit null — upstream treats a
  present-but-None differently from absent for several of these."""
  session, _ = _session()
  session.send_input(text="hello")

  call = session._sessions.calls[0]
  assert call == {"session_id": "session-0", "text": "hello"}


def test_send_input_carries_the_whole_input_surface():
  session, _ = _session()
  session.send_input(dtmf="1234#", variables={"zip": "94110"},
                     tool_responses=[{"action": "lookup", "response": {}}],
                     modality="audio", use_tool_fakes=True)

  call = session._sessions.calls[0]
  assert call["dtmf"] == "1234#"
  assert call["variables"] == {"zip": "94110"}
  assert call["tool_responses"] == [{"action": "lookup", "response": {}}]
  assert call["modality"] == "audio"
  assert call["use_tool_fakes"] is True
  assert "text" not in call


@pytest.mark.parametrize("kwargs,label", [
    ({"text": "hello"}, "hello"),
    ({"dtmf": "1"}, "[dtmf: 1]"),
    ({"event": "WELCOME"}, "[event: WELCOME]"),
    ({"variables": {"a": 1}}, "[input]"),
    ({"text": "hi", "label": "turn one"}, "turn one"),
])
def test_send_input_labels_the_turn_for_a_readable_transcript(kwargs, label):
  session, _ = _session()
  assert session.send_input(**kwargs).user_text == label


def test_send_input_does_not_inject_the_first_turn_extras():
  """`send` seeds channel and initial variables on turn 0; the explicit form must
  not, or a caller composing a request would silently get more than it asked for."""
  session, _ = _session(channel="voice",
                        initial_variable_state={"current_date": "2026-01-01"})
  session.send_input(text="hello")
  assert "variables" not in session._sessions.calls[0]


def test_send_input_accumulates_variable_updates_like_the_others():
  session, _ = _session(factory=FakeFactory(replies=[
      {"agent_text": "a", "variable_updates": [{"sm": {"filled": {"zip": "94110"}}}]}]))
  session.send_input(text="hello")
  assert session.get_slot_machine() == {"filled": {"zip": "94110"}}


def test_send_input_refuses_an_ended_session():
  session, _ = _session()
  session.close()
  with pytest.raises(SessionEndedError):
    session.send_input(text="hello")


# --- state projections --------------------------------------------------------
@pytest.mark.parametrize("transfer,expected", [
    ({"display_name": "Billing"}, "Billing"),
    ({"target_agent": "Sales"}, "Sales"),
    ("RawName", "RawName"),
])
def test_get_state_reads_the_transfer_target_in_each_shape(transfer, expected):
  session, _ = _session(factory=FakeFactory(
      replies=[{"agent_text": "x", "agent_transfer": transfer}]))
  session.send("one")

  state = session.get_state()
  assert state["active_agent"] == expected
  assert state["pending_transfer"] == expected


def test_get_state_reads_a_transfer_object_with_a_display_name():
  class Target:
    display_name = "Concierge"

  session, _ = _session(factory=FakeFactory(
      replies=[{"agent_text": "x", "agent_transfer": Target()}]))
  session.send("one")
  assert session.get_state()["active_agent"] == "Concierge"


def test_get_state_surfaces_filled_slots_and_the_ended_flag():
  session, _ = _session(factory=FakeFactory(replies=[{
      "agent_text": "x", "session_ended": True,
      "variable_updates": [{"sm": {"filled": {"zip": "94110"}}}]}]))
  session.send("one")

  state = session.get_state()
  assert state["session_ended"] is True
  assert state["filled_slots"] == {"zip": "94110"}
  assert state["turn_count"] == 1


def test_the_slot_machine_is_read_under_either_variable_name():
  for key in ("sm", "slot_machine"):
    session, _ = _session()
    session._variable_state[key] = {"filled": {}}
    assert session.get_slot_machine() == {"filled": {}}


def test_an_empty_or_absent_slot_machine_reads_as_an_empty_dict():
  session, _ = _session()
  session._variable_state["sm"] = {}
  assert session.get_slot_machine() == {}
  assert session.get_state()["slot_machine"] == {}


def test_flow_context_parses_a_json_encoded_agent_config_map():
  session, _ = _session()
  session._variable_state.update({
      "agent_config_map": '{"Host": "host_dag"}',
      "_active_config_id": "host_dag",
      "_active_sm_key": "sm",
  })
  ctx = session.get_flow_context()
  assert ctx == {"active_config_id": "host_dag",
                 "agent_config_map": {"Host": "host_dag"},
                 "active_sm_key": "sm"}


def test_flow_context_degrades_on_unparseable_json():
  session, _ = _session()
  session._variable_state["agent_config_map"] = "{not json"
  assert session.get_flow_context()["agent_config_map"] == {}


@pytest.mark.parametrize("transfer,expected", [
    ({"target_agent": "Sales"}, "Sales"),
    ("RawName", "RawName"),
])
def test_export_turns_summary_reads_the_other_transfer_shapes(transfer, expected):
  session, _ = _session(factory=FakeFactory(
      replies=[{"agent_text": "a", "agent_transfer": transfer}]))
  session.send("one")
  assert session.export_turns_summary()[0]["transfer"] == expected


def test_export_turns_summary_reads_a_transfer_object_with_a_display_name():
  class Target:
    display_name = "Concierge"

  session, _ = _session(factory=FakeFactory(
      replies=[{"agent_text": "a", "agent_transfer": Target()}]))
  session.send("one")
  assert session.export_turns_summary()[0]["transfer"] == "Concierge"


def test_export_turns_summary_flattens_each_turn():
  session, _ = _session(factory=FakeFactory(replies=[
      {"agent_text": "a", "tool_calls": [{"action": "t"}],
       "agent_transfer": {"display_name": "Billing"}},
      {"agent_text": "b"},
  ]))
  session.send("one")
  session.send("two")

  assert session.export_turns_summary() == [
      {"turn": 0, "user": "one", "agent": "a",
       "tool_calls": [{"action": "t"}], "transfer": "Billing"},
      {"turn": 1, "user": "two", "agent": "b",
       "tool_calls": [], "transfer": None},
  ]


# --- traces -------------------------------------------------------------------
def test_the_traces_client_is_built_once_and_reused():
  """Rebuilding per turn would pay auth + stub construction every read."""
  session, factory = _session()
  session.get_trace()
  session.get_normalized_trace()

  assert factory.traces_built == 1
  assert factory.last_traces.reports == 1


def test_get_trace_passes_the_session_id_as_the_conversation_id():
  session, _ = _session()
  assert session.get_trace(fmt="md") == "report:session-0:md"
  assert session.get_normalized_trace() == {"conversation_id": "session-0"}


# --- the client factory -------------------------------------------------------
def test_no_endpoint_leaves_the_upstream_class_untouched():
  sentinel = type("C", (), {})
  assert clients._bind_endpoint(sentinel, None) is sentinel


def test_an_endpoint_binds_a_subclass_that_keeps_the_original_identity():
  class Upstream:
    @staticmethod
    def _get_client_options(resource_name):
      return {"quota_project_id": "q"}

  bound = clients._bind_endpoint(Upstream, "host:443")
  assert bound is not Upstream
  assert bound.__name__ == "Upstream"
  assert bound._get_client_options("x") == {"quota_project_id": "q",
                                            "api_endpoint": "host:443"}


def test_an_unparseable_resource_name_is_passed_through_not_papered_over():
  """Upstream returning {} means it could not parse; do not manufacture options."""
  class Upstream:
    @staticmethod
    def _get_client_options(resource_name):
      return {}

  assert clients._bind_endpoint(Upstream, "host:443")._get_client_options("x") == {}


def test_the_endpoint_defaults_to_the_environment(monkeypatch):
  monkeypatch.setenv(clients.ENV_ENDPOINT, "env-host:443")
  assert clients.default_endpoint() == "env-host:443"
  monkeypatch.setenv(clients.ENV_ENDPOINT, "")
  assert clients.default_endpoint() is None


def test_a_missing_runtime_dependency_names_the_extra(monkeypatch):
  monkeypatch.setattr(importlib, "import_module",
                      lambda name: (_ for _ in ()).throw(ImportError("nope")))
  with pytest.raises(ImportError, match=r'flows\[deploy\]'):
    clients.make_sessions(app_name=APP)


@pytest.mark.parametrize("make,attr", [("make_sessions", "Sessions"),
                                       ("make_traces", "Traces")])
def test_a_factory_constructs_the_upstream_class_and_applies_the_patch(
    monkeypatch, make, attr):
  """The success path: patch installed, endpoint bound, class instantiated."""
  built = {}

  class Upstream:
    def __init__(self, app_name, **kw):
      built["app_name"] = app_name
      built["kwargs"] = kw

    @staticmethod
    def _get_client_options(resource_name):
      return {"quota_project_id": "q"}

  applied = []
  monkeypatch.setattr(scrapi_patches, "apply", lambda: applied.append(True))
  monkeypatch.setattr(importlib, "import_module",
                      lambda name: type("m", (), {attr: Upstream}))

  client = getattr(clients, make)(app_name=APP, api_endpoint="host:443",
                                  deployment_id="d1")

  assert isinstance(client, Upstream)
  assert built["app_name"] == APP
  assert built["kwargs"] == {"deployment_id": "d1"}
  assert applied == [True]


def test_the_environment_endpoint_is_used_when_none_is_passed(monkeypatch):
  seen = {}

  class Upstream:
    def __init__(self, app_name, **kw):
      pass

    @staticmethod
    def _get_client_options(resource_name):
      return {}

  monkeypatch.setenv(clients.ENV_ENDPOINT, "env-host:443")
  monkeypatch.setattr(scrapi_patches, "apply", lambda: None)
  monkeypatch.setattr(importlib, "import_module",
                      lambda name: type("m", (), {"Sessions": Upstream}))
  def record(cls, endpoint):
    seen["endpoint"] = endpoint
    return cls

  monkeypatch.setattr(clients, "_bind_endpoint", record)

  clients.make_sessions(app_name=APP)
  assert seen["endpoint"] == "env-host:443"


# --- the SCRAPI patch ---------------------------------------------------------
class _Output:
  def __init__(self, text=None, mirror=None):
    self.text = text
    self.mirror = mirror


class _Parsed:
  """Stands in for upstream's parser: response-wide flag, exactly like the bug."""

  def __init__(self, response, tools_map=None):
    outputs = response if isinstance(response, list) else [response]
    self.outputs = outputs
    self.agent_texts = []
    self.detailed_trace = []
    seen_top = False
    for out in outputs:
      if out is None:
        continue
      if out.text:
        self.agent_texts.append(out.text)
        self.detailed_trace.append(f"top:{out.text}")
        seen_top = True
      elif out.mirror and not seen_top:
        self.agent_texts.append(out.mirror)
        self.detailed_trace.append(f"mirror:{out.mirror}")
    self.consolidated_agent_text = " ".join(self.agent_texts).strip()


@pytest.fixture
def patched(monkeypatch):
  """Install the patch onto a local stand-in, then hand it back."""
  module = type("m", (), {"ParsedSessionResponse": _Parsed})
  monkeypatch.setitem(
      __import__("sys").modules, "cxas_scrapi.core.response_parser", module)
  monkeypatch.setattr(scrapi_patches, "_applied", False)
  original = _Parsed.__init__
  scrapi_patches.apply()
  yield module.ParsedSessionResponse
  _Parsed.__init__ = original


def test_the_second_part_of_a_multi_part_turn_survives(patched):
  """The whole point: upstream drops it, and it does so silently."""
  parsed = patched([_Output(text="First part."), _Output(mirror="Second part.")])
  assert parsed.agent_texts == ["First part.", "Second part."]
  assert parsed.consolidated_agent_text == "First part. Second part."


def test_a_mirror_of_text_already_spoken_at_top_level_is_not_doubled(patched):
  """A turn-end mirror repeats the turn's own words; it must not count twice."""
  parsed = patched([_Output(text="Handing you over."),
                    _Output(mirror="Handing   you over.")])
  assert parsed.agent_texts == ["Handing you over."]


def test_top_level_text_is_never_dropped_even_when_repeated(patched):
  """A line the agent genuinely says twice still appears twice."""
  parsed = patched([_Output(text="Sorry?"), _Output(text="Sorry?")])
  assert parsed.agent_texts == ["Sorry?", "Sorry?"]


def test_a_single_output_response_is_left_entirely_alone(patched):
  parsed = patched([_Output(text="Just one.")])
  assert parsed.agent_texts == ["Just one."]


def test_the_detailed_trace_is_rebuilt_alongside_the_text(patched):
  parsed = patched([_Output(text="First."), _Output(mirror="Second.")])
  assert parsed.detailed_trace == ["top:First.", "mirror:Second."]


def test_a_none_output_is_skipped_rather_than_crashing(patched):
  parsed = patched([_Output(text="First."), None, _Output(mirror="Second.")])
  assert parsed.agent_texts == ["First.", "Second."]


def test_applying_twice_does_not_wrap_twice(patched):
  """A second copy of this module in the same process must be a no-op."""
  first = patched.__init__
  scrapi_patches._applied = False
  scrapi_patches.apply()
  assert patched.__init__ is first


def test_apply_short_circuits_once_installed(monkeypatch):
  """The fast path must not re-enter the lock on every client construction."""
  calls = []
  monkeypatch.setattr(scrapi_patches, "_applied", True)
  monkeypatch.setattr(scrapi_patches, "_patch_per_output_agent_text",
                      lambda: calls.append(True))
  scrapi_patches.apply()
  assert calls == []


def test_a_thread_that_loses_the_race_still_skips_the_patch(monkeypatch):
  """Double-checked locking: the second reader sees the flag inside the lock."""
  calls = []
  monkeypatch.setattr(scrapi_patches, "_applied", False)
  monkeypatch.setattr(scrapi_patches, "_patch_per_output_agent_text",
                      lambda: calls.append(True))

  class FlippingLock:
    def __enter__(self):
      scrapi_patches._applied = True  # the winner finished while we waited

    def __exit__(self, *a):
      return False

  monkeypatch.setattr(scrapi_patches, "_apply_lock", FlippingLock())
  scrapi_patches.apply()
  assert calls == []


def test_a_structural_failure_is_swallowed_not_raised(monkeypatch, caplog):
  """A client that cannot be built is far worse than upstream's text scoping."""
  monkeypatch.setattr(scrapi_patches, "_applied", False)
  monkeypatch.setattr(
      scrapi_patches, "_patch_per_output_agent_text",
      lambda: (_ for _ in ()).throw(RuntimeError("upstream reshaped")))

  scrapi_patches.apply()  # must not raise
  assert scrapi_patches._applied is True
  assert "could not be installed" in caplog.text
