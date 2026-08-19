"""Google Search grounding — answer from the web instead of from the model's priors.

CES models Google Search as a `googleSearchTool`, a `tool_type` oneof sibling of
`pythonFunction`. Like an A2A remote agent it is a BODY-LESS tool — a
`tools/<name>/<name>.json` and nothing else — because the platform performs the search.

    hours = flows.search_tool(
        "support_search",
        "Answer questions about store hours, closures and service areas.",
        preferred_domains=["example-shipping.com"],
        voice_prompt="Answer in one or two short sentences. Never read out a URL.",
    )
    app = flows.App(root_flow=f, search_tools=[hours], ...)

That declaration emits the resource and scopes it onto the agent, so the model can search
whenever it judges the caller needs it and summarise what it finds.

To CAPTURE a result instead of only speaking it, fire the tool from an ordinary task —
pass the `SearchTool` itself as the tool and the rest is filled in for you:

    f.task("lookup", hours, ["question"], "findings", terminal=True,
           then_directive="Answer the caller's question from the search results.")

`then_directive` rather than `then_say` is the whole trick: it hands the result to the
model to compose from, so the caller hears an answer instead of pasted snippets, while
`outputs` still lands the raw `snippets` in the slot. A `then_say` over search results
reads like a search-results page, because a `then_say` is spoken verbatim.

Everything below is what live probes established (`ces-probes` 24-28); none of it is
inferable from the protos, and two of the four findings contradict them.

**The runtime treats search as an ordinary function tool.** The model calls it with a
single `query` argument and receives `{search_query, snippets[{url, title, text}],
instructions}` — not an opaque grounding side-channel. `instructions` is the platform's
own summarisation prompt, which `text_prompt` / `voice_prompt` replace.

**The engine can fire it.** A `before_model`-injected `function_call` dispatches for a
managed tool exactly as it does for a python one, and the payload comes back intact, which
is what lets a `task()` fire a search at all.

**`after_tool` sees the payload**, so intake can map it — but the flat-map trap applies.
`slot_intake._intake_executor` reads `success = bool(response_data.get(success_check))` and
maps `outputs` by FLAT top-level key. A search response carries no `success` key, so a task
naively pointed at one looks failed on every fire. `snippets` IS top-level and is empty
exactly when the search found nothing, so it doubles as the success check — `task()` sets
that for you when it is handed a `SearchTool`, and `build` errors on a hand-written task
that names the tool as a bare string and gets it wrong.

**Visibility is per-AGENT, never per-turn.** `llm_request.config.hide_tool()` does not gate
a managed tool: it suppresses execution, but the turn then dies with
`PARSER_ERROR_TYPE_UNEXPECTED_TOKEN` ("Hmm, I'm having trouble with that") on any question
the model wanted to search — 4 of 5 in probe 26, against a clean control. Hiding is safe
for python tools (probes 02/07) and unsafe for managed ones, so there is deliberately no
`available=`/`when=` knob here. Scope search to the agent that should have it: pass
`search_tools=` to one `Agent` rather than to the `App`, or give the FAQ its own specialist.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional, Sequence

# The search tool's own parameter and response keys. The platform defines these, not us
# (probe 24: `args {query}` -> `{search_query, snippets, instructions}`).
SEARCH_QUERY_PARAM = "query"
SEARCH_SNIPPETS_KEY = "snippets"
SEARCH_QUERY_ECHO_KEY = "search_query"
SEARCH_RESPONSE_KEYS = (SEARCH_SNIPPETS_KEY, SEARCH_QUERY_ECHO_KEY, "instructions")

# Platform caps (google.cloud.ces_v1beta.types.GoogleSearchTool).
MAX_CONTEXT_URLS = 20
MAX_PREFERRED_DOMAINS = 20
MAX_EXCLUDE_DOMAINS = 2000

# A tool name reaches CES as a `displayName` and is referenced from instructions as
# `{@TOOL: name}`; dots/spaces/slashes break that reference.
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _require(value: Any, what: str) -> str:
  text = str(value or "").strip()
  if not text:
    raise ValueError(f"{what} is required")
  return text


def _capped(values: Optional[Sequence[str]], limit: int, what: str) -> tuple[str, ...]:
  """A de-duplicated tuple of non-empty strings, rejected if over the platform cap."""
  out: list[str] = []
  for value in values or ():
    text = str(value or "").strip()
    if text and text not in out:
      out.append(text)
  if len(out) > limit:
    raise ValueError(
        f"search_tool(): {what} accepts at most {limit} entries, got {len(out)}"
    )
  return tuple(out)


@dataclass(frozen=True)
class SearchTool:
  """A Google Search grounding tool, emitted as one body-less resource."""

  name: str
  description: str
  context_urls: tuple[str, ...] = ()
  preferred_domains: tuple[str, ...] = ()
  exclude_domains: tuple[str, ...] = ()
  text_prompt: Optional[str] = None
  voice_prompt: Optional[str] = None

  def tool_payload(self) -> dict[str, Any]:
    """The `googleSearchTool` block of the emitted tool resource.

    The emitter wraps this with the resource-level `name` (a fresh UUID) and
    `displayName`, exactly as it does for a python tool.
    """
    payload: dict[str, Any] = {"name": self.name, "description": self.description}
    if self.context_urls:
      payload["contextUrls"] = list(self.context_urls)
    if self.preferred_domains:
      payload["preferredDomains"] = list(self.preferred_domains)
    if self.exclude_domains:
      payload["excludeDomains"] = list(self.exclude_domains)
    prompt: dict[str, str] = {}
    if self.text_prompt:
      prompt["textPrompt"] = self.text_prompt
    if self.voice_prompt:
      prompt["voicePrompt"] = self.voice_prompt
    if prompt:
      payload["promptConfig"] = prompt
    return payload


def search_tool(
    name: str,
    description: str,
    *,
    context_urls: Optional[Sequence[str]] = None,
    preferred_domains: Optional[Sequence[str]] = None,
    exclude_domains: Optional[Sequence[str]] = None,
    text_prompt: Optional[str] = None,
    voice_prompt: Optional[str] = None,
) -> SearchTool:
  """Declare a Google Search grounding tool.

  Args:
    name: Tool name the model calls and the instruction references as `{@TOOL: name}`.
    description: What this search is for. The model reads it to decide when to search,
      so scope it ("store hours and closures") rather than leaving it generic ("search").
    context_urls: Pages fetched directly for grounding. Max 20.
    preferred_domains: Restrict results to these domains. Max 20.
    exclude_domains: Drop results from these domains. Max 2000.
    text_prompt: Replaces the platform's default summarisation prompt on chat channels.
    voice_prompt: The same for voice. Worth setting: the default asks for markdown lists
      and permits URLs, neither of which can be spoken.

  Returns:
    A `SearchTool` to pass to `App(search_tools=[...])`, `Agent(search_tools=[...])`, or
    straight to `Flow.task(...)` as the tool.
  """
  tool_name = _require(name, "search_tool(): name")
  if not _NAME_RE.match(tool_name):
    raise ValueError(
        f"search_tool(): name {tool_name!r} must be letters, digits, underscores or "
        "dashes — it is referenced from instructions as {@TOOL: name}"
    )
  return SearchTool(
      name=tool_name,
      description=_require(description, "search_tool(): description"),
      context_urls=_capped(context_urls, MAX_CONTEXT_URLS, "context_urls"),
      preferred_domains=_capped(
          preferred_domains, MAX_PREFERRED_DOMAINS, "preferred_domains"),
      exclude_domains=_capped(exclude_domains, MAX_EXCLUDE_DOMAINS, "exclude_domains"),
      text_prompt=(text_prompt or None),
      voice_prompt=(voice_prompt or None),
  )
