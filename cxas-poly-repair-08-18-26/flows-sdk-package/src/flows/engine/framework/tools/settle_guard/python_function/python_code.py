"""Holds a dispatching turn open long enough for its DEFERRED calls to settle.

FRAMEWORK CODE -- shared across all agents using the slot-filling engine.

A deferred (`executionType: ASYNCHRONOUS`) call is launched by the turn that dispatches
it, and when that turn ends immediately afterwards two things are lost, silently and at a
rate: the dispatch itself, and any `context.state` write made by a tool the deferred body
calls nested through `tools` (measured, ces-probes 122-129).

  no guard, multi-call preempt     4/18 legs launched,  0/18 nested writes survived
  guard 0.05s, inline             13/18 legs launched,  8/18 nested writes survived
  guard 0.10s, inline             16/18 legs launched, 15/18 nested writes survived
  guard 0.25s, inline             18/18 legs launched, 18/18 nested writes survived
  guard 0.50s, inline             18/18 legs launched, 18/18 nested writes survived

The mechanism, best guess and consistent with all of 122-129: mutations ship back when an
execution REPORTS. A nested call has no completion envelope of its own, so nothing carries
its writes and they land only while the session's writer is still attached. Holding the
turn open keeps it attached.

SYNCHRONOUS is the whole point. A deferred guard returns its pending placeholder at once
and buys no time at all. It is dispatched in the SAME preempt as the calls it guards --
CES accepts a preempt mixing deferred and synchronous calls, and the synchronous one runs
alongside the concurrent deferred legs (129), so the guard costs no extra model pass.
That matters: a wide fan-out cannot spare one against the ten-pass cap (72).

A quarter of a second, and that number is measured rather than assumed. 128 chose 0.5s and
recorded that it had not found the boundary -- its ladder had no failures in it to separate
the rungs. 130 re-ran the ladder on the shape that does fail: 0.05s and 0.10s both still
lose on both channels, 0.25s is clean in every session and identical to 0.50s.

What is NOT resolved is the interval between 0.10s and 0.25s; the true floor is somewhere
in there, and going lower buys back tenths of a second for a finer ladder's worth of work.
This remains a mitigation rather than a repair -- a race made to pass consistently is still
a race.
"""

import time
from typing import Any

_SETTLE_SECONDS = 0.25


def settle_guard() -> dict[str, Any]:
  """Spend a quarter second so the dispatching turn outlives its deferred launches."""
  time.sleep(_SETTLE_SECONDS)
  return {"settled": True}
