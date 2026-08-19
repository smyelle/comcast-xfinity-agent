# Steering — a first-class intent router

`router_flow` emits CES's `router.json` shape: a `Flow` with `router=True`, an
`active_flow` gate, and a `set_active_flow` bootstrap. On its own it takes bare flow-key
strings and knows nothing about *why* a caller belongs on a route, what to do with a route
it recognises but does not handle, or which phrasings should skip the model — so authors
hand-wrote the `<routing>` instruction, a parallel `route_cues` dict, and one
recognise-and-hand-off flow per deferred intent, kept in sync **by key** across three
separate structures.

Steering makes that one object. `router_flow` now accepts `flows.route(...)` objects.

```python
import flows

steering = flows.router_flow("steering", [
    flows.route("diagnostics", "internet or WiFi is down, slow, or dropping", flow=diag),
    flows.route("reboot", "wants to restart / power-cycle the gateway", flow=reboot,
                cues=["power cycle the modem", "reset my router"]),
    flows.route("billing", "wants to understand or dispute a charge",
                backstop=["my bill", "a charge", "overcharged"]),
    flows.route("payments", "wants to make a payment or set up autopay"),
    flows.route("human", "asks for a person / to be transferred", flow=handoff),  # real hand-off flow
], disambiguate=flows.disambiguation(max_turns=2, on_exhaust="human"))

app = flows.App(root_flow=steering, agent_instruction="You are an Acme Internet agent.")
```

## The route object

`flows.route(name, description, *, flow=None, cues=(), backstop=(), aliases=(),
subroutes=(), disambiguate=None, default="", explicit_only=False)`

| field | meaning |
|---|---|
| `name` | the intent key — the value the model passes to `set_active_flow`, the child flow's config id, AND the label handed downstream as `detected_intent`. Keep it equal to your downstream category. |
| `description` | a by-MEANING statement of what the destination is for. Feeds the generated `<routing>` classifier — keep it free of verbatim caller phrases (that trains the model on your eval strings). |
| `flow` | the child DAG run when this route is **handled** locally. Omit it (`None`) to **defer** to the auto-generated hand-off flow. **Several routes may share one `flow`** — that's how you give a group of intents a real shared hand-off (a transfer / A2A) instead of the canned deferral. |
| `cues` | deterministic phrasings that route here **before** the model runs — a fast-path for obvious wording. |
| `backstop` | keywords consulted **after** the model declines to classify (the post-model net). |
| `aliases` | extra upstream labels that map onto this route. |
| `subroutes` | child routes — makes this an **internal node** (a deeper level). See *Multi-level steering* below. Mutually exclusive with `flow`. |
| `disambiguate` | per-level override of the low-confidence handling, **inherited** down the tree: `None` inherit / `False` force-silent / `True` \| `disambiguation(...)` ask-when-unsure. |
| `default` | for an internal node, the child a level falls back to when nothing classifies (else auto-derived). |
| `explicit_only` | a route the model must never reach by INFERENCE — only on an explicit request (a human/agent hand-off, a cancel). Generates a "choose only when explicitly asked" directive; a `default_route`/`catch_all_route` cannot target one. See *Structured routing policies* below. |

## What the build generates

From the routes, `build.py` generates — so you write none of it:

* **the `<routing>` instruction**, from the descriptions, appended to your persona;
* **one shared deferral flow** every `flow=None` route maps to, via a **non-identity
  `flow_config_map`** (`{"billing": "steering_defer", "payments": "steering_defer", ...}`).
  The chosen route key survives on the gate, so the single flow records `detected_intent`
  correctly per route — no N near-identical defer flows;
* **`route_cues`**, folded from each route's `cues`.

## Deferral & hand-off

A route with **`flow=None`** is deferred: it maps to ONE auto-generated flow that records
`detected_intent` (the chosen key), speaks a default line, and ends. That's the
zero-config "recognize and hand back to an outer orchestrator" path.

For a **real hand-off** — a live-agent transfer, a custom line, an A2A call — don't use a
canned string; write a normal `flow` and point the routes at it. Because the
`flow_config_map` is non-identity, **many routes can share one `flow`**, and the shared
flow reads the chosen route off the `active_flow` gate:

```python
handoff = flows.Flow("handoff", ...)   # says its line, transfers to a live agent, ends
flows.route("billing", "...", flow=handoff)
flows.route("human",   "...", flow=handoff)   # disambiguation on_exhaust="human" lands here too
```

## The routing order

```
1. cues (keywords)     -> route deterministically, skip the model               [pre-model]
2. model (description) -> classify by meaning                                   [primary]
3. backstop (keywords) -> model declined? fall back to these keywords           [post-model]
```

Routing is **model-first**. `cues` are a deterministic shortcut *under* the model. There is
**no `default_flow`** — it's a pre-model preempt that would send every opening turn to the
default before the model ever classifies (a router-defeating trap). Low confidence is
handled ONE way: **`disambiguate`** tells the model to ask a brief clarifying question
whenever it can't confidently route — ambiguous, unclear, or off-topic — rather than
guessing, and `disambiguation(max_turns, on_exhaust)` hands off after a bounded number of
unresolved turns.

> Tiers 1–2 and the deferral flow are built entirely in the authoring/build/emit layers —
> **no engine change**. The post-model `backstop` net and the hard disambiguation
> turn-budget (`disambiguate(max_turns=…, on_exhaust=…)` → escalate) are a small engine
> capability layered on top.

## Robust to a hallucinated flow id

`flow_types` is a closed set, so `set_active_flow` — the bootstrap that fills the
`active_flow` gate — is an **enum setter**: it takes one of your route names
(case-insensitively) and rejects anything else with `not_in_enum`.

That matters more for a gate than for an ordinary slot. A gate filled with a value that
maps to no config reads as **satisfied** — the router thinks it has routed — while
`flow_config_map` misses, so no child DAG ever drives and the rest of the call is the
model improvising with nothing logged. Measured live on a two-flow router: the model
answered `set_active_flow(flow="Troubleshoot Internet")` — a label it invented rather than
the `triage` route name — and every later turn fired no tool, ending in a transfer to a
human; the identical flow as a standalone app was correct throughout.

Two things prevent it, and you author neither:

* the valid names are listed in the setter's docstring — the tool's model-facing schema —
  so the model has them in front of it (`One of diagnostics, reboot, billing, …`);
* a value that still misses is **rejected, not stored**, leaving the gate unfilled so the
  `cues` / `backstop` nets and the `disambiguate` path get their turn instead of the call
  dead-filling.

```
ENUM-REJECT    caller> my internet is broken
               agent > Are you having trouble with everything, or is it just one app?
               [set_active_flow(flow="Troubleshoot Internet") -> not_in_enum, gate unfilled]
               caller> nothing works
               agent > Understood — the problem is everything.
               [cue/backstop net recovered on the next turn; no wasted turn]
```

Give a route `cues` (and `backstop`) anyway: the deterministic net is what routes when the
model answers with a name no id matches, and it is the difference between a re-ask and a
wasted turn.

## Flow isolation

A single-agent router puts one agent in front of every flow it can reach — that is what
makes routing cheap (no transfer, no second agent), and it means the agent carries the
whole tool surface of every flow on every turn. The framework narrows that surface for
you: on the routing turn `router_hide_tools` hides every flow-specific tool (so the model
routes instead of doing the work itself on this turn), and on **every** turn each flow's
`<config_id>_dag` loader is hidden — for *every* sibling, not just the active one — so the
model cannot wander sideways into a flow nobody routed to. You author none of it; see the
**Flow Isolation** guide for the full picture and the one shape that can still surprise
you.

## Multi-level (hierarchical) steering

A route can carry `subroutes` — child routes — making it an **internal node**, a level in
an intent *tree*. The level-1 router picks a top-level route (a model routing turn, as
above); once the caller is there, a **scoped, silent** classifier picks one child from the
**same** utterance, and recurses. So one opening turn resolves a whole intent **path**
(`billing → billing_dispute → dispute_overcharge`).

```python
flows.router_flow("steering", [
    flows.route("billing", "charges, payments, and disputes", subroutes=[
        flows.route("billing_dispute", "believes a charge is wrong", cues=["dispute a charge"],
            subroutes=[
                flows.route("dispute_latefee", "a late fee they think is unfair", cues=["late fee"]),
                flows.route("dispute_overcharge", "charged more than the plan", cues=["overcharged"]),
            ]),
        flows.route("billing_explain", "just wants the bill explained"),   # deferred leaf
    ]),
    flows.route("diagnostics", "internet is down", flow=diag),             # handled leaf (level 1)
    flows.route("human", "asks for a person", flow=handoff),
], disambiguate=flows.disambiguation(max_turns=2, on_exhaust="human"))
```

A route is one of three things:

* **internal node** — has `subroutes` (classifies into a child; runs no flow of its own);
* **handled leaf** — has `flow=` (runs a DAG). *v1: only at the top level.*
* **deferred leaf** — neither (recognised + handed off; records the detected path).

**What the build generates**, per internal node — all passive intent slots + `App.classifiers`,
which the blessed engine already runs, so **no engine change**:

* a passive `intent_slot` `sub_intent__<node>` that classifies the node's children — its
  `option_cues` are each child's `cues` (+`backstop`; see the note below), and its model
  exemplars are each child's `description`. Deeper slots are condition-gated on the parent
  slot's value, so only the routed subtree fills;
* one `App.classifiers` entry per node (`set_sub_intent__<node>`), with the node's `default`
  child as the fallback (so a silent level always resolves);
* a `<router>_record_path` tool that walks the chain from the `active_flow` gate and records
  `detected_intent` (the deepest leaf) + `detected_path` (the slash-joined ancestry).

**Silent by default, disambiguate per level.** With no `disambiguate` anywhere, every level
is silent — it classifies from the utterance and falls to its `default` when unclear.
`disambiguate=` is **inherited**: set it on the router (or any node) and descendants use it
unless they override. An ASKED level is generated as an *asked* slot that still fills
silently when the utterance is clear — it only asks (up to `max_turns`) on genuine
ambiguity; `disambiguate=False` forces a level back to silent.

```python
flows.route("billing_dispute", "...", subroutes=[...])                      # inherits router budget
flows.route("billing_payment", "...", disambiguate=flows.disambiguation(max_turns=1), subroutes=[...])
flows.route("tech_tv", "...", disambiguate=False, default="tv_nosignal", subroutes=[...])  # never asks
```

> **v1 scope.** Deeper `cues`/`backstop` are both folded into the level's deterministic
> `option_cues` (`cues` first) — the true post-model *timing* of `backstop` is a top-level
> engine capability, not yet generalised to deeper slots. Also not yet supported at depth: a
> handled leaf below level 1, and a cross-route `on_exhaust` on a per-level `disambiguate`
> (use `default=` for the fallback child). Both raise a clear build error rather than fail
> silently. See `examples/steering_multilevel.py`.

### How a level resolves the pick — `classifier_style`

Once the model names a sub-intent, the level maps that answer to a canonical child id. By
default this is the **same enum setter the level-1 gate uses** (`classifier_style="enum"`):
the per-node hint lists the exact child keys, the model returns one, and the setter accepts
it (case-insensitive) or returns `not_in_enum` — one closed-choice mechanism at every level,
the router gate included, so there is a single thing to understand. An **asked** level
recovers a miss through its `on_exhaust_fill` / `default`; a **silent** level has no retry
ladder, so an unresolved pick leaves the level empty and the recorder reports the coarser
*parent* intent — it never guesses a leaf.

`classifier_style="fuzzy"` is the opt-out: a classifier setter that also matches a child by
its description and falls to the level's `default` — more forgiving of the shape the model
emits, and it gives a silent level a leaf default. Reach for it only when a level needs that
looseness; `"enum"` is the consistent default.

## Structured routing policies (fallbacks)

Past the route list, every router needs the same handful of cross-cutting rules: a home
for a turn the model can't place, a triage node for an in-scope task that fits no route,
and a route it must never *infer* its way into. Those are the **structured policies** —
named knobs on `router_flow`, rendered as canonical, tuned text at the tail of the
`<routing>` block, so they stay consistent and traceable instead of being hand-written
prose each app copies and lets drift.

| knob (on `router_flow`) | what it does |
|---|---|
| `default_route="X"` | the **home** — chosen when the model is not confident which flow fits, or the turn is off-topic / small talk. The low-confidence fallback. |
| `catch_all_route="Y"` | a real, in-scope task that fits **no specific route** goes to Y (the triage / main-menu node) rather than escalating to a person. |
| `explicit_only=True` (on a `route`) | a high-precision route (a human/agent hand-off, a cancel) the model must **never reach by inference** — only on an explicit request. The guard against over-escalation. |
| `tie_break="primary"` | a multi-intent utterance routes on the **primary** intent (on by default; `"none"` drops the line). |
| `routing_notes=[...]` | the free-text **escape hatch** for genuinely idiosyncratic guidance. A large one is a smell that a policy is missing. |

`default_route` and `catch_all_route` must each name a real top-level route, and neither
may name an `explicit_only` route (that would defeat the guard) — both raise at build.

```python
steering = flows.router_flow(
    "steering",
    [
        flows.route("billing", "the bill, a charge, or money on the account"),
        flows.route("payments", "make a payment or set up autopay"),
        flows.route("repair", "internet is down, slow, or dropping", flow=repair),
        flows.route("main_menu", "a known account task that fits no route above",
                    flow=main_menu),
        flows.route("human", "reach a live person", flow=handoff,
                    explicit_only=True),
    ],
    default_route="repair",        # unsure / off-topic lands here
    catch_all_route="main_menu",   # in-scope task, no route -> triage
    routing_notes=["Checking a balance is billing, not payments."],
)
```

Those knobs render at the **tail** of the generated `<routing>` block, after the route
list — you write none of the wording:

```text
Route on the caller's actual goal.
A single utterance can mention several things; route on the caller's primary intent.
When you are not confident which flow fits, or the turn is off-topic or small talk, choose "repair".
When the caller wants a specific, in-scope task but none of the flows above fit, choose "main_menu" — do not send them to a person for something it can handle.
Choose "human" only when the caller explicitly asks for it; never route there by inference.
Checking a balance is billing, not payments.
```

```
HOME           caller> is it going to rain later?
               agent > I can't help with the weather, but I can take a look at your service.
               [off-topic -> default_route "repair"; the model doesn't force a wrong route]

CATCH-ALL      caller> I need to change the email on my account
               agent > Sure — let me pull up your account options.
               [in-scope, fits no route -> catch_all_route "main_menu", not a transfer]

EXPLICIT       caller> just put me through to a person
               agent > Okay — connecting you to a specialist now. One moment.
               [explicit ask -> human; never reached by inference]
```

**A knob over a note, every time.** `routing_notes` is prose the model reads literally and
the author re-tunes per app; the knobs are canonical text that is tested and shared across
the SDK. Reach for `default_route` / `catch_all_route` / `explicit_only` / `tie_break` for
the four recurring shapes above, and keep `routing_notes` for the one genuinely
app-specific rule no policy captures — like the billing-vs-payments boundary above. A
`routing_notes` that grows past a line or two is the sign a policy is missing; promote it.

## Multi-agent (HostRouter) — same routing-SI generator

A multi-agent `HostRouter` uses the **same** description-driven `<routing>` block. Give each
routed `Agent` a `description=` and the host's generated instruction (and each specialist's
`<switching>` block) is built from those descriptions via the one shared generator — instead
of the older `config_id`-derived host taskflow. Omit the descriptions and the host falls back
to that taskflow unchanged (back-compat).

## Mid-flow switching

Once a caller is *inside* a routed flow, the framework's own intent-first classifier
(`classify_turn_intent`) handles switching to a sibling. It is **description-driven and
domain-neutral**: it renders the same per-route `flow="X" — <description>` lines the router
turn uses, so one set of route descriptions drives both the initial route *and* mid-call
switches.

## A multi-agent config

The same descriptions drive a `HostRouter` (multi-agent). Each routed `Agent` gets a
`description`, and `robust_switching` (default) turns on the mid-flow classifier:

```python
billing_agent = flows.Agent("Billing_Agent", flow=billing,
    description="the caller wants to understand, question, or pay a charge on their bill")
tech_agent = flows.Agent("Tech_Support_Agent", flow=tech,
    description="the caller's internet or WiFi is down, slow, or dropping")

host = flows.HostRouter("Support_Host",
    routes={"billing": billing_agent, "tech_support": tech_agent})   # robust_switching=True

app = flows.App(host=host, agents=[billing_agent, tech_agent])
```

## Sessions

### Intent detection (single-agent router — `examples/steering.py`)

Live against a deployed build (`ces-deployment-dev`), text:

```
CUE-HIT        caller> Hi, can you power cycle the modem for me?
               agent > I've started a restart on your gateway — it'll be back in ~two minutes.
               [routed -> reboot]              (deterministic route_cue, before the model)

MODEL-ROUTE    caller> my wifi keeps dropping every few minutes
               agent > I ran a quick check and your line looks degraded — let's try a few fixes.
               [routed -> diagnostics]         (semantic classification by description)

DEFER          caller> I want to dispute a charge on my bill
               agent > Thanks — let me get you to the right place for that.
               [routed -> billing]             (flow=None -> shared deferral flow; detected_intent="billing")

DISAMBIGUATE   caller> I'm having a problem with my account
  -> RESOLVE   agent > Are you calling about a bill you received, or make a payment?
               caller> my internet keeps going out
               agent > I ran a quick check and your line looks degraded — let's try a few fixes.
               [routed -> diagnostics]         (asked one clarifying question, then routed)

DISAMBIGUATE   caller> I need help with something
  -> ESCALATE  agent > O.K., can you tell me a bit more — internet, billing, or something else?
               caller> it's kind of a whole situation, hard to explain
               agent > Thanks — let me get you to the right team for that. One moment.
               [routed -> human]               (2 unresolved turns hit max_turns -> on_exhaust)
```

`BACKSTOP` (a `route.backstop=[...]` keyword net) fires only when the model declines to
route at all; with a capable model it rarely triggers on keyword-bearing utterances (which
the model routes directly), which is the point — it's a net for model *failures*, not a
primary path.

### Intent switching (multi-agent — `examples/steering_multi_agent.py`)

Detection routes the opening turn; then a mid-flow switch is recognised by MEANING and
hands off to the sibling specialist (the description-driven, domain-neutral classifier):

```
DETECT         caller> Hi, I have a question about a charge on my bill
               agent > I can help with that billing charge. What's your account number?
               [set_active_flow -> billing]

SWITCH         caller> I need to check my bill
               agent > I can help you check your bill. What's your account number?
               [set_active_flow -> billing]
               caller> actually, forget the bill — my internet keeps dropping
               agent > I can help with your internet dropping. What's your account number?
               [set_active_flow -> tech_support]   (mid-flow switch, no cancel)
```

See `examples/steering.py` / `examples/steering_multi_agent.py` for the full apps and
`tests/test_steering.py` + `tests/test_multi_agent.py` for coverage.
