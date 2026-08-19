"""Surfaces — the capability model behind polymorphic (voice + chat) agents.

One agent definition, many delivery surfaces. The author writes a single semantic
intent; the surface decides how it is rendered. Nothing in an agent should ever say
``if channel == "voice"``.

The design rests on one idea: **branch on capabilities, not on identity.** A surface
declares what it can DO — render structure, carry a link, offer eight choices — and
the framework projects each authored message onto it. This is the CSS media-query
lesson: ``@media (max-width: 600px)`` ages well, ``if (isIphone)`` does not. When a
new surface appears (SMS, RCS, an in-car head unit) it declares its capabilities and
every existing agent behaves correctly with no edit.

The capability set is deliberately small and closed. Every capability is a
forever-commitment: it has to be answerable for every surface that will ever exist,
and every one of them multiplies the states an author has to reason about.

    payloads     can render structured content (cards, buttons, chips)
    brevity      "tight" (spoken) | "normal" (read)
    links        a URL is useful rather than unspeakable
    filler       a spoken "one moment" is the right way to mask latency
    keypad       DTMF is available
    max_options  how many choices can be offered at once

``max_options`` earns its place because cardinality is a distinct axis, not a wording
one: you can show a customer eight appointment slots but you cannot read eight aloud,
and no amount of shorter phrasing fixes that.

This module is the BUILD-TIME half. The engine carries its own copy of the same
defaults (it is rendered into the deployed app as a standalone file and cannot import
`flows`), and an app may override or add surfaces via ``App(surfaces=[...])``, which
emits a ``surfaces`` block into the config. Keep the two tables in step — the engine
copy is the one that runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Capability names, in the order they are emitted. Anything not in this set is
# rejected at author time rather than silently ignored — a typo'd capability that
# reads as "absent" would flip a surface into the opposite behavior.
CAPABILITIES = ("payloads", "brevity", "links", "filler", "keypad", "max_options")

BREVITY_LEVELS = ("tight", "normal")


@dataclass(frozen=True)
class Surface:
  """One delivery surface and what it is capable of.

  Defaults describe the most permissive surface (a rich text chat), so a partially
  specified custom surface degrades toward "can do things" rather than accidentally
  declaring itself mute. Voice, the restrictive one, is spelled out in full below.
  """

  name: str
  payloads: bool = True
  brevity: str = "normal"
  links: bool = True
  filler: bool = False
  keypad: bool = False
  max_options: int = 8
  # Free-form channel names that resolve to this surface, beyond `name` itself.
  # Lets a deployment speak CES's vocabulary ("TWILIO", "WEB_UI", "MOBILE") without
  # the agent ever learning those words.
  aliases: tuple[str, ...] = ()

  def __post_init__(self) -> None:
    if self.brevity not in BREVITY_LEVELS:
      raise ValueError(
          f"Surface({self.name!r}): brevity must be one of {BREVITY_LEVELS},"
          f" got {self.brevity!r}")
    if self.max_options < 1:
      raise ValueError(
          f"Surface({self.name!r}): max_options must be >= 1, got"
          f" {self.max_options}")
    if not self.name:
      raise ValueError("Surface(): name is required")

  def capability(self, key: str) -> Any:
    if key not in CAPABILITIES:
      raise KeyError(
          f"unknown capability {key!r}; expected one of {CAPABILITIES}")
    return getattr(self, key)

  def to_config(self) -> dict[str, Any]:
    """The wire form the engine reads. Aliases are emitted only when present."""
    out: dict[str, Any] = {k: getattr(self, k) for k in CAPABILITIES}
    if self.aliases:
      out["aliases"] = list(self.aliases)
    return out


# ── The built-ins ────────────────────────────────────────────────────────────
#
# Aliases map CES's own channel vocabulary onto these two. `ChannelProfile.channel_type`
# uses GOOGLE_TELEPHONY_PLATFORM / TWILIO / FIVE9 / CONTACT_CENTER_AS_A_SERVICE for
# telephony and WEB_UI / API for text, so a deployment that seeds any of those names
# resolves correctly without the agent knowing they exist.

VOICE = Surface(
    name="voice",
    payloads=False,
    brevity="tight",
    links=False,
    filler=True,
    keypad=True,
    max_options=3,
    aliases=("telephony", "phone", "audio", "GOOGLE_TELEPHONY_PLATFORM",
             "TWILIO", "FIVE9", "CONTACT_CENTER_AS_A_SERVICE",
             "CONTACT_CENTER_INTEGRATION"),
)

CHAT = Surface(
    name="chat",
    payloads=True,
    brevity="normal",
    links=True,
    filler=False,
    keypad=False,
    max_options=8,
    aliases=("text", "web", "webchat", "messenger", "WEB_UI", "API", "MOBILE"),
)

BUILTIN_SURFACES = (VOICE, CHAT)

# The surface an unrecognized (or absent) channel resolves to.
#
# Deliberately VOICE, and the reasoning is worth keeping: degrade toward voice,
# because voice failures are unrecoverable and chat failures are merely plain.
# Guessing chat while actually on a phone call means spoken URLs, three-hundred
# character monologues and payloads nobody can hear. Guessing voice while actually
# in a chat window means short text and no cards — degraded, never broken.
DEFAULT_SURFACE = VOICE.name


def resolve(
    channel: str,
    surfaces: Optional[dict[str, dict[str, Any]]] = None,
    default: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
  """Map a channel string to ``(surface_name, capabilities)``.

  Matching is case-insensitive on the surface name and on every alias, so
  ``"VOICE"``, ``"voice"`` and ``"GOOGLE_TELEPHONY_PLATFORM"`` all land on the same
  surface. An unrecognized channel falls back rather than silently matching nothing —
  which is the bug in the status quo, where Slot Studio sends the literal channel
  ``"base"``, matches no override key, and quietly renders the wrong thing.

  Args:
    channel: The inbound channel string; may be empty.
    surfaces: Optional per-app surface table (config ``surfaces`` block), which
      overrides and extends the built-ins.
    default: Optional per-app default surface name.

  Returns:
    The resolved surface name and its capability dict.
  """
  table: dict[str, dict[str, Any]] = {
      s.name: s.to_config() for s in BUILTIN_SURFACES
  }
  alias_of: dict[str, str] = {}
  for s in BUILTIN_SURFACES:
    for a in s.aliases:
      alias_of[a.lower()] = s.name

  for name, caps in (surfaces or {}).items():
    merged = dict(table.get(name) or {k: getattr(CHAT, k) for k in CAPABILITIES})
    merged.update({k: v for k, v in (caps or {}).items() if k != "aliases"})
    table[name] = merged
    for a in (caps or {}).get("aliases") or ():
      alias_of[str(a).lower()] = name

  key = (channel or "").strip().lower()
  resolved = None
  if key:
    if key in {n.lower() for n in table}:
      resolved = next(n for n in table if n.lower() == key)
    elif key in alias_of and alias_of[key] in table:
      resolved = alias_of[key]

  if resolved is None:
    resolved = default or DEFAULT_SURFACE
    if resolved not in table:
      resolved = DEFAULT_SURFACE

  return resolved, table[resolved]


def surfaces_to_config(items: Any) -> dict[str, dict[str, Any]]:
  """Normalize ``App(surfaces=...)`` into the config ``surfaces`` block.

  Accepts Surface objects, or raw dicts for callers coming from YAML/JSON.
  """
  out: dict[str, dict[str, Any]] = {}
  for item in items or ():
    if isinstance(item, Surface):
      out[item.name] = item.to_config()
    elif isinstance(item, dict):
      name = item.get("name")
      if not name:
        raise ValueError("surfaces: a dict entry needs a 'name'")
      caps = {k: v for k, v in item.items() if k != "name"}
      unknown = set(caps) - set(CAPABILITIES) - {"aliases"}
      if unknown:
        raise ValueError(
            f"surfaces[{name!r}]: unknown capability keys {sorted(unknown)};"
            f" expected {CAPABILITIES}")
      out[str(name)] = caps
    else:
      raise TypeError(
          f"surfaces: expected Surface or dict, got {type(item).__name__}")
  return out
