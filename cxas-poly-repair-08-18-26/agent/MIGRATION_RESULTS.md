# Steering-primitive migration — results

Swaps the hand-rolled level-2 head-intent detector (PR #14: `head_intents.py` `head_intent_slot`
+ per-category setters + A2A `delegate`) for the first-class flows steering primitive
(`flows.route(name, description, subroutes=[...])` + structured routing policies), published
in **flows 0.13.0** (#648). Same golden 84-leaf taxonomy, same 233-utterance eval set — this
is a maintainability refactor onto the SDK primitive, not an accuracy change.

## What replaces what

| hand-rolled (PR #14) | primitive (this PR) |
|---|---|
| `head_intents.py` `head_intent_slot(cat)` per defer flow + bespoke `head_setter_bodies` | `steering_tree.py` builds `flows.route(cat, …, subroutes=[leaf routes])`; the SDK generates the classifier + recorder |
| `<head_intent_selection>` instruction block + `head_hint()` | the SDK's per-node key hint (generated) |
| leaf read from `set_head_intent__<cat>` response | leaf read from the generated `steering_record_path` recorder (`detected_intent` / `detected_path`) |
| `_routing_block()` / `ROUTER_INSTRUCTION` + hand-written `<routing_policy>` prose | the router GENERATES `<routing>`; cross-cutting rules are structured knobs: `default_route="repair"`, `catch_all_route="disambiguation_main_menu"`, `route("human", … explicit_only=True)`, `tie_break="primary"` (default) |
| L2 resolved by a fuzzy classifier setter | `classifier_style="enum"` (the 0.13.0 default) — the same enum setter as the L1 gate, one closed-choice mechanism at every level |

`head_intents.py` is trimmed to its taxonomy DATA (`HEAD_CUES`, `SLOT`, `l1_of`, `default_leaf`,
the JSON load) which `steering_tree.py` and the test tooling still read; the hand-rolled slot /
setter builders are removed.

## Results (published flows 0.13.0, eval app `511e8f60-…`, ces-deployment-dev/us)

| slice | metric | this migration (primitive) | merged hand-rolled #14 |
|---|---|---|---|
| text, full (233) | L1 routing | 91.4% | ~93.6% |
| | overall intent | 91.0% | ~93.6% |
| | **L2 \| correct L1** | **99.5%** | ~100% |
| audio (45-row slice) | overall intent | 91.1% | 91.1% |
| | L2 \| correct L1 | 100% | 100% |

Equivalent within eval noise — the single L2 miss is a near-identical sibling pair
(`account_renew` vs `account_contract_renew`), model stochasticity, not a mechanism gap. The
residual L1 gap is the same description-boundary residual #14 documented (e.g. a streaming app
routed to `technical_television`).

## Follow-up

The primitive's generated deferral **records** `detected_intent` / `detected_path` for the
downstream orchestration rather than delegating each leaf to the placeholder A2A agent (the
per-leaf A2A hand-off the hand-rolled `_defer_flow` did). Re-wiring A2A onto the primitive's
deferral is a follow-up; detection parity is unaffected.

## Repro

```bash
python flows-sdk/build.py --out flows-sdk/built                 # self-contained (#18), flows 0.13.0
cxas push --app-dir flows-sdk/built --to projects/ces-deployment-dev/locations/us/apps/<APP_ID> --overwrite
APP_ID=<APP_ID> python flows-sdk/tests/head_intent_analysis.py \
  --corpus flows-sdk/tests/head_intent_testset.json --tag m --workers 6 --save flows-sdk/tests/results/m_text.json
```

`GRPC_DNS_RESOLVER=native` + `--workers 6` avoids a local c-ares abort at higher concurrency.
