"""One agent definition, delivered to both a phone call and a chat window.

An agent that serves voice and chat normally has to exist twice: same slots, same
tasks, same tools, different wording and different affordances — and the two copies
drift apart with every fix. This example is the single definition that replaces both.

Nothing here branches on a channel name. Each surface declares what it CAN DO
(`payloads`, `brevity`, `links`, `filler`, `keypad`, `max_options`) and the authored
content is projected onto it, so a surface nobody has invented yet still behaves
correctly. What the author writes once:

    ask=flows.say("Here's what's available this week: ...",       # the floor
                  brief="I've got Tuesday or Thursday. Which?",   # spoken surfaces
                  card=flows.card(title=..., actions=[...]))      # surfaces that render

    python -m examples.polymorphic
    python -m examples.polymorphic --live projects/<p>/locations/us/apps/<id>

`--live` exists because the offline sim is not proof. The `channel` plumbing this
feature builds on has existed in the engine for a long time and had never once been
verified against a deployed app; a sibling feature (`hold_ack`) passed the sim and
every unit test while being completely broken in production. This exits non-zero if
any check fails, so it can gate a release rather than just print.
"""

import flows

booking = flows.Flow("polybook", root_agent="Booking_Agent",
                     bootstrap={"welcome_slot": "welcome"})

booking.add(
    flows.announce("welcome", ["I can book a technician visit for you."],
                   shared=True),

    # 1. Most fields need nothing. A bare string is still a bare string, and emits
    #    exactly the config it always did — say() is for the few places that differ.
    flows.user_slot("account_number", "What's your account number?",
                    dtmf={str(d): str(d) for d in range(10)}),
    flows.result_slot("available_times", "lookup_task"),

    # 2. The interesting one. Voice hears a short question; chat reads the full list
    #    AND gets a card with a tappable option and a link. One authored intent.
    flows.user_slot(
        "appointment",
        flows.say(
            "Here's what's available this week: Tuesday morning, Tuesday afternoon,"
            " Wednesday morning, Thursday afternoon, or Friday morning.",
            brief="I've got Tuesday morning or Thursday afternoon."
                  " Which works better?",
            card=flows.card(
                title="Available times",
                body="Pick a slot that works for you.",
                actions=[flows.action("Tuesday AM", "pick_tue_am"),
                         flows.action("Thursday PM", "pick_thu_pm"),
                         flows.link("See all times", "https://example.test/times")],
            ),
            # Five real options come back from the backend. Chat can show all
            # eight it is allowed; voice is capped at three and never sees them.
            chips=flows.chips(options_from="available_times",
                              event_name="pick_time"),
        ),
    ),
    flows.result_slot("booking", "book_task"),
)

# 3. Latency is masked differently from one definition. `filler_say` is spoken only
#    where the `filler` capability holds — on a call it covers the dead air while the
#    backend runs; in a chat window it would be an extra bubble saying nothing.
lookup = flows.task("lookup_task", "get_open_appointments", ["account_number"],
                    "available_times", out_key="times")
lookup["filler_say"] = "One moment while I check the schedule."
booking.task(lookup)

# 4. The terminal. The spoken form drops the URL entirely — a surface without the
#    `links` capability should never be read a web address — and offers to text it
#    instead. The chat form keeps it as a button.
booking.task(
    "book_task", "book_visit", ["account_number", "appointment"], "booking",
    out_key="confirmation", terminal=True,
    then_say=flows.say(
        "You're all set — {appointment}, confirmation {booking}."
        " You can reschedule any time at example.test/appointments.",
        brief="You're all set for {appointment}. Confirmation {booking}."
              " I've texted you the details.",
        card=flows.card(
            title="Visit booked",
            body="{appointment} · confirmation {booking}",
            actions=[flows.action("Add to calendar", "add_to_calendar"),
                     flows.link("Manage appointment",
                                "https://example.test/appointments")],
        ),
    ),
)


@flows.tool(flow="polybook")
def get_open_appointments(account_number: str) -> dict:
  """List the appointment windows still open for an account this week."""
  del account_number
  return {"success": True,
          "times": ["Tuesday morning", "Tuesday afternoon", "Wednesday morning",
                    "Thursday afternoon", "Friday morning"]}


@flows.tool(flow="polybook")
def book_visit(account_number: str, appointment: str) -> dict:
  """Book a technician visit and return a confirmation number."""
  del account_number, appointment
  return {"success": True, "confirmation": "BK-4417"}


app = flows.App(
    root_flow=booking,
    app_display_name="Polymorphic Booking (voice + chat)",
    # A text-capable model: the demo is driven over text so both surfaces can be
    # exercised from one deployed app. Voice deployments use a *-flash-live model.
    model="gemini-3.5-flash",
)


def _demo_run() -> None:
  """Render the same flow on each surface, offline.

  The two built-in surfaces plus one alias of each plus an unrecognized channel,
  so the fallback is visible rather than assumed.
  """
  from flows.engine import loader

  cfg = booking.to_config()
  base = {"welcome": "done", "account_number": "8069100230359928"}
  times = ["Tuesday morning", "Tuesday afternoon", "Wednesday morning",
           "Thursday afternoon", "Friday morning"]

  def turn(channel, filled, done=None):
    # `_config_id` matters: without it the engine reads this as a config change and
    # wipes task_results, so an already-completed task looks unfired and runs again.
    out = loader.run_engine(cfg, {"filled": dict(filled), "pending": {},
                                  "status": "in_progress", "_config_id": "polybook",
                                  "task_results": dict(done or {})},
                            last_user_text="", event_data={"channel": channel},
                            config_id="polybook")
    said = (out["action"].get("message") or "").strip().replace("\n", " ")
    parts = (out["sm"].get("_pending_question_payloads") or {}).get("parts") or []
    chips = next((p for p in parts if p.get("type") == "chips"), None)
    return said, parts, chips

  print("  --- the lookup fires: a spoken filler, or nothing to read ---")
  for channel in ("voice", "chat", "TWILIO", "MOBILE", "base"):
    said, _, _ = turn(channel, base)
    print(f"  {channel:8} | {said[:66] or '(silent — chat shows a spinner)'}")

  print("  --- the question: wording, card and chip count all follow the surface ---")
  for channel in ("voice", "chat", "TWILIO", "MOBILE", "base"):
    said, parts, chips = turn(channel, {**base, "available_times": times},
                              done={"lookup_task": {"success": True,
                                                    "times": times}})
    n = len(chips["options"]) if chips else 0
    print(f"  {channel:8} card={'yes' if parts else 'no '} chips={n}/{len(times)}"
          f" | {said[:52]}")


def _said(out):
  return (out.get("agent_text") or "").lower()


# `Wednesday` appears ONLY in the long form of the appointment ask, so it is the
# cleanest tell that the right variant reached the caller.
#
# Assert on CONTENT, never on phrasing. The ask is delivered on a proceed turn,
# which means the engine hands its message to the model and the model writes the
# reply in its own words — live runs come back paraphrased ("I have Tuesday
# morning or Thursday afternoon" for "I've got Tuesday morning or Thursday
# afternoon"). A substring match on the authored wording looks precise and fails
# for the wrong reason; a match on which OPTIONS were offered is what the feature
# actually promises.
LIVE_CHECKS = [
    # (label, channel seeded into event_data, [user turns], predicate over the
    #  structured response of the LAST turn)
    #
    # Voice: the two-option brief, and NO payload — a card on a phone call is
    # content the caller can never perceive.
    ("voice speaks the brief form", "voice",
     ["hi", "8069100230359928"],
     lambda out: "wednesday" not in _said(out) and not out.get("payload")),
    # Chat: the full five-option list AND the card. Same slot, same config, same
    # deployed app, same turn — only the surface differs.
    ("chat gets the full list + card", "chat",
     ["hi", "8069100230359928"],
     lambda out: "wednesday" in _said(out) and bool(out.get("payload"))),
    # An alias nobody authored: CES's own telephony channel name resolves to voice.
    ("TWILIO aliases to voice", "TWILIO",
     ["hi", "8069100230359928"],
     lambda out: "wednesday" not in _said(out) and not out.get("payload")),
    # The footgun this feature fixes. Slot Studio sends the literal channel "base",
    # which used to match no override key and silently render the wrong branch.
    # Falling back must produce a real question, not silence.
    ("unknown channel falls back, not silent", "base",
     ["hi", "8069100230359928"],
     lambda out: bool(_said(out).strip()) and not out.get("payload")),
    # Latency masking follows the surface too: the spoken filler covers dead air on
    # a call, and must NOT appear in a chat window where it is a bubble saying
    # nothing. Asserted on the turn the lookup fires.
    ("voice speaks the filler", "voice",
     ["hi", "8069100230359928"],
     lambda out: "one moment" in _said(out) or "moment" in _said(out)),
    ("chat suppresses the filler", "chat",
     ["hi", "8069100230359928"],
     lambda out: "one moment while i check" not in _said(out)),
]


def _live_run(app_resource: str, app_dir: str, cxas_bin: str = "cxas") -> int:
  """Deploy and ASSERT against the real CES runtime. The offline sim is not proof."""
  import cxas_scrapi
  from flows.deploy.push import deploy

  deploy(app_dir, app_resource, cxas=cxas_bin)
  sessions = cxas_scrapi.Sessions(app_name=app_resource)

  failures = []
  for label, channel, turns, want in LIVE_CHECKS:
    session_id = sessions.create_session_id()
    seed = {"event_data": {"channel": channel}}
    out = {}
    try:
      for turn in turns:
        out = sessions.get_structured_response(
            sessions.run(session_id=session_id, text=turn, modality="text",
                         variables=seed, use_tool_fakes=True))
        # event_data is read on the session's first turn only; the engine sticks
        # the channel into its own state from there.
        seed = None
      ok = bool(want(out))
    except Exception as exc:  # noqa: BLE001 — a live failure is a result, not a crash
      ok, out = False, {"agent_text": f"!! {type(exc).__name__}: {exc}"}
    print(f"  {'ok  ' if ok else 'FAIL'} {label:34} "
          f"text={(out.get('agent_text') or '')[:58]!r} "
          f"payload={'yes' if out.get('payload') else 'no'}")
    if not ok:
      failures.append(label)

  print(f"\n{len(LIVE_CHECKS) - len(failures)}/{len(LIVE_CHECKS)} live checks passed")
  for f in failures:
    print(f"  FAILED {f}")
  return 1 if failures else 0


if __name__ == "__main__":
  import argparse
  import sys

  ap = argparse.ArgumentParser(description=__doc__)
  ap.add_argument("--out", default="./polymorphic_app")
  ap.add_argument("--live", metavar="APP_RESOURCE",
                  help="deploy here and assert against the real runtime")
  ap.add_argument("--cxas", default="cxas", help="path to the cxas CLI")
  args = ap.parse_args()

  errors, warnings = flows.validate_app(app)
  assert errors == [], errors
  _demo_run()
  flows.build_app(app, args.out, overwrite=True)
  print(f"built -> {args.out} (proves: one definition renders per surface —"
        " wording, cards and links all follow the surface's capabilities)")
  if args.live:
    print(f"\nlive: {args.live}")
    sys.exit(_live_run(args.live, args.out, args.cxas))
