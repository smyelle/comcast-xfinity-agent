# relatch — live verification

One tip at a time, however long the caller takes. Two apps, the same authored flow, one
flag apart.

| arm | app | `relatch` |
|---|---|---|
| treatment | `projects/ces-deployment-dev/locations/us/apps/813f62fc-935c-46e2-9cfd-4dcaf9503ccf` | yes |
| control | `projects/ces-deployment-dev/locations/us/apps/c8ee4fb7-3ea3-46cb-a792-b71f30f261a8` | no |

flows 0.19.0, engine `2026.08.18.2+relatch`.

## Run it

```bash
cd packages/flows
PYTHONPATH=src python -m examples.relatch_walkthrough            # treatment
PYTHONPATH=src python -m examples.relatch_walkthrough --control
PYTHONPATH=src python -c "from flows.deploy.push import deploy; deploy('./relatch_walkthrough_app','<treatment>')"
PYTHONPATH=src python -m examples.relatch_walkthrough_drive --app <treatment>
PYTHONPATH=src python -m examples.relatch_walkthrough_drive --control --app <control>
```

The caller says what is wrong, then goes away for fourteen seconds to do each step and
comes back with "okay, I did that". macOS only — the driver synthesizes caller audio with
`say` and `afconvert`.

## Control — the ladder runs ahead of the caller

Session `a3856707-57db-45ea-9b48-492b51d4ea68`.

```
t=   5.3  Start by unplugging the router, waiting thirty seconds, then plugging it back in.
t=  20.0  Next, move the router away from anything metal or electrical.
t=  20.0  Last thing: switch the TV box over to the other wall socket.
t=  20.0  That's everything I can suggest from here -- let me get an engineer to call you.

steps per caller turn : [1, 2, 0, 0]
```

The caller answers the first step and is handed the entire rest of the walkthrough, plus
the hand-off, in one breath. The call is finished twenty seconds in, and they were never
asked about steps two or three. The `since` clock is still sitting on step one, so every
gate after it has been open since the caller's first reply.

## Treatment — one step per turn

Session with `relatch=True` on the same latch:

```
t=   5.4  Start by unplugging the router, waiting thirty seconds, then plugging it back in.
t=  20.2  Next, move the router away from anything metal or electrical.
t=  34.0  Are you still there? Let me know once you have moved the router away…
t=  35.7  Last thing: switch the TV box over to the other wall socket.
t=  49.0  Did you manage to switch the TV box over to the other wall socket?
t=  52.0  That's everything I can suggest from here -- let me get an engineer to call you.

steps per caller turn : [1, 1, 1, 0]
```

Each step waits for the caller to come back. The hand-off arrives only after the third
step has been answered, which is the point at which it is actually true.

## Two traps in grading this

Both cost a re-run, and both are the same mistake in different clothing: **measure the
turn, not the text.**

**A per-message count is always 1.** CES delivers each line as its own server message, so
the control's three-lines-at-t=20.0 scores as three turns of one step. The driver groups
by caller turn instead — the caller's speech boundaries are known, because the driver
built the audio.

**A step is delivered once; after that the model refers back to it.** At t=49.0 the agent
asks *"did you manage to switch the TV box over to the other wall socket?"* — the same cue
as the step itself. Counting it again marks a perfectly paced call as a failure. Cues are
counted on first appearance only.

## Note for anyone re-running this

The example warns `Config has no 'tasks'`. That is correct: the whole walkthrough is
announces and latches, with no backend to call. It is the smallest thing that shows the
behaviour.
