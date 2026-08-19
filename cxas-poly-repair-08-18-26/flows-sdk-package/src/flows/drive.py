"""Drive a deployed CES app — seeded session, tool fakes on.

`flows.cujs` resolves a name to variables; this turns those variables into a live
session. The driver itself lives in `flows.live` and is resolved lazily inside the
functions, so `import flows` stays light and a core-only install still works. Pass
`session_factory` to substitute a host's own driver — any callable
`(app_name, initial_variable_state) -> session`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

DEFAULT_PROJECT = "ces-deployment-dev"
DEFAULT_LOCATION = "us"


@dataclass
class TurnResult:
  """One driven turn: what was said, what came back, which tools fired."""

  utterance: str
  text: str
  tool_calls: list[str]


def app_resource(app: str, *, project: str = DEFAULT_PROJECT,
                 location: str = DEFAULT_LOCATION) -> str:
  """Accept a bare app UUID or a full resource name; return the resource name."""
  if app.startswith("projects/"):
    return app
  return f"projects/{project}/locations/{location}/apps/{app}"


# Long enough that a doubled sentence still collapses, short enough that no real
# agent turn reaches it: without a floor "bye bye" halves to "bye".
_MIRROR_FLOOR = 24


def collapse_mirror(text: str) -> str:
  """Collapse the doubled agent text some apps emit (a transcript-mirror artifact)."""
  said = (text or "").strip()
  half = len(said) // 2
  if half >= _MIRROR_FLOOR and said[:half].strip() == said[half:].strip():
    return said[:half].strip()
  return said


def open_session(cuj, app: str, *, project: str = DEFAULT_PROJECT,
                 location: str = DEFAULT_LOCATION,
                 session_factory: Callable[..., Any] | None = None,
                 use_tool_fakes: bool = True, **session_kwargs):
  """A live session seeded with `cuj`'s variables.

  `cuj` may be a `CUJ`, a plain variable dict, or a CUJ name (resolved against a
  discovered `cujs.yaml`). `use_tool_fakes` is on by default because the per-tool
  fake configs are inert without it, and every CUJ depends on them.
  """
  variables = _variables_of(cuj)
  name = app_resource(app, project=project, location=location)

  if session_factory is None:
    session_factory = _default_session_factory()
  session = session_factory(app_name=name, initial_variable_state=variables,
                            **session_kwargs)

  if use_tool_fakes:
    _enable_tool_fakes(session)
  return session


def _enable_tool_fakes(session) -> None:
  """Default the transport's `use_tool_fakes` on, once, without clobbering a caller.

  ChatSession does not expose the flag, and without it the agent reaches past its
  fake tool configs to the real backends. `setdefault` (not a hard-coded True)
  keeps an explicit `run(use_tool_fakes=...)` legal, and the marker makes a second
  `open_session` over the same session a no-op instead of a double wrap — either
  one otherwise raises `TypeError: got multiple values for keyword argument`.
  """
  transport = session._sessions
  run = transport.run
  if getattr(run, "_flows_tool_fakes", False):
    return

  def run_with_fakes(**kw):
    kw.setdefault("use_tool_fakes", True)
    return run(**kw)

  run_with_fakes._flows_tool_fakes = True
  transport.run = run_with_fakes


def turn(app: str, *, session_id: str | None = None, text: str | None = None,
         dtmf: str | None = None, event: str | None = None,
         event_vars: dict | None = None, variables: dict | None = None,
         tool_responses: list | None = None, modality: str | None = None,
         use_tool_fakes: bool = True, project: str = DEFAULT_PROJECT,
         location: str = DEFAULT_LOCATION,
         session_factory: Callable[..., Any] | None = None) -> dict:
  """One turn against a deployed app, returned as plain data.

  The single-shot counterpart to `open_session`: pass `session_id` to continue a
  conversation that a previous call started, which is what makes this usable from a
  shell, where each invocation is its own process.

  Returns the structured turn — `agent_text`, `tool_calls`, `session_ended` and the
  rest — plus the `session_id` to hand to the next call.
  """
  name = app_resource(app, project=project, location=location)
  factory = session_factory or _default_session_factory()
  session = factory(app_name=name, session_id=session_id)

  record = session.send_input(
      text=text, dtmf=dtmf, event=event, event_vars=event_vars,
      variables=variables, tool_responses=tool_responses, modality=modality,
      use_tool_fakes=use_tool_fakes or None)

  return {
      "session_id": session.session_id,
      "app": name,
      "input": record.user_text,
      "agent_text": collapse_mirror(record.agent_text),
      "tool_calls": record.tool_calls,
      "tool_responses": record.tool_responses,
      "agent_transfer": record.agent_transfer,
      "session_ended": record.session_ended,
      "payloads": record.payloads,
      "filled_slots": session.get_state()["filled_slots"],
  }


def run_steps(cuj, app: str, steps: Iterable[str], *, session=None,
              **kwargs) -> list[TurnResult]:
  """Send each utterance in order; return what the agent said to each."""
  session = session or open_session(cuj, app, **kwargs)
  results = []
  for utterance in steps:
    if session.is_ended:
      results.append(TurnResult(utterance, "(the session has ended)", []))
      break
    turn = session.send(utterance)
    tools = [c.get("action") for c in (turn.tool_calls or [])]
    results.append(TurnResult(utterance, collapse_mirror(turn.agent_text), tools))
  return results


def chat(cuj, app: str, *, opening: str | None = None, say: Iterable[str] | None = None,
         **kwargs) -> int:
  """Drive a CUJ from the terminal: scripted with `say`, otherwise a REPL."""
  resolved = _resolve(cuj)          # None when a plain variable dict was passed
  session = open_session(cuj, app, **kwargs)

  if resolved is not None and resolved.description:
    print(f"\nCUJ: {resolved.name} — {resolved.description}")
  print(f"variables: {_variables_of(cuj)}\n")

  if say:
    for result in run_steps(cuj, app, say, session=session):
      print(f"you   > {result.utterance}")
      print(f"agent > {result.text}\n")
      if result.tool_calls:
        print(f"        [tools: {', '.join(result.tool_calls)}]\n")
    return 0

  pending = opening
  print("(Ctrl-D or 'quit' to exit)\n")
  while True:
    if pending:
      text, pending = pending, None
      print(f"you   > {text}")
    else:
      try:
        text = input("you   > ").strip()
      except EOFError:
        print()
        return 0
      if text.lower() in ("quit", "exit"):
        return 0
      if not text:
        continue
    if session.is_ended:
      print("agent > (the session has ended)")
      return 0
    turn = session.send(text)
    print(f"agent > {collapse_mirror(turn.agent_text)}\n")
    tools = [c.get("action") for c in (turn.tool_calls or [])]
    if tools:
      print(f"        [tools: {', '.join(tools)}]\n")


def _resolve(cuj):
  """A `CUJ` if we have or can find one, else None (a bare dict was passed)."""
  from .cujs import CUJ, load_cujs

  if isinstance(cuj, CUJ):
    return cuj
  if isinstance(cuj, str):
    return load_cujs()[cuj]
  return None


def _variables_of(cuj) -> dict[str, Any]:
  resolved = _resolve(cuj)
  return dict(resolved.variables) if resolved is not None else dict(cuj)


def _default_session_factory():
  """The in-package `ChatSession`, imported lazily.

  Lazy because `flows.live` reaches `cxas_scrapi` (and through it the heavy
  `google.cloud.ces_v1beta` stack plus ADC) on first CLIENT construction, which
  must not be paid by an offline authoring import. A missing `deploy` extra
  therefore surfaces in `flows.live.clients`, not here. Pass `session_factory=`
  to substitute a host's own driver — a service does that to apply a per-request
  endpoint override.
  """
  from .live.session import ChatSession

  return ChatSession
