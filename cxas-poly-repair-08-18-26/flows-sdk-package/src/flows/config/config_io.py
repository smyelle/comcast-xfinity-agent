"""Config import / export + round-trip (TDD section 3.4, 4.1; subtask S3).

Three import paths, two export formats, one canonical internal model:

  * **import by `config_id`** -- `framework_bridge` `importlib`-loads the tool's
    `python_code.py` and calls its zero-arg `<config_id>()` function, yielding a
    plain dict (the `*_dag` modules import only `typing`, so this is safe).
  * **import a raw dict** -- the request already carries a parsed JSON object.
  * **import from source** -- a Python module / dict-literal string. Pure
    literals are parsed with `ast.literal_eval`; lambda expressions in
    `condition` / `readback_fmt` are captured as their *source text* (the
    framework already accepts lambda strings) before the literal parse.

Every path normalizes the dict into the canonical `models.Config` (TDD section
5.1). The `Config` model is the single normal form: importing the same flow by
any path, or re-importing an exported artifact, yields an equal `Config`.

Export (TDD section 4.1, D6 -- **export-to-file only, no write-back**) emits the
normalized config in two formats:

  * **JSON** -- `json.dumps` of the config dict (lossless for the dict/string
    encoded configs the repo ships).
  * **Python rendering** -- a `def <id>_dag(): return {...}` module the author
    pastes straight into a tool's `python_code.py`. The dict body is rendered
    with `pprint` so it is valid, importable Python; lambda-source strings stay
    strings (the framework compiles them on load), so the rendering re-imports to
    an equal `Config`.

`ExportResult` carries `content` + `filename` only -- the client saves it. The
server never writes into a repo file (D6; write-back deferred to v2).

Round-trip fidelity (M3): the corpus configs (bella_notte/takeout/host) use
dict/string-encoded conditions/formatters, so JSON and Python round-trips are
exact against the canonical `Config`. The single documented normalization is
numeric coercion: condition bounds (`gte`/`lte`/`gt`/`lt`) are modeled as
`float`, so an authored `5` normalizes to `5.0`. This is applied identically on
every import, so it never accumulates across a round-trip (idempotent).
"""

from __future__ import annotations

import ast
import json
import pprint
import re
from typing import Any, Optional

from ..engine import loader as framework_bridge
from . import models


class ConfigImportError(Exception):
    """Raised when no usable config can be parsed from the request inputs.

    Carries a line-anchored `diagnostics` list (TDD section 4.1: import returns
    parse `Diagnostic[]`, line-anchored on failure) so the router can surface a
    schema-valid `ImportResult` with diagnostics instead of a bare 500.
    """

    def __init__(self, message: str, diagnostics: Optional[list[models.Diagnostic]] = None):
        super().__init__(message)
        self.diagnostics = diagnostics or [
            models.Diagnostic(severity="error", message=message, raw=message)
        ]


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def import_by_id(config_id: str, framework_root: Optional[str] = None) -> dict[str, Any]:
    """Load a `<config_id>` DAG tool and call its zero-arg function -> raw dict.

    The DAG module exposes a function named exactly `<config_id>` (e.g.
    `bella_notte_dag`). If that symbol is absent we fall back to the first
    module-level zero-arg callable that returns a dict with a `slots` key.
    """
    module = framework_bridge.load_dag(config_id, framework_root)
    fn = getattr(module, config_id, None)
    if callable(fn):
        result = fn()
        if isinstance(result, dict):
            return result
    # Fallback: scan for a zero-arg callable returning a slots-bearing dict.
    for name in dir(module):
        if name.startswith("_"):
            continue
        cand = getattr(module, name)
        if not callable(cand):
            continue
        try:
            out = cand()  # type: ignore[call-arg]
        except Exception:  # pylint: disable=broad-except
            continue
        if isinstance(out, dict) and "slots" in out:
            return out
    raise ConfigImportError(
        f"Tool '{config_id}' has no zero-arg function returning a config dict."
    )


# Matches a `"<key>": lambda ...` entry so we can lift live-lambda sources to
# their string form before `ast.literal_eval` (which rejects lambdas). Captures
# the lambda body up to the entry's trailing comma at the same brace depth.
_LAMBDA_KEYS = ("condition", "readback_fmt", "success_check")


def _lift_lambdas_to_strings(source: str) -> str:
    """Rewrite `"key": lambda ...` occurrences to `"key": "lambda ..."`.

    The framework already accepts lambda *strings* (compiled via `eval` in
    `_compile_config`), so capturing the source as text is lossless for these
    fields. We only lift the known dynamic-field keys to avoid touching unrelated
    text. Balanced brackets/quotes are tracked so the lambda body is captured
    exactly up to its terminating comma or closing brace.
    """
    out: list[str] = []
    i = 0
    n = len(source)
    key_re = re.compile(r"""(['"])(%s)\1\s*:\s*lambda""" % "|".join(_LAMBDA_KEYS))
    while i < n:
        m = key_re.search(source, i)
        if not m:
            out.append(source[i:])
            break
        # Emit text up to (and including) the `key:` prefix; start of `lambda`.
        lambda_start = m.end() - len("lambda")
        out.append(source[i:lambda_start])
        # Scan the lambda body to its terminator at depth 0 (comma or `}`),
        # respecting nested brackets and string literals.
        depth = 0
        j = lambda_start
        quote: Optional[str] = None
        while j < n:
            ch = source[j]
            if quote is not None:
                if ch == "\\":
                    j += 2
                    continue
                if ch == quote:
                    quote = None
                j += 1
                continue
            if ch in "'\"":
                quote = ch
            elif ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif ch == "," and depth == 0:
                break
            j += 1
        body = source[lambda_start:j].rstrip()
        out.append(json.dumps(body))  # safe string-literal escaping
        i = j
    return "".join(out)


def _extract_json_config(tree: ast.Module) -> Optional[dict[str, Any]]:
    """The config of a `return json.loads(_CONFIG)` module, read from its JSON constant.

    `emit.scaffold._starter_dag_code` renders the config as a compact JSON string
    parsed at call time rather than as a dict literal (CES recompiles the module on
    every invocation, so a large literal costs milliseconds per call). There is no
    dict literal for the literal path below to find, so resolve the constant here.

    Deliberately matches only that emitted shape instead of evaluating the returned
    expression generally: this parses generated source, and `export_python` still
    emits the literal form, which must keep taking the path it always has. Returns
    None for anything else so the caller falls through unchanged.
    """
    name: Optional[str] = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        func = call.func
        if (isinstance(func, ast.Attribute) and func.attr == "loads"
                and isinstance(func.value, ast.Name) and func.value.id == "json"
                and len(call.args) == 1 and isinstance(call.args[0], ast.Name)):
            name = call.args[0].id
    if name is None:
        return None
    # The constant is module-level by construction, so don't walk into nested scopes.
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        try:
            value = json.loads(node.value.value)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None
    return None


def import_from_source(source: str) -> dict[str, Any]:
    """Parse a Python module / dict-literal string into a raw config dict.

    A `return json.loads(<const>)` module resolves its JSON constant. Otherwise:
    pure literals parse directly. Lambda expressions in the known dynamic-field
    keys are first captured as source strings (lossless -- the framework accepts
    lambda strings), then the whole thing is `ast.literal_eval`-ed. The dict is
    located as the first/largest dict literal in the parsed expression, or the
    return value of a `def ...(): return {...}` module body.
    """
    text = source.strip()
    if not text:
        raise ConfigImportError("Empty source.")

    # Parsed ONCE and shared: both module-form readers below need the tree, and this runs
    # per dag tool over whole app payloads (`deploy.gates`) and per tool source of a live
    # agent (`uj_studio`), where a second parse of a 100KB+ module is real request latency.
    try:
        tree: Optional[ast.Module] = ast.parse(text)
    except SyntaxError:
        tree = None  # not a module; the literal path below reports the error with its anchor

    # Module form: `def <id>(): return json.loads(_CONFIG)` -- resolve the constant.
    if tree is not None:
        json_cfg = _extract_json_config(tree)
        if json_cfg is not None:
            return json_cfg

    # Module form: `def <id>(): return {...}` -- extract the returned expression.
    dict_text = _extract_returned_dict(text, tree) if tree is not None else None
    if dict_text is None:
        dict_text = text

    lifted = _lift_lambdas_to_strings(dict_text)
    try:
        value = ast.literal_eval(lifted)
    except (ValueError, SyntaxError) as exc:
        # A parse failure has no node to point at (nothing parsed), so it always
        # anchors to the config as a whole; when the exception carries a source
        # line it rides along as `line` (Diagnostic allows extra fields) instead
        # of surviving only inside the message text. Anchoring only in the
        # `lineno is None` case had it exactly backwards: a SyntaxError, the one
        # failure that DOES know where it is, came back with no anchor at all.
        line = getattr(exc, "lineno", None)
        extra = {"line": line} if line is not None else {}
        diag = models.Diagnostic(
            severity="error",
            message=f"Could not parse config source: {exc}",
            raw=str(exc),
            anchor=models.NodeAnchor(kind="field", ref="config"),
            **extra,
        )
        raise ConfigImportError(str(exc), [diag]) from exc

    if not isinstance(value, dict):
        raise ConfigImportError("Source did not evaluate to a config dict.")
    return value


def _extract_returned_dict(text: str, tree: Optional[ast.Module] = None) -> Optional[str]:
    """If `text` is a module defining a function that returns a dict, return the
    source of that returned dict expression; else None.

    Uses the AST to find the *last* `return <dict>` in the module (handles the
    `def <id>_dag(): return _<id>_config()` -> `def _<id>_config(): return {...}`
    two-function form by preferring a return whose value is a dict literal).

    `tree` is the already-parsed `text` when the caller has one, to avoid re-parsing.
    """
    if tree is None:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return None
    dict_node: Optional[ast.Dict] = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            dict_node = node.value
    if dict_node is None:
        return None
    return ast.get_source_segment(text, dict_node)


def normalize(raw: dict[str, Any]) -> models.Config:
    """Normalize a raw config dict into the canonical `models.Config`.

    Validation errors raise `ConfigImportError` with line-free field anchors.
    """
    try:
        return models.Config.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError
        raise ConfigImportError(f"Config does not match the schema: {exc}") from exc


def config_to_dict(config: models.Config) -> dict[str, Any]:
    """The canonical JSON-able dict form of a `Config` (aliases applied,
    None-valued keys dropped) -- the basis for both export formats and for
    round-trip equality.
    """
    return config.model_dump(by_alias=True, exclude_none=True)


def import_config(
    *,
    config_id: Optional[str] = None,
    raw_dict: Optional[dict[str, Any]] = None,
    source: Optional[str] = None,
    framework_root: Optional[str] = None,
) -> models.Config:
    """Resolve the three import paths to a normalized `Config`.

    Precedence when multiple are supplied: explicit `raw_dict` > `source` >
    `config_id` (a caller passing a dict already has the parsed value).
    """
    if raw_dict is not None:
        return normalize(raw_dict)
    if source is not None:
        return normalize(import_from_source(source))
    if config_id is not None:
        return normalize(import_by_id(config_id, framework_root))
    raise ConfigImportError("Import requires one of: config_id, dict, source.")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_json(config: models.Config) -> str:
    """Render the config as pretty JSON (lossless for dict/string configs)."""
    return json.dumps(config_to_dict(config), indent=2, ensure_ascii=False)


def export_python(config: models.Config, config_id: str) -> str:
    """Render `def <id>(): return {...}` Python module text (D6: file content).

    The dict body is emitted with `pprint.pformat` so it is valid, importable
    Python; lambda-source strings stay quoted strings (the framework compiles
    them on load). Re-importing this module yields an equal `Config`.
    """
    body = pprint.pformat(config_to_dict(config), indent=2, width=88, sort_dicts=False)
    # Indent every line of the dict body by 8 spaces under `return`.
    indented = "\n".join(
        ("        " + line) if line else line for line in body.splitlines()
    )
    return (
        '"""Slot-filling DAG configuration (exported by Slot Studio)."""\n\n'
        "from typing import Any\n\n\n"
        f"def {config_id}() -> dict[str, Any]:\n"
        f'    """Return the DAG config for {config_id}."""\n'
        "    return (\n"
        f"{indented}\n"
        "    )\n"
    )


def export_config(
    config: models.Config,
    fmt: str,
    config_id: Optional[str] = None,
) -> models.ExportResult:
    """Build the `ExportResult{content, filename, warnings}` for a format.

    `fmt` is "json" or "python". `config_id` names the file / the rendered
    function (defaults to "config"). Returns content + filename only -- the
    client saves the file (D6: no write-back).
    """
    cid = config_id or "config"
    warnings = _lossy_warnings(config)
    if fmt == "json":
        return models.ExportResult(
            content=export_json(config),
            filename=f"{cid}.json",
            warnings=warnings,
        )
    if fmt == "python":
        return models.ExportResult(
            content=export_python(config, cid),
            filename="python_code.py",
            warnings=warnings,
        )
    raise ConfigImportError(f"Unknown export format: {fmt!r} (expected json|python).")


def _lossy_warnings(config: models.Config) -> list[str]:
    """Flag fields that may not round-trip exactly (TDD section 3.4 lossy spot).

    The corpus configs encode conditions/formatters as dicts or lambda *strings*,
    which round-trip cleanly. The only documented residue is a live-lambda that
    was never captured as source -- impossible here because `Config` only carries
    string/dict forms (the model has no callable type) -- so this returns no
    warnings for the corpus. The hook stays so v2 source-capture gaps surface.
    """
    return []
