# Verifying an agent called as a tool, asynchronously

## What had to be shown

Three things, and only the first two came out as expected.

1. The primitive replaces the workaround — the app builds with no post-emit JSON rewrite.
2. An async agent tool defers, waits, and lands its answer, with the author writing none
   of the wire contract.
3. A `failed with error` envelope is distinguishable from a successful reply. **Not
   shown. See the last section — the attempt to produce one failed, informatively.**

## The app

`examples/agent_tool.py`, two arms. A parcel desk with one slot and one task; the task
fires `ask_specialist`, an `agentTool` pointed at a `helper_agent` in the same app. The
specialist has a tool of its own, and the `failing` arm's copy of that tool raises.

The author writes no `request`, no `out_key`, no `success_check`. Building it emits
`tools/ask_specialist/ask_specialist.json` with `executionType: ASYNCHRONOUS`,
`agents/parcel_specialist/` with its instruction, and no python body for the tool.

The earlier version of this demo needed fifteen lines of post-emit JSON surgery to exist
at all. Those lines are gone, which is the clearest statement that the primitive is
sufficient.

## Driven live — 2026-08-09, `gemini-composite-v1`

Three arms: the two above, plus a **control** built from the same source with the two
engine files reverted to the commit before the verb capture — so the only difference is
the change under test.

**Control — the engine before this change:**

```
> where is parcel AB123
< Let me ask the specialist.
> any news?
< pending  pending          ← the placeholder, spoken to the caller
[session ends]
```

The deferral placeholder was banked as the answer, announced, and the flow ended. The
real reply arrived afterwards with nowhere to go.

**The specialist answers:**

```
> where is parcel AB123
< Let me ask the specialist.            [awaits.say]
> any news?
< Still waiting on them.                [while_waiting[0]]
> and now?
< HELPER9f21 Parcel AB123 is currently out for delivery in Boston.
```

`HELPER9f21` exists only inside the specialist, so the sub-agent genuinely ran. This is
also the first time `while_waiting` has been exercised — the previous confirmation used a
helper that answered within one turn, so the ladder was declared and never reached.

**The specialist's backend is down:**

```
> where is parcel AB123
< Let me ask the specialist.
> any news?
< Still waiting on them.
> and now?
< I'm sorry, but our parcel tracking system is currently experiencing technical
  difficulties and is unavailable. Please try again later.
```

Graceful, and **not** the case this was built for. See below.

## The bug the live drive found, which the tests did not

The first live run failed on all three arms, including the fixed ones: every arm spoke
`pending` and ended.

The unit tests had covered the placeholder against the DEFAULT `success_check`, where a
`{"response": "pending"}` payload has no `success` key and is falsy by arithmetic. But the
configuration `agent_tool` actually produces sets `success_check="response"` — and then
the placeholder **satisfies its own success test**. Intake mapped it into the slot before
the engine's `_is_async_pending` was ever consulted, and by then the fill had happened.

Intake now recognises a placeholder on sight, in either spelling, whatever `success_check`
names. Two tests pin the real configuration, which is the shape the tests should have used
from the start.

## What is still NOT proven: the `failed with error` branch

The `failing` arm was built to produce that envelope, and it did not. Checked against the
conversation record:

```
failed with error      0
completed with response 1
```

The specialist is an agent, so when its own tool raised it did what an agent does — it
caught the problem and answered with a sentence about it. From the platform's point of
view the call **succeeded**; the bad news is in the content.

That is worth knowing on its own terms, and it cuts two ways:

* The verb's **success** branch is the one that matters in practice for agent tools, and
  it is proven live.
* The **failure** branch remains offline-only. It is pinned by a unit test that feeds
  intake a `failed` outcome directly, and that test cannot pass if the verb is discarded —
  but no drive here has produced the envelope on the wire.
* An agent's failures mostly do not look like failures at all. They arrive as successful
  calls carrying an apology, which no engine can detect and no `on_failure` ladder will
  catch. An author who needs to branch on a specialist failing has to read the content.

I would not claim the failure path is verified. It is implemented, unit-pinned, and
undriven.

## Taken with ces-probes 135 and 136

Two things landed on the probe branch after this was built, and both bear on it.

**135 — a failed delegation is catchable, and its shape is not what the docs say.** A
platform-executed agent call that cannot be made comes back as
`{"status": "error", "error": "..."}`, in well under a second. That payload has no
`response` key, which is exactly the condition under which the completion envelope's verb
is consulted here — so on the first cut of this change, an error payload arriving inside a
`completed with response` envelope would have been read as a SUCCESS. The verb can no
longer overrule a payload that has declared itself an error. Whether an async agent failure
arrives that way or under `failed with error` is still unmeasured by anyone, which is
precisely why the guard is worth having.

135 also notes it cannot tell one failure reason from another: a deleted target and an
unreachable host differ only by a trailing clause. So an author gets one handler, not a
branch.

**136 — a tool's `timeout` is accepted, persisted, and ignored** for a platform-executed
call: a one-second budget let a call answer after 3665ms while a python tool carrying the
same budget in the same app and run died at 1242ms. `validate_app` now warns when a timeout
is declared on an agent tool, because the emitted config would read as though the call were
bounded.

Its wider conclusion — "a delegation cannot be bounded at all, not asynchronous and not
timed out" — is true of A2A and **not** of this flavour. An `agentTool` can be asynchronous
(134), so `awaits.max_turns` is a real budget, and that is the strongest argument for
preferring it.

## Also not covered

* Same-app targets only; cross-app is refused at push.
* `on_timeout` is declared and still unreached — the specialist answers inside the budget.
* One model.
* Nothing here exercises a specialist that is slow rather than broken, which is the case
  the whole async path exists for.
