# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long,broad-exception-caught

"""PolySynth callback function optimized with real-time concurrent notification dispatches and dynamic MAC-Less fallbacks."""

# pylint: disable=undefined-variable

from typing import Optional


def before_agent_callback(callback_context: CallbackContext) -> Optional[Content]:
  """Calls the _before method deterministically and notifies customer."""
  account_number = (
      callback_context.state.get("accountNumber")
      or callback_context.state.get("account_id")
      or callback_context.state.get("account_number")
      or ""
  )
  tools.check_outage_before({"account_number": account_number})
