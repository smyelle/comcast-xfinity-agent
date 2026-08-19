"""Specialist proxy — the HTTP service behind the Comcast sweep's `remote_tool`.

WHY THIS EXISTS
---------------
The network and gateway specialists are LLM agents INSIDE CES, wrapped in a synchronous
python tool. Measured live against the real Comcast dev backends, one specialist costs
19-31s and the pair holds the account-number turn for 42-43s. A blocking tool yields no
turns, so nothing can be spoken over it: the caller hears ~40s of dead air.

A `remote_tool` fixes that by making the wait pollable, but it needs an HTTP service —
and the specialists are not endpoints. So this service is a PROXY: it calls back into
the SAME CES agents. Nothing about them is reimplemented here, which is the whole point.
A migration whose purpose is behavioural equivalence cannot afford a second copy of
those prompts.

HOW IT REACHES THEM
-------------------
`SessionConfig.entry_agent` makes a sub-agent addressable on its own, so this opens a
CES session entered directly AT `network_specialist_agent` / `gateway_specialist_agent`.
No separate app, no copied prompt, no duplicated tool wiring.

CREDENTIALS: none here, deliberately. The specialists reach Comcast through an Apigee
auth proxy whose API key lives in Secret Manager and is resolved by CES server-side.
This service holds no Comcast credential of any kind; it authenticates to CES with its
own Cloud Run service-account identity (roles/ces.client). That is the security reason
to proxy rather than to reimplement -- reimplementing would mean copying that key here.

WHAT IT READS BACK, AND WHY NOT THE AGENT'S TEXT
------------------------------------------------
Each specialist is instructed to emit a raw JSON report. Driven through `entry_agent`
both of them reliably render the CES crash envelope ("Hmm, I'm having trouble with
that") on their FINAL model step -- after their backend call has already succeeded. So
the agent's prose is not a dependable channel.

Their own callbacks are. `connect_network_after` and the gateway's `after_tool_callback`
derive the statuses and write them to session state; that state comes back on the wire
as `updated_variables`. Harvesting it is strictly MORE faithful than parsing the model's
restatement of it, because it is the very value the source app's own callbacks compute.
The agent's JSON is still preferred when it parses; the state harvest is the backstop.

THE SIDE EFFECTS THAT MUST COME BACK AS OUTPUTS
-----------------------------------------------
A remote tool has no session state: it runs in another process, and a state write here
is silently lost. Today the network specialist writes `activityType` / `activityCode` /
`jobType` into the LIVE session as a side effect, and the P4 technician-transfer copy
interpolates them. `hooks.py` defaults all three every turn, so losing them does not
raise -- it silently substitutes the wrong dispatch payload, which is worse. They are
therefore declared OUTPUTS of the remote tool, not side effects. Same for `wifi_status`,
which the gateway specialist writes.

FIXTURE MODE
------------
`mock_config_string` is the eval harness's channel, and it is honoured here for the same
reason the CES `agentTool` fakes honour it today: every scripted rung in `diag_check`
gets its statuses from that string. Keeping it means the eval measures the full remote
round trip (start, poll, land) instead of losing 9 of its 13 scenarios. It changes
nothing about the live path -- absent the string, the real specialists run.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional

import google.auth
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from google.api_core.client_options import ClientOptions
from google.cloud.ces_v1beta import SessionServiceClient, types
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("specialist_proxy")

CES_ENDPOINT = os.environ.get("CES_API_ENDPOINT", "ces.googleapis.com")
#: FALLBACK only. The app is now taken per REQUEST, because a single baked-in value made
#: this a one-tenant service: any caller that was not this exact app got its specialists
#: silently failed, which is a `success: false` with nothing naming the cause. The default
#: stays so callers that do not send one keep working unchanged.
DEFAULT_APP = os.environ.get(
    "COMCAST_APP",
    "projects/ces-deployment-dev/locations/us/apps/"
    "2e058bf5-e8ff-40c7-a45f-9676919ab68b")
NETWORK_AGENT = os.environ.get("NETWORK_AGENT", "network_specialist_agent")
GATEWAY_AGENT = os.environ.get("GATEWAY_AGENT", "gateway_specialist_agent")
COLLECTION = os.environ.get("JOBS_COLLECTION", "comcast_specialist_jobs")
DATABASE = os.environ.get("FIRESTORE_DATABASE", "(default)")

DEFAULT_DEADLINE = 120.0
MAX_DEADLINE = 600.0
HEARTBEAT_SECONDS = 5.0
STALE_WORKER_SECONDS = 60.0
# The floor on ONE specialist's budget, whatever deadline the caller asked for. Kept well
# above the measured cost so a caller who asks for less is not asking for a timeout rather
# than an answer. Named rather than inlined so the self-test can shorten it; nothing else
# should.
#
# MEASURED, 2026-08-12, revision 00007 against the real backends, five sequential runs
# (`tests/specialist_wait.py`, raw in `tests/results/specialist_wait.json`): start to
# terminal 7.0 / 7.1 / 8.3 / 8.3 / 9.4s, median 8.3s. Per leg, network 2.6-3.9s and
# gateway 6.7-8.3s, run concurrently, so the gateway is the pole and the pair costs about
# what the gateway costs.
#
# This CORRECTS the figure that was carried here and in two places in app.py: "about
# thirty seconds, network 17.7s, gateway 27.1s". Nothing in the tree ever backed it with a
# run, and it is out by more than 3x. 30.0 stays as the floor -- it is a safety margin,
# and now a generous one -- but no design should be argued from the old number.
MIN_LEG_SECONDS = 30.0

# How long a RECORDED answer pretends to take, so the wait is audible in a demo.
#
# It exists for one reason: a fixture answers in microseconds, so the job lands inside
# the turn that started it and a demo shows a correct verdict with none of the waiting
# the feature exists for -- no tick, no reassurance line, nothing to hear. The real pair
# costs a measured 8.3s median (see MIN_LEG_SECONDS), so this is a recorded latency for a
# recorded answer rather than an invention, and 8.0 is close to the real thing.
#
# Tuned against the demo app's 8s `inactivityTimeout` for EXACTLY ONE reassurance line:
# long enough that the first tick lands while the job is still running, short enough that
# the second one is the verdict rather than a second line. Two lines is a caller
# wondering whether anyone is there; none is a demo that proves nothing.
#
# Only the demo build asks for it (`demo_delay` in `mock_config_string`). The eval
# harness does not, so `diag_check` is as fast as it ever was, and a live call never
# reaches this branch at all.
DEMO_SWEEP_SECONDS = 8.0

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_creds, _project = google.auth.default(scopes=SCOPES)
_ces = SessionServiceClient(client_options=ClientOptions(api_endpoint=CES_ENDPOINT),
                            credentials=_creds)

# The vocabulary the ladder reads. Anything else is reported as healthy, exactly as
# `_specialists_source` does today -- an unknown status must not let a lower-priority
# rung win by default.
GATEWAY_VOCAB = ("reboot", "swap", "no_telemetry", "unsupported_device", "error")

app = FastAPI(title="comcast-specialist-proxy")


def _now() -> float:
  return time.time()


def _iso() -> str:
  return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Job store (Firestore, with a local fallback so the service still boots)
# --------------------------------------------------------------------------
class JobStore:
  name = "unset"

  def put(self, job_id: str, doc: dict) -> None: ...
  def get(self, job_id: str) -> Optional[dict]: ...


class FirestoreStore(JobStore):
  """Durable job state, and it PROVES it is durable before claiming to be.

  The constructor used to only build a client, which does no I/O -- so the store
  reported itself as `firestore` at boot and only discovered it could not write when a
  caller was already on the line. Worse, the first write is inside the start call, so
  the failure surfaced as an HTTP 500 on `POST /resolveSpecialists` and reached the
  agent as `remote_unreachable`: a permissions problem wearing a networking problem's
  name. The canary write below moves that discovery to startup, where the log line is
  the first thing in the revision's history.
  """

  name = "firestore"

  def __init__(self):
    from google.cloud import firestore
    self._c = firestore.Client(project=_project, database=DATABASE)
    probe = self._c.collection(COLLECTION).document("_startup_probe")
    probe.set({"revision": os.environ.get("K_REVISION", "local"), "at": _iso()})
    probe.delete()

  def put(self, job_id, doc):
    self._c.collection(COLLECTION).document(job_id).set(doc)

  def get(self, job_id):
    snap = self._c.collection(COLLECTION).document(job_id).get()
    return snap.to_dict() if snap.exists else None


class MemoryStore(JobStore):
  """Not durable across revisions -- a redeploy turns in-flight jobs into
  `remote_job_lost`, which the agent is written to handle."""
  name = "memory"

  def __init__(self):
    self._d: dict[str, dict] = {}
    self._lock = threading.Lock()

  def put(self, job_id, doc):
    with self._lock:
      self._d[job_id] = dict(doc)

  def get(self, job_id):
    with self._lock:
      d = self._d.get(job_id)
      return dict(d) if d else None


class ResilientStore(JobStore):
  """Firestore when it answers, memory when it does not -- and never quietly.

  Durability matters to a remote tool: a redeploy that drops in-flight jobs makes
  `remote_job_lost` the routine path instead of the rare one, and the agent then pays a
  retry (a whole second job) on every rollout. But an unreachable job store must not
  take the feature down either, so memory is the floor.

  The degradation is DELIBERATELY loud. It was not, and that cost a day: the first
  version logged a warning and answered `/` with a store name nobody reads, so a service
  running on a process-local dict looked identical to a durable one from every angle the
  agent can see. Now the fall back logs at ERROR with the exception, is stamped with the
  revision that did it, and is carried on `degraded` in every status answer -- which
  lands in the CES trace next to the poll that read it.

  The denial that provoked all this was real and is now understood: `roles/datastore.user`
  had been granted to the service account, but the revision serving the traffic had been
  started BEFORE the grant, and a Firestore client caches its authorization. The first
  revision rolled out after the grant writes fine. `_startup_probe` in `FirestoreStore`
  is what makes that legible now -- a revision that cannot write says so in its first log
  line rather than in a caller's 500.
  """

  def __init__(self):
    self._mem = MemoryStore()
    self._fs: Optional[JobStore] = None
    self.reason = ""
    if os.environ.get("JOB_STORE", "firestore") == "firestore":
      try:
        self._fs = FirestoreStore()
      except Exception as exc:  # noqa: BLE001
        self._degrade("startup probe", exc)
    self.name = "firestore" if self._fs else self.name

  def _degrade(self, where: str, exc: Exception) -> None:
    self._fs = None
    self.name = "memory"
    self.reason = f"{where}: {type(exc).__name__}: {exc}"
    logger.error(
        "JOB STORE DEGRADED to in-memory (revision %s): %s. In-flight jobs will not "
        "survive a redeploy and the agent will see remote_job_lost.",
        os.environ.get("K_REVISION", "local"), self.reason)

  @property
  def degraded(self) -> bool:
    return self._fs is None

  # Both of these read `self._fs` into a LOCAL first, and call the local. The check and
  # the call are not one operation: this service is a FastAPI app with a worker thread
  # per job, so a second thread can hit an error and `_degrade()` -- which sets
  # `self._fs = None` -- in the window between them. Re-reading the attribute for the
  # call turned a degradation, which is meant to be survivable, into
  # `AttributeError: 'NoneType' object has no attribute 'put'` on an unrelated caller's
  # thread. The local reference is still usable after a degrade; it just writes to a
  # store nobody will read from again, which is exactly what falling back means.
  def put(self, job_id, doc):
    self._mem.put(job_id, doc)
    fs = self._fs
    if fs:
      try:
        fs.put(job_id, doc)
      except Exception as exc:  # noqa: BLE001
        self._degrade("write", exc)

  def get(self, job_id):
    fs = self._fs
    if fs:
      try:
        doc = fs.get(job_id)
        if doc:
          return doc
      except Exception as exc:  # noqa: BLE001
        self._degrade("read", exc)
    return self._mem.get(job_id)


STORE: ResilientStore = ResilientStore()
logger.info("job store: %s (%s/%s)%s", STORE.name, DATABASE, COLLECTION,
            f" -- {STORE.reason}" if STORE.reason else "")


# --------------------------------------------------------------------------
# Talking to a CES specialist
# --------------------------------------------------------------------------
def _run_specialist(agent: str, text: str, variables: dict[str, str],
                    use_fakes: bool, timeout: float, app: str = "") -> dict[str, Any]:
  """Drive ONE specialist through `entry_agent` and harvest what it produced.

  Returns the agent's parsed JSON report (when it managed to emit one), the session
  state its own callbacks wrote, and the raw tool responses -- the three channels the
  derivation below falls back through.

  `timeout` is enforced on the RPC itself, and it has to be. A `future.result(timeout=)`
  around this call abandons the RESULT and nothing else: the worker thread keeps sitting
  in `run_session` for as long as CES takes, and the pool's own shutdown then waits for
  it -- see the note at the call site. Only the client can actually give up.
  """
  app = app or DEFAULT_APP
  session = f"{app}/sessions/proxy-{uuid.uuid4().hex[:12]}"
  cfg = types.SessionConfig(session=session, use_tool_fakes=use_fakes,
                            entry_agent=f"{app}/agents/{agent}")
  inputs = [types.SessionInput(variables=variables), types.SessionInput(text=text)]

  t0 = _now()
  resp = _ces.run_session(request=types.RunSessionRequest(config=cfg, inputs=inputs),
                          timeout=timeout)
  elapsed = _now() - t0

  state: dict[str, Any] = {}
  texts: list[str] = []
  tool_results: list[dict] = []
  for out in resp.outputs:
    di = getattr(out, "diagnostic_info", None)
    if not (di and getattr(di, "messages", None)):
      continue
    for m in di.messages:
      role = getattr(m, "role", "")
      for c in m.chunks:
        kind = c._pb.WhichOneof("data") if hasattr(c, "_pb") else None
        if kind == "updated_variables":
          for k, v in dict(c.updated_variables).items():
            state[k] = v
        elif kind == "tool_response":
          try:
            tool_results.append(_proto_to_dict(c.tool_response))
          except Exception:  # noqa: BLE001
            pass
        elif kind == "text" and role != "user":
          if c.text and c.text.strip():
            texts.append(c.text.strip())

  report = _first_json_object(texts)
  logger.info("specialist %s: %.2fs state_keys=%s report=%s",
              agent, elapsed, sorted(state.keys())[:12], bool(report))
  return {"elapsed": elapsed, "state": state, "report": report,
          "tool_results": tool_results, "texts": texts}


def _proto_to_dict(msg) -> dict:
  from google.protobuf.json_format import MessageToDict
  return MessageToDict(msg._pb if hasattr(msg, "_pb") else msg)


def _first_json_object(texts: list[str]) -> dict:
  """The specialists are told to answer with a raw JSON block; take the first that is."""
  for t in texts:
    s = t.strip()
    if "```" in s:
      start = s.find("{")
      end = s.rfind("}")
      if start >= 0 and end > start:
        s = s[start:end + 1]
    if s.startswith("{"):
      try:
        v = json.loads(s)
        if isinstance(v, dict):
          return v
      except Exception:  # noqa: BLE001
        continue
  return {}


# --------------------------------------------------------------------------
# Deriving the ladder's vocabulary -- the same rules `_specialists_source` applies
# --------------------------------------------------------------------------
def _derive(net: dict, gw: dict) -> dict[str, str]:
  out: dict[str, str] = {}

  n_report, n_state = net.get("report") or {}, net.get("state") or {}
  g_report, g_state = gw.get("report") or {}, gw.get("state") or {}

  # --- network ---------------------------------------------------------
  # The agent's own JSON first; its callbacks' state write is the backstop.
  net_status = str(n_report.get("network_status")
                   or n_state.get("network_status") or "healthy")
  tech_type = str(((n_report.get("recommendation") or {}).get("technician_type"))
                  or _tech_from_analysis(net) or _tech_from_activity(n_state) or "")

  # "No Technician Required" is an ANSWER, not a type. Passed through it fills the slot
  # the dispatch and fee copy interpolates, so a healthy line offers to send nobody.
  #
  # EMPTY rather than absent, which `_fixture` has always done and this branch did not.
  # `technician_type` is a DECLARED output: the generated wrapper lifts every one of them
  # by name, so an absent key arrives as None, counts as `missing from response`, and the
  # task never completes -- the caller gets `SAY_SWEEP_UNAVAILABLE` and a transfer on a
  # line that came back perfectly healthy. Measured cold on a real account: healthy /
  # healthy derived correctly and the journey still ended in `verdict_no_telemetry`,
  # because this one key was omitted instead of emptied. A declared output is a promise
  # to always send the key; "no technician" is a VALUE of it, not the absence of it.
  out["technician_type"] = (
      tech_type if tech_type and tech_type.strip().lower() != "no technician required"
      else "")

  # A technician recommendation counts as impairment even when the agent called the line
  # healthy -- the source treats the recommendation as the stronger signal.
  if net_status == "impaired" or tech_type.lower() in (
      "network tech", "install and repair tech"):
    out["network_status"] = "impaired"
  elif net_status == "error":
    out["network_status"] = "error"
  else:
    out["network_status"] = "healthy"

  # --- gateway ---------------------------------------------------------
  gw_status = str(g_report.get("gateway_status") or g_state.get("gateway_status")
                  or "healthy")
  out["gateway_status"] = gw_status if gw_status in GATEWAY_VOCAB else "healthy"

  # --- the side effects that would otherwise be lost -------------------
  # Written into the LIVE session today by the network specialist's after_tool callback
  # and interpolated by the P4 transfer. A remote body's state write goes nowhere, so
  # they travel as declared outputs instead.
  out["activityType"] = str(n_state.get("activityType") or "TROUBLE_CALL")
  out["activityCode"] = str(n_state.get("activityCode") or "")
  out["jobType"] = str(n_state.get("jobType") or "")
  out["wifi_status"] = str(g_state.get("wifi_status") or "skipped")
  return out


def _tech_from_analysis(net: dict) -> str:
  """`analysis_response.Recommendation["Technician Type"]`, the callback's own field."""
  for tr in net.get("tool_results") or []:
    resp = (tr or {}).get("response") or {}
    result = resp.get("result") if isinstance(resp.get("result"), dict) else resp
    analysis = (result or {}).get("analysis_response") or {}
    rec = analysis.get("Recommendation") or {}
    if isinstance(rec, dict) and rec.get("Technician Type"):
      return str(rec["Technician Type"])
  return ""


def _tech_from_activity(state: dict) -> str:
  """Invert the callback's own activityCode mapping when nothing else said."""
  return {"PR": "Network Tech", "H3": "Install and Repair Tech"}.get(
      str(state.get("activityCode") or "").upper(), "")


# --------------------------------------------------------------------------
# Fixture mode -- the eval harness's scripted statuses
# --------------------------------------------------------------------------
_NET_ALIASES = {"clear": "healthy", "none": "healthy", "ok": "healthy",
                "impairment": "impaired", "network_tech": "impaired"}


def _query(qs: str, key: str) -> str:
  value = ""
  for pair in str(qs or "").split("&"):
    name, sep, raw = pair.partition("=")
    if sep and name.strip() == key:
      value = raw.strip()
  return value


def _number(raw: str) -> float:
  try:
    return float(str(raw or "").strip())
  except ValueError:
    return 0.0


def _fixture(mock_config_string: str) -> dict[str, str]:
  """The same canned statuses the CES `agentTool` fakes return today."""
  net = _NET_ALIASES.get(_query(mock_config_string, "network_status"),
                         _query(mock_config_string, "network_status")) or "healthy"
  gw = _query(mock_config_string, "gateway_status") or "healthy"
  gw = {"clear": "healthy", "none": "healthy"}.get(gw, gw)
  out = {"network_status": net if net in ("healthy", "impaired", "error") else "healthy",
         "gateway_status": gw if gw in GATEWAY_VOCAB else "healthy",
         "activityType": "TROUBLE_CALL", "activityCode": "H2", "jobType": "Test",
         "wifi_status": "skipped"}
  tech = _query(mock_config_string, "technician_type")
  if out["network_status"] == "impaired":
    tech = tech or "Network Tech"
  if tech and tech.strip().lower() != "no technician required":
    out["technician_type"] = tech.replace("+", " ").replace("%20", " ")
    code = {"network tech": ("SPECIAL_REQUEST", "PR", "PR"),
            "install and repair tech": ("TROUBLE_CALL", "H3", "AO")}.get(
                out["technician_type"].strip().lower())
    if code:
      out["activityType"], out["activityCode"], out["jobType"] = code
  else:
    out["technician_type"] = ""
  return out


# --------------------------------------------------------------------------
# The job
# --------------------------------------------------------------------------
class StartIn(BaseModel):
  #: The CES app whose specialists should answer. Empty means DEFAULT_APP, so an older
  #: caller behaves exactly as before. Sending it is what makes this service multi-tenant:
  #: one proxy can serve every build of the agent instead of whichever one it was pinned to.
  app: str = ""
  accountNumber: str = ""
  cable_modem_mac: str = ""
  current_date: str = ""
  mock_config_string: str = ""
  use_tool_fakes: bool = False
  deadline_seconds: float = Field(default=DEFAULT_DEADLINE)


def _work(job_id: str, body: StartIn) -> None:
  started = _now()

  def beat(extra: dict | None = None) -> None:
    doc = {"status": "running", "started": started, "heartbeat": _now(),
           "deadline": started + min(body.deadline_seconds, MAX_DEADLINE),
           "updated": _iso()}
    doc.update(extra or {})
    STORE.put(job_id, doc)

  beat()
  try:
    # `mock_config_string` IS the harness signal, and nothing else can be. A remote tool
    # declares its own parameters and cannot see `use_tool_fakes`, and the SDK's `mock=`
    # is static data -- it cannot vary with the session, so it can only ever express one
    # of this agent's thirteen scenarios. The variable is the same one the CES agentTool
    # fakes read today, and only the eval harness ever seeds it: a real call arrives with
    # it empty and takes the live path below.
    if _query(body.mock_config_string, "network_status") or _query(
        body.mock_config_string, "gateway_status"):
      # The harness is driving. Same fixture the CES agentTool fakes apply today, so a
      # scripted rung still reaches its verdict -- but over the real remote round trip.
      #
      # `demo_delay` gives that round trip a RECORDED LATENCY -- see DEMO_SWEEP_SECONDS.
      # `demo_delay=on` asks for the tuned default, which is what the demo build sends;
      # an explicit number overrides it, which is how it was tuned.
      asked = _query(body.mock_config_string, "demo_delay")
      delay = _number(asked) or (DEMO_SWEEP_SECONDS if asked else 0.0)
      delay = min(max(delay, 0.0), 60.0)
      deadline = started + min(body.deadline_seconds, MAX_DEADLINE)
      while delay and _now() - started < delay and _now() < deadline:
        # Heartbeat while sleeping: a job quiet for STALE_WORKER_SECONDS is reported
        # lost, and a recorded delay must not look like a retired worker. Sleep only
        # what is LEFT, so the constant means the number it says rather than that
        # number rounded up to the next heartbeat.
        time.sleep(max(min(HEARTBEAT_SECONDS, delay - (_now() - started)), 0.05))
        beat()
      result = _fixture(body.mock_config_string)
      logger.info("job %s: fixture mode (delay=%.0fs) -> %s", job_id, delay, result)
    else:
      variables = {k: v for k, v in {
          "cable_modem_mac": body.cable_modem_mac,
          "accountNumber": body.accountNumber,
          "account_id": body.accountNumber,
          "current_date": body.current_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
          "mock_config_string": body.mock_config_string,
      }.items() if v}

      # The budget goes to the RPC, not just to the wait on the future.
      #
      # `future.result(timeout=)` abandons the RESULT and nothing else -- there is no way
      # to interrupt a running thread in Python -- and `with ThreadPoolExecutor(...)`
      # exits through `shutdown(wait=True)`, which then JOINS both workers. So a wedged
      # specialist used to hang this job's thread at the closing brace, past every
      # timeout, and the job sat `running` until its heartbeat went stale: a
      # `remote_job_lost` the agent then retried into a second wedged pair. The
      # `.result()` timeouts below could not fire before that happened; they were
      # measuring a wait nobody was still waiting on.
      #
      # Two changes, and both are needed. The client-side `timeout` is what actually
      # ends the call, so the worker returns instead of blocking forever. `wait=False`
      # is the backstop for everything a gRPC deadline does not cover (a hung fixture,
      # a retry loop inside the client): this thread stops being hostage to a worker
      # that has not noticed it is late, and Python joins the strays at exit.
      leg_budget = max(body.deadline_seconds, MIN_LEG_SECONDS)
      pool = ThreadPoolExecutor(max_workers=2)
      try:
        net_f = pool.submit(_run_specialist, NETWORK_AGENT, "measure line signals",
                            variables, body.use_tool_fakes, leg_budget, body.app)
        gw_f = pool.submit(_run_specialist, GATEWAY_AGENT, "triage gateway logs",
                           variables, body.use_tool_fakes, leg_budget, body.app)
        # One deadline across BOTH waits, not one each: the legs run concurrently, so
        # waiting the full budget twice would spend two of them on a pair that shares
        # the first.
        wait_until = _now() + leg_budget
        try:
          net = net_f.result(timeout=max(wait_until - _now(), 0.0))
        except Exception as exc:  # noqa: BLE001
          logger.warning("job %s: network specialist failed: %s", job_id, exc)
          net = {}
        try:
          gw = gw_f.result(timeout=max(wait_until - _now(), 0.0))
        except Exception as exc:  # noqa: BLE001
          logger.warning("job %s: gateway specialist failed: %s", job_id, exc)
          gw = {}
      finally:
        pool.shutdown(wait=False)
      result = _derive(net, gw)
      result["_net_seconds"] = f"{net.get('elapsed', 0):.2f}"
      result["_gw_seconds"] = f"{gw.get('elapsed', 0):.2f}"
      logger.info("job %s: live specialists -> %s", job_id, result)

    STORE.put(job_id, {"status": "done", "started": started, "heartbeat": _now(),
                       "finished": _now(), "result": result, "updated": _iso()})
  except Exception as exc:  # noqa: BLE001
    logger.exception("job %s failed", job_id)
    STORE.put(job_id, {"status": "failed", "error_code": "remote_failed",
                       "error": f"{type(exc).__name__}: {exc}",
                       "started": started, "heartbeat": _now(), "updated": _iso()})


@app.get("/")
def root():
  return {"ok": True, "store": STORE.name, "database": DATABASE,
          "collection": COLLECTION, "degraded": STORE.degraded,
          "degraded_reason": STORE.reason,
          "revision": os.environ.get("K_REVISION", "local"),
          "default_app": DEFAULT_APP, "multi_tenant": True,
          "agents": [NETWORK_AGENT, GATEWAY_AGENT]}


@app.post("/resolveSpecialists")
def start(body: StartIn):
  """Start the pair and hand back a handle. Must return in well under a second."""
  job_id = uuid.uuid4().hex
  # WHOSE job this is. The one question a multi-tenant service has to be able to answer
  # afterwards, and the one nothing else records: falling back is indistinguishable from
  # being asked for the default, and a job that answered out of the wrong app looks
  # exactly like one that answered out of the right one. No account number here -- the
  # app redacts those out of its own logs, and this service should not put them back.
  logger.info("job %s: app %s%s", job_id, body.app or DEFAULT_APP,
              "" if body.app else " (fallback: the caller named no app)")
  STORE.put(job_id, {"status": "running", "started": _now(), "heartbeat": _now(),
                     "deadline": _now() + min(body.deadline_seconds, MAX_DEADLINE),
                     "updated": _iso()})
  threading.Thread(target=_work, args=(job_id, body), daemon=True).start()
  return {"jobId": job_id}


@app.get("/resolveSpecialists/{jobId}")
def status(jobId: str):  # noqa: N803
  # `degraded` rides every answer on purpose. The status wrapper ignores keys it did not
  # declare, so this costs the contract nothing -- and it puts "this job was never
  # durable" in the CES trace beside the poll that read it, which is the only place
  # anyone debugging a `remote_job_lost` will be looking.
  extra = {"degraded": STORE.degraded} if STORE.degraded else {}
  doc = STORE.get(jobId)
  if not doc:
    return {"status": "failed", "error_code": "remote_job_lost", **extra}
  if doc.get("status") == "done":
    return {"status": "done", "result": doc.get("result", {}), **extra}
  if doc.get("status") == "failed":
    return {"status": "failed", "error_code": doc.get("error_code", "remote_failed"),
            **extra}
  if _now() > float(doc.get("deadline", 0)):
    return {"status": "timeout", "error_code": "remote_timeout", **extra}
  # A durable store keeps the record when a revision retires mid-flight, but not the
  # worker thread. A stale heartbeat means nobody is working on this any more.
  if _now() - float(doc.get("heartbeat", 0)) > STALE_WORKER_SECONDS:
    return {"status": "failed", "error_code": "remote_job_lost", **extra}
  return {"status": "running", **extra}
