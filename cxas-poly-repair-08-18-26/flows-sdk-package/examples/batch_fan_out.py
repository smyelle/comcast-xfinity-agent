"""`parallel(progressive=False)` — concurrency without the narration, and without the passes.

A group narrates each finding as it lands. That is the default and it is usually what you
want. This example is the case where it is not.

A turn is allowed ten reasoning passes and nothing resets that count. A narrating group
spends one pass dispatching and one more every time it wakes to look for a result, so a
flow that already spends most of its budget before the group is reached runs out
mid-diagnosis and the caller gets a turn that simply stops. That is not hypothetical: it
is what a real repair agent hit, on every journey, until this group was switched over.

`progressive=False` keeps the legs as ordinary synchronous tools. They still go out in one
action and the runtime still runs them concurrently — concurrency comes from dispatching
them together, not from the legs being deferred — but they all come back on the same pass.

    narrating (default)              progressive=False
    ------------------------------   ------------------------------
    legs run concurrently: yes       legs run concurrently: yes
    passes: 1 + one per check        passes: ONE
    a line per finding: yes          one moment, after the slowest leg
    legs keep their tool names: no   yes

Two things this shape gives back, and one it takes away.

Back: the pass budget, and the leg's own tool name. Narrating rewrites each leg into a
generated tool called `<group>_<leg>_leg`, so anything keyed to the ORIGINAL name — a
recorded test, a stand-in response — quietly stops matching it. The batch shape leaves the
names alone, which is worth more than it sounds: a fan-out that silently bypassed its
stand-in responses and called the real backends is exactly how one production adoption
failed, three times, without anyone being able to see why.

Away: the narration. One observation point, after the slowest leg (ces-probes 40). If the
group was covered by a single holding line rather than a line per finding, you were not
using the narration anyway.

Two rules a batch group must follow, both enforced at build:

  * its legs may NOT be `@flows.tool(asynchronous=True)`. A deferred leg answers with a
    placeholder and this shape has nothing waiting to collect it, so it is dispatched
    again on every pass until the turn dies.
  * `deadline` / `waiting_say` / `on_timeout` describe a wait that no longer happens, so
    `flows.parallel` refuses them.

Run: PYTHONPATH=packages/flows/src python packages/flows/examples/batch_fan_out.py
"""

import flows


@flows.tool(flow="acme_fault")
def diagnose_line(phone_number: str = "") -> dict:
    """Measure the line.

    Args:
        phone_number: The line to measure.

    Returns:
        The line finding.
    """
    import time

    time.sleep(4)
    return {"success": True, "line_finding": "dropping about every ten minutes"}


@flows.tool(flow="acme_fault")
def diagnose_account(phone_number: str = "") -> dict:
    """Check the account.

    Args:
        phone_number: The account to check.

    Returns:
        The account finding.
    """
    import time

    time.sleep(4)
    return {"success": True, "account_finding": "nothing on our side is restricting it"}


def build() -> flows.App:
    """The app: ask for a number, run both checks at once, report once.

    Returns:
        The assembled app.
    """
    fault = flows.Flow("acme_fault", root_agent="Acme_Broadband")
    fault.add(flows.user_slot(
        "phone_number",
        ask="What's the number on the account?",
        hint="the phone number on the account"))
    fault.add(flows.result_slot("line_finding", "line_leg"))
    fault.add(flows.result_slot("account_finding", "account_leg"))

    fault.task(flows.parallel(
        "diagnostics",
        tasks=[
            flows.task("line_leg", tool="diagnose_line",
                       inputs=["phone_number"], out_slot="line_finding"),
            flows.task("account_leg", tool="diagnose_account",
                       inputs=["phone_number"], out_slot="account_finding"),
        ],
        # One pass, both legs at once. Eight seconds of work in about four.
        progressive=False,
        # Rides the firing turn, so it lands before either backend is asked. With no
        # per-leg narration this is the only thing covering the wait, which makes it
        # more important here than in a narrating group, not less.
        filler_say="Let me run a couple of checks. This will just take a moment.",
        # The single observation point. Both findings, once, after the slower leg.
        all_done_say=("Both checks are back. Your line is {line_finding}, and"
                      " {account_finding}."),
    ))
    return flows.App(root_flow=fault, app_display_name="Acme Broadband (batch fan-out)")


app = build()


if __name__ == "__main__":
    import sys

    errors, warnings = flows.validate_app(app)
    for w in warnings:
        print("warn:", w)
    for e in errors:
        print("ERROR:", e)
    if errors:
        sys.exit(1)
    print("validate: ok")
