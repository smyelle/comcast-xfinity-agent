"""A hand-off refused for more than one reason must say the right one.

`escalate(condition=...)` decides whether a request for a person may be honoured at all,
and `declined_say` is what the caller hears when it may not. Both shapes `declined_say`
used to have are single-voiced: a line, or a LADDER indexed by how many times the request
has been refused. Neither is indexed by WHAT refused it.

One gate, three refusals that want completely different words:

    the gate says no because      what the caller needs to hear
    ----------------------------  --------------------------------------------
    there is an outage            FINAL. Nobody on the other end can fix it, so
                                  say so and point them somewhere useful.
    the line check is still out   TEMPORARY, and nearly over. "Hold on, we're
                                  almost there" — the opposite sentiment.
    nothing is known yet          Neither. Two details are missing and the flow
                                  cannot even say which way this is going to go.

With one field an author has to pick a sentence vague enough to cover all three, on the
one turn where the caller has asked a direct question — or derive the wording in a
callback, which puts spoken copy where the validator, the linter and every offline oracle
cannot see it.

So `declined_say` also takes a list of REASONS:

    declined_say=[
        {"when": <condition>, "say": "..."},   # first match wins
        {"when": <condition>, "say": [...]},   # `say` may be a ladder
        {"say": "..."},                        # no `when` -> the catch-all
    ]

Each piece earns its place here:

* ORDER IS THE POLICY. The reasons are evaluated top to bottom against the filled state
  and the first match supplies the line. During an outage the check is also still out, so
  both of the first two reasons hold — and the outage one is listed first because it is
  the one that decides the caller's next hour.
* A `say` MAY ITSELF BE A LADDER, so the repeat behaviour composes: the second reason
  answers a second ask with a different sentence, exactly as a plain `declined_say`
  ladder does, and clamps to its last rung rather than draining to silence. The index is
  `escalate_declined` — the number of refusals on the CALL, not within the reason.
* THE CATCH-ALL IS LAST, and has no `when`. Anything after an entry with no `when` can
  never be reached, and what an author writes below a catch-all is invariably their most
  specific wording — so authoring and the validator both reject it. Omitting the
  catch-all is a warning rather than an error: it is reachable config, only silent on a
  refusal none of its conditions describe.
* THE CATCH-ALL IS REACHABLE HERE, deliberately. The second reason's `when` is NARROWER
  than the gate's own second leg — it wants the account id present as well — so a caller
  who asks for a person before giving anything at all matches neither reason and lands on
  it. A catch-all nothing can reach is a catch-all nobody has read.
* AN UNEVALUABLE `when` IS SKIPPED, not matched. That is the opposite of the block's own
  `condition`, which fails OPEN because a broken gate must never swallow a request for a
  human. A broken `when` cannot swallow anything — it only chooses wording — so the
  honest fallback is the next reason down rather than an explanation that may not hold.
  Nothing here relies on that; it is the behaviour to know about when a `when` grows a
  typo. The validator catches the common cause first: a `when` naming a slot the flow
  does not declare is an error, because it would never match and the caller would hear
  the NEXT reason down, which is the wrong explanation delivered with confidence.
* A LIST IS READ AS REASONS ONLY WHEN IT CARRIES ONE. `declined_say=["a", "b"]` is still
  a ladder and means exactly what it always did, so no existing flow moves.

Build + validate + drive the offline engine:
    python -m examples.declined_reasons        # emits ./declined_reasons_app

...then deploy and ASSERT the same paths against the real CES runtime:
    python -m examples.declined_reasons --live projects/<p>/locations/us/apps/<id>

`--live` is not decoration. Which reason a caller hears is decided by the engine, but
WHETHER the engine is ever asked is decided by the model choosing to call the control
tool — and a refusal is spoken as a preempt, so the wording is the engine's and the
timing is the platform's. The offline sim can show the first and neither of the others.
"""

import flows

# The two conditions the gate is built from, named once and reused, so the reason that
# explains a leg cannot drift from the leg it explains. That reuse is the point: a
# `when` copied by hand and then edited on one side is a refusal that speaks about a
# state the gate is not actually in.
OUTAGE = {"slot": "outage_status", "eq": "active"}
CHECK_IS_BACK = {"slot": "check_result", "filled": True}

# FINAL. No hand-off improves an outage, so the honest thing is to say that and give the
# caller something to do that is not holding.
OUTAGE_SAY = (
    "There's an outage on your street right now — I can see the crew is already on it. "
    "Putting you through wouldn't get your service back any sooner, so let me text you "
    "the moment it clears instead."
)

# TEMPORARY, and a LADDER, because a second ask deserves a second sentence. Rung two
# concedes a little more of the reasoning rather than repeating rung one louder.
CHECK_RUNNING_SAY = [
    "The line check is still running. Let me get that back first, so whoever picks up "
    "already knows what's wrong instead of starting you over.",
    "Almost there — it's the last few seconds. I'd rather not hand you across without "
    "the result, because then you'd just be asked all of this again.",
]

# NEITHER, and this is the one an author forgets. Two details are missing, so the flow
# cannot yet say whether this is an outage or a slow check.
CATCH_ALL_SAY = (
    "Let me take a couple of details first — I don't want to pass you to someone who "
    "then has to ask you the same questions."
)

HAND_OFF_SAY = "Of course — let me get you through to an engineer now."


def build() -> flows.App:
  """A short repair flow whose escalate rail can refuse for three different reasons."""
  flow = flows.Flow("line_repair", root_agent="repair_agent")

  flow.add(
      # Seeded by whatever established the network's state earlier in a real flow; here
      # it is an event slot so the example stays one file and one deploy.
      flows.event_slot("outage_status"),
      flows.user_slot(
          "account_id",
          ask="What's the account number on the bill?",
          hint="the account number",
      ),
      flows.user_slot(
          "zip_code",
          ask="And the zip code the line is at?",
          hint="the service address zip code",
      ),
      flows.result_slot("check_result", "run_line_check"),
  )
  flow.task("run_line_check", "run_line_check_tool", ["account_id", "zip_code"],
            "check_result", out_key="check_result",
            condition=flows.has("zip_code"))

  flow.set("escalate", flows.escalate(
      say=HAND_OFF_SAY,
      # Two legs, so two ways to be refused — which is the whole reason the field
      # needed a third shape. An outage is never worth a hand-off; a check still in
      # flight is worth one in about ten seconds.
      condition={"all": [{"not": OUTAGE}, CHECK_IS_BACK]},
      declined_say=[
          {"when": OUTAGE, "say": OUTAGE_SAY},
          # Narrower than the gate's own `CHECK_IS_BACK` leg on purpose: this reason
          # claims the check is RUNNING, and it is only running once there is an
          # account to run it against. Before that the catch-all is the truthful one.
          {"when": {"all": [{"slot": "account_id", "filled": True},
                            {"slot": "check_result", "filled": False}]},
           "say": CHECK_RUNNING_SAY},
          {"say": CATCH_ALL_SAY},
      ],
  ))

  return flows.App(
      root_flow=flow,
      app_display_name="Line Repair (declined reasons)",
      agent_instruction=(
          "You help with a broken internet line. Collect what you are asked to "
          "collect. If the caller asks for a person, the engine decides whether the "
          "hand-off happens — never promise or refuse a transfer in your own words."
      ),
      variables=[{"name": "outage_status",
                  "description": "Whether a known outage covers this line.",
                  "schema": {"type": "STRING", "default": "clear"}}],
  )


app = build()


def before_agent_callback(callback_context) -> None:
  """Put the `outage_status` session variable where the escalate gate reads it.

  A control block's `condition` — and now a refusal reason's `when` — is evaluated
  against FILLED SLOTS. An event slot is not populated from a session variable on its
  own, so without this the gate and every `when` see an empty value: the outage leg
  reads false, the outage reason never matches, and the caller is told about a line
  check during an outage.
  """
  state = callback_context.state
  sm = state.get("sm") or {}
  filled = sm.setdefault("filled", {})
  status = state.get("outage_status")
  if status and not filled.get("outage_status"):
    filled["outage_status"] = status
    state["sm"] = sm


app.hooks = flows.AgentHooks(before_agent=before_agent_callback)


def _demo_run() -> None:
  """Drive every refusal path through the offline engine.

  All of them are the SAME request — "I want a person" — answered differently because
  the state it arrives in is different. That is the half worth showing without a model:
  which reason matches is the engine's decision, so it is fully determined offline.

  The flow's own setters have to be materialized first. A bare framework root carries
  the control tools and nothing else, so the first `set_...` call for a slot this app
  declares would be a `FileNotFoundError` rather than a filled slot.
  """
  import shutil
  import tempfile

  from flows.authoring import build as _build
  from flows.engine import loader as fb
  from flows.sim import engine_sim

  all_map, bodies, _ = _build._assemble(app)  # noqa: SLF001
  tmp_root = tempfile.mkdtemp(prefix="flows_declined_reasons_")
  root = fb.materialize_tools_root(bodies, parent=tmp_root)
  config = all_map[app.config_id]

  def ask_for_a_human(label, *, event=None, prefill=None, check_back=False, times=1):
    engine_sim.reset_store()
    sid, _ = engine_sim.start(config, flow_id=app.config_id, framework_root=root,
                              event_data=event or None)
    # A generated setter's NAME is not derivable from its slot's, so read the map the
    # engine publishes rather than guessing at one.
    setter_of = {v: k for k, v in
                 (engine_sim.session_sm(sid).get("_setter_slots") or {}).items()}
    for slot, value in (prefill or {}).items():
      engine_sim.step({"session_id": sid, "kind": "setter_call",
                       "tool": setter_of[slot], "args": {slot: value}})
    if check_back:
      engine_sim.step({"session_id": sid, "kind": "task_result",
                       "task_name": "run_line_check", "success": True,
                       "result": {"check_result": "line drops every few minutes"}})
    said = ""
    for _ in range(times):
      res = engine_sim.step({"session_id": sid, "kind": "setter_call",
                             "tool": "transfer_to_human", "args": {}})
      said = (res.get("agent_text") or "").strip()
    print(f"  {label:36} -> {said[:88]!r}")

  try:
    ask_for_a_human("nothing known yet (catch-all)")
    ask_for_a_human("an outage (reason 1)", event={"outage_status": "active"})
    ask_for_a_human("check still out (reason 2, rung 0)",
                    prefill={"account_id": "A-1042"})
    ask_for_a_human("check still out (reason 2, rung 1)",
                    prefill={"account_id": "A-1042"}, times=2)
    ask_for_a_human("the check landed (no refusal)",
                    prefill={"account_id": "A-1042", "zip_code": "94043"},
                    check_back=True)
  finally:
    shutil.rmtree(tmp_root, ignore_errors=True)


# (label, seed variables, [caller turns], substring the LAST reply must contain)
#
# Each row is one refusal, and the substrings are deliberately taken from DIFFERENT
# sentences: a demo where every path passes on a shared phrase would pass just as well
# against a `declined_say` that had never learned to branch at all.
LIVE_CHECKS = [
    ("catch-all: nothing known yet",
     {"outage_status": "clear"},
     ["I want to speak to a person"],
     "take a couple of details first"),
    ("reason 1: an outage",
     {"outage_status": "active"},
     ["my internet is down", "just put me through to someone"],
     "outage on your street"),
    ("reason 2: the check is still out",
     {"outage_status": "clear"},
     ["my internet is down", "the account is A 1 0 4 2",
      "can I talk to an engineer"],
     "line check is still running"),
    ("reason 2, rung 1: asked twice",
     {"outage_status": "clear"},
     ["my internet is down", "the account is A 1 0 4 2",
      "can I talk to an engineer", "no, I'd really rather speak to someone"],
     "last few seconds"),
    ("the gate opens once the check lands",
     {"outage_status": "clear"},
     ["my internet is down", "the account is A 1 0 4 2", "the zip is 94043",
      "can I talk to an engineer"],
     "through to an engineer"),
]


def _live_run(app_resource: str, app_dir: str, cxas_bin: str = "cxas") -> int:
  """Deploy and ASSERT every reason against the real CES runtime.

  `flows` authors, `cxas-scrapi` drives; this needs cxas-scrapi importable, the `cxas`
  CLI (pass `--cxas` if it is not on PATH), and credentials for the target project.
  """
  import cxas_scrapi
  from flows.deploy.push import deploy

  deploy(app_dir, app_resource, cxas=cxas_bin)
  sessions = cxas_scrapi.Sessions(app_name=app_resource)
  failures = []
  for label, seed, turns, want in LIVE_CHECKS:
    session_id = sessions.create_session_id()
    said = ""
    for turn in turns:
      res = sessions.run(session_id=session_id, text=turn, variables=seed or None,
                         use_tool_fakes=True)
      said = (sessions.get_agent_text_from_outputs(res.outputs) or "").strip()
    ok = want.lower() in said.lower()
    print(f"  {'ok  ' if ok else 'FAIL'} {label:34} -> {said[:92]!r}")
    if not ok:
      failures.append(f"{label}: wanted {want!r}")
  print(f"\n{len(LIVE_CHECKS) - len(failures)}/{len(LIVE_CHECKS)} live checks passed")
  for f in failures:
    print(f"  FAILED {f}")
  return 1 if failures else 0


if __name__ == "__main__":
  import argparse
  import sys

  ap = argparse.ArgumentParser(description="declined_say reasons demo")
  ap.add_argument("--out", default="./declined_reasons_app")
  ap.add_argument("--live", metavar="APP_RESOURCE",
                  help="deploy to this CES app and assert every reason against the "
                       "real runtime (needs cxas-scrapi + creds)")
  ap.add_argument("--cxas", default="cxas",
                  help="path to the cxas CLI when it is not on PATH")
  args = ap.parse_args()

  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  assert errors == [], errors
  _demo_run()
  flows.build_app(app, args.out, overwrite=True)
  print(f"built -> {args.out} (proves: one escalate gate, three refusals, each with "
        "the words that fit it)")
  if args.live:
    print(f"\nlive: {args.live}")
    sys.exit(_live_run(args.live, args.out, args.cxas))
