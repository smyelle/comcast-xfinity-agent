"""Scaffold a new slot-filling app's minimal file SET.

Builds ``app.json``, ``gecx-config.json``, one agent dir (instruction + the 4
byte-synced framework callbacks), the framework tools, and one ``<config_id>_dag``
config with a gate slot — returning a ``{path: content}`` map as
``list[ScaffoldFile]``. In local mode (when ``target_path`` is given) it also
writes the tree to disk (atomic temp-dir + rename, rollback on partial failure)
and always runs ``validate_dag_config`` on the starter dag plus a callback
byte-sync check against :mod:`slot_studio.blessed_source`.

Obeys the crash-envelope rule: ``build`` never raises; any failure becomes a
typed ``ScaffoldResult(ok=False, ...)``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import uuid

from ..engine import blessed_source
from . import fanout
from . import golden_evals

logger = logging.getLogger(__name__)

# Selectable deployment models for a scaffolded / edited app. CXAS reads the
# deployed model from app.json's TOP-LEVEL ``modelSettings.model`` (per-agent
# JSONs / environment.json carry none), so the scaffold writes it there and the
# ``/api/agent/model`` endpoint patches it. ``DEFAULT_MODEL`` is a voice/live model
# so scaffolded agents drive bidi audio out of the box.
SELECTABLE_MODELS = [
    "gemini-3.1-flash-live",
    "gemini-3-flash",
    "gemini-3.5-flash",
    "gemini-3.1-pro-preview",
]
DEFAULT_MODEL = "gemini-3.1-flash-live"

# The EXACTLY-8 framework variableDeclarations a slot-filling app declares
# (CONTEXT §3). ``current_date`` is a CES predefined variable and MUST NOT be
# declared here (memory: reference_ces_predefined_vars).
_SLOT_FILLING_PROTOCOL_DEFAULT = (
    "<slot_filling_protocol>\n"
    "You are operating in SLOT FILLING mode. Follow these rules strictly:\n\n"
    "1. TOOL-DRIVEN CONVERSATION: After each user message, identify EVERY piece\n"
    "   of information the user provided and call ALL corresponding setter tools\n"
    "   in the SAME response. Never defer a setter call to a later turn when\n"
    "   the user already gave the information.\n\n"
    "2. FOLLOW THE SYSTEM'S NEXT-STEP GUIDANCE: The system provides a directive\n"
    "   below. Relay it to the user naturally — include exact values or\n"
    "   confirmation numbers. Do NOT substitute generic information.\n\n"
    "3. TOOL SELECTION — call ONLY the setter that matches:\n"
    "   (Determine the correct setter from tool names and descriptions.)\n\n"
    "4. ALWAYS CALL TOOLS — NEVER SKIP: Call the setter for EVERY piece of\n"
    "   information, even if out of range or invalid. The system validates all\n"
    "   inputs and handles errors automatically.\n\n"
    "5. NATURAL CONVERSATION: Answer off-topic questions helpfully, then return\n"
    "   to the main flow.\n\n"
    "6. ORDERING: Accept info in any order.\n\n"
    "7. HANDLING UNAVAILABLE REQUESTS: If no matching tool is visible, guide\n"
    "   the user to provide information for one of the available slots instead.\n"
    "</slot_filling_protocol>"
)


def _var_declarations(root_agent: str, config_id: str) -> list[dict]:
    """The EXACTLY-8 framework variableDeclarations (no ``current_date``)."""
    return [
        {
            "name": "sm",
            "description": (
                "Slot filling state: filled/pending slot values, task results, "
                "and status. Initialized by before_agent_callback on first use."
            ),
            "schema": {"type": "OBJECT", "default": {}},
        },
        {
            "name": "slot_filling_protocol",
            "description": (
                "Collection-phase slot filling rules — shown during collection, "
                "empty during readback"
            ),
            "schema": {"type": "STRING", "default": _SLOT_FILLING_PROTOCOL_DEFAULT},
        },
        {
            "name": "readback_protocol",
            "description": (
                "Readback and confirmation rules — shown during collection and "
                "readback phases, empty when complete"
            ),
            "schema": {"type": "STRING", "default": ""},
        },
        {
            "name": "system_directive",
            "description": (
                "System directive for the current turn — next question or "
                "readback prompt"
            ),
            "schema": {"type": "STRING", "default": ""},
        },
        {
            "name": "event_data",
            "description": (
                "External event data for slot pre-filling (e.g. from telephony, "
                "web forms, CRM)"
            ),
            "schema": {"type": "OBJECT", "default": {}},
        },
        {
            "name": "agent_config_map",
            "description": (
                "Maps agent display names to DAG config IDs for the slot-filling "
                "framework"
            ),
            "schema": {
                "type": "STRING",
                "default": json.dumps({root_agent: config_id}),
            },
        },
        {
            "name": "default_config_id",
            "description": (
                "Root/default DAG config ID used when no transfer/cache resolves "
                "(cold turn-1 at the root agent)"
            ),
            "schema": {"type": "STRING", "default": config_id},
        },
        {
            "name": "capture_si",
            "description": (
                "Debug: when true, before_model records the exact system "
                "instruction the LLM sees on each pass into sm._si_trace. Off by "
                "default."
            ),
            "schema": {"type": "BOOLEAN", "default": False},
        },
    ]


def _app_json(req) -> str:
    return json.dumps(
        {
            "name": getattr(req, "app_uuid", None) or str(uuid.uuid4()),
            "displayName": req.app_display_name,
            "rootAgent": req.root_agent,
            # CXAS reads the deployed model from app.json's TOP-LEVEL modelSettings
            # (per-agent JSONs / environment.json carry none). Without this the
            # deployed model is never set; gecx-config.json's model is a Labs-side
            # convenience only and is NOT what CXAS deploys.
            "modelSettings": {"model": req.model},
            "variableDeclarations": _var_declarations(req.root_agent, req.config_id),
        },
        indent=2,
    )


def _gecx_json(req) -> str:
    return json.dumps(
        {
            "gcp_project_id": req.gcp_project,
            "location": req.location,
            "app_name": req.app_display_name,
            "app_dir": "",
            "model": req.model,
            "modality": "text",
            "default_channel": "text",
        },
        indent=2,
    )


def _agent_json(req, framework_tool_names: list[str], dag_tools: list[str] | None = None) -> str:
    """Agent wiring: references the framework tools + the dag tool(s) by name.

    Includes the `end_session` platform tool so the agent can terminate
    conversations (scrapi lint A005) — it needs no tool dir. ``dag_tools`` defaults
    to the single ``<config_id>_dag`` (single-DAG); a multi-DAG bundle passes every
    ``<id>_dag`` so the agent references the whole workspace."""
    cb_base = f"agents/{req.root_agent}"
    dags = set(dag_tools) if dag_tools else {f"{req.config_id}_dag"}
    tools = sorted(set(framework_tool_names) | dags | {"end_session"})
    return json.dumps(
        {
            "name": getattr(req, "agent_uuid", None) or str(uuid.uuid4()),
            "displayName": req.root_agent,
            "instruction": f"{cb_base}/instruction.txt",
            "tools": tools,
            "beforeAgentCallbacks": [
                {
                    "pythonCode": (
                        f"{cb_base}/before_agent_callbacks/"
                        "before_agent_callbacks_01/python_code.py"
                    ),
                    "description": "SM initialization and config resolution",
                }
            ],
            "beforeModelCallbacks": [
                {
                    "pythonCode": (
                        f"{cb_base}/before_model_callbacks/"
                        "before_model_callbacks_01/python_code.py"
                    ),
                    "description": "DAG engine orchestration",
                }
            ],
            "afterToolCallbacks": [
                {
                    "pythonCode": (
                        f"{cb_base}/after_tool_callbacks/"
                        "after_tool_callbacks_01/python_code.py"
                    ),
                    "description": "Setter and executor state management",
                }
            ],
            "afterModelCallbacks": [
                {
                    "pythonCode": (
                        f"{cb_base}/after_model_callbacks/"
                        "after_model_callbacks_01/python_code.py"
                    ),
                    "description": "Payload injection for non-preempted turns",
                }
            ],
        },
        indent=2,
    )


def _instruction_txt(req) -> str:
    return (
        f"You are {req.root_agent}, a helpful assistant that collects the "
        "information needed to complete the user's request. Each turn, follow "
        "the system directive EXACTLY: ask only the question it gives you "
        "(rephrased naturally), one thing at a time. Never invent, add, reorder, "
        "or skip questions, and never ask for information, eligibility, consent, "
        "or verification the directive did not request. Confirm details when "
        "asked, before finalizing.\n"
    )


# ---------------------------------------------------------------------------
# Starter templates (Phase 1: deterministic, no AI)
#
# A template is pure data: a list of user slots plus the hand-written setter
# source for each, merged onto the framework gate/bootstrap floor by
# ``starter_config(template=...)``. Every template must pass
# ``validate_dag_config`` (a test asserts this), so the user lands on a
# populated, valid, simulate-able agent instead of a blank canvas.
#
# Setters are PURE validators: explicit string params (no **kwargs), an
# ``Args:`` block naming exactly those params, and the single-field return shape
# (``{"stored": True, "value": v}`` / ``{"error": True, "error_code": ...}``).
# They use only ``from typing import Any`` so they pass the setter gates.
# ---------------------------------------------------------------------------


def _strip_none(obj):
    """Recursively drop dict entries whose value is None.

    A JSON config_override carries an explicit null for every unset optional
    field; the framework reads list fields with `.get(k, [])` (None-unsafe), so
    the emitted `*_dag()` config must not carry nulls. Lists are walked; scalars
    pass through.
    """
    if isinstance(obj, dict):
        return {k: _strip_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_none(v) for v in obj]
    return obj


def _active_flow_names(config_id: str, cfg: dict, template: str | None) -> list[str]:
    """The flow name(s) this agent's gate accepts (agent-specific).

    Prefer the dag config's declared ``flow_types``; else the template name; else
    the config_id (single-flow placeholder). Lowercased to match the setter.
    """
    flows = cfg.get("flow_types") or ([template] if template else [config_id])
    return sorted({str(f).lower().strip() for f in flows if str(f).strip()})


def _active_flow_setter(flows: list[str]) -> str:
    """Generate the per-agent ``set_active_flow`` gate setter.

    set_active_flow is NOT framework — its valid flows + routing are agent-specific
    — so each agent ships its own. Generated single-agent apps carry no
    target_agent (no cross-agent routing); the engine just needs the gate filled.

    SINGLE-FLOW LENIENCY: when an app exposes at most one flow (the common
    single-agent case), there is no cross-agent routing to protect, and the
    deployed LLM phrases the flow free-form from the caller's words
    (``"Make an appointment"``) — which no fixed ``_VALID_FLOWS`` set can match,
    so a strict gate rejects every caller with ``invalid_flow`` and the agent is
    DOA. So a single-flow gate accepts ANY non-empty flow and normalizes it to the
    one canonical flow (the engine only needs the gate filled). Multi-flow apps
    keep strict validation — there the exact flow name drives routing.
    """
    if len(flows) <= 1:
        canonical = repr(flows[0]) if flows else '"default"'
        return (
            '"""Activate this agent\'s slot-filling flow (agent-specific gate setter).\n\n'
            "Single-flow app: there is no cross-agent routing, so any non-empty flow\n"
            "fills the gate and is normalized to the one canonical flow. (The deployed\n"
            "model phrases the flow free-form, which a strict list can't match.)\n"
            '"""\n'
            "from typing import Any\n\n\n"
            'def set_active_flow(flow: str = "") -> dict[str, Any]:\n'
            '  """Activate a flow.\n\n'
            "  Args:\n"
            "    flow: The flow to activate.\n"
            "  Returns:\n"
            "    Dict with stored/value or error.\n"
            '  """\n'
            "  if not str(flow).strip():\n"
            '    return {"error": True, "error_code": "invalid_flow"}\n'
            f"  return {{\"stored\": True, \"value\": {canonical}}}\n"
        )
    valid = "{" + ", ".join(repr(f) for f in flows) + "}"
    return (
        '"""Activate this agent\'s slot-filling flow (agent-specific gate setter).\n\n'
        "Generated per agent: the valid flows are this agent's own. Fills the\n"
        "active_flow gate so deterministic collection can begin.\n"
        '"""\n'
        "from typing import Any\n\n"
        f"_VALID_FLOWS = {valid}\n\n\n"
        'def set_active_flow(flow: str = "") -> dict[str, Any]:\n'
        '  """Activate a flow.\n\n'
        "  Args:\n"
        "    flow: The flow to activate.\n"
        "  Returns:\n"
        "    Dict with stored/value or error.\n"
        '  """\n'
        "  flow = str(flow).lower().strip()\n"
        "  if flow not in _VALID_FLOWS:\n"
        '    return {"error": True, "error_code": "invalid_flow"}\n'
        '  return {"stored": True, "value": flow}\n'
    )


def _text_setter(fn: str, param: str, what: str) -> str:
    """A non-empty free-text setter."""
    return (
        f'"""Setter for {what}."""\n'
        "from typing import Any\n\n\n"
        f'def {fn}({param}: str = "") -> dict[str, Any]:\n'
        f'  """Record {what}.\n\n'
        "  Args:\n"
        f"    {param}: {what}.\n"
        "  Returns:\n"
        "    Dict with stored/value or error.\n"
        '  """\n'
        f"  value = str({param}).strip()\n"
        "  if not value:\n"
        '    return {"error": True, "error_code": "missing"}\n'
        '  return {"stored": True, "value": value}\n'
    )


def _int_setter(fn: str, param: str, what: str, lo: int, hi: int) -> str:
    """An integer-in-range setter (parses digits out of free text)."""
    return (
        f'"""Setter for {what}."""\n'
        "from typing import Any\n\n\n"
        f'def {fn}({param}: str = "") -> dict[str, Any]:\n'
        f'  """Record {what}.\n\n'
        "  Args:\n"
        f"    {param}: {what}.\n"
        "  Returns:\n"
        "    Dict with stored/value or error.\n"
        '  """\n'
        f"  raw = str({param}).strip()\n"
        '  digits = "".join(c for c in raw if c.isdigit())\n'
        "  if not digits:\n"
        '    return {"error": True, "error_code": "not_a_number"}\n'
        "  n = int(digits)\n"
        f"  if n < {lo} or n > {hi}:\n"
        '    return {"error": True, "error_code": "out_of_range"}\n'
        '  return {"stored": True, "value": n}\n'
    )


def _email_setter(fn: str, param: str, what: str) -> str:
    """An email setter (must contain ``@`` and a dot after it)."""
    return (
        f'"""Setter for {what}."""\n'
        "from typing import Any\n\n\n"
        f'def {fn}({param}: str = "") -> dict[str, Any]:\n'
        f'  """Record {what}.\n\n'
        "  Args:\n"
        f"    {param}: {what}.\n"
        "  Returns:\n"
        "    Dict with stored/value or error.\n"
        '  """\n'
        f"  value = str({param}).strip()\n"
        '  if "@" not in value or "." not in value.split("@")[-1]:\n'
        '    return {"error": True, "error_code": "bad_email"}\n'
        '  return {"stored": True, "value": value}\n'
    )


def _slot(name: str, setter: str, hint: str, ask: str, readback: bool = True) -> dict:
    """A populated user slot referencing its setter."""
    return {
        "name": name,
        "source": "user",
        "setter": setter,
        "hint": hint,
        "ask": ask,
        "requires_readback": readback,
    }


def _executor(fn: str, params: list[str], result_key: str, prefix: str, what: str) -> str:
    """A task EXECUTOR tool: takes the collected slot values (positionally), does
    the 'work' (a deterministic stub), and returns ``{<result_key>, success}``.

    Differs from a setter: it runs after the slots are filled and returns a
    result the task maps to an output slot (``success`` drives success_check).
    Replace the stub body with a real API call. Uses only ``from typing import
    Any``; ``hashlib`` keeps the stub id deterministic without imports of tools.
    """
    sig = ", ".join(f'{p}: str = ""' for p in params)
    arg_lines = "".join(f"    {p}: The collected {p.replace('_', ' ')}.\n" for p in params)
    joined = ' + "|" + '.join(f"str({p}).strip()" for p in params)
    return (
        f'"""Executor for {what}."""\n'
        "from typing import Any\n\n\n"
        f"def {fn}({sig}) -> dict[str, Any]:\n"
        f'  """Perform {what} and return a result.\n\n'
        "  Args:\n"
        f"{arg_lines}"
        "  Returns:\n"
        f"    Dict with {result_key} and a success flag.\n"
        '  """\n'
        f"  basis = {joined}\n"
        "  digits = sum(ord(c) for c in basis) % 10000\n"
        f'  {result_key} = "{prefix}-" + str(digits).zfill(4)\n'
        f'  return {{"{result_key}": {result_key}, "success": True}}\n'
    )


def _task(name: str, tool: str, inputs: list[str], result_key: str, then_text: str) -> dict:
    """A terminal task wiring the collected slots into an executor + a result slot.

    ``success_check`` is the result KEY the engine reads to decide the task
    succeeded (``result.get("success")``) — a plain string, not a dict.
    """
    return {
        "name": name,
        "tool": tool,
        "inputs": list(inputs),
        "outputs": {result_key: result_key},
        "success_check": "success",
        "terminal": True,
        "then_response": [{"type": "text", "text": then_text}],
    }


def _result_slot(name: str, task: str) -> dict:
    """A slot filled by a task's output (source ``task:<name>``)."""
    return {"name": name, "source": f"task:{task}"}


# Each template is a FULL DAG: user slots + their setters, a terminal task with
# an implemented executor tool that produces a result slot, and a one-click
# starter message for the Simulator. Keys mirror the client StartGallery cards.
TEMPLATES: dict[str, dict] = {
    "reservation": {
        "suggested_message": "table for 4 on Friday at 7pm under Alex",
        "slots": [
            _slot("party_size", "set_party_size", "Number of guests",
                  "How many people are in your party?"),
            _slot("reservation_date", "set_reservation_date", "Date of the visit",
                  "What date would you like?"),
            _slot("reservation_time", "set_reservation_time", "Time of the visit",
                  "What time works for you?"),
            _slot("guest_name", "set_guest_name", "Name for the reservation",
                  "What name should I put it under?"),
        ],
        "result_slots": [_result_slot("confirmation_number", "CreateReservation")],
        "tasks": [
            _task(
                "CreateReservation", "create_reservation",
                ["party_size", "reservation_date", "reservation_time", "guest_name"],
                "confirmation_number",
                "You're all set! Your reservation is booked — confirmation "
                "{confirmation_number}.",
            ),
        ],
        "setters": {
            "set_party_size": _int_setter(
                "set_party_size", "party_size", "the party size", 1, 50),
            "set_reservation_date": _text_setter(
                "set_reservation_date", "reservation_date", "the reservation date"),
            "set_reservation_time": _text_setter(
                "set_reservation_time", "reservation_time", "the reservation time"),
            "set_guest_name": _text_setter(
                "set_guest_name", "guest_name", "the guest name"),
        },
        "executors": {
            "create_reservation": _executor(
                "create_reservation",
                ["party_size", "reservation_date", "reservation_time", "guest_name"],
                "confirmation_number", "RES", "the reservation booking"),
        },
    },
    "appointment": {
        "suggested_message": "I'd like a haircut next Tuesday at 2pm, name Jordan",
        "slots": [
            _slot("service_type", "set_service_type", "Service requested",
                  "What service would you like to book?"),
            _slot("appointment_date", "set_appointment_date", "Preferred date",
                  "What date would you like to come in?"),
            _slot("appointment_time", "set_appointment_time", "Preferred time",
                  "What time works best?"),
            _slot("contact_name", "set_contact_name", "Name for the appointment",
                  "Whose name is the appointment under?"),
        ],
        "result_slots": [_result_slot("booking_id", "BookAppointment")],
        "tasks": [
            _task(
                "BookAppointment", "book_appointment",
                ["service_type", "appointment_date", "appointment_time", "contact_name"],
                "booking_id",
                "Booked! Your appointment is confirmed — reference {booking_id}.",
            ),
        ],
        "setters": {
            "set_service_type": _text_setter(
                "set_service_type", "service_type", "the requested service"),
            "set_appointment_date": _text_setter(
                "set_appointment_date", "appointment_date", "the preferred date"),
            "set_appointment_time": _text_setter(
                "set_appointment_time", "appointment_time", "the preferred time"),
            "set_contact_name": _text_setter(
                "set_contact_name", "contact_name", "the contact name"),
        },
        "executors": {
            "book_appointment": _executor(
                "book_appointment",
                ["service_type", "appointment_date", "appointment_time", "contact_name"],
                "booking_id", "APT", "the appointment booking"),
        },
    },
    "lead_capture": {
        "suggested_message": "Hi, I'm Sam from Acme, sam@acme.com — we need a demo",
        "slots": [
            _slot("contact_name", "set_contact_name", "Lead's name",
                  "Who do I have the pleasure of speaking with?"),
            _slot("company", "set_company", "Company name",
                  "What company are you with?"),
            _slot("email", "set_email", "Work email", "What's the best email to reach you?"),
            _slot("interest", "set_interest", "What they're interested in",
                  "What are you looking to solve?", readback=False),
        ],
        "result_slots": [_result_slot("lead_id", "SubmitLead")],
        "tasks": [
            _task(
                "SubmitLead", "submit_lead",
                ["contact_name", "company", "email", "interest"],
                "lead_id",
                "Thanks! We've logged your details — reference {lead_id}. "
                "Someone will be in touch.",
            ),
        ],
        "setters": {
            "set_contact_name": _text_setter(
                "set_contact_name", "contact_name", "the lead's name"),
            "set_company": _text_setter("set_company", "company", "the company name"),
            "set_email": _email_setter("set_email", "email", "the work email"),
            "set_interest": _text_setter(
                "set_interest", "interest", "the area of interest"),
        },
        "executors": {
            "submit_lead": _executor(
                "submit_lead",
                ["contact_name", "company", "email", "interest"],
                "lead_id", "LEAD", "the lead submission"),
        },
    },
    "support_triage": {
        "suggested_message": "My order hasn't arrived, it's urgent, account A123",
        "slots": [
            _slot("issue_category", "set_issue_category", "Type of issue",
                  "What kind of issue are you running into?"),
            _slot("urgency", "set_urgency", "How urgent it is",
                  "How urgent is this for you?"),
            _slot("account_id", "set_account_id", "Account identifier",
                  "What's your account ID or email?"),
            _slot("description", "set_description", "Details of the issue",
                  "Can you describe what's happening?", readback=False),
        ],
        "result_slots": [_result_slot("ticket_id", "RouteTicket")],
        "tasks": [
            _task(
                "RouteTicket", "route_ticket",
                ["issue_category", "urgency", "account_id", "description"],
                "ticket_id",
                "Got it — I've opened ticket {ticket_id} and routed it to the "
                "right team.",
            ),
        ],
        "setters": {
            "set_issue_category": _text_setter(
                "set_issue_category", "issue_category", "the issue category"),
            "set_urgency": _text_setter("set_urgency", "urgency", "the urgency"),
            "set_account_id": _text_setter(
                "set_account_id", "account_id", "the account identifier"),
            "set_description": _text_setter(
                "set_description", "description", "the issue description"),
        },
        "executors": {
            "route_ticket": _executor(
                "route_ticket",
                ["issue_category", "urgency", "account_id", "description"],
                "ticket_id", "TKT", "the support ticket"),
        },
    },
}


def template_tools(template: str | None) -> dict[str, str]:
    """All tool sources for a template — setters + executors ({} if none)."""
    if not template:
        return {}
    entry = TEMPLATES.get(template, {})
    return {**entry.get("setters", {}), **entry.get("executors", {})}


# Back-compat alias: callers that only want the setter map (AI Accept override).
def template_setters(template: str | None) -> dict[str, str]:
    """The ``{setter_name: python_code}`` map for a template (``{}`` if none)."""
    if not template:
        return {}
    return dict(TEMPLATES.get(template, {}).get("setters", {}))


def template_suggested_message(template: str | None) -> str:
    """The Simulator starter message for a template (``""`` if none)."""
    if not template:
        return ""
    return TEMPLATES.get(template, {}).get("suggested_message", "")


def starter_config(config_id: str, *, template: str | None = None) -> dict:
    """The starter ``<config_id>_dag`` config: the gate floor + any template DAG.

    Pure data; mirrors the framework's gate/bootstrap shape so it passes
    ``validate_dag_config`` and is a working starting point for the author. With
    a ``template`` key, the template's populated user slots, result slots, and
    terminal task are appended on top of the gate floor (the setter + executor
    tool sources are emitted separately by :func:`build`).
    """
    cfg = {
        "bootstrap": {
            "tool": "set_active_flow",
            "slot": "active_flow",
            "reset_on_complete": True,
        },
        "gate_slot": "active_flow",
        "slots": [
            {
                "name": "active_flow",
                "source": "user",
                "setter": "set_active_flow",
                "hint": "Which flow the user wants",
                "requires_readback": False,
            },
        ],
        "tasks": [],
    }
    if template and template in TEMPLATES:
        entry = TEMPLATES[template]
        slots = [dict(s) for s in entry["slots"]]
        cfg["slots"].extend(slots)
        cfg["slots"].extend(dict(s) for s in entry.get("result_slots", []))
        cfg["tasks"] = [dict(t) for t in entry.get("tasks", [])]
        if any(s.get("requires_readback") for s in slots):
            cfg["readback_response"] = [
                {"type": "text", "text": "Here's what I have so far — does this look right?"}
            ]
    return cfg


def _starter_dag_code(config_id: str, cfg: dict | None = None) -> str:
    """Render the zero-arg ``<config_id>_dag`` python_function source.

    Comments stay generic — no app-specific slot names beyond the framework's
    own gate slot. ``cfg`` defaults to the empty starter floor; pass an explicit
    config dict (template or AI-proposed) to render that instead.
    """
    if cfg is None:
        cfg = starter_config(config_id)
    # A compact JSON STRING parsed at call time, not a Python literal, and the reason is
    # latency. CES loads this module on every invocation — the engine's own module-level
    # caches come back empty each time, which is how we know — so whatever is here is paid
    # per call, and a real app calls its dag tool several times per turn.
    #
    # A pretty-printed literal makes CPython compile thousands of AST nodes; one string
    # constant plus `json.loads` is a single node and a C-speed parse. For the largest
    # config measured that is 266KB of source down to 85KB, and a 17-config set 362KB
    # down to 129KB.
    #
    # The comment this replaces warned that JSON booleans would `NameError` as source.
    # That is true of a bare literal and does not apply to `json.loads`, which returns
    # real `True`/`False`/`None`. Lambdas are already quoted strings the framework
    # compiles on load, so they survive the round trip like any other string.
    #
    # Sound only while the config is JSON-representable, which is not free: a tuple
    # introduced upstream would round-trip to a list SILENTLY and the agent would run with
    # a config subtly unlike the authored one. Checked here rather than left to a test,
    # because it costs one comparison per BUILD and the failure it prevents is invisible.
    cfg_json = json.dumps(cfg, separators=(",", ":"), ensure_ascii=False)
    if json.loads(cfg_json) != cfg:
        raise ValueError(
            f"config {config_id!r} is not JSON-representable, so emitting it would "
            "change it. The usual cause is a tuple or a set where a list is meant.")
    return (
        '"""Slot-filling DAG configuration for the starter agent."""\n\n'
        "import json\n"
        "from typing import Any\n\n"
        f"_CONFIG = {cfg_json!r}\n\n\n"
        f"def {config_id}_dag() -> dict[str, Any]:\n"
        '  """Return the DAG config (slots, tasks, gate) for this agent."""\n'
        "  return json.loads(_CONFIG)\n"
    )


def _starter_dag_json(config_id: str) -> str:
    tool = f"{config_id}_dag"
    return json.dumps(
        {
            "name": str(uuid.uuid4()),
            "pythonFunction": {
                "name": tool,
                "pythonCode": f"tools/{tool}/python_function/python_code.py",
                "description": (
                    "Zero-argument DAG configuration for the slot-filling "
                    "framework. Returns the slots/tasks/gate dict."
                ),
            },
            "displayName": tool,
        },
        indent=2,
    )


def _setter_tool_json(
    setter_name: str,
    description: str | None = None,
    asynchronous: bool = False,
    timeout_seconds: int | None = None,
) -> str:
    """The ``<setter>.json`` for a template setter — fresh UUID, no params block.

    ``asynchronous`` emits ``executionType: ASYNCHRONOUS``. The key is omitted entirely
    otherwise (CES defaults to SYNCHRONOUS), so existing apps emit byte-identical JSON.

    ``timeout_seconds`` emits ``timeout: "<n>s"``, the seconds CES allows the body to
    run. Also omitted when absent, for the same reason — but the default it falls back
    to is 60 seconds, and overrunning it is silent, so a slow tool that never declares
    one simply stops reporting.
    """
    doc: dict = {
        "name": str(uuid.uuid4()),
        "pythonFunction": {
            "name": setter_name,
            "pythonCode": f"tools/{setter_name}/python_function/python_code.py",
            "description": description or f"Record the value for {setter_name}.",
        },
        "displayName": setter_name,
    }
    if asynchronous:
        doc["executionType"] = "ASYNCHRONOUS"
    if timeout_seconds:
        doc["timeout"] = f"{int(timeout_seconds)}s"
    return json.dumps(doc, indent=2)


def _remote_agent_tool_json(spec: dict) -> str:
    """The ``<name>.json`` for a remote A2A agent — a ``remoteAgentTool`` resource.

    ``remoteAgentTool`` is a ``tool_type`` oneof sibling of ``pythonFunction``, so the
    resource carries no python body and no ``python_function/`` dir is emitted for it.
    ``executionType`` is stated explicitly to match what a live app round-trips.
    """
    return json.dumps(
        {
            "name": str(uuid.uuid4()),
            "executionType": "SYNCHRONOUS",
            "displayName": spec["name"],
            "remoteAgentTool": spec,
        },
        indent=2,
    )


def _agent_tool_json(spec: dict) -> str:
    """The ``<name>.json`` for an agent called as a tool — an ``agentTool`` resource.

    The sibling of ``remoteAgentTool``: same body-less shape, but it names an agent in
    THIS app rather than carrying an A2A card, and it is the only one of the two that can
    be asynchronous — an async ``remoteAgentTool`` is dropped at deploy without a word
    (ces-probes 133), while this one defers and completes (ces-probes 134). So
    ``executionType`` is read from the request rather than hardcoded.
    """
    payload = {k: v for k, v in spec.items() if k != "asynchronous"}
    return json.dumps(
        {
            "name": str(uuid.uuid4()),
            "executionType": (
                "ASYNCHRONOUS" if spec.get("asynchronous") else "SYNCHRONOUS"),
            "displayName": spec["name"],
            "agentTool": payload,
        },
        indent=2,
    )


def _agent_tool_files(req, ScaffoldFile) -> list:
    """One ``tools/<name>/<name>.json`` per declared agent tool.

    Only for tools this app OWNS. A declaration made with ``emit=False`` never reaches
    the request: it describes a resource the app already carries, and writing one would
    overwrite whatever that resource holds — for a converted agent, its
    ``toolFakeConfig`` and the whole mocked path with it.
    """
    return [
        ScaffoldFile(
            path=f"tools/{spec['name']}/{spec['name']}.json",
            content=_agent_tool_json(spec),
        )
        for spec in (getattr(req, "agent_tools", None) or [])
    ]


def _helper_agent_files(req, ScaffoldFile) -> list:
    """One ``agents/<name>/`` per declared helper agent: its JSON and its instruction."""
    out = []
    for spec in (getattr(req, "helper_agents", None) or []):
        name = spec["name"]
        body = {k: v for k, v in spec.items() if k != "instruction"}
        body["instruction"] = f"agents/{name}/instruction.txt"
        out.append(ScaffoldFile(path=f"agents/{name}/{name}.json",
                                content=json.dumps(body, indent=2)))
        out.append(ScaffoldFile(path=f"agents/{name}/instruction.txt",
                                content=spec["instruction"]))
    return out


def _search_tool_json(spec: dict) -> str:
    """The ``<name>.json`` for a Google Search tool — a ``googleSearchTool`` resource.

    ``googleSearchTool`` is a ``tool_type`` oneof sibling of ``pythonFunction``, so the
    resource carries no python body and no ``python_function/`` dir is emitted for it.
    Verified live (ces-probes 24): a tool dir holding only this JSON deploys and the
    model searches with it.
    """
    return json.dumps(
        {
            "name": str(uuid.uuid4()),
            "executionType": "SYNCHRONOUS",
            "displayName": spec["name"],
            "googleSearchTool": spec,
        },
        indent=2,
    )


def _search_tool_names(req) -> set:
    """Tool names of the request's Google Search tools.

    Body-less like a remote agent, so every availability check that scans emitted
    bodies has to be told about them separately or it reads a task firing one as a
    reference to a tool that does not exist.
    """
    return {spec["name"] for spec in (getattr(req, "search_tools", None) or [])}


def _search_tool_files(req, ScaffoldFile) -> list:
    """One ``tools/<name>/<name>.json`` per declared Google Search tool."""
    return [
        ScaffoldFile(
            path=f"tools/{spec['name']}/{spec['name']}.json",
            content=_search_tool_json(spec),
        )
        for spec in (getattr(req, "search_tools", None) or [])
    ]


def _agent_tool_names(req) -> set:
    """Tool names of the request's agent tools, EMITTED or carried.

    Body-less like a remote agent, so every availability check that scans emitted bodies
    has to be told about them separately. Carried ones count too: the resource exists in
    the app even though this build did not write it, so a task firing one is a valid
    reference and must not read as a dangling tool.
    """
    names = {t["name"] for t in (getattr(req, "agent_tools", None) or [])}
    names |= set(getattr(req, "carried_agent_tools", None) or [])
    return names


def _remote_agent_names(req) -> set:
    """Tool names of the request's remote A2A agents.

    They are callable tools with no python body, so every availability check that
    scans emitted bodies has to be told about them separately or it reads a task
    firing one as a reference to a tool that does not exist.
    """
    return {
        spec["name"] for spec in (getattr(req, "remote_agent_tools", None) or [])
    }


def _remote_agent_files(req, ScaffoldFile) -> list:
    """One ``tools/<name>/<name>.json`` per declared remote A2A agent."""
    return [
        ScaffoldFile(
            path=f"tools/{spec['name']}/{spec['name']}.json",
            content=_remote_agent_tool_json(spec),
        )
        for spec in (getattr(req, "remote_agent_tools", None) or [])
    ]


def _toolset_files(req, ScaffoldFile) -> list:
    """The ``toolsets/`` tree + ``environment.json`` for the request's toolsets.

    A toolset is a DIFFERENT resource kind from a tool — it lives under ``toolsets/``,
    not ``tools/``. An OpenAPI toolset carries its spec in a second file; an MCP toolset
    has none (CES discovers its tools from the server), so a payload without a ``spec``
    key emits the resource alone. Nothing here lands in an agent's ``tools[]``: an agent
    cannot call a toolset, only the generated wrappers that forward to it (see
    ``flows.authoring.openapi`` / ``flows.authoring.mcp``).
    """
    files: list = []
    for entry in getattr(req, "toolsets", None) or []:
        name = entry["name"]
        resource = {"name": str(uuid.uuid4()), **entry["resource"]}
        files.append(
            ScaffoldFile(
                path=f"toolsets/{name}/{name}.json",
                content=json.dumps(resource, indent=2),
            )
        )
        spec = entry.get("spec")
        if spec is not None:
            files.append(
                ScaffoldFile(
                    path=f"toolsets/{name}/open_api_toolset/open_api_schema.yaml",
                    content=spec,
                )
            )
    env = getattr(req, "environment_json", None)
    if env:
        files.append(ScaffoldFile(path="environment.json", content=env))
    return files


def _guardrail_files(req, ScaffoldFile) -> list:
    """The ``guardrails/`` tree for the request's guardrails.

    A guardrail is its own resource kind: nothing calls it, and unlike a toolset it IS
    referenced by name — from ``app.json``'s ``guardrails`` array and from each agent
    that scopes it. Those arrays are written elsewhere (the app-settings step and the
    agent JSONs); this only lays down the resources they point at. A name in an array
    with no resource here is not an error anywhere — it is simply a guardrail that never
    applies — which is why the build warns about it instead.
    """
    files: list = []
    for entry in getattr(req, "guardrails", None) or []:
        stem = entry["dir"]
        resource = {"name": _guardrail_uuid(entry["name"]), **entry["resource"]}
        files.append(
            ScaffoldFile(
                path=f"guardrails/{stem}/{stem}.json",
                content=json.dumps(resource, indent=2),
            )
        )
    return files


def _guardrail_uuid(display_name: str) -> str:
    """A STABLE resource id for a guardrail, derived from its display name.

    Deliberately not ``uuid4``. A fresh id on every build is the mistake the app and root
    agent already carry a pinning option for: CES treats a changed id as a NEW resource,
    so a re-push creates a second guardrail and orphans the first instead of updating it
    (see ``ScaffoldRequest.app_uuid``). Every guardrail pulled from a live app has a
    stable id, and the same id recurs across apps derived from one another — ids are
    scoped to their app, so deriving one from the display name collides with nothing.

    The display name is the right key because it already IS the identity: ``app.json``
    and the agent JSONs reference a guardrail by display name, never by id.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"ces-guardrail:{display_name}"))


# Per-tool tool-json descriptions for generated tools that aren't plain setters.
_TOOL_DESCRIPTIONS = {
    "set_active_flow": (
        "Activate the slot-filling flow the user wants, so structured "
        "collection can begin."
    ),
}

# Progressive fan-out generates three kinds of engine-owned tool, all named by
# convention (see flows/emit/fanout.py). The setter default — "Record the value for
# <name>" — is right for a setter and actively wrong for these: a WATCHER described as
# a recorder is an invitation to call it, and calling it costs a reasoning pass and
# blocks for twenty seconds. They are hidden from the model on every turn, so this is
# belt to that braces; a description nothing reads is still one that should not lie.
_GENERATED_SUFFIX_DESCRIPTIONS = (
    ("leg", "Run one leg of a parallel diagnostic group and publish its result where"
             " the framework can narrate it. Engine-dispatched; never call directly."),
    ("peek", "Report which legs of a parallel diagnostic group have published a"
              " result. Engine-dispatched; never call directly."),
    ("watch", "Block until a leg of a parallel diagnostic group finishes."
               " Engine-dispatched; never call directly — it holds for many seconds."),
)


def _tool_description(name: str, code: str = "") -> str | None:
    """The tool-json description for a generated tool, or None for the setter default.

    Keyed on the marker the fan-out codegen stamps into the BODY, not on the tool name:
    an author's own `check_watch` must not be described as an engine-owned watcher.
    """
    if name in _TOOL_DESCRIPTIONS:
        return _TOOL_DESCRIPTIONS[name]
    for kind, text in _GENERATED_SUFFIX_DESCRIPTIONS:
        if f"{fanout.MARKER} {kind}" in (code or ""):
            return text
    return None


def _validate_starter(cfg: dict, available_tools: list[str]) -> dict:
    """Lint the starter dag with the canonical design-time linter.

    Delegates to :func:`blessed_source.lint_config` (the one linter) — the DAG
    validator is design-time only, never deployed into the agent. Returns the
    verdict dict ``{valid, errors, warnings}``.
    """
    return blessed_source.lint_config(cfg, available_tools)


def _agent_callback_files(
    agent_name: str, callbacks: dict, names: list[str] | None = None
) -> list[tuple[str, bytes]]:
    """Callback copies under one agent dir (byte-identical to blessed).

    ``names`` limits which callbacks to emit (default all 4) — a host router carries
    only ``before_model`` + ``after_tool``. Paths resolve through
    ``blessed_source.callback_rel_path`` so the on-disk layout stays in one place.
    """
    names = names or list(blessed_source._CALLBACK_NAMES)
    return [
        (blessed_source.callback_rel_path(agent_name, name), callbacks[name])
        for name in names
    ]


def _callback_files(req, callbacks: dict) -> list[tuple[str, bytes]]:
    """The 4 callback copies under the (single) root agent dir."""
    return _agent_callback_files(req.root_agent, callbacks)


def _tool_names_from_files(framework_files: list[dict]) -> list[str]:
    """Extract the framework tool human names from their ``<tool>.json`` files."""
    names: list[str] = []
    for f in framework_files:
        if f["path"].endswith(".json"):
            try:
                names.append(json.loads(f["content"])["displayName"])
            except (KeyError, ValueError):
                continue
    return names


#: Framework tools whose deployed artifact may ship as precompiled bytecode, mapped to
#: their entry-point function. Only the engine for now: it is 555KB / 49,860 AST nodes and
#: costs 98-153ms per invocation to re-parse (ces-probes 164), while the other framework
#: tools are small enough that packing them would trade readability for nothing.
_PACKABLE = {
    "tools/slot_filling_engine/python_function/python_code.py": (
        "slot_filling_engine", "Slot-filling DAG engine."),
}

#: Opt-in, and deliberately not the default yet. Packing requires the BUILD host to run the
#: same python minor version as CES (3.12), which most contributor machines do not, and a
#: silent fall back to source would mean believing a deploy was fast when it was not. Off:
#: byte-identical to today. On: `packing.pack` raises on a mismatched interpreter rather
#: than degrading quietly.
_PACK_ENV = "FLOWS_PACK_ENGINE"


def _maybe_pack(files: list) -> list:
    """Return ``files`` with packable framework tools replaced by their packed form.

    Runs AFTER the manifest drift gate, never before: the gate's job is to prove the
    framework bytes are canonical, and it must see the canonical bytes to do it. The packed
    artifact stays verifiable afterwards because it embeds its own source, which
    ``blessed_source._canonical`` recovers.
    """
    if os.environ.get(_PACK_ENV, "").strip().lower() not in ("1", "true", "yes", "on"):
        return files
    from ..engine import packing
    from .models import ScaffoldFile

    out = []
    for f in files:
        spec = _PACKABLE.get(f.path)
        if spec is None:
            out.append(f)
            continue
        entry, title = spec
        out.append(ScaffoldFile(path=f.path, content=packing.pack(f.content, entry, title)))
    return out


def _write_tree(target_path: str, files: list, callback_files: list) -> str:
    """Atomically write the file SET under ``target_path``.

    Writes everything into a sibling temp dir first, then ``os.replace``s it into
    place. Refuses to overwrite a non-empty existing dir. On any failure the temp
    dir is removed so nothing partial is left behind.
    """
    target_path = os.path.abspath(os.path.expanduser(target_path))
    if os.path.isdir(target_path) and os.listdir(target_path):
        raise FileExistsError(
            f"Refusing to overwrite non-empty directory: {target_path}"
        )

    parent = os.path.dirname(target_path) or "."
    os.makedirs(parent, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".scaffold_", dir=parent)
    try:
        for sf in files:
            _write_one(tmp, sf.path, sf.content.encode("utf-8"))
        for rel, content in callback_files:
            _write_one(tmp, rel, content)
        # If an empty target dir already exists, drop it so os.replace works.
        if os.path.isdir(target_path):
            os.rmdir(target_path)
        os.replace(tmp, target_path)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return target_path


def _write_one(root: str, rel_path: str, content: bytes) -> None:
    dest = os.path.join(root, rel_path)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(content)


def build(req):
    """Build (and, in local mode, write) the app file SET for ``req``.

    Returns a :class:`slot_studio.routers.agent.ScaffoldResult`. Never raises.
    """
    from .models import ScaffoldFile, ScaffoldResult, ValidationReportLite

    try:
        callbacks = blessed_source.callbacks()
        framework_files = blessed_source.framework_tool_files()
        framework_tool_names = _tool_names_from_files(framework_files)

        config_id = req.config_id
        dag_tool = f"{config_id}_dag"
        template = getattr(req, "template", None)
        config_override = getattr(req, "config_override", None)
        setters_override = getattr(req, "setters_override", None)
        # The dag config + setters come from an explicit override (AI "describe
        # your own" Accept) or from the template (or the empty floor).
        if config_override is not None:
            # A config_override arrives as JSON (AI "describe your own", or a
            # round-tripped Pydantic config) where every unset optional field is
            # an explicit null. The framework validator/engine read list fields as
            # `slot.get("requires", [])` — which returns None (not []) when the key
            # is present-but-null and then crashes on iteration — so strip nulls
            # to the shape a hand-authored *_dag() would produce.
            cfg = _strip_none(dict(config_override))
            setters = dict(setters_override or {})
            suggested = ""
        else:
            cfg = starter_config(config_id, template=template)
            setters = template_tools(template)  # setters + task executors
            suggested = template_suggested_message(template)

        # set_active_flow is agent-specific (its valid flows are this agent's own),
        # so generate it per-agent into the emitted tool set rather than pulling a
        # bella-notte-flavored copy from the framework bundle.
        setters["set_active_flow"] = _active_flow_setter(
            _active_flow_names(config_id, cfg, template)
        )

        # MULTI-DAG bundle + arbitrary pre-authored tool bodies (additive; both
        # empty by default so the single-DAG / setters-only path is unchanged).
        extra_configs = dict(getattr(req, "extra_configs", None) or {})
        emit_tools = {**setters, **(getattr(req, "tools_override", None) or {})}
        dag_tools = [dag_tool] + [f"{cid}_dag" for cid in extra_configs]

        # Assemble the whole file SET (app-root-relative paths).
        files: list = []
        files.append(ScaffoldFile(path="app.json", content=_app_json(req)))
        files.append(ScaffoldFile(path="gecx-config.json", content=_gecx_json(req)))
        files.append(
            ScaffoldFile(
                path=f"agents/{req.root_agent}/{req.root_agent}.json",
                content=_agent_json(
                    req, sorted(set(framework_tool_names) | set(emit_tools)),
                    dag_tools=dag_tools,
                ),
            )
        )
        files.append(
            ScaffoldFile(
                path=f"agents/{req.root_agent}/instruction.txt",
                content=_instruction_txt(req),
            )
        )
        for f in framework_files:
            files.append(ScaffoldFile(path=f["path"], content=f["content"]))
        # Starter <config_id>_dag tool (json + zero-arg python_function).
        files.append(
            ScaffoldFile(
                path=f"tools/{dag_tool}/{dag_tool}.json",
                content=_starter_dag_json(config_id),
            )
        )
        files.append(
            ScaffoldFile(
                path=f"tools/{dag_tool}/python_function/python_code.py",
                content=_starter_dag_code(config_id, cfg),
            )
        )
        # Extra dag tools for a multi-DAG bundle (one <id>_dag per extra config).
        for cid, cfg2 in extra_configs.items():
            files.append(
                ScaffoldFile(path=f"tools/{cid}_dag/{cid}_dag.json", content=_starter_dag_json(cid))
            )
            files.append(
                ScaffoldFile(
                    path=f"tools/{cid}_dag/python_function/python_code.py",
                    content=_starter_dag_code(cid, cfg2),
                )
            )
        # Tool bodies (json + python_function): template/override setters PLUS any
        # arbitrary pre-authored tools (task executors, prereq tools).
        # Tools the author marked ASYNCHRONOUS get `executionType` on their resource.
        _async = set(getattr(req, "async_tools", None) or ())
        _timeouts = dict(getattr(req, "tool_timeouts", None) or {})
        # Author-supplied model-facing descriptions (from a slot's `description=`); fall
        # back to the generated/default when a tool is not named.
        _descs = dict(getattr(req, "tool_descriptions", None) or {})
        for setter_name, setter_code in emit_tools.items():
            files.append(
                ScaffoldFile(
                    path=f"tools/{setter_name}/{setter_name}.json",
                    content=_setter_tool_json(
                        setter_name,
                        _descs.get(setter_name) or _tool_description(setter_name, setter_code),
                        asynchronous=setter_name in _async,
                        timeout_seconds=_timeouts.get(setter_name),
                    ),
                )
            )
            files.append(
                ScaffoldFile(
                    path=f"tools/{setter_name}/python_function/python_code.py",
                    content=setter_code,
                )
            )

        # Remote A2A agents — tool json only, no python body (see the renderer).
        files.extend(_remote_agent_files(req, ScaffoldFile))
        files.extend(_search_tool_files(req, ScaffoldFile))
        files.extend(_agent_tool_files(req, ScaffoldFile))
        files.extend(_helper_agent_files(req, ScaffoldFile))

        # OpenAPI toolsets — the toolsets/ tree, a resource kind of its own.
        files.extend(_toolset_files(req, ScaffoldFile))
        files.extend(_guardrail_files(req, ScaffoldFile))

        # Golden eval smoke set (2-3 live scenarios) for templates that ship one,
        # so a generated agent comes with `cxas push-eval`-ready golden evals.
        for gf in golden_evals.golden_eval_files(template, config_id):
            files.append(ScaffoldFile(path=gf["path"], content=gf["content"]))

        # The 4 byte-identical callback copies under the agent dir. They are part
        # of the app file SET (so the hosted working copy + push payload carry
        # them, and prepush callback_sync can verify them), not just an on-disk
        # side effect. Callbacks are bytes (Python source → utf-8); decode for the
        # str-based ScaffoldFile content.
        callback_files = _callback_files(req, callbacks)
        for cb_path, cb_bytes in callback_files:
            files.append(ScaffoldFile(path=cb_path, content=cb_bytes.decode("utf-8")))

        # --- Invariants the result reports. ---------------------------------
        # UUID uniqueness across every emitted tool/app/agent json.
        tool_uuids = _collect_uuids(files)
        uuids_unique = len(tool_uuids) == len(set(tool_uuids))

        # Callback byte-sync vs blessed (the copies we just emitted must match).
        callback_sync_ok = all(
            content == callbacks[name]
            for name, content in zip(
                ("before_agent", "before_model", "after_model", "after_tool"),
                [c for _, c in callback_files],
            )
        )

        # Stronger gate: EVERY emitted framework file (all 13 tools + the 4
        # callbacks) must match the canonical manifest, so the wizard can never
        # ship an off-manifest framework file. Folds into callback_sync_ok (the
        # superset) — callbacks are a subset of what verify checks.
        framework_in_sync = blessed_source.verify_files(
            {f.path: f.content for f in files}
        ).ok
        callback_sync_ok = callback_sync_ok and framework_in_sync

        # validate_dag_config on the primary dag (multi-DAG cross-validation is
        # handled by the push bundle gate). available_tools spans the whole set so
        # a bundle's cross-references don't false-fail this single-config check.
        available_tools = sorted(
            set(framework_tool_names) | set(dag_tools) | set(emit_tools)
            | _remote_agent_names(req)
            | _search_tool_names(req)
            | _agent_tool_names(req)
        )
        verdict = _validate_starter(cfg, available_tools)
        validation = ValidationReportLite(
            valid=bool(verdict.get("valid")),
            errors=list(verdict.get("errors", [])),
            warnings=list(verdict.get("warnings", [])),
        )

        # Pack AFTER the drift gate above: it must hash the canonical framework bytes.
        # The packed artifact stays verifiable because it embeds its own source.
        files = _maybe_pack(files)

        # --- Local write (only when a target path is supplied). -------------
        written_to = None
        if req.mode == "local" and req.target_path:
            written_to = _write_tree(req.target_path, files, callback_files)

        ok = bool(validation.valid and callback_sync_ok and uuids_unique)
        return ScaffoldResult(
            ok=ok,
            files=files,
            written_to=written_to,
            validation=validation,
            callback_sync_ok=callback_sync_ok,
            uuids_unique=uuids_unique,
            framework_version=blessed_source.version(),
            config=cfg,
            suggested_message=suggested or None,
            error=None,
        )
    except Exception as exc:  # crash-envelope: never raise out
        logger.warning("scaffold.build failed: %s", exc)
        return ScaffoldResult(
            ok=False,
            files=[],
            written_to=None,
            validation=ValidationReportLite(
                valid=False, errors=[str(exc)], warnings=[]
            ),
            callback_sync_ok=False,
            uuids_unique=False,
            error=f"Scaffold failed: {exc}",
        )


# ---------------------------------------------------------------------------
# Multi-agent scaffolder — host/steering agent + N slot-filling sub-agents.
#
# A sibling to ``build()`` (single-agent), sharing every private helper. Emits one
# ``agents/<name>/`` dir per agent: sub-agents run the slot-filling engine (the
# canonical 4 callbacks + scoped tools + their ``<config>_dag``); the host is either
# a non-slot-filling router (``strategy="transfer"`` — custom before_model/after_tool
# + ``childAgents``) or an engine-running router (``strategy="engine"`` — the 4
# framework callbacks + its own router DAG). ``build()`` is untouched, so single-agent
# apps emit byte-identically.
# ---------------------------------------------------------------------------


def _var_declarations_multi(req) -> list[dict]:
    """Framework variableDeclarations for a multi-agent app.

    Starts from the canonical set, then overrides ``agent_config_map`` (the
    name->config_id map). ``default_config_id`` is kept only for the ``engine``
    strategy (the transfer host runs no engine, so it needs none — matches
    bella_notte); ``intent_config_map`` is added when the engine host routes on an
    upstream intent tag.
    """
    base = _var_declarations(req.host.name, "")
    out: list[dict] = []
    for v in base:
        if v["name"] == "agent_config_map":
            v = {**v, "schema": {**v["schema"], "default": json.dumps(req.agent_config_map)}}
        elif v["name"] == "default_config_id":
            if not req.default_config_id:
                continue  # transfer strategy: omit
            v = {**v, "schema": {**v["schema"], "default": req.default_config_id}}
        out.append(v)
    if req.intent_config_map:
        out.append({
            "name": "intent_config_map",
            "description": (
                "Maps an upstream ENTRY_INTENT tag to a DAG config id for turn-1 "
                "routing (before any LLM/transfer)."
            ),
            "schema": {"type": "STRING", "default": json.dumps(req.intent_config_map)},
        })
    return out


def _app_json_multi(req) -> str:
    return json.dumps(
        {
            "name": getattr(req, "app_uuid", None) or str(uuid.uuid4()),
            "displayName": req.app_display_name,
            "rootAgent": req.host.name,
            "modelSettings": {"model": req.model},
            "variableDeclarations": _var_declarations_multi(req),
        },
        indent=2,
    )


def _framework_callback_blocks(agent_name: str) -> dict:
    """The 4 framework callback registrations for a slot-filling agent JSON."""
    cb = f"agents/{agent_name}"
    return {
        "beforeAgentCallbacks": [{
            "pythonCode": f"{cb}/before_agent_callbacks/before_agent_callbacks_01/python_code.py",
            "description": "SM initialization and config resolution",
        }],
        "beforeModelCallbacks": [{
            "pythonCode": f"{cb}/before_model_callbacks/before_model_callbacks_01/python_code.py",
            "description": "DAG engine orchestration",
        }],
        "afterToolCallbacks": [{
            "pythonCode": f"{cb}/after_tool_callbacks/after_tool_callbacks_01/python_code.py",
            "description": "Setter and executor state management",
        }],
        "afterModelCallbacks": [{
            "pythonCode": f"{cb}/after_model_callbacks/after_model_callbacks_01/python_code.py",
            "description": "Payload injection for non-preempted turns",
        }],
    }


def _child_agent_json(agent) -> str:
    """A slot-filling sub-agent JSON: scoped tools + the 4 framework callbacks."""
    cb_base = f"agents/{agent.name}"
    body = {
        "name": str(uuid.uuid4()),
        "displayName": agent.name,
        "instruction": f"{cb_base}/instruction.txt",
        "tools": sorted(set(agent.tools)),
    }
    # Guardrails scoped to this specialist alone. The resource lives once under
    # `guardrails/`; the agent only names it.
    if getattr(agent, "guardrails", None):
        body["guardrails"] = list(agent.guardrails)
    body.update(_framework_callback_blocks(agent.name))
    return json.dumps(body, indent=2)


def _host_agent_json(host) -> str:
    """The host/steering agent JSON. ``transfer`` = router (2 custom callbacks +
    childAgents); ``engine`` = engine-running host (the 4 framework callbacks)."""
    cb_base = f"agents/{host.name}"
    body = {
        "name": str(uuid.uuid4()),
        "displayName": host.name,
        "instruction": f"{cb_base}/instruction.txt",
        "tools": sorted(set(host.tools)),
        "childAgents": list(host.child_agents),
    }
    if getattr(host, "guardrails", None):
        body["guardrails"] = list(host.guardrails)
    if host.strategy == "transfer":
        body["beforeModelCallbacks"] = [{
            "pythonCode": f"{cb_base}/before_model_callbacks/before_model_callbacks_01/python_code.py",
            "description": "Host router: dispatch a pending specialist transfer",
        }]
        body["afterToolCallbacks"] = [{
            "pythonCode": f"{cb_base}/after_tool_callbacks/after_tool_callbacks_01/python_code.py",
            "description": "Host router: capture set_active_flow and queue the transfer",
        }]
    else:
        body.update(_framework_callback_blocks(host.name))
    return json.dumps(body, indent=2)


def build_multi_agent(req):
    """Build (and, in local mode, write) a host + N sub-agents app for ``req``.

    Returns a :class:`ScaffoldResult`. Never raises (crash-envelope, like ``build``).
    """
    from .models import ScaffoldFile, ScaffoldResult, ValidationReportLite

    try:
        callbacks = blessed_source.callbacks()
        host_cbs = blessed_source.host_router_callbacks()
        framework_files = blessed_source.framework_tool_files()
        framework_tool_names = _tool_names_from_files(framework_files)

        all_configs = dict(req.all_configs)
        emit_tools = dict(req.tools_override or {})

        files: list = []
        files.append(ScaffoldFile(path="app.json", content=_app_json_multi(req)))
        files.append(ScaffoldFile(path="gecx-config.json", content=_gecx_json(req)))

        # One agent dir per sub-agent (+ the host), each with instruction.txt.
        for a in req.agents:
            files.append(ScaffoldFile(
                path=f"agents/{a.name}/{a.name}.json", content=_child_agent_json(a)))
            files.append(ScaffoldFile(
                path=f"agents/{a.name}/instruction.txt",
                content=a.instruction.rstrip() + "\n"))
        files.append(ScaffoldFile(
            path=f"agents/{req.host.name}/{req.host.name}.json",
            content=_host_agent_json(req.host)))
        files.append(ScaffoldFile(
            path=f"agents/{req.host.name}/instruction.txt",
            content=req.host.instruction.rstrip() + "\n"))

        # Shared framework tools (13, one copy per app).
        for f in framework_files:
            files.append(ScaffoldFile(path=f["path"], content=f["content"]))

        # Every <config_id>_dag across host + sub-agents (emitted once each).
        for cid, cfg in all_configs.items():
            files.append(ScaffoldFile(
                path=f"tools/{cid}_dag/{cid}_dag.json", content=_starter_dag_json(cid)))
            files.append(ScaffoldFile(
                path=f"tools/{cid}_dag/python_function/python_code.py",
                content=_starter_dag_code(cid, cfg)))

        # Setters / executors / the app's set_active_flow router.
        _async = set(getattr(req, "async_tools", None) or ())
        _timeouts = dict(getattr(req, "tool_timeouts", None) or {})
        # Author-supplied model-facing descriptions (from a slot's `description=`); fall
        # back to the generated/default when a tool is not named.
        _descs = dict(getattr(req, "tool_descriptions", None) or {})
        for tname, tcode in emit_tools.items():
            files.append(ScaffoldFile(
                path=f"tools/{tname}/{tname}.json",
                content=_setter_tool_json(
                    tname, _descs.get(tname) or _tool_description(tname, tcode),
                    asynchronous=tname in _async,
                    timeout_seconds=_timeouts.get(tname))))
            files.append(ScaffoldFile(
                path=f"tools/{tname}/python_function/python_code.py", content=tcode))

        # Remote A2A agents — tool json only, no python body (see the renderer).
        files.extend(_remote_agent_files(req, ScaffoldFile))
        files.extend(_search_tool_files(req, ScaffoldFile))
        files.extend(_agent_tool_files(req, ScaffoldFile))
        files.extend(_helper_agent_files(req, ScaffoldFile))

        # OpenAPI toolsets — the toolsets/ tree, a resource kind of its own.
        files.extend(_toolset_files(req, ScaffoldFile))
        files.extend(_guardrail_files(req, ScaffoldFile))

        # Callback copies: 4 canonical per sub-agent; the host per strategy.
        callback_files: list = []
        for a in req.agents:
            callback_files += _agent_callback_files(a.name, callbacks)
        if req.host.strategy == "transfer":
            for name in blessed_source._HOST_CALLBACK_NAMES:
                callback_files.append(
                    (blessed_source.callback_rel_path(req.host.name, name), host_cbs[name]))
        else:
            callback_files += _agent_callback_files(req.host.name, callbacks)
        for cb_path, cb_bytes in callback_files:
            files.append(ScaffoldFile(path=cb_path, content=cb_bytes.decode("utf-8")))

        # --- Invariants. ----------------------------------------------------
        tool_uuids = _collect_uuids(files)
        uuids_unique = len(tool_uuids) == len(set(tool_uuids))

        # Framework drift check. A "transfer" host carries custom
        # before_model/after_tool that legitimately differ from the canonical
        # callbacks (same _01 suffix verify_files checks), so its dir is excluded.
        # An "engine" host runs the canonical 4 callbacks, so it IS verified.
        # Sub-agent callbacks are always verified.
        if req.host.strategy == "transfer":
          host_prefix = f"agents/{req.host.name}/"
          verify_map = {
              f.path: f.content for f in files if not f.path.startswith(host_prefix)
          }
        else:
          verify_map = {f.path: f.content for f in files}
        callback_sync_ok = blessed_source.verify_files(verify_map).ok

        # validate_dag_config on every config (available tools span the whole set).
        available_tools = sorted(
            set(framework_tool_names)
            | {f"{cid}_dag" for cid in all_configs}
            | set(emit_tools)
            | _remote_agent_names(req)
            | _search_tool_names(req)
            | _agent_tool_names(req)
        )
        errors: list = []
        valid = True
        for cid, cfg in all_configs.items():
            verdict = _validate_starter(cfg, available_tools)
            if not verdict.get("valid"):
                valid = False
            errors += [f"[{cid}] {e}" for e in verdict.get("errors", [])]
        validation = ValidationReportLite(valid=valid, errors=errors, warnings=[])

        # Pack AFTER the drift gate above: it must hash the canonical framework bytes.
        # The packed artifact stays verifiable because it embeds its own source.
        files = _maybe_pack(files)

        written_to = None
        if req.mode == "local" and req.target_path:
            written_to = _write_tree(req.target_path, files, callback_files)

        ok = bool(validation.valid and callback_sync_ok and uuids_unique)
        return ScaffoldResult(
            ok=ok,
            files=files,
            written_to=written_to,
            validation=validation,
            callback_sync_ok=callback_sync_ok,
            uuids_unique=uuids_unique,
            framework_version=blessed_source.version(),
            config=None,
            suggested_message=None,
            error=None,
        )
    except Exception as exc:  # crash-envelope: never raise out
        logger.warning("scaffold.build_multi_agent failed: %s", exc)
        return ScaffoldResult(
            ok=False,
            files=[],
            written_to=None,
            validation=ValidationReportLite(valid=False, errors=[str(exc)], warnings=[]),
            callback_sync_ok=False,
            uuids_unique=False,
            error=f"Scaffold failed: {exc}",
        )


def _collect_uuids(files: list) -> list[str]:
    """Every ``name`` UUID across the emitted ``*.json`` definitions.

    A duplicate UUID across tools is the classic push-time 404 (memory:
    reference_push_tools_not_found), so the result reports uniqueness.
    """
    uuids: list[str] = []
    for sf in files:
        if not sf.path.endswith(".json"):
            continue
        try:
            data = json.loads(sf.content)
        except ValueError:
            continue
        name = data.get("name")
        if isinstance(name, str) and _looks_like_uuid(name):
            uuids.append(name)
    return uuids


def _looks_like_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False
