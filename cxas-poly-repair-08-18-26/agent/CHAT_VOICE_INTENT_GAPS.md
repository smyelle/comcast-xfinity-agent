# Chat ↔ Voice intent coverage gap analysis

A coverage-gap comparison between the **chat** intent taxonomy and the **voice**
(golden head-intent) taxonomy this flows agent routes on.

## Where each taxonomy lives

- **Voice intents** — `flows-sdk/head_intents.json`: **16 L1 categories → 84 leaf
  head-intents** (snake_case). Derived from the GECX golden voice steering agent
  (`get_agent_instructions_for_selecting_head_intent` `INTENT_CONFIG`) by
  `flows-sdk/derive_head_intents.py`.
- **Chat intents** — the **72** dotted `domain.object.action` intents (e.g.
  `billing.payment.pay`, `tv.service.issue`). External to this repo; captured verbatim in
  the crosswalk in `flows-sdk/tests/intent_gap_analysis.py`.

## How this was computed

The chat→voice semantic crosswalk is a hand-authored mapping encoded as data in
`flows-sdk/tests/intent_gap_analysis.py`; the gap sets below are computed from it
exhaustively in both directions (so no leaf is missed by eyeballing). Regenerate:

```
cd flows-sdk && python tests/intent_gap_analysis.py
```

## Structural difference

The two taxonomies are organized on **different axes**:

| | **Chat** | **Voice** |
|---|---|---|
| Shape | flat, `domain.object.action` dotted | 2-level `category → leaf`, snake_case |
| Count | 72 intents | 16 categories, 84 leaves |
| Organizing axis | **verb/action** (`.inquiry` / `.issue` / `.manage` / `.pay` / `.dispute` / `.schedule` / `.cancel` …) | **topic** (action usually implicit: `billing_toohigh`, `tv_troubleshoot_dvr`) |
| Meta | `disambiguation`, `expected_intent` | disambiguation is an L1 route + a diagnostics clarify gate |

Headline: **voice is the richer taxonomy** (deeper long-tail — 31 concepts chat can't
name); **chat is broader on verbs** but has real product holes (Mobile, upgrade, refund,
autopay). Voice's genuine capability gaps are small (7).

---

## 1. What VOICE is missing — add-to-voice candidates

Only **7 true gaps** (chat names something voice has no leaf for):

| priority | chat intent | note |
|---|---|---|
| **High** | `account.app.issue`, `account.app.manage` | The **Xfinity app** itself — a top support topic, absent from voice |
| Med | `account.security.manage`, `internet.security.manage` | General account/internet security; voice only has `phone_security` (SIM-swap, Safe Connections) |
| Med | `billing.payment.payment_arrangement` | Payment plans/arrangements — real call driver, no leaf (collapses into `payments_make_payment`) |
| Low | `billing.bill.view_history` | Partly covered by the coarse `billing_discuss` |
| Low | `phone.equipment.manage` | Low volume for a repair agent |

### Handled by another mechanism — NOT a true gap (5)

Do **not** add head-intents blindly for these; voice already handles them:

- `outage.inquiry`, `outage.report` → resolved inside the **diagnostics sweep**
  (`check_outage` / `verdict_area_outage`), not as a steerable intent.
- `account.service.cancel`, `customer_support.request_live_agent` → the **L1 `human`**
  route (retention / live agent).
- `disambiguation` → the L1 catch-all `disambiguation_main_menu` + the diagnostics
  clarify gate.

> ⚠️ **Handoff-fidelity caveat:** if chat hands off `detected_intent = outage.*` or
> `account.service.cancel`, voice has **no matching category label** to receive it —
> functionally covered, but the label doesn't round-trip.

### Too generic to be a real intent (4)

`service.inquiry`, `account.service.inquiry`, `security.inquiry`, `expected_intent`.

---

## 2. What CHAT is missing — add-to-chat candidates

**31 voice leaves have no chat intent.** Grouped by voice category:

| priority | area | voice leaves chat lacks |
|---|---|---|
| 🚩 **High** | Xfinity Mobile | `mobile_general` — chat has **no mobile intent at all** (entire product line) |
| 🚩 **High** | Sales | `plan_upgrade` — chat has `account.service.downgrade` but **no upgrade** (likely an oversight) |
| **High** | Payments/Billing | `billing_refund_inquiry` (no refund), `payments_manage_autopay` (no autopay), `payments_method_update` (no payment-method), `account_renew` / `account_contract_renew` (no renewal), `billing_toohigh` (no "too high" complaint framing) |
| Med | Account admin (transfers) | `account_add_user`, `miscellaneous_customer_referral_program`, `miscellaneous_inquire_privacy_concerns`, `miscellaneous_track_order_status` |
| Med | Sales / programs | `internet_essentials_general`, `internet_essentials_application_status`, `plan_sports_channel_management` |
| Med | Streaming | `internet_streaming`, `tv_troubleshoot_netflix`, `vague_peacock`, `vague_xumo` |
| Low | Long-tail / niche | `business_general`, `california_lifeline`, `miscellaneous_inquire_broadband_nutrition_labels`, `voice_international_phone_coverage`, `account_handle_bereavement`, `account_order_gen`, `billing_amount_final`, `billing_corporate_address`, `billing_service_gurantee`, `appointment_store`, `appointment_cancel_service`, `miscellaneous_inquire_safe_connections_act` |

---

## 3. Handoff-fidelity mismatches — mapped, but different granularity (18)

Covered on both sides but at different resolution, so a handoff loses precision:

- Chat's `.issue` / `.manage` split often collapses to **one** voice leaf — e.g. both
  `account.email.issue` and `account.email.manage` → `internet_email_inquiry`.
- **All** of chat's `billing.setting.*` **and** `account.communication_preference.manage`
  → only `billing_manage_ecobill` (paperless) — a chat "billing setting" / "comms
  preference" handoff arrives in voice as just paperless.

Full partial list (chat intent ~ voice leaves):

| chat intent | voice leaf(s) |
|---|---|
| `account.appointment.issue` | `appointment_general` |
| `account.communication_preference.manage` | `billing_manage_ecobill` |
| `account.email.issue` | `internet_email_inquiry` |
| `account.email.manage` | `internet_email_inquiry` |
| `account.equipment_fulfillment.issue` | `devices_modem_exchange`, `plan_manage_equipment` |
| `account.management.inquiry` | `account_update_info`, `account_check_ticket_status` |
| `account.security.report_issue` | `miscellaneous_inquire_sim_swap_fraud` |
| `adjustment.inquiry` | `billing_credits_info` |
| `billing.adjustment.issue` | `billing_credits_info`, `billing_lower_bill_request` |
| `billing.adjustment.manage` | `billing_credits_info` |
| `billing.bill.dispute` | `billing_complaint_fraud`, `billing_discuss` |
| `billing.bill.view_statement` | `billing_discuss` |
| `billing.payment.report_technical_issue` | `payments_troubleshoot_issues` |
| `billing.payment.scheduled_payment` | `payments_cancel_payment` |
| `billing.setting.issue` | `billing_manage_ecobill` |
| `billing.setting.manage` | `billing_manage_ecobill` |
| `internet.service.manage` | `internet_hotspot_inquiry`, `internet_sound` |
| `internet.setting.issue` | `login_and_password_networking`, `internet_ip` |

---

## Summary

- 72 chat intents · 84 voice leaves · 53 voice leaves covered by chat.
- **Add-to-voice:** 7 true gaps (Xfinity app + general security dominate); 5 already
  handled via other mechanisms (label-round-trip caveat on outage/cancel); 4 too generic.
- **Add-to-chat:** 31 gaps — Xfinity Mobile (whole line), plan-upgrade, refund, autopay,
  renewal are the high-priority ones.
- **Handoff fidelity:** 18 partial matches where granularity differs (worst: billing
  settings + comms preferences collapse to paperless).
