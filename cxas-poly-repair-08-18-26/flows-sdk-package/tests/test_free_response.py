"""`answer` — a grounded, intent-scoped free-response fallback.

The free-form counterpart of the bounded ladders (`no_input` = silence, a slot's
`validation` = a bad value, `steer_back` = a stall, `push_back` = a decline): those recover
a broken turn; `answer` fields an engaged, on-topic QUESTION the DAG doesn't model. When the
caller — while the node's `condition` holds — asks something no cue/slot matches (the turn that
would otherwise steer back), the engine hands the model a grounded, tool-whitelisted,
NON-ADVANCING directive built from the account data already in state, and lets it compose the
reply. The model may call ONLY the whitelisted read/compute tools; every other tool (setters,
commit/task executors, intent/classify) is hidden that turn, so a commitment is structurally
impossible — the rails still own every account change.

Three layers under test: the DSL helper (`flows.answer`), the validator
(`validate_dag_config`), and the engine turn (`slot_filling_engine`).
"""

from __future__ import annotations

import flows
from flows.engine.framework.tools.slot_filling_engine.python_function import (
    python_code as engine,
)
from flows.engine.framework.tools.validate_dag_config.python_function import (
    python_code as vdc,
)

WHITELIST = ["bill_compute", "get_payment_history"]
SETTER = "set_acct"
COMMIT_TOOL = "submit_waiver"


# ── DSL: flows.answer(...) ───────────────────────────────────────────────────────
def test_answer_helper_builds_the_block():
  blk = flows.answer(
      "bill_qa",
      scope="billing questions about this account",
      instruction="Answer from the data; use the compute tool for math.",
      grounds=["bill_grounding"],
      tools=list(WHITELIST),
      condition={"slot": "bill_context", "filled": True},
      requires=["bill_context"],
      max_turns=8,
  )
  assert blk["name"] == "bill_qa"
  assert blk["grounds"] == ["bill_grounding"]
  assert blk["tools"] == WHITELIST
  assert blk["max_turns"] == 8 and blk["allow_math"] is True
  assert "condition" in blk and blk["requires"] == ["bill_context"]


def test_answer_rejects_no_grounds_and_no_tools():
  # With neither a grounding var nor a tool to fetch one, there is nothing to answer from.
  try:
    flows.answer("x", scope="s", instruction="i")
  except ValueError:
    return
  raise AssertionError("answer() with no grounds and no tools should raise")


def test_answer_rejects_nonpositive_max_turns():
  try:
    flows.answer("x", scope="s", instruction="i", grounds=["g"], max_turns=0)
  except ValueError:
    return
  raise AssertionError("answer() with max_turns<=0 should raise")


def test_answer_is_a_valid_flow_policy_and_round_trips():
  blk = flows.answer("bill_qa", scope="s", instruction="i", grounds=["bill_grounding"])
  f = flows.Flow("bill", root_agent="a")
  f.set("answer", [blk])
  cfg = f.to_config()
  assert cfg["answer"] == [blk]


# ── validator: validate_dag_config accepts/rejects the answer policy ──────────────
_VALIDATOR_TOOLS = ["bill_init", "bill_compute", "get_payment_history",
                    "transfer_to_human", "submit_waiver"]


def _validator_config(answer):
  return {
      "slots": [
          {"name": "bill_context", "source": "task:bill_init"},
          {"name": "bill_grounding", "source": "task:bill_init"},
      ],
      "tasks": [{
          "name": "bill_init", "tool": "bill_init", "inputs": [],
          "outputs": {"status": "bill_context", "data": "bill_grounding"},
      }],
      "answer": answer,
  }


def _good_answer():
  return [{
      "name": "bill_qa",
      "scope": "billing questions about this account",
      "instruction": "Answer from the account data; use the compute tool for math.",
      "max_turns": 8,
      "allow_math": True,
      "grounds": ["bill_grounding"],
      "tools": list(WHITELIST),
      "condition": {"slot": "bill_context", "filled": True},
      "requires": ["bill_context"],
  }]


def _validate(cfg):
  res = vdc.DagConfigValidator(cfg, available_tools=_VALIDATOR_TOOLS).validate()
  return res


def test_validator_accepts_a_well_formed_answer_policy():
  res = _validate(_validator_config(_good_answer()))
  answer_errs = [e for e in res.errors if e.startswith("answer")]
  assert answer_errs == [], answer_errs


def test_validator_rejects_an_undeclared_whitelist_tool():
  bad = [dict(_good_answer()[0], tools=["bill_compute", "no_such_tool"])]
  res = _validate(_validator_config(bad))
  assert any("no_such_tool" in e and "not in agent tool list" in e
             for e in res.errors), res.errors


def test_validator_rejects_an_unknown_answer_key():
  bad = [dict(_good_answer()[0], flavour="oops")]
  res = _validate(_validator_config(bad))
  assert any("unknown keys" in e and "flavour" in e for e in res.errors), res.errors


def test_validator_rejects_a_slot_fill_key_as_non_advancing():
  # The answer turn must not fill a slot — a setter/outputs/fill key is an authoring error.
  bad = [dict(_good_answer()[0], outputs={"result": "bill_context"})]
  res = _validate(_validator_config(bad))
  assert any("non-advancing" in e and "outputs" in e for e in res.errors), res.errors


def test_validator_warns_on_a_mutating_looking_whitelist_tool():
  # A commit tool on the whitelist is DECLARED, so it is not an error — but the lint nudges
  # the author to keep commits off the answer whitelist (they stay DAG cues).
  risky = [dict(_good_answer()[0], tools=["bill_compute", "transfer_to_human"])]
  res = _validate(_validator_config(risky))
  assert any("transfer_to_human" in w for w in res.warnings), res.warnings
  assert not any("transfer_to_human" in e for e in res.errors), res.errors


# ── engine: the grounded, whitelisted, non-advancing answer turn ─────────────────
_GROUNDING = {
    "current_total": "52.59", "prior_total": "62.68",
    "installment": "16.66", "credit": "16.66",
    "line_items": [{"name": "plan", "amount": "65.00"},
                   {"name": "discount", "amount": "-10.00"}],
}
_QUESTION = "did my service go up compared to last month?"


def _engine_config(with_answer=True):
  cfg = {
      "slots": [
          {"name": "acct", "source": "user", "setter": SETTER},
          {"name": "bill_context", "source": "system", "setter": ""},
      ],
      "tasks": [{"name": "waiver", "tool": COMMIT_TOOL}],
      "correction_tool": "set_slot_change",
  }
  if with_answer:
    cfg["answer"] = [{
        "name": "bill_qa",
        "scope": "billing questions about this account",
        "instruction": "You are helping the caller understand this bill.",
        "max_turns": 2,
        "allow_math": False,
        "grounds": ["bill_grounding"],
        "tools": list(WHITELIST),
        "condition": {"slot": "bill_context", "filled": True},
        "requires": ["bill_context"],
    }]
  return cfg


def _sm(filled=None):
  return {"filled": dict(filled or {}), "bill_grounding": dict(_GROUNDING),
          "status": "in_progress"}


_FILLED = {"bill_context": {"ok": True}}


def test_engine_emits_a_grounded_answer_on_an_off_menu_turn():
  cfg = _engine_config()
  res = engine._handle_answer(
      _sm(_FILLED), cfg, _QUESTION, cfg["slots"], _FILLED, {}, {},
      progressed=False, channel="", inv_n=1)
  assert isinstance(res, dict)
  assert res.get("answer_directive")
  assert res.get("preempt") is False
  assert res.get("message") == ""


def test_engine_answer_turn_exposes_only_the_whitelist():
  cfg = _engine_config()
  res = engine._handle_answer(
      _sm(_FILLED), cfg, _QUESTION, cfg["slots"], _FILLED, {}, {},
      progressed=False, channel="", inv_n=1)
  hide = res["hide_tools"]
  # whitelist exposed; DAG setter + commit/task tool + intent setter hidden; rails stay.
  assert all(t not in hide for t in WHITELIST)
  assert SETTER in hide
  assert COMMIT_TOOL in hide
  assert "set_intent_changed" in hide
  assert "cancel_flow" not in hide and "transfer_to_human" not in hide


def test_engine_answer_si_is_self_contained():
  cfg = _engine_config()
  sm = _sm(_FILLED)
  res = engine._handle_answer(
      sm, cfg, _QUESTION, cfg["slots"], _FILLED, {}, {},
      progressed=False, channel="", inv_n=1)
  si = engine._build_phase_suffix(sm, res)
  assert "<answer>" in si
  assert _QUESTION in si                     # the caller's question is embedded
  assert "52.59" in si and "current_total" in si   # grounding data is present
  assert "<system_directive>" not in si
  assert "<readback" not in si
  assert "<steer_back>" not in si


def test_engine_skips_a_progressed_turn():
  cfg = _engine_config()
  res = engine._handle_answer(
      _sm(_FILLED), cfg, _QUESTION, cfg["slots"], _FILLED, {}, {},
      progressed=True, channel="", inv_n=2)
  assert res is None    # a filled slot/cue is progress → the rails handle it


def test_engine_skips_a_pending_readback_turn():
  cfg = _engine_config()
  res = engine._handle_answer(
      _sm(_FILLED), cfg, _QUESTION, cfg["slots"], _FILLED, {"acct": "x"}, {},
      progressed=False, channel="", inv_n=3)
  assert res is None


def test_engine_answer_is_bounded_and_keeps_steer_strikes_at_zero():
  cfg = _engine_config()               # max_turns == 2
  sm = _sm(_FILLED)
  sm["_steer_back_turns"] = 3          # pretend the ladder had already marched
  for i in range(2):
    r = engine._handle_answer(
        sm, cfg, _QUESTION + f" #{i}", cfg["slots"], _FILLED, {}, {},
        progressed=False, channel="", inv_n=10 + i)
    assert r is not None
  assert sm.get("_answer_turns_bill_qa") == 2
  over = engine._handle_answer(
      sm, cfg, _QUESTION + " #over", cfg["slots"], _FILLED, {}, {},
      progressed=False, channel="", inv_n=12)
  assert over is None                  # budget spent → steer-back takes over
  assert sm.get("_steer_back_turns") == 0   # answering never accrued a strike


def test_engine_no_answer_policy_is_inert_and_byte_identical():
  cfg = _engine_config(with_answer=False)
  res = engine._handle_answer(
      _sm(_FILLED), cfg, _QUESTION, cfg["slots"], _FILLED, {}, {},
      progressed=False, channel="", inv_n=20)
  assert res is None
  # the whitelist-hiding param defaults to a no-op: hide list identical to omitting it.
  slot_map = {s["name"]: s for s in cfg["slots"]}
  args = (cfg["slots"], _FILLED, {}, ["confirm_pending", "reject_pending"], slot_map)
  kw = dict(fresh_pending=False, executor_tools=[COMMIT_TOOL],
            correction_tool=cfg.get("correction_tool"))
  assert (engine._compute_hidden_tools(*args, **kw)
          == engine._compute_hidden_tools(*args, **kw, answer_tools=None))


def test_engine_hides_the_whitelist_on_a_normal_turn():
  cfg = _engine_config()
  slot_map = {s["name"]: s for s in cfg["slots"]}
  hidden = engine._compute_hidden_tools(
      cfg["slots"], _FILLED, {}, ["confirm_pending", "reject_pending"], slot_map,
      fresh_pending=False, executor_tools=[COMMIT_TOOL],
      correction_tool=cfg.get("correction_tool"),
      answer_tools=set(WHITELIST))
  assert all(t in hidden for t in WHITELIST)   # only exposed on the answer turn


# ── integration: the answer turn must win over an OPEN offer/closing slot ─────────
# These drive the FULL engine (slot_filling_engine), not _handle_answer in isolation —
# the regression the unit tests above could not see: an off-menu question, while an open
# INTENT offer slot is being collected, was consumed by that slot's no-match/error path
# (an empty "having trouble" render) BEFORE the answer check ran. The answer interception
# now sits ahead of the no-match/slot-error path (yielding only to hard-VALUE collection).
from flows.engine import loader as _fb  # noqa: E402

_QA_ANSWER = [{
    "name": "bill_qa", "scope": "billing questions about this account",
    "instruction": "Answer from the billing data. Use bill_compute for math.",
    "max_turns": 8, "allow_math": True,
    "grounds": ["bill_grounding"], "tools": list(WHITELIST),
    "condition": {"slot": "bill_context", "filled": True}, "requires": ["bill_context"],
}]
_QA_GROUNDING = {"current_total": 52.59, "prior_total": 62.68, "installment": 16.66}


def _drive(cfg, sm, text):
  res = _fb.load_engine().slot_filling_engine({
      "raw_config": cfg, "sm": sm, "last_user_text": text, "scanned_user_text": text,
      "is_inactivity": False, "event_data": {}, "config_id": "t", "n_user_turns": 2})
  return res.get("action", res) if isinstance(res, dict) else {}


def test_answer_wins_over_an_open_offer_slot_end_to_end():
  cfg = {
      "slots": [
          {"name": "bill_context", "source": "system", "setter": ""},
          {"name": "savings", "source": "user", "kind": "intent", "setter": "set_savings",
           "ask": "Want to hear savings options?",
           "option_cues": {"yes": [r"\byes\b"], "no": [r"\bno\b"]}, "max_retries": 2},
      ],
      "tasks": [], "gate_slot": None, "answer": _QA_ANSWER,
  }
  sm = _fb.seed_sm(cfg)
  sm["filled"], sm["pending"] = {"bill_context": "ok"}, {}
  sm["bill_grounding"] = _QA_GROUNDING
  _drive(cfg, sm, "")                                   # turn 1: engine asks the offer
  act = _drive(cfg, sm, "can you add my two bills together?")  # turn 2: off-menu question
  assert act.get("answer_directive"), "answer node did not intercept the off-menu question"
  assert "set_savings" in act.get("hide_tools", [])    # the offer's setter is suppressed
  assert "bill_compute" not in act.get("hide_tools", [])  # the whitelist is exposed


def test_answer_yields_to_a_hard_value_slot_end_to_end():
  cfg = {
      "slots": [
          {"name": "bill_context", "source": "system", "setter": ""},
          {"name": "new_date", "source": "user", "setter": "set_new_date",
           "ask": "What new due date? MM/DD/YYYY", "hint": "the new due date MM/DD/YYYY",
           "validation": {"max_retries": 3, "errors": {"invalid_format": "Use MM/DD/YYYY."}}},
      ],
      "tasks": [], "gate_slot": None, "answer": _QA_ANSWER,
  }
  sm = _fb.seed_sm(cfg)
  sm["filled"], sm["pending"] = {"bill_context": "ok"}, {}
  sm["bill_grounding"] = _QA_GROUNDING
  _drive(cfg, sm, "")                                   # turn 1: engine asks for the date
  act = _drive(cfg, sm, "the fifteenth")               # a bad VALUE, not an off-menu question
  # The answer node must NOT hijack hard-value collection — validation owns this turn.
  assert not act.get("answer_directive")


def test_answer_turn_hides_nested_disposition_tools():
  """A commit tool reached only through a NESTED disposition (`then.tool` in a
  validation on_exhaust, a task on_failure, or a flow no_input on_exhaust) must be hidden
  on the answer turn — `_config_tool_names` alone misses those, so without the nested
  collector the model could call them out-of-order and commit."""
  cfg = {
      "slots": [
          {"name": "bill_context", "source": "system", "setter": ""},
          {"name": "d", "source": "user", "setter": "set_d",
           "validation": {"max_retries": 1,
                          "on_exhaust": {"then": {"tool": "updated_billing_transfer_call"}}}},
      ],
      "tasks": [{"name": "t", "tool": "explain_bill_init_mcp",
                 "on_failure": {"max_retries": 1,
                                "on_exhaust": {"then": {"tool": "billing_mcp_submit_fee_waiver"}}}}],
      "no_input": {"reprompts": ["?"],
                   "on_exhaust": {"then": {"tool": "post_activation_transfer_call"}}},
      "answer": _QA_ANSWER,
  }
  hide = set(engine._answer_hide_tools(cfg, cfg["slots"], ["bill_compute"]))
  for t in ("updated_billing_transfer_call", "billing_mcp_submit_fee_waiver",
            "post_activation_transfer_call"):
    assert t in hide, f"nested disposition tool {t!r} must be hidden on an answer turn"
  assert "bill_compute" not in hide          # the whitelist stays callable
  assert "transfer_to_human" not in hide     # the escalate rail stays callable


def test_nested_disposition_tools_walks_the_config():
  cfg = {
      "slots": [{"name": "s", "push_back": {"then": {"tool": "t_pushback"}}}],
      "tasks": [{"name": "x", "awaits": {"on_timeout": {"then": {"tool": "t_timeout"}}}}],
      "steer_back": {"on_exhaust": {"then": {"tool": "t_steer"}}},
  }
  found = engine._nested_disposition_tools(cfg)
  assert found == {"t_pushback", "t_timeout", "t_steer"}, found
