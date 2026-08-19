#!/usr/bin/env python3
"""How long the specialist pair actually takes, measured against the live service.

The number decides a design, so it should not be folklore. The tree carried two,
three times apart and neither with a run behind it: `app.py` and `main.py` both say
~30s (network 17.7s, gateway 27.1s), while the commit that shipped the Firestore store
reports a live start+poll landing both in 9.8s. Nothing reconciles them, and the gap is
the difference between a wait worth filling with conversation and one that is over
before the first inactivity tick.

    python tests/specialist_wait.py -n 5
    python tests/specialist_wait.py -n 5 --json results/specialist_wait.json

Start-to-terminal wall clock, sequentially so the runs do not contend, polled at 1s
because a 3s poll quantises a 10s job into three buckets. It measures the SERVICE, not
the agent: no engine, no model, no inactivity ticks. What the caller waits is this plus
the turn the dispatch rides on, so treat it as the floor.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import requests  # noqa: E402

from specialist_proxy.probe import SA, URL, headers  # noqa: E402

POLL_SECONDS = 1.0
# Past this a run is a failure worth reporting rather than a slow sample to average in.
GIVE_UP_SECONDS = 300.0


def one(url: str, head: dict, account: str, mac: str, mock: str) -> dict:
  """Start one job and poll to a terminal status. Returns the timing record."""
  t0 = time.time()
  r = requests.post(url + "/resolveSpecialists", headers=head, timeout=60,
                    json={"accountNumber": account, "cable_modem_mac": mac,
                          "mock_config_string": mock})
  started = time.time() - t0
  if not r.ok:
    return {"ok": False, "error": f"start HTTP {r.status_code}: {r.text[:200]}"}
  job = r.json()["jobId"]
  while time.time() - t0 < GIVE_UP_SECONDS:
    time.sleep(POLL_SECONDS)
    p = requests.get(f"{url}/resolveSpecialists/{job}", headers=head, timeout=60)
    body = p.json() if p.ok else {"status": f"HTTP {p.status_code}"}
    if body.get("status") != "running":
      return {"ok": body.get("status") == "done", "job": job,
              "start_seconds": round(started, 2),
              "total_seconds": round(time.time() - t0, 1),
              "status": body.get("status"),
              # The service times each leg itself. Worth carrying: it is the only way to
              # tell a slow pair from one slow specialist, and the two want different fixes.
              "net_seconds": (body.get("result") or {}).get("_net_seconds"),
              "gw_seconds": (body.get("result") or {}).get("_gw_seconds"),
              "degraded": body.get("degraded", False)}
  return {"ok": False, "error": f"still running after {GIVE_UP_SECONDS}s", "job": job}


def main() -> int:
  ap = argparse.ArgumentParser()
  ap.add_argument("-n", type=int, default=5)
  ap.add_argument("--url", default=URL)
  ap.add_argument("--account", default="8069100020078787")
  ap.add_argument("--mac", default="aa:bb:cc:dd:ee:ff")
  ap.add_argument("--mock", default="")
  ap.add_argument("--json", default="", help="write the raw records here")
  a = ap.parse_args()

  head = headers(a.url)
  health = requests.get(a.url + "/", headers=head, timeout=60).json()
  print(f"revision {health.get('revision')}  store={health.get('store')} "
        f"degraded={health.get('degraded')}")
  if health.get("degraded"):
    print("WARNING: the job store is degraded to memory; these numbers are not the "
          "shipped configuration")

  runs = []
  for i in range(a.n):
    rec = one(a.url, head, a.account, a.mac, a.mock)
    runs.append(rec)
    if rec.get("ok"):
      print(f"  run {i + 1}: start {rec['start_seconds']}s  total {rec['total_seconds']}s"
            f"  net={rec['net_seconds']} gw={rec['gw_seconds']}")
    else:
      print(f"  run {i + 1}: FAILED  {rec.get('error') or rec.get('status')}")

  good = [r["total_seconds"] for r in runs if r.get("ok")]
  print()
  if not good:
    print("no successful runs")
    return 1
  # Median, not mean: a cold start is a real event but it is not what most callers get,
  # and one of them drags a mean of five a long way.
  print(f"n={len(good)}/{a.n}  median {statistics.median(good)}s  "
        f"min {min(good)}s  max {max(good)}s")
  if a.json:
    os.makedirs(os.path.dirname(a.json) or ".", exist_ok=True)
    with open(a.json, "w") as fh:
      json.dump({"url": a.url, "revision": health.get("revision"),
                 "service_account": SA, "poll_seconds": POLL_SECONDS,
                 "runs": runs}, fh, indent=2)
    print(f"wrote {a.json}")
  return 0 if len(good) == a.n else 1


if __name__ == "__main__":
  raise SystemExit(main())
