"""Locate the flows SDK, which lives in a different repository.

This conversion is authored against the `flows` SDK from
`depot.code.corp.goog/cloud-gecx/cxas-labs` (`packages/flows`). The agent lives here,
next to the app it converts, so the substrate it grafts is always the current source
— but that puts the SDK out of tree, and every script needs to find it.

Resolution order, first hit wins:

  1. `flows` already imports (installed, or already on PYTHONPATH) — nothing to do.
  2. `$CXAS_LABS` points at a cxas-labs checkout.
  3. One of the usual checkout locations below.

The live drivers additionally need `cxas-scrapi` (the runtime companion that drives a
deployed app) and, for `ChatSession`, the Labs service tree. Those come from the same
checkout.
"""

import os
import sys

ENV_VAR = "CXAS_LABS"
_CANDIDATES = (
    "~/Labs/cxas-labs",
    "~/cxas-labs",
    "~/src/cxas-labs",
)

_CLONE_HINT = f"""Cannot find the `flows` SDK.

This agent is authored against the flows SDK, which lives in a different repo:

    git clone <host>/cloud-gecx/cxas-labs

Then point {ENV_VAR} at it:

    {ENV_VAR}=/path/to/cxas-labs python {{script}}

(or just put its `packages/flows/src` on PYTHONPATH)."""


def _importable() -> bool:
  try:
    import flows  # noqa: F401
  except ImportError:
    return False
  return True


def _driver_deps_importable() -> bool:
  """Whether the live-drive dep (cxas-scrapi) is already installed.

  This is the pinned-wheel workflow: `flows` and `cxas-scrapi` both come from package
  registries (see pyproject), so a deployed app can be driven with no cxas-labs SOURCE
  checkout at all. Scripts that additionally need the Labs `service` tree (ChatSession)
  still require a checkout via $CXAS_LABS; the deployed-app drivers here only use
  cxas_scrapi.core.sessions.
  """
  try:
    import cxas_scrapi  # noqa: F401
  except ImportError:
    return False
  return True


def labs_root() -> str:
  """The cxas-labs checkout, or "" when the SDK is already importable."""
  env = os.environ.get(ENV_VAR)
  if env and os.path.isdir(os.path.join(env, "packages/flows/src")):
    return os.path.abspath(env)
  for cand in _CANDIDATES:
    path = os.path.expanduser(cand)
    if os.path.isdir(os.path.join(path, "packages/flows/src")):
      return path
  return ""


def add_sdk_paths(*, driver: bool = False) -> None:
  """Put the SDK (and optionally the live-drive deps) on `sys.path`.

  `driver=True` also adds cxas-scrapi and the Labs service tree, which the scripts
  that talk to a DEPLOYED app need. Raises SystemExit with something actionable
  rather than letting an ImportError surface from three frames down.
  """
  # An INSTALLED `flows` is the pinned dependency and wins over a checkout that merely
  # happens to sit in one of the usual places. Consulting `labs_root()` first meant any
  # sibling clone shadowed it -- so `make` picked up whatever branch that clone was on,
  # and a stale one failed `require_features` while the correct SDK sat in the venv.
  # `$CXAS_LABS` still wins over both, because setting it is deliberate.
  explicit = os.environ.get(ENV_VAR)
  explicit = bool(explicit and os.path.isdir(os.path.join(explicit, "packages/flows/src")))
  if not explicit and _importable() and (not driver or _driver_deps_importable()):
    return

  root = labs_root()
  if not root:
    raise SystemExit(_CLONE_HINT.format(script=os.path.basename(sys.argv[0] or "")))
  wanted = [os.path.join(root, "packages/flows/src")]
  if driver:
    wanted += [os.path.join(root, "service"),
               os.path.join(root, "vendor/cxas-scrapi/src")]
  for path in wanted:
    if path not in sys.path:
      sys.path.insert(0, path)


# Authoring surfaces this agent depends on, as (builder, keyword). Checked by
# signature because the failure they produce otherwise is a `TypeError` raised deep
# inside app.py at import time, which reads like a bug in the agent rather than
# "your SDK is too old" — and an editable install of some unrelated checkout makes
# that easy to hit without realising the wrong `flows` is in play.
_REQUIRED_FEATURES = (
    ("user_slot", "validation"),
    ("no_input", "hold_ack"),      # a spoken hold gets an answer, not a re-ask
    ("escalate", "condition"),     # the hand-off can be refused during an outage
    ("load_cujs", "path_or_data"),  # the drivers seed from cujs.yaml
    # Guardrails. Checked by KEYWORD rather than by existence alone, because an SDK
    # carrying `blocklist` but not its `scope` is the shape that fails late and quietly:
    # the app builds, deploys, and scans the caller's side as well as the agent's.
    ("safety", "level"),
    ("blocklist", "scope"),
    ("policy", "scope"),
    ("prompt_guard", "custom"),
    ("respond", "text"),
    ("generate", "prompt"),
    ("search_tool", "preferred_domains"),  # device help reads Comcast's own sites only
    # The specialist fan-out. Listed for the reason the docstring above gives, and it is
    # not hypothetical here: against the pinned wheel this raises a bare `TypeError:
    # openapi_toolset() missing 1 required keyword-only argument: 'spec'` from line 215
    # of app.py, which reads as a bug in the agent rather than "your SDK predates #705".
    # No published release carries it yet; see the CXAS_LABS note in the Makefile.
    ("remote_tool", "outputs"),
    # The slot value policy that replaces the hand-written hook mirrors. `default`
    # resolves a `{placeholder}` during the fill stages, before the DAG walk, so a rung's
    # `then_say` cannot raise mid-render; `publish` re-states a slot into session state
    # every turn, which is what the carried tools read. An SDK without them imports
    # cleanly and fails later as an AttributeError from inside the journey modules, which
    # reads like an agent bug rather than a stale checkout.
    ("event_slot", "default"),
    ("user_slot", "publish"),
)


def require_features() -> None:
  """Fail early, and by name, when the resolved SDK is too old for this agent."""
  import inspect

  import flows

  missing = []
  for builder, keyword in _REQUIRED_FEATURES:
    fn = getattr(flows, builder, None)
    if fn is None:
      missing.append(f"flows.{builder}()")
      continue
    try:
      params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
      continue
    if keyword not in params:
      missing.append(f"flows.{builder}(..., {keyword}=...)")
  if missing:
    raise SystemExit(
        "The `flows` SDK found is missing what this agent needs:\n\n    "
        + "\n    ".join(missing)
        + f"\n\nResolved from: {os.path.dirname(os.path.abspath(flows.__file__))}\n"
        f"Point {ENV_VAR} at a newer cxas-labs checkout, or update it.\n"
        "hold_ack and escalate.condition are on cxas-labs main (PR #482); "
        "load_cujs ships with the CUJ presets (flows.cujs); "
        "safety/blocklist/policy/prompt_guard are the guardrails API (PR #631); "
        "search_tool is the Google Search grounding API (PR #565); "
        "remote_tool is the remote-tool contract (PRs #701/#705), which no published "
        "release carries yet — see the CXAS_LABS note in the Makefile.\n\n"
        f"A worktree counts, which is usually the quickest fix:\n"
        f"    export {ENV_VAR}=~/Labs/cxas-labs/.worktrees/<a-checkout-on-main>")


def framework_root() -> str:
  """The framework tool directory the offline oracles load the engine from.

  Derived from the imported package rather than from a path guess, so it stays
  correct however the SDK was found.
  """
  add_sdk_paths()
  import flows
  return os.path.join(os.path.dirname(os.path.abspath(flows.__file__)),
                      "engine/framework/tools")
