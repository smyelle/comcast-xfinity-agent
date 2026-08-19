"""A tool body the SDK did not write, and how long it is allowed to take.

`@flows.tool(timeout=...)` is a decorator argument, so it reaches a tool you declared.
A body handed over as SOURCE has no decorator to carry it — and that is exactly the body
most likely to need one, because a grafted tool is usually the slow legacy fan-out nobody
wanted to reimplement. Until `App(tool_timeouts=...)` it silently took the platform's 60s
default — and driven, that default turns out to be shorter than advertised: a 75s body
with no declaration was gone at 32s, three runs running. The only symptom is a call that
stops.

Two arms, identical but for the declaration:

    ARM=default  python -m examples.tool_timeouts   # no timeout -> killed at ~32s
    ARM=declared python -m examples.tool_timeouts   # 180s -> the 75s body finishes

The body sleeps 75 seconds on purpose: past any plausible default, comfortably inside the
declared budget, so the two arms cannot both pass.
"""

import os

import flows

ARM = os.environ.get("ARM", "declared")

# A body as SOURCE — the shape a grafted tool arrives in. There is no decorator here to
# hang `timeout=` on, which is the whole point of the example.
SLOW_BODY = '''
def run_diagnostics(account_number: str = "") -> dict:
    """Run the carried diagnostic fan-out.

    Args:
      account_number: The account to run checks against.
    """
    import time
    time.sleep(75)
    return {"success": True, "summary": "All checks completed."}
'''

f = flows.Flow("checks", root_agent="root_agent", bootstrap={"welcome_slot": "welcome"})
f.add(
    flows.announce("welcome", ["Diagnostics desk. Say go when you're ready."],
                   shared=True, preempt=True),
    flows.user_slot("go", "Say go to start the checks."),
    flows.result_slot("summary", "Checks"),
    flows.announce("done", ["{summary}"], requires=["summary"], preempt=True, end=True),
)
f.task(flows.task(
    "Checks", "run_diagnostics", [], "summary", out_key="summary",
    condition=flows.has("go"),
    filler_say="Running the checks now.",
    on_failure={"max_retries": 0,
                "on_exhaust": {"say": "The checks didn't finish in time."}},
))

app = flows.App(
    root_flow=f,
    app_display_name=f"Tool Timeouts ({ARM})",
    model="gemini-composite-v1",
    agent_instruction="You run diagnostics. Be brief.",
    tool_bodies={"run_diagnostics": SLOW_BODY},
    # The one line under test. Without it this body takes the platform's 60s default and
    # is killed 15 seconds before it would have answered.
    tool_timeouts=({"run_diagnostics": 180} if ARM == "declared" else {}),
)


if __name__ == "__main__":
  out = f"./tool_timeouts_{ARM}_app"
  flows.build_app(app, out)
  print(f"built: {out}")
