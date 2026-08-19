"""Comcast Xfinity internet-repair agent, authored in the flows SDK.

A hand conversion of CES app `d4ec582c` (`xa-repair-voice-deterministic`,
root agent `repair_orchestration_agent`).

The source is a hybrid: a 33KB generative instruction carrying a P1..P14 priority
ladder, and a hand-written `repair_dag` covering a subset of the same outcomes. Both
are live at once, and where their wording differs the DAG wins — its terminal preempts
the model before the prose is ever consulted. The prose shows through only where no DAG
rung matches. So the fidelity target is the OBSERVED union: the DAG's ordering and
wording, extended with the prose rungs the DAG never modelled (missing hardware,
unsupported device, diagnostics error, the technician-type split).

Here, the diagnostic sweep runs in an author `before_agent` hook (see hooks.py) calling
the source's own `run_comcast_diagnostics` directly, so the verdict lands on the turn
carrying the caller's opening utterance with no dependency on engine-fired tool
dispatch. The ladder is a list of condition-gated TERMINAL TASKS: the engine walks tasks
in declaration order and fires the first whose condition holds and whose inputs are
filled, which is the "evaluate in strict hierarchical order, halt at the first match"
contract the source prose describes. Announces are the wrong vehicle — they cascade, so
several would fire and concatenate. Say-only rungs need a tool too, not for the effect
but for the `verdict_delivered` latch, so they carry their own `verdict_*` executor.
"""

import labs_paths

labs_paths.add_sdk_paths()
labs_paths.require_features()

import flows  # noqa: E402 - after the SDK path is resolved and version-gated
from flows.authoring.dsl import DEFAULT_HOLD_PHRASES, AgentHooks  # noqa: E402

import build_config  # noqa: E402
import clarify  # noqa: E402
import guardrails  # noqa: E402
import hooks  # noqa: E402
import scripts  # noqa: E402
import source_tools  # noqa: E402
import steering_tree  # noqa: E402

# The machinery no single journey owns: `gates` is what every rung tests, `rungs` is
# what every verdict is built from, `status` is the shared vocabulary, `waiting` covers
# the silences.
from journeys.common.gates import (_DIAGNOSED_OR_DONE_WAITING, _OUTAGE_NOW,
                                   INQUIRY_SETTLED, NOT_YET_ANSWERED, SWEPT)
from journeys.common.rungs import (_ends, advice_rung, offer_rung, reboot_rung,
                                   rung, say_rung)
from journeys.common.status import SHARED_STATUS, shared_status_slots
from journeys.common.waiting import _account_no_input, with_filler
from journeys import problem_clarification
from journeys import gateway_restart
from journeys import account_collection
from journeys import human_handoff
from journeys import service_fees
from journeys import acknowledgements
from journeys import inconclusive_checks
from journeys import gateway_replacement
from journeys import missing_equipment
from journeys import restricted_account
from journeys import diagnostics_sweep
from journeys import wifi_walkthrough
from journeys import device_help
from journeys import area_outage
from journeys import technician_visit
from journeys import all_clear

_ROOT_AGENT = "repair_orchestration_agent"

# --------------------------------------------------------------------------- #
# The flow.
# --------------------------------------------------------------------------- #

repair = flows.Flow(
    "repair",
    root_agent="repair_orchestration_agent",
    bootstrap={"reset_on_complete": True},
    # The engine itself detects "I just want to talk to a real person" (a deterministic
    # phrase match, no model involved) and fills the built-in `escalate` control slot.
    # All this block does is say what should HAPPEN then. Left unset, the engine finds no
    # disposition and falls through to the ladder, which speaks a diagnostic verdict over
    # the top of the hand-off.
    #
    # A control block customizes the DISPOSITION only — say / outcome / transfer_to /
    # exit_status / requires_readback. It takes no `tool` key: the setter is uniform
    # across flows (`transfer_to_human`) and the engine ends the session with
    # escalated=True. No payload is needed — asked for a person the SOURCE says nothing
    # at all and calls the platform's `transfer_to_agent`, so an escalated end is the
    # faithful equivalent.
    #
    # `tasks` is the rail's pre-terminal chain and runs BEFORE the disposition, which is
    # what lets the hand-off carry a payload. Without it the receiving human gets no
    # task, no skill, no findings and no account number.
    #
    # `condition` is what makes a refusal real; without it the request is always honoured
    # and only the wording can change. There are TWO reasons to refuse, and they want
    # opposite words:
    #
    #   the outage   Final. The source refuses a live agent during an area outage, since
    #                no amount of troubleshooting brings the service back. This leg sits
    #                OUTSIDE the `any` in the gate, so insisting never reopens it.
    #   the sweep    A HOLD, not a refusal. `EscalateHandoffSummary` is what gives the
    #                hand-off its content, and before the sweep lands there is none, so
    #                a hand-off made now arrives empty and the caller starts over.
    #
    # A hold needs an end, and `_DIAGNOSED_OR_DONE_WAITING` is the three ways out: the
    # sweep lands, some other path answered the caller, or `escalate_declined` reaches
    # the fourth ask and it goes through whatever the sweep is doing.
    #
    # `declined_say` takes a list of REASONS for exactly this: each entry is
    # `{"when": <condition>, "say": ...}`, evaluated in order, and the entry with no
    # `when` is the catch-all.
    #
    # `say` is IMPORTED, not repeated. A copy here would let an edit to the approved
    # hand-off sentence change what the `human` flow and the router say while this rail
    # went on speaking the stale one. One caller-facing sentence, one home.
    escalate=flows.escalate(
        say=scripts.SAY_HUMAN_ESCALATE,
        condition={"all": [{"not": _OUTAGE_NOW}, _DIAGNOSED_OR_DONE_WAITING]},
        declined_say=[
            {"when": _OUTAGE_NOW, "say": scripts.SAY_OUTAGE_NO_AGENT},
            # The catch-all: the checks are not back yet. Last, because an entry after a
            # catch-all can never be reached.
            {"say": [scripts.SAY_HOLD_FOR_CHECKS,
                     scripts.SAY_HOLD_FOR_CHECKS_AGAIN]},
        ],
        tasks=["EscalateHandoffSummary"],
    ),
    # THE OTHER EXIT. `cancel` is the second of the engine's two synthesized control
    # slots — the only two: containment is not a constructor but a PATTERN, an `escalate`
    # with a `condition` and a `declined_say`, exactly as above. The slot is synthesized
    # whether or not a disposition is declared, so leaving it unset does not mean "cancel
    # is off"; it means the engine terminates the call on its own neutral default.
    #
    # `requires_readback` is the point of declaring it: the first fill holds and asks, an
    # affirmative next turn terminates, and ANYTHING else aborts and resumes the flow
    # exactly where it was — so a caller who said "stop" meaning "stop talking" keeps
    # their call and their diagnostics.
    cancel=flows.cancel(
        say=scripts.SAY_CANCELLED,
        requires_readback=True,
        confirm_say=scripts.SAY_CONFIRM_CANCEL,
    ),
    # Silence while the account number is being asked for. Shared verbatim with `reboot`,
    # which asks for the same thing — see `_account_no_input`.
    no_input=_account_no_input(),
    # The caller who keeps talking after the verdict. Unset, the engine falls back to an
    # EMPTY on_exhaust, and empty is the whole problem: it only ends the session when
    # on_exhaust resolves a `then`, so with nothing to resolve it speaks its built-in
    # line and leaves the session open to say it again next turn. A `say` alone does not
    # fix that — it changes the words and keeps the loop.
    #
    # The explicit `response` is what ends the call, and it compensates for a framework
    # gap: the escalate tier sets `sm["status"] = "escalated"` but never calls
    # `_mark_end_session`, unlike the otherwise-identical task-exhaust path, and CES ends
    # a call on seeing a `Part.from_end_session` whatever `sm` says.
    #
    # It does NOT promise a person, deliberately. `on_exhaust` takes no `condition`,
    # unlike the escalate block above, so a transfer here would fire on every path —
    # including an active outage, where this agent refuses a live agent on purpose.
    # A caller who wants a person can still ask, and that request goes through the
    # escalate rail, which honours the condition.
    #
    # Worth knowing: `_steer_back_turns` resets on a substantive tool call and answering
    # a question fires no tool, so unanswerable questions land here even from a perfectly
    # happy caller.
    steer_back={
        # Steer from the FIRST undirected turn. An undirected turn is where this model
        # invents — offering a restart signal that was never sent, or an appointment slot
        # this agent cannot book — and a directed turn is faster as well as safer, so the
        # budget for undirected ones is three and the first already carries a directive.
        "soft_after": 1,
        "hard_after": 2,
        "escalate_after": 3,
        "on_exhaust": {
            "say": ("I'm not able to take this any further on this call. You can reach "
                    "us any time through the Xfinity app or website."),
            "response": [{"type": "end_session", "reason": "cancelled",
                          "escalated": False}],
        },
    },
)



def _session_context_slots():
  """What the platform tells us about where the caller is calling from."""
  return [
      # The caller's client runtime: "xFiMobileXAIOS" and "xFiMobileXAAndroid" (the
      # mobile app), "AIQSDK" (the web portal), and "ivr", "voice", "phone", "audio",
      # "voip" (telephony).
      flows.event_slot("platform"),
      # The entry channel: "xFiMobile" (native app), "AIQSDK" (web assistant), "xMobile"
      # (cellular service), "xStream" (Flex), "XC2" (care portal).
      flows.event_slot("channel"),
      # The rendered [[TS]] troubleshooting summary card markup, for digital/web channels.
      # Filled by the PLATFORM only. Never compose one here: the model reads session state
      # and paraphrases anything that reads like a finding into a finding, so a card built
      # agent-side becomes a spoken outage report with nothing checked.
      flows.event_slot("ts_card"),
  ]


# Every slot the repair flow declares, in ask order.
#
# Slots FIRST, tasks second: the two passes must not be interleaved into a
# journey-at-a-time loop, however much more natural that would read.
# `diagnostics_sweep.sweep_task()` returns a ParallelGroup, and `Flow.task()`
# special-cases it by appending the group's SLOTS as well as its tasks -- so a handful of
# slots legitimately arrive after every `add()` here, and one pass would move them.
#
# Several journeys appear more than once, because `gateway_restart` has to hear the
# request before it can offer and `service_fees` has to hear the question before it can
# remember answering it. The names say which is which.
for _slot in [
    *account_collection.slots(),        # what is your account number
    *shared_status_slots(),             # the status vocabulary every rung reads
    *human_handoff.slots(),             # where the hand-off summary lands
    *diagnostics_sweep.slots(),         # where the checks land their answers
    *wifi_walkthrough.scope_announce(), # ...and the question asked while they run
    *wifi_walkthrough.slots(),          # walking the house, tip by tip
    *service_fees.question_slot(),      # will this cost me
    *acknowledgements.slots(),          # fed up, or already tried it
    *gateway_restart.request_slot(),    # "just restart the thing"
    *area_outage.inquiry_slots(),       # rang to ASK about an outage
    *service_fees.answer_slots(),       # ...and we have answered that once now
    *diagnostics_sweep.state_slots(),   # sweep bookkeeping
    *_session_context_slots(),          # platform, channel, card markup
    *gateway_restart.gate_slots(),      # may the restart question be answered yet
    *problem_clarification.slots(),     # what is actually broken
    *device_help.slots(),               # which box, doing what
    *gateway_restart.confirm_slot(),    # the yes or no itself
    *problem_clarification.bridges(),   # acknowledge, then carry on
]:
    repair.add(_slot)

# The opening line's lead-in, interpolated into the (verbatim) account ask. `repair` is the
# flat build's root and owns the opening turn -- the account-number ask -- and the model
# used to improvise a "Welcome to Xfinity" ahead of it. That greeting is redundant when a
# steering agent has already welcomed the caller and is handing the call over, so the ask
# is made `verbatim` (the model cannot add a greeting) and the greeting is moved into this
# lead-in, which `before_agent` sets: "Welcome to Xfinity. To get started, " on a direct
# call's opening turn, and the bare "To get started, " once an upstream hand-off seeds
# `skip_greeting` (or on any later turn, e.g. reboot's own account ask mid-call). An event
# slot, filled by the hook rather than the caller. NOT a `welcome` announce: an announce
# does not precede the first user_slot ask on a flat flow (only `_router_welcome`, which is
# router-only, gets that turn), so the greeting has to ride the ask itself. The lead-in is
# never empty -- a falsy slot reads as unfilled and the engine re-asks it forever.
repair.add(flows.event_slot("welcome_lead"))

# A second declaration of the same names the shared status slots above already carry.
# The validator checks the two agree: a status slot added to one and not the other is a
# scoping bug that would otherwise surface as a value mysteriously not crossing flows.
repair.set("shared_slots", list(SHARED_STATUS))



# --------------------------------------------------------------------------- #
# The ladder, as one fragment per journey.
#
# Two fragments are named for the same journey on purpose: `technician_visit` is SPLIT by
# `gateway_replacement`, because a hardware fault a truck cannot fix outranks a dispatch.
#
# TASK NAMES ARE NOT RENAMED, here or anywhere. They appear in `tests/ladder_check.py`
# scenarios, in every `built/tools/<name>/` directory and in every recorded golden, so
# the plain-English naming is for MODULES only.
# --------------------------------------------------------------------------- #


# EVERY task the repair flow can fire, in priority order. First match wins, so this
# sequence IS the behaviour — read it top to bottom and you have read the agent's mind.
#
# Items are passed ONE AT A TIME rather than splatted into a single `repair.task(*...)`.
# That is required, not stylistic: `diagnostics_sweep.tasks()` yields a ParallelGroup,
# and the splat path asserts every argument is a dict.
for _task in [
    # Before the sweep. These run on a different picture entirely: the hook has checked
    # the outage and nothing else, so `diagnostics_complete` is unset and every rung
    # below is shut. They are the only tasks eligible until the caller consents to the
    # full check, at which point the ladder takes over normally.
    *area_outage.inquiry_tasks(),    # rang to ASK about an outage
    *device_help.tasks(),            # the problem is a named box, not the line

    # A MID-CALL "get me a supervisor" IS NOT ANSWERED HERE YET, and it should be. The
    # engine's own escalate detector needs a verb beside a noun it knows, so a request
    # naming a rank reaches nothing and the caller is met with silence. A cue-only slot
    # plus a rung declared at this point in the ladder, gated exactly as the `escalate`
    # rail is, is the fix. It cannot be written yet: the rung needs its OWN executor, and
    # binding it to `verdict_human_request` — the only registered tool that means "the
    # caller asked for a person" — puts two tasks of this flow on one tool, which the
    # validator rejects because the runtime routes results by tool name and the loser
    # re-fires forever. That would break the hand-off payload the escalate hold depends
    # on. Register `verdict_human_backstop` in `source_tools.RUNG_TOOLS` first.
    #
    # The OPENING-turn half of the same gap is closed, in `ROUTE_CUES["human"]` below.

    *diagnostics_sweep.tasks(),      # run the checks, cover the wait
    *acknowledgements.tasks(),       # "I hear you" — leads the turn, never consumes it
    # The one walkthrough rung above the ladder, and it belongs to the group it sits in
    # rather than to the group its copy comes from: the caller has just answered a tip and
    # the sweep reports on the same turn, so this leads the turn and the verdict below
    # still speaks on it. Under the ladder it could only be heard AFTER the verdict, which
    # is the ordering being fixed.
    *wifi_walkthrough.tip_ack(),     # ...and so does an answer to a tip we asked about
    # The other half of the same collision, one question earlier: the caller answers the
    # scoping question hoisted into the wait, and the checks report a fault on that same
    # turn. Below the ladder the answer could only be acknowledged after the verdict had
    # talked over it, which is what a caller heard live.
    *wifi_walkthrough.scope_ack(),   # ...and so does an answer to the scoping question
    *service_fees.tasks(),           # will this cost me
    *restricted_account.tasks(),     # the account is on hold: beats even an outage
    *area_outage.verdict(),          # an outage in the area: their line is not at fault
    *missing_equipment.tasks(),      # no gateway on file: nothing to measure
    *technician_visit.predicted(),   # a visit, predicted --.
    *gateway_replacement.tasks(),    # the box needs swapping | no visit fixes a dead box
    *technician_visit.measured(),    # a visit, measured    --'
    *problem_clarification.advice(), # one app only, line clean: advise and stop
    *gateway_restart.on_request(),   # they asked for a restart, unprompted
    *gateway_restart.handshake(),    # we offered one, and they answered
    *inconclusive_checks.tasks(),    # the checks told us nothing: hand off
    *all_clear.tasks(),              # last verdict: only when nothing above matched

    # After the ladder, so neither can ever outrank a diagnostic verdict.
    *wifi_walkthrough.tasks(),       # the line is clean, so walk the house
    *human_handoff.tasks(),          # inert unless the escalate rail has fired
]:
    repair.task(_task)


def _account_number_slot():
    """The account-number user slot: asked before any sweep, seeded upstream when present."""
    # BORROWED from the repair flow rather than restated, so the retry ladder and the
    # give-up line have one home and `repair` and `reboot` cannot say different things
    # about the same number. `account_collection.slots()` builds a FRESH dict on every
    # call, which is the other half of the requirement: two flows must never hold one
    # slot object.
    #
    # Selected by name, not by index, so a slot added alongside it does not silently
    # become the account slot.
    return next(s for s in account_collection.slots()
                if s.get("name") == "accountNumber")


# --------------------------------------------------------------------------- #
# The `reboot` child flow — the caller EXPLICITLY asked to restart their gateway
# (intent internet.equipment.manage). Reached by the router, so it bypasses the whole
# diagnostic ladder by construction. `device_id` is seeded by the before_agent sweep on
# the routing turn.
# --------------------------------------------------------------------------- #

# Both exits, for the same reason `repair` has them: the control slots are synthesized
# per flow and inherit nothing, so without these a caller who asks for a person or gives
# up inside the reboot journey is answered by the framework default and disconnected.
reboot = flows.Flow("reboot", root_agent=_ROOT_AGENT,
                                        bootstrap={"reset_on_complete": True},
                                        escalate=flows.escalate(
                                            say=scripts.SAY_HUMAN_ESCALATE),
                                        cancel=flows.cancel(
                                            say=scripts.SAY_CANCELLED,
                                            requires_readback=True,
                                            confirm_say=scripts.SAY_CONFIRM_CANCEL))
# `no_input` is declared PER FLOW and inherits nothing, so `repair`'s policy does not
# reach here, and without one a caller who goes quiet gets the account-number ask
# repeated on EVERY inactivity tick, unbounded. It is the same slot, the same silence and
# the same 16-digit number as in `repair`, so it gets the same answer.
#
# `on_exhaust` matches `repair`'s rather than closing on something reboot-shaped, because
# exhausting this ladder means the identical thing in both flows: we never learned who is
# calling. The one reboot-shaped close available, `verdict_missing_hardware`, would be
# actively wrong — it reports "no gateway on the account" about an account we never had.
reboot.set("no_input", _account_no_input())
# Collect the account first (pre-seeded upstream in the usual case, so never asked).
# Without it a caller who opens with "reboot my gateway" and no account reaches the rungs
# below with an empty sweep, trips RebootNoGateway, and is told "no gateway on the
# account" — a false negative that hands them off instead of asking who they are.
reboot.add(_account_number_slot())
# `reboot` borrows the account ask, which interpolates `{welcome_lead}`, so it must declare
# the slot too or the render hits an unresolved placeholder. `reboot` is only ever entered
# mid-call, so the hook fills it with the bare "To get started," -- reboot never greets.
reboot.add(flows.event_slot("welcome_lead"))
# The statuses the refusals below read, seeded by the before_agent sweep on the routing
# turn, so they are event slots here exactly as in `repair`. They are also the sweep
# task's declared outputs and its gate, and a task cannot write an output the flow has
# not declared.
for _s in ("cable_modem_mac", "device_id", "diagnostics_complete", "verdict_delivered",
           "account_status", "outage_status", "gateway_status", "convoy_status",
           "outage_message", "customer_message", "network_status", "wifi_status",
           "technician_type", "convoy_customer_message", "caller_spoke", "ts_card",
           "activityType", "activityCode", "jobType"):
    reboot.add(flows.event_slot(_s))

# The SAME sweep the repair ladder runs, in the flow a cold "reboot my modem" lands in.
# Copied slot dicts, not shared ones: `flows.result_slot` returns a mutable dict the
# builder may adopt, and two flows must not hold one object — which is why
# `diagnostics_sweep.sweep_task` is a function.
reboot.add(*[dict(x) for x in diagnostics_sweep.SWEEP_SLOTS])
reboot.task(diagnostics_sweep.context_gate_task())
reboot.task(diagnostics_sweep.specialists_task())
reboot.task(diagnostics_sweep.settle_task())
reboot.task(diagnostics_sweep.sweep_task())

# A reboot the caller asked for is still refusable, and this flow must refuse for the
# same reasons the ladder does. Reached cold from the router, it would otherwise bypass
# every blocker `repair` applies — and restarting a gateway fixes neither an outage nor a
# suspended account, which is not ours to restart in any case.
#
# Declared BEFORE DoReboot: the engine fires the first rung whose condition holds, so
# order is the priority. Each refusal reuses the same terminal and the same approved
# words the ladder uses for that state, so a caller gets one answer for their situation
# whichever door they came in by.
_REBOOT_BLOCKED = [
    ("RebootBlockedOutage", "verdict_area_outage", scripts.AREA_OUTAGE,
     scripts.SAY_AREA_OUTAGE),
    ("RebootBlockedAccount", "verdict_account_block", scripts.RESTRICTED_ACCOUNT,
     scripts.SAY_ACCOUNT_BLOCK),
    ("RebootBlockedSwapGateway", "verdict_hardware_swap", scripts.HARDWARE_SWAP_GATEWAY,
     scripts.SAY_HARDWARE_SWAP_GATEWAY),
    ("RebootBlockedSwapConvoy", "verdict_convoy_swap", scripts.HARDWARE_SWAP_CONVOY,
     scripts.SAY_HARDWARE_SWAP_CONVOY),
]
for _n, _tool, _cond, _say in _REBOOT_BLOCKED:
    reboot.task(flows.task(_n, _tool, [], "verdict_delivered",
                           out_key="verdict_delivered",
                           condition={"all": [SWEPT, NOT_YET_ANSWERED, _cond]},
                           then_say=_say))

# What DoReboot must NOT fire through — the same states the refusals above catch. Without
# this the refusals would only win by declaration order, and any later reordering would
# silently re-open the hole.
_REBOOT_ALLOWED = {"all": [
    {"slot": "account_status", "not_in": ["suspended", "disconnected",
                                          "pending activation"]},
    {"slot": "outage_status", "not_in": ["active", "degradation"]},
    {"slot": "gateway_status", "neq": "swap"},
    {"slot": "convoy_status", "neq": "predictive_swap"},
]}
# Two condition-gated rungs, exactly the repair-ladder pattern: a rung fires its executor
# and renders its `then_say` on the routing turn, where an `announce` renders empty
# because it does not drive the turn. Non-terminal + `verdict_delivered` latch so exactly
# one speaks. `device_id` is seeded by the before_agent sweep on the cold routing turn.
#
# Both rungs require SWEPT: they read `cable_modem_mac`, which is empty both when there is
# no gateway AND before the sweep has run at all. Ungated, a caller routed here with no
# account reaches RebootNoGateway on the opening turn and is wrongly told "no gateway on
# the account" while the accountNumber slot is still asking who they are.
reboot.task(
        "DoReboot", "verdict_steering_reboot", ["device_id"], "verdict_delivered",
        out_key="verdict_delivered",
        condition={"all": [SWEPT, NOT_YET_ANSWERED, _REBOOT_ALLOWED,
                           {"slot": "cable_modem_mac", "not_in": ["NOT_FOUND", ""]}]},
        then_say=scripts.SAY_REBOOT_STARTED)
# No gateway on the account: you cannot restart what is not there — say so and hand off,
# rather than claim a reboot over a no-op.
reboot.task(
        "RebootNoGateway", "verdict_missing_hardware", [], "verdict_delivered",
        out_key="verdict_delivered",
        condition={"all": [SWEPT, NOT_YET_ANSWERED,
                                              {"slot": "cable_modem_mac", "in": ["NOT_FOUND", ""]}]},
        then_say=scripts.SAY_MISSING_HARDWARE)


# --------------------------------------------------------------------------- #
# The `defer` child flows — one per out-of-scope intent. "Handle if we can, otherwise
# set the intent and hang up": each records its own intent (in the end_session reason)
# and ends the leg, so the outer GECX orchestration routes the caller onward.
# --------------------------------------------------------------------------- #

# The routable out-of-scope intents: the golden intent CATEGORIES this app recognises
# but does not resolve. Defined once in source_tools.ROUTE_CATALOGUE, which also
# generates the <routing> instruction, so the flow keys and the router descriptions
# cannot drift. Each key is a golden category, so a deferred call's detected_intent is a
# label the downstream GECX orchestration routes on.
DEFER_INTENTS = source_tools.DEFER_INTENTS


# The defer flows are not hand-built. Each defer category is an INTERNAL route node
# (steering_tree), and the multi-level router GENERATES one classification flow per
# category (the scoped head-intent classifier) plus a shared deferral flow, and a
# recorder that writes `detected_intent` (the leaf) and `detected_path` for downstream
# GECX routing.


# --------------------------------------------------------------------------- #
# The `human` child flow — the caller asked for a person on the opening turn. The
# repair flow keeps its own `escalate` control block for a human request that
# arises MID-repair; this handles the request that arrives as the routing turn,
# where that control block would not fire in time (the ladder speaks a verdict first).
#
# TWO DOORS INTO THIS FLOW, and it needs an answer on both, which is why it carries a
# control block AND a rung:
#
#   the engine's detector  `_ESCALATE_RE` recognises "speak to someone", "a live agent",
#                          "connect me to a person". `_handle_terminal_slots` runs at the
#                          TOP of the turn, ahead of collection and ahead of the ladder,
#                          so on this door the rung below is never reached — the
#                          disposition owns the turn, and undeclared it is `ctrl = {}`.
#   the router only        The route cues carry vocabulary the detector regex does not:
#                          "speak to a supervisor" matches the cue and NOT
#                          `_ESCALATE_RE`, whose noun list stops at
#                          person/human/agent/representative/rep/operator/someone.
#                          Nothing fires the control slot, so the rung has to speak.
#
# Both say SAY_HUMAN_ESCALATE, so the caller hears one sentence either way and the copy
# has one home. `cancel` is declared for the same reason it is on `repair`: "I want to
# cancel my service" routes here and matches `_CANCEL_OBJECT_RE`.
# --------------------------------------------------------------------------- #

human = flows.Flow("human", root_agent=_ROOT_AGENT,
                                      bootstrap={"reset_on_complete": True},
                                      escalate=flows.escalate(
                                          say=scripts.SAY_HUMAN_ESCALATE),
                                      cancel=flows.cancel(
                                          say=scripts.SAY_CANCELLED,
                                          requires_readback=True,
                                          confirm_say=scripts.SAY_CONFIRM_CANCEL))
# The statuses the hand-off payload WOULD be composed from, if this flow could ever see
# them. It cannot: the sweep is a remote job dispatched on the routing turn whose legs
# report seconds later, and this flow terminates on its first turn.
#
# So they must NOT be named as the rung's `inputs`. `inputs` is a HARD GATE
# (`_task_fireable`), so naming them there does not enrich the payload, it suppresses the
# rung entirely and makes SAY_HUMAN_ESCALATE — the golden line this flow exists to speak
# — unreachable config. Every param on `verdict_human_request` defaults to `""` and
# `compose_key_events=True` builds the summary from whatever it is handed.
#
# The slots stay declared: they are what the event mapping writes into if a status ever
# does land here, and removing them would silently drop it.
for _s in ("account_status", "outage_status", "network_status", "gateway_status"):
    human.add(flows.event_slot(_s))
human.add(flows.event_slot("verdict_delivered"))
_escalate_rung = flows.task("Escalate", "verdict_human_request", [],
                                            "verdict_delivered",
                                            out_key="verdict_delivered", condition=NOT_YET_ANSWERED,
                                            then_say=scripts.SAY_HUMAN_ESCALATE)
# ...and it ENDS the call. Without this the rung speaks the hand-off line and leaves the
# session open on a flow with nothing else in it, which is dead air for a caller who has
# just been told they are being connected. `escalated=True` is what the downstream GECX
# orchestration reads to place them.
_escalate_rung["then_response"] = _ends(True)
human.task(_escalate_rung)


# --------------------------------------------------------------------------- #
# The `steering` router — the top-level MULTI-LEVEL component. Its routes are built from
# ROUTE_CATALOGUE by steering_tree.build_routes: `repair`/`reboot`/`human` are handled
# leaves (each runs a local DAG), and each of the 15 defer categories is an INTERNAL node
# whose children are that category's golden head-intent leaves. The router GENERATES the
# <routing> block from the route descriptions and appends it to the App's
# agent_instruction (source_tools.AGENT_INSTRUCTION, whose <routing_policy> carries the
# cross-cutting fallback rules). L1 routing is the MODEL's decision via set_active_flow;
# the per-route `cues` below are a deterministic keyword backstop layered UNDER the model.
# --------------------------------------------------------------------------- #

# A deterministic keyword BACKSTOP layered under the model classifier. A matched cue
# fast-paths the route (engine `route_backstop`) BEFORE the model runs; anything a cue
# does NOT match falls to the model. So neither is relied on alone: cues catch the
# obvious phrasings deterministically, the model generalizes to the rest.
#
# Two rules these obey:
#   * `repair` gets NO entry. It is the catch-all the instruction sends anything
#     unmatched to, and a cue list for "any broken thing" would collide with every other
#     category's vocabulary ("my box", "my camera", "no picture") — two matching cues
#     leave the route unresolved. Leaving it cue-less also keeps the eval corpus an
#     honest generalization test.
#   * Cues are NATURAL production phrases, not corpus wording. Some do overlap the golden
#     corpus, and for those rows the backstop rather than the model decides, so the
#     honest model-generalization number comes from the cue-disjoint held-out corpus
#     (tests/routing_heldout.json).
ROUTE_CUES = {
        "reboot": ["reboot my gateway", "power cycle the modem", "reset the router"],
        "human": ["live agent", "a representative", "speak to a supervisor",
                            "connect me to someone",
                            # The RANK words. `_ESCALATE_NOUN_RE` stops at
                            # person/human/agent/representative/rep/operator/someone, so
                            # without these "get me a manager" and "I want your supervisor"
                            # reach no detector at all and are answered with silence,
                            # measured 3/3 over both text and voice. A hand-off request
                            # naming a rank is still a hand-off request.
                            #
                            # These are ROUTING-turn cues, so they catch the caller who
                            # opens with the demand. The same demand made MID-call is still
                            # unanswered — see the note above `diagnostics_sweep.tasks()`.
                            #
                            # The bare word "help" is deliberately NOT here, and that is a
                            # knowing divergence: on a repair line "can you help me" is the
                            # opening sentence of nearly every call, so treating it as a
                            # transfer request would send the whole queue to a person on
                            # their first breath.
                            "a manager", "your manager", "a supervisor",
                            "your supervisor", "an operator", "the operator",
                            "escalate",
                            # `_ESCALATE_RE`'s verb list is speak|talk|chat|connect|
                            # transfer|reach, so a request naming no verb reaches no
                            # detector -- and on the ROUTING turn there is no control
                            # slot to reach either: a router is neither cancelable nor
                            # escalatable by default, so `_escalate_tool` is empty and
                            # `transfer_to_human` maps to nothing. Without a cue the turn
                            # falls wholly to the model.
                            #
                            # Phrased long on purpose. A bare "someone else" would catch
                            # "is someone else having this problem?" -- an outage
                            # question, and one this agent really is asked.
                            "is there someone else", "someone else i can",
                            "anyone else i can"],
        "billing": ["my bill", "a charge on my account", "understand my bill", "my statement"],
        "payments": ["make a payment", "pay my bill", "set up autopay", "update my card"],
        "sales": ["upgrade my plan", "add a channel", "start new service", "renew my plan"],
        "technical_phone": ["my home phone", "no dial tone", "my voicemail", "landline"],
        "xfinity_mobile": ["xfinity mobile", "my mobile line", "my cell plan"],
        "appointments": ["my appointment", "schedule a technician", "reschedule my visit"],
        "activations": ["activate my equipment", "activate my service"],
        "service_center": ["nearest xfinity store", "a service center", "return my equipment"],
        "accessibility": ["closed captions", "audio description", "tty service"],
        "equipment_swap": ["swap my equipment", "exchange my device", "replace my box"],
        "phone_security": ["sim swap", "port-out fraud", "someone stole my number"],
        "transfers": ["add a user to my account", "referral program", "track my order"],
        "disambiguation_main_menu": ["wifi password", "rename my network", "guest network",
                                                                  "seasonal hold", "update my address"],
}

steering = flows.router_flow(
        "steering",
        steering_tree.build_routes(
            {"repair": repair, "reboot": reboot, "human": human}, ROUTE_CUES),
        # The cross-cutting routing policy, as structured knobs: repair is the home for
        # unsure/off-topic; disambiguation_main_menu is the catch-all for a known task
        # with no specific flow, so the model does not over-escalate to human. `human` is
        # explicit_only (see steering_tree). These render at the tail of the generated
        # <routing> block.
        default_route="repair",
        catch_all_route="disambiguation_main_menu",
        # classifier_style is not passed: it defaults to "enum", and the L1 gate and every
        # L2 leaf resolve with the same enum setter.
        # The routing turn is the slowest turn of the call. This goes out as a partial
        # preempt, so the route still lands on the same turn behind it.
        filler_say=scripts.FILLER_ROUTING,
        root_agent=_ROOT_AGENT,
)

# The greeting lives HERE, not in a child flow, because the router owns the opening turn:
# it is the turn on which nothing has been routed yet, and "what's going on with your
# service today?" is the routing question. Carried by a child, it arrives after that
# child's bridge line, back to front.
#
# `welcome_slot` on the bootstrap is the framework's own shape for this, so the engine
# speaks this announce on the opening turn, verbatim and ahead of the model, while the
# gate is still unfilled. `shared=True` so it does not re-speak after a hop.
steering.add(flows.announce(
        "welcome",
        [scripts.SAY_WELCOME],
        shared=True))
steering.set("bootstrap", dict(steering.to_config().get("bootstrap") or {},
                               welcome_slot="welcome"))


# Steering is a build flag (see build_config), OFF by default. The multi-level router is
# the root flow only when it is on; otherwise the flat single-flow `repair` agent is the
# root — the pre-router shape, where `repair` owns the opening turn (its account-number
# ask) and handles reboot-on-request and a human hand-off through its own rung and
# escalate rail. The `steering` router is still CONSTRUCTED above, but an unrooted flow is
# not emitted (the builder walks from `root_flow`), so the flat build carries no <routing>
# block and no router leaves; `AGENT_INSTRUCTION` is then emitted verbatim, without the
# generated routing block the router would otherwise append.
root_flow = steering if build_config.current().steering else repair


app = flows.App(
    root_flow=root_flow,
    # repair/reboot/human (handled leaves) plus the generated per-category classification
    # flows and the shared deferral flow are all added automatically from the router's
    # route tree, so no extra_flows are needed here.
    app_display_name="xa-repair-voice-flows",
    # The specialist proxy's generated spec. Declared here rather than grafted, because
    # unlike every other toolset in this app it is not the source's -- it is the contract
    # `diagnostics_sweep.resolve_specialists_remote` declares and the Cloud Run service
    # implements.
    toolsets=[diagnostics_sweep.specialists_service],
    # Composite for audio: its TTS renders bracket/affective tags and drives the bidi
    # voice path. The AUTHORED model wins over the source app's live model — see
    # build.patch_app_json, which restores this after grafting the source settings.
    #
    # Known robustness cost, independent of the guardrails: when the diagnostic backends
    # cannot answer, flash-live reaches the diagnostics-error rung and speaks a hand-off,
    # while composite loops on re-calls and then 400s with the caller hearing nothing.
    model="gemini-composite-v1",
    # The clock the reassurance runs on. A held turn produces no ticks by itself: the
    # platform emits one only when `audioProcessingConfig.inactivityTimeout` elapses, and
    # `while_waiting` drains a line per tick. With no timeout declared there are no ticks,
    # so the fan-out holds the floor in TOTAL SILENCE and every reassurance line authored
    # on the group is dead copy -- authored, validated, emitted, never spoken.
    #
    # Declared HERE rather than left to `flows deploy --inactivity-timeout`, the only
    # other way to set it. That flag applies at deploy time, so an app pushed any other
    # way -- `cxas push`, the console's own import -- silently loses it.
    #
    # The timeout is the CADENCE OF THE WAIT: only one task speaks per turn, and on a
    # silent line the only thing that produces the next turn is this timer, so everything
    # the sweep says after the bridge line is gated on it. It is also the UNIT every
    # silence-driven ladder in the app is measured in, so shortening it shortens all of
    # them at once, and ticks are polls as well as beats, making this a model-call rate.
    #
    # `no_input` is the one ladder that has to be paid for rather than absorbed: it counts
    # TURNS, so the wall clock a silent caller gets is (rungs + 1) x this timeout. See
    # `hold_reprompts` in `_account_no_input`.
    app_settings={"audioProcessingConfig": {"inactivityTimeout": "5s"}},
    # before_model completes the diagnostic sweep on the turn the caller speaks over
    # VOICE, where before_agent runs before the transport attaches the utterance and the
    # verdict lands a turn late.
    hooks=AgentHooks(before_agent=hooks.before_agent_callback,
                     before_model=hooks.before_model_callback),
    # Platform-enforced checks around every turn. Naming them here is what applies them:
    # the source's resources are copied to disk by the graft and nothing else reads them.
    # See guardrails.py for what each one is for and what a guardrail cannot do here.
    guardrails=guardrails.GUARDRAILS,
    # NOT declaring a tool timeout, and that is a dependency rather than a decision. The
    # sweep is a fan-out whose every leg retries with exponential backoff, and CES kills a
    # tool at 60s unless the resource says otherwise -- not a comfortable margin. A
    # raw-source body has no way to say so: `@flows.tool(timeout=)` is the decorator's
    # route and this body is emitted as source. `App(tool_timeouts=)` adds one, and is
    # cxas-labs PR #672, unmerged. Add
    # `tool_timeouts={source_tools.SWEEP_RESOLVED: 180}` here once that has shipped.
    #
    # Turns an arriving `async_sweep_armed` VARIABLE into a filled SLOT. Inert while the
    # sweep is synchronous, but kept wired because the hook reads it and a variable map is
    # the supported bridge: an event slot is filled only from event_data, so a bare
    # variable declaration leaves the hook and the task's condition reading two different
    # things.
    variable_maps=[
        flows.variable_map(
            "async_sweep", {"async_sweep_armed": "async_sweep_armed"},
            description="Take the ASYNC sweep path: the hook yields, SweepAsync owns it."),
    ],
    # NOT `search_tools=[...]`. That scopes search onto the agent for every turn and
    # leaves prose as the only restraint. The tool rides in on the device-help tasks
    # instead, and the SDK's `engine_task_tools` keeps it off the model.
    tool_bodies=source_tools.tool_bodies(),
    classifiers={
        "set_clarify_reply": (clarify.REPLY_CLASSIFIER, "UNSURE"),
        "set_wifi_walkthrough": (scripts.WIFI_WALKTHROUGH_CLASSIFIER, "DECLINE"),
        "set_wifi_scope": (scripts.WIFI_SCOPE_CLASSIFIER, "ALL_DEVICES"),
        # The device-worded clarifying question needs the same backstop as the app one;
        # without it a reply the cues cannot resolve has nowhere to land.
        "set_clarify_reply_device": (clarify.REPLY_CLASSIFIER, "UNSURE"),
    },
    variables=source_tools.variable_declarations() + [
        # The source app carries this in runtime state and never declared it. Seeding it
        # is what makes an account-less call ASK for the account instead of ending
        # without a word, so the converted app declares it.
        {
            "name": "awaiting_account_info",
            "description": "Set while the agent is waiting for an account number.",
            "schema": {"type": "STRING"},
        },
        # Set by an upstream agent that HANDS THIS CALL OVER -- a steering router via
        # transferToNga, or an A2A caller -- to suppress the opening greeting: the caller
        # has already been welcomed, so `repair` speaks only the account ask. A VARIABLE so
        # a hand-off can seed it (an event slot cannot be); `before_agent` reads it to pick
        # the account ask's `{welcome_lead}` -- bare "To get started," when set, greeting
        # and all when not. Unset on a direct call, where the greeting is spoken.
        {
            "name": "skip_greeting",
            "description": ("Set by an upstream agent handing this call over (e.g. "
                            "transferToNga) to drop the opening greeting; the caller was "
                            "already welcomed. Unset on a direct call. Read by "
                            "before_agent to choose the account ask's lead-in."),
            "schema": {"type": "STRING"},
        },
        # Declared as an app VARIABLE, not only as an event slot, because the two are not
        # interchangeable: a slot is filled by the engine, while a variable is what a
        # session can be SEEDED with — and seeding is how one call is put on the async
        # path while every other call keeps the hook. With only the slot, the hook's guard
        # reads state the seed never reaches and both paths behave identically.
        {
            "name": "async_sweep_armed",
            "description": ("Set to take the ASYNC sweep path: the hook yields and the "
                            "SweepAsync task owns the sweep. Unset everywhere by "
                            "default."),
            "schema": {"type": "STRING"},
        },
        # WHOSE specialists the proxy should open. The proxy is one Cloud Run service in
        # front of every build of this agent, and it opens a CES session at each
        # specialist — a session belongs to an APP, so a request naming none is answered
        # out of whichever app the service itself was deployed against. That is invisible
        # from here: two legs that never opened derive a HEALTHY line, and the caller is
        # told their connection is fine.
        #
        # A variable rather than anything baked into the flow, because a tool body has no
        # way to learn which app is running it: `context` carries `variables` and `state`
        # and nothing else. The id is assigned by CES at push time, so `build.py
        # --ces-app` bakes it here for a push to a known app and a session can seed it
        # otherwise. Empty is the safe default — the caller then sends nothing and the
        # proxy falls back exactly as it did before.
        {
            "name": build_config.CES_APP_VARIABLE,
            "description": ("This app's own CES resource name, sent to the specialist "
                            "proxy so it opens the specialists in THIS app."),
            "schema": {"type": "STRING", "default": build_config.current().ces_app},
        },
    ],
    # The multi-level router GENERATES the <routing> block from the route descriptions and
    # appends it to this instruction, so AGENT_INSTRUCTION (persona + constraints +
    # follow-up) is all that is passed here.
    agent_instruction=source_tools.AGENT_INSTRUCTION,
)


if __name__ == "__main__":
    errors, warnings = flows.validate_app(app)
    for w in warnings:
        print("warn:", w)
    for e in errors:
        print("ERROR:", e)
    print("errors:", len(errors), "warnings:", len(warnings))
