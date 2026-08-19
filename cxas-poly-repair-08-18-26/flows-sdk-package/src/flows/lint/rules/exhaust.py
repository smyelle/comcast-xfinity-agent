"""FLW005 — an `on_exhaust.then` that names a tool nothing will register.

The engine turns `then` into a function call verbatim: a bare string becomes
`{"name": <string>}`, a dict becomes `{"name": then["tool"]}`. Nothing checks that the
name resolves, and the platform's behavior when it does not is the worst kind — probe
`69-ghost-leg-hang` in the conformance corpus records that a leg naming no registered
tool "takes down the ENTIRE tool invocation — no return, no state write and no readable
error."

So this deploys clean and then silently kills the turn it was supposed to rescue, which
is precisely the turn a caller reaches after already failing several times.

The rule exists because the framework's own examples got this wrong: `"then": "escalate"`
appeared in a `user_slot` docstring, in the docs and in three shipped examples, and there
has never been an `escalate` tool. `validate`, `lint`, `emit` and `check` all passed it.
"""

from __future__ import annotations

from typing import Iterable, Iterator

from ..context import LintContext
from ..models import Category, Finding, Location
from ..registry import Rule, rule
from ...config.models import NodeAnchor


def _then_target(exhaust: object) -> str | None:
  """The tool name an `on_exhaust.then` resolves to, mirroring the engine exactly."""
  if not isinstance(exhaust, dict):
    return None
  then = exhaust.get("then")
  if isinstance(then, str) and then:
    return then
  if isinstance(then, dict):
    tool = then.get("tool")
    return tool if isinstance(tool, str) and tool else None
  return None


def _exhaust_sites(ctx: LintContext, cid: str) -> Iterator[tuple[str, str, str]]:
  """`(target, node, json_path)` for every on_exhaust carrying a `then`.

  Three places declare one, and all three reach the same engine resolver: a slot's
  `validation` (the no-match ladder), a task's `on_failure` (the retry ladder), and the
  flow-level `no_input` (the silence ladder).
  """
  config = ctx.configs.get(cid) or {}

  for slot in ctx.slots(cid):
    name = slot.get("name", "?")
    target = _then_target((slot.get("validation") or {}).get("on_exhaust"))
    if target:
      yield target, name, f"slots/{name}/validation/on_exhaust/then"

  for task in ctx.tasks(cid):
    name = task.get("name", "?")
    target = _then_target((task.get("on_failure") or {}).get("on_exhaust"))
    if target:
      yield target, name, f"tasks/{name}/on_failure/on_exhaust/then"

  target = _then_target((config.get("no_input") or {}).get("on_exhaust"))
  if target:
    yield target, "no_input", "no_input/on_exhaust/then"


@rule(
    code="FLW005",
    category=Category.WIRING,
    severity="error",
    title="on_exhaust.then names a tool that will not exist at run time",
    docs="FLW005",
)
class ExhaustThenUnresolved(Rule):
  """An exhaust disposition that dispatches a name nothing registers.

  Error, not a warning: the caller is already several failures deep when this fires,
  and the platform's response to an unresolvable name is to drop the whole invocation
  silently. There is no partial success to weigh against.
  """

  #: Platform tools the EMITTER adds to every app, so they resolve at run time even
  #: though nothing in the config declares them (`emit/scaffold.py` adds `end_session`
  #: to the tool list of every agent it writes).
  #:
  #: `config.tool_refs._FRAMEWORK_THEN_ACTIONS` groups `end_session` and `escalate`
  #: together as "framework dispositions", and that grouping is the root of this whole
  #: defect: `end_session` is registered by the emitter and `escalate` is registered by
  #: nobody. One resolves and one silently kills the turn. They are not the same thing.
  EMITTED_PLATFORM_TOOLS = frozenset({"end_session"})

  def check(self, ctx: LintContext) -> Iterable[Finding]:
    known = (set(ctx.available) | set(ctx.reserved_tool_names()) | set(ctx.bodies)
             | self.EMITTED_PLATFORM_TOOLS)
    for cid in sorted(ctx.configs):
      for target, node, path in _exhaust_sites(ctx, cid):
        if target in known:
          continue
        yield self.finding(
            message=(
                f"on_exhaust.then dispatches {target!r}, which is not a declared tool, a "
                f"framework tool, or a tool with a body. The engine calls it by name "
                f"anyway, and the platform drops the entire invocation with no error "
                f"(see conformance probe 69-ghost-leg-hang). Name a real tool — "
                f"'transfer_to_human' is the usual intent — or use "
                f"{{'tool': '<name>'}} to be explicit."),
            location=Location(config_id=cid, node=node, json_path=path),
            anchor=NodeAnchor(kind="field", ref=node, field="on_exhaust"),
            rationale=("An unresolvable exhaust target deploys clean and then kills the "
                       "one turn that existed to rescue the caller."),
            fix_id="name_a_real_exhaust_tool",
        )
