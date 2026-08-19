#!/usr/bin/env python3
"""Drive the deployed specialist proxy directly, the way the agent does.

The service is OIDC-only, so this mints an ID token by impersonating the service's own
account (which is an invoker on itself for exactly this reason). Three things it answers
that no agent-side trace can:

  * which job store the live revision is really using (`/`)
  * whether a start call returns a handle in under a second
  * what a poll actually says, turn by turn, until the job lands

    python specialist_proxy/probe.py                          # health only
    python specialist_proxy/probe.py --start                  # live specialists
    python specialist_proxy/probe.py --start --mock 'network_status=impaired'
"""
from __future__ import annotations

import argparse
import json
import time

import google.auth
import requests
from google.auth import impersonated_credentials
from google.auth.transport.requests import Request

URL = "https://comcast-specialist-proxy-555355609568.us-central1.run.app"
SA = "comcast-spec-proxy@ces-deployment-dev.iam.gserviceaccount.com"
SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


def headers(url: str) -> dict:
  source, _ = google.auth.default(scopes=SCOPES)
  target = impersonated_credentials.Credentials(
      source_credentials=source, target_principal=SA, target_scopes=SCOPES, lifetime=600)
  idc = impersonated_credentials.IDTokenCredentials(target, target_audience=url,
                                                    include_email=True)
  idc.refresh(Request())
  return {"Authorization": f"Bearer {idc.token}"}


def main() -> int:
  ap = argparse.ArgumentParser()
  ap.add_argument("--url", default=URL)
  ap.add_argument("--start", action="store_true", help="start a job and poll it")
  ap.add_argument("--mock", default="", help="mock_config_string")
  ap.add_argument("--account", default="8069100020078787")
  ap.add_argument("--mac", default="aa:bb:cc:dd:ee:ff")
  ap.add_argument("--polls", type=int, default=40)
  a = ap.parse_args()

  head = headers(a.url)
  r = requests.get(a.url + "/", headers=head, timeout=60)
  print(f"GET  /            {r.status_code}  {r.text[:300]}")
  if not a.start:
    return 0 if r.ok else 1

  t0 = time.time()
  r = requests.post(a.url + "/resolveSpecialists", headers=head, timeout=60,
                    json={"accountNumber": a.account, "cable_modem_mac": a.mac,
                          "mock_config_string": a.mock})
  start = time.time() - t0
  print(f"POST /resolve...  {r.status_code}  {start:.2f}s  {r.text[:200]}")
  if not r.ok:
    return 1
  job = r.json()["jobId"]

  for i in range(a.polls):
    time.sleep(3)
    p = requests.get(f"{a.url}/resolveSpecialists/{job}", headers=head, timeout=60)
    body = p.json() if p.ok else {"status": f"HTTP {p.status_code}"}
    print(f"  poll {i + 1:2d} t={time.time() - t0:6.1f}s  {json.dumps(body)[:260]}")
    if body.get("status") != "running":
      return 0 if body.get("status") == "done" else 1
  print("still running after the poll budget")
  return 1


if __name__ == "__main__":
  raise SystemExit(main())
