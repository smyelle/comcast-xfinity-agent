#!/usr/bin/env python3
"""Coverage-gap analysis: chat intent taxonomy vs voice (golden head-intent) taxonomy.

The mapping below is a hand-authored semantic crosswalk (my judgment) from each CHAT
intent to the voice leaf(s) in head_intents.json it corresponds to. From it we compute,
exhaustively and both directions:
  * chat intents with NO adequate voice leaf   -> candidate ADD-to-voice
  * voice leaves referenced by NO chat intent  -> candidate ADD-to-chat
  * partial matches (mapped, but coarser/finer on one side)
  * things voice HANDLES via a different mechanism (not a head-intent) -> not a true gap

Run from flows-sdk/:  python tests/intent_gap_analysis.py
"""
import json
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VOICE = {l: c for c, spec in
         json.load(open(os.path.join(HERE, "head_intents.json")))["categories"].items()
         for l in spec["head_intents"]}

# marker constants for chat intents with no voice LEAF
GAP = "∅GAP"          # no voice capability at all
MECH = "∅MECH"        # voice handles it, but via a mechanism that isn't a head-intent
GEN = "∅GEN"          # too generic to be a real intent

# chat intent -> (voice leaves | marker, quality)  quality: full | partial
CHAT = {
    "accessibility.general": (["accessibility_general"], "full"),
    "account.app.issue": (GAP, "-"),          # Xfinity app itself: no voice leaf
    "account.app.manage": (GAP, "-"),
    "account.appointment.inquiry": (["appointment_general"], "full"),
    "account.appointment.issue": (["appointment_general"], "partial"),
    "account.appointment.manage": (["appointment_change_schedule"], "full"),
    "account.appointment.schedule": (["appointment_schedule_appointment"], "full"),
    "account.communication_preference.manage": (["billing_manage_ecobill"], "partial"),  # only paperless
    "account.email.issue": (["internet_email_inquiry"], "partial"),
    "account.email.manage": (["internet_email_inquiry"], "partial"),
    "account.equipment_fulfillment.inquiry": (["devices_activation", "account_equipment_return"], "full"),
    "account.equipment_fulfillment.issue": (["devices_modem_exchange", "plan_manage_equipment"], "partial"),
    "account.equipment_fulfillment.manage": (["plan_manage_equipment", "account_equipment_return"], "full"),
    "account.login.issue": (["login_and_password_general"], "full"),
    "account.login.manage": (["login_and_password_general", "login_and_password_networking"], "full"),
    "account.management.inquiry": (["account_update_info", "account_check_ticket_status"], "partial"),
    "account.management.manage": (["account_update_info", "account_update_address"], "full"),
    "account.new_customer": (["account_add_new_customer"], "full"),
    "account.security.manage": (GAP, "-"),    # general account security: only phone_security exists
    "account.security.report_issue": (["miscellaneous_inquire_sim_swap_fraud"], "partial"),
    "account.service.activate": (["devices_activation"], "full"),
    "account.service.cancel": (MECH, "-"),    # voice -> human/retention route, no head-intent
    "account.service.create": (["account_add_new_customer", "plan_add"], "full"),
    "account.service.downgrade": (["plan_downgrade"], "full"),
    "account.service.inquiry": (GEN, "-"),
    "account.service.pause": (["plan_manage_temporary_disconnect"], "full"),
    "account.service.transfer": (["account_transfer_service"], "full"),
    "account.ticket.inquiry": (["account_check_ticket_status"], "full"),
    "adjustment.inquiry": (["billing_credits_info"], "partial"),
    "billing.adjustment.inquiry": (["billing_credits_info"], "full"),
    "billing.adjustment.issue": (["billing_credits_info", "billing_lower_bill_request"], "partial"),
    "billing.adjustment.manage": (["billing_credits_info"], "partial"),
    "billing.bill.dispute": (["billing_complaint_fraud", "billing_discuss"], "partial"),
    "billing.bill.important_dates": (["billing_amount_future"], "full"),
    "billing.bill.inquiry": (["billing_discuss"], "full"),
    "billing.bill.lower": (["billing_lower_bill_request"], "full"),
    "billing.bill.view_amount_due": (["payments_balance_due"], "full"),
    "billing.bill.view_history": (GAP, "-"),  # no bill-history leaf (only billing_discuss, coarse)
    "billing.bill.view_statement": (["billing_discuss"], "partial"),
    "billing.payment.pay": (["payments_make_payment"], "full"),
    "billing.payment.payment_arrangement": (GAP, "-"),  # no payment-plan/arrangement leaf
    "billing.payment.report_issue": (["payments_troubleshoot_issues"], "full"),
    "billing.payment.report_technical_issue": (["payments_troubleshoot_issues"], "partial"),
    "billing.payment.scheduled_payment": (["payments_cancel_payment"], "partial"),
    "billing.setting.issue": (["billing_manage_ecobill"], "partial"),
    "billing.setting.manage": (["billing_manage_ecobill"], "partial"),
    "customer_support.request_live_agent": (MECH, "-"),  # voice -> L1 human route
    "disambiguation": (MECH, "-"),           # voice -> L1 disambiguation_main_menu + clarify gate
    "expected_intent": (GEN, "-"),           # test/meta artifact
    "home_security.issue": (["home_security_troubleshoot"], "full"),
    "home_security.manage": (["home_security_certificate"], "full"),
    "internet.equipment.issue": (["devices_modem_exchange", "internet_troubleshoot"], "full"),
    "internet.equipment.manage": (["internet_manage_wifi_xfi_pod", "devices_modem_exchange"], "full"),
    "internet.security.manage": (GAP, "-"),  # no internet-security leaf
    "internet.service.issue": (["internet_troubleshoot"], "full"),
    "internet.service.manage": (["internet_hotspot_inquiry", "internet_sound"], "partial"),
    "internet.setting.issue": (["login_and_password_networking", "internet_ip"], "partial"),
    "internet.setting.manage": (["internet_manage_wifi_xfi_pod", "internet_ip"], "full"),
    "outage.inquiry": (MECH, "-"),           # voice -> diagnostics sweep (check_outage), no head-intent
    "outage.report": (MECH, "-"),
    "phone.equipment.manage": (GAP, "-"),    # no phone-equipment leaf
    "phone.service.issue": (["voice_troubleshoot", "voice_troubleshoot_static_clicking_distortion"], "full"),
    "phone.service.manage": (["voice_blocking", "voice_callforward", "voice_voicemail_troubleshoot"], "full"),
    "retail_store.inquiry": (["customer_support_locate_service_center"], "full"),
    "security.inquiry": (GEN, "-"),
    "service.inquiry": (GEN, "-"),
    "tv.equipment.issue": (["account_order_remote", "tv_program_remote", "tv_remote_general"], "full"),
    "tv.equipment.manage": (["tv_remote_general", "tv_settings"], "full"),
    "tv.service.issue": (["tv_troubleshoot", "tv_troubleshoot_channels", "tv_troubleshoot_dvr"], "full"),
    "tv.service.manage": (["tv_channel_guide", "tv_on_demand_general", "tv_miscellaneous"], "full"),
    "tv.setting.issue": (["tv_settings"], "full"),
    "tv.setting.manage": (["tv_settings"], "full"),
}


def main() -> int:
  referenced = set()
  gap_voice, mech, generic, partial = [], [], [], []
  for chat, (target, q) in CHAT.items():
    if target == GAP:
      gap_voice.append(chat)
    elif target == MECH:
      mech.append(chat)
    elif target == GEN:
      generic.append(chat)
    else:
      for leaf in target:
        assert leaf in VOICE, f"BAD MAPPING: {chat} -> unknown leaf {leaf}"
        referenced.add(leaf)
      if q == "partial":
        partial.append((chat, target))

  chat_gaps = sorted(set(VOICE) - referenced)  # voice leaves no chat intent covers

  print(f"chat intents: {len(CHAT)} | voice leaves: {len(VOICE)} | "
        f"voice leaves referenced by chat: {len(referenced)}\n")

  print(f"== VOICE GAPS — chat has it, voice has NO leaf ({len(gap_voice)}) ==")
  for c in gap_voice:
    print(f"  {c}")
  print(f"\n== HANDLED BY OTHER MECHANISM (not a true gap) ({len(mech)}) ==")
  for c in mech:
    print(f"  {c}")
  print(f"\n== TOO GENERIC to be a real intent ({len(generic)}) ==")
  for c in generic:
    print(f"  {c}")

  print(f"\n== CHAT GAPS — voice leaf with NO chat intent ({len(chat_gaps)}) ==")
  bycat = {}
  for leaf in chat_gaps:
    bycat.setdefault(VOICE[leaf], []).append(leaf)
  for cat in sorted(bycat):
    print(f"  [{cat}]  " + ", ".join(bycat[cat]))

  print(f"\n== PARTIAL matches — mapped but coarser/finer ({len(partial)}) ==")
  for c, t in partial:
    print(f"  {c:44s} ~ {', '.join(t)}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
