"""Polymorphic text — one authored intent, projected onto whatever surface answers.

``say()`` is accepted anywhere a plain ``str`` is accepted today. A bare string is
still a bare string, so every existing agent keeps working untouched; reach for
``say()`` only at the handful of places where voice and chat genuinely differ.

    ask=say("Here's what's available this week.",
            brief="I've got {options}. Which works better?",
            card=card(title="Available times", body="{options}"))

What it lowers to is the point. There is no new runtime concept: ``say()`` compiles
into the response-part list that the engine already renders, with each part carrying
the per-part ``condition`` the engine already evaluates. The only thing the engine
learns is how to read a ``capability`` leaf in a condition. So:

    say("Long prose.", brief="Short.", card=card(title="T"))
        ↓
    field        : "Long prose."                        # floor — unchanged, always valid
    field_response: [
        {"type": "text",    "text": "Short.",     "condition": {"capability": "brevity", "eq":  "tight"}},
        {"type": "text",    "text": "Long prose.","condition": {"capability": "brevity", "neq": "tight"}},
        {"type": "payload", "data": {...},        "condition": {"capability": "payloads"}},
    ]

The floor string is retained on the base field for two reasons: an engine that
predates this feature still says something correct, and a surface that resolves to
nothing renderable still has a sentence to fall back on.

Naming follows the pattern the framework already established for tasks —
``then_say`` (string) pairs with ``then_response`` (parts) pairs with
``channel_then_response`` (explicit per-channel override). ``say()`` generalizes that
triple to every text field rather than inventing a fourth spelling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Union

# The capability predicates say() generates. Kept here (not inlined) so the
# vocabulary is greppable and so a change lands in one place.
_TIGHT = {"capability": "brevity", "eq": "tight"}
_NOT_TIGHT = {"capability": "brevity", "neq": "tight"}
_PAYLOADS = {"capability": "payloads"}

_BUTTON_KINDS = ("event", "hyperLink", "deepLink", "doc", "cms")


# ── Structured content ───────────────────────────────────────────────────────


def action(text: str, event: str, *, display: Optional[str] = None) -> dict[str, Any]:
  """A button that fires a CES event back into the conversation.

  The event name is what the agent receives; ``display`` is the optional human
  label echoed into the transcript when the user taps it.
  """
  if not text:
    raise ValueError("action(): text is required")
  if not event:
    raise ValueError(f"action({text!r}): event name is required — a button that "
                     "fires nothing is a label, not an action")
  btn: dict[str, Any] = {"type": "button", "buttonType": "event", "text": text,
                         "event": {"name": event}}
  if display:
    btn["event"]["display"] = display
  return btn


def link(text: str, url: str, *, kind: str = "hyperLink") -> dict[str, Any]:
  """A button that opens a URL.

  Only rendered on a surface with the ``links`` capability. On voice the card it
  belongs to is dropped wholesale, so the URL is never spoken — which is the entire
  reason ``links`` is a capability rather than a style preference.
  """
  if kind not in _BUTTON_KINDS:
    raise ValueError(f"link(): kind must be one of {_BUTTON_KINDS}, got {kind!r}")
  if not url:
    raise ValueError(f"link({text!r}): url is required")
  return {"type": "button", "buttonType": kind, "text": text, "link": url}


def card(
    *,
    title: Optional[str] = None,
    body: Optional[str] = None,
    subtitle: Optional[str] = None,
    actions: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
  """Structured content for a surface that can render it.

  Emits the wire shape the CXAS clients already classify (see the simulator's
  ``classifyPayload``): an ``info`` rich-content item when there are no buttons, and
  a ``scenarios`` block when there are — buttons only exist in the scenario family,
  so a card with actions has to be one.

  Those two shapes do not carry the same fields, and ``subtitle`` is the casualty:
  the scenario family has a name and one text block, and nowhere to put it. Passing
  ``subtitle`` alongside ``actions`` is therefore rejected rather than quietly
  dropped — fold it into ``body``.

  ``{slot}`` placeholders in ``title``/``body``/``subtitle`` are interpolated by the
  same engine substitution that handles them in plain text, so a card body can quote
  a backend message exactly as a spoken line would.
  """
  if not any((title, body, subtitle, actions)):
    raise ValueError("card(): give at least one of title/body/subtitle/actions")
  for a in actions or ():
    if not isinstance(a, dict) or a.get("type") != "button":
      raise TypeError(
          "card(actions=): expected action()/link() buttons, got "
          f"{a!r} — build them with flows.action() or flows.link()")
  if actions and subtitle:
    raise ValueError(
        "card(): a card with actions has no subtitle. Buttons only exist in the"
        " scenario wire shape, which carries a title and ONE text block — a"
        " subtitle passed here would either be dropped (when body is also given)"
        " or silently promoted to the body (when it is not). Fold it into body.")

  if actions:
    responses: list[dict[str, Any]] = []
    if body:
      responses.append({"type": "text", "text": body})
    responses.extend(actions)
    return {"scenarios": [{"name": title or "StaticResponse",
                           "responses": responses}]}

  item: dict[str, Any] = {"type": "info"}
  if title:
    item["title"] = title
  if subtitle:
    item["subtitle"] = subtitle
  if body:
    item["text"] = body
  return {"richContent": [[item]]}


def chips(
    options: Optional[list[str]] = None,
    *,
    options_from: Optional[str] = None,
    event_name: Optional[str] = None,
) -> dict[str, Any]:
  """Tappable quick-replies. Either literal ``options`` or a slot to read them from.

  ``options_from`` names a slot holding a list, so the choices can come from a
  backend result rather than being hard-coded — which is what makes chips usable for
  things like "here are your available appointment times".
  """
  if bool(options) == bool(options_from):
    raise ValueError(
        "chips(): pass options OR options_from, not both and not neither")
  part: dict[str, Any] = {"type": "chips"}
  if options:
    part["options"] = [{"text": o} for o in options]
  if options_from:
    part["options_from"] = options_from
  if event_name:
    part["event_name"] = event_name
  return part


# ── The polymorphic text object ──────────────────────────────────────────────


@dataclass(frozen=True)
class Say:
  """One message, and how it should look on surfaces that can do more or less.

  Built by :func:`say`; authors should not construct this directly.
  """

  text: str = ""
  brief: Optional[str] = None
  card: Optional[dict[str, Any]] = None
  chips: Optional[dict[str, Any]] = None

  def floor(self) -> str:
    """The plain-string value for the base field — what every surface can render."""
    return self.text

  def text_variants(self) -> list[dict[str, Any]]:
    """Conditional TEXT parts — the per-surface wording of the message itself.

    These REPLACE the floor on a surface whose condition matches. They lower to a
    `<field>_variants` key, which is deliberately NOT one of the framework's
    `*_response` keys: those APPEND to what the model says, and appending a second
    phrasing of the same question would make the caller hear it twice.

    Both branches are emitted explicitly rather than letting the long form fall
    through to the floor. The floor is a fallback for surfaces this resolution
    never reached at all, and conflating "no variant matched" with "use the long
    one" is how a brief-only message ends up spoken on every surface.
    """
    if not self.brief or self.brief == self.text:
      return []
    return [{"type": "text", "text": self.brief, "condition": dict(_TIGHT)},
            {"type": "text", "text": self.text, "condition": dict(_NOT_TIGHT)}]

  def payload_parts(self) -> list[dict[str, Any]]:
    """Structured parts — the content that ACCOMPANIES the message.

    These lower to the framework's existing `response` / `then_response` fields
    and keep their existing append semantics, because a card genuinely is
    additional to what was said rather than a restatement of it.
    """
    out: list[dict[str, Any]] = []
    if self.card:
      out.append({"type": "payload", "data": self.card,
                  "condition": dict(_PAYLOADS)})
    if self.chips:
      part = dict(self.chips)
      part["condition"] = dict(_PAYLOADS)
      out.append(part)
    return out

  def is_polymorphic(self) -> bool:
    """True when this actually differs by surface (i.e. lowering is worthwhile)."""
    return bool(self.brief or self.card or self.chips)


def say(
    text: str,
    *,
    brief: Optional[str] = None,
    card: Optional[dict[str, Any]] = None,
    chips: Optional[dict[str, Any]] = None,
) -> Say:
  """Author one message with optional per-surface projections.

  Args:
    text: The message as written — the floor, and the only required argument. Every
      surface can render it, so a surface that understands nothing else still gets
      a correct sentence.
    brief: The tight-brevity form, used on spoken surfaces. Omit it and voice simply
      hears ``text``; there is no penalty for not writing one.
    card: Structured content from :func:`card`, rendered only where ``payloads``.
    chips: Quick-replies from :func:`chips`, rendered only where ``payloads``.

  ``text`` is mandatory on purpose. Allowing ``say(brief=...)`` alone reads as
  "only say this on voice", but there is nowhere for the floor to come from, so the
  brief form leaks onto every other surface — a hidden mode that does the opposite
  of what it looks like. A line that should exist on only some surfaces belongs in a
  response part with an explicit ``condition``.
  """
  if not text:
    raise ValueError(
        "say(): text is required — it is the floor every surface can render."
        " Pass brief= for the spoken form, and use a response part with a"
        " condition for a line that should exist on only some surfaces.")
  if card is not None and not isinstance(card, dict):
    raise TypeError("say(card=): expected flows.card(...), got "
                    f"{type(card).__name__}")
  if chips is not None and (not isinstance(chips, dict)
                            or chips.get("type") != "chips"):
    raise TypeError("say(chips=): expected flows.chips(...)")
  return Say(text=text, brief=brief, card=card, chips=chips)


# ── Lowering ─────────────────────────────────────────────────────────────────

TextLike = Union[str, Say, None]


def variants_of(value: TextLike) -> Optional[list[dict[str, Any]]]:
  """Per-surface wording parts, or None. See :meth:`Say.text_variants`."""
  if isinstance(value, Say):
    return value.text_variants() or None
  return None


def payloads_of(value: TextLike) -> Optional[list[dict[str, Any]]]:
  """Accompanying structured parts, or None. See :meth:`Say.payload_parts`.

  A plain string returns None, so a config authored without ``say()`` emits
  exactly the bytes it always did.
  """
  if isinstance(value, Say):
    return value.payload_parts() or None
  return None


def floor_of(value: TextLike) -> Optional[str]:
  """The plain-string form of a str/Say, for callers that need text before lowering.

  ``user_slot`` needs this: it derives its default reprompts from the ask
  ("Sorry, I didn't catch that. {ask}"), and those have to be strings.
  """
  if value is None:
    return None
  if isinstance(value, str):
    return value
  if isinstance(value, Say):
    return value.floor()
  # An ask LADDER floors to its first rung: that is the wording a caller hears first,
  # so it is the one the derived reprompts should quote back.
  if isinstance(value, (list, tuple)):
    return floor_of(value[0]) if value else None
  raise TypeError(
      f"expected str, list, or flows.say(...), got {type(value).__name__}")
