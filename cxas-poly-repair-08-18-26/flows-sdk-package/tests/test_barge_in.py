"""Barge-in: knowing what the caller heard, and reacting to it.

Measured behavior this is built on (ces-probes 161/162, live on gemini-composite-v1 and
gemini-3.1-flash-live). When the caller talks over the agent the platform truncates the
speech -- always, config or no config. If the app declares
`audioProcessingConfig.bargeInConfig.bargeInAwareness: true`, it also prefixes the NEXT
user turn with:

    <context>agent speaking was interrupted. user only heard '<prefix>' in the last agent
     response.</context> <what the caller actually said>

where the quoted string is a VERBATIM PREFIX of what the caller heard. Nothing else
reports it: the agent's own history still records the full text it intended to say, and
`Event.interrupted` exists on the event type but is never set.

The defect that motivates all of it is the announce CASCADE. It speaks every announce it
can reach as ONE response and latches each one delivered as it goes, so a caller who
barges during announce #1 of four never hears #2-#4 and all four are recorded as spoken.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_barge_in.py
"""

from __future__ import annotations

import flows
from flows.authoring import continuers
from flows.engine import loader as _loader

# The wire shape, verbatim from the live runs.
ENVELOPE = ("<context>agent speaking was interrupted. user only heard '{heard}' in the "
            "last agent response.</context>")

PARTS = [
    "Before we continue I need to read you the terms.",
    "Calls are recorded for training.",
    "Your data is retained for ninety days.",
    "You can opt out at any time.",
]


# --- the envelope parser (S1) --------------------------------------------------
def _extract_barge(text):
  """What before_model lifts, via the engine's twin matcher."""
  eng = _loader.load_engine(None)
  match = eng._BARGE_ENVELOPE.search(text)  # noqa: SLF001
  return (bool(match), (match.group("heard") or "").strip() if match else "")


def test_the_envelope_is_recognized_and_the_prefix_lifted():
  hit, heard = _extract_barge(ENVELOPE.format(heard=PARTS[0]) + " stop stop stop")
  assert hit and heard == PARTS[0]


def test_a_bodyless_envelope_still_reports_the_interruption():
  """The prefix is what degrades, not the fact. A wrapper we cannot read the body of
  still means the caller was cut off, which is enough to fire the policy."""
  hit, heard = _extract_barge("<context>agent speaking was interrupted.</context> hello")
  assert hit and heard == ""


def test_curly_quotes_are_accepted():
  """The prefix is prose the PLATFORM composes, not a field we control."""
  hit, heard = _extract_barge(
      "<context>agent speaking was interrupted. user only heard ‘abc def’ in "
      "the last agent response.</context>")
  assert hit and heard == "abc def"


def test_the_other_context_envelopes_are_not_barge_ins():
  assert _extract_barge("<context>no user activity detected for 5 seconds.</context>") \
      == (False, "")
  assert _extract_barge("<context>user pressed 1 on keypad.</context>") == (False, "")


def test_prose_mentioning_the_phrase_is_not_an_envelope():
  """Anchored on the whole wrapper, so a caller quoting the words cannot forge one."""
  hit, _ = _extract_barge("the agent speaking was interrupted, honestly")
  assert not hit


# --- the split and the ledger (S2) ---------------------------------------------
def _eng():
  return _loader.load_engine(None)


def _ledger(parts=PARTS, slot="terms"):
  return [{"slot": slot, "i": i, "text": t} for i, t in enumerate(parts)]


def test_the_cascade_case_marks_everything_after_the_cut_unspoken():
  """THE defect. Cut inside announce part 1: parts 2-4 were never started, and they need
  no text analysis at all to replay -- which is the whole reason the ledger is per part."""
  rows = _eng()._classify_ledger(  # noqa: SLF001
      _ledger(), "Before we continue I need to read you the")
  assert [r["state"] for r in rows] == ["cut", "unspoken", "unspoken", "unspoken"]


def test_a_cut_in_a_later_part_leaves_the_earlier_ones_heard():
  rows = _eng()._classify_ledger(  # noqa: SLF001
      _ledger(), PARTS[0] + " Calls are recor")
  assert [r["state"] for r in rows] == ["heard", "cut", "unspoken", "unspoken"]


def test_heard_nothing_and_heard_everything_are_both_representable():
  assert [r["state"] for r in _eng()._classify_ledger(_ledger(), "")] == [  # noqa: SLF001
      "unspoken"] * 4
  assert [r["state"] for r in _eng()._classify_ledger(  # noqa: SLF001
      _ledger(), " ".join(PARTS))] == ["heard"] * 4


def test_the_split_is_exact_on_a_literal_prefix():
  out = _eng()._split_heard(PARTS[0], "Before we continue I need to read you the")  # noqa: SLF001
  assert out["exact"] and out["unheard"] == "terms."


def test_the_split_survives_transcription_drift():
  """The prefix comes back through the platform's own transcription, so casing and
  punctuation will not match what we sent."""
  out = _eng()._split_heard(PARTS[0], "before we continue i need to read you the")  # noqa: SLF001
  assert out["exact"] and out["unheard"] == "terms."


def test_the_split_refuses_to_guess_when_the_text_diverges():
  """Withholding {unheard} is the safe failure. A confidently wrong "you missed X" is
  worse than saying the line again."""
  out = _eng()._split_heard(PARTS[0], "something else entirely, unrelated")  # noqa: SLF001
  assert not out["exact"] and out["unheard"] == ""


def test_the_split_handles_an_empty_prefix():
  out = _eng()._split_heard(PARTS[0], "")  # noqa: SLF001
  assert not out["exact"] and out["unheard"] == ""


# --- following-along cues (S3) -------------------------------------------------
def test_backchannels_are_recognized():
  for text in ("mhmm", "mm-hmm", "uh huh", "Got it.", "gotcha", "Okay", "right",
               "I see", "makes sense", "go on", "I'm with you", "that's good",
               "yeah, ok, got it"):
    assert continuers.is_continuer(text), text


def test_a_continuer_with_substance_attached_is_not_a_continuer():
  """The trap autofill.py records for the agent side, in its caller-side form: a
  bag-of-words gate eats a real question that merely opens with a noise."""
  for text in ("mhm but what about the fee?", "okay book it for tuesday",
               "right, so my account number is 12345",
               "sounds good but can you repeat the last bit"):
    assert not continuers.is_continuer(text), text


def test_yes_and_no_are_not_continuers():
  """They are answers first. The pending-slot check cannot rescue a yes/no that arrives
  when no slot is pending, so they stay out of the vocabulary entirely."""
  assert not continuers.is_continuer("yes")
  assert not continuers.is_continuer("no")


def test_an_author_can_widen_or_replace_the_vocabulary():
  assert not continuers.is_continuer("keep it coming")
  assert continuers.is_continuer("keep it coming", extra=["keep it coming"])
  assert not continuers.is_continuer("mhmm", phrases=["only this"])


def test_the_engine_copy_of_the_vocabulary_matches_the_authoring_one():
  """Duplicated verbatim because a CES tool cannot import the authoring package -- the
  same constraint that duplicates the _KEEP_* registries into slot_intake."""
  assert _eng()._DEFAULT_CONTINUERS == continuers.DEFAULT_CONTINUER_PHRASES  # noqa: SLF001
  assert _eng()._MAX_CONTINUER_WORDS == continuers.MAX_CONTINUER_WORDS  # noqa: SLF001


def test_the_two_matchers_agree_on_every_default_phrase_and_a_decoy_set():
  eng = _eng()
  for text in list(continuers.DEFAULT_CONTINUER_PHRASES) + [
      "mhm but what about the fee", "yes", "no", "book it for tuesday", ""]:
    assert eng._is_continuer(text) == continuers.is_continuer(text), text  # noqa: SLF001


# --- the authoring surface (S3/S4) ---------------------------------------------
def test_repair_rejects_an_unknown_mode():
  """A typo'd mode must not silently degrade to doing nothing on a live call."""
  try:
    flows.repair(mode="whatever")
  except ValueError as exc:
    assert "parts" in str(exc)
  else:
    raise AssertionError("expected ValueError")


def test_announce_carries_its_repair_spec():
  a = flows.announce("terms", PARTS, repair=flows.repair(lead_in="As I was saying —"))
  assert a["repair"]["mode"] == "parts"
  assert a["repair"]["lead_in"] == "As I was saying —"


def test_an_announce_without_repair_is_unchanged():
  """The opt-in promise: an agent that does not ask for this behaves exactly as before."""
  assert "repair" not in flows.announce("terms", PARTS)


def test_the_policies_attach_to_a_flow():
  f = flows.Flow("d", root_agent="A")
  f.set("continue_cues", flows.continue_cues(extra=["keep going"]))
  f.set("on_interrupted", flows.on_interrupted(say="You missed {unheard}"))


def test_on_interrupted_carries_the_action_arms():
  policy = flows.on_interrupted(say="x", then="escalate", open_slot="offer")
  assert policy["then"] == "escalate" and policy["open_slot"] == "offer"


# --- end to end through the engine ---------------------------------------------
def _disclosure_flow(repair=None, on_interrupted=None, cues=None):
  """Four announces the DAG will cascade into ONE response, then a question."""
  f = flows.Flow("t", root_agent="A")
  for i, text in enumerate(PARTS):
    f.add(flows.announce(f"p{i}", [text], **({"repair": repair} if repair else {})))
  f.add(flows.user_slot("acct", "What is your account number?"))
  if on_interrupted is not None:
    f.set("on_interrupted", on_interrupted)
  if cues is not None:
    f.set("continue_cues", cues)
  return f


def _sm():
  return {"filled": {}, "pending": {}, "status": "in_progress", "task_results": {}}


def _turn(cfg, sm, text="", **kw):
  """One engine turn -> (everything the caller would hear, sm).

  An announce's `texts` become RESPONSE parts (`_pending_announce_payloads`), while a
  question or a preempt travels as `message`. A repair replay is a preempt, so a test that
  reads only one channel sees half the conversation.
  """
  out = _loader.run_engine(cfg, sm, last_user_text=text, config_id="t", **kw)
  new_sm = out["sm"]
  # Live, `after_model` POPS the announce payloads as it delivers them. Offline nobody
  # does, so a test that only reads them sees last turn's lines again on this turn and
  # every replay assertion passes for the wrong reason.
  spoken = [p.get("text", "") for p in
            (new_sm.pop("_pending_announce_payloads", None) or [])
            if isinstance(p, dict) and p.get("type") == "text"]
  message = out["action"].get("message") or ""
  if message:
    spoken.append(message)
  return " ".join(s for s in spoken if s), new_sm


def test_the_cascade_speaks_everything_then_asks():
  """The baseline the interruption cases are read against."""
  msg, sm = _turn(_disclosure_flow(repair=flows.repair()).to_config(), _sm())
  for part in PARTS:
    assert part in msg


def test_an_interrupted_cascade_replays_only_what_was_missed():
  """THE case. Cut inside part 1: parts 2-4 were never spoken, and today they are lost
  forever because all four announces are latched delivered."""
  cfg = _disclosure_flow(repair=flows.repair(lead_in="As I was saying —")).to_config()
  sm = _sm()
  first, sm = _turn(cfg, sm)
  assert PARTS[3] in first

  replay, sm = _turn(cfg, sm, "mhmm", is_barge_in=True,
                     barge_heard="Before we continue I need to read you the")

  assert replay.startswith("As I was saying —")
  assert PARTS[1] in replay and PARTS[2] in replay and PARTS[3] in replay
  # The part the caller already heard most of is not read back at them.
  assert replay.count("Before we continue") <= 1


def test_replay_does_not_clear_the_announce_latches():
  """Replay reads a recording; it must never re-enter the cascade. Clearing `filled`
  would re-fire every downstream gate."""
  cfg = _disclosure_flow(repair=flows.repair()).to_config()
  sm = _sm()
  _, sm = _turn(cfg, sm)
  latched = {k: v for k, v in sm["filled"].items() if k.startswith("p")}
  _, sm = _turn(cfg, sm, "mhmm", is_barge_in=True, barge_heard=PARTS[0])
  assert {k: v for k, v in sm["filled"].items() if k.startswith("p")} == latched


def test_an_announce_without_repair_replays_nothing():
  """The opt-in promise, driven rather than asserted on the config."""
  cfg = _disclosure_flow().to_config()
  sm = _sm()
  _, sm = _turn(cfg, sm)
  replay, _ = _turn(cfg, sm, "mhmm", is_barge_in=True, barge_heard=PARTS[0])
  assert PARTS[1] not in replay and PARTS[2] not in replay


def test_repeated_interruptions_stop_at_max_repairs():
  """A caller who talks over every attempt must not be told the same thing forever."""
  cfg = _disclosure_flow(repair=flows.repair(max_repairs=1)).to_config()
  sm = _sm()
  _, sm = _turn(cfg, sm)
  first, sm = _turn(cfg, sm, "mhmm", is_barge_in=True, barge_heard=PARTS[0])
  assert PARTS[2] in first
  second, sm = _turn(cfg, sm, "mhmm", is_barge_in=True, barge_heard=PARTS[0])
  assert PARTS[2] not in second


def test_on_interrupted_speaks_when_there_is_nothing_to_repair():
  cfg = _disclosure_flow(
      on_interrupted=flows.on_interrupted(say_unknown="Sorry, let me repeat that.")
  ).to_config()
  sm = _sm()
  _, sm = _turn(cfg, sm)
  msg, _ = _turn(cfg, sm, "wait", is_barge_in=True, barge_heard=PARTS[0])
  assert "let me repeat that" in msg.lower()


def test_say_unknown_is_used_when_the_split_cannot_be_trusted():
  """{unheard} is withheld rather than guessed when the prefix does not line up."""
  cfg = _disclosure_flow(
      on_interrupted=flows.on_interrupted(
          say="You missed: {unheard}", say_unknown="Let me say that again.")
  ).to_config()
  sm = _sm()
  _, sm = _turn(cfg, sm)
  msg, _ = _turn(cfg, sm, "wait", is_barge_in=True,
                 barge_heard="completely unrelated words here")
  assert msg.startswith("Let me say that again.")
  assert "You missed" not in msg


def test_on_interrupted_can_open_a_slot_so_authors_can_gate_on_it():
  cfg = _disclosure_flow(
      on_interrupted=flows.on_interrupted(open_slot="was_cut")).to_config()
  sm = _sm()
  _, sm = _turn(cfg, sm)
  _, sm = _turn(cfg, sm, "wait", is_barge_in=True, barge_heard=PARTS[0])
  assert sm["filled"].get("was_cut") is True


def test_a_backchannel_does_not_count_as_a_stall():
  """Today a bare "mhm" fills nothing, so steer-back counts it and enough of them
  escalate the call. Agreeing with the agent should not push the caller to a human."""
  cfg = _disclosure_flow().to_config()
  sm = _sm()
  _, sm = _turn(cfg, sm)
  for _ in range(3):
    _, sm = _turn(cfg, sm, "mhmm")
  assert sm.get("_steer_back_turns", 0) == 0
  assert sm.get("_continuer") is True


def test_a_real_answer_is_never_swallowed_as_a_backchannel():
  """The pending slot wins. "okay" is in the vocabulary, so this is the case that
  decides whether the feature is safe to have on by default."""
  cfg = _disclosure_flow().to_config()
  sm = _sm()
  _, sm = _turn(cfg, sm)
  _, sm = _turn(cfg, sm, "my account is 4021")
  assert not sm.get("_continuer")


def test_continue_cues_can_be_switched_off_per_flow():
  cfg = _disclosure_flow(cues=flows.continue_cues(enabled=False)).to_config()
  sm = _sm()
  _, sm = _turn(cfg, sm)
  _, sm = _turn(cfg, sm, "mhmm")
  assert not sm.get("_continuer")


def test_the_engine_parses_the_raw_envelope_when_no_scalars_are_supplied():
  """The offline path: the simulator never loads the callbacks package, so the engine's
  own matcher has to see the barge. Same split as DTMF, for the same reason."""
  cfg = _disclosure_flow(repair=flows.repair()).to_config()
  sm = _sm()
  _, sm = _turn(cfg, sm)
  replay, _ = _turn(cfg, sm, ENVELOPE.format(heard=PARTS[0]) + " mhmm")
  assert PARTS[1] in replay and PARTS[2] in replay


# --- the skipped-sentence regression -------------------------------------------
# Found by asking "are we skipping parts of a sentence?" of a live transcript. We were
# skipping a WHOLE one, silently, and the demo had been doing it all along.
WELCOME = "Thanks for calling Northwind. I can open a new account for you."


def test_a_non_repairable_announce_still_occupies_the_heard_prefix():
  """THE bug. The reported prefix covers everything the caller heard, including an
  announce that declared no `repair=`. Leaving that announce out of the ledger shifts the
  boundary by its whole length and marks the NEXT line heard when nobody heard it."""
  full = [{"slot": "w", "i": 0, "text": WELCOME}] + [
      {"slot": f"p{i}", "i": i + 1, "text": t} for i, t in enumerate(PARTS)]
  rows = _eng()._classify_ledger(full, WELCOME + " Before we continue I")  # noqa: SLF001
  assert [r["state"] for r in rows] == [
      "heard", "cut", "unspoken", "unspoken", "unspoken"]


def test_a_misaligned_prefix_replays_more_rather_than_dropping_a_line():
  """When the reported prefix does not line up with what we recorded, the boundary falls
  back to the common prefix -- always the SHORTER answer. Repeating a line the caller
  already heard is noticeable and harmless; dropping one they did not is neither."""
  rows = _eng()._classify_ledger(_ledger(), WELCOME + " Before we continue I")  # noqa: SLF001
  assert [r["state"] for r in rows] == ["unspoken"] * 4


def test_the_ledger_records_every_announce_once_any_of_them_asks_for_repair():
  """Repairability is decided at replay, not at recording: the offsets are only right
  when the recording covers all the speech."""
  f = flows.Flow("t", root_agent="A")
  f.add(flows.announce("welcome", [WELCOME], preempt=True))          # no repair=
  f.add(flows.announce("terms", [PARTS[0]], preempt=True, repair=flows.repair()))
  f.add(flows.user_slot("acct", "What is your account number?"))
  _, sm = _turn(f.to_config(), _sm())
  assert [r["slot"] for r in sm["_said_parts"]] == ["welcome", "terms"]


def test_a_flow_with_no_repair_anywhere_writes_no_ledger():
  """The opt-out promise survives the fix: nothing recorded, nothing changed."""
  f = flows.Flow("t", root_agent="A")
  f.add(flows.announce("welcome", [WELCOME], preempt=True))
  f.add(flows.user_slot("acct", "What is your account number?"))
  _, sm = _turn(f.to_config(), _sm())
  assert not sm.get("_said_parts")


def test_the_non_repairable_announce_is_not_itself_replayed():
  """It is recorded so the offsets work, NOT so it gets re-spoken."""
  f = flows.Flow("t", root_agent="A")
  f.add(flows.announce("welcome", [WELCOME], preempt=True))
  for i, text in enumerate(PARTS):
    f.add(flows.announce(f"p{i}", [text], preempt=True, repair=flows.repair()))
  f.add(flows.user_slot("acct", "What is your account number?"))
  cfg = f.to_config()
  sm = _sm()
  _, sm = _turn(cfg, sm)
  replay, _ = _turn(cfg, sm, "mhmm", is_barge_in=True, barge_heard=WELCOME)
  assert "Thanks for calling Northwind" not in replay
  assert PARTS[0] in replay
