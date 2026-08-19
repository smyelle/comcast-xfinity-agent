"""Telephony hand-off — the vendor payload that actually routes the caller to a human.

A contact-center platform does not learn that a call is being escalated from anything
the agent SAYS. It learns it from a structured payload on the turn: a `payload`
response part whose `data` carries the vendor's own escalation object. The spoken line
("I'll connect you with a live agent, please hold") is for the caller; the payload is
for the platform, and only one of the two puts a person on the line.

    human = flows.handoff(flows.ujet(menu_id="90"))

    flows.announce("human_transfer", ["Alright — I'll connect you now. Please hold."],
                   handoff=human, requires=["agent_escalation"])

`handoff()` emits BOTH parts of the hand-off as one unit:

    {"type": "payload", "data": {"ujet": {...}}}
    {"type": "end_session", "reason": "transfer", "escalated": True}

and that pairing is the whole point of this module. **A hand-off payload without its
`end_session` is a hang-up, and an `end_session` without its payload is worse.** Both
have shipped:

* Payload with no end: the platform is told to escalate, the agent keeps the leg, and
  the caller sits listening to an agent that has nothing left to say.
* End with no payload: the generic escalate rail (`flows.escalate`) closed the call
  with a friendly line and no payload at all, so the caller was told a person was
  coming and then disconnected. Nothing routed them anywhere. That is the defect
  `escalate(handoff=...)` exists to close.

Because the two parts must travel together, nothing here hands out a lone payload
part. `ujet()` / `dialogflow_cx()` return a `HandoffPayload` (vendor data, not a
response part); only `handoff()` turns one into parts, and it always emits the pair.
The framework validator enforces the same invariant on hand-written configs — a
recognized vendor payload with no following `end_session` is a validation ERROR.

Generic, not UJET-shaped
------------------------
One production app already emits two different vendor shapes (a UJET live-agent
escalation and a legacy Dialogflow CX transfer), and the next customer runs Genesys or
Five9. So the vendor payload is a separate, swappable piece:

    flows.handoff(flows.ujet(menu_id="90"))                          # live agent
    flows.handoff(flows.dialogflow_cx(project="p", location="us",    # platform transfer
                                      agent_id="a", parameters={...}))
    flows.handoff(flows.cxas(project="p", location="us",             # another CES app
                             app_id="a", variables={...}))
    flows.handoff({"genesys": {...}}, escalated=True)                # unrecognized vendor

Adding a vendor means adding it to `HANDOFF_VENDORS` (with a builder) AND to the
validator's `_HANDOFF_VENDORS` table — a CES tool cannot import this module, so the
registry genuinely exists twice, held together by a drift gate
(`packages/flows/tests/test_handoff_vendor_sync.py`) rather than by hand. Until a shape
is in both, the raw-dict form works and simply is not shape-checked.

Surfaces
--------
A hand-off carries NO surface condition by default, and that is a decision rather than
an omission:

* The pair must never be split by a condition. Gating the payload while the
  `end_session` survives reproduces the hang-up exactly, so `surface=` puts the SAME
  condition on both parts and the validator rejects a pair whose conditions differ.
* `{"capability": "payloads"}` — the gate `say()` uses for cards — is the one gate that
  must never appear here: voice declares `payloads: False`, so it would drop the
  hand-off on precisely the surface it exists for. Both this module and the validator
  reject it.
* Unconditional is the safe default. An unrecognized payload part is inert on a chat
  client, while a missing one on a phone call is a dropped caller — and an unknown
  channel resolves to the VOICE surface, so "no condition" is right for every channel a
  deployment has not told the agent about. An app that genuinely serves both and has a
  chat-native escalation elsewhere can say `surface="voice"` and get an atomic pair.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Optional, Union

# The `end_session` reason a hand-off ends on. A hand-off is not a COMPLETED call: the
# conversation continues somewhere else, and reporting that reads `reason` should say so.
HANDOFF_REASON = "transfer"

# Vendor discriminators — the key inside a payload part's `data` that identifies the
# shape. Mirrored in the framework validator (`_HANDOFF_VENDORS`), which is a standalone
# sandbox file and cannot import this one.
UJET_KEY = "ujet"
DIALOGFLOW_KEY = "transferToDialogflow"
# CX Agent Studio (CXAS) — a transfer to another CES app. The discriminator VALUE is the
# CES wire directive `transferToNga`, unchanged: "NGA" (Next Gen Agent) is the platform's
# older name for a CXAS app and is baked into the directive, which is a live integration
# contract verified against ces-deployment-dev. The SDK surface (`cxas()`) uses the current
# product name; the wire key keeps the name CES recognizes — exactly as `dialogflow_cx()`
# emits `transferToDialogflow`.
CXAS_KEY = "transferToNga"

# The vendor registry: discriminator -> the fields the validator requires inside that
# vendor's object. Dialogflow CX's and CXAS's payloads are the target STRING, so they
# have none.
#
# This is the twin of the validator's `_HANDOFF_VENDORS`. The duplication is forced —
# a CES tool cannot import this module, and the alternative (a marker key on the wire)
# would change bytes a live integration depends on — so the two are held together by a
# DRIFT GATE instead: `packages/flows/tests/test_handoff_vendor_sync.py` fails when
# they disagree on the vendor keys or on a vendor's required fields. Adding a vendor
# means adding it in both places, and now the gate says so out loud.
HANDOFF_VENDORS: dict[str, tuple[str, ...]] = {
    UJET_KEY: ("menu_id", "action", "escalation_reason"),
    DIALOGFLOW_KEY: (),
    CXAS_KEY: (),
}

# UJET escalation defaults, from the shape a live app emits.
UJET_ESCALATION_ACTION = "escalation"
_UJET_MESSAGE_TYPE = "action"
_UJET_DEFAULT_REASON = "by_virtual_agent"
_UJET_DEFAULT_LANGUAGE = "en"

# `projects/<p>/locations/<l>/agents/<a>` — with `{slot}` placeholders allowed in each
# segment, since the target is routinely loaded from a backend at runtime.
_DFCX_AGENT_RE = re.compile(
    r"^projects/[^/]+/locations/[^/]+/agents/[^/]+$")

# `projects/<p>/locations/<l>/apps/<a>` — a CXAS target is a CES app, not a Dialogflow
# agent, so the resource ends in `apps/`, not `agents/`. `{slot}` placeholders allowed.
_CXAS_APP_RE = re.compile(
    r"^projects/[^/]+/locations/[^/]+/apps/[^/]+$")

_PAYLOADS_CAPABILITY = "payloads"


def _require(value: Any, what: str) -> str:
  """A required string field, rejected when blank."""
  text = "" if value is None else str(value).strip()
  if not text:
    raise ValueError(f"{what} is required and cannot be empty")
  return text


@dataclass(frozen=True)
class HandoffPayload:
  """One vendor's hand-off object — the `data` of the payload part, and nothing else.

  Deliberately NOT a response part: a part could be dropped into a `response` list on
  its own, which is the failure this module exists to prevent. Pass it to
  :func:`handoff`, which is the only thing that can turn it into parts.

  `escalation` records what the payload MEANS — a live-agent escalation (which reports
  as an escalated call) versus a platform-to-platform transfer. It is the default for
  the `escalated` flag on the paired `end_session`.
  """

  vendor: str
  data: dict[str, Any]
  escalation: bool

  def payload_part(self) -> dict[str, Any]:
    """The `payload` response part for this vendor object (a fresh deep copy)."""
    return {"type": "payload", "data": copy.deepcopy(self.data)}


def ujet(
    *,
    menu_id: Union[str, int],
    escalation_reason: str = _UJET_DEFAULT_REASON,
    language: str = _UJET_DEFAULT_LANGUAGE,
    action: str = UJET_ESCALATION_ACTION,
    message_type: str = _UJET_MESSAGE_TYPE,
    extra: Optional[dict[str, Any]] = None,
) -> HandoffPayload:
  """A UJET hand-off object — the live-agent escalation a UJET-fronted call needs.

  Args:
    menu_id: The UJET menu the caller is escalated into. This is the routing decision
      — it picks the queue and therefore which team answers — so there is no default.
      An int is accepted and stringified (menu ids ride the wire as strings).
    escalation_reason: Why the escalation happened. Defaults to `by_virtual_agent`,
      the reason for one the agent itself decided on.
    language: The caller's language, so UJET routes to an agent who speaks it.
    action: The UJET action. `escalation` (the default) is the live-agent hand-off and
      is what marks the paired `end_session` escalated; any other action is treated as
      a plain transfer.
    message_type: The UJET message `type`. `action` unless UJET tells you otherwise.
    extra: Additional vendor fields merged into the object, for anything this
      signature does not name.

  Returns:
    A `HandoffPayload` to pass to :func:`handoff`.
  """
  obj: dict[str, Any] = {
      "menu_id": _require(menu_id, "ujet(): menu_id"),
      "escalation_reason": _require(
          escalation_reason, "ujet(): escalation_reason"),
      "type": _require(message_type, "ujet(): message_type"),
      "action": _require(action, "ujet(): action"),
      "language": _require(language, "ujet(): language"),
  }
  for key, value in (extra or {}).items():
    if key in obj:
      raise ValueError(
          f"ujet(extra=): {key!r} is already a named argument — pass it as "
          f"ujet({key}=...) rather than through extra")
    obj[key] = value
  # Read the registry rather than trusting the signature above to match it: this is
  # what keeps `HANDOFF_VENDORS` load-bearing on this side, so a field added to the
  # table fails HERE (loudly, at authoring time) instead of at the validator, on a
  # payload that has already been written into a config.
  missing = [k for k in HANDOFF_VENDORS[UJET_KEY]
             if not str(obj.get(k, "") or "").strip()]
  if missing:
    raise ValueError(
        f"ujet(): the hand-off registry requires {', '.join(missing)} on a UJET "
        "payload and this one does not carry them — a payload missing a routing "
        "field reaches no queue. Add the argument to ujet(), or pass it via extra=.")
  return HandoffPayload(
      vendor=UJET_KEY, data={UJET_KEY: obj},
      escalation=obj["action"] == UJET_ESCALATION_ACTION)


def dialogflow_cx(
    *,
    agent: Optional[str] = None,
    project: Optional[str] = None,
    location: Optional[str] = None,
    agent_id: Optional[str] = None,
    parameters: Optional[dict[str, Any]] = None,
) -> HandoffPayload:
  """A hand-off to a legacy Dialogflow CX agent — a platform transfer, not an escalation.

  The caller moves to another automated system, so the paired `end_session` is a
  `transfer` that is NOT marked escalated: nobody was escalated to a human, and
  reporting that counts these as escalations overstates every containment number.

  Give the full `agent` path, or `project` / `location` / `agent_id` and let it be
  composed. Any segment may be a `{slot}` placeholder — the target is routinely loaded
  from a backend on the call.

  Args:
    agent: Full `projects/<p>/locations/<l>/agents/<a>` path. Mutually exclusive with
      the three parts.
    project: Project of the target agent.
    location: Location of the target agent (e.g. `us`). Never defaulted: a wrong region
      is a transfer into a different agent, or none at all.
    agent_id: The target agent's id.
    parameters: Session parameters handed to the receiving agent (`{slot}` placeholders
      are interpolated at runtime). Omitted from the payload entirely when empty.

  Returns:
    A `HandoffPayload` to pass to :func:`handoff`.
  """
  parts = (project, location, agent_id)
  if agent and any(parts):
    raise ValueError(
        "dialogflow_cx(): pass agent= (the full path) OR project/location/agent_id, "
        "not both")
  if not agent:
    if not all(parts):
      raise ValueError(
          "dialogflow_cx(): give agent= (the full "
          "projects/<p>/locations/<l>/agents/<a> path) or all three of "
          "project/location/agent_id")
    agent = (f"projects/{_require(project, 'dialogflow_cx(): project')}"
             f"/locations/{_require(location, 'dialogflow_cx(): location')}"
             f"/agents/{_require(agent_id, 'dialogflow_cx(): agent_id')}")
  agent = _require(agent, "dialogflow_cx(): agent")
  if not _DFCX_AGENT_RE.match(agent):
    raise ValueError(
        f"dialogflow_cx(): agent must be a projects/<p>/locations/<l>/agents/<a> "
        f"path, got {agent!r} — the telephony platform routes on this string, so a "
        "malformed one is a transfer that silently goes nowhere")
  if parameters is not None and not isinstance(parameters, dict):
    raise TypeError(
        "dialogflow_cx(parameters=): expected a dict of session parameters, got "
        f"{type(parameters).__name__}")
  data: dict[str, Any] = {DIALOGFLOW_KEY: agent}
  if parameters:
    data["parameters"] = dict(parameters)
  return HandoffPayload(vendor=DIALOGFLOW_KEY, data=data, escalation=False)


def cxas(
    *,
    app: Optional[str] = None,
    project: Optional[str] = None,
    location: Optional[str] = None,
    app_id: Optional[str] = None,
    variables: Optional[dict[str, Any]] = None,
) -> HandoffPayload:
  """A hand-off to another CX Agent Studio app — a platform transfer, not an escalation.

  The live conversation is handed to another CES app, which takes the caller over
  directly — its own greeting, tools and latency-hiding — with no synchronous reply for
  this app to wait on. It is the exact twin of :func:`dialogflow_cx`, one CES app over
  instead of a legacy Dialogflow CX agent, and the flows-native form of the
  `transferToNga` directive a raw `before_model_callback` would otherwise emit by hand.
  (CXAS is the current name for what the CES wire directive still calls an NGA.)

  Because the caller moves to another automated system, the paired `end_session` is a
  `transfer` that is NOT marked escalated: nobody reached a human, and reporting that
  counts these as escalations overstates every containment number.

  This is a transfer to a different APP. To move between sub-agents WITHIN this app,
  use `announce(transfer_to=...)`, which emits the in-app `transfer` part instead.

  Give the full `app` resource, or `project` / `location` / `app_id` and let it be
  composed. Any segment may be a `{slot}` placeholder — the target is routinely loaded
  from a backend on the call.

  Args:
    app: Full `projects/<p>/locations/<l>/apps/<a>` resource. Mutually exclusive with
      the three parts.
    project: Project of the target app.
    location: Location of the target app (e.g. `us`). Never defaulted: a wrong region
      is a transfer into a different app, or none at all.
    app_id: The target app's id.
    variables: Session variables seeded into the receiving app (`{slot}` placeholders
      are interpolated at runtime). Omitted from the payload entirely when empty. This
      is the transfer directive's own key — the counterpart of `dialogflow_cx`'s
      `parameters`.

  Returns:
    A `HandoffPayload` to pass to :func:`handoff`.
  """
  parts = (project, location, app_id)
  if app and any(parts):
    raise ValueError(
        "cxas(): pass app= (the full resource) OR project/location/app_id, not both")
  if not app:
    if not all(parts):
      raise ValueError(
          "cxas(): give app= (the full projects/<p>/locations/<l>/apps/<a> resource) "
          "or all three of project/location/app_id")
    app = (f"projects/{_require(project, 'cxas(): project')}"
           f"/locations/{_require(location, 'cxas(): location')}"
           f"/apps/{_require(app_id, 'cxas(): app_id')}")
  app = _require(app, "cxas(): app")
  if not _CXAS_APP_RE.match(app):
    raise ValueError(
        f"cxas(): app must be a projects/<p>/locations/<l>/apps/<a> resource, got "
        f"{app!r} — the platform routes on this string, so a malformed one is a "
        "transfer that silently goes nowhere")
  if variables is not None and not isinstance(variables, dict):
    raise TypeError(
        "cxas(variables=): expected a dict of session variables, got "
        f"{type(variables).__name__}")
  data: dict[str, Any] = {CXAS_KEY: app}
  if variables:
    data["variables"] = dict(variables)
  return HandoffPayload(vendor=CXAS_KEY, data=data, escalation=False)


# ── The pair ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Handoff:
  """A vendor hand-off and the session end that has to travel with it.

  Built by :func:`handoff`. Every consumer goes through :meth:`parts`, so there is no
  way to emit one half: an author who wants the payload gets the `end_session` too.
  """

  payload: HandoffPayload
  reason: str = HANDOFF_REASON
  escalated: bool = True
  condition: Optional[dict[str, Any]] = None

  def parts(self) -> list[dict[str, Any]]:
    """The response parts: the vendor payload, then the `end_session` that ends the leg.

    Order is load-bearing. The payload has to reach the platform before the leg closes,
    which is also the order every live app emits it in.
    """
    payload_part = self.payload.payload_part()
    end: dict[str, Any] = {"type": "end_session", "reason": self.reason}
    if self.escalated:
      end["escalated"] = True
    if self.condition is not None:
      # The SAME condition on both, never one of them: a surviving `end_session` whose
      # payload was filtered out is the hang-up this module exists to prevent.
      payload_part["condition"] = copy.deepcopy(self.condition)
      end["condition"] = copy.deepcopy(self.condition)
    return [payload_part, end]

  def on_exhaust(self, say: str = "", **extra: Any) -> dict[str, Any]:
    """An `on_exhaust` disposition that hands the caller off instead of apologizing.

    Drops straight into a task's `on_failure.on_exhaust` or a slot's
    `validation.on_exhaust` — the two rungs where a flow has run out of retries and the
    only honest next step is a person:

        flows.task(..., on_failure={"max_retries": 3,
                                    "on_exhaust": human.on_exhaust("...")})

    `say` is spoken verbatim on that turn (an exhaust preempts), and the parts ride the
    same turn. No `then`: the response already ends the session, so a control tool
    fired here would be a second, competing disposition.
    """
    for key in ("then", "response", "fill"):
      if key in extra:
        raise ValueError(
            f"Handoff.on_exhaust(): {key!r} cannot be combined with a hand-off. "
            "The hand-off IS the disposition — it routes the caller and ends the "
            f"leg — so a {key!r} alongside it is a second one that either never runs "
            "or runs on top of it.")
    block: dict[str, Any] = {}
    if say:
      block["say"] = say
    block["response"] = self.parts()
    block.update(extra)
    return block

  def __iter__(self):
    """Unpack as parts — `[*handoff]` / `list(handoff)`."""
    return iter(self.parts())


def _condition_names_payloads(spec: Any) -> bool:
  """Whether a condition reads the `payloads` capability anywhere in its tree."""
  if isinstance(spec, dict):
    if spec.get("capability") == _PAYLOADS_CAPABILITY:
      return True
    return any(_condition_names_payloads(v) for v in spec.values())
  if isinstance(spec, (list, tuple)):
    return any(_condition_names_payloads(v) for v in spec)
  return False


def handoff(
    payload: Union[HandoffPayload, dict[str, Any]],
    *,
    reason: str = HANDOFF_REASON,
    escalated: Optional[bool] = None,
    surface: Optional[str] = None,
    condition: Optional[dict[str, Any]] = None,
) -> Handoff:
  """Hand the caller to a live agent (or another platform), payload and end together.

  Attach the result wherever the flow gives up the call:

  * `flows.escalate(say=..., handoff=h)` — the flow-level escalate rail
  * `flows.announce(name, [text], handoff=h)` — a terminal announce
  * `h.on_exhaust(say=...)` — a task's `on_failure.on_exhaust` or a slot's
    `validation.on_exhaust`

  Args:
    payload: A `flows.ujet(...)` / `flows.dialogflow_cx(...)` / `flows.cxas(...)` object,
      or a raw vendor `data` dict for a platform with no builder yet (`escalated` is then
      required —
      no default is right for a shape nothing here recognizes).
    reason: The `end_session` reason. `transfer` unless you have a reason to differ:
      the call did not complete, it left for another system, and reporting reads this.
    escalated: Whether the leg ends as an ESCALATED call. Defaults to what the payload
      means — true for a live-agent escalation, false for a platform transfer.
    surface: Emit the pair only on this surface (e.g. `"voice"`). The condition goes on
      BOTH parts, so the pair can never be split. Omit it unless the app genuinely has
      a different escalation path on another surface — an unrecognized payload is inert
      on a client that cannot use it, and a dropped one on a phone call is not.
    condition: A full declarative condition, for a gate `surface` cannot express. Same
      rule: it is applied to both parts.

  Returns:
    A `Handoff`.
  """
  if isinstance(payload, Handoff):
    raise TypeError(
        "handoff(): already a Handoff — pass the vendor payload "
        "(flows.ujet(...) / flows.dialogflow_cx(...)), not the result of handoff()")
  if isinstance(payload, dict):
    if set(payload) >= {"type", "data"}:
      raise ValueError(
          "handoff(): got a whole response part, not a vendor payload. Pass the "
          "part's `data` (e.g. {'ujet': {...}}) — handoff() builds the part.")
    if not payload:
      raise ValueError(
          "handoff(): the vendor payload is empty; there would be nothing for the "
          "platform to route on")
    if escalated is None:
      raise ValueError(
          "handoff(): pass escalated=True (a live-agent escalation) or "
          "escalated=False (a transfer to another platform) with a raw vendor "
          "payload. It is what marks the call escalated in reporting, and no default "
          "is right for a shape this SDK does not recognize — or use a builder "
          "(flows.ujet / flows.dialogflow_cx / flows.cxas), which knows.")
    payload = HandoffPayload(
        vendor=sorted(payload)[0], data=dict(payload), escalation=escalated)
  if not isinstance(payload, HandoffPayload):
    raise TypeError(
        "handoff(): expected flows.ujet(...) / flows.dialogflow_cx(...) / flows.cxas(...) "
        f"or a raw vendor payload dict, got {type(payload).__name__}")
  if surface is not None and condition is not None:
    raise ValueError(
        "handoff(): pass surface= or condition=, not both — surface= IS a condition "
        '({"surface": name}) and the two would fight over the same key')
  gate: Optional[dict[str, Any]] = condition
  if surface is not None:
    gate = {"surface": _require(surface, "handoff(): surface")}
  if gate is not None and not isinstance(gate, dict):
    raise TypeError(
        "handoff(condition=): expected a declarative condition dict, got "
        f"{type(gate).__name__}")
  if _condition_names_payloads(gate):
    raise ValueError(
        "handoff(): the `payloads` capability is the one gate a hand-off must not "
        "use. Voice declares payloads:False, so this would drop the hand-off on "
        'exactly the surface it exists for. Use surface="voice" to restrict it to '
        "telephony, or leave it unconditional.")
  return Handoff(
      payload=payload,
      reason=_require(reason, "handoff(): reason"),
      escalated=payload.escalation if escalated is None else bool(escalated),
      condition=gate,
  )


def as_handoff(value: Any, caller: str) -> Handoff:
  """Coerce a `handoff=` argument, so every site accepts the same three spellings.

  A bare `HandoffPayload` is promoted with the vendor's own defaults, which is what
  makes `announce(..., handoff=flows.ujet(menu_id="90"))` read straight. A raw dict is
  NOT — it cannot say whether the call ends escalated, and guessing that is how a
  containment metric quietly becomes wrong.
  """
  if isinstance(value, Handoff):
    return value
  if isinstance(value, HandoffPayload):
    return handoff(value)
  if isinstance(value, dict):
    raise TypeError(
        f"{caller}: handoff= takes flows.handoff(...) / flows.ujet(...) / "
        "flows.dialogflow_cx(...) / flows.cxas(...). Wrap a raw vendor payload in "
        "flows.handoff(data, escalated=...) so the end_session it needs is emitted "
        "with it — a payload on its own leaves the caller on a call nobody is coming to.")
  raise TypeError(
      f"{caller}: handoff= expected a flows.handoff(...), got "
      f"{type(value).__name__}")


# ── Native-channel return on end_session.params ──────────────────────────────
# A different member of the handoff family: where `Handoff` delivers vendor data as a
# separate `payload` PART + a paired `end_session`, a NATIVE contact-center channel (e.g.
# FIVE9) reads its outbound data ONLY from the `end_session` tool call's
# `params[<envelope>]` — a payload part never reaches it, and a session variable never
# reaches the wire on its own. `EndParamsHandoff` declares that channel + the session
# variable the app stages the return into; attached flow-wide via `flow.on_end(...)`, the
# framework's deterministic terminal emit folds `{envelope: state[from_state]}` onto the
# end_session at EVERY terminal end (any reason). See docs/end-params-handoff.md.


@dataclass(frozen=True)
class EndParamsHandoff:
  """A native-channel return delivered on the end_session tool call's `params`.

  `envelope` is the params key the channel reads (FIVE9 / CX Agent Studio:
  `LIVE_AGENT_HANDOFF`); `from_state` is the session variable the app stages the return
  into (e.g. a terminal setter computes the disposition -> xHeaders). The framework resolves
  `{envelope: state[from_state]}` at the terminal choke and emits it deterministically, so
  the model can never drop the end. Attach with `flow.on_end(...)`; it is a no-op on any leg
  whose `from_state` is unstaged.
  """

  envelope: str
  from_state: str

  def to_config(self) -> dict[str, Any]:
    """The flow-level `on_end` config the engine reads."""
    return {"delivery": "end_params", "envelope": self.envelope, "from_state": self.from_state}


def end_params_handoff(*, envelope: str, from_state: str) -> EndParamsHandoff:
  """Declare a native-channel return delivered on `end_session.params[envelope]`, sourced at
  runtime from the session variable `from_state`. Attach flow-wide with `flow.on_end(...)`.

  The app still COMPUTES the return and stages it into `from_state` (its own disposition
  logic); this declares the channel/contract once instead of a magic reserved var. See
  docs/end-params-handoff.md.
  """
  if not isinstance(envelope, str) or not envelope.strip():
    raise ValueError("end_params_handoff(): envelope must be a non-empty string")
  if not isinstance(from_state, str) or not from_state.strip():
    raise ValueError("end_params_handoff(): from_state must be a non-empty string")
  # from_state names a session variable the engine reads at runtime; hold it to a plain
  # identifier so a malformed name can never compile into a bogus variable reference.
  if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", from_state):
    raise ValueError(
        "end_params_handoff(): from_state must be a valid variable identifier "
        f"(^[A-Za-z_][A-Za-z0-9_]*$); got {from_state!r}")
  return EndParamsHandoff(envelope=envelope, from_state=from_state)
