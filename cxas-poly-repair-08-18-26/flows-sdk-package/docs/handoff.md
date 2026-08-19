# Telephony hand-off — reaching a live agent

A contact-center platform does not learn that a call is being escalated from anything
the agent says. It learns it from a structured payload on the turn: a `payload` response
part whose `data` carries the vendor's own escalation object. The spoken line is for the
caller; the payload is for the platform, and only one of the two puts a person on the
line.

```python
human = flows.handoff(flows.ujet(menu_id="90"))

flow.set("escalate", flows.escalate(
    say="Of course — let me get you to someone who can help.",
    handoff=human,
))
```

That emits both parts of the hand-off, in this order:

```json
{"type": "payload", "data": {"ujet": {"menu_id": "90", "escalation_reason": "by_virtual_agent",
                                      "type": "action", "action": "escalation", "language": "en"}}}
{"type": "end_session", "reason": "transfer", "escalated": true}
```

Runnable demo: [`examples/telephony_handoff.py`](../examples/telephony_handoff.py).

## The pair is the point

**A hand-off payload without its `end_session` is a hang-up, and an `end_session`
without its payload is worse.** Both have shipped:

- *Payload, no end.* The platform is told to escalate; the agent keeps the leg. The
  caller hears "connecting you now" and then sits listening to an agent that has nothing
  left to say.
- *End, no payload.* This was the generic `escalate` rail's behavior. It spoke a
  friendly line and emitted a bare `end_session`, so the caller was told a person was
  coming and was then disconnected with nothing routing them anywhere. Every log said
  the escalation succeeded.

So nothing here hands out a lone payload part. `ujet()` and `dialogflow_cx()` return a
`HandoffPayload` — vendor data, not a response part — and only `handoff()` turns one
into parts, always as a pair. On the other side, the framework validator **rejects** a
config whose payload has no `end_session` after it, whether the parts were authored by
these builders or written by hand:

```
Slot 'human_transfer' response[1] is a UJET hand-off payload with no 'end_session' part
after it. The payload asks the platform to route the caller, but nothing gives up the
leg — the agent keeps the call and the caller waits for a person who never arrives.
```

## Where a hand-off goes

A flow gives up a call in exactly four places, and all four take one. Declare the
hand-off once and reuse it: the menu id is the routing decision — it picks which team
answers — and four hand-written copies are four chances for one to drift.

| Site | How |
|---|---|
| the flow-level escalate rail | `flows.escalate(say=..., handoff=human)` |
| a terminal announce | `flows.announce(name, [text], handoff=human)` |
| a task's failure ladder | `on_failure={"on_exhaust": human.on_exhaust("...")}` |
| a slot's No-Match ladder | `flows.user_slot(..., on_exhaust_handoff=human)` |

```python
flows.announce("to_an_agent", ["Connecting you with an agent now. Please hold."],
               requires=["anything_else"], handoff=human)

flows.task("Lookup", "lookup_order", ["order_number"], "status_msg",
           on_failure={"max_retries": 2,
                       "on_exhaust": human.on_exhaust("I can't reach that system, "
                                                      "so I'll put you through.")})

flows.user_slot("order_number", ask="What's your order number?",
                on_exhaust="Let me get you to someone who can look it up.",
                on_exhaust_handoff=human)
```

`user_slot`'s default exhaust is `then: {"tool": "transfer_to_human"}`, and that marker
only **records** the request — on a platform that routes on a payload it reaches nobody.
`on_exhaust_handoff` is what replaces the marker with the real thing, and passing both
raises: they are competing dispositions for the same rung.

## Vendors

The vendor payload is a separate, swappable piece, because one app already emits two
shapes and the next customer runs something else entirely.

```python
flows.handoff(flows.ujet(menu_id="90"))                              # live agent
flows.handoff(flows.dialogflow_cx(project="p", location="us",        # platform transfer
                                  agent_id="a", parameters={...}))
flows.handoff(flows.cxas(project="p", location="us",                 # another CES app
                         app_id="a", variables={...}))
flows.handoff({"genesys": {...}}, escalated=True)                    # no builder yet
```

`escalated` is derived from what the payload MEANS, and they differ:

- **UJET** with `action="escalation"` is a live-agent escalation, so the end is marked
  `escalated: true`.
- **Dialogflow CX** is a transfer to another automated system. Nobody was escalated, so
  the end carries no `escalated` flag — marking it would overstate every escalation
  count that reads it.
- **CX Agent Studio** (`cxas()`) is the same kind of platform transfer, one CES app over:
  the live conversation is handed to another CES app, which takes the caller over
  directly. Not an escalation, so the end carries no `escalated` flag. The wire directive
  it emits is `transferToNga` — the platform's older name for a CXAS app, and a live
  contract — exactly as `dialogflow_cx()` emits `transferToDialogflow`. Seed the receiving
  app with `variables={...}` (the counterpart of `dialogflow_cx`'s `parameters`). To move
  between sub-agents *within* one app, use `announce(transfer_to=...)` instead — that is an
  in-app transfer, not a hand-off.

A raw dict has to say which it is (`escalated=True` / `False`). There is no right
default for a shape the SDK cannot read, and guessing is how a containment metric
quietly becomes wrong.

Adding a vendor properly means adding it to `flows.authoring.handoff`'s
`HANDOFF_VENDORS` (plus a builder) **and** to the validator's `_HANDOFF_VENDORS` table.
A CES tool cannot import the authoring module, and the alternative — a marker key so a
hand-off announces itself on the wire instead of being recognized structurally — would
change bytes a live integration depends on. So the registry genuinely exists twice, and
a **drift gate** holds the two together:
`packages/flows/tests/test_handoff_vendor_sync.py` fails when they disagree on the
vendor keys or on a vendor's required fields. Until a shape is in both, the raw-dict
form works and simply is not shape-checked.

## Surfaces

A telephony hand-off is meaningless on a chat surface, and yet it carries **no
condition** by default. That is a decision:

- The pair must never be split. Gating the payload while the `end_session` survives
  reproduces the hang-up exactly, so `surface=` puts the same condition on **both**
  parts and the validator rejects a pair whose conditions differ.
- `{"capability": "payloads"}` — the gate `say()` uses for cards — is the one gate a
  hand-off must never use. Voice declares `payloads: False`, so it would drop the
  hand-off on precisely the surface it exists for. Both the builder and the validator
  reject it.
- Unconditional is the safe direction. An unrecognized payload part is inert on a client
  that cannot use it; a missing one on a phone call is a dropped caller. An unknown
  channel resolves to the **voice** surface, so "no condition" is right for every
  channel a deployment has not told the agent about.

An app that genuinely serves both, with a chat-native escalation authored elsewhere, can
ask for an atomic gated pair:

```python
flows.handoff(flows.ujet(menu_id="90"), surface="voice")
```

## What the validator checks

Every rule keys on a **recognized vendor shape**, so an ordinary payload part — a card,
chips, an app's own structured data — is untouched.

| | |
|---|---|
| error | a hand-off payload with no `end_session` after it |
| error | a hand-off payload and its `end_session` carrying different conditions |
| error | a hand-off payload gated on the `payloads` capability |
| error | a UJET payload missing `menu_id` / `action` / `escalation_reason` |
| error | a control block carrying both a hand-off and `transfer_to` |
| warning | a live-agent escalation whose `end_session` is not marked `escalated` |
| warning | a platform transfer whose `end_session` claims `escalated` |
| warning | a hand-off whose `end_session` reason is not `transfer` |

The checks run on every response list, including two the validator did not previously
walk at all: an `on_exhaust`'s `response` and a control block's.

## `escalate` vs `transfer_to`

They are different hand-offs and cannot both happen:

- `transfer_to` moves control to another **agent inside this app**. The session
  continues; nothing ends.
- A hand-off ends the leg and gives the caller to the **contact-center platform**.

Passing both to `escalate()` or `announce()` raises, and a config carrying both is a
validation error — which of the two the client honors is not something to leave to the
client.
