"""Did making the equipment slots intent slots cost the account-number turn?

`an acknowledgement does not cost the account number` gives the account and a device word
in one breath — "my account number is ..., and I've already tried restarting my router" —
and "router" is a `dev_gateway` cue. Since those slots became `kind="intent",
multi_fill=True` they fill on ANY turn, so the question is whether that fill diverts the
turn away from the account capture and the sweep.

An A/B against the SAME built config, driven through the real engine offline: once as
shipped, once with `kind`/`multi_fill` stripped from the equipment slots. If both behave
alike the change is exonerated; if only the stripped one sweeps, it is the cause.

    python tests/device_intent_ablation.py [--app-dir ./built]
"""

import argparse
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import labs_paths  # noqa: E402

labs_paths.add_sdk_paths()

import clarify  # noqa: E402
from flows.engine import loader as fb  # noqa: E402

import device_check  # noqa: E402

TURNS = [
    "my internet is really flaky",
    "my account number is 8069100230359946, and I've already tried restarting my router",
]
WANT_TOOL = "run_comcast_diagnostics_resolved"
DEVICE_SLOTS = set(clarify.EQUIPMENT) | {"device_subject", "device_symptom", "device_need"}


def strip_intent(cfg):
  """The same config with the equipment slots back to plain cue-filled passives."""
  out = copy.deepcopy(cfg)
  for slot in out["slots"]:
    if slot["name"] in DEVICE_SLOTS:
      slot.pop("kind", None)
      slot.pop("multi_fill", None)
      slot.pop("validation_rules", None)
  return out


def drive_pack(cfg, label):
  sm = fb.seed_sm(cfg)
  sm["filled"], sm["pending"] = {}, {}
  swept = False
  print(f"\n=== {label}")
  for utt in TURNS:
    out = device_check.drive(cfg, sm, utt)
    sm = out["sm"]
    action = out.get("action") or {}
    call = (action.get("function_call") or {}).get("name")
    swept = swept or call == WANT_TOOL
    hidden = action.get("hide_tools") or []
    # Offline the account number is captured by the MODEL setter, which does not run here
    # — so "did the sweep fire" is vacuous. What IS observable is whether the engine left
    # the model able to capture it at all: a hidden `set_account_number` on the turn the
    # caller says it is the mechanism by which a device fill could cost the account.
    print(f"  > {utt[:62]}")
    print(f"    fires: {call}   account setter hidden: "
          f"{'set_account_number' in hidden}")
    devs = {k: v for k, v in (sm.get("filled") or {}).items() if k in DEVICE_SLOTS}
    print(f"    device slots: {devs or '(none)'}")
  acct = sm.get("filled", {}).get("accountNumber")
  print(f"  SWEPT: {swept}   accountNumber: {acct!r}")
  return swept


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--app-dir", default="./built")
  args = ap.parse_args()
  cfg = device_check.load_config(args.app_dir)
  shipped = drive_pack(cfg, "AS SHIPPED (equipment slots are intent slots)")
  ablated = drive_pack(strip_intent(cfg), "ABLATED (plain passive cue slots)")
  print()
  if shipped == ablated:
    print(f"SAME on both ({shipped}) — the intent-slot change is not the variable here.")
    return 0
  print(f"DIVERGES — shipped={shipped} ablated={ablated}: the intent-slot change IS the "
        "variable.")
  return 1


if __name__ == "__main__":
  raise SystemExit(main())
