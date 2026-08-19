# `since` on a slot gate — driven live on `gemini-composite-v1`

Everything below is a real transcript from a deployed app. Offline tests prove the gate
opens on the right turn; only a live drive proves the caller's answer is actually
CAPTURED there, because the failure mode is not an error — the control arm returns a
perfectly well-formed turn that happens to ask the same question twice.

## The two apps

```
cd packages/flows
PYTHONPATH=src python -m examples.since_slot_gate            # treatment
PYTHONPATH=src python -m examples.since_slot_gate --control  # A/B control
```

Both validate clean (`0 errors, 0 warnings`). The arms differ in exactly one key: the
control reads the offer's latch with `filled: True` where the treatment reads it with
`since_turns`. Same lines, same cues, same tools, same model.

```
cxas push --app-dir since_slot_gate_app --project-id ces-deployment-dev \
          --location us --display-name "Since slot gate TREATMENT"
cxas push --app-dir since_slot_gate_control_app --project-id ces-deployment-dev \
          --location us --display-name "Since slot gate CONTROL"
```

| arm | app |
|---|---|
| treatment | `40026db0-424b-4d6f-8b89-66a579407b88` |
| control | `d8a78e30-2bda-4fae-8477-24b14f59b51d` |

The offline build prints the emitted gate, so the arms are distinguishable before either
is pushed:

```
  arm: TREATMENT (since_turns)          arm: CONTROL (filled)
  rebook slot gate : {'slot': 'rebook_offered', 'since_turns': 1}
                                        {'slot': 'rebook_offered', 'filled': True}
```

## The A/B — same three utterances, one extra question

**Treatment** — session `43ebe343-5dde-4b71-92ea-d1a16abdbee0`

```
caller: my parcel never turned up
agent : I'm sorry to hear that your parcel hasn't arrived. Could you please provide
        your tracking number so I can look into this for you?
caller: seven seven three
agent : That one's back at the depot. Shall I book a redelivery for tomorrow?
caller: yes please
agent : Done — it's booked for tomorrow.
```

The offer speaks alone. `rebook` is shut on that turn, so the engine has no question of
its own to add. On the next turn the gate is open, `yes please` matches the cue, and the
consuming rung fires.

**Control** — session `784eb0e2-a7cc-4d49-a138-4e167c26650e`

```
caller: my parcel never turned up
agent : I'm sorry to hear that your parcel hasn't arrived. Could you please provide
        your tracking number so I can look into this for you?
caller: seven seven three
agent : That one's back at the depot. Shall I book a redelivery for tomorrow?
        Would you like me to book that redelivery?
caller: yes please
agent : Done — it's booked for tomorrow.
```

`filled: True` is satisfied the instant the announce writes the latch, so `rebook` is
already open on the offer's own turn and the engine appends the slot's own `ask` to the
line that just asked it. Two wordings of one question, in one breath.

## Why this needs the engine change

`since_turns` is read in two places and they are at opposite ends of a turn. A TASK
condition is evaluated during the DAG walk; a SLOT condition is evaluated much earlier, by
the cue matcher, which decides whether a slot is active enough to capture an utterance.
Two orderings had to be fixed for a slot gate to mean what it says:

* the turn context is now published BEFORE the fill stages, not only after them, so the
  cue matcher measures against this turn rather than the previous one;
* stamps are swept again AFTER the DAG walk, because an announce's `sets`, a rung's latch
  and a task's outputs are all written there — a latch written by the walk used to carry
  no stamp until the next turn stamped it, so a gate on it opened one turn later than it
  read.

Both were caught by this demo rather than by the unit tests, and the second one only
showed up live: the first live drive of the treatment arm answered

```
caller: yes please
agent : Hmm, I'm having trouble with that. Do you want me to try again?
```

because the offer's latch had been stamped on turn 3 instead of turn 2, leaving the gate
shut on the very turn the caller answered. The trace is what named it — `rebook_offered`
carried a stamp of `3.0` on the turn it should have read `2`.

Both arms were re-deployed and re-driven after the `_TURN_CTX` cleanup landed, so these
transcripts are from the engine as it stands, not from an earlier revision of it.

## Offline

```
PYTHONPATH=src pytest tests/test_example_evals.py -k since_slot_gate
PYTHONPATH=src pytest tests/test_slot_value_policy.py -k "since or latch"
```

Two eval scenarios (the answer captured on the next turn; the offer's own turn refused)
and four unit tests: one fails without each half of the engine change, and one pins
that the turn context does not outlive the turn.
