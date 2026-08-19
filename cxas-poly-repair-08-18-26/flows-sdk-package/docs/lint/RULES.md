# flows lint: rule catalog

Status: PROPOSED (plan). Companion to [DESIGN.md](DESIGN.md).

This is the seed catalog for `flows lint`. Codes are permanent once shipped.
Each rule lists: code, severity (default), what it detects, why it matters (with
the engine/validator mechanism), a detection sketch, and the fix (message +
`fix_id` + whether autofixable). FILE:LINE citations point at the grounding in
today's code.

## Legend

**Code scheme:** `FL` + category letter + 3 digits. Prefix == category.

| Prefix | Category | Blocking by default? |
| --- | --- | --- |
| `FLW` | Wiring & dependencies | error -> yes; needs_review -> no |
| `FLR` | Reachability & flow control | error -> yes |
| `FLB` | Robustness & failure handling | error -> yes; warning -> no |
| `FLM` | Model reliance & determinism | warning -> no |
| `FLC` | Conversation design | info -> no |
| `FLV` | Voice & copy | needs_review / info -> no |
| `FLA` | Multi-agent | error -> yes |
| `FLP` | Performance | warning -> no |

**Severities:** `error` (breaks build/deploy or guaranteed-bad runtime),
`warning` (likely bug / degraded), `info` (best-practice polish),
`needs_review` (can't be statically decided; eyeball it). See DESIGN.md section 5.

**Status column:** `NEW` = net-new rule; `PORT` = wraps/replaces an existing
`_check_*` (message preserved on port); `WRAP` = surfaces a blessed-validator
diagnostic earlier / better.

---

## FLR - Reachability & flow control

### FLR001 - on_exhaust `open_slot` has no reachable next question  `error`  NEW

**This is issue #596 case 2 and the highest-confidence rule (it has a runtime oracle to mirror).**

Detects: a task `on_failure.on_exhaust.open_slot` (or `no_input.on_exhaust.open_slot`)
whose target slot, once armed at the exhaust state, leads to no reachable next
askable question - so the engine silently degrades to `"An error occurred."`.

Why: at runtime the engine arms `open_slot`, calls `_find_next_question`, and if
nothing is reachable it sets status complete and speaks
`exhaust.get("say", "An error occurred.")`, logging
`task_exhaust_open_slot_unreachable`. Grounding:
`engine/framework/tools/slot_filling_engine/python_function/python_code.py:3383-3405`
(and the no_input twin at `:6428-6466`). The blessed validator only checks that
`open_slot` names a *declared* slot (`validate_dag_config/...:4847-4857`) - presence,
not reachability.

Detection sketch: reuse `LintContext`'s reachability fixpoint (the same
producer/consumer walk `_check_reachability` uses). From the exhaust state
(the failed task's slots cleared per `clear_slots`), compute whether any user
slot is askable after arming `open_slot`. If not -> fire.

Fix (`fix_id: exhaust_replace_open_slot_with_say`, autofixable):
"Task `X` exhaust opens slot `S`, but from the exhaust state no further askable
slot is reachable, so the engine speaks \"An error occurred.\" instead. Replace
`on_exhaust:{open_slot: S}` with `on_exhaust:{say: '<terminal line>', then: <tool>}`
to control the terminal message and action."

Does NOT fire when: a next question IS reachable from the offer slot (the
intended in-flow re-ask), or when `on_exhaust` already uses `say`/`then`/`component`.

---

### FLR010 - unreachable slot / task (`requires` cannot be satisfied)  `error`  WRAP

**This is issue #596 case 3 - already covered by the blessed validator; the linter surfaces it earlier with named producer/consumer sites.**

Detects: an announce/slot/task that `requires` (or `inputs`) a slot no reachable
producer can fill (e.g. a missing `result_slot` for a task `out_key`).

Why: the blessed `_check_reachability` already flags this
(`validate_dag_config/...:3521-3650`) but the issue reports it only surfaced
*after emit*, because the producer wiring materializes during emit's assembly.
The linter runs post-assembly / pre-deploy and re-emits the diagnostic with the
producing and consuming sites named.

Status: do NOT reimplement the solver; `blessed_adapter` maps the existing
diagnostic into a `Finding` and enriches the message with the named sites. Mark
redundant-with-validator in docs.

Fix (`fix_id: add_producer_for_requires`): "Slot `S` requires `R`, which no
reachable task/announce/setter produces. Add a `result_slot`/`out_key` on the
producing task, or an announce/event source for `R`."

---

### FLR020 - journey op-terminal gating is unsound  `error`  PORT

Ports `_check_journey_gates` (`authoring/build.py:567`, wired at `:878`). A
Shape-B journey (intent slot with `option_cues`, >=2 terminals) whose op
terminals are ungated, gated on an unknown intent value, missing for a value, or
duplicated. Message preserved verbatim (already best-in-class: names the terminal
+ condition + intent slot, states the invariant, lists valid values).

### FLR021 - orphaned slot (no fill mechanism)  `warning`  WRAP

Wraps the blessed `_check_orphaned_slots` (`validate_dag_config/...:3500`): a slot
with no setter (user), no `event_key` (event), or `task:<unknown>` source.

---

## FLW - Wiring & dependencies

### FLW003 - registered tool is never referenced (dead / unwired)  `needs_review`  NEW

**Issue #596 case 1.**

Detects: a tool with a body (`@flows.tool`, `App.tool_bodies`) or otherwise
`available` that is referenced by NO task `tool`, slot `setter`, `correction_tool`,
`on_exhaust.then`, or announce. Example from the issue: `set_transfer_subject` sat
dormant across sessions.

Why: only the reverse is checked today - `_check_extra_tools`
(`authoring/build.py:787`) and the blessed `_check_tool_availability`
(`validate_dag_config/...:3744`) flag a *referenced* tool with no body. Nothing
flags a body nobody calls. It is dead weight and usually a wiring mistake (the
author meant to attach it), but it can be intentional (WIP), hence `needs_review`.

Detection sketch: `available_with_body - referenced_tool_set` from
`LintContext`'s tool graph. Exclude framework/builtin tools and declared remote
agents (legitimately body-less / externally referenced).

Fix (`fix_id: wire_or_remove_tool`): "Tool `T` has a body but is referenced by no
task, setter, correction_tool, on_exhaust.then, or announce. Wire it to the
task/slot that should call it, or remove it. (If intentional, suppress with
`lint_ignore=['FLW003: <reason>']`.)"

Does NOT fire when: the tool is a framework tool, a declared remote agent /
search tool (body-less by design), or referenced anywhere in the graph.

### FLW001 - author-scoped extra tool resolves to nothing  `error`  PORT

Ports `_check_extra_tools` (`authoring/build.py:787`). An `extra_tools` name with
no framework tool / `_dag` / remote agent / body behind it. Message preserved
(names owner + missing names + three concrete fixes).

### FLW002 - tool body reads an un-inlined global  `error`  PORT

Ports the unresolved-globals check in `_run_validation`
(`authoring/build.py:856-866`): a `@flows.tool` body referencing a module-level
constant/helper not inlined into the emitted file -> `NameError` on first call.
Message preserved (names the tool + names + the exact runtime error + two fixes).

---

## FLB - Robustness & failure handling

### FLB001 - task `success_check` key absent from tool's return model  `error`  PORT

Ports `_check_task_success_keys` (`authoring/build.py:805`). The "silent hang":
intake reads `success = bool(response_data.get(success_check))`, so a key the tool
never returns makes the task look failed every call and fill nothing. Message
preserved.

### FLB010 - async tool fired without `awaits`  `error`  PORT
### FLB011 - `awaits` on a synchronous tool (dead config)  `warning`  PORT

Port `_check_async_pairing` (`authoring/build.py:1241`). FLB010: async tool with
no `awaits` -> CES answers with a "pending" placeholder that reads as failure and
escalates on the first fire. FLB011: `awaits` on a sync tool -> the wait never
engages. Messages preserved (name task + tool + mechanism + exact
`awaits=flows.awaits(max_turns=...)` fix).

### FLB020 - search-tool task uses a non-search success key  `error`  PORT

Ports `_check_search_tasks` (`authoring/build.py:986`). `success_check`/`outputs`
key not in `{search_query, snippets, instructions}`. Message preserved.

### FLB030 - A2A task uses a non-reply success key  `error`  PORT
### FLB031 - A2A task succeeds on the `task` (accepted) reply, not `message`  `warning`  PORT

Port `_check_a2a_tasks` (`authoring/build.py:1159`). FLB030: key outside the A2A
reply oneof (`message`/`task`). FLB031: succeeding on the SUBMITTED receipt, not
the COMPLETED answer. Messages preserved (fix = `flows.delegate()`).

### FLB040 - OpenAPI task fires a toolset, not a tool  `error`  PORT

Ports `_check_openapi_tasks` (`authoring/build.py:1079`). Firing a toolset name or
the in-sandbox `<toolset>_<operationId>` symbol. Message preserved (fix spells out
`flows.api_tool(name, toolset, operationId)`).

### FLB050 - `mock_apis=True` but an API tool declared no mock  `warning`  PORT

Ports `_check_api_mocks` (`authoring/build.py:1135`). An "offline" app still hits
the real API. Message preserved (three ways to add a mock).

### FLB060 - failure/exhaust path has no escalation or transfer target  `info`  NEW

Detects: an `on_failure`/`on_exhaust`/escalate `ControlBlock` that terminates
without a `transfer_to`/`then` transfer target, or an escalate block with no
pre-terminal summary `tasks` chain (cold transfer). Grounding: `ControlBlock`
(`config/models.py:520`), `on_exhaust.then` default `transfer_to_human`
(`authoring/dsl.py:459`).

Fix (`fix_id: add_escalation_target`): "Escalation/exhaust path on `X` ends
without a transfer target; the caller hits a dead terminal. Add
`on_exhaust:{then: transfer_to_human}` or a `ControlBlock.transfer_to`, and a
summary `tasks` chain to avoid a cold hand-off."

---

## FLM - Model reliance & determinism

### FLM001 - multi-outcome branch with no proceed-turn directive  `warning`  NEW

**Issue #596 case 4 - the higher-value design-lint.**

Detects: a slot/task that branches into N>=2 conditional outcomes where the
following PROCEED turn(s) provide no `then_directive` (task) / verbatim steering -
so the model must infer the next question and tends to improvise the wrong
branch's question.

Why: on a proceed turn the engine computes the deterministic next-question
message but discards it (delivered only on the PREEMPT path), arming it merely as
an empty-render backstop in `_render_fallback`. Grounding:
`slot_filling_engine/...:6543-6553`. `then_directive` exists on tasks and reliably
steers (`config/models.py:497`; engine `:3261`) but there is no slot->slot
equivalent (see the framework-gap issue this rule points at). Proved on Elevance:
raw gemini-3.5-flash follows the branch 5/5 when given the directive.

Detection sketch: find a slot/task whose downstream fans into >=2 outcomes
(condition-gated tasks/slots keyed on the same slot, or a task with multiple
`on_complete`/outcome paths). For each proceed transition out of that branch,
check for `then_directive`/`then_say`/verbatim steering. Fire when absent. Default
`warning` (heuristic; promote/suppress per app) - can be set to `needs_review` by
teams wary of false positives.

Fix (`fix_id: add_then_directive`): "Branch on slot `S` has N outcomes but the
following proceed turn provides no `then_directive`/verbatim steering, so the model
must infer the question and may improvise the wrong branch. Add a `then_directive`
on the task (or, for a slot->slot proceed, once the framework supports it, a
slot-level directive - see issue #599)."

Does NOT fire when: the branch is single-outcome, the proceed turn is a preempt
(directive delivered directly), or a `then_directive`/verbatim line is present.

---

## FLC - Conversation design (best-practice; mostly `info`)

Grounding: `docs/slot-studio-conversation-design.md` (the capability catalog),
`config/models.py` field vocabulary.

### FLC101 - asked user slots but no `no_input` (silence) ladder  `info`  NEW
A voice flow with user-asked slots and no `no_input` ladder degrades on caller
silence. `NoInput` model `config/models.py:613`; `Config.no_input:646`. Reward
`reprompts` + `on_exhaust` (+ `hold_*` for hold handling). `fix_id: add_no_input_ladder`.

### FLC110 - asked slot has no escalating no-match ladder  `info`  NEW
A slot whose `validation` has a single retry line, not an escalating `reprompts`
ladder + terminal `on_exhaust`. `Validation.reprompts:353`, `errors:345`; the DSL
`user_slot` already builds a 2-rung ladder defaulting `on_exhaust.then` to
`transfer_to_human` (`authoring/dsl.py:452-459`). `fix_id: add_nomatch_ladder`.

### FLC120 - slow/remote tool task with no latency masking  `info`  NEW
A task firing a slow / remote / OpenAPI / async tool with no `while_running` /
`filler_say` leaves dead air. `Task.while_running`/`filler_say`
(`config/models.py:507-508`). `fix_id: add_filler_say`.

### FLC121 - `awaits` block with no spoken wait cue  `info`  NEW
An `awaits` with no `say`/`while_waiting` is a silent wait. `Awaits`
(`config/models.py:449-469`). `fix_id: add_await_say`.

### FLC130 - transfer part without disclaimer / context  `info`  NEW
A `transfer`/handoff `TransferPart` with no `disclaimer` (cold hand-off) or no
`context` (receiving agent loses caller state). `config/models.py:295-296`.
`fix_id: add_transfer_disclaimer` / `add_transfer_context`.

### FLC140 - digit-bearing user slot without readback  `info`  NEW
A phone/ZIP/SSN/DOB/account user slot without `requires_readback` +
digit/date `readback_fmt`. A `readback_verbatim` digit slot without
`readback_fmt: digits` reaches TTS as one giant number (`ReadbackDigits` note
`config/models.py:196-203`). `fix_id: add_digit_readback`.

### FLC150 - menu/enum slot on voice without DTMF twin  `info`  NEW
A menu/enum slot with `option_cues` but no `dtmf_map` on a voice surface; or a
`dtmf_map` whose keys diverge from `option_cues` (the divergence is already WARNed
by the blessed `_check_dtmf_optioncues_twins:1922`). `fix_id: add_dtmf_map`.

### FLC160 - cancel-with-return doesn't clear the slot it backs out of  `warning`  WRAP
Wraps the blessed `_check_cancel_menu_return` (`validate_dag_config/...:1860`): a
menu-returning cancel with empty/typo'd `clear_slots` re-asks the question the
caller just cancelled. `fix_id: fix_cancel_clear_slots`.

### FLC170 - long/high-friction flow with no `steer_back` escalation  `info`  NEW
A flow that can loop without `steer_back` soft/hard/escalate thresholds. `SteerBack`
(`config/models.py:582-588`). `fix_id: add_steer_back`.

---

## FLV - Voice & copy (spoken-text quality)

Spoken text fields (rule targets): slot `ask`/`hint`/`message`/`response[].text`/
`ask_variants`/`filler_say`/`push_back.say`/`push_back.reprompts`/`validation.errors`;
task `then_say`/`then_response[].text`/`then_say_variants`/`filler_say`; control
`say`/`confirm_say`/`declined_say`/`retry_say`; `Awaits.say`/`hold_say`/`hold_ack`/
`while_waiting`/`hold_reprompts`; `NoInput.reprompts`; `Validation.reprompts`;
`OnExhaust.say`; flow `all_done_say`/`filler_say`/`readback_response`.
(`config/models.py`, see DESIGN 6.) `then_directive` is deliberately excluded: it is an
instruction the model composes from, not verbatim TTS.

`LintContext.iter_spoken` yields these as `(node_kind, node, json_path, text)`.
A rule that needs the DELIVERY of a line — `partial`, `interruptable` — wants
`iter_spoken_parts`, which pairs each item with the response descriptor it came from
(None when the text is not part-shaped). `iter_spoken` keeps its four-field shape
because `LintContext` is exported and out-of-tree rules unpack it.

### FLV001 - dash in spoken copy will chop TTS  `needs_review`  NEW
**GAP: no lint today, but the engine itself follows the convention.** Flags em/en
dashes (`—`, `–`) and compound hyphens ("door-tag") in spoken fields. The engine's
own neutral fallback comment notes "no dash - dashes chop TTS"
(`slot_filling_engine/...`). `needs_review` because a hyphen can be legitimate
(brand names). Fix: replace with a comma / rephrase / spelled twin.
`fix_id: despace_dash` (autofixable for compound hyphens -> spaced words).

### FLV002 - audio tag in spoken copy is model-dependent  `warning`  NEW
Flags a bracket audio tag (`[whispers]`, `[calm]`) in caller-heard text. Measured
(ces-probes 84/87): honoured on `gemini-composite-v1`, and **read aloud as a word** on
`gemini-3.1-flash-live` — the caller hears "whispers". Which model runs is not knowable
from the config: `modelSettings` is in `deploy/prep.py:PRESERVE` and
`merge_live_settings` takes the live target's, so `App.model` is a guess and a tag is a
coin flip. `warning`, not error, because on a pinned composite target it is intentional
— suppress with `lint_ignore=['FLV002: composite only']`. Skips parts FLV003 owns.
`fix_id: strip_audio_tag`.

### FLV003 - audio tag in a `partial` part truncates the utterance  `error`  NEW
Flags an audio tag inside a response part marked `partial`. Measured on composite-v1
(ces-probes 86): the markup is read aloud AND the line truncates at ~1.9s against ~5s
for every other shape, so the caller hears "left bracket whispers right bracket" and
then half a sentence. `partial` is the holding-line shape — a latency filler or an A4
prefix — so this cuts off the line whose whole job was to cover a wait. `error`, and it
blocks a push through the `lint` pre-push gate. The engine strips the tag at runtime as
a backstop (`before_model._safe_spoken`); this is the authoring-time fix.
`fix_id: strip_audio_tag`.

### FLV010 - enum/intent value spoken as its raw key  `needs_review`  NEW
An enum/intent slot read back without a `values`/`prefix` spoken map speaks
`snake_case` keys aloud (e.g. `remove_lift`). `ReadbackPrefix.values` note
`config/models.py:182-184`. `fix_id: add_spoken_values`.

### FLV020 - digit string spoken unspaced  `needs_review`  NEW
A phone/ZIP/SSN slot preempted (`readback_verbatim`) without `readback_fmt: digits`
speaks one enormous number. Overlaps FLC140 but targets the copy, not the ladder.
`fix_id: set_readback_fmt_digits`.

### FLV030 - `{placeholder}` in `ask` may render empty  `warning`  WRAP
Wraps the blessed `_check_ask_format_requires` (`validate_dag_config/...:3972`): a
`{X}` in `ask` not in `requires`. `fix_id: add_requires_for_placeholder`.

---

## FLA - Multi-agent

### FLA001 - overlapping route phrasing / alias across sub-agents  `error`  PORT
Ports `_check_route_phrasings` (`authoring/build.py:446`). A phrasing/alias/cue
claimed by >1 sub-agent, a cue mapped to an unknown flow key, an empty cue list,
or a dup cue. Message preserved (names each collision + both owners).

### FLA002 - host routes reference undeclared agents / duplicate agent names  `error`  PORT
Ports `_check_multi_agent_wiring` (`authoring/build.py:503`). Message preserved.

### FLA010 - escalate/cancel block missing transfer target (multi-agent)  `warning`  WRAP
Wraps the blessed `_check_control_block_transfer` (`validate_dag_config/...:5477`).

---

## FLP - Performance

### FLP001 - fan-out group won't narrate progressively  `warning`  PORT
Ports `_check_fanout_lowering` (`authoring/build.py:1315`). A parallel group that
keeps the old batch shape - caller hears nothing until the slowest leg, then
everything at once. Message preserved (fix = give every leg a real body).

---

## Rule index (summary)

| Code | Title | Sev | Status |
| --- | --- | --- | --- |
| FLR001 | on_exhaust open_slot dead-end (#596-2) | error | NEW |
| FLR010 | unreachable slot/task requires (#596-3) | error | WRAP |
| FLR020 | journey op-terminal gating unsound | error | PORT |
| FLR021 | orphaned slot | warning | WRAP |
| FLW001 | extra tool resolves to nothing | error | PORT |
| FLW002 | tool body reads un-inlined global | error | PORT |
| FLW003 | registered tool never referenced (#596-1) | needs_review | NEW |
| FLB001 | success_check key not in return model | error | PORT |
| FLB010/011 | async pairing (no awaits / dead awaits) | error/warning | PORT |
| FLB020 | search-tool wrong success key | error | PORT |
| FLB030/031 | A2A wrong success key / accepted-not-answered | error/warning | PORT |
| FLB040 | OpenAPI toolset fired as a tool | error | PORT |
| FLB050 | mock_apis but unmocked tool | warning | PORT |
| FLB060 | failure path no escalation target | info | NEW |
| FLM001 | multi-outcome branch no directive (#596-4) | warning | NEW |
| FLC101 | no no_input ladder | info | NEW |
| FLC110 | no no-match ladder | info | NEW |
| FLC120 | slow tool no latency masking | info | NEW |
| FLC121 | awaits no spoken cue | info | NEW |
| FLC130 | transfer no disclaimer/context | info | NEW |
| FLC140 | digit slot no readback | info | NEW |
| FLC150 | menu slot no DTMF twin | info | NEW |
| FLC160 | cancel-return no clear_slots | warning | WRAP |
| FLC170 | no steer_back escalation | info | NEW |
| FLV001 | dash chops TTS | needs_review | NEW |
| FLV002 | audio tag is model-dependent | warning | NEW |
| FLV003 | audio tag in a `partial` part truncates | error | NEW |
| FLV010 | enum key spoken raw | needs_review | NEW |
| FLV020 | digit string unspaced | needs_review | NEW |
| FLV030 | ask placeholder may render empty | warning | WRAP |
| FLA001 | route phrasing collision | error | PORT |
| FLA002 | host wiring bad | error | PORT |
| FLA010 | escalate block no transfer target | warning | WRAP |
| FLP001 | fan-out not progressive | warning | PORT |

**Issue #596 coverage:** case 1 -> FLW003, case 2 -> FLR001, case 3 -> FLR010
(already covered by the validator; surfaced earlier), case 4 -> FLM001 (+ the
slot-level `then_directive` framework-gap, filed as #599).
