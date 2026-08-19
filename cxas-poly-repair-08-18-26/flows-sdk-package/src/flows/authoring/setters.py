"""Auto-generated tool bodies for user-slot setters and stub task executors.

A `user` slot needs a `set_<name>` setter tool; a `task` needs its executor tool.
`flows` generates the standard ones from the slot/task shape so authors only hand-
write the tools that carry real logic (via `@flows.tool`). Ported from the proven
per-agent builders — the setter envelope is the framework contract
(`{"stored": True, "value": ...}` on success; `{"error": True, ...}` otherwise).
"""

from __future__ import annotations

from typing import Optional


def gen_setter(name: str, param: str) -> str:
  """Standard value-recording setter: trims, rejects empty, stores the value."""
  return (
      f'"""Setter: record {param.replace("_", " ")}."""\n'
      "from typing import Any\n\n\n"
      f'def {name}({param}: str = "") -> dict[str, Any]:\n'
      f'  """Record the value for {param}.\n\n'
      "  Args:\n"
      f"    {param}: The collected value.\n"
      "  Returns:\n"
      "    Dict with stored/value or error.\n"
      '  """\n'
      f"  v = str({param}).strip()\n"
      "  if not v:\n"
      '    return {"error": True, "error_code": "missing"}\n'
      '  return {"stored": True, "value": v}\n'
  )


def gen_enum_setter(name: str, param: str, options: list) -> str:
  """A value-recording setter that REJECTS a value outside the slot's enum.

  `validation_rules` on a single-field slot was inert: `gen_setter` stores any non-empty
  string, so an enum slot would happily accept "mmm hard to say" and then match no
  condition — the flow silently stalls with a filled-but-meaningless slot. Rejecting here
  produces the `not_in_enum` slot error the No-Match ladder is built on, which is also
  what makes `validation.on_exhaust` (and its `fill` disposition) reachable at all.
  """
  opts = list(options)
  return (
      f'"""Setter: record {param.replace("_", " ")} (one of {"|".join(opts)})."""\n'
      "from typing import Any\n\n\n"
      f"_OPTIONS = {opts!r}\n\n\n"
      f'def {name}({param}: str = "") -> dict[str, Any]:\n'
      f'  """Record the value for {param}.\n\n'
      "  Args:\n"
      f"    {param}: One of {', '.join(opts)}.\n"
      "  Returns:\n"
      "    Dict with stored/value, or an error when the value is not one of them.\n"
      '  """\n'
      f"  v = str({param}).strip()\n"
      "  if not v:\n"
      '    return {"error": True, "error_code": "missing"}\n'
      "  for opt in _OPTIONS:\n"
      "    if v.lower() == str(opt).lower():\n"
      '      return {"stored": True, "value": opt}\n'
      '  return {"error": True, "error_code": "not_in_enum"}\n'
  )


def gen_validating_setter(name: str, param: str, rules: list[dict]) -> str:
  """A single-field setter that ENFORCES this slot's `validation_rules` and returns
  the single-setter contract (`{"stored": True, "value": ...}` /
  `{"error": True, "error_code": ...}`).

  `gen_setter` stores any non-empty value, so a rule like `luhn` (card checksum) on a
  standalone slot would be inert. This reuses the SAME per-rule check lines as the
  multi-field setter (`_multi_field_check_lines`) — length_digits / date_format /
  luhn — so a lone `user_slot("card_number", validation_rules=[{"kind":"luhn"}])`
  validates identically to a grouped one. On the first failed rule the field lands in
  `field_errors` and we surface its code; a valid value is normalized (e.g. digits
  stripped) and stored.
  """
  check_lines, needs_re, needs_dt = _multi_field_check_lines(param, rules or [])
  imports = ["from typing import Any"]
  if needs_re:
    imports.append("import re")
  if needs_dt:
    imports.append("import datetime")
  head = (
      "\n".join(imports) + "\n\n\n"
      f'def {name}({param}: str = "") -> dict[str, Any]:\n'
      f'    """Record + validate {param.replace("_", " ")}.\n\n'
      "    Args:\n"
      f"      {param}: The collected value.\n"
      "    Returns:\n"
      "      Dict with stored/value or error (single-setter contract).\n"
      '    """\n'
      # Underscore-prefixed so a slot literally named `values`/`field_errors` can't
      # shadow these locals (which caused a circular-reference JSON crash).
      "    _values, _field_errors = {}, {}\n"
  )
  tail = [
      "    if _field_errors:",
      '        return {"error": True, "error_code": next(iter(_field_errors.values()))}',
      f"    if {param!r} in _values:",
      f'        return {{"stored": True, "value": _values[{param!r}]}}',
      '    return {"error": True, "error_code": "missing"}',
  ]
  return head + "\n".join(check_lines) + "\n" + "\n".join(tail) + "\n"


def gen_wrap_up_setter(name: str, param: str) -> str:
  """Classify a wrap-up answer: no (done) / text (SMS the details) / yes (more).

  A deterministic branch: a model-driven action tool mid-wrap gets swallowed by the
  slot engine as a no-input, so the flow handles 'text me' explicitly.

  The emitted matcher compares WHOLE WORDS. Substring matching classified "another
  one" and "I want to know more" as "no" (the token hides inside "a-no-ther" and
  "k-no-w"), i.e. it hung up on a caller who was asking for more.
  """
  # Whole-word tokens (see the matcher below). "not really" / "nah" / "no more" are
  # listed explicitly: the old bare-substring test caught them by accident inside
  # "not"/"no", at the cost of also catching "a-no-ther" and "k-no-w".
  neg = ("no", "nope", "nah", "not really", "no more", "nothing", "that's all",
         "thats all", "that's it", "thats it", "i'm good", "im good", "all good",
         "all set", "done", "goodbye", "bye", "no thanks", "no thank you")
  txt = ("text", "sms", "send", "message", "text me", "text it", "send it",
         "send me", "email", "the details")
  return (
      f'"""Setter: classify the wrap-up answer (done / text / more)."""\n'
      "from typing import Any\n\n"
      f"_NEG = {neg!r}\n"
      f"_TEXT = {txt!r}\n\n\n"
      f'def {name}({param}: str = "") -> dict[str, Any]:\n'
      f'  """Classify {param}: no = done; text = SMS the details; yes = wants more.\n\n'
      "  Args:\n"
      f"    {param}: The caller's answer to 'anything else?'.\n"
      "  Returns:\n"
      "    Dict with stored/value or error.\n"
      '  """\n'
      f"  v = str({param}).strip().lower()\n"
      "  if not v:\n"
      '    return {"error": True, "error_code": "missing"}\n'
      "  # Match on WHOLE words: blank out punctuation, collapse the runs and pad\n"
      "  # with spaces, so a token is only found on word boundaries. A bare\n"
      '  # substring test reads "another one" / "know more" as the token "no" and\n'
      "  # hangs up on a caller who is asking for MORE.\n"
      '  v = v.replace("\\u2019", "\'")\n'
      '  v = "".join(c if (c.isalnum() or c == "\'") else " " for c in v)\n'
      '  v = " " + " ".join(v.split()) + " "\n'
      '  if any(" " + t + " " in v for t in _TEXT):\n'
      '    return {"stored": True, "value": "text"}\n'
      '  if any(" " + n + " " in v for n in _NEG):\n'
      '    return {"stored": True, "value": "no"}\n'
      '  return {"stored": True, "value": "yes"}\n'
  )


def gen_classifier_setter(
    name: str, param: str, mapping: dict[str, list[str]], default: Optional[str]
) -> str:
  """A setter mapping free-form speech to a canonical menu key (first substring hit
  wins) so DAG conditions like `eq(slot, 'hold')` branch reliably. Unmatched ->
  `default` if given, else an error that re-prompts."""
  rows = "".join(
      f"    ({k!r}, {sorted(set(syns))!r}),\n" for k, syns in mapping.items()
  )
  if default is None:
    fallback = '  return {"error": True, "error_code": "unrecognized_selection"}\n'
  else:
    fallback = f'  return {{"stored": True, "value": {default!r}}}\n'
  return (
      f'"""Setter: classify {param.replace("_", " ")} to a canonical key."""\n'
      "from typing import Any\n\n"
      "_MAP = [\n" + rows + "]\n\n\n"
      f'def {name}({param}: str = "") -> dict[str, Any]:\n'
      f'  """Map the caller\'s phrasing for {param} to a canonical key.\n\n'
      "  Args:\n"
      f"    {param}: The caller's stated choice (free text or a key).\n"
      "  Returns:\n"
      "    Dict with stored/value or error.\n"
      '  """\n'
      f"  v = str({param}).strip().lower()\n"
      "  if not v:\n"
      '    return {"error": True, "error_code": "missing"}\n'
      "  for key, syns in _MAP:\n"
      "    if v == key.lower():\n"
      '      return {"stored": True, "value": key}\n'
      "  for key, syns in _MAP:\n"
      "    for s in syns:\n"
      "      if s.lower() in v:\n"
      '        return {"stored": True, "value": key}\n'
      f"{fallback}"
  )


# Source date-format tokens → strptime formats. Mirrors backend/common/setter.py's
# `_DATE_FORMATS` so a `date_format` rule lowers to the SAME strptime check on both
# paths; a `detail` that is already a strptime string (e.g. "%Y-%m-%d") passes through.
_DATE_FORMATS = {"YYYY-MM-DD": "%Y-%m-%d", "MMDDYYYY": "%m%d%Y", "MM/DD/YYYY": "%m/%d/%Y"}


def _multi_field_check_lines(field: str, rules: list[dict]) -> tuple[list[str], bool, bool]:
  """Validation lines for ONE multi-setter field (4-space-indented, inside the body).

  Ported line-for-line from backend/common/setter.py's `_field_check_lines` so the
  flows author path and the CES backend emit byte-identical setter bodies. A rule is
  `{"kind"|"type": ..., "detail": ...}` (both key spellings accepted — the DSL's
  `validation_rules` uses `type`, the migration SFIR uses `kind`). Known kinds:
  `length_digits` (re.sub-strip then len-check), `date_format` (strptime), `enum`
  (case-insensitive membership; `detail` is a `|`-joined string OR a list). Unknown
  kinds → store as-is (never silently reject a value we can't check). On a failed
  check the field is recorded in `field_errors` and NOT stored, so the engine re-asks
  it. Returns `(lines, needs_re, needs_datetime)`.
  """
  known: list[tuple] = []
  needs_re = needs_dt = False
  for r in rules or []:
    kind = r.get("kind") or r.get("type")
    detail = r.get("detail")
    if kind == "length_digits" and str(detail or "").isdigit():
      known.append(("length_digits", int(detail)))
      needs_re = True
    elif kind == "date_format":
      fmt = _DATE_FORMATS.get(detail, detail)
      if fmt:
        known.append(("date_format", fmt))
        needs_dt = True
    elif kind == "enum" and detail:
      opts = detail if isinstance(detail, list) else [
          o.strip() for o in str(detail).split("|") if o.strip()
      ]
      opts = [o for o in opts if o]
      if opts:
        known.append(("enum", opts))
    elif kind == "luhn":
      # Credit/debit card checksum (mod-10). No detail; mirrors
      # authoring/validators.luhn_valid (kept in lockstep by test_luhn.py).
      known.append(("luhn", None))
      needs_re = True
  if not known:
    return ([f'    if {field} not in ("", None):',
             f'        _values[{field!r}] = {field}'], False, False)
  lines = [f'    if {field} not in ("", None):',
           f'        _ok, _val = True, {field}']
  for kind, arg in known:
    if kind == "length_digits":
      # Only str/int are valid digit inputs. Reject bool (str(True)=="True" -> "")
      # and float/list (str(12.3)->"123" would spuriously pass a length check) up
      # front. `[^0-9]` (not `\D`) so Unicode digits (e.g. Arabic-Indic ١٢٣) are
      # stripped rather than surviving into the count.
      lines += ['        if _ok:',
                '            if not isinstance(_val, (str, int)) or isinstance(_val, bool):',
                f'                _field_errors[{field!r}] = "invalid_length"; _ok = False',
                '            else:',
                '                _d = re.sub(r"[^0-9]", "", str(_val))',
                f'                if len(_d) != {arg}:',
                f'                    _field_errors[{field!r}] = "invalid_length"; _ok = False',
                '                else:',
                '                    _val = _d']
    elif kind == "date_format":
      lines += ['        if _ok:',
                '            try:',
                f'                datetime.datetime.strptime(str(_val), {arg!r})',
                '            except ValueError:',
                f'                _field_errors[{field!r}] = "invalid_date_format"; _ok = False']
    elif kind == "enum":
      lines += ['        if _ok:',
                f'            _opts = {arg!r}',
                '            _m = [o for o in _opts if o.lower() == str(_val).strip().lower()]',
                '            if not _m:',
                f'                _field_errors[{field!r}] = "not_in_enum"; _ok = False',
                '            else:',
                '                _val = _m[0]']
    elif kind == "luhn":
      # Card checksum (mod-10). Type-guard first (no lists/dicts/bool), strip to
      # ASCII digits only (`[^0-9]`, so Unicode digits can't corrupt `ord(_c)-48`),
      # then the length gate BEFORE the summation loop (so a pathologically long
      # string is rejected without walking every digit). Mirrors validators.luhn_valid.
      lines += ['        if _ok:',
                '            if not isinstance(_val, (str, int)) or isinstance(_val, bool):',
                f'                _field_errors[{field!r}] = "invalid_card"; _ok = False',
                '            else:',
                '                _d = re.sub(r"[^0-9]", "", str(_val))',
                '                if not 12 <= len(_d) <= 19:',
                f'                    _field_errors[{field!r}] = "invalid_card"; _ok = False',
                '                else:',
                '                    _s, _alt = 0, False',
                '                    for _c in reversed(_d):',
                '                        _n = ord(_c) - 48',
                '                        if _alt:',
                '                            _n *= 2',
                '                            if _n > 9: _n -= 9',
                '                        _s += _n; _alt = not _alt',
                '                    if _s % 10 != 0:',
                f'                        _field_errors[{field!r}] = "invalid_card"; _ok = False',
                '                    else:',
                '                        _val = _d']
  lines.append(f'        if _ok: _values[{field!r}] = _val')
  return lines, needs_re, needs_dt


def gen_multi_setter(setter_name: str, fields: list[dict]) -> str:
  """The CANONICAL multi-field validating setter body (shared by the flows author
  path AND the CES backend — ONE generator, so both emit identical bodies).

  A carve/`setter_group` flow collects its user slots through ONE `set_<op>_inputs`
  tool. `fields` is an ordered list of `{"name": <setter_field>, "validation_rules":
  [{kind|type, detail}, ...]}` (one per grouped slot; the `name` is the key written
  into `values`, matching each slot's `setter_field`). Emits a self-contained module:
  EXPLICIT typed params (NEVER `**kwargs` — CES derives the call schema from the
  signature, so a `**kwargs` setter registers none and Slot Studio reports "the tool
  isn't deployed"), per-field validation lines lowered from the rules, and the
  `{"stored": bool, "values": {...}, "field_errors": {...}}` return shape the
  `slot_intake` multi-setter path consumes. Sandbox-safe: stdlib/typing only.
  """
  fields = fields or [{"name": "confirm_action", "validation_rules": []}]
  names = [f["name"] for f in fields]
  params = ", ".join(f'{n}: str = ""' for n in names)
  body: list[str] = []
  needs_re = needs_dt = False
  for f in fields:
    ls, nr, nd = _multi_field_check_lines(f["name"], f.get("validation_rules") or [])
    body += ls
    needs_re, needs_dt = needs_re or nr, needs_dt or nd
  imports = ["from typing import Any"]
  if needs_re:
    imports.append("import re")
  if needs_dt:
    imports.append("import datetime")
  args_doc = "\n".join(
      f"      {n}: Collected value for {n.replace('_', ' ')}." for n in names
  )
  head = (
      "\n".join(imports) + "\n\n\n"
      f"def {setter_name}({params}) -> dict[str, Any]:\n"
      '    """Record + validate the collected inputs for this operation.\n\n'
      "    Args:\n"
      f"{args_doc}\n"
      "    Returns:\n"
      "      Dict with stored/values or field_errors (framework multi-setter contract).\n"
      '    """\n'
      # Underscore-prefixed so a grouped field literally named `values`/`field_errors`
      # can't shadow these locals (circular-reference JSON crash). The RETURNED keys
      # stay `values`/`field_errors` — the framework multi-setter contract.
      "    _values, _field_errors = {}, {}\n"
  )
  return (
      head + "\n".join(body)
      + '\n    return {"stored": bool(_values), "values": _values,'
        ' "field_errors": _field_errors}\n'
  )


def gen_executor(name: str, params: list[str], out_keys: list[str]) -> str:
  """A deterministic stub task executor returning `{...out_keys, success: True}`.

  Emitted for a task whose tool isn't hand-authored — a placeholder the author
  replaces with a real `@flows.tool`. The stub value is a sha256 digest of the
  inputs, so it is stable ACROSS processes (`hash()` of a str is salted per
  interpreter) and offline simulation is genuinely repeatable.
  """
  sig = ", ".join(f'{p}: str = ""' for p in params) or ""
  arg_lines = "".join(f"    {p}: input {p}.\n" for p in params) or "    (none)\n"
  basis = ' + "|" + '.join(f"str({p})" for p in params) if params else '"x"'
  ret = ", ".join(f'"{k}": _v' for k in out_keys)
  return (
      f'"""Executor stub for {name} — returns {{success, ...}}; replace with a real @flows.tool."""\n'
      "import hashlib\n"
      "from typing import Any\n\n\n"
      f"def {name}({sig}) -> dict[str, Any]:\n"
      f'  """Perform {name} and return a result.\n\n'
      "  Args:\n"
      f"{arg_lines}"
      "  Returns:\n"
      f"    Dict with {', '.join(out_keys) or 'result'} and a success flag.\n"
      '  """\n'
      f"  basis = {basis}\n"
      "  # sha256, not hash(): PYTHONHASHSEED salts str hashing per process, so\n"
      "  # hash() gives this stub a different value on every run and an offline\n"
      "  # simulation cannot be replayed.\n"
      '  _digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()\n'
      "  _v = str(int(_digest[:12], 16) % 100000).zfill(5)\n"
      f"  return {{{ret + ', ' if ret else ''}\"success\": True}}\n"
  )


def gen_update_language(names: list[str]) -> str:
  """The `update_language` tool body for a multilingual app (see language.py).

  Gates a caller-requested language switch and persists it: writes the chosen
  language into `context.state["active_language"]` (the CES session state the
  language-lock hook and framework relay read via `callback_context.state`) and
  returns an `agent_action` telling the model to continue the whole conversation in
  the new language. `context` is CES-injected as a module global; the defaulted param
  would shadow it, so the body falls back to the global. Writing `context.variables`
  instead — a separate read-oriented namespace — silently fails to stick the switch.
  `names` are the human display names the model uses (e.g. ["English", "Spanish"]).
  """
  valid = "{" + ", ".join(repr(n) for n in names) + "}"
  options = ", ".join(f'"{n}"' for n in names)
  return (
      '"""Tool: switch the active conversation language when the caller asks."""\n'
      "from typing import Any\n\n"
      f"_SUPPORTED = {valid}\n\n\n"
      'def update_language(new_language: str = "", context: Any = None) -> dict[str, Any]:\n'
      '  """Switch the active conversation language when the caller requests it.\n\n'
      "  Call this BEFORE producing your first response in the new language.\n\n"
      "  Args:\n"
      f"    new_language: The language to switch to. One of: {options}.\n"
      "  Returns:\n"
      "    Dict with success, active_language, and agent_action.\n"
      '  """\n'
      "  name = str(new_language).strip().title()\n"
      "  if name not in _SUPPORTED:\n"
      "    return {\n"
      '        "success": False,\n'
      '        "active_language": "",\n'
      '        "agent_action": "Ask the caller which supported language they want.",\n'
      "    }\n"
      "  # Persist to context.state -- the CES session state the language-lock hook and the\n"
      "  # framework relay read via callback_context.state['active_language']. CES injects\n"
      "  # `context` as a module GLOBAL (the convention the framework's own tools use); the\n"
      "  # `context` PARAM below defaults to None and would SHADOW that global, so fall back\n"
      "  # to it. context.variables is a separate, read-oriented namespace that never feeds\n"
      "  # callback_context.state, so a write there silently fails to stick the switch.\n"
      '  ctx = context if context is not None else globals().get("context")\n'
      "  if ctx is not None:\n"
      "    try:\n"
      '      ctx.state["active_language"] = name\n'
      "    except Exception:\n"
      "      pass\n"
      "  return {\n"
      '      "success": True,\n'
      '      "active_language": name,\n'
      '      "agent_action": "Continue the entire conversation in " + name + ".",\n'
      "  }\n"
  )


def gen_active_flow_router(routes: dict[str, str]) -> str:
  """The app-specific `set_active_flow` for a multi-agent app (flow key -> agent).

  Unlike the single-agent gate setter, this returns `target_agent` so the host's
  after_tool (and a sub-agent's slot_intake) can transfer to the right specialist.
  `routes` maps each canonical flow key to the target agent display name; both the
  host's silent routing and a sub-agent's mid-call sibling hop call this same tool.
  """
  valid = "{" + ", ".join(repr(k) for k in routes) + "}"
  rows = "".join(f"    {k!r}: {v!r},\n" for k, v in routes.items())
  return (
      '"""Setter: route to a specialist agent (host routing + sibling transfer)."""\n'
      "from typing import Any\n\n"
      f"_VALID_FLOWS = {valid}\n"
      "_FLOW_TO_AGENT = {\n" + rows + "}\n\n\n"
      'def set_active_flow(flow: str = "") -> dict[str, Any]:\n'
      '  """Activate a flow and route to its specialist agent.\n\n'
      "  Args:\n"
      "    flow: The flow to activate.\n"
      "  Returns:\n"
      "    Dict with stored/value (+ target_agent) or error.\n"
      '  """\n'
      "  flow = str(flow).lower().strip()\n"
      "  if flow not in _VALID_FLOWS:\n"
      '    return {"error": True, "error_code": "invalid_flow"}\n'
      '  result = {"stored": True, "value": flow}\n'
      "  target = _FLOW_TO_AGENT.get(flow)\n"
      "  if target:\n"
      '    result["target_agent"] = target\n'
      "  return result\n"
  )
