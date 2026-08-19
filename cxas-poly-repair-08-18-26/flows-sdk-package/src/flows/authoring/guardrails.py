"""Guardrails — the platform's own checks on what the caller says and what the agent says.

A guardrail is a CES *resource*, not a callback and not a tool. It lives at
`guardrails/<Name>/<Name>.json` and is attached BY DISPLAY NAME through a `guardrails`
array that exists on both the app and an agent. Nothing calls it; the platform evaluates
it around the turn.

    App(guardrails=[flows.safety("Safety"), flows.prompt_guard("Prompt Guard")])

Until now `App(guardrails=[...])` took only strings — names of resources someone had
created in the console, which `flows` could reference but never produce. A name with no
resource behind it is a guardrail that never applies, so the list could silently mean
nothing. The constructors here emit the resource too; bare strings still work and still
mean "a name I expect the target to already have".

What actually prevents anything — TYPE as well as scope
------------------------------------------------------
**This is the decision the API exists to force, and it is not symmetric.** Measured in
audio, on both models, in ces-probes `102`-`103`, `108`, `109`:

                          scope="user"    scope="agent"
    blocklist  (filter)   prevents        prevents on BOTH models
    policy     (judged)   prevents        composite: prevents
                                          flash-live: DETECTS ONLY — the caller hears
                                          the offending line, and then the action

A judged rule cannot run until there is a response to judge, which on a streaming model
is after the words are out. A deterministic matcher has no such dependency and gates the
stream — for a literal (`108`) and for a regex (`109`) alike. The transcript shows only
the action in every one of these cases, so none of it is visible to anything reading text.

The consequence for authoring: **if a rule can be expressed as a phrase or a pattern, use
`blocklist`.** It is free, it has no false positives, and on a voice agent it is the only
response-side control that actually stops the line.

A `policy` at `scope="agent"` is a DETECTOR on the live model. Pair it with
`transfer_to(...)`, which changes what happens next, rather than `respond(...)`, which
tries to replace a line the caller has already heard. `validate_app` warns about exactly
that pairing — and only for a policy, since a filter does not have the problem.

The awkward part is what is left over. The failures worth guarding that a matcher CANNOT
express — a hallucinated confirmation, a false "you're verified" — are also not
predictable from the caller's turn, so `scope="user"` cannot catch them either.
Detect-then-hand-off is the honest ceiling for those on flash-live.

Naming
------
`safety` / `blocklist` / `policy` / `prompt_guard` are the four CES types.
`respond` / `generate` / `transfer_to` are the three `TriggerAction` outcomes, named for
what they do rather than reusing `say` (polymorphic text), `handoff` (a telephony vendor
payload) or `escalate` (the flow-level give-up rail) — all three of which already mean
something else here, and none of which is this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Union

# scope= -> the CES `policyScope` enum. "user" is the only one that PREVENTS on a live
# model (ces-probes 102/103), which is why it is spelled out rather than defaulted into.
_SCOPES = {
    "user": "USER_QUERY",
    "agent": "AGENT_RESPONSE",
    "both": "USER_QUERY_AND_AGENT_RESPONSE",
}

# match= -> the CES `matchType` enum.
_MATCHES = {
    "any": "SIMPLE_STRING_MATCH",
    "word": "WORD_BOUNDARY_STRING_MATCH",
    "regex": "REGEXP_MATCH",
}

_HARM_CATEGORIES = (
    "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_DANGEROUS_CONTENT",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "HARM_CATEGORY_HARASSMENT",
)

# level= -> one threshold applied to every harm category. The names match the CES
# console's own three presets so a guardrail authored here reads the same in the UI.
_SAFETY_LEVELS = {
    "relaxed": "BLOCK_ONLY_HIGH",
    "balanced": "BLOCK_MEDIUM_AND_ABOVE",
    "strict": "BLOCK_LOW_AND_ABOVE",
}

_THRESHOLDS = frozenset(
    {"BLOCK_LOW_AND_ABOVE", "BLOCK_MEDIUM_AND_ABOVE", "BLOCK_ONLY_HIGH", "BLOCK_NONE",
     "OFF"}
)


def _one_of(value: str, table: Mapping[str, str], arg: str) -> str:
  if value not in table:
    raise ValueError(
        f"{arg}={value!r} is not one of {sorted(table)}")
  return table[value]


# ── Actions — what happens when a guardrail fires ────────────────────────────
#
# One `TriggerAction`, three shapes. Omitting the action entirely is legal and lets CES
# generate its own refusal, which is what the console's default guardrails do.


@dataclass(frozen=True)
class Action:
  """A guardrail's outcome. Build one with `respond`, `generate` or `transfer_to`.

  Omitting `on_trigger` does NOT mean "no action" — see `Guardrail.resource_body`. CES
  requires one, and a guardrail without it fails at runtime rather than at deploy.
  """

  body: Mapping[str, Any]
  # The Agent this action transfers to, when it is a transfer. Held so the build can
  # check the target really is an agent in this app instead of letting a typo become a
  # runtime no-op.
  target: Optional[Any] = None

  def to_dict(self) -> dict[str, Any]:
    return dict(self.body)


def respond(text: str) -> Action:
  """Say exactly this, instead of whatever triggered the guardrail.

  On a `scope="agent"` guardrail against a live model the caller hears the offending
  line first and this second (ces-probes `102`) — prefer `transfer_to` there.
  """
  if not text or not text.strip():
    raise ValueError("respond() needs the text to say")
  return Action({"respondImmediately": {"responses": [{"text": text}]}})


def generate(prompt: str) -> Action:
  """Generate a refusal from `prompt`, rather than saying a fixed line."""
  if not prompt or not prompt.strip():
    raise ValueError("generate() needs a prompt describing the response to generate")
  return Action({"generativeAnswer": {"prompt": prompt}})


def transfer_to(agent: Any) -> Action:
  """Hand the conversation to another agent in this app.

  Takes the `Agent` OBJECT, not its name: the target is then checked at build time.
  CES resolves it by name at deploy, and a name that matches nothing fails silently —
  the guardrail triggers and the caller goes nowhere.

  This is the right action for a `scope="agent"` rule on a live model. It cannot un-say
  the line, but it changes what happens next, which `respond` does not.
  """
  name = getattr(agent, "name", agent)
  if not isinstance(name, str) or not name.strip():
    raise ValueError(
        "transfer_to() takes the Agent object (or its display name) to transfer to")
  return Action({"transferAgent": {"agent": name}}, target=agent)


# ── The guardrail resource ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Guardrail:
  """One CES guardrail resource, emitted as `guardrails/<Name>/<Name>.json`.

  `payload` is the type-specific block (exactly one of the five CES guardrail types).
  Build one with `safety`, `blocklist`, `policy` or `prompt_guard`.
  """

  name: str
  payload: Mapping[str, Any]
  action: Optional[Action] = None
  description: str = ""
  enabled: bool = True
  # Set on an llmPolicy so the build can lint scope/action combinations without
  # re-deriving them out of the emitted JSON.
  scope: str = ""

  @property
  def dir_name(self) -> str:
    """The on-disk directory/file stem. CES's own convention: spaces become `_`."""
    return self.name.replace(" ", "_")

  def resource_body(self) -> dict[str, Any]:
    """The resource JSON, minus its `name` (the UUID the emitter mints)."""
    doc: dict[str, Any] = {"displayName": self.name}
    if self.description:
      doc["description"] = self.description
    doc["enabled"] = self.enabled
    # An action is REQUIRED, even though the proto field is optional. A guardrail with
    # none deploys cleanly and then fails every turn at runtime with
    # `400 Trigger action type is not supported: ACTION_NOT_SET` — the app is not
    # broken until someone talks to it, so neither the emit nor a schema check catches
    # it. `{"generativeAnswer": {}}` is what the console writes for "generate a
    # response" with no specific instruction, and is what every real guardrail we have
    # carries.
    doc["action"] = (self.action.to_dict() if self.action is not None
                     else {"generativeAnswer": {}})
    doc.update(self.payload)
    return doc

  def payload_entry(self) -> dict[str, Any]:
    """What the scaffold request carries."""
    return {"name": self.name, "dir": self.dir_name, "resource": self.resource_body()}


def _guardrail(name: str, payload: Mapping[str, Any], action: Optional[Action],
               description: str, enabled: bool, scope: str = "") -> Guardrail:
  if not name or not name.strip():
    raise ValueError("a guardrail needs a display name")
  if action is not None and not isinstance(action, Action):
    raise ValueError(
        "on_trigger= must be flows.respond(...), flows.generate(...) or "
        f"flows.transfer_to(...), got {type(action).__name__}")
  return Guardrail(name=name, payload=payload, action=action, description=description,
                   enabled=enabled, scope=scope)


def safety(
    name: str = "Safety",
    *,
    level: str = "balanced",
    overrides: Optional[Mapping[str, str]] = None,
    on_trigger: Optional[Action] = None,
    description: str = "",
    enabled: bool = True,
) -> Guardrail:
  """Google's harm-category filters — hate speech, dangerous content, sexual, harassment.

  `level` sets one threshold across all four (`relaxed` / `balanced` / `strict`, the
  console's own presets). `overrides` tunes or disables individual categories:

      flows.safety("Safety", level="strict",
                   overrides={"HARM_CATEGORY_DANGEROUS_CONTENT": "BLOCK_ONLY_HIGH"})
  """
  threshold = _one_of(level, _SAFETY_LEVELS, "level")
  settings = {c: threshold for c in _HARM_CATEGORIES}
  for category, value in (overrides or {}).items():
    if category not in _HARM_CATEGORIES:
      raise ValueError(
          f"safety() override {category!r} is not a harm category; expected one of "
          f"{list(_HARM_CATEGORIES)}")
    if value not in _THRESHOLDS:
      raise ValueError(
          f"safety() threshold {value!r} for {category} is not one of "
          f"{sorted(_THRESHOLDS)}")
    settings[category] = value
  payload = {"modelSafety": {"safetySettings": [
      {"category": c, "threshold": settings[c]} for c in _HARM_CATEGORIES]}}
  return _guardrail(name, payload, on_trigger, description, enabled)


def blocklist(
    name: str,
    phrases: Sequence[str],
    *,
    match: str = "word",
    scope: str = "both",
    diacritics: bool = True,
    on_trigger: Optional[Action] = None,
    description: str = "",
    enabled: bool = True,
) -> Guardrail:
  """Deterministic phrase or regex matching — no LLM, no judgement, no added latency.

  The cheapest guardrail and the only one with no false positives, so it is the right
  tool for a hard stop: a PII pattern, a competitor name, a phrase legal has banned.

      flows.blocklist("PII", [r"\\d{3}-\\d{2}-\\d{4}"], match="regex", scope="agent")

  `scope` picks which side is matched — `user` and `agent` map to the CES fields that
  scan only that side; `both` scans each. `diacritics=False` ignores accents when
  matching.

  **`scope="agent"` PREVENTS here, on both models** — measured in audio, ces-probes
  `108` for a word match and `109` for a regex.
  That is the opposite of `policy`, whose `scope="agent"` only detects on
  `gemini-3.1-flash-live` (`102`). A judge cannot run until there is a response to judge,
  which on a streaming model is after the words are out; a deterministic matcher has no
  such dependency and gates the stream.

  So on a live voice agent this is not merely the cheapest response-side control, it is
  the only one that actually stops the line.
  """
  items = [p for p in (phrases or []) if isinstance(p, str) and p.strip()]
  if not items:
    raise ValueError(f"blocklist({name!r}) needs at least one non-empty phrase")
  if scope not in _SCOPES:
    raise ValueError(f"scope={scope!r} is not one of {sorted(_SCOPES)}")
  key = {"user": "bannedContentsInUserInput",
         "agent": "bannedContentsInAgentResponse",
         "both": "bannedContents"}[scope]
  block: dict[str, Any] = {key: list(items),
                           "matchType": _one_of(match, _MATCHES, "match")}
  if not diacritics:
    block["disregardDiacritics"] = True
  return _guardrail(name, {"contentFilter": block}, on_trigger, description, enabled,
                    scope=scope)


def policy(
    name: str,
    prompt: str,
    *,
    scope: str = "user",
    window: int = 1,
    fail_open: bool = True,
    on_trigger: Optional[Action] = None,
    description: str = "",
    enabled: bool = True,
) -> Guardrail:
  """A rule in natural language, judged per turn by a separate model.

  `scope` defaults to `"user"` deliberately: it is the only scope that PREVENTS on a
  live model. `scope="agent"` is judged after the model has spoken and, on
  `gemini-3.1-flash-live`, after the caller has already heard the line — see the module
  docstring and ces-probes `102`/`103`.

  `window` is how many trailing messages the judge sees (CES defaults to 10; 1 is
  usually right and cheaper). `fail_open=True` means a judge error lets the turn
  through — the safe default when the action hands callers to a human, since a judge
  outage would otherwise transfer every one of them.

  A rule the judge can apply consistently needs three parts: when to trigger, what to
  flag INCLUDING implicit phrasings, and what not to flag. Without the third it
  over-fires on prerequisite steps; without the second it only catches the obvious.
  """
  if not prompt or not prompt.strip():
    raise ValueError(f"policy({name!r}) needs a prompt stating the rule")
  if not isinstance(window, int) or window < 1:
    raise ValueError(f"policy({name!r}) window= must be a positive integer")
  block: dict[str, Any] = {
      "maxConversationMessages": window,
      "prompt": prompt,
      "policyScope": _one_of(scope, _SCOPES, "scope"),
      "failOpen": fail_open,
  }
  return _guardrail(name, {"llmPolicy": block}, on_trigger, description, enabled,
                    scope=scope)


def prompt_guard(
    name: str = "Prompt Guard",
    *,
    custom: str = "",
    window: int = 1,
    fail_open: bool = True,
    on_trigger: Optional[Action] = None,
    description: str = "",
    enabled: bool = True,
) -> Guardrail:
  """Jailbreak and prompt-injection screening on the caller's input.

  With no `custom` prompt this is CES's built-in screening, which is what the console
  creates by default. `custom` replaces it with your own classifier prompt.
  """
  block: dict[str, Any] = ({"customPolicy": {"maxConversationMessages": window,
                                             "prompt": custom,
                                             "policyScope": _SCOPES["user"]},
                            "failOpen": fail_open}
                           if custom.strip() else {"defaultSettings": {}})
  return _guardrail(name, {"llmPromptSecurity": block}, on_trigger, description, enabled,
                    scope="user")


# ── Helpers the build and the emitter share ──────────────────────────────────


def display_name(entry: Union[str, Guardrail]) -> str:
  """The name that goes in an app's or agent's `guardrails` array."""
  return entry.name if isinstance(entry, Guardrail) else entry


def resources(entries: Sequence[Union[str, Guardrail]]) -> list[Guardrail]:
  """Just the entries that carry a resource — bare strings reference someone else's."""
  return [e for e in entries or [] if isinstance(e, Guardrail)]
