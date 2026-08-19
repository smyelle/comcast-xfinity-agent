# The flows SDK version of this agent

**This directory is the same Comcast/Xfinity internet-repair agent as the one in the
repository root, re-authored in the [flows](https://depot.code.corp.goog/cloud-gecx/cxas-labs/tree/main/packages/flows)
SDK.** The root app (`../agents`, `../tools`, `../app.json` — CES app `d4ec582c`,
`xa-repair-voice-deterministic`, root agent `repair_orchestration_agent`) is the
original. This is a second implementation of its orchestration, nothing more.

Written by hand against the SDK — no migration tooling.

It lives here, rather than in a repo of its own, because it **grafts the source app
directly**: `build.py` reads `../toolsets`, `../tools` (with every `toolFakeConfig`)
and the three specialist sub-agents out of the parent directory and copies them into
its output verbatim. Only the orchestration layer is rewritten, so any behavioural
difference is attributable to it — and the substrate it builds against is always the
current source, rather than a snapshot that can drift.

## What it changes, and what it does not

The source is a hybrid: a 33KB generative instruction carrying a P1–P14 priority
ladder, alongside a hand-written `repair_dag` + vendored slot-filling engine covering
a subset of the same outcomes. Both are live at once. This version replaces that pair
with one authored DAG — an ordered list of condition-gated rungs — and keeps the
diagnostic plumbing untouched.

Measured against the original, driven live through all 58 evaluation scenarios:

* **51/58 first agent turns identical**
* **6** where the original is broken and this is not (4 die on the 10-step reasoning
  cap; 2 render "Hmm, I'm having trouble" where this speaks the golden line)
* **1** deliberate difference (`Account_Number_Invalid` — see Known gaps)
* **0** scenarios where the original is right and this is not

## Requirements

The SDK is in a **different repository** (`cloud-gecx/cxas-labs`, `packages/flows`).
Every script here finds it via `labs_paths.py`, which checks, in order: an already
importable `flows`, `$CXAS_LABS`, then the usual checkout locations.

```bash
git clone <host>/cloud-gecx/cxas-labs
export CXAS_LABS=/path/to/cxas-labs
```

`hold_ack` and `escalate.condition` are required. Both are on cxas-labs `main` as of
[#482](https://depot.code.corp.goog/cloud-gecx/cxas-labs/pull/482), so a current
checkout is enough. `build.py` checks for them by signature and tells you what is
missing rather than failing with a `TypeError` from inside `app.py` — worth having,
because an editable install of some other checkout will happily shadow the one you
meant to use.

## Files

**The agent is `app.py`** — the flow, the ladder, and the app, in that order. Start there.

| file | lines | what it is |
|---|---|---|
| **`app.py`** | 1261 | **the agent**: slots, the P1–P10 ladder as ordered rungs, the `flows.App` |
| `scripts.py` | 873 | the verbatim customer-facing text + the declarative conditions that select it |
| `clarify.py` | 378 | the Intent Clarification Gate's cue sets |
| `hooks.py` | 457 | the `before_agent` hook that runs the diagnostic sweep |
| `guardrails.py` | 167 | the seven platform guardrails, and why each is a filter or a judge |
| `source_tools.py` | 877 | which source tools are carried over, and the per-rung tools |
| `build.py` | 531 | emit, then graft the source's toolsets / fakes / specialist agents |
| `build_config.py` | 240 | every build switch, resolved once from the command line and frozen (`build.py --help`) |
| `labs_paths.py` | 153 | finds the flows SDK in the other repo, and checks it is new enough |
| `demo_stub.py` | 89 | the `--demo` diagnostics stub (imported by `build.py`, so it lives here, not in `tests/`) |
| `cujs.yaml` | 90 | the named scenarios every driver seeds from — one account + mock string per journey (at root so both `build.py --cuj` and the `tests/` drivers discover it) |

All test/check/drive code lives in **`tests/`** (run from this directory, e.g.
`python tests/ladder_check.py`):

| `tests/` file | what it is |
|---|---|
| `tests/ladder_check.py` | offline oracle: 28 scenarios asserting which rung fires (no model) |
| `tests/clarify_check.py` | offline oracle: 31 utterances classified with no model |
| `tests/drive_app.py` / `tests/cuj_drive.py` | live fidelity vs the original, and 5 multi-turn CUJs |
| `tests/cuj_diff.py` | drives BOTH apps through 18 multi-turn journeys, diffs every turn |
| `tests/try_agent.py` | chat with a deployment |
| `tests/build_oracle.py` | builds the eval oracle from `../evaluations` for `tests/drive_app.py --kind golden` |
| `tests/score_corpus.py` | scores a `drive_app` corpus run vs the goldens; flags router misroutes |
| `tests/guard_check.py` | which guardrails fired, and a false-positive suite — the only thing here that can tell a working guardrail from an absent one |


## The whole agent, in outline

```python
repair = flows.Flow("repair", root_agent="repair_orchestration_agent",
                    bootstrap={"reset_on_complete": True},
                    escalate={...}, no_input={...})

repair.add(flows.user_slot("accountNumber", ask=..., setter="set_account_number", ...))
for status in (...):                       # what the sweep resolves
    repair.add(flows.event_slot(status))
repair.add(flows.passive_slot("complaint_scope", kind="intent", option_cues=...),
           flows.passive_slot("app_name", option_cues=..., cue_priority="first"),
           flows.intent_slot("clarify_reply", ..., on_exhaust_fill="UNSURE"))
repair.add(flows.user_slot("confirm_reboot", ask=..., condition=...))

for rung in [                              # ORDER IS THE CONTRACT
    rung("HandleBillingBlock",   "verdict_account_block",  RESTRICTED_ACCOUNT, ...),
    rung("HandleAreaOutage",     "verdict_area_outage",    AREA_OUTAGE,        ...),
    ...                                    # P3..P9
    rung("HandleAllClear",       "verdict_all_clear",      ALL_CLEAR,          ...),
]:
    repair.task(rung)

app = flows.App(root_flow=repair, model=..., hooks=AgentHooks(before_agent=...),
                tool_bodies=source_tools.tool_bodies(), classifiers={...})
```

## Build, check, deploy

Run everything from this directory. The source app is the parent, so nothing needs
pointing at it.

```bash
export CXAS_LABS=/path/to/cxas-labs           # where the flows SDK lives

python build.py --out ./built                 # validate + emit + graft the substrate (flat: steering OFF)
python build.py --out ./built --steering      # ... with the multi-level router as the root flow
python build.py --out ./built --skip-greeting # ... with the opening greeting baked OFF (transfer-target build)
python tests/ladder_check.py                        # 28/28 offline, no model
python tests/clarify_check.py                       # 31/31 offline, no model
python tests/cuj_drive.py                           # 5/5 multi-turn CUJs, live
python tests/cuj_diff.py                            # this vs the original, turn by turn

cxas push --app-dir ./built \
    --to projects/ces-deployment-dev/locations/us/apps/<APP_ID> --overwrite
```

### Named scenarios (`cujs.yaml`)

Every driver seeds from `cujs.yaml`, so a journey is a name rather than an account
number plus a query string you have to remember. `flows cujs` lists them.

```bash
flows cujs                                    # what's available
flows cujs gateway_reboot --json              # the variables it resolves to
flows chat --cuj gateway_reboot --app <APP_ID>            # drive it (or: python tests/try_agent.py reboot)
flows chat --cuj gateway_reboot --app <APP_ID> --say "my internet is not working" --say "yes please"
```

`flows chat` needs the Labs `app` package, and this directory's own `app.py` shadows it
whenever the current directory lands on `sys.path` — as `python -m flows.cli` makes it.
Use the installed `flows` console script, or `PYTHONSAFEPATH=1`. `try_agent.py` orders
the paths itself and is unaffected.

To make a DEPLOYED app open on a journey with nothing to seed — which is what the CES
console needs, since a console session never fires the tool fakes — bake the CUJ into
the variable defaults:

```bash
python build.py --out ./built --demo --cuj gateway_reboot
```

That only updates the draft. The console will follow it; a phone/GTP number keeps
serving its pinned version until you promote.

`COMCAST_SOURCE` overrides the grafted source if you ever need to build against a
different checkout of the app.

## Trying it interactively

```bash
python build.py --out ./built_demo --demo
```

`--demo` swaps the real fan-out for a stub that resolves every status from the
`mock_config_string` session variable. It exists because the per-tool `toolFakeConfig`
mocks only engage when the CALLER sets a session-level fakes flag — a console session does
not, so on the shipped build every interactive conversation reaches for unreachable
Comcast backends and lands on "I couldn't get all the info I need". With `--demo` (plus
defaults baked into `variableDeclarations`) every scenario is reachable by editing one
variable. The shipped build always calls the real tool.

Deployed for verification as
`projects/ces-deployment-dev/locations/us/apps/5fc33f37-19c2-4dee-a0c0-7e88c911f627`,
which is what the live harnesses here default to.

Every other mode is a flag too, and `python build.py --help` is the list. The ones worth
knowing: `--specialists local` runs the specialist pair in-sandbox instead of through the
Cloud Run proxy (Cloud Run is the default and the deployed shape), `--legs live` opts a
demo build back out of the inlined leg fixtures, and `--sweep-delay` / `--leg-delay` /
`--specialist-delay` make the waiting audible. Whatever was chosen is stamped into the
emitted dir as `build_manifest.json`, so a built app can be asked how it was built.

### The engine ships as bytecode

Every build packs the slot-filling engine tool into precompiled bytecode. CES re-parses a
python tool's module on **every** invocation, and the engine is 555KB / 49,860 AST nodes;
packed it is 161 nodes, which measured 165 to 74ms and 169 to 70ms per engine call on this
agent, about eleven calls a session, all of it ahead of the first spoken word. The packed
module carries its own source compressed, so the framework drift gate still hashes the
blessed bytes and the deployed tool falls back to source if it ever cannot load the
bytecode.

`--no-pack-engine` emits the readable source instead. Reach for it when you want to read or
patch the deployed engine body; nothing else about the app changes.

Packing needs a python 3.12 interpreter, because that is what CES runs and `marshal` has no
version check of its own: mismatched bytecode loads fine and then misbehaves at call time.
This repo's venv is 3.13, so the build runs **only the packing step** under a 3.12 found
through `uv` (`flows.engine.packing` is stdlib-only, so it needs no venv of its own; the
hop costs about 0.2s). On a host where no 3.12 can be had, the build emits source, prints a
banner that cannot be missed, and records `engine_packing: "skipped"` in the manifest,
which is distinct from `packed` and from `off`. An app was once deployed named for being
packed while serving source, and nothing in the artifact contradicted the name; those three
states are what make that impossible, and `tests/config_check.py` gate E mutates a build
six ways to prove they can still catch it.

### Tell the specialist proxy which app it is working for

```bash
python build.py --out ./built_demo --demo \
  --ces-app projects/ces-deployment-dev/locations/us/apps/<APP_ID>
```

The proxy opens the two specialist agents as a CES session, and a session belongs to an
APP. It serves whichever app asks; a request that names none falls back to the app the
service itself was deployed against, so every other app's specialists answer out of
somebody else's deployment — or fail to open at all, which the derivation then reads as a
perfectly healthy line. Neither is visible in the result.

`--ces-app` bakes the id as the `ces_app` variable's default, which is what the generated
caller sends. A tool body cannot ask CES which app is running it, and the id does not
exist until the app has been pushed — so a brand-new app is pushed once and then rebuilt
with the id it was given. Leaving the flag off keeps the old fallback exactly, and a
session that seeds `ces_app` overrides the baked default either way.

### Driving a demo build cold: the account number picks the journey

```bash
python build.py --out ./built_ux --demo --specialists local
```

A `--demo` build answers every carried tool from its recorded fixture, baked into the
tool's own body — so it needs no session fakes and no seeded variables, which is exactly
what the CES console cannot supply. The account number is then the only thing a console
caller can vary, so it is what chooses the journey: the bindings come from `cujs.yaml`, so
this table is generated rather than maintained (`source_tools._demo_account_scenarios`).

Say one of these and nothing else. Everything on the left is drivable from a cold click on
the app link.

| Account | Journey | What the caller hears |
| --- | --- | --- |
| `8069100230359946` | `all_clear` | The full sweep, the scoping question during the wait, an all-clear verdict and the Wi-Fi walkthrough. **The one with a wait in it.** |
| `8069100230361003` | `gateway_reboot` | Sweep, then a gateway reboot offered and sent |
| `8069100230359928` | `convoy_predictive_reboot` | Sweep, then a reboot off a predictive-offline signal |
| `8069100230361005` | `gateway_swap` | Sweep, then "the gateway needs replacing" |
| `8069100230359944` | `network_impaired` | Sweep, then a technician dispatch |
| `8069100230361006` | `convoy_technician` | Sweep, then the appointment specialist |
| `8069100020078787` | `account_suspended` | Billing hand-off **before any check runs** — this account can never sweep, by design |
| `8344200010126021` | `area_outage` | "I'm not seeing an Xfinity Gateway on your account" — it resolves with no modem MAC, and missing hardware outranks the outage advisory it is named for |
| anything else | — | Treated as an ordinary all-clear customer, on a default MAC |

A seeded `mock_config_string` still wins over all of it, so `--cuj`, `--var` and the eval
harness behave exactly as before.

**The default build is not this**, deliberately. Its gate calls the real context hub, which
is the point of having a build that talks to real backends — and while that hub is failing
in dev, every cold conversation on it ends at the account step with "I see an issue with
your account status". That sentence means two different things depending on which build
said it: on a demo build it is the suspended account's correct answer; on the default build
it is currently the hub being down.

## The three decisions that matter

**The sweep runs in a `before_agent` hook, not as a task.** The CES runtime only
dispatches an engine-fired tool as a continuation of a model tool call, so a task that
must run with nothing to collect first never fires. Calling `run_comcast_diagnostics`
directly from the hook sidesteps dispatch and lands the verdict on the same turn as the
caller's opening utterance. The source resolves its own chain the same way.

**The ladder is ordered condition-gated tasks, and they are NOT terminal.** Tasks are
first-match-wins in declaration order, which is exactly the source's "evaluate in strict
hierarchical order, halt at the first match". Announces were the wrong vehicle — they
cascade. `terminal` is omitted deliberately: the engine defers a terminal fire on any
turn carrying user text, waiting for a post-setter re-invoke that never comes in a flow
with nothing to collect, so the verdict is simply never spoken. Each rung instead
latches `verdict_delivered` (the source's own gate), which also avoids the source's
absorbing-terminal defect where every later turn re-speaks the same sentence forever.

**The diagnostic substrate is carried over byte-for-byte.** `build.py` copies the
source's toolsets, tool JSON (including every `toolFakeConfig`, which is what the
`mock_config_string` scenarios drive) and the three specialist sub-agents. Only the
orchestration is rewritten, so any behavioural difference is attributable to it.

## Steering — a real model-classified intent router

The source agent does not run in isolation: a GECX **front-end steering agent** detects
the caller's intent and routes to the right place. This app implements that as a
first-class **steering router** — NOT a keyword/cue match and NOT a regex over an injected
header. A single-agent `router_flow` classifies the caller's intent with the **model**
(the engine's Pass-A `classify_turn_intent` / `set_active_flow`), guided by the
natural-language flow descriptions in `source_tools.ROUTER_INSTRUCTION`, and routes into a
child flow. "Handle if we can, otherwise set the intent and hang up."

> **Opt-in — OFF by default (`--steering`).** The router is a build flag (`build_config`,
> like every other switch). Without it the build is the flat single-flow `repair` agent
> (the pre-router shape): `repair` is the root flow and owns the opening turn, and
> reboot-on-request and a human hand-off are handled inside it (the `RebootOnRequest` rung
> and the `escalate` rail). Pass `--steering` to make the multi-level router the root flow,
> which then owns routing and the opening greeting and routes the deferred golden
> categories. `build.py --steering`; the mode is recorded in `build_manifest.json`.

> **The opening greeting is gated, two ways.** The flat `repair` agent opens on its
> account ask, and the greeting ("Welcome to Xfinity") rides a `{welcome_lead}` that
> `before_agent` fills — spoken on a direct call, dropped once an upstream agent handing the
> call over (transferToNga / A2A) seeds the **`skip_greeting`** variable, since the caller
> was welcomed one agent ago. The ask is `verbatim` so the model cannot re-add a greeting of
> its own. For a build deployed purely as a transfer target, the **`--skip-greeting`** build
> flag bakes the greeting off outright (the ask is emitted greeting-free), so it never greets
> and does not depend on the upstream seeding the variable. `greeting_check.py` covers both.

```
steering = router_flow("steering", ["reboot", "diagnostics",
                                    "video", "billing", "wifi_settings",
                                    "speed_test", "other"],
                    route_cues=ROUTE_CUES)         # keyword backstop; no default_flow
```

* **`diagnostics`** — any internet connectivity problem. This is the former single flow:
  the before_agent sweep + the P0–P10 ladder + `escalate`/`no_input`. A request for a
  person routes here too (its `escalate` block handles it).
* **`reboot`** — the caller explicitly asks to restart their gateway.
* **defer flows** (`video`, `billing`, `wifi_settings`, `speed_test`, `other`) — intents
  this agent does not handle. Each records the detected intent (`verdict_defer` reads it
  off the `active_flow` gate) and hands the leg back for onward routing.

**Model classification is primary; `route_cues` are a deterministic backstop under it —
not the reverse.** The idiomatic router shape (see `silent_home_base` / `bella_notte` /
`host_route_cues`): a matched cue fast-paths the route (engine `route_backstop`) BEFORE
the model runs; anything a cue does NOT match falls to the model, which classifies from
the by-MEANING descriptions in `ROUTER_INSTRUCTION` (`set_active_flow`). So neither layer
is relied on alone — cues catch the obvious phrasings deterministically, the model
generalizes to the rest (verified: `"power cycle the modem"` fast-paths via cue,
`"mind giving it a restart"` routes via the model, both to `reboot`).

The cues obey two rules so they don't overfit: **`diagnostics` gets no cues** (it is the
model's "when unsure" home, and a connectivity cue would risk preempting the model on the
exact inputs the corpus uses), and **every cue is a generic author synonym, never a string
an eval sends** (human uses `"live agent"`/`"representative"`, never the corpus's
`"real person"`). Verified: no corpus input matches a cue, so the backstop leaves 43/43
unchanged and only adds coverage for production phrasings the corpus never exercises.

**The routing descriptions are hand-authored (`ROUTER_INSTRUCTION`) — deliberately, for
now.** The SDK *can* auto-generate them: the `intent_first` Pass-A classifier
(`_build_classifier_suffix`) expands `flow_types` + `route_cues` into the routing SI, so
the cues would be the single source of truth. We are NOT using it yet because that
generated SI has restaurant/order-domain examples baked into the blessed engine
(*"the guest's latest message," "its own party/date," "a dietary note"*) — off-domain for
a repair agent. **Follow-up:** generalize `_build_classifier_suffix` in `cxas-labs`
`packages/flows` to be domain-neutral, then switch this router to `intent_first` and delete
the hand-written `<routing>` block (see "Framework gaps worth fixing").

**Two non-obvious things learned building it:**
* Child flows must fire condition-gated **rungs** (`flows.task` with `then_say`), not
  `announce`s. A rung fires its executor and renders on the routing turn; an announce
  renders empty. The `reboot` and `defer` flows are rungs for this reason.
* The before_agent **sweep** runs on the cold routing turn (before the model routes, when
  `active_flow` is still empty), so `device_id`/statuses are seeded before `diagnostics`
  or `reboot` needs them.

A human request that arrives ON the routing turn goes to the **`human`** route (a rung
that transfers and speaks the escalate line) — the diagnostics `escalate` control block
only fires once diagnostics owns the turn, which is too late on turn 1.

**Validation.** Offline (no model/deploy): `tests/ladder_check.py` (28/28) and `tests/clarify_check.py`
(31/31), both retargeted to the `diagnostics` child DAG. Live: build → `cxas push` to a
scratch app → drive the eval corpus (`tests/drive_app.py --oracle <oracle> --kind golden`; build
the oracle from `../evaluations` with `tests/build_oracle.py`). Live result on the 43-golden
corpus: **43/43 matched, zero misroutes, zero errors** — the model routes internet-trouble
→ `diagnostics`, restart-my-gateway → `reboot`, person → `human`, TV → `video`, bill →
`billing` correctly, and never sends a scenario to the wrong flow.

**Getting to 43/43 was test-fixture maintenance, NOT agent tuning.** The router itself
introduced zero eval failures: driven against the *original* source app, the same 43
goldens gave **0 regressions** (every scenario the original passes, the router passes) and
**+1 improvement** (`5_5` escalation). The corpus started at 26/43 only because of
pre-existing stale fixtures — two honest fixes, both leaving the agent untouched (no
keyword matching, no script tuning):

1. **Fixture completion.** The `Stubbed_*` goldens seed *only an account number*, but the
   mock tools are 100% `mock_config_string`-driven and default to all-clear — so those
   evals structurally tested all-clear regardless of their name. Added the missing
   `mock_config_string` scenario seed (e.g. `gateway_status=swap`, `context_status=suspended`)
   every other eval already carries, so each eval exercises its named fault.
2. **Stale-wording refresh.** Where the agent produced the correct *outcome* but a golden
   held *older wording*, the golden's expected text was refreshed to the current
   source-verbatim output.

Integrity check on the refresh: the **original source app also scores 38/43** on the
refreshed goldens (up from 25), so the refreshed text reflects genuine current source
behavior, not the flows app specifically — the original's remaining 5 are its known
10-step-cap / "having trouble" breakages that this version fixes (hence 43 vs 38). One
note: `Stubbed_WiFi_Interference` now asserts all-clear because wifi diagnostics are
deliberately disabled in the substrate (the gateway fake pins `wifi_status=healthy`) — the
golden documents current behavior; re-enabling wifi diagnostics is a separate feature.

## Multi-turn CUJs (`tests/cuj_drive.py`)

The eval corpus is one turn per scenario, so the interesting paths — the ones needing a
second turn — were unverified live for a long time. All five now pass against the
deployed app:

* reboot offered -> **accepted** -> "sending a signal to reboot"
* reboot offered -> **declined** -> hand-off to a gateway specialist
* a verdict is delivered **once**; the follow-up turn gets follow-up handling, not the
  verdict again (the `verdict_delivered` latch, live)
* clarification -> "only that app" -> advice, and no diagnostics run
* clarification -> "everything is down" -> the bridge line **and** the verdict in ONE
  turn, so the caller is not told "let me check your service now" and left waiting

## The eval corpus is entirely single-turn — so most of the call was unmeasured

Every one of the 58 scenarios sends exactly **one** utterance. The 51/58 number below
therefore only ever compared the FIRST agent turn. Everything a repair call actually
does after that was covered by the five hand-written CUJs above and nothing else.

`tests/cuj_diff.py` closes that hole: it drives BOTH apps through the same 18 multi-turn
journeys and diffs turn by turn, so a difference is pinned to an exchange instead of a
score. `--repeat N` separates a real divergence from model noise (the original is
nondeterministic and regularly hits its 10-step reasoning cap).

It immediately found a defect nothing else could see.

**The caller was transferred to a human, silently.** After the technician verdict, the
caller asks "when will the technician come?". The ladder is finished, so the engine has
nothing to say and the model is free — and it answered by calling
`transfer_to_appointment_specialist`, which hands off to a human queue and returns
`{"success": True}` with no words. The caller heard **nothing**, and every later turn was
dead air. Reproduced 3/3.

The root cause was tool surface, not prompting: this app offered the model **55** tools
where the source offers 26 — the extra 29 being diagnostic fan-out probes and the rungs'
own delegate targets. The source keeps the model off them with 33KB of prose. Declaring
a tool on the agent is what OFFERS it to the model, and a tool called from inside another
tool's body resolves against the app instead — which the source itself proves, since its
`run_comcast_diagnostics` fans out to probes its root agent never declares. So
`source_tools.ENGINE_ONLY_TOOLS` takes them back off: the model now sees 41 declared, of
which the engine hides the 14 `verdict_*` executors at runtime, leaving ~27 against the
source's 26. Verified live that the rungs still transfer (a failed delegate would surface
as `rung_failed`, and the now-undeclared fan-out still resolves the diagnostics).

Every transfer in this conversion is performed by a ladder rung, so the model reaching
for one directly is never correct — this makes it structurally impossible rather than
discouraged.

## Fidelity

Both apps driven through all 58 eval scenarios (`tests/drive_app.py`), first agent turn
compared, whitespace/punctuation-normalized:

* **51/58 identical**
* **4** where the ORIGINAL dies on the 10-step reasoning cap and this version answers
  correctly (`All_Diagnostics_Healthy`, `Diagnostic_Tool_Failure`,
  `Gateway_Reboot_Recommended`, `No_Telemetry_Data`)
* **2** where the ORIGINAL renders "Hmm, I'm having trouble" and this version speaks the
  GOLDEN line and hands off (`5_5_Customer_Requests_Agent`,
  `Intent_Clarification_Agent_Request`)
* **0** failures here
* **1** genuine remaining difference — below

There is no scenario where the original is right and this version is not.

So it matches the original everywhere it works, and is correct on six scenarios where the
original is not.

## Guardrails

Seven, authored in `guardrails.py` and emitted by the SDK. Before this the converted app
ran with **none**: `build.py` copied the source's four resource directories but the
`guardrails` array that names them was never carried, so they shipped bound to nothing —
and a bare `cxas push --overwrite` of an app.json with no `guardrails` key would strip
whatever the live target still had.

Two are the source's platform baseline re-authored, names kept byte-identical so the
deployed app keeps the same two rather than gaining two renamed ones. Five are new:
competitor names, a competitor judge, agent profanity, internal markup, and an unprompted
credit offer.

**What decides whether a guardrail prevents anything** (ces-probes `102`/`103`/`108`,
measured live in audio on both models):

| | `scope="user"` | `scope="agent"` |
|---|---|---|
| `blocklist` — deterministic | prevents | **prevents on both models** |
| `policy` — judged by a model | prevents | composite: prevents · flash-live: detects only |

A judged rule cannot run until there is a response to judge, which on a streaming model is
after the words are out. So four of the five new rules are blocklists, which cost nothing
and prevent. The fifth has to be judged — a match on "credit" cannot tell a prohibited
offer from a legitimate answer after the caller raised money — which is why **the app now
runs `gemini-composite-v1`**.

Two things that look like details and are not:

* **The competitor list is three lists.** Several rival brands are ordinary words in
  network repair — "radio spectrum", "optimum signal strength", "boost your signal", "your
  satellite dish" — and Cox is a customer surname. Those go to the judge. Streaming brands
  are not blocked at all: the ladder has an `app_specific` outcome and "Netflix keeps
  buffering" is the normal case.
* **Profanity is scoped to the agent, never the caller.** A caller whose internet is broken
  may well swear, and blocking them would be a worse product than the problem it solves.

Most of this agent's recorded defects are **not** guardrail-shaped and are deliberately
absent — silent turns, a transfer with no words, a wrong verdict, verbatim repetition. The
reasoning is in `guardrails.py` so it does not have to be re-derived.

```bash
python guard_check.py --app <APP_NAME>     # false-positive suite + provocations
```

Every case seeds a CUJ through `flows.open_session`, which is load-bearing: without
`use_tool_fakes` the fake configs are inert, the agent never gets past asking for the
account, and the guardrails are only ever judged against that one question. The
verdict prose — where "a service charge may apply" and the diagnostic wording live —
is where the false-positive risk actually is, and the first version of this suite
never reached it.

`guard_check.py` exists because nothing else here can tell a working guardrail from an
absent one — `drive_app.py` and `cuj_diff.py` compare text, and an inert guardrail is
invisible to both. It caught a real one on its first run: the competitor judge flagged the
assistant for naming **Comcast**, its own company, because the policy never said who "we"
are.

Parity was measured rather than assumed. The pre-change build (flash-live, no guardrails)
was deployed to a scratch app and driven through the same goldens: **43/43 first turns are
byte-identical** to the guarded composite build.

## Known gaps

Closed since: the off-catalogue app name. `option_cues` is a closed set, but
`cue_priority="first"` lets a GENERIC catch-all be declared last — a real product name
still wins, and "just the service" now resolves to the display string `the service`,
which renders straight into the advice. No model involvement.

1. **`Account_Number_Invalid`** — the only first-turn difference left, and not one worth
   closing. The caller trails off ("...and my account number is."); the source calls
   `set_account_number` with an empty value, its setter rejects that as `invalid_format`,
   and it answers "Please provide a valid 9 to 16 digit account number or a 10 digit
   phone number." This version asks its opening question instead. Matching would mean
   scolding someone about digit counts when they never gave a number at all, so the
   divergence stands on purpose. Structural parity is in place either way: the slot points
   at the source's own `set_account_number` (a generated setter reports `missing`, which
   that error map has no entry for), so a genuinely malformed number does get that line.
Previously listed here as "unmatchable": the source speaks TWO different swap sentences.
It is not unmatchable — the wording tracks HOW the swap was discovered, and that is
state you can condition on. Convoy predicted it (`before_agent` seeds `convoy_status`
directly, the DAG rung matches, the DAG's wording ships, with a comma) versus the gateway
specialist found it (`gateway_status` never reaches the slot machine, no DAG rung matches,
the model falls through to the prose ladder, which words it differently). Two rungs, two
scripts, both scenarios now match.

2. **CLOSED — a spoken hold request is now acknowledged.** "My internet is not working.
Hold on, I need a moment to find my account number." used to get the account-number
question anyway, which is the one reply that request rules out. The engine already
DETECTED it (the utterance sets hold state) but that only fed the silence ladder and the
steer-back exemption, neither of which changes what is said on a turn carrying text. Now
handled by the framework's `no_input.hold_ack`, wired to the source's own line — this
scenario is byte-identical to the original.

3. **CLOSED — a request for a human during an outage is now refused.** The source is
explicit ("During an outage, we are unable to connect you with a live agent...") and
ships that sentence in the outage advisory, then transfers anyway. This version declines
via the framework's `escalate.condition` and speaks the refusal. It is the one place this
agent deliberately does NOT match the original's observed behaviour, because the original
contradicts its own stated policy.

Both needed framework primitives that did not exist; they were added rather than hacked
around, because a half fix (refusing while still saying "connecting you now", or
acknowledging the hold and then asking anyway) reads worse than the honest gap.


**The 24 golden misses are stale goldens, not conversion defects (verified).** Scoring
against the eval corpus's expected text, this version matches 19 of 43 and the source
matches 18 — and there is no scenario the source matches that this one misses. The
shortfall is concentrated in the `Stubbed_*` scenarios, which seed only an account number
and expect a specific fault. Tracing the sweep shows the carried-over tool fakes now
resolve `account_status=clear, outage_status=none, network_status=healthy,
gateway_status=healthy, convoy_status=clear` for exactly those accounts, so all-clear is
the correct reading of the substrate and the source says the same thing. The goldens were
recorded against fake configs that no longer map those accounts to those faults.

**Dead config found and removed:** the flow's `escalate` block carried
`"tool": "verdict_human_request"`, which the engine never reads — a control block
customizes only `say / outcome / transfer_to / exit_status / requires_readback`, and the
setter is uniformly `transfer_to_human`. It read as though the source's transfer payload
was being sent when nothing of the kind happened. Checked live what the source actually
does when asked for a person: it says **nothing at all** and calls the platform's
`transfer_to_agent`, so an escalated end is the faithful equivalent — and this version
speaks the golden line the source itself fails to produce.

**Transcript artifact, NOT a live defect (chased and closed):** on a turn that ends the
session, the spoken line appears TWICE in a captured transcript. The caller hears it once.
The engine emits it once, `_preempt_parts` builds one text part, and the CES trace labels
the second copy `Agent Text (Diag)`. The vendored response parser deliberately drops that
diagnostic mirror — but the guard (`output_has_top_text`) is scoped per OUTPUT, and a
session-ending turn arrives as two outputs: one carrying the top-level text, one carrying
the `end_session` whose diagnostic mirror has no top-level text of its own. So the mirror
survives and `consolidated_agent_text` joins both. Affects `vendor/cxas-scrapi`'s parser
only; deliberately not patched from here.

## Open: on backend failure, composite loops where flash-live hands off

Narrower than it first looked, and the first write-up of it here was wrong — recorded
because the mistake is an easy one to repeat.

**The agent is fine on the supported path.** `tests/cuj_drive.py` is 5/5, and an
unidentified caller who gives an account mid-conversation gets a correct verdict:

```
> my internet is not working
< To get started, could you please tell me your Xfinity account number...
> my account number is 8069100230359944
< We detected that your gateway is currently offline, though there are no neighborhood outages...
```

What produces the loop is driving with **no `mock_config_string`** — which `cujs.yaml`
already warns about at the top of the file: *"Without BOTH, the agent reaches for real
Comcast systems and dead-ends."* With no fake and no reachable backend,
`perform_connect_network_analysis` never returns a terminal answer, and the specialist
re-calls it until the platform's ten-reasoning-loop cap:

```
[network_specialist_agent]: BeforeAgent -> connect_network_before -> Guardrail ->
  BeforeModel -> LLM -> Async Tool -> AfterTool ->  ...x10
```

So it is a test artifact of an unsupported drive — **except for one part that is not.**
Backends do fail in production, and the two models handle that failure very differently:

| model | when diagnostics cannot answer |
|---|---|
| flash-live | the diagnostics-error rung fires: *"I'm sorry, I'm having trouble running diagnostics right now. Let me connect you to a live agent."* |
| composite | ten reasoning loops, then a 400. **The caller hears nothing.** |

Reproduced 3/3 on each, and on upstream `initial-push` with no guardrails, so it is neither
the guardrails nor this branch.

The engine cannot catch it: the cap is enforced at the platform turn level, so the
`before_agent` hook's own except-and-degrade never runs. Fixing it properly means bounding
the specialist's retry — and the specialists are grafted source agents this conversion
deliberately holds fixed, so it is a source change, not an orchestration one.

Until then it is a real robustness gap on composite and a graceful degrade on flash-live,
which is worth weighing against composite's audio advantages.

### The guardrails are not what loops

The trace carries a `Guardrail` step, so the obvious suspicion is that seven guardrails eat
the ten-step budget. They do not. Counting steps on the same journey, same app, same turn:

| build | `Guardrail` steps | LLM | Async Tool | total steps |
|---|---|---|---|---|
| upstream `initial-push`, **0 guardrails** | 0 | 10 | 10 | 43 |
| this branch, **7 guardrails** | **1** | 10 | 10 | 43 |

The loop budget is **identical**. Seven attached guardrails add exactly one step to the
whole turn — evaluated once per agent invocation, not once per reasoning step and not once
per rule — and the ten loops are consumed entirely by the specialist's own
`LLM -> Async Tool -> AfterTool` cycle, re-calling `perform_connect_network_analysis` and
never getting a terminal answer.

The general fact worth keeping: a guardrail costs a step, not a loop.

## Open: the reboot timeline gate is bypassed

Found while tracing the tool surface for guardrails, **not fixed here**, because the fix
needs a conversational outcome this change should not invent.

`source_tools.py` calls `reboot` with `{"device_id": device_id}`, which leaves the tool's
`restart` at its default of `True`. The timeline check in
`../tools/reboot/python_function/python_code.py` sits inside `if not restart:` — so the
converted app **skips the reboot rate-limit gate on every reboot**. A caller can trigger
repeated gateway restarts, which is the defect
`comcast_deliverable/agent/docs/journeys/comcast_reboot_journey.md` records.

The one-token fix is `{"device_id": device_id, "restart": False}`, and the happy path is
unchanged: with `restart=False` the tool runs the check and auto-proceeds to a real reboot
when no recent restart is found. But it makes the blocked path reachable for the first
time, and `ExecuteReboot`'s `then_say` is unconditionally `SAY_REBOOT_STARTED` — so the
caller would be told a reboot started when none did. **Restoring the gate without a
companion outcome trades a safety bug for a false claim**, which is the trade this repo's
whole guardrail story is about not making.

The complete fix is a second condition-gated rung on a `timeline_blocked` result, or
mapping that result onto the existing hand-off. Either is a ladder change and belongs with
the eval work, not with guardrails.

## Defects found in the source while doing this

Documented in `/tmp/cw/SOURCE_SPEC.md` (16 catalogued). The load-bearing ones:

* The DAG task `ComcastDiagnostics` never writes any slots — the synthesized response is
  missing 3 of its 9 declared output keys and output mapping is all-or-nothing — so 7 of
  its 10 terminals are unreachable and those outcomes are decided by the prose ladder
  instead. Two decision layers, same outcome, different sentences.
* DAG terminals are absorbing: `sm.status="complete"` makes every later turn re-speak the
  same sentence verbatim.
* `confirm_reboot` is tested with `is True` / `is False`, but the setter records the
  SPOKEN answer, so a plain "no" is the string `"no"` and both branches die silently.
* `pending_activation` (underscore) matches no handler; the mapping produces
  `"pending activation"` (space).

---

# Addendum: how much of the "generative" behaviour is actually declarative

The first pass wrote off the Intent Clarification Gate and the human-agent request as
"genuinely generative". That was wrong. Both are expressible, and the evidence is in
`CLARIFICATION_DESIGN.md` and `INTENT_AND_RESUME.md`.

## Intent Clarification Gate — now slots (`clarify.py`)

Three slots, no prose: `complaint_scope` (passive intent slot), `app_name` (cue slot
keyed on display strings), `clarify_reply` (the question + three-way branch).
`tests/clarify_check.py` proves **31/31** utterances classify with **no model involved** —
"My Zoom keeps dropping" separates from "The internet keeps dropping", and
"I only tried Netflix" resolves to UNSURE rather than colliding with the ONLY_APP cue.

Two properties of cue matching drive the whole design:

* cues are case-insensitive **unanchored regexes** over the raw text, no normalization;
* **two matching values ⇒ the slot stays empty.** No priority, no longest-match. So cue
  sets must be mutually exclusive — the broad set is anchored on broad NOUNS, never on
  verbs it shares with the app-specific set.

Where the model is still required, and why that is the right split:

* an app outside the catalogue (open-ended by nature) — degrades safely to "just run
  diagnostics", and one instruction line lets the model recover the gate by calling
  `set_complaint_scope` / `set_app_name`;
* an ambiguous reply — backstopped by `App.classifiers`, which unlike `option_cues` is
  ordered first-hit-wins **and** has a default, so it always yields a value.

The rule worth remembering: **`option_cues` is engine-side, model-free, and drops on
ambiguity; `App.classifiers` is model-invoked, ordered and total. Use both.**

## Human-agent request — a control block, not a rung

The engine already detects it deterministically (a phrase regex, before the DAG sees the
turn). All that was missing was the `escalate` ControlBlock saying what should HAPPEN.
Now configured with the source's line and a `transfer_potato_to_agent_v2` hand-off.

## Framework gaps worth fixing

Full syntax for the fixes — wire config, SDK, pydantic model, validator whitelist and
engine semantics — is in `PROPOSED_PRIMITIVES.md`. Summary:

0. **The `intent_first` classifier SI is domain-specific (restaurant/order).**
   `_build_classifier_suffix` (`packages/flows/.../slot_filling_engine`) auto-generates the
   routing/switch SI from `flow_types` + `route_cues` — the right way to avoid hand-writing
   a `<routing>` block — but its examples are hard-coded to a restaurant order flow
   (*"the guest's latest message," "its own party/date," "an extra dish," "a dietary note …
   seating preference"*). It reads as off-domain for any non-restaurant agent. Fix:
   parameterize the noun ("guest"→caller/customer, configurable) and move the illustrative
   examples out of the blessed string (e.g. derive them from the flows' own descriptions, or
   accept a per-app `classifier_examples`), so an agent can adopt `intent_first` routing
   without inheriting restaurant wording. Once done, this router switches to `intent_first`
   and drops its hand-written `<routing>` block (blessed-engine edit → re-bless + byte-sync
   the studio_framework mirrors).

1. **`escalate` disposition is unreachable on the default path.** The keyword backstop
   fires `transfer_to_human`, but `slot_intake` has no mapping from that tool back to
   the `escalate` control slot — it only handles escalate via `classify_turn_intent`,
   i.e. `intent_first` mode. So on a non-`intent_first` flow the engine asks for a
   hand-off and nothing consumes it; the DAG then speaks over the top. Reproduced
   offline and live.
2. **`validation_rules` on a plain `user_slot` is inert.** Only `setter_group` lowers an
   enum rule into a real check; a single-field setter stores any non-empty string. A
   slot that looks validated is not.
3. **No declarative "N unresolvable answers → take the default and continue".**
   `validation.on_exhaust` never fires for a slot the caller answers unrecognisably
   (the retry counter only counts setter-reported errors), and `steer_back` fires but
   tears the flow down. Proposed primitive: per-slot
   `on_no_match: {"after": N, "value": <enum value>}` — reuses the existing counter site
   and fill path, and is exactly the "give up gracefully into the safe branch" semantics
   conversational gates need.
4. **`option_cues` has no priority form.** An ordered list would let ambiguity resolve
   instead of dropping, and would remove most of the negative-lookahead hand-tuning.
5. **`intent_change.switch` is diagram-only** — the runtime never reads it, so
   "on intent change, go to flow X" cannot be declared.
6. **A slot-level `default`** would remove the `requires=["app_name"]` workaround used
   to stop a question rendering a literal `{placeholder}`.

## Interrupt → answer → resume (the FAQ pattern)

Proven to work: `sm["_flow_state"]` is a real pause stack. Switching flows pushes the
current instance's filled/pending/task_results, and `resume_flow` (by keyword, by
`intent='resume'`, or by an automatic offer) restores it with slots intact and re-asks
nothing. For a one-shot FAQ that does NOT need a second flow — the service-charge
question, say — the lighter and proven shape is a `passive_slot(kind="intent",
option_cues=...)` plus a condition-gated `announce`, which answers and returns to the
pending question in the SAME turn. Caveats: it is one-shot per slot (asking the same FAQ
twice is not expressible), two FAQ topics in one utterance is not expressible, and under
a single-agent router the automatic resume offer is unreachable.
