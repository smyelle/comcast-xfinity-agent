"""FLC — conversation design (caller-experience best practices).

These are `info` by default: the agent works without them, but a best-in-class
voice agent has them. They never block a build.
"""

from __future__ import annotations

from typing import Any, Iterable, Iterator

from ..context import LintContext, relative_field
from ..models import Category, Finding, Location
from ..registry import Rule, rule
from ...config.models import NodeAnchor


def _is_router(ctx: LintContext, cid: str) -> bool:
  """A synthesized host/router config has no caller-asked slots to design for."""
  return cid == ctx.host_cid or bool(ctx.configs[cid].get("router"))


@rule(
    code="FLC101",
    category=Category.CONVERSATION,
    severity="info",
    title="asked user slots but no no-input (silence) ladder",
    docs="FLC101",
)
class NoSilenceLadder(Rule):
  """A voice flow that asks the caller questions but has no flow-level `no_input`
  reprompt ladder degrades on silence (the caller says nothing and the agent just
  waits or repeats). Add `no_input.reprompts` + a terminal `on_exhaust`.
  """

  def check(self, ctx: LintContext) -> Iterable[Finding]:
    for cid in ctx.config_ids():
      if _is_router(ctx, cid) or not ctx.user_askable_slots(cid):
        continue
      ni = ctx.configs[cid].get("no_input")
      if isinstance(ni, dict) and ni.get("reprompts"):
        continue
      yield self.finding(
          message=(
              "This flow asks the caller questions but has no no_input (silence) "
              "ladder, so it degrades when the caller stays silent. Add "
              "no_input.reprompts (one line per silent turn) and an on_exhaust "
              "disposition (e.g. transfer_to_human)."),
          location=Location(config_id=cid, node="no_input", json_path="no_input"),
          anchor=NodeAnchor(kind="field", ref="no_input", field="reprompts"),
          rationale="On voice, silence is a first-class turn; without a ladder the "
                    "caller gets stuck.",
          fix_id="add_no_input_ladder",
      )


@rule(
    code="FLC121",
    category=Category.CONVERSATION,
    severity="info",
    title="async wait with no spoken cue",
    docs="FLC121",
)
class SilentAsyncWait(Rule):
  """A task whose tool is asynchronous (`awaits`) but whose wait says nothing —
  no `say` on the pending turn and no `while_waiting` lines — leaves the caller in
  dead air while the backend runs. Give the wait a spoken cue.
  """

  def check(self, ctx: LintContext) -> Iterable[Finding]:
    for cid in ctx.config_ids():
      for i, task in enumerate(ctx.tasks(cid)):
        aw = task.get("awaits")
        if not isinstance(aw, dict):
          continue
        if aw.get("say") or aw.get("while_waiting"):
          continue
        tname = task.get("name", f"<task {i}>")
        yield self.finding(
            message=(
                f"Task {tname!r} waits on an asynchronous tool but says nothing while "
                "it runs (no awaits.say, no awaits.while_waiting), so the caller hears "
                "dead air. Add awaits.say (spoken when the wait starts) and/or "
                "awaits.while_waiting (one line per idle turn)."),
            location=Location(config_id=cid, node=tname,
                              json_path=f"tasks[{i}].awaits"),
            anchor=NodeAnchor(kind="task", ref=tname, field="awaits.say"),
            rationale="A silent multi-turn wait reads as a dropped call.",
            fix_id="add_await_say",
        )


def _iter_response_lists(ctx: LintContext, cid: str) -> Iterator[tuple]:
  """Yield `(node_kind, node_name, json_path, parts_list)` for every response list."""
  cfg = ctx.configs[cid]
  for i, slot in enumerate(ctx.slots(cid)):
    if isinstance(slot.get("response"), list):
      yield ("slot", slot.get("name", f"<slot {i}>"), f"slots[{i}].response",
             slot["response"])
  for i, task in enumerate(ctx.tasks(cid)):
    if isinstance(task.get("then_response"), list):
      yield ("task", task.get("name", f"<task {i}>"), f"tasks[{i}].then_response",
             task["then_response"])
  for block in ("cancel", "escalate"):
    b = cfg.get(block)
    if isinstance(b, dict) and isinstance(b.get("response"), list):
      yield ("field", block, f"{block}.response", b["response"])


@rule(
    code="FLC130",
    category=Category.CONVERSATION,
    severity="info",
    title="transfer without disclaimer or context",
    docs="FLC130",
)
class ColdTransfer(Rule):
  """A transfer response part with no `disclaimer` is a cold hand-off (the caller
  is not told they are being transferred); with no `context` the receiving agent
  loses the caller's state. Add both.
  """

  def check(self, ctx: LintContext) -> Iterable[Finding]:
    for cid in ctx.config_ids():
      for node_kind, node, base, parts in _iter_response_lists(ctx, cid):
        for j, part in enumerate(parts):
          if not isinstance(part, dict) or part.get("type") != "transfer":
            continue
          missing = [k for k in ("disclaimer", "context") if not part.get(k)]
          if not missing:
            continue
          yield self.finding(
              message=(
                  f"Transfer in {node_kind} {node!r} is missing {', '.join(missing)}: "
                  "no disclaimer is a cold hand-off (the caller is not told they are "
                  "being transferred); no context loses the caller's state for the "
                  "receiving agent. Add disclaimer= and context=."),
              location=Location(config_id=cid, node=node, json_path=f"{base}[{j}]"),
              anchor=NodeAnchor(
                  kind=node_kind if node_kind in ("slot", "task", "field") else "field",
                  ref=node, field=relative_field(f"{base}[{j}]")),
              rationale="A warm transfer tells the caller what is happening and hands "
                        "the next agent the context.",
              fix_id="add_transfer_disclaimer",
          )
