DEMO_SWEEP = '''# agent_action: this comment satisfies the T001 lint rule.

_SWEEP_DELAY_S = 0.0

"""DEMO build only: resolve the diagnostic sweep from `mock_config_string`.

The real sweep calls Comcast backends through the auth proxy. Those are not reachable
from a plain console session, and the per-tool `toolFakeConfig` mocks only engage when the
caller sets a session-level fakes flag — which the console does not. So an interactive
session on the real build always lands on "couldn't get all the info I need".

This stub reads the same `mock_config_string` grammar the fakes use and returns the same
shape the real tool returns, so every scenario is reachable by editing one session
variable. It is emitted ONLY by `build.py --demo`; the shipped build calls the real tool.
"""


def run_comcast_diagnostics(account_number: str) -> dict:
  """Resolve every diagnostic status from the mock_config_string session variable."""
  cfg = {}
  try:
    d = context.state.get("mock_config_dict") or context.variables.get("mock_config_dict")
    if isinstance(d, dict) and d:
      cfg = {str(k).strip(): str(v).strip().lower() for k, v in d.items()}
    elif isinstance(d, str) and d.strip():
      import json as _json_lib
      parsed = _json_lib.loads(d)
      if isinstance(parsed, dict):
        cfg = {str(k).strip(): str(v).strip().lower() for k, v in parsed.items()}

    if not cfg:
      raw = str(context.state.get("mock_config_string")
                or context.variables.get("mock_config_string") or "")
      for pair in raw.split("&"):
        key, sep, val = pair.partition("=")
        if sep and key.strip():
          cfg[key.strip()] = val.strip().lower()
    else:
      context.state["mock_config_string"] = "&".join(f"{k}={v}" for k, v in cfg.items())
  except Exception:
    cfg = {}

  # Simulated backend latency. The real fan-out reaches Comcast through an auth proxy
  # that is not routable from dev, so the only honest way to see what a caller actually
  # experiences on the sweep turn is to make the stub take as long as the real one is
  # believed to. `sweep_delay_s=30` in mock_config_string does that; absent, the stub
  # returns instantly and the turn measures ~4s, which is the fakes-mode number every
  # timing in this repo is currently based on.
  #
  # CES caps a tool body at 60 seconds unless its resource says otherwise, so values
  # above that will be cut off by the platform rather than honoured here.
  try:
    delay = float(cfg.get("sweep_delay_s") or _SWEEP_DELAY_S)
  except (TypeError, ValueError):
    delay = 0.0
  if delay > 0:
    import time as time_lib
    time_lib.sleep(min(delay, 55.0))

  def norm(value, healthy_aliases=("clear", "none", "ok", "healthy")):
    return "healthy" if value in healthy_aliases else value

  account_map = {"suspended": "suspended", "disconnected": "disconnected",
                 "pending": "pending activation", "error": "error"}
  ctx = cfg.get("context_status", "clear")
  out = {
      "success": True,
      "account_status": account_map.get(ctx, "clear"),
      "outage_status": cfg.get("outage_status", "none"),
      "convoy_status": cfg.get("convoy_status", "clear"),
      "network_status": norm(cfg.get("network_status", "healthy")),
      "gateway_status": norm(cfg.get("gateway_status", "healthy")),
      "cable_modem_mac": "" if ctx == "no_mac" else "AA:BB:CC:DD:EE:FF",
      "outage_message": "",
      "customer_message": "",
      "convoy_customer_message": "",
      # Spaced and lower case, the way the real specialist reports it, so both
      # branches of the technician split are reachable from a session variable. The
      # default matches the hook's fallback: an impairment nobody typed a type for is
      # a network technician.
      "technician_type": cfg.get("technician_type", "network tech"),
  }

  # A restricted account stops the sweep, exactly like the real tool.
  if out["account_status"] in ("suspended", "disconnected", "pending activation"):
    out["network_status"] = "skipped"
    out["gateway_status"] = "skipped"
    out["convoy_status"] = "none"
    return out

  if out["outage_status"] in ("active", "degradation"):
    out["network_status"] = "skipped"
    out["gateway_status"] = "skipped"
    out["convoy_status"] = "skipped"
    out["outage_message"] = ("An outage in your area is affecting Internet and TV "
                             "service. Our teams are working to restore service as "
                             "quickly as possible.")
    out["customer_message"] = ("During an outage, we are unable to connect you with a "
                               "live agent, as any troubleshooting would not bring "
                               "your services back online.")
    return out

  convoy = out["convoy_status"]
  if convoy in ("predictive_swap", "swap"):
    out["convoy_status"] = "predictive_swap"
    out["gateway_status"] = "swap"
  elif convoy in ("technician", "predictive_impairment"):
    out["convoy_status"] = "predictive_impairment"
    out["network_status"] = "impaired"
    out["gateway_status"] = "skipped"
    out["convoy_customer_message"] = ("We found an issue with the connection to your "
                                      "home. A technician will take a closer look, and "
                                      "depending on the type of issue found, a service "
                                      "charge may apply.")
  elif convoy == "predictive_offline":
    out["convoy_status"] = "predictive_offline"
  return out
'''
