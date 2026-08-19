"""Run every check we can, and keep the caller company while they run."""

# Reassurance while the fan-out is out, one line per idle turn, draining rather than
# cycling. Deliberately NOT a per-leg status line: the legs are named for the systems
# they query, and a caller told "convoy is back" has been handed our plumbing instead of
# an answer.
#
# No dash and no figure of speech in either line: a dash chops the audio at the break
# (FLV001), and the guidelines rule out idioms outright. Each line says what is happening
# and that an answer is coming. The two stay DIFFERENT sentences, or the reassurance
# ladder check reads them as one line repeated.
SAY_SWEEP_WAITING = [
    "Still running those checks. I'll have the results in just a moment.",
    "Almost there, just waiting on the last result.",
]

SAY_SWEEP_UNAVAILABLE = (
    "I wasn't able to finish the checks on your line just now. Let me get you to "
    "someone who can help."
)

# The bridge line, split so `ContextGate` can carry it without the MUTE shape.
# `check_split_shapes` bans `filler_say` with nothing after it, and `ContextGate` is the
# turn every call passes through. Split at the seam the copy already had, so the caller
# hears the identical sentence in the identical order: "Thanks." lands immediately,
# covering the context fetch, and the rest follows when the gate returns.
SAY_BRIDGE_ACK = "Thanks."

# The turn the account number arrives on. `before_agent` has already run by the time the
# setter fires, so the sweep cannot happen until the NEXT turn and every diagnostic rung
# requires it. That leaves nothing pending and nothing eligible, and a free turn is where
# this model invents progress reports and calls raw backend operations to make them true.
#
# It also carries the implicit confirmation that the number landed, and that is the ONLY
# confirmation it ever gets: a mis-heard digit is otherwise discovered half a minute later,
# as an account-not-found hand-off. Riding the bridge turn is what makes it free: restated
# inside a line the caller was going to hear anyway, so it costs no turn and asks for no
# answer, which is what C7 means by implicit.
#
# No digits are read back. 2.13.1 and 4.4 authorize a last-four confirmation for a PHONE
# number only, and this slot accepts a phone number or an account number. Upstream also
# hands over redacted identifiers with no digits in them at all, so an interpolation would
# leave the caller hearing "the account ending in ." Restating that we have the account
# catches the same mis-hear, since a caller who gave the wrong number is being told their
# number was accepted, and it is true whatever upstream sent.
SAY_BRIDGE_TO_SWEEP = (
    "Thanks. I've got your account. Give me just a moment while I check your "
    "connection."
)

SAY_BRIDGE_TO_SWEEP_REST = (
    "I've got your account. Give me just a moment while I check your connection."
)

__all__ = [
    'SAY_BRIDGE_ACK',
    'SAY_BRIDGE_TO_SWEEP',
    'SAY_BRIDGE_TO_SWEEP_REST',
    'SAY_SWEEP_UNAVAILABLE',
    'SAY_SWEEP_WAITING',
]


import build_config
import flows
import scripts
import source_tools


# Silent ticks before the reassurance ladder starts, because the caller has just been
# ASKED something. `_async_idle_line` speaks on any turn the flow has nothing else to do,
# and a turn the caller spends THINKING looks exactly like dead air from inside the
# engine — it would skip the line for a pending slot, but the scoping question is a
# fragment of another task's line and `wifi_scope_early` is passive, so nothing is
# pending. Reassuring someone you have just asked something talks over them.
#
# An empty line is the engine's own silent-wait tick (`hold["silent"]`: an empty response,
# no audio), the same mechanism the `no_input` ladder uses to leave the caller the first
# word.
_QUIET_TICKS = 2


# The ladder drains a line per idle turn and those turns come fast, so how many the
# caller hears is the length of this list rather than a function of how long the sweep
# takes. A demo wants one; a real sweep is long enough to earn both.
_DEMO_WAITING = [""] * _QUIET_TICKS + (
    SAY_SWEEP_WAITING[:1]
    if build_config.current().demo
    else SAY_SWEEP_WAITING)


def context_gate_task():
  """The gate the sweep opens with: resolve the account and the MAC the rest depend on."""
  return flows.task(
      "ContextGate", source_tools.CONTEXT_GATE, ["accountNumber"],
      "has_mac", out_key="has_mac",
      extra_outputs={k: k for k in (
          "account_status", "cable_modem_mac",
          # Present only on the skip branches; absent otherwise, so the legs fill them.
          "network_status", "gateway_status", "outage_status", "convoy_status",
          # Marshalled through so the deferred legs get it as an ARGUMENT.
          "mock_config_string", "diagnostics_complete")},
      filler_say=SAY_BRIDGE_ACK,
      then_say=flows.say(SAY_BRIDGE_TO_SWEEP,
                         brief=SAY_BRIDGE_TO_SWEEP_REST),
      condition={"all": [{"slot": "accountNumber", "filled": True},
                         {"slot": "caller_spoke", "filled": True},
                         {"slot": "diagnostics_complete", "filled": False}]},
      on_failure={"max_retries": 0,
                  "on_exhaust": {"say": SAY_SWEEP_UNAVAILABLE,
                                 "then": {"tool": "verdict_no_telemetry"}}})


# The specialists are LLM agents inside CES rather than endpoints, so the service behind
# this contract is a PROXY: it opens a CES session entered directly at the specialist
# agents (`SessionConfig.entry_agent`) and lets the SAME agents answer, so nothing about
# them is reimplemented. It holds no Comcast credential of any kind — CES resolves the
# Secret Manager key server-side. `env_scoped` keeps the URL in environment.json and
# `service_agent_auth` is Cloud Run's own IAM, so there is no secret to wire here either.
specialists_service = flows.openapi_toolset(
    "specialist_proxy",
    base_url="https://comcast-specialist-proxy-555355609568.us-central1.run.app",
    auth=flows.service_agent_auth(),
    env_scoped=True,
)


# EVERY value the flow needs has to come back through `outputs`. A remote body runs in
# another process: no `context.variables`, no `context.state`, no `tools.`, and a
# side-effect write there is lost SILENTLY. `activityType`, `activityCode` and `jobType`
# are the sharp end of that — the technician-transfer copy interpolates them, and
# `hooks.py` defaults all three every turn, so losing them substitutes the WRONG dispatch
# payload instead of raising.
resolve_specialists_remote = flows.remote_tool(
    "resolve_specialists_remote", specialists_service, "resolveSpecialists",
    params={"accountNumber": str, "cable_modem_mac": str,
            "mock_config_string": str},
    outputs={"network_status": str, "gateway_status": str, "technician_type": str,
             "activityType": str, "activityCode": str, "jobType": str,
             "wifi_status": str},
    description=("Ask the network and gateway specialists for their diagnostic "
                 "statuses."),
    # SECONDS, and deliberately generous against the pair's real cost: a too-large budget
    # buys a slow failure nobody sees, a too-small one writes off a healthy job, and only
    # the second reaches the caller as a wrong answer.
    timeout=180,
)


def specialists_task():
  """The two specialist agents, as a REMOTE job rather than a held turn."""
  # A remote job because the agent never holds anything: the start call returns a handle
  # in under a second and the engine polls the job's status once per turn. On voice the
  # platform's inactivity ticks are those turns, so a silent caller still drives the
  # polling and still hears the reassurance below. Neither alternative works — an
  # `agentTool` cannot be a fan-out leg (progressive lowering inlines a python body, and
  # it has none), a synchronous tool yields no turns to speak over, and a deferred body's
  # nested agent call does not survive its turn.
  return flows.task(
      "Specialists", resolve_specialists_remote,
      {"accountNumber": "accountNumber", "cable_modem_mac": "cable_modem_mac",
       "mock_config_string": "mock_config_string"},
      "network_status", out_key="network_status",
      extra_outputs={k: k for k in ("gateway_status", "technician_type",
                                    "activityType", "activityCode", "jobType",
                                    "wifi_status")},
      condition={"all": [{"slot": "accountNumber", "filled": True},
                         {"slot": "caller_spoke", "filled": True},
                         {"slot": "diagnostics_complete", "filled": False},
                         {"slot": "account_status", "eq": "clear"},
                         {"slot": "has_mac", "eq": "true"},
                         {"slot": "network_status", "filled": False}]},
      # No `say`: `ContextGate` speaks both halves of the bridge line on this very turn,
      # and a third opening line stacks acknowledgements into one breath. `while_waiting`
      # speaks on the turns AFTER that.
      #
      # THE ONLY `while_waiting` in this flow. `SweepLegs` runs concurrently, and the
      # engine's idle line walks the pending waits in order with a counter PER WAIT, so a
      # second copy of the list is spoken over again from the top once this one drains.
      # It belongs here because this is the long pole: the legs are sub-second calls and
      # the reassurance has to outlast the wait it covers.
      #
      # Turns, not seconds. 30 is generous against a 180s budget on 8s ticks.
      awaits=flows.awaits(
          max_turns=30,
          while_waiting=_DEMO_WAITING,
          # NO `answer_first`, deliberately. With it set, an utterance that matches no cue
          # for an open slot returns an EMPTY response during this wait and wedges the
          # session permanently: no re-prompt, no reassurance, no hand-off, no end.
          # Reproduced 9 times out of 9 on "uh", "yeah", "what is taking so long" and
          # "are you still there". Without it, the caller's answer to the outstanding
          # question is dropped if it lands on the turn the job finishes on, which costs
          # one turn because the flow re-asks. A wedge costs the whole call.
          #
          # Not `answer_first=0`: the builder rejects anything that is not a positive
          # whole number of turns, so the only way to say "off" is to leave it out.
          #
          # This is a mitigation, not the cure, and the cure is not authorable here. The
          # `awaits` block accepts `say`, `while_waiting`, `answer_first`, `max_turns`,
          # `on_timeout` and `verbatim`, and none of them speak on an unmatched turn. The
          # floor belongs in the framework as an `on_unmatched` line. Do not restore
          # `answer_first` without re-driving TIMING-04, TIMING-05, TIMING-08 and
          # TIMING-09.
          on_timeout={"say": SAY_SWEEP_UNAVAILABLE,
                      "then": {"tool": "verdict_no_telemetry"}}),
      # A specialist that cannot answer must still leave a status behind, or the ladder
      # stays shut on every rung that reads one. Keyed by the service's own error code:
      # only a LOST job is worth restarting, because nobody said the work failed — that
      # is what the service redeploying looks like from here.
      on_failure={"max_retries": {"remote_job_lost": 1, "_default": 0},
                  "on_exhaust": {"say": SAY_SWEEP_UNAVAILABLE,
                                 "then": {"tool": "verdict_no_telemetry"}}})


def settle_task():
  """Close the sweep: reconcile the legs' output into the vocabulary the rungs read."""
  # This is where a routing action becomes a convoy status, where an absent status becomes
  # `skipped` rather than a free win for a lower rung, and where the two interpolated
  # messages get a value instead of raising mid-render. It also supplies
  # `diagnostics_complete`, so the ladder's gate means what it says.
  return flows.task(
      "Settle", source_tools.SETTLE, [], "diagnostics_complete",
      out_key="diagnostics_complete",
      extra_outputs={k: k for k in (
          "account_status", "outage_status", "convoy_status", "network_status",
          "gateway_status", "wifi_status", "technician_type", "outage_message",
          "customer_message", "convoy_customer_message", "cable_modem_mac", "device_id")},
      # Both halves of the sweep, not just the group: `network_status` is filled either by
      # `Specialists` or by the gate's MAC-less short circuit, so it is the one condition
      # that means "the specialists have answered" on every path.
      condition={"all": [{"slot": "SweepLegs_done", "filled": True},
                         {"slot": "network_status", "filled": True},
                         {"slot": "gateway_status", "filled": True},
                         {"slot": "diagnostics_complete", "filled": False}]})


def sweep_task():
  """The sweep, as ONE definition spliced into every flow whose rungs read it."""
  # A TASK is scoped to the flow it is declared in, so each door the router opens has to
  # carry the sweep or silently lose the statuses its rungs gate on. A function rather
  # than a module constant, because `flows.task` returns a mutable dict the builder may
  # adopt and two flows must not hold one object.
  return (
  # The mocked ladder cannot score this group. A honoured fake CANCELS the deferral: a
  # faked ASYNCHRONOUS tool answers inline instead of returning the `pending` placeholder
  # this group's bookkeeping is built on, so nothing marks the legs in flight and they are
  # dispatched again on the next pass. The statuses then race and the winning verdict
  # varies run to run. Judge this group live, or with the fixtures inlined into the
  # wrappers (`--legs=fake`), which sidesteps the platform fake path entirely.
  flows.parallel(
      "SweepLegs",
      tasks=[
          flows.task("leg_outage", "check_outage", {"accountNumber": "account_number",
                      "mock_config_string": "mock_config_string"},
                     "leg_outage_res",
                     out_key="outage_detected",
                     # The ladder gates on `outage_status`, never on `leg_outage_res`.
                     # Without this the leg runs, succeeds, and feeds nothing.
                     extra_outputs={k: k for k in (
                         "outage_status", "outage_message", "customer_message")},
                     condition={"all": [{"slot": "accountNumber", "filled": True},
                                        {"slot": "caller_spoke", "filled": True},
                                        {"slot": "diagnostics_complete", "filled": False},
                                        {"slot": "account_status", "eq": "clear"},
                                        {"slot": "outage_status", "filled": False}]}),
          flows.task("leg_convoy", "check_convoy_recommendations",
                     {"accountNumber": "account_number",
                      "mock_config_string": "mock_config_string"},
                     "leg_convoy_res", out_key="routing_action",
                     extra_outputs={"convoy_status": "convoy_status"},
                     # `has_mac` matters here as much as it does to the specialists: a
                     # recommendation derived without a cable modem is about nobody's
                     # hardware.
                     condition={"all": [{"slot": "accountNumber", "filled": True},
                                        {"slot": "caller_spoke", "filled": True},
                                        {"slot": "diagnostics_complete", "filled": False},
                                        {"slot": "account_status", "eq": "clear"},
                                        {"slot": "has_mac", "eq": "true"},
                                        {"slot": "convoy_status", "filled": False}]}),
      ],
      # Everything in this group is a real python tool. The two specialists are
      # `agentTool`s and cannot be legs: progressive lowering inlines a leg's python body,
      # and an agent tool has none.
      #
      # No `all_done_say`. Deferred, the group closes on one turn and the verdict rung
      # speaks on the next, so a bridge line here announces results the caller then waits
      # again to hear. The rungs carry their own lead copy.
      progressive=True,
      # No filler and no `while_waiting` here. `ContextGate` fires first on the same turn
      # and already speaks the opening line, and the reassurance belongs on `Specialists`
      # for the reason recorded there: `_async_idle_line` walks pending waits with a
      # counter PER WAIT, so a second copy of the list is spoken over again from the top.
      #
      # Turns, not seconds — the engine has no clock. Counted in TICKS, so on voice the
      # platform's inactivity timeout generates them even while the caller says nothing.
      deadline=15,
      # A deadline with no disposition drops the wait silently: the status slots never
      # fill, every verdict rung stays gated off, and the turn renders empty, which the
      # platform speaks as its own crash line.
      on_timeout={"say": SAY_SWEEP_UNAVAILABLE,
                  "then": {"tool": "verdict_no_telemetry"}},
  ))


# Which SOURCE tool each lowered leg wraps. Progressive lowering emits `<group>_<leg>_leg`
# and INLINES the author's body into it rather than calling the tool through `tools`,
# because a nested call from a deferred body is aborted once it outlives the turn. The
# cost is that the original tool's `toolFakeConfig` is bypassed with it, so every mocked
# scenario for a lowered leg goes silently live. `build.py` copies the fake across using
# this map.
SPIKE_LEG_FAKES = {
    "SweepLegs_leg_outage_leg": "check_outage",
    "SweepLegs_leg_convoy_leg": "check_convoy_recommendations",
}


SWEEP_SLOTS = [
    # `has_mac` is a string because DAG conditions compare strings; a bool would need a
    # second spelling everywhere it is read.
    flows.result_slot("has_mac", "ContextGate"),
    # `parallel(all_done_say=...)` normally creates this, and that line is dropped, so the
    # flag has to be declared here or every verdict rung loses the slot SWEPT gates on.
    flows.event_slot("SweepLegs_done"),
    flows.result_slot("mock_config_string", "ContextGate"),
    flows.result_slot("leg_outage_res", "leg_outage"),
    flows.result_slot("leg_convoy_res", "leg_convoy"),
]


def slots():
  """Where the checks land their answers."""
  return [
      *SWEEP_SLOTS,
  ]


def state_slots():
  """Bookkeeping the sweep needs, declared here because that is its ask position."""
  return [
      flows.event_slot("sweep_bridged"),
      flows.event_slot("async_sweep_armed"),
  ]


def tasks():
  """Run the checks, and keep the caller company while they run."""
  # DECLARATION ORDER is not cosmetic: `_async_idle_line` walks the pending waits in it,
  # so `Specialists` must come after `SweepLegs` for the reassurance ladder to sit on the
  # wait that actually lasts. Eligibility itself is settled by each task's condition.
  #
  # `sweep_task()` returns a ParallelGroup rather than a task dict, and `Flow.task()`
  # special-cases it by appending the group's SLOTS as well as its tasks, so the assembly
  # must keep passing items one at a time for that path to be reached.
  return [sweep_task(), context_gate_task(), specialists_task(), settle_task()]
