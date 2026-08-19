"""Chat with the deployed agent from a terminal, on a named CUJ.

Every interesting path in this agent depends on two things a plain browser session cannot
give you: the backend tool FAKES (otherwise it reaches for real Comcast systems) and a
seeded `mock_config_string` (which decides what those fakes report). The CUJs in
`cujs.yaml` carry the seed; this wires up the fakes and drops you into a REPL.

    python try_agent.py                 # list the CUJs
    python try_agent.py outage
    python try_agent.py reboot          # then answer "yes" or "no"
    python try_agent.py --app <APP_ID> swap

`flows chat --cuj <name> --app <id>` does the same thing without this script.
Ctrl-D or "quit" to exit.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import labs_paths  # noqa: E402

labs_paths.add_sdk_paths(driver=True)

import flows  # noqa: E402

DEFAULT_APP = "5fc33f37-19c2-4dee-a0c0-7e88c911f627"      # xa-repair-voice-flows
OPENING = "my internet is not working"


def main(argv):
    app_id = DEFAULT_APP
    if "--app" in argv:
        i = argv.index("--app")
        app_id = argv[i + 1]
        del argv[i:i + 2]
    name = argv[0] if argv else ""

    cujs = flows.load_cujs(start=os.path.dirname(os.path.abspath(__file__)))
    if name not in cujs:
        print(__doc__)
        print("CUJs:")
        for cuj in cujs:
            alias = f" ({', '.join(cuj.aliases)})" if cuj.aliases else ""
            print(f"  {cuj.name + alias:34} {cuj.description}")
        return 0 if not name else 2

    return flows.chat(cujs[name], app_id, opening=OPENING)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
