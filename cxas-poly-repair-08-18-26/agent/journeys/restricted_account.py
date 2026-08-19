"""The account is on hold, so nothing else we could say would help."""

import scripts
from journeys.common.rungs import rung


def tasks():
  """The account is on hold, or we cannot find it at all."""
  return [
      # P1 — account / billing standing beats everything, including an active outage
      # (confirmed by MVP_TC_21_Hierarchy_Account_vs_Outage).
      #
      # Not split, unlike the rungs below: `say_first` puts the diagnosis in the same
      # message as the `transfer_to_billing` call, and the model then invents a reason for
      # the suspension rather than saying it cannot see one.
      rung("HandleBillingBlock", "verdict_account_block",
           scripts.RESTRICTED_ACCOUNT, scripts.SAY_ACCOUNT_BLOCK, ends=True),

      rung("HandleAccountNotFound", "verdict_account_not_found",
           scripts.ACCOUNT_NOT_FOUND, scripts.SAY_ACCOUNT_NOT_FOUND, ends=True),
  ]
