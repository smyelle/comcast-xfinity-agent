# Refusal reasons

`escalate.condition` decides whether a request for a person may be honored at all.
`declined_say` is what the caller hears when it may not. This page is about the third
shape `declined_say` takes — a list of **reasons** — and about why a gate with two legs
cannot be explained with one sentence.

```python
OUTAGE = {"slot": "outage_status", "eq": "active"}

f.set("escalate", flows.escalate(
    say="Of course — let me get you through to an engineer now.",
    condition={"all": [{"not": OUTAGE},
                       {"slot": "check_result", "filled": True}]},
    declined_say=[
        {"when": OUTAGE, "say": OUTAGE_LINE},
        {"when": {"all": [{"slot": "account_id", "filled": True},
                          {"slot": "check_result", "filled": False}]},
         "say": [CHECK_RUNNING, CHECK_NEARLY_DONE]},
        {"say": TAKE_SOME_DETAILS},
    ],
))
```

Runnable, deployed and driven: `examples/declined_reasons.py`, with the live transcripts
in `examples/DECLINED_REASONS_VERIFY.md`.

## The three shapes, and how they are told apart

| written | read as |
| --- | --- |
| `"a line"` | one reason, one answer |
| `["first", "second"]` | one reason, a LADDER indexed by the refusal count |
| `[{"when": …, "say": …}, …]` | REASONS, evaluated in order |

The discriminator is **does the list contain a dict**. A list of plain strings is a
ladder, exactly as it always was, so every existing flow is byte-identical. Nothing about
the first two shapes changed.

## Why the ladder was not enough

A ladder is indexed by *how many times* the caller has been refused. It is not indexed by
*what* refused them.

That is sufficient for a gate with one leg, and almost every gate starts with one. The
moment a second leg appears, the two refusals usually want opposite words. An outage is
final: no hand-off makes the street's cable work, and the useful answer points the caller
somewhere else. A diagnostic still in flight is the reverse — the refusal is temporary and
nearly over, and the useful answer is "hold on".

With one field, an author has two bad options:

* **One sentence covering both.** Vague by construction, and it is spoken on the one turn
  where the caller has asked a direct question.
* **Derive the wording in a callback.** This is the worse one. It puts spoken copy where
  the validator cannot resolve it, the linter cannot read it, and no offline oracle can
  grade it — and refusal copy is exactly the copy that gets reviewed hardest.

## Evaluation

Reasons are evaluated **in order** against the filled state, and the first match supplies
the line. Order is therefore the policy, not a formatting choice: in the example above,
during an outage *both* of the first two conditions hold, and the outage one speaks
because it is listed first. That is the right call — it is the answer that decides the
caller's next hour rather than their next ten seconds — but it is a decision the author
makes by ordering the list.

An entry with **no `when`** always matches. So does a bare string in the list. Either is
the catch-all, and it must be last.

## Composition with the ladder

A reason's `say` is an ordinary `declined_say` value, so a reason may itself be a ladder:

```python
{"when": CHECKS_STILL_OUT, "say": ["Let me get the check back first.",
                                   "Almost there — the last few seconds."]}
```

The rung is chosen by `escalate_declined`, which counts refusals **on the call**, not
within the reason. A caller refused once for an outage and then once for a running check
reads rung *two* of the second reason, skipping its first line. That is worth planning
for once a flow has more than two reasons.

Like every `declined_say` ladder, it **clamps** to its last line rather than draining to
silence. A hold going quiet is fine — the caller is waiting rather than asking. A refusal
answers a direct question, and hearing nothing back is the worst available reply.

## Why an unevaluable `when` skips

A block's own `condition` **fails open**. If it raises, the hand-off happens: a broken
gate must never be the reason a caller cannot reach a person.

A reason's `when` does the **opposite**. If it raises, that reason is skipped and
evaluation falls through to the next one.

They differ because they decide different things. By the time reasons are read the
request has already been refused; all that is left to choose is the wording. Failing open
there would mean asserting an explanation that may not hold — telling someone about an
outage on a day there is not one. Falling through to a more general reason is the honest
answer. `declined_say_when_error` is logged when it happens, so the condition that raised
is visible rather than merely quiet, and `declined_say_no_reason_matched` is logged when
every reason misses and the caller is refused in silence.

## What is refused, and where

Two gates, catching different things. `flows.escalate(...)` sees only its own arguments;
`validate_app` sees the whole flow, so it is the one that can resolve a `when` against the
declared slots. Both run — the builder so the traceback points at the line you wrote, the
validator so a raw dict block (`f.set("escalate", {...})`) is covered too.

| written | outcome | caught by |
| --- | --- | --- |
| an entry after a catch-all | error | both |
| a reason with no `say` | error | both |
| a `say` that is not a line or a ladder of lines | error | both |
| an entry that is neither a string nor a dict | error | both |
| `declined_say=[]` | error | both |
| a `when` naming a slot the flow does not declare | error | the validator |
| a `when` that is not a dict | error | the validator |
| a branching form with no catch-all | **warning** | the validator |

Verbatim:

```text
escalate(): declined_say[2] can never be reached — declined_say[1] has no `when`, so it
matches every refusal. Put the catch-all last.

escalate(): declined_say[1] is a reason with no `say`, so matching it would refuse the
caller in silence

[line_repair] escalate.declined_say[0].when references undeclared slot 'outage_state'

[line_repair] escalate.declined_say has no catch-all reason (an entry without `when`), so
a refusal none of its conditions match is silent — and a refusal is an answer to a direct
question.
```

**The undeclared-slot rule is the load-bearing one.** It is the only failure here that is
not obviously wrong on the page — one letter off the slot the flow declares. Such a `when`
never matches, so the caller hears the *next* reason down: a fluent, confident, wrong
explanation, on a call that looks completely healthy from every other angle.

**No catch-all is a warning rather than an error** because it is reachable config. It is
only silent on a refusal none of its conditions describe, which may be a state the author
knows cannot happen. The validator says so and moves on.

## Two things to get right when you write one

**Make the catch-all reachable.** It is easy to write a set of reasons whose conditions
between them cover every way the gate can be false, which leaves the catch-all as dead
config nobody has ever heard. In `examples/declined_reasons.py` the second reason's `when`
is deliberately *narrower* than the gate's corresponding leg — it wants the account id as
well — so a caller who asks for a person before giving anything lands on the catch-all
rather than being told about a check that is not running.

**Reuse the condition, do not retype it.** Bind the gate's leg to a name and use the same
object in the `when`. A condition copied by hand and then edited on one side is a refusal
that speaks confidently about a state the gate is not in, and nothing detects it.

## Linting

A reason's lines go through the same spoken-copy pass as any other line, under
`<block>.declined_say[i].say` (or `.say[j]` for a ladder). A reason is not a place to hide
unreviewed copy. `escalate(verbatim=True)` pins them against a `speech` policy exactly as
it pins a single-line `declined_say`.
