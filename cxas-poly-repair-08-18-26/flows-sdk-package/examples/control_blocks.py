"""Flow-level control blocks: cancel / escalate / no_input dispositions.

Demos the three control-block builders. Each defaults its outcome correctly (the
engine's raw default is the block NAME, which would break a `== "cancelled"` resume
gate) and keeps `transfer_to` optional:

  * `cancel(say=..., transfer_to=...)` — the caller abandons: stop with `outcome:
    "cancelled"`, optionally returning to a parent.
  * `escalate(say=..., transfer_to=...)` — hand off to a human/team with `outcome:
    "escalated"`. `condition=` makes the hand-off conditional and `declined_say=`
    is spoken when it is refused.
  * `no_input(reprompts=..., on_exhaust=...)` — a plain-silence ladder spoken one rung
    per silent turn, then a terminal `on_exhaust` action. `hold_ack=` answers a caller
    who ASKS for time instead of putting the same question to them again.

Set on the flow via `Flow.set(policy, block)`. `_demo_run()` drives all four paths
through the offline engine: the silence ladder, a spoken hold, and a request for a
human both refused (scheduler down) and honoured (scheduler up).

    # build + validate + drive the offline engine -> ./control_blocks_app
    python -m examples.control_blocks

    # ...then deploy and ASSERT the same paths against the real CES runtime
    python -m examples.control_blocks --live projects/<p>/locations/us/apps/<id>

`--live` exists because the offline sim is not proof: `hold_ack` passed the sim and
every unit test while being completely broken on a deployed agent. It exits non-zero
if any check fails, so it can gate a release rather than just print.
"""

import flows

appointment = flows.Flow("book_appointment", root_agent="Appointment_Agent",
                         bootstrap={"welcome_slot": "welcome"})

# cancel: the caller changed their mind — acknowledge and stop (outcome "cancelled").
appointment.set("cancel", flows.cancel(
    say="No problem, I've cancelled that. Is there anything else I can do?"))

# escalate: hand the caller to a scheduler with a spoken lead-in (outcome "escalated").
# `condition` makes the hand-off CONDITIONAL. Some dispositions are only valid in
# some states — there is no point queueing a caller for a team that is offline — so
# while the scheduling system is down the request is DROPPED (they can ask again once
# it is back) and `declined_say` explains why. With no condition the block is always
# available, which is the behaviour every existing flow keeps.
appointment.set("escalate", flows.escalate(
    say="Let me connect you with a scheduler who can help.",
    transfer_to="Scheduler_Agent",
    condition={"neq": "maintenance", "slot": "system_status"},
    declined_say=("Our scheduling team is offline for maintenance right now, so I "
                  "can't put you through — but I can still take your booking here.")))

# no_input: plain-silence ladder, then escalate on exhaust. `hold_ack` answers the
# caller who ASKS for time rather than falling silent — without it the flow re-asks
# the very question they just said they needed a moment for.
appointment.set("no_input", flows.no_input(
    reprompts=["Are you still there?", "I still didn't hear anything."],
    hold_reprompts=["", "", "Take your time — I'm still here."],
    hold_ack="Of course, take your time. I'll be here when you're ready.",
    on_exhaust={"say": "I'll let you go for now. Please call back anytime. Goodbye.",
                "then": {"tool": "transfer_to_human"}}))

appointment.add(
    flows.announce("welcome", ["I can book an appointment for you."], shared=True),
    flows.event_slot("system_status"),
    flows.user_slot("preferred_day", "What day works best for you?"),
    flows.result_slot("booking", "book_task"),
)
appointment.task("book_task", "book_appointment_tool", ["preferred_day"], "booking",
                 out_key="confirmation", terminal=True,
                 then_say="You're booked — confirmation {booking}.",
                 condition=flows.has("preferred_day"))

def before_agent_callback(callback_context) -> None:
  """Put the `system_status` session variable where the escalate condition reads it.

  A control block's `condition` is evaluated against FILLED SLOTS, and an event slot
  is not populated from a session variable on its own — something has to write it.
  This is the smallest realistic way to do that, and without it the condition sees an
  empty value, the gate passes, and the hand-off happens whatever the variable says.
  """
  state = callback_context.state
  sm = state.get("sm") or {}
  filled = sm.setdefault("filled", {})
  status = state.get("system_status")
  if status and not filled.get("system_status"):
    filled["system_status"] = status
    state["sm"] = sm


app = flows.App(
    root_flow=appointment,
    app_display_name="Appointment Booking (control blocks)",
    model="gemini-3.5-flash",
    hooks=flows.AgentHooks(before_agent=before_agent_callback),
    variables=[{"name": "system_status",
                "description": "Scheduling system availability.",
                "schema": {"type": "STRING", "default": "ok"}}],
)


def _demo_run() -> None:
  """Drive the three dispositions through the offline engine.

  Silence walks the plain ladder; a SPOKEN hold is acknowledged instead of re-asked;
  and a request for a human while the scheduler is down is declined rather than
  queued. The last two are the behaviours you cannot get from wording alone.
  """
  from flows.sim import engine_sim

  engine_sim.reset_store()
  sid, res = engine_sim.start(appointment.to_config(), "book_appointment")
  print(f"  start          -> asks={res['agent_text'][:52]!r}")
  for i in range(1, 3):
    res = engine_sim.step({"session_id": sid, "kind": "user_text", "text": "",
                           "is_inactivity": True})
    print(f"  silence {i}      -> next_action={res['next_action']!r} "
          f"reprompt={res['agent_text'][:52]!r}")

  # A caller who ASKS for time. Before hold_ack this re-asked "What day works best
  # for you?" — the one reply the request rules out.
  #
  # The three below it all carry a hold marker and are NOT requests for time, so the
  # veto disqualifies them and the turn is answered instead of waited out. Answering
  # "why do you need that?" with "take your time" is the failure a generous marker list
  # buys if nothing checks the rest of the sentence.
  for label, text in (
      ("spoken hold  ", "hold on, let me grab my calendar"),
      ("veto question", "hold on, why do you need my day"),
      ("veto a person", "hold on, just get me a person"),
      ("veto value   ", "hold on, it's the 14th of 2026 at 0930"),
  ):
    engine_sim.reset_store()
    sid, _ = engine_sim.start(appointment.to_config(), "book_appointment")
    res = engine_sim.step({"session_id": sid, "kind": "user_text", "text": text})
    print(f"  {label}  -> {res['agent_text'][:72]!r}")

  # A request for a human, once with the scheduler down and once with it up. The
  # request arrives as the framework's own control setter, which is how the model
  # raises it live.
  for status, label in (("maintenance", "human (down)"), ("ok", "human (up)  ")):
    engine_sim.reset_store()
    sid, _ = engine_sim.start(appointment.to_config(), "book_appointment",
                              event_data={"system_status": status})
    res = engine_sim.step({"session_id": sid, "kind": "setter_call",
                           "tool": "transfer_to_human", "args": {}})
    print(f"  {label}   -> {res['agent_text'][:72]!r}")


LIVE_CHECKS = [
    # (label, seed variables, [user turns], substring the LAST reply must contain)
    ("spoken hold",
     {}, ["hi", "hold on, let me grab my calendar"], "take your time"),
    # The veto, live: the same marker, and the caller must get an ANSWER rather than
    # patience. Asserted on the disposition the request actually deserves.
    ("veto: a person beats the marker",
     {"system_status": "ok"}, ["hi", "hold on, just get me a person"],
     "connect you with a scheduler"),
    ("human, scheduler down",
     {"system_status": "maintenance"}, ["I want to speak to a person"],
     "offline for maintenance"),
    ("human, scheduler up",
     {"system_status": "ok"}, ["I want to speak to a person"],
     "connect you with a scheduler"),
]


def _live_run(app_resource: str, app_dir: str, cxas_bin: str = "cxas") -> int:
  """Deploy and ASSERT the same three paths against the real CES runtime.

  The offline sim is not proof. `hold_ack` passed the sim and every unit test
  while being completely broken live — a before_agent hook seeding event slots
  counted as "the caller answered" and suppressed it. Nothing caught that until
  a deployed agent was driven, so the demo carries a mode that does it.

  `flows` authors, `cxas-scrapi` drives (see the package docstring); this needs
  cxas-scrapi importable, the `cxas` CLI (pass `--cxas` if it is not on PATH), and
  credentials for the target project.
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
      res = sessions.run(session_id=session_id, text=turn,
                         variables=seed or None, use_tool_fakes=True)
      said = (sessions.get_agent_text_from_outputs(res.outputs) or "").strip()
      # A session-ending turn is mirrored in the transcript; the caller hears it
      # once. Collapse so an assertion is about what was SAID.
      half = len(said) // 2
      if half and said[:half].strip() == said[half:].strip():
        said = said[:half].strip()
    ok = want.lower() in said.lower()
    print(f"  {'ok  ' if ok else 'FAIL'} {label:24} -> {said[:88]!r}")
    if not ok:
      failures.append(f"{label}: wanted {want!r}")
  print(f"\n{len(LIVE_CHECKS) - len(failures)}/{len(LIVE_CHECKS)} live checks passed")
  for f in failures:
    print(f"  FAILED {f}")
  return 1 if failures else 0


if __name__ == "__main__":
  import argparse
  import sys

  ap = argparse.ArgumentParser(description=__doc__)
  ap.add_argument("--out", default="./control_blocks_app")
  ap.add_argument("--live", metavar="APP_RESOURCE",
                  help="deploy to this CES app and assert the three paths "
                       "against the real runtime (needs cxas-scrapi + creds)")
  ap.add_argument("--cxas", default="cxas",
                  help="path to the cxas CLI when it is not on PATH")
  args = ap.parse_args()

  errors, warnings = flows.validate_app(app)
  assert errors == [], errors
  _demo_run()
  flows.build_app(app, args.out, overwrite=True)
  print(f"built -> {args.out} "
        "(proves: cancel/escalate/no_input control blocks, a conditional "
        "escalate, and hold_ack on a deployable flow)")
  if args.live:
    print(f"\nlive: {args.live}")
    sys.exit(_live_run(args.live, args.out, args.cxas))
