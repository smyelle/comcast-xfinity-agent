# `announce(sets=)` — driven live on `gemini-composite-v1`

Everything below is a real transcript from a deployed app. Offline tests prove the latch
is written; only a live drive proves the caller hears one verdict instead of four, because
the failure mode is not an error — the control arm returns a perfectly well-formed turn
that happens to contain four contradictory conclusions.

## The two apps

```
cd packages/flows
PYTHONPATH=src python -m examples.announce_ladder            # treatment
PYTHONPATH=src python -m examples.announce_ladder --control  # A/B control
```

Both validate clean (`0 errors, 0 warnings`). The arms differ in exactly one key: the
control drops `sets=` from every rung and changes nothing else — same lines, same
conditions, same order, same model.

```
cxas push --app-dir announce_ladder_app --project-id ces-deployment-dev \
          --location us --display-name "Announce ladder TREATMENT"
cxas push --app-dir announce_ladder_control_app --project-id ces-deployment-dev \
          --location us --display-name "Announce ladder CONTROL"
```

| arm | app |
|---|---|
| treatment | `4f245acf-ed7f-480d-b1ee-937b2349e185` |
| control | `67040748-368d-4c5b-b17e-65487a85057f` |

The offline build prints which rungs close the gate, so the arms are distinguishable
before either is pushed:

```
  arm: TREATMENT (sets=)              arm: CONTROL (no sets)
  PaymentFailed      True             PaymentFailed      False
  AddressUnverified  True             AddressUnverified  False
  Backordered        True             Backordered        False
  AllClear           True             AllClear           False
```

## The A/B — same order, opposite outcomes

Order `A9` has all three problems at once, so all four rungs (three problems plus the
`AllClear` floor) are eligible on the same pass. That is the point: the conditions are
deliberately NOT mutually exclusive, so anything that resolves the ladder has to be the
latch rather than the gating.

**Treatment**

```
caller: my order has a problem
agent : I can help with that. Could you please provide your order number?
caller: A nine
agent : Your payment didn't go through, so the order is on hold. Once the card is
        sorted the rest will follow.
```

One rung. `PaymentFailed` wrote `verdict_given`, the cascade recomputed, and the three
lower rungs were no longer active.

**Control** — same utterances, `sets=` removed

```
caller: my order has a problem
agent : I can help with that. Could you please provide your order number?
caller: A nine
agent : Your payment didn't go through, so the order is on hold. Once the card is
        sorted the rest will follow. We couldn't verify your delivery address, so
        nothing will ship until that's confirmed. One item is on back order, so the
        order will ship in two parts. Everything checks out — your order is on track.
```

Four verdicts in a single breath, because the whole announce cascade leaves as one
preempt. The last line is the tell: `AllClear` is the floor rung with no problem flag, and
it fires straight after three problems have been read out — eligible precisely because
nothing had closed the ladder.

## What this rules out

A one-line result on its own would not have proven much: it is also what you would see if
the three lower conditions simply never held. The control is what excludes that. Same
data, same conditions, same order, and it speaks all four — so the conditions are
demonstrably satisfiable and the single line in the treatment arm is the latch doing the
work.

## Why not tasks

The same ladder is expressible today as tasks with `out_key`. It works, and each rung
costs a platform round trip: the engine emits a `function_call`, CES runs a tool whose
body is `pass`, `after_tool` fires, and `before_model` re-enters the engine to process
the result. Measured on a deployed production agent by reading `Span.duration` off the
live response, that re-entry is **~230ms per rung** — to write one constant. As announces
the whole ladder resolves inside a single engine invocation.
