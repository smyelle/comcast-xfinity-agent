# Head-intent accuracy — honest results

Two-level intent detection: an L1 **steering router** picks the golden intent *category*
(billing, sales, …), then a scoped **head-intent** LLM pick chooses the leaf within that
category (Approach 1 — see `../head_intents.py`). This file records the measured accuracy
and how to reproduce it.

## The test set

`head_intent_testset.json` — **233 utterances**, all **76 leaves** across the **15 defer
categories**. Disjoint from the L1/L2 cues and the golden/held-out corpora (0 collisions),
so it measures the **model**, not the deterministic cue backstop or in-sample memory.
Generator: `gen_head_intent_testset.py`. Three numbers are always kept separate:

- **(a) L1 routing** — `route == expected_flow`
- **(b) overall intent** — `leaf == expected_head_intent` (the headline)
- **(c) L2 | correct L1** — leaf correct *among rows that routed to the right category*
  (the honest within-category classifier number)

## Result — before vs after the L1 hill-climb

The 233-set showed L1 routing was the entire bottleneck: L2 was already ~99% conditional
on a correct L1. The climb added **semantic coverage** to the router's `ROUTE_CATALOGUE`
(the `disambiguation_main_menu` catch-all topics + the billing/payments boundary) — no
verbatim eval strings, so it generalizes.

Discipline: a stratified **train / frozen-held-out** split (`split_head_testset.py`, 2/3 :
1/3). Only train misses were inspected while editing; the held-out was measured once, at the
end. Train ≈ held-out ⇒ generalization, not overfitting.

| slice | metric | baseline (`8d308d92`) | after climb (`47185b4a`) |
|---|---|---|---|
| **frozen held-out (77)** | L1 routing | 76.6% | **93.5%** |
| | **overall intent** | 75.3% | **93.5%** |
| | L2 \| correct L1 | 98.3% | **100%** |
| train (156) | L1 routing | 76.9% | 93.6% |
| | overall intent | 76.3% | 93.6% |
| **full 233** | **overall intent** | ~76% | **93.6%** |
| | L2 \| correct L1 | 98.9% | **100%** |

The `47185b4a` / `8d308d92` apps above were deleted to free deploy quota; their raw rows
remain under `tests/results/` (`climb_v1_47185b4a_text.json`, `baseline_8d308d92_text.json`).

## After merging the trunk (`initial-push`) — app `338e9113`

Merging `initial-push` (steering→repair rename #15, guardrails #13, DAG-tool-hiding #17) into
the branch shifted routing. Measured on the merged app `338e9113` (`climb_v2_338e9113_text.json`):

| slice | L1 routing | overall intent | L2 \| correct L1 |
|---|---|---|---|
| frozen held-out (77) | 88.3% | **88.3%** | 100% |
| full 233 | 89.7% | **89.3%** | 99.5% |

Down from 93.5% — **9 regressions, 0 improvements** vs `47185b4a`, in three buckets:
1. the trunk's deliberate streaming→repair change (~3-4 rows: "Netflix login", "Xumo isn't
   working", "set up Xumo box" now route to repair/activations — a miss vs the golden label
   but arguably correct behavior);
2. disambig-description dilution from the merge (address-correction leaked to `billing`,
   ticket-status to `transfers`) — fixable by tightening the merged description;
3. two `→(none)` no-route turns (payments/sales) — likely `after_model`/guardrail turns or
   run-to-run noise.

FOLLOW-UP: a targeted re-tune of the merged `disambiguation_main_menu` / `billing` boundary to
re-own address + ticket-status (keeping the trunk's streaming→repair intent) is expected to
recover ~92%.

## Audio — the gains survive TTS→STT

Representative 45-scenario slice (3 per defer category, `build_audio_repr.py` →
`audio_repr.json`) driven through the full **TTS → CES STT → router** path, compared to
**text on the identical scenarios**:

| same 45 scenarios | L1 routing | overall intent | L2 \| correct L1 | transport errors |
|---|---|---|---|---|
| text | 41/45 = 91.1% | 91.1% | 100% | — |
| **audio** | 41/45 = **91.1%** | **91.1%** | **100%** | **0** |

Identical — all 4 misses are the same rows in both modalities (a "cable box"
cross-category ambiguity spanning service_center / technical_television / activations /
equipment_swap), a golden-taxonomy boundary issue, not an audio artifact.

## Raw evidence + how to reproduce

Raw per-row results are under `tests/results/`: `baseline_8d308d92_text.json`,
`climb_v1_47185b4a_text.json`, `climb_v1_47185b4a_audio.json` (pre-merge), and
`climb_v2_338e9113_text.json` (post-merge).

Build + deploy (post-merge workflow — the trunk needs current flows `main`, and #16 removed
the in-repo source app so the build grafts from an EXTERNAL source that carries the
`environment.json` resolving `inspectTemplate`/`deidentifyTemplate`/BQ `$env_var`):

```bash
# flows main must be current (guardrails API PR #631): update CXAS_LABS to origin/main.
# extract the source app + environment.json from a pre-#16 commit into $SRC, then:
CXAS_LABS=<flows-main> PYTHONPATH=$CXAS_LABS/packages/flows/src COMCAST_SOURCE=$SRC \
  python build.py --out ./built
cxas_scrapi ... push --app-dir built --env-file built/environment.json \
  --display-name xa-l1-climb-vN --project-id ces-deployment-dev --location us

# eval (full 233 + per-split breakdown):
APP_ID=<new app id> python tests/head_intent_analysis.py \
  --corpus tests/head_intent_testset.json --tag rerun --workers 12 \
  --save tests/results/rerun_text.json
python tests/split_analyze.py --results tests/results/rerun_text.json
# audio slice:
APP_ID=<new app id> python tests/head_intent_analysis.py --corpus tests/audio_repr.json \
  --modality audio --tag rerun_audio --workers 8
```

Apps live in `ces-deployment-dev` / `us`; composite-model apps cannot be updated in place, so
each iteration is a fresh app (the region has a per-project app quota — delete stale apps to
free slots). Current: `338e9113-09db-48ed-a658-179a23ddd75d` (`xa-l1-climb-v2-merged`) is the
post-merge build; the pre-merge `47185b4a` (93.5%) and baseline `8d308d92` were deleted for
quota (raw rows retained here).
