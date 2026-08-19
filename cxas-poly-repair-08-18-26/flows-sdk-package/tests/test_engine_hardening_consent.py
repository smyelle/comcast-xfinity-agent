"""Engine hardening — what counts as the caller saying YES.

Two engine paths read a reply as consent, and both act WITHOUT the model:

  * auto-confirm (`_is_affirmative`) preempts the turn with `confirm_pending`, which
    commits every pending slot outright;
  * inline-confirm (`_starts_affirmative`) silently confirms them and lets the model
    run on the rest of the message.

So a false positive here is not a wording problem — it writes values the caller never
agreed to, and does it before anything else can object. Three ways that happened:

  * A STALL is not consent. Over voice a hold reads as "ok hold on", "sure let me
    look", "ok one second" — a leading filler yes and a request for time. Any short
    reply starting with an affirmative and carrying no correction word confirmed
    everything in flight. A cancellation ("yes cancel that") and a sign-off ("ok bye")
    landed the same way.

  * `_starts_affirmative` only scanned words[1:4] for a retraction, so "yes that is
    all wrong" — the whole point of which is the last word — inline-confirmed the very
    values the caller had just rejected.

  * `_has_value_token` only knew DIGITS, so "yes its four people" auto-confirmed and
    dropped the party size, while "yes its 4 people" correctly deferred to the model.
    ASR writes a small spoken number as a word, so the common case was the broken one.

The inputs here are ASR text: lowercase, no punctuation.

Fully offline: no network, no creds, no LLM.

Run:
  cd /Users/fsamuel/Labs/cxas-labs
  PYTHONPATH=packages/flows/src .venv/bin/python -m pytest \
      packages/flows/tests/test_engine_hardening_consent.py -q
"""

from __future__ import annotations

import pytest

from flows.engine import loader as fb

eng = fb.load_engine()


# A reply that must still commit the pending values — the whole reason the fast path
# exists. Regressing any of these costs a model call on every plain "yes".
CONSENT = ["yes", "yeah", "correct", "that's right", "yep", "sure", "yes that's right",
           "yes that looks right", "ok perfect"]

# A leading affirmative that is NOT consent: a stall, a cancellation, a sign-off.
NOT_CONSENT = ["ok hold on", "ok one second", "ok give me a second", "sure let me look",
               "yes cancel that", "right now", "ok bye", "yeah hang on",
               "ok just a minute", "sure stop"]


@pytest.mark.parametrize("text", CONSENT)
def test_a_plain_yes_still_auto_confirms(text):
  assert eng._is_affirmative(text) is True


@pytest.mark.parametrize("text", NOT_CONSENT)
def test_a_stall_cancel_or_signoff_is_not_auto_confirm(text):
  """These preempted the model and committed every pending slot."""
  assert eng._is_affirmative(text) is False


@pytest.mark.parametrize("text", NOT_CONSENT)
def test_a_stall_cancel_or_signoff_is_not_inline_confirm_either(text):
  """Inline-confirm commits the same values, just without the preempt — closing only
  the auto-confirm door would have left the identical write on the other path."""
  assert eng._starts_affirmative(text) is False


def test_a_short_retraction_no_longer_inline_confirms():
  """The retraction word is the LAST one, past the old words[1:4] window, so the
  engine committed exactly what the caller had just told it was wrong."""
  assert eng._starts_affirmative("yes that is all wrong") is False


def test_a_long_reply_still_only_scans_its_opening_clause():
  """Past the opening clause the words really are new content, not a retraction —
  narrowing the window there is what lets "ok, so I need to change my flight" run the
  model on the change instead of being read as a rejection."""
  assert eng._starts_affirmative("ok so i need to change my flight") is True


@pytest.mark.parametrize("text", CONSENT + NOT_CONSENT + [
    "yes that is all wrong", "ok so i need to change my flight",
    "yes its four people", "yes book my table for the usual time",
])
def test_auto_confirm_always_implies_inline_confirm(text):
  """The invariant between the two paths: auto-confirm is the strictly stronger read,
  so anything it accepts inline-confirm must accept too. Broken, a reply takes the
  preempting path while the gentler one rejects it."""
  if eng._is_affirmative(text):
    assert eng._starts_affirmative(text) is True, text


# --------------------------------------------------------------------------- #
# a spelled-out number is a value


@pytest.mark.parametrize("text", [
    "yes its four people", "yes there are two of us", "sure twenty minutes",
])
def test_a_spelled_out_number_defers_to_the_model(text):
  """Auto-confirm would preempt the model, and the value would be silently gone."""
  assert eng._is_affirmative(text) is False


def test_the_digit_form_of_the_same_reply_behaves_identically():
  """The defect was the DIVERGENCE: "4" deferred and "four" did not, so whether the
  caller was heard depended on how ASR happened to write the number."""
  assert eng._is_affirmative("yes its 4 people") == eng._is_affirmative(
      "yes its four people")


def test_a_date_word_or_digit_is_still_a_value():
  assert eng._has_value_token(["ok", "friday"]) is True
  assert eng._has_value_token(["yes", "7pm"]) is True


def test_ordinary_confirmation_chatter_is_not_read_as_a_value():
  """The word list stays tight on purpose: a false value token pushes a plain yes
  onto the model path, costing a turn for nothing."""
  assert eng._has_value_token(["that", "looks", "right"]) is False
