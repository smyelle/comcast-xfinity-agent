"""Validation wrappers + diagnostic mapper (TDD section 3.2 / 4.2 / 7, S2).

Two responsibilities:

1. **Wrap the real validator.** `validate_single` / `validate_cross` call the
   framework's `DagConfigValidator` / `CrossConfigValidator` (loaded via
   `framework_bridge`) so Studio's verdict is byte-identical to the repo tool
   (PRD M2 fidelity). Source-aware checks are unlocked by passing
   `setter_sources` / `task_tool_sources` when available; without them the
   result matches a plain `validate_dag_config({"raw_config": cfg})` call.

2. **Map flat strings -> anchored Diagnostics.** The framework returns flat
   `errors` / `warnings` string lists. `map_diagnostics` parses each into a
   node/edge/field-anchored `Diagnostic`, **always** preserving the original
   string in `Diagnostic.raw` and **never** dropping a diagnostic (an
   unrecognised message still becomes a `Diagnostic` with no anchor). Fix
   synthesis (⚡/🪄 remedies) lives in `fix_synth.py`; diagnostics are read-only.

The mapper is intentionally regex/prefix based over the message text rather than
keyed to source line numbers, so a framework message tweak degrades to "anchored
less precisely / raw preserved" instead of dropping information.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from ..engine import loader as fb
from . import models, tool_scan


# ---------------------------------------------------------------------------
# Wrapping the real validator.
# ---------------------------------------------------------------------------

def _to_plain(config: Any) -> dict:
    """Coerce a pydantic `Config` (or already-dict) to the framework raw dict."""
    if isinstance(config, models.Config):
        return config.model_dump(by_alias=True, exclude_none=True)
    if hasattr(config, "model_dump"):
        return config.model_dump(by_alias=True, exclude_none=True)
    return dict(config)


def raw_validate_single(
    config: dict,
    available_tools: Optional[list[str]] = None,
    setter_sources: Optional[dict[str, str]] = None,
    task_tool_sources: Optional[dict[str, str]] = None,
    framework_root: Optional[str] = None,
) -> tuple[bool, list[str], list[str]]:
    """Direct class-API call -> `(valid, errors, warnings)` flat strings.

    Mirrors `validate_dag_config({"raw_config": cfg, ...})` exactly when
    `setter_sources` / `task_tool_sources` are None; supplying them adds the
    source-aware output-key checks (a strict superset of diagnostics).
    """
    validator_mod = fb.load_validator(framework_root)
    result = validator_mod.DagConfigValidator(
        config,
        available_tools=available_tools,
        setter_sources=setter_sources,
        task_tool_sources=task_tool_sources,
    ).validate()
    return result.valid, list(result.errors), list(result.warnings)


def raw_validate_cross(
    configs: dict[str, dict],
    framework_root: Optional[str] = None,
) -> tuple[bool, list[str], list[str]]:
    """Direct `CrossConfigValidator` call -> `(valid, errors, warnings)`."""
    validator_mod = fb.load_validator(framework_root)
    result = validator_mod.CrossConfigValidator(configs).validate()
    return result.valid, list(result.errors), list(result.warnings)


def validate_single(
    req: models.ValidateRequest,
    framework_root: Optional[str] = None,
    auto_sources: bool = True,
) -> models.ValidationReport:
    """Validate one config -> anchored `ValidationReport`.

    `available_tools` / `setter_sources` / `task_tool_sources` from the request
    win; when absent and `auto_sources` is set, they are scanned from the
    default framework root (TDD D5) so source-aware checks run by default.
    """
    config = _to_plain(req.config)

    available_tools = req.available_tools
    setter_sources = req.setter_sources
    task_tool_sources = req.task_tool_sources
    if auto_sources:
        if available_tools is None:
            available_tools = tool_scan.discover_available_tools(framework_root)
        if setter_sources is None:
            setter_sources = tool_scan.read_setter_sources(
                config, framework_root)
        if task_tool_sources is None:
            task_tool_sources = tool_scan.read_task_tool_sources(
                config, framework_root)

    validator_mod = fb.load_validator(framework_root)
    result = validator_mod.DagConfigValidator(
        config,
        available_tools=available_tools,
        setter_sources=setter_sources,
        task_tool_sources=task_tool_sources,
    ).validate()
    diagnostics = map_diagnostics(
        list(result.errors), list(result.warnings), config,
        getattr(result, "diagnostics", None),
    )
    return models.ValidationReport(
        valid=result.valid,
        diagnostics=diagnostics,
        blockers=list(getattr(result, "blockers", []) or []),
        shippable=getattr(result, "shippable", result.valid),
    )


def validate_cross(
    req: models.ValidateCrossRequest,
    framework_root: Optional[str] = None,
) -> models.CrossValidationReport:
    """Validate a config corpus -> per-config + cross-config diagnostics.

    Reproduces `validate_dag_config({"all_configs": ...})`: per-config reports
    use the same plain `DagConfigValidator(cfg, available_tools=...)` path, and
    the combined view adds the `CrossConfigValidator` diagnostics (the
    `across_flows` group).
    """
    configs = {cid: _to_plain(c) for cid, c in req.configs.items()}
    available_tools = req.available_tools

    per_config: dict[str, models.ValidationReport] = {}
    combined_valid = True
    combined_diags: list[models.Diagnostic] = []

    for cid, cfg in configs.items():
        valid, errors, warnings = raw_validate_single(
            cfg, available_tools=available_tools, framework_root=framework_root,
        )
        diags = map_diagnostics(errors, warnings, cfg)
        per_config[cid] = models.ValidationReport(valid=valid, diagnostics=diags)
        if not valid:
            combined_valid = False
        # Combined errors/warnings are prefixed `[config_id]` by the framework;
        # mirror that in the message + anchor to the flow.
        for d in diags:
            combined_diags.append(_with_flow_prefix(d, cid))

    cross_valid, cross_errors, cross_warnings = raw_validate_cross(
        configs, framework_root=framework_root,
    )
    if not cross_valid:
        combined_valid = False
    for d in map_diagnostics(cross_errors, cross_warnings, None):
        d.group = "across_flows"
        combined_diags.append(d)

    return models.CrossValidationReport(
        valid=combined_valid,
        diagnostics=combined_diags,
        perConfig=per_config,
    )


def _with_flow_prefix(d: models.Diagnostic, config_id: str) -> models.Diagnostic:
    """Re-anchor a per-config diagnostic under the combined cross report.

    The framework's combined list prefixes each entry `[config_id] `; we keep
    that in the raw string for parity while preserving the parsed anchor.
    """
    return models.Diagnostic(
        severity=d.severity,
        message=f"[{config_id}] {d.message}",
        raw=f"[{config_id}] {d.raw}",
        anchor=d.anchor,
    )


# ---------------------------------------------------------------------------
# Diagnostic mapper: flat string -> anchored Diagnostic.
# ---------------------------------------------------------------------------

# Leading-token patterns. Each returns a NodeAnchor (or None). Ordered: the
# first that matches wins; the raw string is always preserved regardless.
_SLOT_RE = re.compile(r"^(?:Announce slot|Slot) '([^']+)'")
_TASK_RE = re.compile(r"^Task '([^']+)'")
_TASK_INDEX_RE = re.compile(r"^Task at index (\d+) has no 'name'")
_SLOT_INDEX_RE = re.compile(r"^Slot at index (\d+) has no 'name'")
_SETTER_RE = re.compile(r"^Setter '([^']+)'")
_SETTER_MAP_RE = re.compile(r"^Slots \[.*\] all map to setter '([^']+)'")
_CONDITION_CTX_RE = re.compile(r"^<slot:([^:]+):condition>")
_TASK_CONDITION_CTX_RE = re.compile(r"^<task:([^:]+):condition>")
_FLOW_PREFIX_RE = re.compile(r"^\[([^\]]+)\]\s+")

# Top-level field keys that anchor to a config field rather than a node.
_FIELD_TOKENS = (
    "bootstrap", "gate_slot", "steer_back", "cancel", "exit_status",
    "event_mappings", "readback_response", "channel_readback_response",
    "shared_slots", "correction_tool", "confirm_transition_prefix",
)


def map_diagnostics(
    errors: list[str],
    warnings: list[str],
    config: Optional[dict],
    diagnostics: Optional[list[dict]] = None,
) -> list[models.Diagnostic]:
    """Map flat error/warning strings to anchored Diagnostics (lossless).

    When `diagnostics` (the validator's structured
    {severity,message,code,anchor,fix_id} twin) is supplied, a matching
    entry's stable `code`/`anchor` is PREFERRED over regex parsing; the regex
    mapper stays the fallback for any message whose structured entry lacks
    them. `raw` always keeps the original string; nothing is ever dropped.
    """
    struct_by_msg: dict[tuple[str, str], dict] = {}
    for d in (diagnostics or []):
        struct_by_msg[(d.get("severity"), d.get("message"))] = d
    out: list[models.Diagnostic] = []
    for msg in errors:
        out.append(_map_one(
            msg, "error", config, struct_by_msg.get(("error", msg))))
    for msg in warnings:
        out.append(_map_one(
            msg, "warning", config, struct_by_msg.get(("warning", msg))))
    return out


def _map_one(
    raw: str, severity: models.Severity, config: Optional[dict],
    struct: Optional[dict] = None,
) -> models.Diagnostic:
    """One flat string -> one Diagnostic. Anchor best-effort; raw preserved.

    `struct` (when present) is the validator's structured twin for this exact
    message; its stable `code`/`anchor` win over regex parsing.
    """
    # Strip any `[config_id]` prefix from cross-config combined strings so the
    # anchor parser sees the real message; raw keeps the full original string.
    body = raw
    flow_prefix = _FLOW_PREFIX_RE.match(raw)
    if flow_prefix:
        body = raw[flow_prefix.end():]

    code = None
    anchor = None
    fix_id = None
    if struct is not None:
        code = struct.get("code")
        fix_id = struct.get("fix_id")
        sa = struct.get("anchor")
        if isinstance(sa, dict) and sa.get("kind"):
            anchor = models.NodeAnchor(
                kind=sa["kind"], ref=sa.get("ref"), field=sa.get("field"))
    # Regex fallback only when the structured entry did not carry an anchor.
    if anchor is None:
        anchor = _anchor_for(body, config)
    # code/fix_id ride as extra fields (Diagnostic has extra='allow'); fix
    # synthesis lives in fix_synth.py and can dispatch on fix_id.
    return models.Diagnostic(
        severity=severity, message=body, raw=raw, anchor=anchor,
        code=code, fix_id=fix_id,
    )


def _anchor_for(body: str, config: Optional[dict]) -> Optional[models.NodeAnchor]:
    """Best-effort NodeAnchor for a (prefix-stripped) message body."""
    m = _SLOT_RE.match(body)
    if m:
        return models.NodeAnchor(
            kind="slot", ref=m.group(1), field=_slot_field(body))
    m = _SLOT_INDEX_RE.match(body)
    if m:
        return models.NodeAnchor(kind="slot", field="name")

    m = _TASK_RE.match(body)
    if m:
        return models.NodeAnchor(
            kind="task", ref=m.group(1), field=_task_field(body))
    m = _TASK_INDEX_RE.match(body)
    if m:
        return models.NodeAnchor(kind="task", field="name")

    m = _CONDITION_CTX_RE.match(body)
    if m:
        return models.NodeAnchor(kind="slot", ref=m.group(1), field="condition")
    m = _TASK_CONDITION_CTX_RE.match(body)
    if m:
        return models.NodeAnchor(kind="task", ref=m.group(1), field="condition")

    m = _SETTER_MAP_RE.match(body)
    if m:
        return models.NodeAnchor(kind="field", ref=m.group(1), field="setter")
    m = _SETTER_RE.match(body)
    if m:
        return models.NodeAnchor(kind="field", ref=m.group(1), field="setter")

    # Cross-config flow-level diagnostics. ("Announce slot" is NOT listed: the
    # quoted-name form is already claimed by _SLOT_RE above and returned there,
    # and the unquoted form matches neither inner branch — so the entry could
    # never produce an anchor and only made the branch dead.)
    if body.startswith("Config '"):
        cm = re.match(r"^Config '([^']+)'", body)
        return models.NodeAnchor(kind="flow", ref=cm.group(1) if cm else None)
    if body.startswith(("Different bootstrap tools", "Different gate_slot")):
        return models.NodeAnchor(kind="flow")

    # Top-level field-anchored messages (bootstrap.x, steer_back.x, cancel.x …).
    for tok in _FIELD_TOKENS:
        if body.startswith(tok) or body.startswith(f"'{tok}'"):
            field = _dotted_field(body, tok)
            return models.NodeAnchor(kind="field", ref=tok, field=field)

    # Whole-config structural messages.
    if body.startswith("Config has no") or body.startswith("Unknown top-level"):
        return models.NodeAnchor(kind="field", ref="config")

    return None


_SLOT_FIELDS = (
    "condition", "readback_fmt", "validation", "validate_against",
    "requires", "options_from", "source", "setter_field", "setter",
    "event_key", "ask", "hint", "message", "response", "preempt",
)
_TASK_FIELDS = (
    "on_complete", "on_failure", "outputs", "inputs", "requires",
    "success_check", "condition", "terminal", "tool", "readback_inputs",
)

# The field a message complains is *missing/required* is the salient one, even
# if another field name appears earlier in the sentence ("has source 'user' but
# no 'ask'" -> ask; "requires 'message'" -> message). Look for that first.
_MISSING_FIELD_RE = re.compile(
    r"(?:requires|missing|but no|without|but missing|needs) '([^']+)'")


def _pick_field(body: str, fields: tuple[str, ...]) -> Optional[str]:
    """Best slot/task sub-field for a message: prefer the named missing field."""
    m = _MISSING_FIELD_RE.search(body)
    if m and m.group(1) in fields:
        return m.group(1)
    for field in fields:
        if re.search(rf"\b{re.escape(field)}\b", body):
            return field
    return None


def _slot_field(body: str) -> Optional[str]:
    """Pull a slot sub-field name out of a `Slot 'x' <field> ...` message."""
    return _pick_field(body, _SLOT_FIELDS)


def _task_field(body: str) -> Optional[str]:
    return _pick_field(body, _TASK_FIELDS)


def _dotted_field(body: str, tok: str) -> Optional[str]:
    """For `bootstrap.slot ...` -> `slot`; for plain `bootstrap ...` -> None."""
    m = re.match(rf"^'?{re.escape(tok)}'?\.([A-Za-z_]+)", body)
    return m.group(1) if m else None
