"""Start latency vs job duration against the deployed Cloud Run specialist proxy.

    python tests/measure_remote_latency.py

The whole claim of the remote-job design is that the caller's TURN cost stops tracking the
backend's duration. That is two numbers, and they have to be measured separately: what the
agent pays to START the job (which is what the caller waits for) and how long the job then
takes (which the caller does not wait for at all).
"""
import json, ssl, statistics, subprocess, time, urllib.request
import certifi
# Corp TLS interception: the system trust store is not on Python's path here.
CTX = ssl.create_default_context(cafile=certifi.where())

URL = "https://comcast-specialist-proxy-555355609568.us-central1.run.app"
TOK = subprocess.run(["gcloud", "auth", "print-identity-token"],
                     capture_output=True, text=True).stdout.strip()
HDR = {"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"}
import sys
# Two scenarios, because they answer different questions. The fixture path with an
# explicit delay holds the BACKEND constant so the start cost can be read against a known
# duration; the live path says what the real Comcast specialists actually cost today.
SCENARIOS = {
    "recorded 30s job": ("outage_status=none&convoy_status=clear&network_status=clear"
                         "&gateway_status=clear&context_status=clear&demo_delay=30"),
    "real specialists": "",
}

def post(path, body):
  req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), headers=HDR)
  return json.load(urllib.request.urlopen(req, timeout=120, context=CTX))

def get(path):
  req = urllib.request.Request(URL + path, headers=HDR)
  return json.load(urllib.request.urlopen(req, timeout=120, context=CTX))

def measure(label, scen, n=5):
 starts, totals = [], []
 for i in range(n):
  t0 = time.time()
  job = post("/resolveSpecialists", {"accountNumber": "8069100230359946",
                                     "cable_modem_mac": "AA:BB:CC:DD:EE:FF",
                                     "mock_config_string": scen})
  start = time.time() - t0
  starts.append(start)
  while True:
    time.sleep(0.5)
    st = get("/resolveSpecialists/" + job["jobId"])
    if st.get("status") != "running":
      break
  total = time.time() - t0
  totals.append(total)
  print(f"  run {i+1}: start={start:.2f}s  job={total:.1f}s  status={st.get('status')}")
 s_med, t_med = statistics.median(starts), statistics.median(totals)
 print(f"  start  median {s_med:.2f}s  range {min(starts):.2f}-{max(starts):.2f}s")
 print(f"  job    median {t_med:.1f}s   range {min(totals):.1f}-{max(totals):.1f}s")
 print(f"  the turn pays the START, not the JOB: {s_med:.2f}s vs {t_med:.1f}s "
       f"= {t_med/s_med:.0f}x\n")
 return s_med, t_med

for _label, _scen in SCENARIOS.items():
  print(f"== {_label} ==")
  measure(_label, _scen)
