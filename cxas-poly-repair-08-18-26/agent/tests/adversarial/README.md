# Adversarial scenarios

A standing corpus of calls designed to BREAK this agent, and a record of which ones it
survives. The point is hill climbing: a scenario that fails today is a target, and a
scenario that passes today is a regression test for tomorrow. Both are worth keeping, so
nothing here is deleted when it starts passing.

This is a measurement pass. **Scenarios do not fix anything.** A run that ends with code
changed is a run whose results cannot be trusted, because the thing measured is no longer
the thing shipped.

## The format

One file per family, `scenarios/<family>.md`. Every scenario carries the same fields so
that families are comparable and a later run can diff against this one.

```
### <FAMILY>-NN  <one-line name>

**Why this should break it:** the hypothesis. What weakness is being probed, and why it is
plausible. A scenario with no hypothesis is a random call, not a probe.

**Setup:** app id, account number, modality (voice/text), anything seeded. Say "cold" if
nothing was seeded — and mean it.

**Caller script:** the turns, in order, with hold durations where timing matters.

**Expected:** what a correct agent does. Derive this from the authored copy in `scripts.py`
and the rungs in `app.py`, NOT from what the agent happened to do. An expectation written
after seeing the result is not an expectation.

**Observed:** what actually happened, quoted from the transcript. Real lines, not summary.

**Verdict:** PASS | FAIL | PARTIAL | BLOCKED
  PASS     - did the right thing for the right reason
  FAIL     - wrong behaviour
  PARTIAL  - right outcome, wrong route or wrong copy
  BLOCKED  - could not be tested; say what stopped it

**Defect:** only when FAIL or PARTIAL. What is broken, where (file:line if known), and how
severe from the caller's point of view. No fix, no patch — the diagnosis only.

**Reproduced:** how many times out of how many attempts. A one-off is not a finding.
```

## Rules that make the results mean something

**Re-run every failure serially before recording it.** Concurrent calls against one CES app
fail *together* — this has been measured on other agents (4/4 spaced passed, 0/4 concurrent
passed). A failure seen while other drives are in flight is contention until proven
otherwise. Record `Reproduced: 3/3 spaced` or do not record a FAIL.

**Vary timing on anything timing-dependent.** The diagnostics sweep lands somewhere around
43-54s. A single pass at one hold duration has produced a false PASS twice on this agent.
Report the margin, not just the outcome.

**Expected comes from the source, not the sample.** Read `scripts.py` for the authored line
and `app.py` for the rung that should fire. If the agent speaks something that is in neither,
that is a finding even when it sounds plausible — the model improvising over a dropped
`then_say` has been a real defect here more than once.

**Quote, do not paraphrase.** "It handled it well" is not evidence.

**BLOCKED is a real answer.** If the dev backend is down or a fixture cannot express the
case, say so. A scenario silently downgraded to something testable is worse than a gap.

## Index

`scenarios/INDEX.md` holds the running tally: one row per scenario, verdict, and one line of
detail. That file is the hill-climbing worklist.
