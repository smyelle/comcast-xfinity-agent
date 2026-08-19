# Verifying a timeout on a body the SDK did not write

## Why offline green proves nothing

`validate_app` cannot see how long a body takes, and neither can the emitted JSON: a tool
that sleeps for an hour validates clean, builds clean and deploys clean. The only thing an
offline test can check is that `"timeout": "180s"` appears on the right resource, which is
a statement about a string, not about whether the platform honours it.

That distinction is not theoretical. ces-probes 136 and 140 measured the same field being
**accepted, persisted, and ignored** for a platform-executed call — a one-second budget let
an agent tool answer after 3665ms. So "it is in the JSON" and "it bounds the call" are
genuinely different claims, and only one of them can be tested from a laptop.

## The app

`examples/tool_timeouts.py`. One slot, one task, and a body handed over as **source** —
the shape a grafted tool arrives in, with no decorator to hang `timeout=` on:

```python
tool_bodies={"run_diagnostics": SLOW_BODY},      # sleeps 75s
tool_timeouts={"run_diagnostics": 180},          # the line under test
```

Two arms, identical but for that second line. 75 seconds is chosen so the arms cannot both
pass: past any plausible default, comfortably inside the declared budget.

Emitted, the difference is exactly one key:

```
declared   "timeout": "180s"
default    (none)
```

## Driven live — 2026-08-09, `gemini-composite-v1`

```
default    32.5s   Running the checks now.  The checks didn't finish in time.
declared   78.0s   Running the checks now.  All checks completed.
```

The declared arm ran the full 75s body and answered. The undeclared arm never got there:
its task hit `on_failure` and the caller was told the checks failed, on a backend that was
merely slow — which is the failure this field exists to prevent.

## The finding nobody was looking for

The undeclared arm did not die at 60 seconds. Three runs:

```
32.3s   32.5s   33.4s
```

**About 30 seconds**, consistently — roughly half the 60s the platform documents as its
default, and half what the SDK's own tools page said before this change. Both have been
corrected to the measurement.

It is consistent with the ~30s ceiling ces-probes 138 found for agent calls and with `110`
reporting `DP@30s` for a dispatched python body, which suggests one bound rather than
three. This example does not establish that — it measures one shape, at one duration, on
one model — but it does mean an author budgeting against "60s" has half the room they
think.

## What this does not cover

* One duration either side of the boundary; the exact undeclared bound is not resolved,
  only shown to be well under 60s.
* Synchronous only. On an `ASYNCHRONOUS` body the kill is quiet — nothing reports and the
  wait runs out to `awaits.max_turns` — which is worse and is not exercised here.
* A body the **platform** runs rather than the sandbox ignores this field entirely
  (ces-probes 136/140). For those the bound is `awaits`, and this example says nothing
  about them.
