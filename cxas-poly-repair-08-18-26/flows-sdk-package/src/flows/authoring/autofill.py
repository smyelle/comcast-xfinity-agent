"""Build-time automatic fillers — hoist a contentless opener into ``filler_say``.

Authors routinely write the filler and then put it in the wrong place: at the front
of the line spoken AFTER the tool returns.

    then_say="Thanks for holding. Your balance is {bal}."

"Thanks for holding." is fixed, carries nothing, and is spoken once the holding is
over. Moved to ``filler_say`` it rides the dispatching preempt instead, so the tool's
round trip is speech rather than dead air.

The hard constraint is that a filler is DROPPABLE. The chat surface skips it, a pool
entry may be silence, the engine allows one per caller turn, and the reasoning-pass
budget cuts it off deep in a ladder. So a hoisted fragment must never carry
information: losing it has to cost a pleasantry, never a fact.

That is why detection is a closed list of PHRASES rather than a heuristic. A miss
costs nothing — the text stays exactly as authored. A false positive puts a claim in
front of the tool that was supposed to substantiate it. The two errors are not
symmetric, so this module only recognizes what it has been explicitly taught.

An earlier draft matched a vocabulary of allowed WORDS instead, which leaked badly:
every word of "All good.", "No." and "We will take it back." is individually an
acknowledgement, so a bag-of-words gate hoists all three. "No." is an answer, and the
other two are promises about what the backend did. Whole phrases are matched here for
that reason — a combination nobody reviewed is not a combination this pass will speak.
"""

from __future__ import annotations

import re
import string
from typing import Iterable, NamedTuple, Optional

# Phrases that may be spoken before the work is done. A candidate opener matches if it
# appears here whole, or if every one of its comma-separated clauses does — so "Okay,
# let me check that for you." passes as `okay` + `let me check that for you`.
#
# Deliberately absent: anything that reports an OUTCOME ("all set", "done", "all
# good"), and anything that ANSWERS ("no", "yes"). Both read as acknowledgement and
# both assert something the tool has not said yet. Add a phrase only if you would be
# happy for it to be dropped silently, because on a chat surface it will be.
ACK_PHRASES: frozenset[str] = frozenset({
    # acknowledgement
    "ok", "okay", "alright", "all right", "sure", "certainly", "absolutely",
    "great", "perfect", "excellent", "wonderful", "fantastic", "lovely",
    "got it", "understood", "noted", "gotcha", "sure thing", "of course",
    # thanks
    "thanks", "thank you", "thanks for that", "thank you for that",
    "thanks for holding", "thank you for holding", "thanks for waiting",
    "thank you for waiting", "thanks for your patience",
    "thank you for your patience", "i appreciate that",
    # hold / wait
    "one moment", "one moment please", "just a moment", "a moment please",
    "one second", "just a second", "one sec", "just a sec",
    "give me a moment", "give me one moment", "give me a second",
    "hang tight", "bear with me", "please hold", "hold on", "please hold on",
    # A trailing courtesy clause: "One moment, please."
    "please",
    # look it up
    "let me check", "let me check that", "let me check that for you",
    "let me take a look", "let me have a look", "let me look into that",
    "let me look that up", "let me pull that up", "let me see",
    "i'll take a look", "i'll check that", "i'll look into that",
    "i'll be right back", "i'll be right back with you",
    # politeness
    "no problem", "no worries", "my pleasure", "happy to help",
})

# A backstop on top of the phrase list: eight words covers "Okay, let me check that for
# you." with room to spare, and caps how far a widened vocabulary can reach.
MAX_ACK_WORDS = 8

# The character twin of MAX_ACK_WORDS, checked first because it is O(1) and the parses
# after it are not. Deliberately loose: the longest phrase the list holds is 32
# characters, so nothing legitimate comes close.
MAX_ACK_CHARS = 150

_PUNCT_TO_STRIP = "".join(c for c in string.punctuation if c != "'")

_CURRENCY = "$€£¥₹"

# A sentence ends at .!? followed by whitespace. Naive on purpose: the phrase gate
# runs afterwards, so an abbreviation like "Mr. Smith called." cannot survive it
# ("mr"/"smith"/"called" are not acknowledgements). The splitter never has to be
# smarter than the vocabulary allows.
_SENTENCE_END = re.compile(r"[.!?]+(?=\s)")

_WORD = re.compile(r"[a-z][a-z'’]*")


class Hoist(NamedTuple):
  """A text split into a droppable opener and the substance that follows."""

  filler: str
  separator: str
  remainder: str

  def rejoin(self) -> str:
    """The original string, exactly. The split never edits, only cuts."""
    return self.filler + self.separator + self.remainder


def _tokens(text: str) -> list[str]:
  return _WORD.findall(text.lower().replace("’", "'"))


def _has_placeholder(text: str) -> bool:
  """True when the sentence interpolates anything, so it is not fixed copy.

  A malformed format string counts as a placeholder: the engine would fail to render
  it, and either way it is not something to speak before the values exist.
  """
  try:
    return any(field is not None
               for _, field, _, _ in string.Formatter().parse(text))
  except ValueError:
    return True


def _normalize(clause: str) -> str:
  """A clause reduced to comparable form: lowercase, unpunctuated, single-spaced."""
  cleaned = clause.lower().replace("’", "'").translate(
      str.maketrans(_PUNCT_TO_STRIP, " " * len(_PUNCT_TO_STRIP)))
  return " ".join(cleaned.split())


def allowed_phrases(extra_ack: Iterable[str] = ()) -> frozenset[str]:
  """The framework phrases widened by an app's own acknowledgements.

  Additive only. An app may teach the pass that "righto" is a hold phrase; it can
  never relax the structural gates, so the worst a bad entry can do is hoist one more
  contentless phrase.
  """
  return ACK_PHRASES | {_normalize(p) for p in extra_ack}


def split_leading_filler(
    text: object,
    *,
    extra_ack: Iterable[str] = (),
) -> Optional[Hoist]:
  """Split a droppable opener off ``text``, or return None to leave it alone.

  Returns None unless EVERY gate passes: the value is a plain string, a first sentence
  splits off with substance behind it, that sentence interpolates nothing, is short,
  carries no digits or currency, and it is a known acknowledgement phrase — either
  whole, or as comma-separated clauses that are each known.

  Only the FIRST sentence moves. `"Okay. One moment. Your balance is {b}."` hoists
  `"Okay."` and leaves `"One moment."` to be spoken after the tool answers, which is
  not what the author meant. Join them with a comma — `"Okay, one moment. Your balance
  is {b}."` — and the whole opener moves as one.
  """
  if not isinstance(text, str):
    return None  # a say() object or a ladder — the caller decides what that means

  match = _SENTENCE_END.search(text)
  if match is None:
    return None  # one sentence, or no terminator: nothing to split

  filler = text[:match.end()]
  rest = text[match.end():]
  remainder = rest.lstrip()
  if not remainder:
    return None  # the opener IS the line; hoisting it would leave the turn silent
  separator = rest[:len(rest) - len(remainder)]

  # Cheap bound before the format parse and the tokenizer. It is a backstop for
  # MAX_ACK_WORDS rather than a second opinion on length — the longest phrase the list
  # holds is 32 characters — so it is set generously and only exists to keep a
  # pathological single sentence from making the build do real work.
  if len(filler) > MAX_ACK_CHARS:
    return None

  if _has_placeholder(filler):
    return None
  if any(c.isdigit() for c in filler) or any(c in _CURRENCY for c in filler):
    return None

  words = _tokens(filler)
  if not words or len(words) > MAX_ACK_WORDS:
    return None

  # Whole sentence first, then clause by clause. Both are needed: splitting on commas
  # is what lets "Okay, let me check that for you." pass as two known phrases, but it
  # would also break a registered phrase that CONTAINS a comma — `extra_ack=["righto,
  # mate"]` normalizes to one entry and would never be compared against one.
  known = allowed_phrases(extra_ack)
  if _normalize(filler) not in known:
    clauses = [_normalize(c) for c in filler.split(",")]
    if not all(c in known for c in clauses):
      return None

  return Hoist(filler=filler, separator=separator, remainder=remainder)


# ── Which nodes may be hoisted ───────────────────────────────────────────────
#
# Shared by the build pass (which skips) and the lint rule (which explains). Only
# STRUCTURAL blockers live here — an author's `automatic_fillers=False` is a build-time
# marker stripped before anything else sees the config, and a deliberate opt-out is
# not something to warn about anyway.

BLOCK_HAS_FILLER = "has_filler"
BLOCK_AWAITS_SAY = "awaits_say"
BLOCK_VERBATIM = "verbatim"
BLOCK_IMPROVISE_FILLER = "improvise_filler"
BLOCK_COMPONENT = "component"
BLOCK_PARALLEL = "parallel"
BLOCK_VARIANTS = "variants"
BLOCK_FLOW_POOL = "flow_pool"
BLOCK_ASK_LADDER = "ask_ladder"
BLOCK_REPEATED = "repeated"
BLOCK_READBACK_VERBATIM = "readback_verbatim"
BLOCK_NOT_ASKED = "not_asked"

BLOCK_REASONS: dict[str, str] = {
    BLOCK_HAS_FILLER: "it already sets filler_say, so the wait is covered",
    BLOCK_AWAITS_SAY: "awaits.say already speaks to this wait, and two hold phrases"
                      " in one turn thank the caller for a wait that has not started",
    BLOCK_VERBATIM: "verbatim=True pins the copy as authored, delivery order included",
    BLOCK_IMPROVISE_FILLER: (
        "config.speech.improvise includes 'filler', which hands the whole fire turn"
        " — tool call included — to the model"),
    BLOCK_COMPONENT: "a component task never speaks filler_say (the descent returns first)",
    BLOCK_PARALLEL: "a fan-out group takes its filler from the first leg only",
    BLOCK_VARIANTS: "per-surface variants replace this text, so a split would desync surfaces",
    BLOCK_FLOW_POOL: "a fixed line here would shadow the flow's rotating filler_say pool",
    BLOCK_ASK_LADDER: "the ask is a ladder; hoisting one rung would desync the rest",
    BLOCK_REPEATED: "repeated.ask_more is a second ask that would not be hoisted",
    BLOCK_READBACK_VERBATIM: "readback_verbatim asks for deterministic delivery",
    BLOCK_NOT_ASKED: "the slot is never asked, so a filler would never be armed",
}

# The engine's own capability, mirrored: a preempting announce (the default) gates the
# model-turn filler off entirely, so an announce slot can never speak one.
_ANNOUNCE = "announce"


def _sources(slot: dict) -> list[str]:
  src = slot.get("source")
  if isinstance(src, list):
    return [x for x in src if isinstance(x, str)]
  if isinstance(src, str):
    return [src]
  return ["user"]


def _improvises_filler(cfg: dict) -> bool:
  speech = cfg.get("speech")
  if not isinstance(speech, dict):
    return False
  return "filler" in (speech.get("improvise") or ())


def hoist_blocked_by(node: dict, cfg: dict, *, is_task: bool) -> Optional[str]:
  """The structural rule that stops this node being hoisted, or None if nothing does.

  Returns a stable key from the ``BLOCK_*`` constants; ``BLOCK_REASONS`` renders it.
  """
  if node.get("filler_say") is not None:
    return BLOCK_HAS_FILLER
  if node.get("verbatim"):
    return BLOCK_VERBATIM
  if _improvises_filler(cfg):
    return BLOCK_IMPROVISE_FILLER

  if is_task:
    if node.get("component") or not node.get("tool"):
      return BLOCK_COMPONENT
    if node.get("parallel"):
      return BLOCK_PARALLEL
    # isinstance, not `or {}`: this pass runs BEFORE validation, so a mistyped
    # `awaits=["..."]` would reach here and turn a clean validation error into an
    # AttributeError traceback out of the build.
    awaits = node.get("awaits")
    if isinstance(awaits, dict) and awaits.get("say"):
      return BLOCK_AWAITS_SAY
    if node.get("then_say_variants") or node.get("channel_then_say_variants"):
      return BLOCK_VARIANTS
    return None

  if node.get("ask_variants") or node.get("channel_ask_variants"):
    return BLOCK_VARIANTS
  if cfg.get("filler_say") is not None:
    return BLOCK_FLOW_POOL
  if isinstance(node.get("ask"), list):
    return BLOCK_ASK_LADDER
  repeated = node.get("repeated")
  if isinstance(repeated, dict) and repeated.get("ask_more"):
    return BLOCK_REPEATED
  if node.get("readback_verbatim"):
    return BLOCK_READBACK_VERBATIM
  if (node.get("passive") or _ANNOUNCE in _sources(node)
      or "user" not in _sources(node)):
    return BLOCK_NOT_ASKED
  return None


def hoist_field(is_task: bool) -> str:
  """The authored field a node's opener is mined from."""
  return "then_say" if is_task else "ask"


# Blockers worth telling the author about. `BLOCK_HAS_FILLER` and `BLOCK_NOT_ASKED`
# are omitted: the first is the correct state already, and the second is a fact about
# the node that no author action would change.
REPORTABLE_BLOCKS: frozenset[str] = frozenset({
    BLOCK_VERBATIM, BLOCK_IMPROVISE_FILLER, BLOCK_COMPONENT, BLOCK_PARALLEL,
    BLOCK_VARIANTS, BLOCK_FLOW_POOL, BLOCK_ASK_LADDER, BLOCK_REPEATED,
    BLOCK_READBACK_VERBATIM,
})


_POLICY_KEYS = frozenset({"extra_ack"})


def filler_policy(app: object) -> tuple[bool, tuple[str, ...]]:
  """`(enabled, extra_ack)` from `App.automatic_fillers`. Off unless the app asks.

  `True` runs the pass with the framework phrases; a dict runs it and widens them with
  `extra_ack`. An unknown key raises rather than being ignored — a typo'd
  `extra_acks` would otherwise turn the pass on while silently dropping the very
  vocabulary it was written to add.
  """
  raw = getattr(app, "automatic_fillers", False)
  if isinstance(raw, dict):
    unknown = sorted(set(raw) - _POLICY_KEYS)
    if unknown:
      raise ValueError(
          f"App.automatic_fillers: unknown key(s) {unknown}; "
          f"expected any of {sorted(_POLICY_KEYS)}")
    extra = raw.get("extra_ack") or ()
    # A bare string would iterate into single characters, quietly teaching the pass
    # that "a" and "b" are hold phrases — in the one place where a wrong entry is
    # spoken before the tool that would justify it.
    if isinstance(extra, str):
      extra = (extra,)
    return True, tuple(extra)
  return bool(raw), ()
