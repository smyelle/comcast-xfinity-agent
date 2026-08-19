#!/usr/bin/env python3
"""Generate tests/head_intent_testset.json — a large, HONEST level-2 head-intent test set.

For each leaf of the 15 DEFER categories, hand-authored natural caller utterances (4-6,
trimmed for quality) that unambiguously map to THAT leaf and not a sibling. The generator:

  * validates every (leaf) label is a real leaf of its L1 category (from head_intents.json),
  * enforces DISJOINTNESS so the set measures the MODEL, not the deterministic backstop:
      - no utterance may substring-contain any HEAD_CUES phrase (head_intents.py), and
      - no utterance may already appear (exact, lowercased) in routing_corpus.json,
        head_intent_heldout.json, or routing_heldout.json,
    dropping (and reporting) any collision,
  * emits route_check corpus format.

Run:  python tests/gen_head_intent_testset.py
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SEED = {
    "accountNumber": "1234567890",
    "account_id": "1234567890",
    "mock_config_string": "account_status=active;gateway_status=online;network_status=healthy;outage_status=none",
}

# leaf -> L1 category, from the taxonomy (source of truth for labels).
_TAX = json.load(open(os.path.join(ROOT, "head_intents.json")))["categories"]
LEAF_TO_L1 = {leaf: cat for cat, spec in _TAX.items() for leaf in spec["head_intents"]}


def _head_cues():
  src = open(os.path.join(ROOT, "head_intents.py")).read()
  block = re.search(r"HEAD_CUES\s*=\s*\{.*?\n\}", src, re.S).group(0)
  phrases = []
  for _leaf, vals in re.findall(r'"([a-z_]+)":\s*\[([^\]]*)\]', block):
    for c in re.findall(r'"([^"]+)"', vals):
      phrases.append(c.lower())
  return phrases


def _existing_utts():
  utts = set()
  for f in ("routing_corpus.json", "head_intent_heldout.json", "routing_heldout.json"):
    p = os.path.join(HERE, f)
    if os.path.exists(p):
      for s in json.load(open(p))["scenarios"]:
        for u in s["user_utterances"]:
          utts.add(u.strip().lower())
  return utts


# ---------------------------------------------------------------------------
# Authored utterances: {leaf: [utterance, ...]}. Natural caller language, varied
# register/length, each clearly THAT leaf vs its siblings. Added category by category.
# ---------------------------------------------------------------------------
DATA: dict[str, list[str]] = {}

# ===== BILLING (14 leaves) =====
DATA.update({
    "billing_toohigh": [
        "I'm really upset, this month's bill is enormous",
        "there is no way my bill should be this much",
        "I'm shocked at how much I'm being billed, this is outrageous",
        "my bill is far higher than it has any right to be and I'm frustrated",
    ],
    "billing_lower_bill_request": [
        "what can you do to bring my monthly cost down",
        "I need you to help me get my bill reduced somehow",
        "can you apply any discounts to knock some money off what I pay",
        "is there a way to negotiate a better rate on my account",
    ],
    "billing_discuss": [
        "can you help me understand what all these charges are for",
        "I'd like to go over my statement line by line with someone",
        "what's this new fee that showed up on my latest statement",
        "walk me through why my charges are what they are",
    ],
    "billing_credits_info": [
        "I was promised a credit and I don't see it applied to my account",
        "where is the credit that was supposed to show up on my bill",
        "can you check on a credit that seems to be missing",
        "I'm asking about a bill credit I was expecting to receive",
    ],
    "billing_refund_inquiry": [
        "I overpaid and I want that money returned to me",
        "when will I see the money you owe me back on my card",
        "can I be reimbursed for the extra amount I was charged",
    ],
    "payments_troubleshoot_issues": [
        "I was charged twice for the same payment this month",
        "my payment went through but it's not showing as applied to my account",
        "my card got billed two separate times for one bill",
        "a payment I made didn't post correctly and I need it fixed",
    ],
    "payments_balance_due": [
        "how much do I currently owe on my account",
        "what's my outstanding balance right now",
        "can you tell me the total amount I still owe",
    ],
    "billing_amount_future": [
        "how much is my next bill going to be",
        "when is my next payment due",
        "what should I expect to pay next month",
        "what date is my bill due this cycle",
    ],
    "billing_amount_final": [
        "I cancelled my service, what will my last bill come to",
        "how much do I owe on my final statement after closing the account",
        "what's my closing balance now that I've disconnected",
    ],
    "account_update_info": [
        "I need to change the contact phone number on my account",
        "please update the email address you have on file for me",
        "I changed my last name and need it corrected on my account",
    ],
    "billing_corporate_address": [
        "what is the mailing address for Comcast's corporate office",
        "where do I send a letter to Comcast corporate headquarters",
        "I need the corporate office address to mail something",
    ],
    "billing_complaint_fraud": [
        "there are charges on my bill from someone who stole my identity",
        "I think I'm the victim of a billing scam on my account",
        "someone opened this account in my name fraudulently",
    ],
    "billing_manage_ecobill": [
        "I want to stop getting paper statements in the mail",
        "can you switch me to electronic statements only",
        "sign me up to receive my bill by email instead of by mail",
    ],
    "billing_service_gurantee": [
        "does Comcast have a money-back guarantee I can use",
        "I heard there's a satisfaction guarantee, can you explain it",
        "what's your service guarantee policy exactly",
    ],
})

# ===== PAYMENTS (4 leaves) =====
DATA.update({
    "payments_make_payment": [
        "I want to pay what I owe right now with my debit card",
        "let me take care of my balance today over the phone",
        "I'd like to send in a payment from my checking account",
    ],
    "payments_cancel_payment": [
        "I scheduled a payment for next week and need to call it off",
        "please stop the upcoming payment I set up from going through",
        "take back the payment I arranged for Friday",
    ],
    "payments_manage_autopay": [
        "I'd like my bill to be paid automatically every month",
        "can you turn on recurring monthly payments for me",
        "please turn off the automatic monthly charge on my account",
    ],
    "payments_method_update": [
        "I got a new credit card and need to put it on my account",
        "the card you have for me expired, I need to give you a different one",
        "swap out the bank account you have saved for another one",
    ],
})

# ===== SALES (10 leaves) =====
DATA.update({
    "account_transfer_service": [
        "I'm relocating to a new apartment and want to bring my service along",
        "we bought a house and need to take our internet over there",
        "I'm changing addresses, how do I carry my service to the new place",
    ],
    "account_add_new_customer": [
        "I'm not a customer yet and I'd like to sign up for internet",
        "how do I become a new Xfinity customer",
        "I want to get service set up at my place for the very first time",
    ],
    "account_contract_renew": [
        "my contract is about to expire and I want to lock in a new term",
        "I'd like to sign a new agreement to keep my rate",
        "can I renew my two-year term agreement",
    ],
    "account_renew": [
        "my subscription is up for renewal and I want to continue it",
        "keep my existing package active for another year",
    ],
    "plan_add": [
        "I'd like to add HBO to my package",
        "can I include a premium movie channel on my current plan",
        "I want to add a home phone line to my services",
    ],
    "plan_upgrade": [
        "I want to move up to a faster internet speed",
        "can you put me on a higher tier package",
        "get me onto your top level of service",
    ],
    "plan_downgrade": [
        "I want to drop down to a smaller package",
        "can I reduce my services to a lower tier",
        "scale me back to something with less",
    ],
    "plan_sports_channel_management": [
        "I want to add the sports package so I can watch the games",
        "can I get the NFL channels added to my lineup",
        "please remove the sports tier from my channels",
    ],
    "internet_essentials_general": [
        "what is the Internet Essentials program all about",
        "do I qualify for the low-income internet program",
        "tell me about eligibility for Internet Essentials",
    ],
    "internet_essentials_application_status": [
        "I applied for Internet Essentials, where does my application stand",
        "did my Internet Essentials sign-up get approved yet",
        "what's the status of the Internet Essentials application I submitted",
    ],
})


# ===== TECHNICAL_TELEVISION (10 leaves) =====
DATA.update({
    "tv_troubleshoot": [
        "my cable box isn't showing any picture at all",
        "the screen just says no signal on my TV",
        "my television picture keeps breaking up and pixelating",
    ],
    "tv_troubleshoot_channels": [
        "a few of the networks I used to get aren't coming in anymore",
        "my channel lineup dropped some of the stations I normally watch",
        "I lost access to a couple of channels that used to work",
    ],
    "tv_troubleshoot_dvr": [
        "my DVR stopped saving the shows I set to record",
        "the recorded programs won't start when I press play",
        "my DVR box won't record the series I scheduled",
    ],
    "tv_channel_guide": [
        "what channel is ESPN on for me",
        "how do I pull up the program listings on my TV",
        "where do I find the schedule of what's on tonight",
    ],
    "tv_on_demand_general": [
        "how do I rent a movie through On Demand",
        "I want to buy a show from the On Demand store",
        "how do I get to the On Demand library on my box",
    ],
    "tv_settings": [
        "how do I switch my TV audio to Spanish",
        "I want to change the menu language on my cable box",
        "help me adjust the display settings on my box",
    ],
    "tv_remote_general": [
        "my remote won't control the TV anymore",
        "the buttons on my cable remote have stopped responding",
        "my remote just isn't doing anything when I press it",
    ],
    "tv_program_remote": [
        "how do I pair my remote to the cable box",
        "I need to reprogram my remote to work with the TV",
        "help me set up my new remote with my equipment",
    ],
    "account_order_remote": [
        "I lost my remote and need a replacement sent to me",
        "can you ship me a new remote control",
        "my remote is broken, I need a new one mailed out",
    ],
    "tv_miscellaneous": [
        "I have a TiVo box and need some help with it",
        "the caller ID isn't popping up on my TV screen anymore",
        "I need help with my TiVo recording setup",
    ],
})

# ===== TECHNICAL_PHONE (5 leaves) =====
DATA.update({
    "voice_troubleshoot": [
        "my landline isn't working at all right now",
        "I can't make any calls from my home phone",
        "my fax machine won't connect through the phone line",
    ],
    "voice_troubleshoot_static_clicking_distortion": [
        "there's a lot of crackling whenever I'm on a call",
        "I hear an echo every time I talk on my home phone",
        "there's a constant humming noise in the background of my calls",
    ],
    "voice_voicemail_troubleshoot": [
        "I can't get into my voicemail messages",
        "my voicemail greeting won't record properly",
        "how do I listen to the messages people left me",
    ],
    "voice_blocking": [
        "I keep getting robocalls and want them blocked",
        "how do I stop spam calls to my home phone",
        "can you block a specific number that keeps calling me",
    ],
    "voice_callforward": [
        "I want to forward my home phone calls to my cell",
        "how do I set up call forwarding on my line",
        "please turn off the call forwarding I have on",
    ],
})

# ===== XFINITY_MOBILE (1 leaf) =====
DATA.update({
    "mobile_general": [
        "I want to add a new line to my Xfinity Mobile",
        "can I get a new phone through Xfinity Mobile",
        "I'd like to upgrade my mobile device and plan",
    ],
})

# ===== TECHNICAL_XFINITY_HOME (2 leaves) =====
DATA.update({
    "home_security_troubleshoot": [
        "my home security alarm keeps false-triggering",
        "the window sensor on my Xfinity Home system won't connect",
        "my security keypad isn't responding when I enter my code",
    ],
    "home_security_certificate": [
        "I need a monitoring certificate from my alarm for my insurance",
        "can you send me proof of my home security monitoring",
        "my insurance company wants a certificate for my alarm system",
    ],
})


# ===== APPOINTMENTS (5 leaves) =====
DATA.update({
    "appointment_schedule_appointment": [
        "I need to set up a time for a tech to come install my internet",
        "can I arrange for someone to come out and set up my service",
        "I'd like to get an installation date on the calendar",
    ],
    "appointment_change_schedule": [
        "something came up, I need a different day for my tech visit",
        "can we move my scheduled service to next week instead",
        "I need to switch my visit to a later date",
    ],
    "appointment_cancel_service": [
        "I don't need the technician to come anymore, please call it off",
        "cancel the service visit I had set up",
        "I want to call off my upcoming tech visit",
    ],
    "appointment_general": [
        "when is the technician supposed to arrive at my house",
        "what time is my visit scheduled for",
        "is my service appointment still on for today",
    ],
    "appointment_store": [
        "I want to book a time to visit the Xfinity store",
        "can I set up an in-store appointment to get help",
        "I'd like to reserve a spot at the store",
    ],
})

# ===== ACTIVATIONS (1 leaf) =====
DATA.update({
    "devices_activation": [
        "I need to activate my new modem",
        "how do I get my newly received cable box working",
        "turn on the equipment I just got in the mail",
    ],
})

# ===== SERVICE_CENTER (2 leaves) =====
DATA.update({
    "account_equipment_return": [
        "I want to bring my old modem back to a store",
        "how do I hand in my equipment at a location",
        "I need to give back my cable boxes in person",
    ],
    "customer_support_locate_service_center": [
        "where is the closest Xfinity store to me",
        "I need to find a service center in my area",
        "is there a Comcast retail location nearby",
    ],
})

# ===== ACCESSIBILITY (1 leaf) =====
DATA.update({
    "accessibility_general": [
        "I'm blind and need help using the audio description feature",
        "do you offer TTY support for hard-of-hearing customers",
        "I need accessibility options for a visually impaired person",
    ],
})

# ===== EQUIPMENT_SWAP (1 leaf) =====
DATA.update({
    "plan_manage_equipment": [
        "I want to exchange my modem for a different model",
        "can I swap my old cable box for a newer one",
        "I'd like to replace my gateway with an updated device",
    ],
})

# ===== PHONE_SECURITY (2 leaves) =====
DATA.update({
    "miscellaneous_inquire_sim_swap_fraud": [
        "somebody did a SIM swap on my mobile line",
        "my number got ported out to another company without my permission",
        "I think a scammer took over my phone number",
    ],
    "miscellaneous_inquire_safe_connections_act": [
        "I need to separate my line from my abuser under the Safe Connections Act",
        "I'm a domestic violence survivor and need my own line off a shared account",
        "how do I use the Safe Connections Act to get my line separated",
    ],
})

# ===== TRANSFERS (4 leaves) =====
DATA.update({
    "miscellaneous_track_order_status": [
        "I placed an order and want to know where my shipment is",
        "can you tell me if my equipment order has shipped yet",
        "where is the package from the order I made last week",
    ],
    "account_add_user": [
        "I want to add my spouse as a user on my account",
        "can you give my roommate access to manage the account",
        "add another authorized person who can call in about our account",
    ],
    "miscellaneous_customer_referral_program": [
        "how does the refer-a-friend program work",
        "I referred someone, when do I get my referral credit",
        "do you have a referral code I can share with friends",
    ],
    "miscellaneous_inquire_privacy_concerns": [
        "I want to opt out of you selling my personal data",
        "how do I stop Comcast from collecting my information",
        "what are my privacy rights regarding my account data",
    ],
})

# ===== DISAMBIGUATION_MAIN_MENU (14 leaves) =====
DATA.update({
    "plan_manage_temporary_disconnect": [
        "I'll be away for a few months and want to pause my service temporarily",
        "can I put my internet on a temporary hold and turn it back on later",
        "I want to freeze my account while I'm out of the country, not cancel it",
    ],
    "account_handle_bereavement": [
        "my father passed away and I need to handle his Comcast account",
        "my spouse died, how do I close out their service",
        "I'm dealing with a deceased family member's account",
    ],
    "account_order_gen": [
        "I'd like to place a brand new order for equipment",
        "I want to put in an order for a new service",
        "can I place an order over the phone",
    ],
    "account_check_ticket_status": [
        "what's the status of the trouble ticket I opened",
        "can you check on the service request I filed",
        "is there any update on my open ticket",
    ],
    "account_update_address": [
        "I need to change the mailing address on my account",
        "please update the billing address you have for me",
        "my address on file is wrong, can you correct it",
    ],
    "tv_troubleshoot_netflix": [
        "the Netflix app on my X1 box won't load",
        "I can't log into Netflix through my Xfinity box",
        "Netflix keeps freezing when I watch it on my cable box",
    ],
    "vague_peacock": [
        "I need help with my Peacock app",
        "how do I get Peacock added to my account",
        "Peacock won't stream on my TV",
    ],
    "vague_xumo": [
        "I'm having trouble with my Xumo device",
        "how do I set up my Xumo stream box",
        "my Xumo isn't working right",
    ],
    "business_general": [
        "I have a Comcast Business account and need support",
        "this call is about my business internet service",
        "I need help with my company's Comcast Business line",
    ],
    "internet_manage_wifi_xfi_pod": [
        "I need help setting up my xFi Pods",
        "my WiFi doesn't reach the back of the house, can pods help",
        "how do the mesh WiFi pods extend my coverage",
    ],
    "internet_ip": [
        "I need a static IP address for my setup",
        "how do I request a dedicated IP",
        "my IP address keeps changing and I need it to stay fixed",
    ],
    "voice_international_phone_coverage": [
        "I want to add international calling to my home phone",
        "can I get the unlimited international calling pass",
        "please remove the international calling add-on from my line",
    ],
    "california_lifeline": [
        "do I qualify for California Lifeline",
        "how do I enroll in the California Lifeline program",
        "tell me about the California Lifeline benefits",
    ],
    "miscellaneous_inquire_broadband_nutrition_labels": [
        "where can I find the broadband nutrition label for my plan",
        "I want to see the broadband facts label for my internet",
        "tell me about the internet plan transparency label",
    ],
})


def build():
  cues = _head_cues()
  existing = _existing_utts()
  scenarios = []
  dropped = []
  seen = set()
  per_leaf = {}
  for leaf, utts in DATA.items():
    if leaf not in LEAF_TO_L1:
      raise SystemExit(f"UNKNOWN LEAF (not in taxonomy): {leaf}")
    l1 = LEAF_TO_L1[leaf]
    kept = 0
    for utt in utts:
      low = utt.strip().lower()
      hit = next((c for c in cues if c in low), None)
      if hit:
        dropped.append((leaf, utt, f"cue-substring:{hit!r}"))
        continue
      if low in existing:
        dropped.append((leaf, utt, "in-existing-corpus"))
        continue
      if low in seen:
        dropped.append((leaf, utt, "duplicate"))
        continue
      seen.add(low)
      kept += 1
      scenarios.append({
          "id": f"ts_{leaf}_{kept}",
          "kind": "test",
          "seeded_variables": dict(SEED),
          "user_utterances": [utt],
          "expected_flow": l1,
          "acceptable_flows": [],
          "expected_head_intent": leaf,
          "tag": "test",
      })
    per_leaf[leaf] = kept

  out = {
      "source": "hand-authored level-2 head-intent test set (disjoint from cues + prior evals)",
      "scenarios": scenarios,
  }
  with open(os.path.join(HERE, "head_intent_testset.json"), "w") as f:
    json.dump(out, f, indent=2)
    f.write("\n")

  cats = {}
  for leaf, n in per_leaf.items():
    cats.setdefault(LEAF_TO_L1[leaf], []).append((leaf, n))
  print(f"WROTE {len(scenarios)} scenarios across {len(per_leaf)} leaves / {len(cats)} categories")
  for cat in sorted(cats):
    leaves = cats[cat]
    print(f"  {cat:26s} {sum(n for _, n in leaves):3d}  ({len(leaves)} leaves)")
  if dropped:
    print(f"\nDROPPED {len(dropped)} utterance(s) (disjointness / dedupe):")
    for leaf, utt, why in dropped:
      print(f"  [{why}] {leaf}: {utt!r}")
  else:
    print("\nno drops — all utterances pass disjointness")
  # leaves with < 2 kept: flag (thin coverage)
  thin = [leaf for leaf, n in per_leaf.items() if n < 2]
  if thin:
    print(f"\nTHIN leaves (<2 kept): {thin}")


if __name__ == "__main__":
  build()
