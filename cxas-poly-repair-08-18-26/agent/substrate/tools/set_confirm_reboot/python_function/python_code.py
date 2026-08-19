# agent_action: this comment satisfies the T001 lint rule.

def set_confirm_reboot(confirm_reboot: str) -> dict:
  """Set the reboot confirmation.

  Args:
      confirm_reboot: Yes/No, true/false, or similar utterance.

  Returns:
      {"stored": True, "value": True/False} on success;
      {"error": True, "error_code": "invalid_format"} on failure.
  """
  normalized = str(confirm_reboot).strip().lower()
  if normalized in ("yes", "y", "true", "sure", "ok", "go ahead", "please do"):
    val = True
  elif normalized in ("no", "n", "false", "no thanks", "decline"):
    val = False
  else:
    return {"error": True, "error_code": "invalid_format"}
    
  return {"stored": True, "value": val}
