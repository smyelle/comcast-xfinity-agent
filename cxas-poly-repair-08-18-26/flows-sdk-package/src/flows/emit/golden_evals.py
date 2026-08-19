"""Golden eval smoke sets shipped with generated template agents.

The hermetic conformance suite (tests/conformance) is the FAST CI gate — it
scripts the tool calls and runs the real engine, no LLM. Golden evals are the
slower, live layer: a handful of scenario evals run against the DEPLOYED app with
the real model (`cxas push-eval` → `runEvaluation`), proving the LLM actually
drives the agent. EVERY template ships a small (2-3) golden smoke set so "does
this agent really work end to end" is one command away.

Format mirrors the CES golden schema (examples/bella_notte/evaluations): a list
of turns, each a userInput followed by expectations (toolCall args and/or an `sm`
filled subset match). Scored per expectation by the eval system.
"""

from __future__ import annotations

import json
import uuid

# Stable namespace so a given (config_id, eval name) always gets the same UUID —
# regenerating a template produces identical eval files (no uuid4 churn).
_NS = uuid.UUID("5c0fa11e-0000-4000-8000-000000000001")


def _stable_id(*parts: str) -> str:
  return str(uuid.uuid5(_NS, "/".join(parts)))


def _suite_name(config_id: str) -> str:
  return f"{config_id} — Smoke Suite"


# Per-template golden inputs: the routing intent, an ordered (setter, utterance)
# per slot, the terminal executor tool, and an optional setter-validation case.
# Realistic utterances so the live LLM actually drives the agent.
_GOLDEN_SPECS = {
    "reservation": {
        "intent": "I'd like to book a table",
        "slots": [
            ("set_party_size", "4 people"),
            ("set_reservation_date", "Friday July 17th"),
            ("set_reservation_time", "7pm"),
            ("set_guest_name", "under Alex"),
        ],
        "task": "create_reservation",
        "validation": {
            "setter": "set_party_size",
            "bad": "actually make it 99 people",
            "good": "sorry, just 4 of us",
            "good_args": {"party_size": 4.0},
        },
    },
    "appointment": {
        "intent": "I'd like to book an appointment",
        "slots": [
            ("set_service_type", "a haircut"),
            ("set_appointment_date", "next Tuesday"),
            ("set_appointment_time", "2pm"),
            ("set_contact_name", "under Jordan"),
        ],
        "task": "book_appointment",
    },
    "lead_capture": {
        "intent": "Hi, I'd like to get in touch with sales",
        "slots": [
            ("set_contact_name", "I'm Sam"),
            ("set_company", "from Acme"),
            ("set_email", "sam@acme.com"),
            ("set_interest", "we're looking for a demo"),
        ],
        "task": "submit_lead",
        "validation": {
            "setter": "set_email",
            "bad": "my email is not-an-email",
            "good": "it's sam@acme.com",
            "good_args": {"email": "sam@acme.com"},
        },
    },
    "support_triage": {
        "intent": "I have a problem with my order",
        "slots": [
            ("set_issue_category", "my order hasn't arrived"),
            ("set_urgency", "it's urgent"),
            ("set_account_id", "account A123"),
            ("set_description", "it was due yesterday and never came"),
        ],
        "task": "route_ticket",
    },
}


def _gate_turn(spec) -> dict:
  return {"steps": [
      {"userInput": {"text": spec["intent"]}},
      {"expectation": {"note": "Flow_Activated",
                       "toolCall": {"tool": "set_active_flow"}}},
  ]}


def _happy_path(config_id: str, spec) -> dict:
  turns = [_gate_turn(spec)]
  for setter, utterance in spec["slots"]:
    turns.append({"steps": [
        {"userInput": {"text": utterance}},
        {"expectation": {"note": f"{setter}_Called", "toolCall": {"tool": setter}}},
    ]})
  turns.append({"steps": [
      {"userInput": {"text": "yes, that's all correct"}},
      {"expectation": {"note": "Task_Fired", "toolCall": {"tool": spec["task"]}}},
  ]})
  return {
      "name": _stable_id(config_id, "happy"),
      "displayName": "Smoke - Happy Path",
      "description": "The agent activates the flow, collects each slot, and fires "
                     "the terminal task after the final confirm.",
      "tags": ["smoke", "happy-path", "slot-filling"],
      "evaluationDatasets": [_suite_name(config_id)],
      "golden": {"turns": turns},
  }


def _readback_reject(config_id: str, spec) -> dict:
  setter, utterance = spec["slots"][0]
  return {
      "name": _stable_id(config_id, "reject"),
      "displayName": "Smoke - Readback Reject Recollects",
      "description": "A read-back value the user rejects must NOT be committed.",
      "tags": ["smoke", "readback"],
      "evaluationDatasets": [_suite_name(config_id)],
      "golden": {"turns": [
          _gate_turn(spec),
          {"steps": [
              {"userInput": {"text": utterance}},
              {"expectation": {"note": "Captured", "toolCall": {"tool": setter}}},
          ]},
          {"steps": [
              {"userInput": {"text": "no, that's wrong"}},
              {"expectation": {"note": "Not_Filled",
                               "updatedVariables": {"sm": {"filled": {}}}}},
          ]},
      ]},
  }


def _validation(config_id: str, spec) -> dict:
  v = spec["validation"]
  return {
      "name": _stable_id(config_id, "validation"),
      "displayName": "Smoke - Invalid Input Recovers",
      "description": "Invalid input is rejected (not stored), then a valid value "
                     "is captured — proves validation surfaces and recovers.",
      "tags": ["smoke", "validation", "error-recovery"],
      "evaluationDatasets": [_suite_name(config_id)],
      "golden": {"turns": [
          _gate_turn(spec),
          {"steps": [
              {"userInput": {"text": v["bad"]}},
              {"expectation": {"note": "Bad_Called", "toolCall": {"tool": v["setter"]}}},
              {"expectation": {"note": "Not_Stored",
                               "updatedVariables": {"sm": {"filled": {}}}}},
          ]},
          {"steps": [
              {"userInput": {"text": v["good"]}},
              {"expectation": {"note": "Valid_Recaptured",
                               "toolCall": {"tool": v["setter"], "args": v["good_args"]}}},
          ]},
      ]},
  }


def _evals_for(config_id: str, spec) -> list[dict]:
  evals = [_happy_path(config_id, spec), _readback_reject(config_id, spec)]
  if spec.get("validation"):
    evals.append(_validation(config_id, spec))
  return evals


def golden_eval_files(template, config_id: str) -> list[dict]:
  """ScaffoldFile dicts ({path, content}) for a template's golden smoke set.

  Returns [] for templates without a spec. Emits one file per eval under
  ``evaluations/<Name>/<Name>.json`` plus the dataset under
  ``evaluationDatasets/<Suite>/<Suite>.json`` (the CES on-disk layout).
  """
  spec = _GOLDEN_SPECS.get(template or "")
  if spec is None:
    return []
  evals = _evals_for(config_id, spec)
  files: list[dict] = []
  for ev in evals:
    safe = ev["displayName"].replace(" ", "_").replace("/", "_")
    files.append({
        "path": f"evaluations/{safe}/{safe}.json",
        "content": json.dumps(ev, indent=2) + "\n",
    })
  suite = {
      "name": _stable_id(config_id, "suite"),
      "displayName": _suite_name(config_id),
      "evaluations": [ev["displayName"] for ev in evals],
  }
  safe_suite = suite["displayName"].replace(" ", "_").replace("—", "-").replace("/", "_")
  files.append({
      "path": f"evaluationDatasets/{safe_suite}/{safe_suite}.json",
      "content": json.dumps(suite, indent=2) + "\n",
  })
  return files
