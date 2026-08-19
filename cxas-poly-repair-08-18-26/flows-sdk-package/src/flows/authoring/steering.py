"""Steering — a first-class model-classified intent router.

`router_flow` is the low-level factory that emits CES's `router.json` shape (a `Flow`
with `router=True`, an `active_flow` gate, and a `set_active_flow` bootstrap). On its
own it takes bare flow-key strings and knows nothing about WHY a caller belongs on a
route, what to do with a route it recognises but does not handle, or which phrasings
should skip the model — so an author hand-writes the `<routing>` instruction, a parallel
`route_cues` dict, and one recognise-and-hand-off flow per deferred intent, all kept in
sync BY KEY across three separate structures.

This module makes that a single object. A `route(...)` carries everything about one
destination — its `name`, a semantic `description` the model classifies on, the child
`flow` it runs when handled, deterministic `cues` that skip the model, and a `backstop`
(reserved for the post-model keyword net). `router_flow([...routes])` then GENERATES the
rest at build time:

* the `<routing>` instruction, from the routes' descriptions (no hand-written block);
* ONE shared deferral flow that every unhandled route maps to — the flow key survives on
  the `active_flow` gate, so the single flow records `detected_intent` correctly per
  route (see `build._router_runtime_vars`, which emits the non-identity `flow_config_map`
  this needs) instead of N near-identical hand-written flows;
* the `route_cues` dict, folded from each route's `cues`.

Routing stays MODEL-FIRST: `cues` are a deterministic fast-path under the model, and there
is NO `default_flow` — it deterministically preempts the model on the opening turn, which
defeats a multi-intent router (the trap the reference Comcast router documented). Low
confidence is handled ONE way: `disambiguate` makes the model ask a brief clarifying
question whenever it can't confidently route (ambiguous, unclear, or off-topic), and
`disambiguation(max_turns, on_exhaust)` hands off after a bounded number of turns. The
post-model `backstop` keyword net rides the same after-model handler.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
  from .dsl import Flow

# A route name is emitted verbatim as a flow key, a config id, AND into the `<routing>`
# instruction, so restrict it to an identifier-ish shape (no quotes/spaces/specials that
# could malform the instruction or collide with a config path).
_ROUTE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class Route:
  """One destination of a steering router.

  `name` is the intent key. It is the value the model passes to `set_active_flow`, the
  id of the child flow config, AND the label handed downstream as `detected_intent` —
  keep it equal to the downstream category so a deferred intent routes onward by that
  label. `description` is a by-MEANING statement of what the destination is FOR; it feeds
  the generated `<routing>` classifier, so keep it free of verbatim caller phrases (that
  would train the model on your eval strings). `flow` is the child DAG run when the route
  is HANDLED locally; leave it None to DEFER — recognise the intent and hand it off (the
  shared deferral flow records `detected_intent` and speaks the hand-off line).

  `cues` are deterministic phrasings that route here BEFORE the model runs (a fast-path
  for obvious wording; keep cues off a broad catch-all route so they don't preempt the
  model on its own inputs). `backstop` are keywords consulted only AFTER the model
  declines to classify — the post-model net (activated by the engine capability; accepted
  here so route definitions are forward-compatible). `aliases` are extra spoken phrasings
  for this route; they are folded into its `cues` (another pre-model fast-path).
  """

  name: str
  description: str
  flow: Optional["Flow"] = None
  cues: tuple[str, ...] = ()
  backstop: tuple[str, ...] = ()
  aliases: tuple[str, ...] = ()
  # --- multi-level steering (a route is a node in an intent TREE) -------------------
  # `subroutes` are this route's finer intents (the next level down). A route with
  # subroutes is an INTERNAL node: once the caller is here, a scoped, silent classifier
  # picks one child from the same utterance (and recurses), so a single opening turn
  # resolves a whole intent PATH (billing -> dispute -> overcharge). A node with
  # subroutes runs no `flow` of its own (the two are mutually exclusive) — the resolved
  # leaf is what a deferred call hands downstream.
  subroutes: tuple["Route", ...] = ()
  # Per-LEVEL low-confidence handling, INHERITED down the tree unless overridden:
  # None = inherit the nearest ancestor's setting (ultimately the router's `disambiguate=`);
  # False = force this level SILENT (fall to `default`); True / disambiguation(...) = when
  # this level cannot resolve silently, ASK a clarifying question (bounded by the budget).
  disambiguate: Any = None
  # The fallback child a level resolves to when nothing classifies (and, for a silent
  # level, always). "" = auto-derive (prefer a general/other child, else the first).
  default: str = ""
  # A HIGH-PRECISION route the model must never reach by INFERENCE — only on an explicit
  # request (a live-agent/human hand-off, a cancel/disconnect). Generates a "choose only
  # when explicitly asked" directive in <routing>, and makes this route ineligible as a
  # `default_route` / `catch_all_route` target — the generic guard against over-escalation.
  explicit_only: bool = False

  @property
  def handled(self) -> bool:
    """A route with a local `flow` is handled here; without one it is deferred."""
    return self.flow is not None

  @property
  def is_internal(self) -> bool:
    """An internal node (has `subroutes`) classifies into a child instead of running a
    flow or deferring directly; leaves (handled or deferred) have no subroutes."""
    return bool(self.subroutes)

  def child(self, name: str) -> "Optional[Route]":
    """The direct subroute named `name`, or None."""
    return next((s for s in self.subroutes if s.name == name), None)


def route(
    name: str,
    description: str,
    *,
    flow: Optional["Flow"] = None,
    cues: "list[str] | tuple[str, ...]" = (),
    backstop: "list[str] | tuple[str, ...]" = (),
    aliases: "list[str] | tuple[str, ...]" = (),
    subroutes: "list[Route] | tuple[Route, ...]" = (),
    disambiguate: Any = None,
    default: str = "",
    explicit_only: bool = False,
) -> Route:
  """Declare one steering destination (see `Route`).

  Pass `subroutes=[route(...), ...]` to make this a level in an intent TREE: once routed
  here, a scoped silent classifier picks one child (and recurses), so one utterance
  resolves a whole path. `disambiguate=` overrides the inherited low-confidence handling
  for THIS level (None inherit / False silent / True|disambiguation(...) ask); `default=`
  names the child a level falls back to. Internal nodes take no `flow` (they classify,
  they do not run a DAG)."""
  name = str(name).strip() if name else ""
  if not name:
    raise ValueError("route(): name is required")
  if not _ROUTE_NAME_RE.match(name):
    raise ValueError(
        f"route(): name {name!r} must match [A-Za-z0-9_-]+ — it is emitted as a flow key, "
        "a config id, and into the <routing> instruction")
  if not description or not str(description).strip():
    raise ValueError(f"route({name!r}): a semantic description is required — it is what "
                     "the model classifies on")

  def _strs(field_name: str, val) -> tuple:
    out = tuple(val)
    if not all(isinstance(x, str) for x in out):
      raise ValueError(f"route({name!r}): {field_name} must all be strings, got {out!r}")
    return out

  subs = tuple(subroutes)
  if not all(isinstance(s, Route) for s in subs):
    raise ValueError(
        f"route({name!r}): subroutes must all be flows.route(...) objects, got {subs!r}")
  if subs and flow is not None:
    raise ValueError(
        f"route({name!r}): a route with subroutes is an internal node that CLASSIFIES into "
        "a child — it cannot also have a flow= (that is a handled leaf). Drop one.")
  _child_names = [s.name for s in subs]
  _dupes = sorted({n for n in _child_names if _child_names.count(n) > 1})
  if _dupes:
    raise ValueError(f"route({name!r}): duplicate subroute names {_dupes}")
  if default:
    if not subs:
      raise ValueError(f"route({name!r}): default={default!r} needs subroutes to pick from")
    if default not in _child_names:
      raise ValueError(
          f"route({name!r}): default={default!r} is not one of the subroutes {_child_names}")
  if disambiguate is not None and not subs:
    raise ValueError(
        f"route({name!r}): disambiguate= only applies to an internal node (one with "
        "subroutes) — a leaf has nothing to disambiguate between")

  return Route(
      name=name,
      description=str(description).strip(),
      flow=flow,
      cues=_strs("cues", cues),
      backstop=_strs("backstop", backstop),
      aliases=_strs("aliases", aliases),
      subroutes=subs,
      disambiguate=disambiguate,
      default=str(default or ""),
      explicit_only=bool(explicit_only),
  )


def all_nodes(routes: "list[Route] | tuple[Route, ...]") -> "list[Route]":
  """Every Route in the tree (pre-order), descending through `subroutes`. Used to validate
  tree-wide name uniqueness and per-node settings."""
  out: list[Route] = []
  for r in routes:
    out.append(r)
    out.extend(all_nodes(r.subroutes))
  return out


# --- flat single-pass folding (A2) ------------------------------------------------
#
# `route_mode="flat"` keeps a category TREE as authoring input (so the grouped prompt and
# the leaf->category derivation come for free) but folds classification into ONE inference
# to the LEAF. `flatten_tree` turns the tree into (flat_leaf_routes, groups, leaf_paths):
# the leaves become plain deferred routes (the gate enum, sharing the one deferral flow),
# the grouping is retained for the <routing> block, and each leaf's full path is baked in.


def _leaf_paths_under(node: "Route", prefix: "list[str]") -> "list[tuple[Route, list[str]]]":
  """Every deepest LEAF under `node`, paired with its full path (prefix + names down to it).
  A node with no subroutes is itself a leaf."""
  here = prefix + [node.name]
  if not node.subroutes:
    return [(node, here)]
  out: "list[tuple[Route, list[str]]]" = []
  for c in node.subroutes:
    out.extend(_leaf_paths_under(c, here))
  return out


def _flat_leaf_route(leaf: "Route") -> "Route":
  """A leaf recast as a TOP-LEVEL flat route: keeps its meaning-based description, cues,
  backstop, aliases, and any local `flow` (a handled leaf stays handled); drops the tree-only
  fields (subroutes / disambiguate / default) it no longer has a level to use."""
  return Route(
      name=leaf.name, description=leaf.description, flow=leaf.flow,
      cues=leaf.cues, backstop=leaf.backstop, aliases=leaf.aliases,
      explicit_only=leaf.explicit_only)


def _rule_out_note(cat: str, sibling_cats: "list[str]") -> str:
  """A contrastive "belongs to a different topic area" note for a category: choose a leaf
  here only when the goal is really this area, else pick from the named sibling areas."""
  others = [c for c in sibling_cats if c != cat]
  if not others:
    return ""
  # Single-quote each sibling area (matching the quoted '{cat}' below) so the rendered
  # list reads ('payments', 'support') — clearer to the model than a bare payments, support.
  named = ", ".join(f"'{o}'" for o in others)
  return (f"choose a '{cat}' leaf only when the request is really about this topic area; if "
          f"it actually belongs to a different topic area ({named}), pick a leaf from that "
          f"area instead.")


def flatten_tree(
    routes: "list[Route] | tuple[Route, ...]",
) -> "tuple[list[Route], tuple, dict[str, str]]":
  """Fold a category tree into `(flat_leaf_routes, groups, leaf_paths)` for FLAT mode.

  Each top-level route is one TOPIC AREA: its deepest leaves become flat top-level routes
  (grouped under the area, with a generated contrastive rule-out note), and every leaf gets
  its full `<area>/.../<leaf>` path. A top-level route that is ITSELF a leaf (a handled flow
  or a plain deferred intent with no subroutes) stays as its own single-leaf area (no
  rule-out note — there is nothing under it to rule out)."""
  cat_names = [r.name for r in routes]
  flat: "list[Route]" = []
  groups: "list[tuple]" = []
  leaf_paths: "dict[str, str]" = {}
  for r in routes:
    leaves = _leaf_paths_under(r, [])
    pairs: "list[tuple[str, str]]" = []
    for leaf, path in leaves:
      flat.append(_flat_leaf_route(leaf))
      leaf_paths[leaf.name] = "/".join(path)
      pairs.append((leaf.name, leaf.description))
    rule_out = _rule_out_note(r.name, cat_names) if r.is_internal else ""
    groups.append((r.name, r.description, tuple(pairs), rule_out))
  return flat, tuple(groups), leaf_paths


@dataclass(frozen=True)
class Disambiguation:
  """A multi-turn disambiguation budget for a steering router.

  When the model is torn it asks a brief clarifying question (instruction-driven) instead
  of guessing. This bounds that: after `max_turns` turns where the model still has not
  routed, the post-model handler fires `on_exhaust` — the name of a route to send the
  caller to (typically a human/agent route). `max_turns=0` (the plain `disambiguate=True`
  form) is ask-with-no-budget: the model may keep clarifying, nothing forces a hand-off.
  """

  max_turns: int = 0
  on_exhaust: str = ""


def disambiguation(max_turns: int = 2, on_exhaust: str = "") -> Disambiguation:
  """A disambiguation budget: ask up to `max_turns` clarifying turns, then route to the
  `on_exhaust` route (e.g. a human hand-off). See `Disambiguation`."""
  if max_turns < 0:
    raise ValueError("disambiguation(): max_turns must be >= 0")
  on_exhaust = str(on_exhaust or "")
  if on_exhaust and max_turns <= 0:
    raise ValueError(
        "disambiguation(): on_exhaust needs max_turns > 0 — with max_turns=0 there is no "
        "budget to exhaust, so on_exhaust would never fire. Use disambiguation(max_turns=N, "
        "on_exhaust=...) for a hand-off, or disambiguate=True for ask-with-no-budget.")
  return Disambiguation(max_turns=int(max_turns), on_exhaust=on_exhaust)


@dataclass
class SteeringSpec:
  """The compiled intent of a `router_flow([...routes])` call.

  Stashed on the router `Flow` (authoring-time reference, never emitted as config, like
  `Flow._remote_agents`). `build.py` reads it to add the handled child flows + the shared
  deferral flow, to emit the non-identity `flow_config_map`, and to generate the
  `<routing>` instruction. `disambiguate` / `engine_only_tools` are carried for the engine
  capability + the cold-start guard.

  A route with `flow=None` is deferred to ONE generated deferral flow (records
  `detected_intent`, speaks a default hand-off line, ends). For a richer hand-off (a real
  transfer, a custom line, A2A), give the routes a shared `flow` instead — many routes
  pointing at one flow is just a non-identity `flow_config_map`, and that flow reads the
  chosen route off the `active_flow` gate.
  """

  routes: tuple[Route, ...]
  # The router's own config_id — used to NAMESPACE the generated members (the shared
  # deferral flow + its recorder tool) so two Route-based routers in one app never collide.
  config_id: str = "steering"
  engine_only_tools: tuple[str, ...] = ()
  disambiguate: Any = None
  # --- structured routing guidance (rendered at the tail of the <routing> block) --------
  # These name the recurring, cross-cutting routing POLICIES so they are consistent, tuned,
  # validatable, and traceable — instead of hand-written prose. See `routing_block`.
  #   default_route   — the "home": chosen when the model is not confident, or off-topic.
  #   catch_all_route — a real in-scope task that fits NO specific route (prefer over an
  #                     escalation; the classic triage / main-menu node).
  #   tie_break       — how to handle a multi-intent utterance ("primary" = route on the
  #                     primary intent, the default; "none" = omit the line).
  #   routing_notes   — the escape hatch for genuinely idiosyncratic guidance (a large one
  #                     is a smell that a policy is missing).
  default_route: str = ""
  catch_all_route: str = ""
  tie_break: str = "primary"
  routing_notes: tuple[str, ...] = ()
  # How a sub-intent level RESOLVES the model's pick to a canonical child id:
  #   "enum"  — (default) the SAME enum setter the L1 gate uses: accept an exact key
  #             (case-insensitive) else `not_in_enum`. One closed-choice mechanism at every
  #             level — the router gate and every sub-level behave identically. An ASKED
  #             level recovers a miss via its on_exhaust_fill/default; a SILENT level has no
  #             retry ladder, so a miss leaves the level unresolved and the recorder reports
  #             the coarser parent intent (it does not guess a leaf).
  #   "fuzzy" — the opt-out escape hatch: a classifier setter (exact key, then
  #             case-insensitive description match, then the level's default), which is more
  #             forgiving of the shape the model emits and gives a silent level a leaf default.
  # The per-node hint names the exact keys either way, so the model emits keys; "enum" then
  # rejects a stray non-key instead of best-effort matching it.
  classifier_style: str = "enum"
  # --- flat single-pass mode (A2) ------------------------------------------------------
  # `route_mode="flat"` FOLDS a category tree into ONE model inference: `routes` above are
  # the flattened LEAVES (each a plain deferred route, so the gate enum is the leaf set and
  # `set_active_flow` classifies straight to a leaf), while the original category grouping is
  # preserved HERE for the <routing> block and the leaf->path derivation:
  #   * `groups` — the CATEGORY-GROUPED prompt structure, one tuple per top-level topic area:
  #     `(cat_name, cat_desc, ((leaf, leaf_desc), ...), rule_out_note)`. `routing_instruction`
  #     renders it as a grouped block with contrastive "belongs to a different topic area"
  #     rule-out notes, instead of a flat ungrouped list.
  #   * `leaf_paths` — `{leaf -> "<cat>/.../<leaf>"}`, baked into the deferral recorder so a
  #     flat pick still records `detected_path` (the category is DERIVED, not model-picked).
  # "hierarchical" (default) leaves both empty and every branch below byte-intact.
  route_mode: str = "hierarchical"
  groups: tuple = ()
  leaf_paths: "dict[str, str]" = field(default_factory=dict)

  @property
  def defer_config_id(self) -> str:
    """Config id of the ONE shared deferral flow (namespaced by the router)."""
    return f"{self.config_id}_defer"

  @property
  def record_intent_name(self) -> str:
    """Name of the generated detected_intent recorder tool (namespaced by the router)."""
    return f"{self.config_id}_record_intent"

  @property
  def record_path_name(self) -> str:
    """Name of the generated multi-level path recorder tool (namespaced by the router)."""
    return f"{self.config_id}_record_path"

  @property
  def has_deferred(self) -> bool:
    """True when any route is a PLAIN deferred leaf (no `flow`, no `subroutes`) — those
    share the ONE generated deferral flow. An INTERNAL node instead gets its own generated
    classification flow, so it does not count here."""
    return any(not r.handled and not r.is_internal for r in self.routes)

  @property
  def has_internal(self) -> bool:
    """True when any route opens a second level (has subroutes)."""
    return any(r.is_internal for r in self.routes)

  @property
  def internals(self) -> "list[tuple[Route, Optional[Route], Any]]":
    """Every INTERNAL node in the tree (any depth), as `(node, parent, effective)`.

    `effective` is the node's disambiguation setting, resolved top-down by INHERITANCE: a
    node uses its own `disambiguate` when set, else the nearest ancestor's, ultimately the
    router's `disambiguate`. `parent` is None for a level-1 node."""
    out: "list[tuple[Route, Optional[Route], Any]]" = []

    def _walk(node: Route, parent: "Optional[Route]", inherited: Any) -> None:
      eff = node.disambiguate if node.disambiguate is not None else inherited
      out.append((node, parent, eff))
      for c in node.subroutes:
        if c.is_internal:
          _walk(c, node, eff)

    for r in self.routes:
      if r.is_internal:
        _walk(r, None, self.disambiguate)
    return out

  def head_classifiers(self) -> "dict[str, tuple[dict, Any]]":
    """`{setter: (mapping, default)}` for `App.classifiers`, one per internal node.

    The mapping is `{child_name: [child_description]}` — the model's classification
    exemplars. The default is the node's fallback child for a SILENT level (so the passive
    slot always resolves), and None for an ASKED level (so a no-match re-asks, then rides
    the slot's on_exhaust_fill after the budget).

    Empty under `classifier_style="enum"`: the sub-intent setter is then the enum setter
    (driven by the slot's enum `validation_rules`), matching the L1 gate — one closed-choice
    mechanism at every level."""
    if self.classifier_style == "enum":
      return {}
    out: "dict[str, tuple[dict, Any]]" = {}
    for node, _parent, eff in self.internals:
      mapping = {c.name: [c.description] for c in node.subroutes}
      dflt = None if _is_ask(eff) else default_child(node)
      out[f"set_sub_intent__{node.name}"] = (mapping, dflt)
    return out

  def route_cues(self) -> dict[str, list[str]]:
    """Per-route deterministic cues, folded into one dict (order preserved). A route's
    `aliases` (extra spoken phrasings for it) are folded in alongside its `cues`, since
    both act as pre-model keyword fast-paths for that route."""
    out: dict[str, list[str]] = {}
    for r in self.routes:
      merged = list(r.cues) + [a for a in r.aliases if a not in r.cues]
      if merged:
        out[r.name] = merged
    return out

  def backstop_cues(self) -> dict[str, list[str]]:
    """Per-route POST-model keyword net (`route.backstop`), folded into one dict. The
    engine consults these only when the model declined to route. Empty ⇒ not emitted."""
    return {r.name: list(r.backstop) for r in self.routes if r.backstop}

  def disambiguate_config(self) -> "Optional[dict[str, Any]]":
    """The runtime disambiguation budget `{max_turns, on_exhaust}`, or None when there is
    no hard budget (plain `disambiguate=True` is instruction-only, no runtime state)."""
    d = self.disambiguate
    if isinstance(d, Disambiguation) and d.max_turns > 0:
      return {"max_turns": d.max_turns, "on_exhaust": d.on_exhaust}
    return None

  def config_map(self) -> dict[str, str]:
    """`flow key -> DAG config id`: a handled route to its own flow, an INTERNAL node to its
    own generated classification flow (config id == the route name), and every plain
    deferred leaf to the ONE shared deferral flow."""
    out: dict[str, str] = {}
    for r in self.routes:
      if r.handled:
        out[r.name] = r.flow.config_id  # type: ignore[union-attr]
      elif r.is_internal:
        out[r.name] = r.name
      else:
        out[r.name] = self.defer_config_id
    return out

  def child_flows(self, no_input: "Optional[dict[str, Any]]" = None) -> "list[Flow]":
    """The DAGs `build` must add to the app: each handled route's flow, one generated
    classification flow per INTERNAL route (the silent multi-level classifier + a path
    recorder), plus the shared deferral flow when any plain leaf defers.

    `no_input` is the ROUTER's silence-and-hold policy, inherited by the flows generated
    here. Flow policies inherit nothing in the engine, and these flows are not authored
    by hand, so there is nowhere for an author to put one -- which left a caller who asks
    for time at a disambiguation question ("is this about your bill, or your bill
    payment?") with no acknowledgement and, worse, no hold state, so the request read as
    a failure to answer and spent the one retry those levels have. A handed route's own
    flow is NOT touched: that one has an author, and its own policy.
    """
    flows: list[Flow] = [r.flow for r in self.routes if r.handled]  # type: ignore[misc]
    generated: list[Flow] = []
    for r in self.routes:
      if r.is_internal:
        generated.append(classification_flow(r, self))
    if self.has_deferred:
      generated.append(defer_flow(self))
    if no_input:
      for f in generated:
        # A shallow copy per flow. NOT because the engine mutates the policy -- it reads
        # it with `.get` and nothing more, and the ladder's own state lives in `sm` keyed
        # by flow. It is the BUILD that edits configs in place (language select, the
        # sensitive-marker strip), and a post-processor that rewrote one flow's ladder
        # would rewrite every generated flow's if they shared the object.
        f.set("no_input", dict(no_input))
    return flows + generated


# --- generators -------------------------------------------------------------------

_ROUTING_HEADER = (
    "On a routing turn your only job is to send the caller to the right place: call "
    "set_active_flow exactly once with one of the flows below, chosen by the MEANING of "
    "what the caller wants — not by matching particular words. Do not try to answer or "
    "handle the request yourself on this turn; route first."
)

# Same job, but for a multi-agent host that routes SILENTLY (the specialist speaks first).
_ROUTING_HEADER_SILENT = (
    "On a routing turn your only job is to send the caller to the right specialist: call "
    "set_active_flow exactly once with one of the flows below, chosen by the MEANING of "
    "what the caller wants — not by matching particular words. Route SILENTLY: say nothing "
    "on the routing turn, and let the specialist speak first."
)

_DISAMBIGUATE_LINE = (
    "If you cannot confidently choose a route — the request is ambiguous, unclear, or "
    "off-topic — ask ONE brief clarifying question instead of guessing, and route once the "
    "caller answers.")


def routing_block(
    routes: "list[tuple[str, str]]", *, disambiguate: Any = None, silent: bool = False,
    tie_break: str = "none", default_route: str = "", catch_all_route: str = "",
    explicit_routes: "tuple[str, ...]" = (), notes: "tuple[str, ...]" = (),
) -> str:
  """The `<routing>` block — the SINGLE description-driven routing-SI generator.

  `routes` is a list of `(flow_key, description)` pairs. Used by both the single-agent
  `router_flow([route(...)])` path (via `routing_instruction`) and the multi-agent
  `HostRouter` path (see `build._default_host_instruction`), so there is ONE routing-SI
  style across the SDK. `silent=True` frames it for a host that routes without speaking.
  When `disambiguate` is set, the model is told to ask a clarifying question for any
  low-confidence turn rather than guessing.

  The tail carries the STRUCTURED routing policies (canonical, tuned text — not free-form
  prose the author has to rewrite), each rendered only when set: `tie_break="primary"`
  (multi-intent -> route on the primary intent), `default_route` (the home when unsure or
  off-topic), `catch_all_route` (a real in-scope task that fits no specific route -> prefer
  it over an escalation), `explicit_routes` (choose only on an explicit request), then any
  free-text `notes`. The renderer defaults `tie_break="none"` so the host/bare paths are
  unchanged; the route-based router turns it on via `SteeringSpec.tie_break`.
  """
  lines = ["<routing>", _ROUTING_HEADER_SILENT if silent else _ROUTING_HEADER, ""]
  for name, desc in routes:
    lines.append(f'  - flow="{name}" — {desc}')
  lines.append("")
  lines.append("Route on the caller's actual goal.")
  if tie_break == "primary":
    lines.append("A single utterance can mention several things; route on the caller's "
                 "primary intent.")
  if default_route:
    lines.append(f'When you are not confident which flow fits, or the turn is off-topic or '
                 f'small talk, choose "{default_route}".')
  if catch_all_route:
    lines.append(f'When the caller wants a specific, in-scope task but none of the flows '
                 f'above fit, choose "{catch_all_route}" — do not send them to a person for '
                 f'something it can handle.')
  if explicit_routes:
    named = ", ".join(f'"{n}"' for n in explicit_routes)
    lines.append(f'Choose {named} only when the caller explicitly asks for it; never route '
                 f'there by inference.')
  for n in notes:
    if n and n.strip():
      lines.append(n.strip())
  if disambiguate is not None:
    lines.append(_DISAMBIGUATE_LINE)
  lines.append("</routing>")
  return "\n".join(lines)


_GROUPED_HEADER = (
    "The destinations are organised by TOPIC AREA. First read the topic-area lines to find "
    "the ONE area the caller's goal belongs to; then, within that area, choose the single "
    "most specific destination. Set the intent to that exact leaf key — do not stop at the "
    "topic area, and do not invent a key."
)


def grouped_routing_block(
    groups: "tuple", *, disambiguate: Any = None, silent: bool = False,
    tie_break: str = "primary", default_route: str = "", catch_all_route: str = "",
    explicit_routes: "tuple[str, ...]" = (), notes: "tuple[str, ...]" = (),
) -> str:
  """The CATEGORY-GROUPED `<routing>` block for FLAT single-pass steering (A2).

  Same closed set of leaf destinations as a plain-flat list, but organised under their topic
  area with a header per category and a contrastive "belongs to a different topic area"
  rule-out note — the hypothesised accuracy lever over an ungrouped flat prompt. The model
  still makes ONE pick (a leaf key); the category is derived offline from the leaf. `groups`
  is `((cat_name, cat_desc, ((leaf, leaf_desc), ...), rule_out_note), ...)`. The structured
  policy tail matches `routing_block` so the two generators stay stylistically identical."""
  lines = ["<routing>", _ROUTING_HEADER_SILENT if silent else _ROUTING_HEADER, "",
           _GROUPED_HEADER, ""]
  for cat, cat_desc, pairs, rule_out in groups:
    lines.append(f"# TOPIC AREA — {cat}: {cat_desc}")
    for leaf, desc in pairs:
      lines.append(f'  - flow="{leaf}" — {desc}')
    if rule_out:
      lines.append(f"  Rule out: {rule_out}")
    lines.append("")
  lines.append("Route on the caller's actual goal.")
  if tie_break == "primary":
    lines.append("A single utterance can mention several things; route on the caller's "
                 "primary intent.")
  if default_route:
    lines.append(f'When you are not confident which flow fits, or the turn is off-topic or '
                 f'small talk, choose "{default_route}".')
  if catch_all_route:
    lines.append(f'When the caller wants a specific, in-scope task but none of the flows '
                 f'above fit, choose "{catch_all_route}" — do not send them to a person for '
                 f'something it can handle.')
  if explicit_routes:
    named = ", ".join(f'"{n}"' for n in explicit_routes)
    lines.append(f'Choose {named} only when the caller explicitly asks for it; never route '
                 f'there by inference.')
  for n in notes:
    if n and n.strip():
      lines.append(n.strip())
  if disambiguate is not None:
    lines.append(_DISAMBIGUATE_LINE)
  lines.append("</routing>")
  return "\n".join(lines)


def routing_instruction(spec: "SteeringSpec") -> str:
  """The generated `<routing>` block for a single-agent `router_flow`, from the routes'
  descriptions + the router's structured policies. Appended to the author's persona
  (`App.agent_instruction`) at emit. In FLAT mode (A2) it renders the CATEGORY-GROUPED
  block from `spec.groups` instead of an ungrouped flat list."""
  explicit = tuple(r.name for r in spec.routes if r.explicit_only)
  if spec.route_mode == "flat" and spec.groups:
    return grouped_routing_block(
        spec.groups,
        disambiguate=spec.disambiguate,
        tie_break=spec.tie_break,
        default_route=spec.default_route,
        catch_all_route=spec.catch_all_route,
        explicit_routes=explicit,
        notes=spec.routing_notes,
    )
  return routing_block(
      [(r.name, r.description) for r in spec.routes],
      disambiguate=spec.disambiguate,
      tie_break=spec.tie_break,
      default_route=spec.default_route,
      catch_all_route=spec.catch_all_route,
      explicit_routes=explicit,
      notes=spec.routing_notes,
  )


# A complete, self-contained tool module (CES runs each tool in an isolated sandbox; it
# carries its own imports and uses the sandbox-provided `context`). Reads the detected
# intent off the `active_flow` gate — the router set it to the chosen route key, and that
# key SURVIVES even when many keys map to this one shared flow (the resolved config id is
# stored separately) — records it as `detected_intent` for downstream routing, and
# returns it so the deferral rung can speak/route on it.
_RECORD_INTENT_TOOL = "steering_record_intent"
_RECORD_INTENT_SOURCE = '''from typing import Any, Dict


def steering_record_intent() -> Dict[str, Any]:
  """Record the router-detected intent (off the active_flow gate) for downstream routing."""
  intent = ""
  try:
    intent = str(context.state.get("active_flow") or "").strip()  # noqa: F821
  except Exception:
    intent = ""
  try:
    context.state["detected_intent"] = intent  # noqa: F821
  except Exception:
    pass
  return {"success": True, "detected_intent": intent}
'''


def record_intent_tool(name: str = _RECORD_INTENT_TOOL) -> tuple[str, str, list[str]]:
  """`(name, source, output_keys)` for the generated deferral recorder tool.

  `name` namespaces the tool (and its `def`) by the router's config_id, so two Route-based
  routers in one app emit distinct recorder tools that don't collide in the registry."""
  return name, _RECORD_INTENT_SOURCE.replace(_RECORD_INTENT_TOOL, name), \
      ["success", "detected_intent"]


# The FLAT-mode deferral recorder. A flat router's `set_active_flow` picks a LEAF straight,
# so the gate carries the leaf; this reads it (state, then `sm.filled` fallback like the
# multi-level recorder) and, from a build-time `_LEAF_PATHS` map baked in, records
# `detected_path=<category>/.../<leaf>` alongside `detected_intent=<leaf>` — deriving the
# category the flat classification never asked the model to name. Self-contained (sandboxed).
_RECORD_FLAT_TOOL = "steering_record_intent"
_RECORD_FLAT_SOURCE = '''from typing import Any, Dict

_LEAF_PATHS = __LEAF_PATHS__


def steering_record_intent() -> Dict[str, Any]:
  """Record the flat-router leaf intent + its derived category path for downstream routing."""
  intent = ""
  try:
    state = context.state  # noqa: F821
    intent = str(state.get("active_flow") or "").strip()
    if not intent:
      sm = state.get("sm") or {}
      filled = sm.get("filled") if isinstance(sm, dict) else None
      if isinstance(filled, dict):
        intent = str(filled.get("active_flow") or "").strip()
  except Exception:
    intent = ""
  path = _LEAF_PATHS.get(intent, intent)
  try:
    context.state["detected_intent"] = intent  # noqa: F821
    context.state["detected_path"] = path  # noqa: F821
  except Exception:
    pass
  return {"success": True, "detected_intent": intent, "detected_path": path}
'''


def record_flat_intent_tool(
    name: str, leaf_paths: "dict[str, str]") -> "tuple[str, str, list[str]]":
  """`(name, source, output_keys)` for the FLAT-mode deferral recorder — like
  `record_intent_tool`, but bakes in `leaf_paths` so it also records `detected_path`."""
  src = _RECORD_FLAT_SOURCE.replace("__LEAF_PATHS__", repr(dict(leaf_paths)))
  return name, src.replace(_RECORD_FLAT_TOOL, name), \
      ["success", "detected_intent", "detected_path"]


# The line the AUTO-GENERATED deferral flow speaks (for `flow=None` routes). Deliberately
# not a knob — for a custom line or a real transfer, give the routes a shared `flow`.
_DEFAULT_DEFER_SAY = "Thanks — let me get you to the right place for that."


def defer_flow(spec: "SteeringSpec") -> "Flow":
  """The ONE shared deferral flow every unhandled route maps to.

  A rung (a tool-calling task, NOT a bare announce) so it renders the hand-off line on
  the routing turn: a silent child reached by `set_active_flow` renders empty, because a
  rung's executor call is what drives the turn. It records `detected_intent` (off the
  `active_flow` gate — the chosen route key survives even though many keys share this one
  flow) and speaks a default hand-off line. For a custom line or a real transfer, don't
  rely on this: give the routes a shared `flow` of your own instead.
  """
  from .dsl import Flow, result_slot, task

  f = Flow(spec.defer_config_id)
  # A router child that terminates must re-arm the router, or the router (and its
  # siblings) go 'zombie' after the leg completes.
  f.set("bootstrap", {"reset_on_complete": True})
  f.add(result_slot("detected_intent", "record_detected_intent"))
  f.task(task(
      "record_detected_intent",
      spec.record_intent_name,
      [],
      "detected_intent",
      out_key="detected_intent",
      then_say=_DEFAULT_DEFER_SAY,
      terminal=True,
  ))
  return f


# --- multi-level (hierarchical) classification ------------------------------------
#
# A route with `subroutes` is an INTERNAL node: once the router sends the caller here
# (level 1, a model routing turn), a SCOPED, SILENT classifier picks one child from the
# same utterance, and recurses, so one opening turn resolves a whole intent PATH. Each
# internal node compiles to:
#   * a passive `intent_slot` named `sub_intent__<node>` that classifies the node's
#     CHILDREN (its `option_cues` = each child's cues+backstop; its classifier exemplars
#     = each child's description — see `SteeringSpec.head_classifiers`). Deeper slots are
#     condition-gated on the parent slot's value, so only the routed subtree fills.
#   * when a level's effective `disambiguate` is set, its slot is ASKED instead of passive
#     (it still fills silently when the utterance is clear; it asks only when it is not).
# The internal node's own flow (config id == the route name) holds this slot chain plus a
# recorder rung that walks the chain and records `detected_intent` (deepest leaf) +
# `detected_path` (the slash-joined ancestry), then defers. NO engine change: this is all
# passive intent slots + `App.classifiers`, both of which the blessed engine already runs.

# Name fragments that mark a child as the least-committal fallback (general / other / …).
_DEFAULT_CHILD_HINTS = ("_general", "general", "_other", "other", "discuss", "misc",
                        "_gen", "unsure", "anything_else", "something_else")


def _humanize(name: str) -> str:
  """A weak spoken cue for a child with no author cues (its de-underscored name)."""
  return name.replace("_", " ").strip()


def _is_ask(eff: Any) -> bool:
  """Whether an effective `disambiguate` value means ASK (True / disambiguation(...)) vs
  stay SILENT (None inherit-to-off / False explicit-off)."""
  return not (eff is None or eff is False)


def _ask_turns(eff: Any) -> int:
  """The clarify budget for an ASKED level: a disambiguation()'s max_turns, else 2."""
  if isinstance(eff, Disambiguation) and eff.max_turns > 0:
    return eff.max_turns
  return 2


def default_child(node: "Route") -> str:
  """The child a level falls back to when nothing classifies: the author's `default=`, else
  a least-committal child (general/other/discuss/…), else the first — so a SILENT level
  always resolves (the classifier default) and an ASKED level has somewhere to land."""
  names = [c.name for c in node.subroutes]
  if node.default:
    return node.default
  for hint in _DEFAULT_CHILD_HINTS:
    for n in names:
      if hint in n:
        return n
  return names[0]


def _disambig_ask(children: "tuple[Route, ...]") -> str:
  """A caller-friendly clarifying question built from the children's DESCRIPTIONS (never
  their internal names), for an ASKED level."""
  descs = [c.description for c in children]
  if len(descs) == 1:
    body = descs[0]
  elif len(descs) == 2:
    body = f"{descs[0]}, or {descs[1]}"
  else:
    body = ", ".join(descs[:-1]) + f", or {descs[-1]}"
  return f"Just to make sure I help with the right thing — is this about {body}?"


def _sub_intent_hint(node: "Route") -> str:
  """The model-facing directive for a node's classifier: the child allowlist (key — meaning)
  plus an instruction to return EXACTLY one key.

  Without this the model, handed a `set_sub_intent__<node>` tool for a many-child node, does
  not know the exact keys and invents a plausible slug (`seasonal_hold` for
  `plan_manage_temporary_disconnect`) that the setter cannot match, so the level collapses to
  its default. Listing the keys with their meanings and telling the model to copy a key
  verbatim is what makes a scoped, silent classification land on the right leaf — the same
  device the bespoke per-category hint used before this was a first-class primitive.
  """
  opts = "\n".join(f"  {c.name} — {c.description}" for c in node.subroutes)
  return (
      f"The caller has been routed to '{node.name}'. From what they have ALREADY said, "
      f"silently choose the single best-matching sub-intent below by MEANING and call "
      f"set_sub_intent__{node.name} with EXACTLY that key — copy the key verbatim, do not "
      f"paraphrase, translate, or invent a new label. If genuinely none fits, use "
      f"'{default_child(node)}'.\n\nOptions (key — meaning):\n" + opts)


def sub_intent_slot(node: "Route", eff: Any, *, condition: Any = None) -> dict:
  """The `sub_intent__<node>` slot that classifies `node`'s children.

  `option_cues` = each child's `cues` + `backstop` (a deterministic pre-model net; at depth
  both fold into cues, since the post-model net is a top-level-only engine capability),
  falling back to the humanized child name. The model classifier (registered separately in
  `SteeringSpec.head_classifiers`) does the semantic pick from the child descriptions, guided
  by the `hint` (the child allowlist + "return a key verbatim"; see `_sub_intent_hint`).
  Silent (passive) unless the effective `disambiguate` says to ASK.
  """
  from .dsl import intent_slot

  options: dict[str, list[str]] = {}
  for c in node.subroutes:
    cues = list(c.cues) + [a for a in c.backstop if a not in c.cues]
    options[c.name] = cues or [_humanize(c.name)]
  setter = f"set_sub_intent__{node.name}"
  name = f"sub_intent__{node.name}"
  if _is_ask(eff):
    slot = intent_slot(
        name, options, passive=False, setter=setter,
        ask=_disambig_ask(node.subroutes),
        on_exhaust_fill=default_child(node), max_retries=_ask_turns(eff),
        condition=condition)
  else:
    slot = intent_slot(name, options, passive=True, setter=setter, condition=condition)
  slot["hint"] = _sub_intent_hint(node)
  return slot


def classification_flow(l1_route: "Route", spec: "SteeringSpec") -> "Flow":
  """The generated flow an INTERNAL level-1 route activates (config id == the route name).

  Holds a condition-gated chain of `sub_intent__<node>` slots for the whole subtree (each
  deeper level gated on its parent's resolved value) + a terminal recorder rung. The
  recorder requires the level-1 slot (so it never fires on the bare routing turn) and then
  walks whatever depth resolved. Re-arms the router on completion.
  """
  from .dsl import Flow, eq, result_slot, task

  f = Flow(l1_route.name)
  f.set("bootstrap", {"reset_on_complete": True})

  def _emit(node: "Route", parent: "Optional[Route]", inherited: Any) -> None:
    eff = node.disambiguate if node.disambiguate is not None else inherited
    cond = None if parent is None else eq(f"sub_intent__{parent.name}", node.name)
    f.add(sub_intent_slot(node, eff, condition=cond))
    for c in node.subroutes:
      if c.is_internal:
        _emit(c, node, eff)

  _emit(l1_route, None, spec.disambiguate)
  f.add(result_slot("detected_intent", "record_path"))
  f.task(task(
      "record_path",
      spec.record_path_name,
      [],
      "detected_intent",
      out_key="detected_intent",
      then_say=_DEFAULT_DEFER_SAY,
      terminal=True,
      requires=[f"sub_intent__{l1_route.name}"],
  ))
  return f


# The multi-level recorder walks the `sub_intent__<node>` chain from the `active_flow` gate
# (the level-1 route the model picked) down through each resolved sub-intent, and records
# `detected_intent` (the deepest leaf) + `detected_path` (the slash-joined ancestry).
# Reads every slot (the `active_flow` gate included) via a helper that checks `context.state`
# first and then `context.state["sm"]["filled"]` — in the blessed engine the gate rides
# `sm.filled` like any other slot, so the root read must use the same fallback, not a bare
# `state.get`. Self-contained (CES runs each tool sandboxed).
_RECORD_PATH_TOOL = "steering_record_path"
_RECORD_PATH_SOURCE = '''from typing import Any, Dict


def steering_record_path() -> Dict[str, Any]:
  """Walk the sub_intent chain and record the detected intent PATH for downstream routing."""
  try:
    state = context.state  # noqa: F821
  except Exception:
    return {"success": True, "detected_intent": "", "detected_path": ""}

  def _slot(key):
    try:
      v = state.get(key)
    except Exception:
      v = None
    if v not in (None, ""):
      return v
    try:
      sm = state.get("sm") or {}
      filled = sm.get("filled") if isinstance(sm, dict) else None
      if isinstance(filled, dict):
        return filled.get(key)
    except Exception:
      pass
    return None

  root = str(_slot("active_flow") or "").strip()
  path = [root] if root else []
  cur = root
  for _ in range(32):  # depth guard
    if not cur:
      break
    nxt = _slot("sub_intent__" + cur)
    if not nxt:
      break
    nxt = str(nxt).strip()
    if not nxt or nxt in path:
      break
    path.append(nxt)
    cur = nxt
  detected = cur or root
  detected_path = "/".join(path)
  try:
    state["detected_intent"] = detected
    state["detected_path"] = detected_path
  except Exception:
    pass
  return {"success": True, "detected_intent": detected, "detected_path": detected_path}
'''


def record_path_tool(name: str = _RECORD_PATH_TOOL) -> "tuple[str, str, list[str]]":
  """`(name, source, output_keys)` for the generated multi-level path recorder tool.

  `name` namespaces the tool (and its `def`) by the router's config_id so two Route-based
  routers in one app emit distinct recorders that don't collide in the registry."""
  return name, _RECORD_PATH_SOURCE.replace(_RECORD_PATH_TOOL, name), \
      ["success", "detected_intent", "detected_path"]
