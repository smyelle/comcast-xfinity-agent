# flows lint: build-time authoring linter (design + implementation plan)

Status: PROPOSED (design/plan, not yet implemented)
Owner: (tbd)
Related: issue #596 (flows build-time authoring linter), the aspirational `fix_synth.py` referenced in `config/validation.py`
Companion artifact: [RULES.md](RULES.md) (the rule catalog)

## 1. Why

`flows validate` already runs a rich authoring layer (`validate_app` in
`authoring/build.py:1292`) on top of the blessed `DagConfigValidator`, and it
catches real defects. But three things are missing:

1. A whole class of authoring mistakes only surface as **deployed-behavior**
   failures (a caller hears "An error occurred.", the model improvises the wrong
   branch question). Each one currently costs a build + deploy + eval cycle to
   find. See issue #596 for the concrete cases hit on the Elevance agent.
2. There is **no notion of "is this a GOOD flows agent"** at build time: no
   best-practice / design-quality feedback (reprompt ladders, latency masking,
   voice-copy hygiene, transfer disclaimers). Those defects never fail a build;
   they just make callers unhappy.
3. The current output is **flat `error:` / `warning:` prose** (`cli.py:98-110`)
   with no stable codes, categories, machine schema, or fix affordances, so
   neither a human triaging 30 findings nor a coding agent driving a fix loop
   gets what it needs.

`flows lint` is a static, deterministic authoring linter that runs over the
emitted flow config **before deploy**, classifies findings by category and
severity, and tells the author (human or agent) exactly how to fix each one.

Goal: **remove errors, bugs, and caller-experience defects at build time, and
guide authors toward best-in-class flows agents.**

## 2. Scope and boundaries (what it is NOT)

flows already has three adjacent surfaces. The linter must not blur into them.

| Surface | Question it answers | Keep as-is |
| --- | --- | --- |
| blessed `DagConfigValidator` (`engine/framework/.../validate_dag_config`) | "Is this config well-formed and internally consistent?" Must stay **byte-identical** to the deployed CES framework. | Yes. New rules NEVER go here. The linter WRAPS its output. |
| `flows validate` -> `validate_app` (`build.py`) | "Will this emit/deploy correctly?" (blockers + errors) | Becomes a filtered *view* of the linter (error+ severities), so there is one source of truth. |
| `flows check` -> `authoring/integrity.py` | "Did what I emitted actually land on disk?" (post-emit diff, framework drift) | Yes. Orthogonal; unchanged. |
| `cxas lint` / `cxas llm-lint` (GECX linter) | GECX hand-authored-prompt conventions. Produces ~99% noise on slot-filling apps (`agent_report_card/contracts.py:245`). | Separate product. We steal its **engine shape**, none of its **rule content**. |

**Design boundary (fidelity guarantee):** the blessed validator is the source of
truth for correctness and must not drift from CES. All new lint rules live in a
new authoring-side `flows/lint/` package, exactly as the existing `_check_*`
authoring oracles live in `build.py` "not in the blessed validator". The linter
consumes the blessed validator's diagnostics via the existing
`config/validation.py` mapper and adds authoring rules on top.

## 3. Design principles

Distilled from the cxas_scrapi review (what worked, what to avoid) and flows'
structured-DAG reality.

1. **Library first, CLI is a thin shell.** `lint_app(app) -> LintReport` returns
   data; it never calls `sys.exit` and never prints. The CLI decides exit codes
   and formatting. (cxas's `LintReport.print_and_exit` forced every consumer into
   a subprocess + stdout-JSON dance; `review/runner.py:288-299` had to reverse
   engineer it. We will not repeat that.)
2. **Structured anchors, not line-scraping.** Findings anchor to DAG nodes via
   the existing `NodeAnchor{kind, ref, field}` (`config/models.py:733`) plus a
   JSON path into the config, resolved from the parsed config. Never count
   newlines in generated text (cxas `_find_line`). This is what lets the Studio
   UI highlight the exact slot/task and what lets an agent patch the exact key.
3. **Stable, self-documenting rule codes.** `FL` + category letter + 3 digits
   (`FLR001`). Codes are permanent (never renumbered or reused) so suppressions
   and agent references survive. Prefix == category (cxas violated this: `config`
   -> `A`, `schema`/`variables` both -> `V`).
4. **Assert on the graph, never regex-as-semantics.** flows has a real parsed
   DAG; use it. cxas's `"agent_action" not in content` substring rules (T001) and
   IF/ELSE line-counting (I003) are the documented source of massive false
   positives. The only text rules we allow are genuine copy-style rules (dashes
   in spoken fields), and those are `needs_review`, never `error`.
5. **Parse once, share everything.** A single `LintContext` precomputes every
   index (slot/task maps, producer/consumer sets, reachability fixpoint, tool
   graph, condition graph) and every rule reads from it. No rule re-parses or
   re-reads. (cxas re-`ast.parse`d the same file up to 4x.)
6. **Dual audience, one schema.** Every finding renders to a human TTY line and a
   machine JSON record from the *same* record. The JSON is **versioned** and has
   **one** shape always (cxas emitted an unversioned array with a divergent
   "setup error" record).
7. **Every finding is actionable.** A finding without a concrete fix is a bug in
   the rule. Three tiers: (a) a fix imperative baked into the message, (b) a
   structured `fix_id` + hint, (c) an optional autofix patch. This matches the
   quality bar already set by the best `_check_*` messages (which name the node,
   state the runtime consequence, and spell out the exact API call).
8. **False-positive discipline is a feature.** The `needs_review` severity exists
   precisely so a rule that *cannot* be statically decided does not masquerade as
   an error. A rule that fires on a legitimate pattern is worse than a missing
   rule, because it trains authors (and agents) to ignore the linter. Every rule
   ships with a documented "does NOT fire when ..." near-miss test.
9. **Suppressible without code surgery.** Inline (`# flows: allow FLV001 - reason`)
   and config-file (`[tool.flows.lint]`) suppression, both requiring a reason
   where possible. cxas had config-only suppression, which is too coarse.
10. **Two-tier, deterministic core.** `flows lint` is fast and deterministic and
    runs on every build. Any future LLM/semantic pass (mirroring `cxas llm-lint`)
    is a separate opt-in command and never blocks the build gate.

## 4. Architecture

New package `packages/flows/src/flows/lint/`:

```
flows/lint/
  __init__.py        # public API: lint_app, lint_configs, LintReport, run_rules
  models.py          # Finding, LintReport, LintConfig, Category, RuleMeta
  registry.py        # @rule decorator, RULES registry, selection/severity resolution
  context.py         # LintContext: build-once indices + reachability + tool graph
  runner.py          # orchestration: build context -> run rules -> assemble report
  render.py          # human TTY, JSON, (optional) SARIF renderers
  suppress.py        # inline + config suppression resolution
  fix.py             # autofix application (implements the aspirational fix synthesis)
  config.py          # load [tool.flows.lint] from pyproject / flows-lint.toml
  rules/
    __init__.py      # imports every rule module (registration side-effect)
    wiring.py        # FLW*
    reachability.py  # FLR*
    robustness.py    # FLB*
    model_reliance.py# FLM*
    conversation.py  # FLC*
    voice.py         # FLV*
    multi_agent.py   # FLA*
    performance.py   # FLP*
    blessed_adapter.py  # adapts DagConfigValidator diagnostics into Findings
```

### 4.1 The Rule model

A rule is a small, pure, stateless object registered by a decorator (adopted from
cxas, which itself is "inspired by pylint/ruff"):

```python
@rule(
    code="FLR001",
    category=Category.REACHABILITY,
    severity="error",
    title="on_exhaust open_slot has no reachable next question",
    docs="lint/FLR001",          # deep-link slug into flows-docs
)
class ExhaustOpenSlotDeadEnd(Rule):
    def check(self, ctx: LintContext) -> Iterable[Finding]:
        for cid, cfg in ctx.configs.items():
            for task in ctx.tasks(cid):
                ...
                yield self.finding(
                    ctx, cid, anchor=NodeAnchor(kind="task", ref=task.name,
                        field="on_failure.on_exhaust.open_slot"),
                    message=(f"Task {task.name!r} exhaust opens slot {slot!r}, but "
                             "from the exhaust state no further askable slot is "
                             "reachable, so the engine speaks \"An error occurred.\" "
                             "instead. Use on_exhaust:{say, then} to control the "
                             "terminal message."),
                    fix_id="exhaust_replace_open_slot_with_say",
                    rationale=("open_slot only falls through when a NEXT user "
                               "question is reachable; at a terminal failure it "
                               "degrades to the engine error fallback."),
                    related=["engine log tag: task_exhaust_open_slot_unreachable"],
                )
```

`Rule` base carries the class-level metadata (`code`, `category`,
`default_severity`, `title`, `docs`) and one method `check(ctx) -> Iterable[Finding]`.
`self.finding(...)` stamps the code + resolved severity onto a `Finding`. Rules
never read files or config directly; everything is on `ctx`.

Parameterized rules (one class instantiated N times from a table, e.g. the
per-tool-shape checks for search/A2A/OpenAPI) use a simple constructor loop, not
`type()`-with-closures metaprogramming (cxas `schema.py` did the latter; it is
hard to read).

### 4.2 LintContext (the build-once read model)

Constructed exactly once from the assembled configs (the same
`(all_map, bodies, available)` that `validate_app` already computes via
`_assemble` / `_assemble_multi`). Holds:

- `app`, `configs: {cid: dict}`, `bodies: {tool: source}`, `available_tools`.
- Per-config indices: `slot_by_name`, `task_by_name`, `terminals`, `user_slots`,
  `intent_slots`, spoken-text field index.
- **Producer/consumer graph** and the **reachability fixpoint** (which slots are
  fillable from bootstrap/events/setters/task-outputs), computed once and shared.
  This reuses the same algorithm the blessed validator's `_check_reachability`
  implements (`validate_dag_config/...:3521`) and the engine's
  `_find_next_question` uses at runtime, so the linter's verdict matches runtime.
- **Tool reference graph**: referenced-by map (which task/setter/exhaust/announce
  names each tool) and the registry facts (`registered_output_keys`,
  `registered_unresolved_globals`).
- Cross-config: component graph, routing chain, host routes.

Rationale: the two expensive operations (reachability fixpoint, tool graph) are
O(nodes^2) worst case; computing them once and handing them to ~40 rules keeps
the whole lint well under budget (see Performance).

### 4.3 Finding and LintReport (reuse existing models)

flows already defines the output vocabulary in `config/models.py`; the linter
extends rather than reinvents:

- `Severity = Literal["error", "warning", "info", "needs_review"]` (`models.py:729`) - reuse as-is.
- `NodeAnchor{kind, ref, field}` (`models.py:733`) - reuse.
- `DiagnosticFix{label, patch}` (`models.py:746`) - reuse for autofix.

`Finding` extends `Diagnostic` with the linter-specific fields:

```python
class Finding(BaseModel):
    code: str                    # "FLR001"  (stable)
    category: Category           # "reachability"
    severity: Severity           # resolved (default or overridden)
    title: str                   # short human label
    message: str                 # full human message, ends with a fix imperative
    anchor: NodeAnchor | None    # structured node anchor
    location: Location           # {config_id, node, json_path}
    fix: DiagnosticFix | None    # optional autofix patch
    fix_id: str | None           # stable fix identifier for a synthesizer
    rationale: str | None        # the "why it matters" (also shown by --explain)
    docs_url: str | None         # deep link to the rule's doc page
    related: list[str] = []      # e.g. engine log tags, sibling findings
    suppressed_by: str | None    # set if a suppression matched (kept, not dropped)

class LintReport(BaseModel):
    schema_version: int          # bump on any breaking JSON change
    findings: list[Finding]      # sorted deterministically
    summary: Summary             # counts by severity + category + fixable count
    ran_rules: list[str]         # codes actually executed (after selection)
```

`validate_app`'s existing `(errors, warnings)` return is derived from a
`LintReport` filtered to `error`/`warning`, preserving back-compat.

### 4.4 Renderers

- **Human TTY** (`render.py`): grouped by severity then category, colorized,
  ruff/eslint-flavored:

  ```
  member_flow > task 'verify_member' > on_failure.on_exhaust.open_slot
    error  FLR001  on_exhaust open_slot has no reachable next question
           Task 'verify_member' exhaust opens slot 'wrap_choice', but from the
           exhaust state no further askable slot is reachable, so the engine
           speaks "An error occurred." Use on_exhaust:{say, then} instead.
           fix: replace open_slot with an explicit terminal say+then   (autofixable)
           docs: https://.../flows-docs/lint/FLR001

  Summary: 2 errors, 5 warnings, 3 info, 1 needs-review  (3 autofixable)
  ```

- **JSON** (`--format json`): `{schema_version, findings: [...], summary}` - one
  record shape always. This is the primary coding-agent interface (see 8).
- **SARIF** (`--format sarif`, optional / later): for GitHub code-scanning. Anchors
  to the App module file; node-level location rides in `properties`.

## 5. Severity model and gating

Reuse the existing four severities; assign gating semantics modeled on
`deploy/gates.py` (which already distinguishes blocking vs advisory checks):

| Severity | Meaning | Blocks emit/deploy? |
| --- | --- | --- |
| `error` | Will break at build/deploy, or a guaranteed-bad runtime behavior (dead-end, unresolved ref, silent hang). | Yes. |
| `warning` | Likely bug or degraded behavior; not fatal. | No by default; `--strict` promotes to blocking. |
| `info` | Best-practice / polish; the agent works but is not best-in-class. | No. |
| `needs_review` | Cannot be statically decided; a human/agent should eyeball it. | No. |

- One severity path only. A rule declares a `default_severity`; config can
  override per-rule / per-path. There is no second code path with different
  semantics (cxas clobbered rule severity on one path but not the other).
- `flows validate` == `flows lint` filtered to `error` (+ blockers). `flows lint
  --strict` == treat `warning` as blocking too.

## 6. Rule taxonomy (categories / classes)

Eight categories. Full rule list with codes, detection, and fixes is in
[RULES.md](RULES.md); summary here.

| Prefix | Category | Covers | Seeds from |
| --- | --- | --- | --- |
| `FLW` | Wiring & dependencies | dead/unwired tools, unresolved refs, dependency gaps, tool-body globals | #596 #1, #3; `_check_extra_tools`, unresolved-globals |
| `FLR` | Reachability & flow control | open_slot dead-ends, unreachable slots/tasks, orphans, journey gating | #596 #2; `_check_journey_gates` |
| `FLB` | Robustness & failure handling | success_check mismatch, on_failure/exhaust shape, async pairing, tool-shape (search/A2A/OpenAPI), mocks | `_check_task_success_keys`, `_check_async_pairing`, `_check_a2a_tasks`, `_check_openapi_tasks`, `_check_search_tasks`, `_check_api_mocks` |
| `FLM` | Model reliance & determinism | proceed-turn improvisation after multi-outcome branch | #596 #4 |
| `FLC` | Conversation design | reprompt/no-input/no-match ladders, latency masking, transfer disclaimer+context, cancel-return, steer-back, DTMF twin, readback | best-practice corpus |
| `FLV` | Voice & copy | dashes chop TTS, enum-key spoken raw, digit formatting, robotic menu phrasing | best-practice corpus (GAP: no lint today) |
| `FLA` | Multi-agent | route-phrasing collisions, host wiring, transfer targets | `_check_route_phrasings`, `_check_multi_agent_wiring` |
| `FLP` | Performance | fan-out progressive narration, dead config, redundant emit weight | `_check_fanout_lowering` |

Code ranges are per-category `001..`. Codes are permanent.

## 7. CLI surface

New subcommand in `cli.py`, mirroring `_cmd_validate`:

```
flows lint <module>                       # human output, exit 1 iff errors
  --format {human,json,sarif}             # default human
  --select FLR001,FLW003 | --select-category reachability,voice
  --ignore FLV001,FLC402
  --strict                                # warnings block too (promote to error gate)
  --fix                                   # apply safe autofixes, then re-lint
  --explain FLR001                        # print rule rationale + bad/good example, exit 0
  --list-rules [--format json]           # dump the full rule catalog, exit 0
  --config path/to/flows-lint.toml        # explicit config (else pyproject discovery)
```

**Exit codes (documented, stable):**

| Code | Meaning |
| --- | --- |
| 0 | No blocking findings (clean, or only warning/info/needs_review without `--strict`). |
| 1 | Blocking findings present (`error`, or `warning` under `--strict`). |
| 2 | Bad invocation / config error (distinct from lint findings). |

`flows emit` calls `lint_app` internally and refuses on `error` (the existing
fail-closed emit contract, `packages/flows/README.md:43`), so the linter runs on
every build whether or not the author invoked it directly.

## 8. Consumption by coding agents (first-class)

Coding agents are a primary user. Affordances:

1. **`--format json` with a versioned, single-shape schema.** Each finding is
   self-contained: `code`, `severity`, `category`, human `message`, structured
   `fix`/`fix_id`, `docs_url`, and a `location.json_path` an agent can navigate or
   patch directly. `schema_version` lets an agent guard against format drift.
2. **`--list-rules --format json`** dumps the whole catalog (code, category,
   severity, title, docs_url) so an agent can discover the ruleset without docs.
3. **`--explain <code>`** returns the rationale + a good/bad example for one rule
   - an agent can query a single rule cheaply instead of fetching a doc page.
4. **`--fix`** applies safe autofixes and reports what changed, enabling a
   deterministic lint -> fix -> re-lint loop. Non-autofixable findings carry a
   `fix_id` + hint the agent can act on.
5. **Deterministic ordering** (config_id, then node, then code) so re-runs and
   diffs are stable.
6. **Library API** (`lint_app(app) -> LintReport`) so an in-process agent
   (Specter, slotfill_migration) consumes the pydantic report directly, no
   subprocess.

## 9. Fix guidance (three tiers)

Every finding must offer at least tier 1.

1. **Message with the fix baked in (mandatory).** Ends with a concrete
   imperative that spells out the exact edit / API call. This is the bar the best
   `_check_*` messages already hit, e.g. `_check_extra_tools`: "Give each one a
   body (@flows.tool, or App.tool_bodies={'name': source}) or declare it as a
   remote agent". Every new rule matches this quality.
2. **Structured `fix_id` + hint.** A stable identifier a fix synthesizer / Studio
   can dispatch on (flows already threads `fix_id` through
   `config/validation.py:256-269`; the framework validator already emits
   `fix_id=add_ask` etc.). Enables one-click ("wand") remedies in Studio.
3. **Autofix patch (`fix.py`, gated behind `--fix`).** A `ConfigPatch`
   (`DiagnosticFix.patch`) applied to the config, only for mechanically-safe,
   deterministic edits (add a missing `hint`, convert `open_slot` -> `say+then`,
   add a `dtmf_map` twin from `option_cues`). This finally implements the
   `fix_synth.py` that `config/validation.py` promises but that does not exist
   yet. Never autofix a `needs_review` finding.

Each rule also owns a doc page under `flows-docs/content/lint/<code>.md`
(rationale, the engine mechanism it protects against, a bad/good example). The
`docs_url` on every finding is a stable deep link.

## 10. Suppression and configuration

**Config file** - `[tool.flows.lint]` in `pyproject.toml`, or `flows-lint.toml`:

```toml
[tool.flows.lint]
select = ["ALL"]                 # or explicit codes/categories
ignore = ["FLV001"]             # globally off
strict = false

[tool.flows.lint.severity]      # per-rule severity overrides
FLC402 = "info"                  # downgrade
FLW003 = "warning"              # promote

[tool.flows.lint.per-agent]     # per-sub-agent overrides (multi-agent apps)
"billing" = { ignore = ["FLC201"] }
```

**Inline / App-level suppression** (finer than cxas, which had config-only):

- App-level: `flows.App(..., lint_ignore=["FLV001"])`.
- Per-node in the DSL: `flows.slot(..., lint_ignore=["FLC402"])` /
  `flows.task(..., lint_ignore=["FLM001"])`. Rides into the config as a stripped
  `_lint_ignore` key (removed at emit, like `sensitive`).
- A suppressed finding is **kept** in the report with `suppressed_by` set (not
  silently dropped), so `--format json` still shows an agent what was silenced
  and why. The TTY view hides them unless `--show-suppressed`.

Where a suppression is authored, a reason is encouraged (`lint_ignore=["FLV001:
brand name has a hyphen on purpose"]`) and surfaced in the report.

## 11. Performance

- Single `LintContext` build; rules are O(nodes) reads over prebuilt indices.
- The two O(nodes^2)-worst-case computations (reachability fixpoint, tool
  reference graph) run once in the context, not per rule.
- No I/O in rules; tool bodies are already in memory from assembly.
- Target: < 100 ms for a typical app (tens of slots/tasks), < 500 ms for a large
  multi-agent app. A perf test with a synthetic large app guards the budget.
- Determinism: no wall-clock, no set iteration order leaks; findings sorted
  before return.

## 12. Integration points

1. **`validate_app` refactor (incremental, low-risk).**
   - Phase A: leave every existing `_check_*` untouched; add the `flows/lint/`
     engine alongside. `lint_app` runs the blessed validator (via the existing
     `config/validation.py` mapper -> `blessed_adapter` rules) + the new rules.
     `validate_app` gains an internal `lint_app` call and merges results; its
     `(errors, warnings)` signature is unchanged.
   - Phase B: port each `_check_*` into a first-class rule with a code (message
     text preserved), then delete the original. One PR per few checks; golden
     tests prove message parity.
2. **Deploy gates** (`deploy/gates.py`): add a `lint` gate that blocks on lint
   `error` (advisory on `warning` unless `strict`), reusing the existing
   advisory-vs-blocking machinery.
3. **Studio UI**: the `Diagnostic`/`Finding` models already feed the Studio
   report (`NativeReport.tsx`); extend it to render `code`, `category`, the
   `docs_url` link, and a "fix" button dispatching on `fix_id`.
4. **flows-docs**: add a `lint/` section (one page per rule + an overview) to the
   flows-docs SPA content.

## 13. Testing strategy

- **Golden fixtures**: a "clean" reference app -> 0 findings; a "dirty" app
  seeded with each defect -> asserts the exact set of codes fires. Seed the dirty
  app from the Elevance repros issue #596 offers as fixtures.
- **Per-rule tests**: for every rule, (a) a minimal config that triggers exactly
  that code, and (b) a near-miss that must NOT trigger it (the false-positive
  guard - mandatory, this is how we avoid the cxas noise problem).
- **Renderer snapshots**: human + JSON output snapshot tests; JSON validated
  against the published schema.
- **Determinism test**: same input -> byte-identical ordered output.
- **Perf test**: large synthetic app under budget.
- **Parity test** (Phase B): each ported `_check_*` produces the same finding as
  before.

## 14. Phased delivery

| Phase | Deliverable | Ships |
| --- | --- | --- |
| 0 | Engine: `models`, `registry`, `context`, `runner`, human+JSON `render`, `flows lint` CLI, blessed-validator adapter, `--list-rules`/`--explain`. Port 2 existing checks as reference rules. | The framework + one visible category. |
| 1 | The #596 rules: `FLR001` (open_slot dead-end, error), `FLW003` (dead tool, needs_review), `FLM001` (multi-outcome no directive, warning), and surface the already-covered `requires` gap earlier with a better message (`FLW010`). | Closes issue #596's core. |
| 2 | Best-practice tier: `FLC*` conversation design + `FLV*` voice/copy (mostly info / needs_review). | "Best-in-class agent" guidance. |
| 3 | Autofix (`--fix` + `fix.py`), config file (`[tool.flows.lint]`), inline suppression. | Fix loop + noise control. |
| 4 | Port remaining `_check_*` into rules (Phase B); deploy-gate + Studio integration; optional SARIF. | One source of truth + UI. |
| (sep) | Slot-level `then_directive` framework gap (the real fix `FLM001` points at) - FILED as #599. | Framework follow-up. |

## 15. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| False positives train users to ignore the linter (cxas hit ~99% noise on slot-filling apps). | `needs_review` severity for undecidable cases; mandatory near-miss test per rule; ship best-practice rules as `info`, not `error`; per-rule severity override + suppression. |
| Drift from the blessed validator / CES. | New rules never touch the blessed validator; the linter wraps its output. Whitelist-drift tests already exist (`test_whitelist_drift.py`). |
| Big-bang `validate_app` refactor breaks emit. | Incremental (Phase A adds alongside; Phase B ports with parity tests). |
| Heuristic rules (`FLM001`) misfire. | Default `warning`/`needs_review`, not `error`; documented near-misses; suppressible. |
| Autofix corrupts a config. | Autofix only for mechanically-safe deterministic edits; never on `needs_review`; `--fix` re-lints after applying and reports the diff. |

## 16. Decisions (confirmed)

1. **Command surface**: CONFIRMED - new `flows lint` is the primary surface;
   `flows validate` becomes a filtered (error+) view over the same engine.
2. **v1 scope**: CONFIRMED - Phase 0 + Phase 1 (#596 rules) **+ Phase 2**
   (the best-practice tier: `FLC*` conversation design + `FLV*` voice/copy). So v1
   ships the engine, the #596 four, and the best-practice guidance together.
3. **Default gating**: `error` blocks, `warning` advisory, `--strict` promotes
   (matches `deploy/gates.py`).
4. **Autofix**: defer to Phase 3 (after v1); v1 ships fix imperatives + `fix_id`
   hints, not the `--fix` applier.
5. **Framework-gap issue**: FILED - the slot-level `then_directive` gap that
   `FLM001`'s real fix depends on (split out of #596).
