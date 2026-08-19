"""How a product asks for a deploy — the one place a request is constructed.

Extracting the deploy *machinery* isn't enough to stop divergence. The failure mode
we actually hit was upstream of it: Specter hand-rolled a ``ScaffoldRequest`` and a
``PushRequest``, slotfill_migration hand-rolled its own, and Slot Studio built a third
from an HTTP body. Add a field to the contract and you have three places to remember,
one of which will be missed — which is exactly how ``overwrite=True`` came to be set
on Specter's push and nowhere else, and how a re-push after self-correction shipped a
config referencing tools CES had never created.

So the request objects are built HERE, by these two functions, and each product
passes its intent as arguments rather than assembling the object itself.
``tests/deploy/test_three_product_parity.py`` drives all three products' build paths
with identical inputs and asserts the serialized requests are byte-identical; that
test is only meaningful because there is a single constructor for it to converge on.

Both functions are pure and side-effect free.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

from ..config import models
from ..emit.models import ScaffoldFile, ScaffoldRequest
from .models import PushConfigEntry, PushSpec


def build_scaffold_request(spec: Mapping[str, Any]) -> ScaffoldRequest:
    """A ``ScaffoldRequest`` from a plain kwargs mapping.

    ``spec`` is what the migration's ``build_scaffold_spec`` / Specter's plan emit /
    the Studio's scaffold body all produce: a dict of ScaffoldRequest fields.

    An explicit ``None`` on an OPTIONAL field is dropped, so passing ``target_path=None``
    and omitting it build the same request — otherwise two products that differ only in
    how thorough their kwargs are would build "different" objects and the parity test
    would be measuring tidiness. Fields with a non-None default (``location``,
    ``model``, ``mode``) are NOT dropped: ``location=None`` is a caller bug and must
    still raise rather than silently deploy to ``us``.
    """
    nullable = {n for n, f in ScaffoldRequest.model_fields.items() if f.default is None}
    return ScaffoldRequest(
        **{k: v for k, v in dict(spec).items() if not (v is None and k in nullable)}
    )


def _as_scaffold_files(app_files: Optional[Iterable[Any]]) -> Optional[list[ScaffoldFile]]:
    if app_files is None:
        return None
    return [ScaffoldFile(**f) if isinstance(f, dict) else f for f in app_files]


def _as_config(config: Any) -> Optional[models.Config]:
    if config is None:
        return None
    if isinstance(config, models.Config):
        return config
    return models.Config.model_validate(config)


def _as_entries(
    configs: Optional[Iterable[Any]],
) -> Optional[list[PushConfigEntry]]:
    """Bundle entries from PushConfigEntry / ``{config_id, config}`` dicts.

    The BARE-id normalization is on ``PushConfigEntry`` itself, not here: the wire path
    (FastAPI parsing a body) never runs this function, and the bundle has to be keyed
    the same way whichever door it came in through.
    """
    if configs is None:
        return None
    out: list[PushConfigEntry] = []
    for c in configs:
        if isinstance(c, PushConfigEntry):
            cid, cfg = c.config_id, c.config
        elif isinstance(c, Mapping):
            cid, cfg = c["config_id"], c["config"]
        else:
            cid, cfg = c.config_id, c.config
        out.append(PushConfigEntry(config_id=str(cid), config=_as_config(cfg)))
    return out


def build_push_spec(
    *,
    app_files: Optional[Sequence[Any]] = None,
    config: Any = None,
    config_id: Optional[str] = None,
    configs: Optional[Sequence[Any]] = None,
    to: Optional[str] = None,
    display_name: Optional[str] = None,
    deployed_app_id: Optional[str] = None,
    run_gates: bool = True,
    strict: bool = False,
    overwrite: bool = False,
) -> PushSpec:
    """The one constructor for a deploy request.

    Every argument is normalized before it lands in the model — ``app_files`` from
    dicts to ``ScaffoldFile``, ``config`` from a dict to ``models.Config``, bundle
    entries to BARE-id ``PushConfigEntry`` — so two products that hold the same data
    in different shapes still produce the same request.

    On the two flags worth being explicit about:

    * ``strict`` demands the aggressive gates (setter return-shape, tool availability).
      A programmatic builder that authors every tool it ships should set it; a UI
      re-pushing somebody else's hosted app should not, or it blocks a push the author
      didn't break.
    * ``overwrite`` sets CES ``conflict_strategy=OVERWRITE``. Required when
      ``app_files`` is the COMPLETE authoritative app: without it a re-push to an
      existing app is a partial MERGE that never creates tools added since the last
      push, so the deployed config references setters that were never registered.
      This is why Specter's redeploy-after-self-correction used to ship broken.
    """
    return PushSpec(
        app_files=_as_scaffold_files(app_files),
        config=_as_config(config),
        config_id=config_id,
        configs=_as_entries(configs),
        to=to,
        display_name=display_name,
        deployed_app_id=deployed_app_id,
        run_gates=run_gates,
        strict=strict,
        overwrite=overwrite,
    )
