"""Drive `examples/automatic_fillers` offline and print what the caller actually hears.

The claim this feature makes is about WHEN a line is spoken, and that is invisible in
the config: both builds contain the same words. So this driver builds the same app
twice — pass off, pass on — runs the blessed engine over each, and prints the three
turns side by side.

Nothing is mocked. `_assemble` is the real build, `load_engine` is the blessed engine,
and `run_intake` lands the tool result the way the platform does. Only
`App.automatic_fillers` differs between the two runs.

    python -m examples.automatic_fillers_drive
"""

from __future__ import annotations

import copy

from flows.authoring import build as _build
from flows.engine import loader as fb

from examples.automatic_fillers import app as _app

_CID = "billing"
# The tool answers this; the wait is what we are trying to fill, not the payload.
_RESULT = {"success": True, "balance": "42 dollars and 10 cents"}


def _drive(enabled: bool) -> list[tuple[str, str]]:
  """Three turns against the real engine; returns `(label, what the caller hears)`."""
  app = copy.copy(_app)
  app.automatic_fillers = enabled
  config = _build._assemble(app)[0][_CID]  # noqa: SLF001

  engine = fb.load_engine()
  sm = fb.seed_sm(config)
  sm["filled"], sm["pending"] = {}, {}
  gate = sm.get("_gate_slot") or config.get("gate_slot")
  if gate:
    sm[gate] = sm["filled"][gate] = _CID

  def turn(text: str, n: int) -> dict:
    return engine.slot_filling_engine({
        "raw_config": config, "sm": sm, "last_user_text": text,
        "scanned_user_text": text, "is_inactivity": False, "event_data": {},
        "config_id": _CID, "n_user_turns": n,
    })["action"]

  ask = turn("I'd like my balance", 1)
  # The caller answers the question, which is the turn the task fires on.
  sm["filled"]["account_number"] = "5551234"
  fire = turn("5551234", 2)
  sm.update(fb.run_intake("fetch_balance", _RESULT, sm)["sm"])
  result = turn("", 3)

  silence = "(silence)"
  return [
      ("ask turn", ask.get("filler_partial") or silence),
      ("fire turn", fire.get("message") or silence),
      ("result turn", result.get("message") or silence),
  ]


def _hoists() -> list[tuple[str, str, str]]:
  """`(node, filler, what is left)` for everything the pass moved."""
  authored = {t["name"]: t.get("then_say")
              for t in _app.root_flow.to_config()["tasks"]}
  config = _build._assemble(_app)[0][_CID]  # noqa: SLF001
  out = []
  for task in config["tasks"]:
    filler = task.get("filler_say")
    if not filler:
      continue
    # The halves must rejoin to the authored line, or the pass edited rather than cut.
    assert f"{filler} {task['then_say']}" == authored[task["name"]]
    out.append((task["name"], filler, task["then_say"]))
  return out


def main() -> None:
  print("The tool sleeps 3 seconds. What fills that gap is the whole question.\n")
  for enabled in (False, True):
    print(f"automatic_fillers {'ON ' if enabled else 'OFF'}")
    for label, heard in _drive(enabled):
      marker = ""
      if enabled and label == "fire turn" and heard != "(silence)":
        marker = "   <- rides the fetch_balance call"
      print(f"  {label:12} {heard}{marker}")
    print()

  print("hoisted, and the halves rejoin to the authored line byte for byte:")
  for name, filler, rest in _hoists():
    print(f"  {name:14} {filler!r} + {rest!r}")

  print()
  print("refused, because the opener reports what the tool did:")
  config = _build._assemble(_app)[0][_CID]  # noqa: SLF001
  for task in config["tasks"]:
    if not task.get("filler_say") and task.get("then_say"):
      print(f"  {task['name']:14} {task['then_say']!r}")


if __name__ == "__main__":
  main()
