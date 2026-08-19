"""Offline proof that the authored ladder picks the right rung for every scenario.

Seeds the status slots the before_agent hook would have written, runs one engine turn
against the EMITTED config, and reports which rung fired and what it would speak. This
is the cheap oracle: it exercises priority order and condition correctness without a
deploy, a model, or a backend.

    python ladder_check.py [--config built/tools/repair_dag/python_function/python_code.py]
"""

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import labs_paths  # noqa: E402

labs_paths.add_sdk_paths()

import scripts  # noqa: E402
from flows.engine import loader  # noqa: E402

FRAMEWORK_ROOT = labs_paths.framework_root()

# Rungs that speak in TWO parts: `say_first` on the turn the tool fires, `then_say` once
# it returns. The value is the approved sentence in full, and the check is that the two
# halves rejoin to exactly it, in that order.
#
# This map exists because the oracle was blind to spoken text. It captured `spoken` and
# only printed it, truncated to 110 characters — so a split could reorder the halves,
# drop one, or mute the rung entirely and still score 30/30. Both of this agent's worst
# voice defects passed validation and failed on the phone; a presence-only assertion is
# what let a wrong-order regression ship upstream, which is why this compares the joined
# string rather than checking each half is present somewhere.
# Which task holds the turn the sweep starts on: the context gate, which speaks the
# holding line and dispatches the rest behind it. Named rather than inlined because the
# scenarios using it pin "a sweep task fires here and no verdict does", and that assertion
# should not have to be rewritten in a dozen places if the first task changes again.
SWEEP_TASK = "ContextGate"

SPLIT_SCRIPTS = {
    # The reboot's halves are no longer two clauses of one approved sentence. D4 moved
    # the whole approved sentence behind `success_check` into `then_say`, because
    # asserting the signal had gone before the call returned was a lie on two of the
    # tool's three outcomes. What leads now is a holding line that promises nothing.
    # Pinned joined, as before, so the approved sentence still cannot be reworded or
    # reordered — only what precedes it has changed.
    "ExecuteReboot": scripts.SAY_REBOOT_HOLD_WHOLE,
    # R6 speaks the same two parts as the offered reboot: the caller asked for the same
    # action, so they hear the same words.
    "RebootOnRequest": scripts.SAY_REBOOT_HOLD_WHOLE,
    # The walkthrough rungs, split to cover the ~2s model wait rather than a tool
    # (see the FILLER_TIP_* note in scripts.py). Pinned joined like every other split,
    # so the approved half still cannot be reworded or reordered.
    "AskWifiScope": scripts.FILLER_ASK_SCOPE + " " + scripts.ASK_WIFI_SCOPE,
    # The two wordings share the filler on purpose: one call reaches exactly one of them,
    # so there is no tic to avoid and no second constant to keep in step.
    "AskWifiScopeAgain": scripts.FILLER_ASK_SCOPE + " " + scripts.ASK_WIFI_SCOPE_AGAIN,
    "WifiTipRejoin": scripts.FILLER_TIP_REJOIN + " " + scripts.SAY_WIFI_TIP_REJOIN,
    "WifiTipCloser": scripts.FILLER_TIP_CLOSER + " " + scripts.SAY_WIFI_TIP_CLOSER,
    "WifiTipToggle": scripts.FILLER_TIP_TOGGLE + " " + scripts.SAY_WIFI_TIP_TOGGLE,
    "WifiTipPlacement": (scripts.FILLER_TIP_PLACEMENT + " "
                         + scripts.SAY_WIFI_TIP_PLACEMENT),
    "WifiTipNearby": scripts.FILLER_TIP_NEARBY + " " + scripts.SAY_WIFI_TIP_NEARBY,
    "WifiTipRestart": scripts.FILLER_TIP_RESTART + " " + scripts.SAY_WIFI_TIP_RESTART,
    # The turn the sweep starts on. Split only to keep `ContextGate` out of the mute
    # shape, so the two halves must still rejoin to the approved sentence EXACTLY --
    # asserted below rather than restated, which is what stops the split drifting into
    # a reword.
    "ContextGate": scripts.SAY_BRIDGE_ACK + " " + scripts.SAY_BRIDGE_TO_SWEEP_REST,
    "HandleMissingHardware": scripts.SAY_MISSING_HARDWARE,
    "HandleNetworkTech": scripts.SAY_NETWORK_TECH,
    "HandleNetworkImpairment": scripts.SAY_NETWORK_GENERIC,
}

# Of the splits above, the ones whose LEAD is APPROVED COPY rather than a holding line.
#
# The distinction decides what a caller on a TEXT channel reads. `filler_say` is the
# framework's latency mask and the engine gates it on the surface's `filler` capability,
# which is False for `chat` -- and `text`, `web`, `webchat`, `api` and `mobile` all alias
# to `chat`. So on every text channel the lead is discarded, silently, and the `then_say`
# renders on its own. For a holding line that is correct: the surface shows a spinner
# rather than an empty bubble. For approved copy it is a defect, and it was one. Measured
# live in text, the technician verdict arrived as "Just so you know, you don't need to be
# home unless the technician needs access to your property" and nothing else -- the whole
# diagnosis gone, the logistics of a visit whose reason was never stated. Three of these
# four lose their entire diagnosis that way; `ContextGate` loses "Thanks."
#
# These four must therefore carry a `then_say` VARIANT that puts the whole sentence back
# on a surface that dropped the lead, and the complement -- the reboot pair and the seven
# walkthrough rungs, which lead with fillers this repo authored -- must not. Both
# directions are pinned: the shape in `check_split_shapes`, the rendered text in
# `check_text_projection`. This is the same partition `check_filler_echo` computes, and
# the reason the defect survived 79 green scenarios is that everything else in this file
# passes NO channel, so the engine resolves to voice and the voice projection was always
# correct.
SPLIT_COPY = ("ContextGate", "HandleMissingHardware",
              "HandleNetworkTech", "HandleNetworkImpairment")

# The state each copy split needs in order to be the rung that fires, so its TEXT
# projection can be replayed. Keyed by task name, like `FAILURE_SEEDS` below; a name in
# `SPLIT_COPY` with no seed here is a failure rather than a quiet skip.
TEXT_PROJECTION_SEEDS = {
    # The sweep has not run, so the gate itself takes the turn.
    "ContextGate": dict(diagnostics_complete=None),
    "HandleMissingHardware": dict(cable_modem_mac="NOT_FOUND", device_id="NOT_FOUND",
                                  gateway_status="offline"),
    "HandleNetworkTech": dict(network_status="impaired",
                              technician_type="network tech"),
    "HandleNetworkImpairment": dict(network_status="impaired"),
}

# The bridge line is approved copy that happens to be delivered in two parts. Splitting it
# is a delivery decision; rewording it is not one this file gets to make silently.
assert SPLIT_SCRIPTS["ContextGate"] == scripts.SAY_BRIDGE_TO_SWEEP, (
    "the ContextGate halves no longer rejoin to SAY_BRIDGE_TO_SWEEP:\n"
    f"  halves:   {SPLIT_SCRIPTS['ContextGate']!r}\n"
    f"  approved: {scripts.SAY_BRIDGE_TO_SWEEP!r}")

# The in-home walkthrough's speaking rungs. Every one of them says something that is only
# true once the checks have come back clean, so `check_early_scope_ask` holds them behind
# the offer -- with the single, argued exception of the scope question.
WALKTHROUGH_RUNGS = ("AskWifiScope", "AskWifiScopeAgain",
                     "WifiTipRejoin", "WifiTipCloser", "WifiTipToggle",
                     "WifiTipPlacement", "WifiTipNearby", "WifiTipRestart")

# What the caller is asked at the top of the call. Seeded into the post-executor replay
# below: with `reason_for_call` empty the engine correctly appends this to the verdict,
# which is faithful to a hypothetical caller who never said why they rang, but not to
# any real one — by the time a verdict lands the slot is filled. Leaving it unseeded
# makes every second half compare unequal for a reason that has nothing to do with copy.
_REASON_SEEDED = "internet is not working"


def _replay(config: dict, seeded: dict, channel: str = None,
            user_text: str = "my internet is not working"):
  """Drive one turn from a seeded state, then replay the tool's return.

  Returns `(fired, spoken, said_after, cascaded, action)`. `spoken` is what the caller
  hears as the tool is dispatched, `said_after` what they hear once it returns — the two
  halves of a split rung, and the only way to see which of them a given surface renders.

  `channel` is passed through as `event_data`, and passing NONE is the normal case: the
  engine then resolves to voice, which is the projection every scenario below is written
  against. `check_text_projection` is the one caller that sets it, because the surface a
  turn is rendered for is otherwise invisible here — and a whole class of defect lives
  in that blind spot.

  Extracted from the scenario loop rather than copied, so a second caller cannot model a
  turn the scenarios do not: the async placeholder and the `success_check` replay below
  are both hard-won corrections to what "the tool returned" means.
  """
  sm = loader.seed_sm(config)
  sm["filled"] = dict(seeded, accountNumber="8344200010126021")
  sm.setdefault("pending", {})
  sm[sm.get("_gate_slot") or "active_flow"] = "repair"
  if sm.get("_gate_slot"):
    sm["filled"][sm["_gate_slot"]] = "repair"
  kwargs = {"event_data": {"channel": channel}} if channel else {}

  # Drive with real user text. An empty-text turn is the engine's post-tool
  # re-invoke, not a caller turn, and it hides the deferral behaviour that decides
  # whether a rung fires at all — the outage rung passed this check with empty text
  # while silently failing live.
  out = loader.run_engine(config, sm, last_user_text=user_text, config_id="repair",
                          **kwargs)
  action = out.get("action", {})
  fired = (action.get("task") or {}).get("name") or action.get("task_name")
  if not fired and action.get("function_call"):
    tool = action["function_call"].get("name")
    for t in config["tasks"]:
      if t.get("tool") == tool:
        fired = t["name"]
        break        # first match: two rungs may legitimately share a tool
  spoken = action.get("message") or ""

  # A rung must fire EXACTLY once per turn. The rungs are not terminal (see the
  # `rung` docstring), so the engine keeps walking the task list within the same
  # turn; only the `verdict_delivered` latch that each rung returns stops the next
  # matching rung from speaking too. Replay the intake to prove the latch lands.
  cascaded = None
  said_after = ""
  if fired:
    # Latch what the rung ACTUALLY outputs, rather than `verdict_delivered` for
    # everything. Most rungs do latch that, but the walkthrough's do not — the
    # all-clear latches its offer, and the four tips share one per-turn flag — so
    # hardcoding it replayed a state no real turn produces and reported cascades that
    # cannot happen.
    fired_task = next(t for t in config["tasks"] if t["name"] == fired)
    # An ASYNCHRONOUS task answers twice, and the FIRST answer is the platform's
    # `{"result": "pending"}` placeholder -- the real payload arrives a turn or more
    # later as a synthetic user turn. Replaying a success here would model a turn that
    # never happens and walk the cascade past a dispatch the engine actually stops at
    # (`_is_async_pending` -> `_async_hold`), reporting a verdict-in-the-same-breath
    # that no caller can hear. Replay the placeholder instead: the outputs are NOT
    # filled, because on this turn they genuinely are not known yet.
    if fired_task.get("awaits"):
      sm.setdefault("task_results", {})[fired] = {"result": "pending"}
    else:
      outs = dict(fired_task.get("outputs") or {}) or {"v": "verdict_delivered"}
      sm.setdefault("task_results", {})[fired] = dict(
          {"success": True}, **{k: "true" for k in outs})
      # A task with `success_check` is asking the engine a question about its own
      # result, and the engine maps NO outputs when the answer is falsy. Synthesising
      # a result without that key replays a FAILED tool, so the reboot's success path
      # scored as a cascade into its own failure ladder. This is the success replay;
      # the failure one is below, and is deliberately separate.
      if fired_task.get("success_check"):
        sm["task_results"][fired][fired_task["success_check"]] = True
      for _slot in outs.values():
        sm["filled"][_slot] = "true"
    sm["filled"].setdefault("reason_for_call", _REASON_SEEDED)
    sm["_task_just_completed"] = fired
    nxt = loader.run_engine(config, sm, last_user_text="", config_id="repair", **kwargs)
    na = nxt.get("action", {})
    said_after = na.get("message") or ""
    nfc = (na.get("function_call") or {}).get("name")
    if nfc:
      cascaded = nfc
  return fired, spoken, said_after, cascaded, action


_ECHO_STOP = frozenset("""a an and at do does for i if in is it let me my of on so s t
the that this to us we with you your""".split())


def check_filler_echo(config: dict) -> int:
  """A filler must not say the same word as the line it introduces.

  A filler is spoken as a partial preempt and the real line follows in the same breath,
  so a shared word lands twice a second apart. Recorded live: "Thanks, let me pull that
  up. Thanks. Give me just a moment while I check your connection", and "Right. Let me
  check. Just so I check the right thing."

  Only fillers this repo AUTHORED are checked. The split verdict rungs also carry a
  `filler_say`, but there it is the first half of an approved sentence and any repetition
  inside it is the source's, not ours to fix — those are pinned by SPLIT_SCRIPTS instead.
  """
  import re
  failures = 0
  authored = {t["name"] for t in config.get("tasks", [])
              if t.get("filler_say") and t["name"] not in SPLIT_SCRIPTS} | set(
              WALKTHROUGH_RUNGS)
  words = lambda x: {w for w in re.findall(r"[a-z']+", str(x).lower())
                     if w not in _ECHO_STOP}
  pairs = [(t["name"], t.get("filler_say"), t.get("then_say"))
           for t in config.get("tasks", []) if t["name"] in authored]
  pairs += [(sl["name"], sl.get("filler_say"), sl.get("ask"))
            for sl in config.get("slots", []) if sl.get("filler_say")]
  for name, filler, line in pairs:
    if not filler or not line:
      continue
    for one in (filler if isinstance(filler, list) else [filler]):
      if not one:
        continue
      dup = words(one) & words(line)
      if dup:
        print(f"FAIL {name:28} filler {one!r} echoes {sorted(dup)} from the line it "
              f"introduces")
        failures += 1
  return failures


SCOPE_ONE_DEVICE = [
    "only my xFi pod, everything else is fine",
    "just my camera, everything else works",
    "only the TV box, everything else is fine",
]


def check_scope_cues(config: dict) -> int:
  """One device, said the broad way, must not read as ALL of them.

  `wifi_scope` carries `cue_priority="first"`, so a phrase matching BOTH values is not
  handed to the model as ambiguous — the earliest declared value takes it, silently. The
  bare word "everything" put "only my xFi pod, everything else is fine" on the
  ALL_DEVICES branch, and the caller was answered "since it's everything, let's look at
  the gateway" one breath after saying it was not everything, then walked through moving
  a gateway that was not the problem.

  Every SCENARIO below seeds `wifi_scope` with a value outright, so none of them touches
  the cue matching that decides it — which is how this survived a green suite. This
  checks the words.

  Checked on BOTH slots that can capture the answer. `wifi_scope_early` listens during
  the sweep and `wifi_scope` after the verdict, and they must agree: a phrase reading as
  ONE_DEVICE early and ALL_DEVICES later would make the diagnosis depend on when the
  caller happened to say it. They share one cue map in app.py, and this is what stops
  someone helpfully "fixing" one of them in isolation.
  """
  import re
  failures = 0
  by_name = {s["name"]: s for s in config.get("slots", [])}
  for slot_name in ("wifi_scope", "wifi_scope_early"):
    cues = (by_name.get(slot_name) or {}).get("option_cues")
    if not cues:
      print(f"FAIL {slot_name} has no option_cues — this check needs re-pointing")
      failures += 1
      continue
    for utt in SCOPE_ONE_DEVICE:
      matched = [v for v, pats in cues.items()
                 if any(re.search(p, utt, re.IGNORECASE) for p in pats)]
      if matched != ["ONE_DEVICE"]:
        print(f"FAIL {slot_name} {utt!r} matched {matched} — want exactly "
              "['ONE_DEVICE']; two matches let cue_priority take the earliest declared")
        failures += 1
  early = (by_name.get("wifi_scope_early") or {}).get("option_cues")
  late = (by_name.get("wifi_scope") or {}).get("option_cues")
  if early and late and early != late:
    print("FAIL wifi_scope and wifi_scope_early no longer share a cue map — the same "
          "words would mean different things before and after the verdict")
    failures += 1
  return failures


def check_early_scope_ask(config: dict) -> int:
  """The scope question may be asked during the sweep; nothing else may.

  Asking scope while the diagnostics job runs is safe because it asserts NOTHING: it is a
  symptom, useful whether the checks come back clear, an outage or a hardware fault. The
  rest of the walkthrough is not. Offering in-home troubleshooting to a caller who turns
  out to have an area outage is the AdviseAppSpecific defect, and what keeps that from
  happening is that every other walkthrough rung is gated behind the offer.

  It is an ANNOUNCE, not a rung, and that is what lets it be heard on the turn it belongs
  to -- only one TASK speaks per turn, so as a rung it landed a whole inactivity tick late
  (measured: 9.4s at an 8s timeout, 5.6s at 3s). It used to be a `{scope_ask}` placeholder
  filled by a before_agent callback; the announce takes a condition directly and latches
  its own name, so both the derivation and its latch are gone. This checks it stays gone.
  """
  failures = 0
  tasks = {t["name"]: t for t in config.get("tasks", [])}
  slots = {s["name"]: s for s in config.get("slots", [])}

  def slots_in(cond):
    out = set()
    if isinstance(cond, dict):
      if "slot" in cond:
        out.add(cond["slot"])
      for v in cond.values():
        out |= slots_in(v)
    elif isinstance(cond, list):
      for v in cond:
        out |= slots_in(v)
    return out

  early = slots.get("AskScopeEarly")
  if not early or early.get("source") != "announce":
    print("FAIL AskScopeEarly is missing or is no longer an announce — as a task it cannot "
          "be heard until the next turn, which on a silent line is an inactivity tick away")
    return 1

  # Verbatim. `texts` reach the caller as written; `message` is handed to the model to
  # reword, and this is approved copy.
  said = " ".join(p.get("text", "") for p in (early.get("response") or [])
                  if p.get("type", "text") == "text")
  if said.strip() != scripts.ASK_WIFI_SCOPE_EARLY.strip():
    print(f"FAIL AskScopeEarly no longer speaks the approved line verbatim: {said!r}")
    failures += 1

  # The engine reads `.get("preempt", True)` while the DSL defaults to False, so an
  # announce that omits the key means the opposite of what it looks like.
  if not early.get("preempt"):
    print("FAIL AskScopeEarly does not set preempt — the line is handed to the model "
          "rather than spoken as written")
    failures += 1

  # `has_mac` is ContextGate's own output, so requiring it is what puts the question on the
  # turn the gate ANSWERS rather than the turn it is dispatched.
  if "has_mac" not in (early.get("requires") or []):
    print("FAIL AskScopeEarly does not require `has_mac` — it no longer rides the turn "
          "ContextGate answers on")
    failures += 1

  early_gates = slots_in(early.get("condition"))
  for forbidden in ("wifi_offered", "wifi_answer_allowed", "verdict_delivered"):
    if forbidden in early_gates:
      print(f"FAIL AskScopeEarly is gated on {forbidden!r} — it fires before the verdict, "
            "so that gate can never open and the question is never asked early")
      failures += 1
  for required in ("diagnostics_complete", "network_status"):
    if required not in early_gates:
      print(f"FAIL AskScopeEarly does not gate on {required!r} — it would ask the scoping "
            "question after the checks are back, or on a path with no wait at all")
      failures += 1

  # BOTH wordings of the post-verdict ask, or the gate covers one of them and the other
  # asks a caller a question they answered thirty seconds earlier.
  for name in ("AskWifiScope", "AskWifiScopeAgain"):
    late = tasks.get(name)
    if late and "wifi_scope" not in slots_in(late.get("condition")):
      print(f"FAIL {name} is not gated on `wifi_scope` being unfilled — a caller who "
            "answered during the sweep is asked the same question again after the verdict")
      failures += 1

  # ...and the two must be kept apart by the announce's latch, in both directions. They
  # share `wifi_scope_asked`, so an overlap is not two questions -- it is one question in
  # whichever wording wins, and the caller who has already heard it hears it verbatim
  # again, which is the defect the second wording exists to fix.
  again = tasks.get("AskWifiScopeAgain")
  if again and "AskScopeEarly" not in slots_in(again.get("condition")):
    print("FAIL AskWifiScopeAgain does not gate on `AskScopeEarly` — it would say "
          "'back to the one thing I asked earlier' to a caller who was never asked")
    failures += 1
  first = tasks.get("AskWifiScope")
  if first and "AskScopeEarly" not in slots_in(first.get("condition")):
    print("FAIL AskWifiScope does not gate on `AskScopeEarly` — it outranks the re-ask "
          "on declaration order, so the question is put twice in identical words")
    failures += 1

  for name in WALKTHROUGH_RUNGS:
    gates = slots_in((tasks.get(name) or {}).get("condition"))
    if not gates & {"wifi_offered", "wifi_answer_allowed"}:
      print(f"FAIL {name} is not gated on the walkthrough having been offered — it could "
            "speak in-home advice on a call whose checks have not come back")
      failures += 1

  # The callback must stay deleted. A hook is an escape hatch, and this one was replaced by
  # a primitive that already existed; re-deriving the question in Python would put the
  # conversation logic back somewhere the validator and these oracles cannot see it.
  hook_src = (pathlib.Path(__file__).resolve().parents[1] / "hooks.py").read_text()
  # Comment lines stripped, or this trips on the note explaining the deletion.
  hook_code = "\n".join(ln for ln in hook_src.splitlines()
                        if not ln.lstrip().startswith("#"))
  # Quoted, because `wifi_scope_asked` contains `scope_ask` as a substring.
  if '"scope_ask"' in hook_code:
    print("FAIL hooks.py derives `scope_ask` again — the announce takes a condition "
          "directly, so this belongs in the DAG")
    failures += 1

  anchor = 'state.get("wifi_scope_early")'
  if anchor not in hook_src:
    print("FAIL hooks.py no longer promotes wifi_scope_early into wifi_scope — an answer "
          "given during the sweep is captured and then dropped")
    failures += 1
  else:
    block = hook_src.split(anchor, 1)[1][:600]
    for key in ("wifi_scope", "wifi_scope_asked", "wifi_scope_allowed"):
      if f'"{key}"' not in block:
        print(f"FAIL hooks.py promotes wifi_scope_early without setting {key!r} — the "
              "promotion has to be atomic or the slot is deactivated and the value lost")
        failures += 1
  return failures


def check_two_turn_gates(config: dict) -> int:
  """A rung that CONSUMES an answer must be gated on the question having been asked.

  Both of this agent's yes/no questions are spoken by a rung and answered by a slot, and
  a question asked and answered inside one turn is the model answering for the caller.
  Two things have to hold, and only one of them is about the slot:

    * the slot may not be FILLED until the turn after the question — that is the
      `<x>_allowed` gate the hook derives, and it lives on the slot;
    * the consuming RUNG may not FIRE until then either — because a value can reach a
      slot without going through the slot's own gate (a cue match on a turn where it
      happened to be open, a carry-in from an earlier flow, a seeded variable), and the
      rungs are not terminal, so the engine walks straight on to the next eligible one.

  The reboot pair has both. The Wi-Fi walkthrough had only the first, and the second is
  what stops the offer turn from also asking the scoping question. The SCENARIOS below
  cannot see it: every walkthrough case seeds `wifi_offered="true"`, so they all describe
  the turn AFTER the offer and none of them describes the offer turn itself.
  """
  # (rung, the gate its condition must name, what it consumes)
  pairs = [("AskWifiScope", "wifi_answer_allowed", "the walkthrough offer"),
           ("WifiDeclined", "wifi_answer_allowed", "the walkthrough offer"),
           ("ExecuteReboot", "reboot_answer_allowed", "the reboot offer"),
           ("DeclineRebootTransfer", "reboot_answer_allowed", "the reboot offer")]
  failures = 0
  for rung, gate, question in pairs:
    task = next((t for t in config.get("tasks", []) if t["name"] == rung), None)
    if task is None:
      print(f"FAIL {rung} is not in the config — this check needs re-pointing")
      failures += 1
      continue
    if gate not in json.dumps(task.get("condition") or {}):
      print(f"FAIL {rung} consumes {question} without naming {gate!r} — it can fire on "
            "the same turn the question is asked, so the caller cannot answer")
      failures += 1
  return failures


def check_filler_pool_collisions() -> int:
  """No two fillers a caller can hear in one call may open on the same word.

  A pool is sampled at random, so two slots drawing from overlapping vocabularies will
  eventually open two turns of one call the same way -- "Okay, one moment." followed by
  "Okay, let's do that.", which reads as the agent having one word for everything.

  This is checked on the POOLS, not on driven journeys, because that is where the property
  lives. Auditing 43 live turns across 12 journeys did not catch the collision that
  prompted this: no call happened to draw it. A sampled defect needs a static check.

  Compares first WORDS rather than whole phrases: "Okay." and "Okay, one moment." are the
  same opening to a listener, and whole-phrase equality would pass them both.
  """
  import re as _re
  pools = {n: v for n, v in vars(scripts).items()
           if n.startswith("FILLER") and isinstance(v, (str, list))}
  # A FLOOR, because this check reads its own denominator. Most of the copy now lives in
  # the journey modules and reaches `scripts` through the re-export block at the bottom of
  # that file; if that block ever stops binding names eagerly -- a PEP-562 `__getattr__`
  # is the obvious "tidy-up" -- `vars()` returns nothing, this finds zero pools, compares
  # zero of them and reports success. A check that silently stops checking is worse than
  # no check, so it now fails loudly instead.
  if len(pools) < 11:
    print(f"  FAIL filler pools: found {len(pools)}, expected at least 11 — the copy is "
          f"not reaching `vars(scripts)` and this check is inspecting nothing")
    return 1
  first = {}
  failures = 0
  for name, val in sorted(pools.items()):
    for phrase in ([val] if isinstance(val, str) else val):
      word = _re.split(r"[ ,.!?]", phrase.strip().lower())[0]
      if word in first and first[word] != name:
        print(f"FAIL filler pools {name} and {first[word]} both open on {word!r} "
              f"({phrase!r}) — one call can draw both")
        failures += 1
      else:
        first[word] = name
  return failures


def check_reassurance_ladders(config: dict) -> int:
  """One reassurance ladder per wait, and no line said by two of them.

  `_async_idle_line` walks the waits that are pending and holds a counter PER WAIT, so
  two concurrent tasks carrying the same `while_waiting` list do not share it — the
  first drains, the loop falls through to the second, and the caller hears the whole
  ladder over again from the top. `Specialists` and the `SweepLegs` group were both set
  to `_DEMO_WAITING`, which in the demo build is ONE line: the same sentence twice, on
  consecutive ticks, from a mechanism that exists specifically to DRAIN rather than
  cycle.

  Checked statically, for the reason the filler-pool check is: whether the duplicate is
  audible depends on how many ticks the wait happens to get. The live drive that signed
  this off got one tick and heard one line, and the second copy was simply never
  reached. Compares the LINES, not the lists, so a re-worded copy of the same
  reassurance is caught too.
  """
  failures = 0
  owner: dict[str, str] = {}
  for task in config.get("tasks", []):
    for line in ((task.get("awaits") or {}).get("while_waiting") or []):
      key = " ".join(str(line).lower().split())
      if key in owner and owner[key] != task["name"]:
        print(f"FAIL {task['name']} and {owner[key]} both reassure with {line!r} — they "
              "wait concurrently, so the ladder drains twice and the caller hears it "
              "again. Declare while_waiting on the LONGEST wait only")
        failures += 1
      else:
        owner[key] = task["name"]
  return failures


def check_failure_ladders(config: dict) -> int:
  """A rung that can be REFUSED must not promise the thing it was refused.

  D4. `verdict_execute_reboot` announced "I'm sending a signal to reboot your gateway
  now" as the tool was DISPATCHED, so the two outcomes where the gateway declines —
  `timeline_blocked` after a recent restart, `error` when Convoy is unreachable — were
  announced as successes. Three outcomes, one sentence.

  Replays a FAILED executor, which the scenario loop below cannot: it synthesises a
  successful result, so it only ever walks the happy half. Asserts three things about
  every task carrying `success_check`:

    * the failure is spoken, and speaks the on_exhaust line rather than `then_say`
    * `then_say`'s claim does NOT reach the caller on that path
    * the output slot does not latch, so the ladder stays open behind it
  """
  failures = 0
  by_name = {t["name"]: t for t in config.get("tasks", [])}
  # Driven from the registry rather than from the config, because `success_check` is not
  # the marker it looks like: the builder writes `success_check: "success"` on all 24
  # tasks by default, matching the `{"success": True}` every executor returns. Only a
  # task whose tool can legitimately REFUSE carries a meaningful one. Iterating the
  # registry means a rung that loses its `on_failure` fails here instead of quietly
  # dropping out of the loop.
  for name, seed in FAILURE_SEEDS.items():
    task = by_name.get(name)
    if task is None:
      print(f"FAIL {name:28} is in FAILURE_SEEDS but not in the config — stale entry")
      failures += 1
      continue
    check = task.get("success_check")
    if not check or check == "success":
      print(f"FAIL {name:28} has no meaningful success_check ({check!r}) — its tool's "
            f"refusal cannot be distinguished from its success")
      failures += 1
      continue
    exhaust = ((task.get("on_failure") or {}).get("on_exhaust") or {})
    if not exhaust.get("say"):
      print(f"FAIL {name:28} has success_check and no on_exhaust.say — a refused "
            f"tool would be silent")
      failures += 1
      continue
    sm = loader.seed_sm(config)
    sm["filled"] = dict(scenario(**seed), accountNumber="8344200010126021")
    sm.setdefault("pending", {})
    # The shape the engine sees when the tool ran and reported it did NOT do the thing:
    # success at the transport level, the checked key falsy.
    sm.setdefault("task_results", {})[name] = {"success": True, check: False}
    sm["_task_just_completed"] = name
    sm["filled"].setdefault("reason_for_call", _REASON_SEEDED)
    out = loader.run_engine(config, sm, last_user_text="", config_id="repair")
    said = (out.get("action", {}).get("message") or "").strip()
    if exhaust["say"] not in said:
      print(f"FAIL {name:28} refused tool did not speak its on_exhaust line")
      print(f"       said    : {said!r}")
      print(f"       expected: {exhaust['say']!r}")
      failures += 1
    then_say = (task.get("then_say") or "").strip()
    if then_say and then_say in said:
      print(f"FAIL {name:28} refused tool STILL promised the thing it did not do")
      print(f"       said: {said!r}")
      failures += 1
    for slot in (task.get("outputs") or {}).values():
      if sm["filled"].get(slot):
        print(f"FAIL {name:28} refused tool latched {slot!r} — ladder is closed behind "
              f"a verdict that never happened")
        failures += 1
  return failures


def check_latches_are_real(config: dict, app_dir: str) -> int:
  """A rung's latch has to come back as "true", not as whatever the stub invented.

  Every rung here is say-only: the script IS the effect, and the one thing its executor
  does is RETURN its latch so the engine maps it into `filled`. An executor missing from
  the registry in source_tools still BUILDS -- the emitter writes a generic stub for it --
  and the stub returns `str(abs(hash("x")) % 100000)` under the out_key. The slot fills,
  so nothing downstream that only asks "is it filled?" notices; everything that compares
  it to "true" silently stops working.

  MEASURED, on the whole-house wording of the mid-sweep offer. Its rung was added with a
  new tool name that was never registered, so `wifi_offered_early` landed as "15400".
  `wifi_gates` opens the answer gate with `== "true"`, so it never opened, the acceptance
  was never collectible, no tip was ever eligible, and the model was handed the
  walkthrough -- which ran out of directed turns and closed the call on "I'm not able to
  take this any further". Every offline check passed: the rung fired, the copy was right,
  the slot filled. Only the VALUE was wrong.

  Read off the emitted tool source rather than the config, because that is where the
  difference lives. `python app.py` reports nothing, and the two rungs are identical in
  the DAG.
  """
  failures = 0
  for task in config.get("tasks", []):
    tool = task.get("tool") or ""
    outs = task.get("outputs") or {}
    # Say-only rungs only: an executor with real inputs returns real values, and a
    # latch it computes is not this defect.
    if not tool.startswith("verdict_") or task.get("inputs") or len(outs) != 1:
      continue
    path = os.path.join(app_dir, "tools", tool, "python_function", "python_code.py")
    if not os.path.exists(path):
      print(f"FAIL {task['name']:28} has no emitted executor at {tool}")
      failures += 1
      continue
    src = open(path).read()
    if "Executor stub" in src:
      print(f"FAIL {task['name']:28} runs the emitter's generic STUB — {tool} is not "
            f"registered in source_tools, so its latch is a hash, not \"true\"")
      failures += 1
      continue
    latch = next(iter(outs.values()))
    if f'"{latch}": "true"' not in src:
      print(f"FAIL {task['name']:28} executor never returns {latch!r} as \"true\" — "
            f"anything gating on that value can never open")
      failures += 1
  return failures


def check_escalation_hold(config: dict) -> int:
  """A request for a human is HELD while the sweep runs, and honoured once it lands.

  A hand-off made before the diagnostics answer carries nothing: `EscalateHandoffSummary`
  is the chain that gives it content, and there is no content yet. The receiving human
  would start the same conversation over on a caller who has already sat through it. So
  the escalate gate waits for `diagnostics_complete`, and the caller hears WHY.

  Not a SCENARIO row, and that is not a shortcut. Every row above asserts which RUNG
  fired; a declined control request fires no rung at all — the engine returns a preempt
  from `_handle_terminal_slots` before the DAG walk is reached — so the row shape cannot
  see it. It also cannot see the exit, which is what this check is really for: a hold is
  only defensible if it ends, and the two ways it ends are three refusals apart.

  Driven against the real engine, one ask per turn, with `escalate` pending exactly as
  the `transfer_to_human` setter leaves it.
  """
  failures = 0
  esc = config.get("escalate") or {}
  hold = scripts.SAY_HOLD_FOR_CHECKS
  hold_again = scripts.SAY_HOLD_FOR_CHECKS_AGAIN
  transfer = (esc.get("say") or "").strip()

  refusals = {hold, hold_again, scripts.SAY_OUTAGE_NO_AGENT}

  def ask(filled, times=1):
    """Ask for a human `times` times against one seeded state; return what was said."""
    sm = loader.seed_sm(config)
    sm["filled"] = dict(filled, accountNumber="8344200010126021")
    sm["pending"] = {}
    said = []
    for i in range(1, times + 1):
      sm["pending"]["escalate"] = True
      out = loader.run_engine(config, sm, last_user_text="I want to talk to a person",
                              config_id="repair", n_user_turns=i)
      said.append((out.get("action", {}).get("message") or "").strip())
    return said, sm

  def honoured(filled):
    """Was the request ALLOWED — either spoken through, or armed on the escalate rail?

    Two shapes reach a human and only one of them speaks on the asking turn. With the
    `EscalateHandoffSummary` chain eligible, the rail arms first and the chain owns the
    turn silently (`_escalate_path`), and the disposition line follows once it drains;
    with the chain gated off, the disposition speaks at once. Asserting only on the
    spoken line would score the first shape as a failure and the second as a pass, for
    a difference that has nothing to do with whether the caller reaches anybody.
    """
    said, sm = ask(filled)
    reached = bool(sm.get("_escalate_path")) or transfer in said[0]
    return reached and said[0] not in refusals, said[0]

  # Mid-sweep: the account is known, nothing has reported. The hold, not a hand-off.
  mid = {"caller_spoke": "true", "scope_ask": "",
         "reason_for_call": _REASON_SEEDED}
  said, _ = ask(mid)
  if said[0] != hold:
    print("FAIL escalation mid-sweep was not held on the authored line")
    print(f"       said    : {said[0]!r}")
    print(f"       expected: {hold!r}")
    failures += 1

  # ...and asking again keeps holding rather than falling through to a human on the
  # second or third try, which is the whole requirement. A second identical sentence
  # reads as the agent not listening, so the ladder's second rung answers instead.
  said, _ = ask(mid, times=3)
  if said != [hold, hold_again, hold_again]:
    print("FAIL a repeated request mid-sweep did not keep getting the hold line")
    print(f"       said: {said!r}")
    failures += 1
  if any(transfer and transfer in s for s in said):
    print("FAIL a repeated request mid-sweep reached the hand-off line anyway")
    failures += 1

  # THE EXIT, and the reason the hold is not a trap. Three refusals is the cap; the
  # fourth ask goes through whatever the sweep is doing. Without this a caller whose
  # sweep never answers can never reach a person: a declined control request returns
  # before `steer_back` is reached, so repeated asks do not advance that ladder either.
  said, _ = ask(mid, times=4)
  if transfer not in said[3]:
    print("FAIL insisting four times did not reach a human — the hold has no exit")
    print(f"       said: {said[3]!r}")
    failures += 1

  # The ordinary exit: the sweep lands and escalation behaves exactly as it did before.
  ok, said0 = honoured(scenario())
  if not ok:
    print("FAIL escalation after the sweep completed was not honoured")
    print(f"       said: {said0!r}")
    failures += 1

  # ...and a verdict reached without the sweep completing releases it too.
  ok, said0 = honoured(dict(mid, verdict_delivered="true"))
  if not ok:
    print("FAIL escalation after a verdict was spoken was not honoured")
    print(f"       said: {said0!r}")
    failures += 1

  # The OTHER refusal still speaks its own words. Two reasons share one field, and the
  # regression this guards is the caller in an outage hearing the hold line — being told
  # to wait for checks that have already answered, and never getting the advisory.
  said, _ = ask(scenario(outage_status="active", network_status="skipped",
                         gateway_status="skipped", wifi_status="skipped"))
  if said[0] != scripts.SAY_OUTAGE_NO_AGENT:
    print("FAIL an outage refusal no longer speaks the outage line")
    print(f"       said    : {said[0]!r}")
    print(f"       expected: {scripts.SAY_OUTAGE_NO_AGENT!r}")
    failures += 1
  # ...and insisting does NOT talk its way past it. The count-based exit above releases
  # the wait, never the policy: no amount of asking makes a live agent able to fix a
  # street the crew is already on.
  said, _ = ask(scenario(outage_status="active", network_status="skipped",
                         gateway_status="skipped", wifi_status="skipped"), times=4)
  if any(transfer and transfer in s for s in said):
    print("FAIL insisting during an outage reached a live agent after all")
    print(f"       said: {said!r}")
    failures += 1
  return failures


def check_split_shapes(config: dict) -> int:
  """Static guards on the two-part shape, before any scenario runs.

  A rung carrying `filler_say` with no SECOND PART is the recorded MUTE shape: measured,
  "How long until it's back?" asked straight after the reboot returned empty agent text
  4 times in 5, while every rung that had a `then_say` answered it fine. The root cause
  was never established, so this is a shape ban rather than a fix.

  A `then_directive` counts as the second part. The original measurement only ever covered
  `then_say`, because nothing carried a directive at the time; the device-help searches
  do, and cannot use a `then_say` — pinning search results produces a paragraph of
  stitched-together fragments instead of an answer. Re-measured on that shape over voice,
  filler + directive spoke on 6 of 6. That does not weaken the ban on filler with NOTHING
  after it, which is what the 4-in-5 was.
  """
  failures = 0
  for task in config.get("tasks", []):
    name, filler = task["name"], task.get("filler_say")
    then = task.get("then_say") or task.get("then_directive")
    if not filler:
      continue
    if not then:
      print(f"FAIL {name:28} has say_first and NO second part — the mute shape")
      failures += 1
    elif not task.get("then_say"):
      continue  # directive-backed: composed, so there is no fixed script to pin
    elif name not in SPLIT_SCRIPTS:
      print(f"FAIL {name:28} is split but unpinned — add it to SPLIT_SCRIPTS")
      failures += 1
  # ...and the copy/holding-line partition, in both directions. A copy split with no
  # variant is half-mute on every text channel; a holding line WITH one re-injects "give
  # me just a moment" in front of an answer that has already arrived. The two sets have
  # to stay where they are, and this is what stops them drifting.
  for task in config.get("tasks", []):
    name = task["name"]
    has_variants = bool(task.get("then_say_variants"))
    if name in SPLIT_COPY and not has_variants:
      print(f"FAIL {name:28} leads with approved COPY and carries no then_say variant "
            f"— its lead is dropped on every text channel")
      failures += 1
    elif task.get("filler_say") and name not in SPLIT_COPY and has_variants:
      print(f"FAIL {name:28} leads with a HOLDING LINE and carries a then_say variant "
            f"— a text surface would read the mask it was right to drop")
      failures += 1
  for name in SPLIT_COPY:
    if name not in TEXT_PROJECTION_SEEDS:
      print(f"FAIL {name:28} is a copy split with no TEXT_PROJECTION_SEEDS entry — its "
            f"text projection cannot be replayed")
      failures += 1
  return failures


def check_text_projection(config: dict) -> int:
  """What a caller on a TEXT channel actually reads from a two-part rung.

  Every other scenario in this file passes no channel at all, so the engine resolves to
  voice (`_DEFAULT_SURFACE`) and renders the voice projection. That is exactly why 79
  green scenarios could not see a defect that only exists off voice: the lead of a split
  verdict lives in `filler_say`, which a surface with no `filler` capability discards.

  So this replays each copy split a second time with `channel: "text"` and asserts the
  whole approved sentence still reaches the caller -- as ONE message this time, since the
  turn that would have carried the lead is silent by design. `chat` is driven too,
  because `text` merely aliases to it and an alias is a thing that can be removed.
  """
  failures = 0
  for name in SPLIT_COPY:
    seed = TEXT_PROJECTION_SEEDS.get(name)
    if seed is None:
      continue  # already reported by check_split_shapes
    approved = SPLIT_SCRIPTS[name]
    for channel in ("text", "chat"):
      fired, spoken, said_after, _, _ = _replay(config, scenario(**seed),
                                                channel=channel)
      if fired != name:
        print(f"FAIL {name:28} [{channel}] fired {fired!r}, so the projection was not "
              f"measured — fix TEXT_PROJECTION_SEEDS")
        failures += 1
        continue
      if spoken.strip():
        print(f"FAIL {name:28} [{channel}] spoke a filler on a surface that cannot "
              f"render one: {spoken!r}")
        failures += 1
      if said_after.strip() != approved:
        print(f"FAIL {name:28} [{channel}] does not read as the whole approved sentence")
        print(f"       read    : {said_after!r}")
        print(f"       approved: {approved!r}")
        failures += 1
  return failures


def check_fee_rungs_fire_on_the_asking_turn(config: dict) -> int:
  """The three fee answers must not be held back to the turn after the question.

  A say-only task with no `inputs` and no `requires` is parked by the engine on any turn
  the caller has spoken while an askable slot is still unfilled, so it cannot preempt the
  model's setter. These three rungs answer a caller who has JUST spoken, by definition --
  the cue that arms them is in the very utterance being held against them. Measured, with
  the walkthrough consent or the account number outstanding: "will I be charged for
  that?" filled the cue, the rung was held, and the pending question was re-asked
  verbatim instead -- and again on the next turn, because a cue turn advances no retry
  counter.

  `requires=["cost_question"]` opts them out. It gates nothing new: `cost_question` is
  already the first leg of all three conditions. This check exists because the hold is
  invisible offline -- `_turn_user_text` is only set from a real turn -- so no scenario
  in this file can fail when the keyword is dropped again.
  """
  failures = 0
  for name in ("AnswerFeeAgain", "AnswerServiceFee", "AnswerNoCharge"):
    task = next((t for t in config.get("tasks", []) if t["name"] == name), None)
    if task is None:
      print(f"FAIL {name:28} is missing from the config entirely")
      failures += 1
      continue
    if "cost_question" not in (task.get("requires") or []):
      print(f"FAIL {name:28} has no requires=['cost_question'] — it is held back to the "
            f"turn AFTER the money question, and the pending ask re-asks instead")
      failures += 1
  return failures

# The state each `success_check` rung needs in order to be the one that fires, so its
# REFUSAL can be replayed. Keyed by task name; `check_failure_ladders` fails a task that
# grows a `success_check` without one, rather than skipping it quietly.
FAILURE_SEEDS = {
    "ExecuteReboot": dict(gateway_status="reboot", confirm_reboot=True,
                          reboot_offered="true"),
    # R6 reaches the same restart by a different door, so its refusal has to be replayed
    # separately — a shared `on_failure` block is not evidence that both rungs use it.
    "RebootOnRequest": dict(reboot_request="asked"),
}

# `caller_spoke` is in the baseline because every scenario here drives an utterance, and
# the hook sets it the moment the caller says anything. The one state it excludes is the
# opening turn of a silent call — which has no rung to assert, and is covered live.
# `scope_ask` is the hook's job live, and the oracle does not run the hook -- but
# ContextGate's `then_say` interpolates it, and an unresolved placeholder is a render
# error rather than a missing word. Empty is the right default here: these scenarios pin
# the ladder, and the ones that care about the question being ASKED are the live drives.
CLEAR = dict(scope_ask="", caller_spoke="true",
             diagnostics_complete="true", reboot_answer_allowed="true", account_status="clear", outage_status="none", convoy_status="clear",
             network_status="healthy", gateway_status="healthy", wifi_status="healthy",
             cable_modem_mac="AA:BB:CC:DD:EE:FF", device_id="AA:BB:CC:DD:EE:FF")


def scenario(**overrides):
  """A seeded state as the before_agent hook would leave it.

  The hook guarantees a message for any rung whose script interpolates one: an
  unresolvable `{placeholder}` makes the engine raise while rendering `then_say`
  (the missing-field value-gate only covers TERMINAL tasks, and these rungs are
  deliberately not terminal). Mirroring that guarantee here keeps the check honest
  about what actually reaches the engine.
  """
  s = dict(CLEAR)
  s.update(overrides)
  # `None` means UNSET, not "present and empty". The outage inquiry needs
  # `diagnostics_complete` genuinely absent — that is the state the hook leaves when it
  # answers the one question it was asked — and a key present with a null value is a
  # different thing the engine may well read as filled.
  for _k, _v in list(overrides.items()):
    if _v is None:
      s.pop(_k, None)
  if s.get("outage_status") in ("active", "degradation"):
    s.setdefault("outage_message", "OUTAGE_MSG")
    s.setdefault("customer_message", "CUST_MSG")
  if s.get("convoy_status") == "predictive_impairment":
    s.setdefault("convoy_customer_message", "CONVOY_MSG")
  return s


# (name, seeded state, expected rung). Mirrors the source's own scenario matrix, plus
# the hierarchy cases that prove ORDER (a restricted account outranks an active outage;
# a hardware fault outranks the reboot offer).
SCENARIOS = [
    ("account suspended", scenario(account_status="suspended"), "HandleBillingBlock"),
    ("account disconnected", scenario(account_status="disconnected"),
     "HandleBillingBlock"),
    ("pending activation", scenario(account_status="pending activation"),
     "HandleBillingBlock"),

    # P1b — the number was the right shape and matched no account. Before this rung
    # existed, `not_found` was in none of the ladder's `in` lists, nothing fired, and the
    # caller reached the model with a session full of skipped statuses -- which read as
    # healthy and produced the all-clear about an account that does not exist (4/4 over
    # voice). The row is here to keep that hole shut: a status with no rung is a status
    # the model answers.
    ("account not found", scenario(account_status="not_found",
                                   network_status="skipped", gateway_status="skipped",
                                   wifi_status="skipped", outage_status="none",
                                   convoy_status="none",
                                   cable_modem_mac="NOT_FOUND"),
     "HandleAccountNotFound"),
    # ...and it must not be mistaken for a restricted account, whose desk is billing.
    ("not found beats nothing else", scenario(account_status="not_found",
                                              network_status="healthy",
                                              gateway_status="healthy",
                                              wifi_status="healthy"),
     "HandleAccountNotFound"),

    ("area outage", scenario(outage_status="active", network_status="skipped",
                             gateway_status="skipped", wifi_status="skipped",
                             outage_message="OUTAGE_MSG",
                             customer_message="CUST_MSG"), "HandleAreaOutage"),

    # R5 — the service-charge question, in the three places it actually gets asked.
    #
    # Asked while the ladder is still open, the fee answer must NOT consume the turn:
    # it latches `cost_answered`, not `verdict_delivered`, so the engine keeps walking
    # and the caller hears the fee and the diagnosis in one breath. The two-tuple is
    # what asserts that second half — without it a fee answer that silently swallowed
    # the verdict would score as a pass.
    # A reboot is on the table, not a technician — so the honest answer is "no charge",
    # not the visit fee schedule. Getting this wrong is what a real caller heard four
    # times on one call.
    ("fee asked mid-diagnosis with no technician: no charge, AND still diagnose",
     scenario(gateway_status="reboot", cost_question="asked"),
     ("AnswerNoCharge", "verdict_offer_reboot")),
    # A technician IS on the table, so the approved schedule applies.
    ("fee asked when a technician is proposed: the schedule",
     scenario(network_status="impaired", technician_type="network tech",
              cost_question="asked"), ("AnswerServiceFee", "verdict_network_tech")),

    # The clarification gate is a PRE-diagnostic question. A stray product name late in
    # a call must not re-open it after the all-clear, and the app-advice rung must not
    # contradict a clean sweep the caller has already been told about.
    ("a product name after the all-clear does not re-open the gate",
     scenario(complaint_scope="app_specific", app_name="Xbox",
              wifi_offered="true"), None),
    ("app advice cannot fire once the walkthrough is offered",
     scenario(complaint_scope="app_specific", app_name="Xbox",
              clarify_reply="ONLY_APP", wifi_offered="true"), None),

    # Acknowledgement leads the turn and does NOT consume it: the caller hears that they
    # were heard, then the substantive answer, in one breath.
    ("frustration is acknowledged, and the answer still lands",
     scenario(network_status="impaired", technician_type="network tech",
              frustration="yes"), ("AckFrustration", "verdict_network_tech")),
    ("acknowledged once, not on every later turn",
     scenario(network_status="impaired", technician_type="network tech",
              frustration="yes", frustration_ack="true"), "HandleNetworkTech"),
    ("\"I already tried that\" is acknowledged too",
     scenario(network_status="impaired", technician_type="network tech",
              already_tried="yes"), ("AckAlreadyTried", "verdict_network_tech")),
    # Both signals at once gets ONE apology, not two.
    ("fed up AND repeating themselves: one acknowledgement, not two",
     scenario(network_status="impaired", technician_type="network tech",
              frustration="yes", already_tried="yes"),
     ("AckFrustration", "verdict_network_tech")),
    # The commonest case by far, and the one a `NOT_YET_ANSWERED` gate would have
    # broken: the caller has just been told a technician is coming and asks what it
    # costs. Nothing left to cascade to — the verdict is already delivered.
    ("fee asked AFTER the verdict: still answered",
     scenario(network_status="impaired", technician_type="network tech",
              verdict_delivered="true", cost_question="asked"),
     "AnswerServiceFee"),
    # Asked twice. `cost_answered` is the latch that stops the fee schedule being read
    # at the caller on every subsequent turn, the way an announce would.
    ("fee already answered: do not repeat it",
     scenario(network_status="impaired", technician_type="network tech",
              verdict_delivered="true", cost_question="asked",
              cost_answered="true"), None),

    # A SWAP IS NOT A VISIT. Both swap verdicts send the caller to a store or a web form
    # in the same words -- "You can swap it at a local store or request a replacement on
    # the Xfinity website" -- so nothing chargeable has been proposed and the schedule
    # does not apply. Driven: the caller was told to collect a replacement from a store
    # and, one breath later, read the full schedule with the visit fee in it. Both legs
    # are rowed, because removing one from the condition and leaving the other ships
    # half a fix, and the convoy leg is the one no drive had reached.
    ("fee asked on a gateway swap: no charge, not the schedule",
     scenario(gateway_status="swap", cost_question="asked"),
     ("AnswerNoCharge", "verdict_hardware_swap")),
    ("fee asked on a convoy swap: no charge, not the schedule",
     scenario(convoy_status="predictive_swap", gateway_status="skipped",
              cost_question="asked"),
     ("AnswerNoCharge", "verdict_convoy_swap")),
    # ...but an impairment UNDERNEATH a swap still means a visit. This is the row that
    # stops the two removed legs being read as "swaps are free": which verdict speaks is
    # the ladder's business, and it is not the same question as whether anyone is coming.
    ("fee asked on a swap over an impairment: still the schedule",
     scenario(gateway_status="swap", network_status="impaired",
              technician_type="network tech", cost_question="asked"),
     ("AnswerServiceFee", "verdict_hardware_swap")),

    # The turn the answer and the fault land on TOGETHER, which is the commoner of the two
    # -- the question is hoisted into the wait, so the answer turn and the turn the
    # specialists report on are frequently one turn. Measured live over voice: the caller
    # said "Honestly, I think it's everything" and heard the gateway verdict first, with
    # their answer acknowledged behind it, in words written for a turn that had already
    # happened. The two-tuple is the whole point -- the acknowledgement leads and the
    # verdict still speaks on the same turn, second.
    ("scope answered on the verdict's own turn: acknowledged first",
     scenario(gateway_status="swap", AskScopeEarly="true",
              wifi_scope_early="ALL_DEVICES"),
     ("AckScopeBeforeVerdict", "verdict_hardware_swap")),
    # ...and where the early acknowledgement already spoke, the verdict stands alone. Same
    # leg, same reason, as its after-verdict sibling three rows down.
    ("already acknowledged during the sweep: the verdict leads its own turn",
     scenario(gateway_status="swap", AskScopeEarly="true",
              wifi_scope_early="ALL_DEVICES", wifi_offered_early="true"),
     "HandleHardwareSwap"),
    # An ALL-CLEAR keeps the walkthrough and has rungs of its own for this turn, so the
    # acknowledgement must not lead an offer the caller has not heard yet.
    ("scope answered as the checks come back clean: the all-clear owns the turn",
     scenario(AskScopeEarly="true", wifi_scope_early="ALL_DEVICES"), "HandleAllClear"),

    # The turn AFTER a fault verdict, when the caller answers the scoping question that
    # was asked during the sweep. Every gate is shut by then -- the ladder by
    # `verdict_delivered`, every tip by `WALKTHROUGH_SAFE`, and the two early
    # acknowledgements by their own `diagnostics_complete` leg -- so before this rung
    # existed the engine returned NOTHING and the model owned the turn. Driven twice on
    # exactly this state: in-home advice about a line just called impaired, and an offer
    # to book a technician visit on an agent with no appointment tool.
    ("scope answered after a fault verdict: acknowledged, not left to the model",
     scenario(network_status="impaired", technician_type="network tech",
              verdict_delivered="true", AskScopeEarly="true",
              wifi_scope_early="ONE_DEVICE"), "AckScopeAfterVerdict"),
    # One rung covers both answers, which is only honest because the line says nothing
    # about how much of the house is affected. The early pair needs two wordings; this
    # one must not need a second.
    ("...and the same for a whole-house answer",
     scenario(network_status="impaired", technician_type="network tech",
              verdict_delivered="true", AskScopeEarly="true",
              wifi_scope_early="ALL_DEVICES"), "AckScopeAfterVerdict"),
    ("acknowledged once, not on every turn after it",
     scenario(network_status="impaired", technician_type="network tech",
              verdict_delivered="true", AskScopeEarly="true",
              wifi_scope_early="ONE_DEVICE", scope_noted_late="true"), None),
    # ...and not at all when the EARLY acknowledgement already spoke. Driven 3/3 in text
    # before this leg existed: the answer landed during the sweep, "Got it, that helps"
    # was said, and two turns later the verdict arrived with "Thanks, that's useful to
    # know and I've made a note of it" appended to it -- an acknowledgement of a turn the
    # caller never took. Where the early rung spoke, the verdict alone is the whole reply.
    ("already acknowledged during the sweep: the verdict stands alone",
     scenario(network_status="impaired", technician_type="network tech",
              verdict_delivered="true", AskScopeEarly="true",
              wifi_scope_early="ONE_DEVICE", wifi_offered_early="true"), None),
    # An ALL-CLEAR keeps the walkthrough, which has rungs of its own for this turn. The
    # acknowledgement must not step in front of them.
    ("scope answered after an all-clear: the walkthrough still owns the turn",
     scenario(verdict_delivered="true", AskScopeEarly="true",
              wifi_scope_early="ONE_DEVICE"), None),
    # ...and the money question still wins the turn it is asked on. This rung is declared
    # after the whole ladder precisely so it can never outrank an answer.
    ("a fee question after the verdict beats the scope acknowledgement",
     scenario(network_status="impaired", technician_type="network tech",
              verdict_delivered="true", AskScopeEarly="true",
              wifi_scope_early="ONE_DEVICE", cost_question="asked"),
     ("AnswerServiceFee", "verdict_scope_noted_late")),

    # Phase 4 — the outage inquiry. These run on a DIFFERENT picture from every scenario
    # above: the hook checked the outage and nothing else, so `diagnostics_complete` is
    # unset. `scenario()` seeds it, so these pass `diagnostics_complete=None` explicitly
    # — which is also the assertion that matters, because if any main-ladder rung could
    # fire without the sweep, this caller would be diagnosed at without consenting.
    ("inquiry, outage found: the advisory, no diagnosis",
     scenario(call_intent="outage_inquiry", outage_status="active",
              network_status="skipped", gateway_status="skipped",
              wifi_status="skipped"), "InquiryOutageFound"),
    ("inquiry, no outage: good news and an offer",
     scenario(call_intent="outage_inquiry"), "InquiryNoOutage"),
    # Answered, and the caller declined the full check. A warm close, not a hand-off —
    # nothing is wrong and transferring them would be worse service than saying goodbye.
    ("inquiry declined: close warmly, do not transfer",
     scenario(call_intent="outage_inquiry", inquiry_answered="true",
              full_check_allowed="true", full_check="DECLINE"), "InquiryDeclined"),
    # The answer gate. Offered and answered inside ONE turn is the model answering for
    # the caller, so a DECLINE that arrives before the offer has been spoken is ignored.
    ("inquiry: a decline before the offer was made is not honoured",
     scenario(call_intent="outage_inquiry", full_check_allowed="false",
              full_check="DECLINE"), "InquiryNoOutage"),
    # Consent taken: the hook sweeps, `diagnostics_complete` lands, and the caller drops
    # into the ordinary ladder rather than a second copy of the inquiry.
    # Consent taken. The hook has cleared `verdict_delivered` by this point — that is
    # the one thing that reopens the ladder — so the caller lands in the ordinary
    # journey rather than a second copy of the inquiry.
    ("inquiry accepted: the verdict lands on that same turn",
     scenario(call_intent="outage_inquiry", inquiry_answered="true",
              full_check_allowed="true", full_check="ACCEPT",
              gateway_status="reboot"), "OfferReboot"),

    # R6 — "just reboot my modem". Honoured on an otherwise-clean picture...
    # The reboot happens AND the diagnosis still lands, in one turn: `reboot_done` is
    # the latch, not `verdict_delivered`, so the ladder stays open behind it. A caller
    # who asks for a restart on a healthy line gets the restart and is told the line is
    # healthy, rather than the request swallowing the verdict.
    ("explicit reboot request is honoured, verdict still lands",
     scenario(reboot_request="asked"), ("RebootOnRequest", "verdict_all_clear")),
    # ...and still honoured after a verdict has landed, which is the case a
    # NOT_YET_ANSWERED gate would have silently dropped.
    ("explicit reboot request after a verdict is still honoured",
     scenario(reboot_request="asked", verdict_delivered="true"), "RebootOnRequest"),
    # ...but NOT during an area outage. A restart cannot clear one, so honouring the
    # request would spend the caller's time on something already known not to work and
    # talk over the verdict that explains the real fault.
    ("reboot request during an outage is refused, outage wins",
     scenario(reboot_request="asked", outage_status="active",
              network_status="skipped", gateway_status="skipped",
              wifi_status="skipped", outage_message="OUTAGE_MSG",
              customer_message="CUST_MSG"), "HandleAreaOutage"),
    # ...nor when the gateway needs replacing, for the same reason.
    ("reboot request with a dead gateway is refused, swap wins",
     scenario(reboot_request="asked", gateway_status="swap"),
     "HandleHardwareSwap"),
    # ...nor on a suspended account. This one is the SOURCE's own blocker, not an
    # addition: its Priority-0 bypass names account_status and outage_status explicitly.
    # Ladder position cannot cover it, because this rung is not gated on the ladder
    # being open — a suspended caller who asked on a later turn would have got a reboot.
    ("reboot request on a suspended account is refused, billing wins",
     scenario(reboot_request="asked", account_status="suspended"),
     "HandleBillingBlock"),
    # `degradation` is the other half of the source's outage test. No tool in the export
    # produces the value — the mock's modes are active/none/error and the real
    # `check_outage` never emits it — but the source's prose branches on it in two
    # places, so it is carried as a defensive alias. Dropping it would leave the value
    # matching no rung at all and fall the caller through to the model.
    ("reboot request during a DEGRADATION is refused too",
     scenario(reboot_request="asked", outage_status="degradation",
              network_status="skipped", gateway_status="skipped",
              wifi_status="skipped"), "HandleAreaOutage"),

    # `wifi_status` is likewise unreachable today: the sweep never returns it and the
    # hook always seeds "skipped". Both conditions that read it are therefore inert —
    # but they are CORRECT, and deleting a correct guard makes the config wrong the day
    # the value arrives. Pinned instead, so they are guards with evidence rather than
    # dead config nobody has tested.
    ("an unhealthy wifi_status is not an all-clear",
     scenario(wifi_status="impaired"), None),
    ("an errored wifi_status reaches the diagnostics-failure rung",
     scenario(wifi_status="error"), "HandleDiagnosticError"),
    # Once done, it does not run again. `reboot_done` is shared precisely so a caller
    # who asks twice does not get two restarts.
    # Asked twice. `reboot_done` is shared precisely so the re-arm cannot empty it and
    # hand the caller a second restart. Expecting the all-clear rather than nothing is
    # the honest assertion: the ladder is open on a healthy picture, so SOMETHING fires
    # — what matters is that it is not another reboot.
    ("reboot already performed: do not do it twice",
     scenario(reboot_request="asked", reboot_done="true"), "HandleAllClear"),
    ("account beats outage", scenario(account_status="suspended",
                                      outage_status="active"),
     "HandleBillingBlock"),
    ("outage beats network", scenario(outage_status="active",
                                      network_status="impaired"),
     "HandleAreaOutage"),
    ("no gateway on account", scenario(cable_modem_mac="NOT_FOUND",
                                       device_id="NOT_FOUND",
                                       gateway_status="offline"),
     "HandleMissingHardware"),
    ("convoy impairment", scenario(convoy_status="predictive_impairment",
                                   network_status="impaired",
                                   gateway_status="skipped",
                                   convoy_customer_message="CONVOY_MSG"),
     "HandleConvoyImpairment"),
    ("gateway swap", scenario(gateway_status="swap"), "HandleHardwareSwap"),
    # Convoy wins when both are set — the source DAG's own precedence.
    ("convoy predictive swap", scenario(convoy_status="predictive_swap",
                                        gateway_status="swap"),
     "HandleConvoySwap"),
    ("convoy swap, gateway silent", scenario(convoy_status="predictive_swap",
                                             gateway_status="skipped"),
     "HandleConvoySwap"),
    ("swap beats network", scenario(gateway_status="swap",
                                    network_status="impaired"),
     "HandleHardwareSwap"),
    ("network tech", scenario(network_status="impaired",
                              technician_type="network_tech"), "HandleNetworkTech"),
    ("network generic", scenario(network_status="impaired"),
     "HandleNetworkImpairment"),
    # The specialist reports the type spaced and lower case. The condition tested the
    # underscored form, so on a real value the split fell through to the generic rung —
    # and while the sweep dropped the value entirely, the hook's constant made every
    # impairment a network technician, which hid it from the other side.
    ("network tech, as the specialist actually spells it",
     scenario(network_status="impaired", technician_type="network tech"),
     "HandleNetworkTech"),
    ("install and repair tech takes the service-charge branch",
     scenario(network_status="impaired",
              technician_type="install and repair tech"),
     "HandleNetworkImpairment"),
    ("reboot offered (asks)", scenario(gateway_status="reboot"), "OfferReboot"),
    # The single-app advice rung, which the oracle never fired at all until it turned
    # out to be suppressing outages. It sits below every plant fault and above the
    # reboot offer, so all three of these have to hold at once.
    ("only that app, nothing else wrong",
     scenario(complaint_scope="app_specific", app_name="Netflix",
              clarify_reply="ONLY_APP"), "AdviseAppSpecific"),
    # The defect: the sweep has ALREADY measured the outage by the time this rung is
    # eligible, so advising about the app means withholding a fault Comcast knows about.
    ("only that app, but the area is out",
     scenario(complaint_scope="app_specific", app_name="Netflix",
              clarify_reply="ONLY_APP", outage_status="active",
              network_status="skipped", gateway_status="skipped",
              wifi_status="skipped"), "HandleAreaOutage"),
    # …and the same for a restricted account, which outranks the outage in turn.
    ("only that app, but the account is suspended",
     scenario(complaint_scope="app_specific", app_name="Netflix",
              clarify_reply="ONLY_APP", account_status="suspended"),
     "HandleBillingBlock"),
    # Advice still outranks proposing a gateway restart: a reboot is disruptive and
    # about the wrong thing when the only complaint is one app.
    ("only that app, gateway would otherwise offer a reboot",
     scenario(complaint_scope="app_specific", app_name="Netflix",
              clarify_reply="ONLY_APP", gateway_status="reboot"),
     "AdviseAppSpecific"),
    # Nothing may be advised before the checks have run, for the same reason nothing
    # may be diagnosed: the advice is only true if the plant came back clean.
    # Before the sweep no VERDICT may speak — but the turn is not silent either. The
    # account is known and the check has not run, so the bridge says so. Without it the
    # turn is undirected and the model invents a progress report.
    ("only that app, before the sweep: the bridge, never a verdict",
     dict(complaint_scope="app_specific", app_name="Netflix",
          clarify_reply="ONLY_APP", accountNumber="8069100230361003",
          caller_spoke="true", scope_ask=""),
     # The sweep is synchronous, so it completes in-turn and the ladder may reach the
     # verdict in the same breath. Before the sweep no verdict may speak; that is still
     # what this pins -- the sweep firing FIRST is the thing that makes the verdict legal.
     (SWEEP_TASK, "verdict_app_specific")),
    # The offer must sit BEHIND the clarification gate like every concluding rung.
    # It did not, and the effect was a flow that offered to reboot the gateway and
    # asked "is it only Netflix?" in the same breath — proposing a disruptive action
    # before establishing the connection was even at fault. Nothing here covered the
    # combination, so the oracle stayed green while the live call contradicted itself.
    ("reboot condition met but complaint still unclarified",
     scenario(gateway_status="reboot", complaint_scope="app_specific"), None),
    ("reboot offered once the caller says everything is down",
     scenario(gateway_status="reboot", complaint_scope="app_specific",
              clarify_reply="EVERYTHING_DOWN"), "OfferReboot"),
    ("reboot confirmed", scenario(gateway_status="reboot", confirm_reboot=True, reboot_offered="true"),
     "ExecuteReboot"),
    ("reboot declined", scenario(gateway_status="reboot", confirm_reboot=False, reboot_offered="true"),
     "DeclineRebootTransfer"),
    # The setter records the SPOKEN answer, not a boolean.
    ("reboot spoken yes", scenario(gateway_status="reboot", confirm_reboot="yes", reboot_offered="true"),
     "ExecuteReboot"),
    ("reboot spoken no", scenario(gateway_status="reboot", confirm_reboot="no", reboot_offered="true"),
     "DeclineRebootTransfer"),
    ("reboot spoken no (convoy)", scenario(convoy_status="predictive_offline",
                                           confirm_reboot="no", reboot_offered="true"),
     "DeclineRebootTransfer"),
    # The model answering its own question on the sweep turn must NOT fire a rung; the
    # caller has to hear the question first.
    # The model answering before the caller was asked must NOT decide the branch —
    # the offer is spoken instead, and the answer is collected on the next turn.
    ("reboot answered too early",
     dict(scenario(gateway_status="reboot", confirm_reboot="no"),
          reboot_answer_allowed="false"), "OfferReboot"),
    ("predictive offline (asks)", scenario(convoy_status="predictive_offline",
                                           gateway_status="reboot"), "OfferReboot"),
    ("unsupported device", scenario(gateway_status="unsupported_device"),
     "HandleUnsupportedDevice"),
    ("no telemetry", scenario(gateway_status="no_telemetry"), "HandleNoTelemetry"),
    ("diagnostics error", scenario(gateway_status="healthy", network_status="error"),
     "HandleDiagnosticError"),
    ("all clear", scenario(), "HandleAllClear"),
    # The offer turn must END on the question. This is the one shape the walkthrough
    # cases below could not see: they all seed `wifi_offered="true"`, so they pin what
    # happens on the turn AFTER the offer and say nothing about the offer turn itself.
    # An accept that arrives in the same breath as the account number — a cue match on
    # "ok", or the model classifying the turn — used to make the whole walkthrough
    # cascade: the caller heard "Would you like me to walk you through a few things to
    # try?" and then "Got it. Is everything having trouble connecting, or just one
    # device?" in one turn, so they could neither accept nor decline.
    ("all clear + an accept in the same breath -> the offer, and NOTHING after it",
     scenario(wifi_walkthrough="ACCEPT"), "HandleAllClear"),
    # The Wi-Fi walkthrough. The all-clear is an OFFER now, so each of these seeds the
    # state a real call would be in by that point and asserts the one turn that follows.
    ("all clear, already offered — does not re-offer",
     scenario(reason_for_call="internet is slow", wifi_offered="true"), None),
    # `AskScopeEarly` UNFILLED, and that is now what picks the wording: the question was
    # never hoisted into the sweep, so this caller is hearing it for the first time.
    ("walkthrough accepted, never asked before -> the scoping question, and nothing else",
     scenario(reason_for_call="internet is slow", wifi_offered="true", wifi_answer_allowed="true",
              wifi_walkthrough="ACCEPT"), "AskWifiScope"),
    # THE PAYOFF of asking scope during the sweep, and the part of it this oracle can
    # score. Which task fires DURING the wait is not checkable here -- the harness cannot
    # model a task sitting in `_awaiting_async`, so `ContextGate` is eligible on every
    # seeded mid-sweep state and wins on declaration order. What is checkable is the state
    # the wait LEAVES BEHIND, seeded here exactly as the hook's promotion writes it:
    # `wifi_scope` carrying the answer and `wifi_scope_asked` set alongside it.
    #
    # The two must move together, and the reason is why this scenario exists. Promoting
    # the answer alone leaves the value in a slot whose own gate (WIFI_SCOPE_ASKABLE) is
    # shut, where it does not read as filled -- so `AskWifiScope` stayed eligible and the
    # caller was asked a question they had answered thirty seconds earlier. That was this
    # scenario failing, not a harness artefact; `check_early_scope_ask` now pins the hook
    # side of it too.
    ("scope answered during the sweep -> straight to the tip, no second scope question",
     scenario(reason_for_call="internet is slow", wifi_offered="true",
              wifi_answer_allowed="true", wifi_scope_allowed="true",
              wifi_walkthrough="ACCEPT", wifi_scope_asked_early="true",
              wifi_scope_asked="true", wifi_scope="ONE_DEVICE"),
     "WifiTipRejoin"),
    # ...and the other half of that contract. The early question was ASKED but never
    # answered -- the caller said nothing, or said something the cues did not match -- so
    # the walkthrough must still ask it. This is what the early rung's separate latch
    # buys: sharing `wifi_scope_asked` would suppress the question here and strand the
    # walkthrough with no scope and every tip gated shut.
    #
    # `AskScopeEarly` is the announce's own latch, and the seed that used to stand for
    # "asked early" here (`wifi_scope_asked_early`) is a leftover of the RUNG the announce
    # replaced -- nothing reads it any more, so the scenario was silently pinning the
    # never-asked path under the asked-early name. Both are seeded now: the live one picks
    # the wording, the dead one proves it is not what does.
    ("scope asked during the sweep but never answered -> asked again, in words that "
     "say it is the second time",
     scenario(reason_for_call="internet is slow", wifi_offered="true",
              wifi_answer_allowed="true", wifi_walkthrough="ACCEPT",
              AskScopeEarly="true",
              wifi_scope_asked_early="true"), "AskWifiScopeAgain"),
    ("scoped to one device -> first tip",
     scenario(reason_for_call="internet is slow", wifi_offered="true",
              wifi_answer_allowed="true", wifi_scope_allowed="true",
              wifi_walkthrough="ACCEPT",
              wifi_scope_asked="true", wifi_scope="ONE_DEVICE"),
     "WifiTipRejoin"),
    # The adaptivity that made three hand-written rungs worth it over a fixed list: a
    # caller who says they already rejoined is not told to rejoin.
    ("caller already rejoined -> skip to the next tip they have not tried",
     scenario(reason_for_call="internet is slow", wifi_offered="true",
              wifi_answer_allowed="true", wifi_scope_allowed="true",
              wifi_walkthrough="ACCEPT",
              wifi_scope_asked="true", wifi_scope="ONE_DEVICE",
              wifi_tried="rejoin"), "WifiTipCloser"),
    # The whole-house caller gets whole-house advice. Every device-specific tip is
    # scoped out — telling someone whose laptop, TV and console are all struggling to
    # forget the network "on the device that's struggling" asks them to do it on every
    # device they own, which is what a recorded caller objected to.
    ("whole house offline -> whole-house tips, not device-specific ones",
     scenario(reason_for_call="internet is slow", wifi_offered="true",
              wifi_answer_allowed="true", wifi_scope_allowed="true",
              wifi_walkthrough="ACCEPT",
              wifi_scope_asked="true", wifi_scope="ALL_DEVICES"),
     "WifiTipPlacement"),
    # ...and the device-specific ones stay for the caller who named one device.
    ("one device -> the device-specific tip, not the gateway one",
     scenario(reason_for_call="internet is slow", wifi_offered="true",
              wifi_answer_allowed="true", wifi_scope_allowed="true",
              wifi_walkthrough="ACCEPT",
              wifi_scope_asked="true", wifi_scope="ONE_DEVICE"), "WifiTipRejoin"),
    # A money question and a troubleshooting step must not share a turn.
    ("a fee question does not also carry a tip",
     scenario(reason_for_call="internet is slow", wifi_offered="true",
              wifi_answer_allowed="true", wifi_scope_allowed="true",
              wifi_walkthrough="ACCEPT", wifi_scope_asked="true",
              wifi_scope="ONE_DEVICE", cost_question="asked"),
     ("AnswerNoCharge", None)),
    ("three tips spent -> hand off with what was tried",
     scenario(reason_for_call="internet is slow", wifi_offered="true",
              wifi_answer_allowed="true", wifi_scope_allowed="true",
              wifi_walkthrough="ACCEPT",
              wifi_scope_asked="true", wifi_scope="ONE_DEVICE",
              wifi_tip_rejoin="true", wifi_tip_closer="true",
              wifi_tip_toggle="true", wifi_tips_exhausted="true"),
     "WifiExhausted"),
    ("caller says it is working -> warm close, no transfer",
     scenario(reason_for_call="internet is slow", wifi_offered="true",
              wifi_answer_allowed="true", wifi_scope_allowed="true",
              wifi_walkthrough="ACCEPT",
              wifi_scope_asked="true", wifi_tip_rejoin="true",
              wifi_fixed="yes"), "WifiFixed"),
    # THE SAME SENTENCE ON THE OTHER ROUTE, and the row that would have caught the
    # inequivalence. There are two doors into the walkthrough -- the all-clear's offer
    # latches `wifi_offered`, the offer made while the checks are still running latches
    # `wifi_offered_early` -- and `WifiFixed` named only the first. Identical caller words
    # got the authored warm close on one route and permanent silence on the other, 3/3
    # live against a passing control. Every rung downstream now reads
    # `WALKTHROUGH_OFFERED`, and this row is what keeps it that way: swap the condition
    # back to a single latch and this goes red while the row above stays green.
    ("caller says it is working on the EARLY path -> the same warm close",
     scenario(reason_for_call="internet is slow", wifi_offered_early="true",
              wifi_offered=None,
              # The early route's own all-clear has already spoken by this point -- it is
              # what `ALL_CLEAR_ALREADY_TRYING` exists for -- so its latch is set. Without
              # it this row measures rung ORDER rather than the latch it is about.
              all_clear_told="true",
              wifi_answer_allowed="true", wifi_scope_allowed="true",
              wifi_walkthrough="ACCEPT",
              wifi_scope_asked="true", wifi_tip_rejoin="true",
              wifi_fixed="yes"), "WifiFixed"),
    # Declining is not a dead end: the requirement hands these callers to a person.
    ("walkthrough declined -> a person, not a closed call",
     scenario(reason_for_call="internet is slow", wifi_offered="true", wifi_answer_allowed="true",
              wifi_walkthrough="DECLINE"), "WifiDeclined"),
    ("verdict already delivered", scenario(outage_status="active",
                                           verdict_delivered="true"), None),
    # Before the sweep runs there are no statuses; nothing may be judged yet or the
    # caller gets a verdict before being asked for an account number.
    ("sweep not yet run: the sweep itself takes the turn, not silence and not a verdict",
     {"accountNumber": "8344200010126021", "caller_spoke": "true", "scope_ask": ""},
     SWEEP_TASK),
    # And it does not run twice. The old rung expressed this with its own `sweep_bridged`
    # latch; the task expresses it with `diagnostics_complete`, plus -- live, and NOT
    # modelled here -- the engine's `_awaiting_async` mark, which keeps a task that is
    # still in flight out of the selector. Offline this oracle cannot see an ASYNCHRONOUS
    # tool return `{"result": "pending"}`, so it walks the cascade past the dispatch as
    # though the sweep had already answered. That is why the no-verdict-before-the-sweep
    # scenarios above are pinned on the TASK rather than on what follows it.
    ("the sweep runs once",
     {"accountNumber": "8344200010126021", "diagnostics_complete": "true",
      "verdict_delivered": "true", "caller_spoke": "true"}, None),
    # The OPENING turn of a silent call: an account is pre-seeded upstream, but the
    # caller has not spoken, so the hook has not swept and there is nothing to bridge to.
    # `repair` must say NOTHING here and leave the turn to the router's welcome.
    #
    # This scenario exists because removing the `caller_spoke` gate left the suite at
    # 72/72 — the fix was real (driven live, a silent caller was told "give me just a
    # moment while I check your connection") but completely unprotected. Mutation-checked
    # both ways: drop the gate and this fails, restore it and it passes.
    ("silent opening: no bridge, because there is no sweep to bridge to",
     {"accountNumber": "8344200010126021"}, None),
]


def load_config(app_dir: str) -> dict:
  # The ladder now lives in the `repair` child flow of the steering router.
  path = os.path.join(app_dir, "tools", "repair_dag", "python_function",
                      "python_code.py")
  namespace: dict = {}
  with open(path) as fh:
    exec(compile(fh.read(), path, "exec"), namespace)  # noqa: S102 - our own emitted file
  return namespace["repair_dag"]()


def run(app_dir: str) -> int:
  loader.set_framework_root(FRAMEWORK_ROOT)
  config = load_config(app_dir)
  print(f"config: {len(config.get('slots', []))} slots, "
        f"{len(config.get('tasks', []))} tasks\n")

  shape_failures = (check_split_shapes(config) + check_filler_echo(config)
                    + check_filler_pool_collisions() + check_scope_cues(config)
                    + check_two_turn_gates(config) + check_reassurance_ladders(config)
                    + check_early_scope_ask(config) + check_escalation_hold(config)
                    + check_latches_are_real(config, app_dir)
                    + check_text_projection(config)
                    + check_fee_rungs_fire_on_the_asking_turn(config))
  refusal_failures = check_failure_ladders(config)
  failures = 0
  for name, seeded, expected in SCENARIOS:
    fired, spoken, said_after, cascaded, action = _replay(config, seeded)

    # A split rung must rejoin. `spoken` is the half said as the tool is dispatched,
    # `said_after` the half said once it returns; together, in that order, they have to
    # be the approved sentence and nothing else.
    rejoined = None
    if fired in SPLIT_SCRIPTS:
      approved = SPLIT_SCRIPTS[fired]
      rejoined = " ".join(p for p in (spoken.strip(), said_after.strip()) if p)
      if rejoined != approved:
        print(f"FAIL {name:28} SPLIT does not rejoin to the approved script")
        print(f"       say_first: {spoken!r}")
        print(f"       then_say : {said_after!r}")
        print(f"       rejoined : {rejoined!r}")
        print(f"       approved : {approved!r}")
      if not spoken.strip():
        print(f"FAIL {name:28} SPLIT is silent on the turn the tool fires")

    # `expected` is normally a rung name and a cascade is a failure — two verdicts in one
    # breath. R5 is the deliberate exception: the fee answer is not a verdict, latches
    # its own flag, and is SUPPOSED to be followed by the diagnosis in the same turn. A
    # two-tuple says so, naming the rung and then the TOOL the engine goes on to call, so
    # "they co-fire" is asserted rather than merely tolerated.
    want_cascade = None
    if isinstance(expected, tuple):
      expected, want_cascade = expected
    ok = (fired == expected) and cascaded == want_cascade and (
        rejoined is None or (rejoined == SPLIT_SCRIPTS[fired] and spoken.strip()))
    if cascaded != want_cascade:
      print(f"FAIL {name:28} CASCADE: {fired} then {cascaded} (wanted {want_cascade})")
    failures += (not ok)
    status = "ok  " if ok else "FAIL"
    print(f"{status} {name:28} fired={fired!s:24} expected={expected!s}")
    if not ok:
      print(f"       action={ {k: v for k, v in action.items() if k != 'sm'} }")
    elif spoken:
      print(f"       says: {spoken[:110]!r}")

  print(f"\n{len(SCENARIOS) - failures}/{len(SCENARIOS)} scenarios correct, "
        f"{len(SPLIT_SCRIPTS)} split rung(s) pinned"
        + f", {len(FAILURE_SEEDS)} refusal path(s) replayed"
        + (f", {shape_failures} shape failure(s)" if shape_failures else "")
        + (f", {refusal_failures} refusal failure(s)" if refusal_failures else ""))
  return 1 if (failures or shape_failures or refusal_failures) else 0


if __name__ == "__main__":
  ap = argparse.ArgumentParser()
  ap.add_argument("--app-dir", default="./built")
  raise SystemExit(run(os.path.abspath(ap.parse_args().app_dir)))
