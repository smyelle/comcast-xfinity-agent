# Behavioural eval format for `examples/`

These files are the **regression suite** for the example apps. `test_examples.py` proves an
example *builds*; these prove it still *behaves* — asks the right slots, fires the right
tools, takes the right branch, ends the right way. They run offline through the deterministic,
LLM-free Engine-mode simulator (`flows.sim.engine_sim`) — no model, no network, no GCP — so
they are fast and gate every PR (see `tests/evals/harness.py`).

## One file per example, many scenarios, both polarities

- **One YAML file per example**: `examples/evals/<example>.eval.yaml` where `<example>`
  matches `examples/<example>.py`.
- Each file holds a `scenarios:` list. Every scenario is graded independently (one pytest case
  each: `<example>::<scenario>`).
- **Every file must have ≥1 positive (happy-path) scenario AND ≥1 negative/edge scenario**
  (re-ask ladder, validation failure, tool-failure branch, interruption, containment — whatever
  the example demonstrates). A file with only a positive scenario is treated as insufficient
  coverage and **blocks the release**, same as a missing eval (enforced by
  `test_example_eval_coverage.py`). Tag scenarios `positive` / `negative` to declare polarity.

## Schema

```yaml
meta:
  example: track_shipment          # must match examples/track_shipment.py
  claim: "Tracks a shipment, answers a store-hours FAQ, then tracks another."
scenarios:
  - name: happy_path
    polarity: positive             # positive | negative  (>=1 of each per file)
    seed: {}                       # optional on-entry event prefill (variable-map ingress)
    tool_fakes:                    # deterministic backend results for @flows.tool executors
      lookup_shipment: {status_message: "out for delivery by 8 PM", success: true}
    on_start:                      # optional: expectations against the opening turn
      - said_contains: "track a shipment"
    turns:
      - answer: {slot: tracking_number, value: "1Z999"}
        expect:
          - tool_called: lookup_shipment
          - said_contains: "out for delivery"
          - disposition: complete
  - name: unanswered_reask
    polarity: negative
    turns:
      - say: "hmm"                  # a caller turn the flow cannot use
        expect:
          - asked_slot: tracking_number
```

## Turn kinds (one per turn)

| key | engine_sim step | use |
|---|---|---|
| `say: "<text>"` | `user_text` | a caller utterance the engine reads deterministically (steer-back / affirm / negative / unusable) |
| `answer: {slot, value}` | `setter_call` | answer the asked user slot via its generated setter (real validation). `args: {...}` overrides the default `{slot: value}` |
| `task_result: {task, success, result}` | `task_result` | inject a tool result explicitly (usually unnecessary — see auto-drain) |
| `confirm: true` / `reject: true` | `confirm`/`reject` | commit / discard a pending readback |
| `event: {slot: value}` | `event_prefill` | an on-entry event prefill mid-conversation |

**Auto-drain**: after every turn, when the engine returns a `fire` for an executor that has a
`tool_fakes` entry, the harness injects that fake as a `task_result` and re-runs — exactly as
the deployed platform would run the tool. So a normal turn is just `answer`/`say` + a
`tool_called` expectation; you rarely write `task_result` by hand. A tool that fires with **no**
declared fake is an ERROR (not a FAIL) — declare what every backend returns.

## Expectations (list under `expect:`, or `on_start:`)

Each is a single-key mapping, evaluated against the (drained) result of its turn:

| expectation | holds when |
|---|---|
| `said_contains: "<s>"` | `<s>` is a case-insensitive substring of what the agent said |
| `said_equals: "<s>"` | the agent said exactly `<s>` |
| `asked_slot: <slot>` | the agent spoke one of that slot's `ask` rungs |
| `tool_called: <tool>` | `<tool>` fired during the turn (incl. auto-drain) |
| `no_tools_called: true` | no tool fired during the turn |
| `tool_not_called: <tool>` | `<tool>` did **not** fire during the turn (the usual negative-scenario assertion, where `no_tools_called` is too blunt) |
| `tool_args: {tool: <name>, args: {...}}` | the last fired tool's args include these |
| `slot_filled: {<slot>: <value>}` | the slot holds that value after the turn |
| `slot_status: {<slot>: open\|filled\|pending\|deferred}` | slot is in that state |
| `next_action: <a>` | engine phase is `announce\|fire\|next_question\|readback\|terminal\|gate\|preempt` |
| `status: <s>` | session status is `in_progress\|complete\|zombie\|escalated` |
| `disposition: complete\|escalate\|transfer\|handoff` | the flow ended that way (`complete` covers `zombie`) |
| `active_flow: <id>` | the session switched to / is running that flow (routing) |

## What offline CANNOT prove (use the live tier instead)

The simulator fakes the clock and runs legs in order, and never calls an LLM. So model-decided
routing (`steering*`), improvised wording (`improvise*`), real timeouts (`*_timeout`), and true
concurrency (`*_fan_out`) are **not** decidable here. Those examples are marked `tier: live` in
`registry.yaml` and covered by `evals_live/` (see the plan). Do not force a misleading offline
eval for them.

## Discovering setter / task names while authoring

Run the dev aid to print the engine trace (setters, fires, spoken text) for a scripted probe:

```
PYTHONPATH=src python -m tests.evals.harness track_shipment "where is my package"
```
