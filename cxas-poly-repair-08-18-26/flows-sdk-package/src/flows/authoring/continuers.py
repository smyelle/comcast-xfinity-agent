"""Following-along cues: the noises a caller makes to say "keep going".

A caller who says "mhmm" or "got it" while the agent is talking is not taking the floor.
They are agreeing. Today the framework has no idea: the utterance fills no slot, so
`_handle_state_change` reports no progress, so `_handle_steer_back` counts the turn as a
stall — and enough stalls escalate the call. Agreeing with the agent currently pushes the
caller toward a human. When the backchannel also lands over a line the platform then cut
short, the rest of that line is lost as well.

WHOLE UTTERANCES ARE MATCHED, NEVER WORDS. `authoring/autofill.py` learned this the
expensive way for the agent's own openers, and its docstring is worth quoting because the
trap is identical here: "every word of 'All good.', 'No.' and 'We will take it back.' is
individually an acknowledgement, so a bag-of-words gate hoists all three." The caller-side
version of that mistake swallows "mhm, but what about the fee?" — a real question, dropped
because it opens with a noise. So a turn is a continuer only if, once normalized, the
ENTIRE thing is a known phrase (or a run of them: "yeah, ok, got it").

This vocabulary is deliberately NOT `autofill.ACK_PHRASES`. That list is what an AGENT says
before a tool ("let me check that for you", "one moment"), and it deliberately excludes
"yes" and "no". This one is what a CALLER says to mean "still with you", which is a
different set with a different failure mode.

Two things keep it safe, and both live at the call site rather than here:

  * the pending slot wins. A caller answering "okay" to "shall I book it?" is answering,
    not backchanneling, so the engine consults expected values, `option_cues` and
    `dtmf_map` FIRST and only falls through to this list when the utterance answers
    nothing. That is why "okay", "sure" and "right" can safely be in the vocabulary at all.
  * the list stays short and unambiguous. Widening it is an author's call
    (`flows.continue_cues(extra=[...])`), because only the author knows whether "fine" is
    agreement or a complaint in their domain.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

# The default vocabulary. Every entry must be something a caller can say that CANNOT be a
# substantive answer on its own -- which is the test for adding one. "yes" and "no" are
# absent on purpose: they are answers first and agreement second, and the pending-slot
# check cannot save a yes/no that arrives when no slot is pending.
DEFAULT_CONTINUER_PHRASES: frozenset[str] = frozenset({
    # the pure backchannel noises
    "mhm", "mhmm", "mm", "mmm", "mm hmm", "mmhmm", "uh huh", "uhhuh", "ah ha",
    "aha", "hmm", "hm", "yup", "yep", "yeah", "yea", "ya", "right", "ok",
    "okay", "k", "sure", "alright", "all right",
    # short acknowledgements
    "got it", "gotcha", "understood", "i see", "i understand", "makes sense",
    "that makes sense", "fair enough", "sounds good", "that works",
    "no problem", "of course", "cool", "great", "perfect", "excellent",
    "good", "thats good", "very good", "nice",
    # explicit "keep going"
    "go on", "carry on", "keep going", "continue", "im listening",
    "im with you", "with you", "im here", "still here", "go ahead",
})

# A backstop on the same principle as autofill.MAX_ACK_WORDS: a continuer is short by
# nature, so anything long is substance even if every clause looks like agreement.
MAX_CONTINUER_WORDS = 6

_WS = re.compile(r"\s+")
_KEEP = re.compile(r"[^a-z0-9 ]+")
# Callers chain them -- "yeah, ok, got it" is one continuer, not three utterances.
_SPLIT = re.compile(r"[,.;!?]+| and ")


def normalize(text: str) -> str:
  """Lowercase, drop punctuation and apostrophes, collapse whitespace.

  Apostrophes are DROPPED rather than preserved so "I'm listening" and "im listening" are
  one entry in the vocabulary. Transcription is inconsistent about them and a vocabulary
  that has to spell both is a vocabulary that will miss the third spelling.
  """
  low = (text or "").lower().replace("’", "").replace("'", "")
  return _WS.sub(" ", _KEEP.sub(" ", low)).strip()


def phrase_set(phrases: Optional[Iterable[str]] = None,
               extra: Optional[Iterable[str]] = None) -> frozenset[str]:
  """The active vocabulary: `phrases` replaces the default, `extra` adds to it."""
  base = frozenset(normalize(p) for p in phrases) if phrases is not None \
      else DEFAULT_CONTINUER_PHRASES
  if extra:
    base = base | frozenset(normalize(p) for p in extra)
  return frozenset(p for p in base if p)


def is_continuer(text: str,
                 phrases: Optional[Iterable[str]] = None,
                 extra: Optional[Iterable[str]] = None) -> bool:
  """Is this WHOLE utterance nothing but following-along noise?

  False for anything carrying substance, including a continuer with substance attached --
  "mhm but what about the fee" is a question, and treating it as agreement would drop it.
  """
  norm = normalize(text)
  if not norm or len(norm.split()) > MAX_CONTINUER_WORDS:
    return False
  vocab = phrase_set(phrases, extra)
  if norm in vocab:
    return True
  # A run of them, each clause a known phrase: "yeah, ok, got it". Split the RAW text --
  # normalize() has already eaten the commas by this point, so splitting the normalized
  # form finds nothing and a chained continuer reads as one unknown phrase.
  clauses = [normalize(c) for c in _SPLIT.split(text or "")]
  clauses = [c for c in clauses if c]
  return len(clauses) > 1 and all(c in vocab for c in clauses)
