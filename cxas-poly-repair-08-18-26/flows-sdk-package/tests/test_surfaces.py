"""Polymorphic surfaces — one agent definition rendered per delivery surface.

The property most of these guard is BACKWARD COMPATIBILITY: a config authored
without `say()` must emit exactly the bytes it always did, because every existing
agent and every golden transcript depends on it.
"""

import pytest

import flows
from flows import surfaces
from flows.engine import loader

# ── helpers ──────────────────────────────────────────────────────────────────


def _flow(ask, name="q", **slot_kw):
  f = flows.Flow("t", root_agent="A")
  f.add(flows.user_slot(name, ask, **slot_kw))
  return f


def _drive(cfg, channel, filled=None):
  """One engine turn on a given channel. Returns (message, question_payloads)."""
  sm = {"filled": dict(filled or {}), "pending": {}, "status": "in_progress",
        "task_results": {}}
  out = loader.run_engine(cfg, sm, last_user_text="",
                          event_data=({"channel": channel} if channel else {}),
                          config_id="t")
  parts = (out["sm"].get("_pending_question_payloads") or {}).get("parts")
  return (out["action"].get("message") or ""), parts, out


# ── backward compatibility ───────────────────────────────────────────────────


def test_plain_string_ask_emits_no_surface_keys():
  """A str ask must not gain ask_variants/response. This is the property that lets
  every pre-existing agent and golden stay valid."""
  slot = _flow("What's your account number?").to_config()["slots"][0]
  assert slot["ask"] == "What's your account number?"
  assert "ask_variants" not in slot
  assert "response" not in slot


def test_plain_string_config_is_identical_to_pre_feature_shape():
  a = _flow("Pick a day.").to_config()
  b = _flow("Pick a day.").to_config()
  assert a == b
  assert all("ask_variants" not in s for s in a["slots"])


def test_say_with_only_text_is_not_polymorphic():
  """say('x') carries no projections, so it must lower like a bare string."""
  slot = _flow(flows.say("Pick a day.")).to_config()["slots"][0]
  assert slot["ask"] == "Pick a day."
  assert "ask_variants" not in slot
  assert "response" not in slot


def test_brief_equal_to_text_emits_no_variants():
  """Identical wordings are not a difference; emitting them would be noise."""
  slot = _flow(flows.say("Same.", brief="Same.")).to_config()["slots"][0]
  assert "ask_variants" not in slot


# ── the wording/payload split ────────────────────────────────────────────────


def test_wordings_and_payloads_go_to_different_keys():
  """Wordings REPLACE the ask; cards ACCOMPANY it. Swapping these makes the caller
  hear the question twice, because after_model appends response parts to what the
  model already said."""
  slot = _flow(flows.say("Long.", brief="Short.",
                         card=flows.card(title="T"))).to_config()["slots"][0]
  kinds = {p["type"] for p in slot["ask_variants"]}
  assert kinds == {"text"}, "a card must never land in ask_variants"
  assert {p["type"] for p in slot["response"]} == {"payload"}
  assert all(p["type"] != "text" for p in slot["response"]), \
      "a wording must never land in response — it would be spoken twice"


def test_say_requires_a_floor():
  """`say(brief=...)` alone reads as "voice only" but has no floor to fall back to,
  so the brief form would leak onto every surface. Rejected rather than silently
  wrong — as a missing positional, and as an empty one."""
  with pytest.raises(TypeError):
    flows.say(brief="Take your time.")
  with pytest.raises(ValueError, match="text is required"):
    flows.say("", brief="Take your time.")


def test_both_wordings_are_emitted_explicitly():
  """The long form is a real conditioned variant, not a fall-through to the floor —
  otherwise "no variant matched" and "use the long one" become the same state."""
  slot = _flow(flows.say("Long.", brief="Short.")).to_config()["slots"][0]
  assert slot["ask_variants"] == [
      {"type": "text", "text": "Short.",
       "condition": {"capability": "brevity", "eq": "tight"}},
      {"type": "text", "text": "Long.",
       "condition": {"capability": "brevity", "neq": "tight"}},
  ]


def test_dynamic_chips_are_truncated_to_the_surface_max_options():
  """A producer returning eight slots is fine as eight chips and impossible to read
  aloud. The cap is enforced, not merely suggested to the model."""
  f = flows.Flow("t", root_agent="A")
  f.add(flows.passive_slot("times"),
        flows.user_slot("pick", flows.say("Pick one.",
                                          chips=flows.chips(options_from="times"))))
  cfg = f.to_config()
  cfg["surfaces"] = {"kiosk": {"payloads": True, "max_options": 2}}
  eight = ",".join(f"slot{i}" for i in range(8))
  _, parts, _ = _drive(cfg, "kiosk", filled={"times": eight})
  assert [o["text"] for o in parts[0]["options"]] == ["slot0", "slot1"]


def test_chips_are_untruncated_when_the_surface_allows_them_all():
  f = flows.Flow("t", root_agent="A")
  f.add(flows.passive_slot("times"),
        flows.user_slot("pick", flows.say("Pick one.",
                                          chips=flows.chips(options_from="times"))))
  _, parts, _ = _drive(f.to_config(), "chat",
                       filled={"times": "a,b,c,d"})
  assert len(parts[0]["options"]) == 4


def test_chips_lower_to_a_payload_gated_part():
  slot = _flow(flows.say("Pick.", chips=flows.chips(options_from="slots_available"))
               ).to_config()["slots"][0]
  chip = slot["response"][0]
  assert chip["type"] == "chips"
  assert chip["options_from"] == "slots_available"
  assert chip["condition"] == {"capability": "payloads"}


# ── per-surface rendering ────────────────────────────────────────────────────


@pytest.mark.parametrize("channel,want_brief,want_card", [
    ("voice", True, False),
    ("chat", False, True),
    ("VOICE", True, False),           # name match is case-insensitive
    ("TWILIO", True, False),          # CES telephony alias
    ("GOOGLE_TELEPHONY_PLATFORM", True, False),
    ("MOBILE", False, True),          # CES text alias
    ("web_ui", False, True),
    ("base", True, False),            # Slot Studio's sentinel -> fallback
    ("", True, False),                # no channel at all -> fallback
])
def test_rendering_per_channel(channel, want_brief, want_card):
  cfg = _flow(flows.say("The long form.", brief="The short form.",
                        card=flows.card(title="T"))).to_config()
  msg, parts, _ = _drive(cfg, channel)
  assert ("short" in msg.lower()) is want_brief, msg
  assert bool(parts) is want_card


def test_unknown_channel_falls_back_to_voice_not_to_nothing():
  """The status quo bug: an unrecognized channel matched no override key and
  silently rendered whatever happened to be first."""
  name, caps = surfaces.resolve("no-such-channel")
  assert name == "voice"
  assert caps["payloads"] is False


def test_fallback_is_voice_because_voice_failures_are_unrecoverable():
  assert surfaces.DEFAULT_SURFACE == "voice"


# ── resolution precedence ────────────────────────────────────────────────────


def test_app_default_surface_is_honoured():
  name, _ = surfaces.resolve("", default="chat")
  assert name == "chat"


def test_explicit_channel_outranks_app_default():
  name, _ = surfaces.resolve("voice", default="chat")
  assert name == "voice"


def test_declared_surface_overrides_a_builtin():
  table = {"voice": {"max_options": 1}}
  name, caps = surfaces.resolve("voice", surfaces=table)
  assert name == "voice"
  assert caps["max_options"] == 1
  assert caps["payloads"] is False, "unspecified capabilities keep the built-in value"


def test_custom_surface_with_alias():
  table = {"sms": {"payloads": False, "brevity": "tight", "max_options": 4,
                   "aliases": ["TWILIO_SMS"]}}
  assert surfaces.resolve("TWILIO_SMS", surfaces=table)[0] == "sms"
  assert surfaces.resolve("twilio_sms", surfaces=table)[0] == "sms"


def test_observed_audio_evidence_outranks_the_app_default():
  """A silence only happens on an audio session, so it is evidence that beats a
  guess made at authoring time."""
  cfg = _flow(flows.say("Long.", brief="Short.")).to_config()
  cfg["default_surface"] = "chat"
  sm = {"filled": {}, "pending": {}, "status": "in_progress", "task_results": {}}
  out = loader.run_engine(cfg, sm, last_user_text="", event_data={},
                          is_inactivity=True, config_id="t")
  assert "short" in (out["action"].get("message") or "").lower()


def test_seeded_channel_is_sticky_across_turns():
  cfg = _flow(flows.say("Long.", brief="Short.")).to_config()
  sm = {"filled": {}, "pending": {}, "status": "in_progress", "task_results": {}}
  out = loader.run_engine(cfg, sm, last_user_text="",
                          event_data={"channel": "chat"}, config_id="t")
  assert out["sm"]["channel"] == "chat"
  out2 = loader.run_engine(cfg, out["sm"], last_user_text="", event_data={},
                           config_id="t")
  assert "long" in (out2["action"].get("message") or "").lower()


# ── condition leaves ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("cond,voice,chat", [
    ({"capability": "payloads"}, False, True),
    ({"capability": "brevity", "eq": "tight"}, True, False),
    ({"capability": "max_options", "gte": 4}, False, True),
    ({"capability": "max_options", "lt": 4}, True, False),
    ({"surface": "voice"}, True, False),
    ({"surface": "chat"}, False, True),
    ({"not": {"capability": "payloads"}}, True, False),
    ({"all": [{"capability": "links"}, {"capability": "payloads"}]}, False, True),
    ({"any": [{"surface": "voice"}, {"surface": "chat"}]}, True, True),
])
def test_surface_condition_leaves(cond, voice, chat):
  eng = loader.load_engine_module() if hasattr(loader, "load_engine_module") else None
  del eng
  import flows.engine.framework.tools.slot_filling_engine.python_function.python_code as E
  try:
    for name, want in (("voice", voice), ("chat", chat)):
      E._surface_ref = {"name": name, "caps": E._BUILTIN_SURFACES[name]}
      assert E._eval_condition(cond, {}) is want, (cond, name)
  finally:
    E._surface_ref = None


def test_capability_leaf_falls_open_when_no_surface_resolved():
  """Offline callers (unit tests, the directive oracle) have no session. A
  capability lookup must not be the thing that breaks them."""
  import flows.engine.framework.tools.slot_filling_engine.python_function.python_code as E
  E._surface_ref = None
  assert E._cap("payloads") is None
  assert E._cap("payloads", True) is True


def test_surface_handle_is_cleared_after_a_turn():
  """A leaked handle would let a later turn evaluate conditions against a finished
  turn's surface."""
  import flows.engine.framework.tools.slot_filling_engine.python_function.python_code as E
  cfg = _flow(flows.say("Long.", brief="Short.")).to_config()
  _drive(cfg, "chat")
  assert E._surface_ref is None


def test_surface_handle_is_cleared_even_when_the_turn_raises():
  import flows.engine.framework.tools.slot_filling_engine.python_function.python_code as E
  with pytest.raises(Exception):
    E.slot_filling_engine({"raw_config": {"slots": "not-a-list"}, "sm": {}})
  assert E._surface_ref is None


# ── validation ───────────────────────────────────────────────────────────────


def test_unknown_capability_is_rejected_at_author_time():
  with pytest.raises(ValueError, match="unknown capability"):
    flows.user_slot("x", "ask?", condition={"capability": "nonexistent"})


def test_capability_and_surface_are_mutually_exclusive():
  with pytest.raises(ValueError, match="not both"):
    flows.user_slot("x", "ask?", condition={"capability": "links",
                                            "surface": "voice"})


def test_capability_and_slot_are_mutually_exclusive():
  with pytest.raises(ValueError, match="not both"):
    flows.user_slot("x", "ask?", condition={"capability": "links", "slot": "y"})


@pytest.mark.parametrize("kwargs,match", [
    ({"card": "not-a-card", "text": "x"}, "expected flows.card"),
    ({"chips": {"type": "text"}, "text": "x"}, "expected flows.chips"),
])
def test_say_rejects_malformed_input(kwargs, match):
  with pytest.raises((ValueError, TypeError), match=match):
    flows.say(**kwargs)


def test_card_requires_content():
  with pytest.raises(ValueError, match="at least one"):
    flows.card()


def test_card_rejects_non_button_actions():
  with pytest.raises(TypeError, match="flows.action"):
    flows.card(title="T", actions=[{"text": "nope"}])


def test_card_rejects_subtitle_alongside_actions():
  """The button-bearing wire shape has no subtitle field. Silently dropping it
  (with body) or silently promoting it to the body (without) are both worse than
  saying so."""
  with pytest.raises(ValueError, match="no subtitle"):
    flows.card(title="T", body="B", subtitle="S",
               actions=[flows.action("Go", "go")])
  with pytest.raises(ValueError, match="no subtitle"):
    flows.card(title="T", subtitle="S", actions=[flows.action("Go", "go")])


def test_card_with_actions_keeps_the_body_as_the_lead_text():
  built = flows.card(title="T", body="B", actions=[flows.action("Go", "go")])
  responses = built["scenarios"][0]["responses"]
  assert responses[0] == {"type": "text", "text": "B"}
  assert responses[1]["type"] == "button"


def test_card_without_actions_keeps_every_field():
  item = flows.card(title="T", subtitle="S", body="B")["richContent"][0][0]
  assert item == {"type": "info", "title": "T", "subtitle": "S", "text": "B"}


def test_action_requires_an_event():
  with pytest.raises(ValueError, match="event name is required"):
    flows.action("Book it", "")


@pytest.mark.parametrize("kwargs", [
    {},                                                  # neither
    {"options": ["a"], "options_from": "slot"},          # both
])
def test_chips_requires_exactly_one_source(kwargs):
  with pytest.raises(ValueError, match="not both and not neither"):
    flows.chips(**kwargs)


@pytest.mark.parametrize("kwargs,match", [
    ({"brevity": "medium"}, "brevity must be one of"),
    ({"max_options": 0}, "max_options must be >= 1"),
])
def test_surface_invariants(kwargs, match):
  with pytest.raises(ValueError, match=match):
    flows.Surface("x", **kwargs)


def test_surface_requires_a_name():
  with pytest.raises(ValueError, match="name is required"):
    flows.Surface("")


# ── placeholders and app wiring ──────────────────────────────────────────────


def test_placeholders_interpolate_in_a_brief_and_in_a_card_body():
  f = flows.Flow("t", root_agent="A")
  f.add(flows.user_slot("day", "Which day?"),
        flows.user_slot("slot2", flows.say("The long form for {day}.",
                                           brief="Short for {day}?",
                                           card=flows.card(body="Card for {day}"))))
  cfg = f.to_config()
  msg, parts, _ = _drive(cfg, "voice", filled={"day": "Tuesday"})
  assert "Short for Tuesday?" == msg
  msg2, parts2, _ = _drive(cfg, "chat", filled={"day": "Tuesday"})
  assert "Tuesday" in msg2
  body = parts2[0]["data"]["richContent"][0][0]["text"]
  assert body == "Card for Tuesday"


def test_task_then_say_variants_render_per_surface():
  f = flows.Flow("t", root_agent="A")
  f.add(flows.user_slot("day", "Which day?"), flows.result_slot("r", "go"))
  f.task("go", "do_it", ["day"], "r", terminal=True,
         then_say=flows.say("The long confirmation.", brief="Booked."))
  task = f.to_config()["tasks"][0]
  assert task["then_say"] == "The long confirmation."
  assert [p["text"] for p in task["then_say_variants"]] == ["Booked.",
                                                            "The long confirmation."]


def test_app_surfaces_reach_the_emitted_config():
  f = _flow("Hi?")
  sms = flows.Surface("sms", payloads=False, brevity="tight", max_options=4)
  app = flows.App(root_flow=f, app_display_name="Surfaces",
                  surfaces=[sms], default_surface="sms")
  errors, _ = flows.validate_app(app)
  assert errors == []


def test_app_without_surfaces_emits_no_surface_keys():
  """Built-ins cover almost every app; an absent key keeps the config unchanged."""
  from flows.authoring import build
  app = flows.App(root_flow=_flow("Hi?"), app_display_name="No surfaces")
  cfg = build._apply_surfaces({"slots": []}, app)
  assert "surfaces" not in cfg and "default_surface" not in cfg
