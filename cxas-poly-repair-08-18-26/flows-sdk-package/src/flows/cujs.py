"""Named CUJ presets: a `cujs.yaml` mapping a journey name to its seed variables.

Driving a specific journey against a deployed agent means seeding session variables
that nobody remembers ("account 8069100230361003 with gateway_status=reboot in the
mock config string"). A `cujs.yaml` next to the app declares those bundles once, by
name; `flows.load_cujs()` resolves a name to a flat `{variable: value}` dict that can
be seeded into a session (see `flows.drive`) or baked into an app's declaration
defaults (`apply_to_app_dir`).

File shape::

    version: 1
    variable_aliases: {account: [accountNumber, account_id]}
    querystring_variables: [mock_config_string]
    defaults:
      variables:
        mock_config_string: {outage_status: none, gateway_status: clear}
    cujs:
      gateway_reboot:
        description: Gateway fault -> reboot offered.
        aliases: [reboot]
        variables:
          account: "8069100230361003"
          mock_config_string: {gateway_status: reboot}

`defaults.variables` merge under every CUJ (deep for mappings, replace for scalars),
so a bundle states only what makes it different. A variable listed in
`querystring_variables` has its mapping value serialized to `k=v&k=v` in insertion
order — an explicit opt-in, because CES also has genuinely OBJECT-typed variables that
must survive as objects. `variable_aliases` fans one authored key out to several real
variables, which is how `account` sets both `accountNumber` and `account_id`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Iterator

CUJS_FILENAME = "cujs.yaml"
CUJS_ENV_VAR = "FLOWS_CUJS"

_FILE_KEYS = {"version", "variable_aliases", "querystring_variables", "defaults", "cujs"}
_CUJ_KEYS = {"description", "aliases", "variables"}


@dataclass(frozen=True)
class CUJ:
  """One named journey and the session variables that put an agent on it."""

  name: str
  description: str = ""
  variables: dict[str, Any] = field(default_factory=dict)
  aliases: tuple[str, ...] = ()
  source: str = ""


class CUJSet:
  """The CUJs from one file, addressable by name or alias."""

  def __init__(self, cujs: list[CUJ], *, source: str = ""):
    self.source = source
    self._cujs = list(cujs)
    self._by_key: dict[str, CUJ] = {}
    for c in self._cujs:
      for key in (c.name, *c.aliases):
        self._by_key[key] = c

  def names(self) -> list[str]:
    return [c.name for c in self._cujs]

  def get(self, name: str, default: Any = None) -> Any:
    return self._by_key.get(name, default)

  def __getitem__(self, name: str) -> CUJ:
    try:
      return self._by_key[name]
    except KeyError:
      known = ", ".join(sorted(self._by_key)) or "(none)"
      where = f" in {self.source}" if self.source else ""
      raise KeyError(f"no CUJ {name!r}{where}. Available: {known}") from None

  def __contains__(self, name: object) -> bool:
    return name in self._by_key

  def __iter__(self) -> Iterator[CUJ]:
    return iter(self._cujs)

  def __len__(self) -> int:
    return len(self._cujs)


def find_cujs_file(start: str | None = None) -> str | None:
  """Locate a `cujs.yaml`: $FLOWS_CUJS, else walk up from `start` (or cwd)."""
  env = os.environ.get(CUJS_ENV_VAR)
  if env:
    return env
  # Drivers get run both from a repo root and from the app dir next to the file.
  d = os.path.abspath(start or os.getcwd())
  while True:
    candidate = os.path.join(d, CUJS_FILENAME)
    if os.path.isfile(candidate):
      return candidate
    parent = os.path.dirname(d)
    if parent == d:
      return None
    d = parent


def load_cujs(path_or_data=None, *, start: str | None = None) -> CUJSet:
  """Load CUJs from a YAML path, a parsed dict, or a discovered `cujs.yaml`."""
  source = ""
  if path_or_data is None:
    path_or_data = find_cujs_file(start)
    if path_or_data is None:
      raise FileNotFoundError(
          f"no {CUJS_FILENAME} found from {os.path.abspath(start or os.getcwd())} upward"
          f" (set ${CUJS_ENV_VAR} or pass a path)")
  if not isinstance(path_or_data, dict):
    source = str(path_or_data)
  data = _read(path_or_data)
  return _cujset_from_dict(data, source=source)


def cuj_variables(name: str, path_or_data=None, *, start: str | None = None) -> dict[str, Any]:
  """The resolved variable dict for one CUJ — the one-liner most callers want."""
  return load_cujs(path_or_data, start=start)[name].variables


def apply_to_app_dir(app_dir: str, cuj, *, strict: bool = True) -> list[str]:
  """Set each CUJ variable as the `schema.default` of its app.json declaration.

  This is what makes a deployed app land on the journey with no session variables at
  all (a CES console session cannot seed them). Unlike the build-time variable
  injection, this OVERRIDES an existing default — that is the whole point.

  Returns the variable names written. With `strict`, raises when a CUJ variable has no
  declaration to attach to: silently writing nothing looks exactly like success.
  """
  import json

  variables = cuj.variables if isinstance(cuj, CUJ) else dict(cuj)
  path = _find_app_json(app_dir)
  with open(path, "r", encoding="utf-8") as f:
    app = json.load(f)

  declarations = app.get("variableDeclarations") or []
  by_name = {d.get("name"): d for d in declarations}
  missing = [k for k in variables if k not in by_name]
  if missing and strict:
    raise ValueError(
        f"{path} declares no variable(s) {', '.join(sorted(missing))} — a CUJ can only"
        " default a variable the app declares")

  written = []
  for key, value in variables.items():
    decl = by_name.get(key)
    if decl is None:
      continue
    decl.setdefault("schema", {})["default"] = value
    written.append(key)

  app["variableDeclarations"] = declarations
  # Via a temp file in the same directory: the app dir may have been pulled from a
  # live app rather than emitted, so a half-written app.json is not regenerable.
  tmp = path + ".tmp"
  with open(tmp, "w", encoding="utf-8") as f:
    json.dump(app, f, indent=2)
  os.replace(tmp, path)
  return written


def _find_app_json(root: str) -> str:
  direct = os.path.join(root, "app.json")
  if os.path.isfile(direct):
    return direct
  for d in sorted(os.listdir(root)):
    nested = os.path.join(root, d, "app.json")
    if os.path.isfile(nested):
      return nested
  raise FileNotFoundError(f"no app.json under {root}")


def _cujset_from_dict(data: dict[str, Any], *, source: str = "") -> CUJSet:
  if not isinstance(data, dict):
    raise ValueError(f"{source or 'cujs'}: expected a mapping at the top level")
  unknown = set(data) - _FILE_KEYS
  if unknown:
    raise ValueError(
        f"{source or 'cujs'}: unknown top-level key(s) {', '.join(sorted(unknown))};"
        f" expected {', '.join(sorted(_FILE_KEYS))}")

  aliases_map = data.get("variable_aliases") or {}
  querystring = set(data.get("querystring_variables") or [])
  defaults = ((data.get("defaults") or {}).get("variables")) or {}
  raw_cujs = data.get("cujs") or {}
  if not isinstance(raw_cujs, dict):
    raise ValueError(f"{source or 'cujs'}: `cujs` must be a mapping of name -> CUJ")

  cujs: list[CUJ] = []
  claimed: dict[str, str] = {}
  for name, spec in raw_cujs.items():
    spec = spec or {}
    if not isinstance(spec, dict):
      raise ValueError(f"cuj {name!r}: expected a mapping")
    unknown = set(spec) - _CUJ_KEYS
    if unknown:
      raise ValueError(
          f"cuj {name!r}: unknown key(s) {', '.join(sorted(unknown))};"
          f" expected {', '.join(sorted(_CUJ_KEYS))}")

    merged = _merge(defaults, spec.get("variables") or {})
    resolved = _resolve(merged, aliases_map, querystring)
    cuj_aliases = tuple(spec.get("aliases") or ())
    for key in (name, *cuj_aliases):
      if key in claimed:
        raise ValueError(
            f"cuj {name!r}: the name/alias {key!r} is already used by {claimed[key]!r}")
      claimed[key] = name
    cujs.append(CUJ(
        name=name,
        description=str(spec.get("description") or ""),
        variables=resolved,
        aliases=cuj_aliases,
        source=source,
    ))
  return CUJSet(cujs, source=source)


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
  """Defaults under a CUJ: recursive for mappings (so one leaf can differ)."""
  out = dict(base)
  for k, v in over.items():
    if isinstance(v, dict) and isinstance(out.get(k), dict):
      out[k] = _merge(out[k], v)
    else:
      out[k] = v
  return out


def _querystring(key: str, value: Any) -> str:
  """Serialize a mapping to `k=v&k=v`. Only scalars have a place in one."""
  if not isinstance(value, dict):
    return _scalar(value)
  parts = []
  for k, v in value.items():
    if isinstance(v, (dict, list)):
      raise ValueError(
          f"variable {key!r} is listed under querystring_variables, so {k!r}"
          f" must be a scalar — a {type(v).__name__} has no representation in"
          " a query string")
    parts.append(f"{k}={_scalar(v)}")
  return "&".join(parts)


def _resolve(variables: dict[str, Any], aliases_map: dict[str, Any],
             querystring: set) -> dict[str, Any]:
  """Serialize querystring variables, fan aliases out, stringify scalars."""
  out: dict[str, Any] = {}
  for key, value in variables.items():
    if key in querystring:
      value = _querystring(key, value)
    elif isinstance(value, (dict, list)):
      pass  # a genuinely OBJECT/ARRAY-typed variable — leave it alone
    else:
      value = _scalar(value)
    for target in aliases_map.get(key, [key]):
      out[target] = value
  return out


def _scalar(value: Any) -> str:
  if isinstance(value, bool):
    return "true" if value else "false"
  return str(value)


def _read(path_or_data) -> dict[str, Any]:
  if isinstance(path_or_data, dict):
    return path_or_data
  import yaml  # local import: pyyaml is a core dep but keep import surface tight

  with open(path_or_data, "r", encoding="utf-8") as f:
    try:
      return yaml.safe_load(f)
    except yaml.YAMLError as e:
      # A malformed file is an authoring mistake like any other in here, so it
      # surfaces the same way rather than as a parser traceback.
      raise ValueError(f"{path_or_data}: {e}") from e
