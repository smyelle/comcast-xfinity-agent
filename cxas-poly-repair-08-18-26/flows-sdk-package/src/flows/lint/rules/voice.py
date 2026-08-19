"""FLV — voice & copy: spoken-text quality rules.

These are the only rules that assert on TEXT rather than the DAG, so they carry a
`needs_review` severity by default: a hyphen can be legitimate (a brand name), and
the linter must not train authors to ignore it. See DESIGN.md principle 8.
"""

from __future__ import annotations

import re
from typing import Iterable

from ..context import LintContext, relative_field
from ..models import Category, Finding, Location
from ..registry import Rule, rule
from ...authoring import autofill as _autofill
from ...config.models import NodeAnchor

# An em-dash or en-dash anywhere, or an ASCII hyphen wedged between two word
# characters ("door-tag", "5-digit"). A hyphen with spaces around it is a normal
# clause break in speech and is NOT flagged.
_EM_EN = re.compile(r"[–—]")
_COMPOUND_HYPHEN = re.compile(r"\w-\w")

# Spans that are NOT spoken as written — a hyphen inside them is not a TTS concern.
# A `{template}` is interpolated before TTS; URLs/emails are read specially or not
# aloud at all. Stripped before the hyphen check to avoid false positives.
_NON_SPOKEN = re.compile(r"\{[^}]*\}|https?://\S+|\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# A CES bracket audio tag: [whispers], [calm], [slow]. Deliberately narrow so it
# cannot swallow a `{template}` or an SSML `<tag>`, which are different things.
_AUDIO_TAG = re.compile(r"\[[a-zA-Z][a-zA-Z ]{1,20}\]")


def _spoken_only(text: str) -> str:
  return _NON_SPOKEN.sub(" ", text)


@rule(
    code="FLV001",
    category=Category.VOICE,
    severity="needs_review",
    title="dash in spoken copy can chop TTS audio",
    docs="FLV001",
)
class DashInSpokenCopy(Rule):
  """Flag em/en dashes and compound hyphens in caller-heard text.

  The engine itself follows this convention (its neutral fallback comment notes
  "no dash - dashes chop TTS"); nothing enforced it until now.
  """

  def check(self, ctx: LintContext) -> Iterable[Finding]:
    for cid in ctx.config_ids():
      for node_kind, node, json_path, raw_text in ctx.iter_spoken(cid):
        text = _spoken_only(raw_text)
        em = _EM_EN.search(text)
        comp = _COMPOUND_HYPHEN.search(text)
        if not em and not comp:
          continue
        if em:
          kind_desc = "an em/en dash"
          sample = text[max(0, em.start() - 12): em.end() + 12].strip()
          fix = ("Replace the dash with a comma or rephrase so the sentence reads "
                 "as continuous speech.")
        else:
          kind_desc = "a compound hyphen"
          sample = text[max(0, comp.start() - 8): comp.end() + 8].strip()
          fix = ("Replace the hyphen with a space or spell the words out "
                 "(e.g. 'door-tag' -> 'door tag', '5-digit' -> 'five digit').")
        yield self.finding(
            message=(
                f"Spoken text in {node_kind} {node!r} ({json_path}) contains "
                f"{kind_desc} (...{sample}...), which can chop the TTS audio. {fix} "
                "If the dash is intentional (e.g. a brand name), suppress with "
                "lint_ignore=['FLV001: <reason>']."),
            location=Location(config_id=cid, node=node, json_path=json_path),
            anchor=NodeAnchor(
                kind=node_kind if node_kind in ("slot", "task", "field") else "field",
                ref=node, field=relative_field(json_path)),
            rationale=("Dashes and compound hyphens make many TTS engines pause or "
                       "clip; spoken copy should read as continuous words."),
            fix_id="despace_dash",
        )


@rule(
    code="FLV002",
    category=Category.VOICE,
    severity="warning",
    title="audio tag in spoken copy behaves differently per model",
    docs="FLV002",
)
class AudioTagInSpokenCopy(Rule):
  """Flag a bracket audio tag (`[whispers]`) in caller-heard text.

  A tag is wrong on one of the two models whichever one you ship, and which one runs
  is not knowable here: `modelSettings` is preserved from the deploy target, so the
  authored `App.model` is a guess. Measured (ces-probes 84/87): honoured on
  `gemini-composite-v1`, and READ ALOUD as a word on `gemini-3.1-flash-live` — the
  caller hears "whispers".
  """

  def check(self, ctx: LintContext) -> Iterable[Finding]:
    for cid in ctx.config_ids():
      for item, part in ctx.iter_spoken_parts(cid):
        if (part or {}).get("partial"):
          continue  # FLV003 owns that case, and reports it as an error
        m = _AUDIO_TAG.search(_spoken_only(item.text))
        if not m:
          continue
        yield self.finding(
            message=(
                f"Spoken text in {item.node_kind} {item.node!r} ({item.json_path}) "
                f"contains the audio tag {m.group(0)!r}. On gemini-composite-v1 it is "
                "honoured; on gemini-3.1-flash-live the caller hears the word "
                "'whispers' read aloud. The model comes from the deploy target, not "
                "from this config, so a tag is a coin flip. Remove it, or suppress "
                "with lint_ignore=['FLV002: composite only'] if the target is pinned."),
            location=Location(config_id=cid, node=item.node,
                              json_path=item.json_path),
            anchor=NodeAnchor(
                kind=item.node_kind if item.node_kind in ("slot", "task", "field")
                else "field",
                ref=item.node, field=relative_field(item.json_path)),
            rationale=("Audio tags are model-specific: composite-v1 applies them, "
                       "flash-live speaks them as words (ces-probes 84, 87)."),
            fix_id="strip_audio_tag",
        )


@rule(
    code="FLV003",
    category=Category.VOICE,
    severity="error",
    title="audio tag in a partial part truncates the utterance",
    docs="FLV003",
)
class AudioTagInPartialPart(Rule):
  """Flag a bracket audio tag in a part marked `partial`.

  Errors rather than warns because the failure is not cosmetic and not
  model-dependent enough to argue with: measured on `gemini-composite-v1`
  (ces-probes 86), a tagged partial part reads the markup aloud AND truncates at
  ~1.9s against ~5s for every other shape, so the caller hears "left bracket whispers
  right bracket" and then half a sentence. `partial` is exactly the holding-line
  shape — a latency filler or an A4 prefix — so this cuts off the line whose whole
  job was to cover a wait.
  """

  def check(self, ctx: LintContext) -> Iterable[Finding]:
    for cid in ctx.config_ids():
      for item, part in ctx.iter_spoken_parts(cid):
        if not (part or {}).get("partial"):
          continue
        m = _AUDIO_TAG.search(_spoken_only(item.text))
        if not m:
          continue
        yield self.finding(
            message=(
                f"The partial part in {item.node_kind} {item.node!r} "
                f"({item.json_path}) contains the audio tag {m.group(0)!r}. A tagged "
                "partial part truncates at ~1.9s on gemini-composite-v1 and reads the "
                "markup aloud, so the caller hears the tag and half the line. Drop the "
                "tag, or drop `partial` from the part."),
            location=Location(config_id=cid, node=item.node,
                              json_path=item.json_path),
            anchor=NodeAnchor(
                kind=item.node_kind if item.node_kind in ("slot", "task", "field")
                else "field",
                ref=item.node, field=relative_field(item.json_path)),
            rationale=("A partial part carrying an audio tag is truncated and its "
                       "markup spoken (ces-probes 86). The engine strips the tag at "
                       "runtime as a backstop; this is the authoring-time fix."),
            fix_id="strip_audio_tag",
        )


@rule(
    code="FLV004",
    category=Category.VOICE,
    severity="needs_review",
    title="a wait this node could have covered is left uncovered",
    docs="FLV004",
)
class BlockedLatencyHoist(Rule):
  """Flag copy that opens with a hoistable line the latency pass was not allowed to move.

  Only fires on an app that turned `automatic_fillers` on, and only when a STRUCTURAL rule
  did the blocking. An author who wrote `automatic_fillers=False` made a decision and does
  not need it questioned every build — and that marker is stripped before the linter
  runs anyway, so a node with no nameable blocker is silently left alone.

  `needs_review` rather than `warning`: every one of these blockers exists for a reason,
  and several (verbatim copy, a flow-level pool) are usually the right call. The finding
  is here so the trade is visible, not so it gets "fixed".
  """

  def check(self, ctx: LintContext) -> Iterable[Finding]:
    enabled, extra_ack = _autofill.filler_policy(ctx.app)
    if not enabled:
      return
    for cid in ctx.config_ids():
      cfg = ctx.configs[cid]
      for kind, nodes in (("slot", ctx.slots(cid)), ("task", ctx.tasks(cid))):
        is_task = kind == "task"
        field = _autofill.hoist_field(is_task)
        for i, node in enumerate(nodes):
          text = node.get(field)
          # An `ask` ladder is one of the blockers worth reporting, so test its first
          # rung — passing the list itself would return None and the rule would never
          # fire on the case it documents.
          if isinstance(text, list):
            text = text[0] if text else None
          hoist = _autofill.split_leading_filler(text, extra_ack=extra_ack)
          if hoist is None:
            continue
          blocker = _autofill.hoist_blocked_by(node, cfg, is_task=is_task)
          if blocker not in _autofill.REPORTABLE_BLOCKS:
            continue
          name = node.get("name", "<unnamed>")
          yield self.finding(
              message=(
                  f"{kind.capitalize()} {name!r} opens {field} with "
                  f"{hoist.filler!r}, which the automatic-filler pass would have "
                  f"spoken during the wait — but {_autofill.BLOCK_REASONS[blocker]}. "
                  "Either accept the uncovered wait or author "
                  "`filler_say` on this node yourself."),
              # Index-based, matching every other rule (model_reliance `slots[i]`,
              # conversation `tasks[i].awaits`); Slot Studio resolves the anchor.
              location=Location(config_id=cid, node=name,
                                json_path=f"{kind}s[{i}].{field}"),
              anchor=NodeAnchor(kind=kind, ref=name, field=field),
              rationale=("A tool round trip or model turn the caller waits through in "
                         "silence, where the copy to cover it was already written."),
              fix_id="author_filler_say",
          )
