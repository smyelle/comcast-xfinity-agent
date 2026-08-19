# Multi-language support in `flows`

Author multilingual CXAS/CES agents from a few `flows.App` fields. The feature covers
three shapes:

- **single language** — just declare the app's locales (`languageSettings`).
- **caller-initiated switch** (`explicit` / `auto`) — the caller can switch language
  mid-call (e.g. "can you speak Spanish?") and the agent continues in it.
- **turn-1 menu + hard lock** (`select`) — the caller picks a language at the start
  (say it, or press 9) and the conversation is locked to it for the rest of the call.

It is an **opt-in authoring feature**: it changes nothing until you set the fields
below, and it does **not** modify the slot-filling engine/framework (see
[Safety & backward compatibility](#safety--backward-compatibility)).

---

## Quick start

### New app — caller can switch on request (`explicit`)

```python
import flows

app = flows.App(
    root_flow=my_flow,
    app_display_name="My Agent",
    languages=["en-US", "es-US", "fr-CA"],   # first is the default unless default_language is set
    default_language="en-US",
    language_switching="explicit",           # caller says "speak Spanish" -> switches + stays
)
```

Emits: `app.json` `languageSettings`, an `update_language` tool, a `<language_detection>`
instruction block (conservative guardrails so it only switches on an explicit request),
and an `active_language` state variable.

### New app — turn-1 menu + hard lock (`select`)

```python
app = flows.App(
    root_flow=my_flow,
    app_display_name="My Agent",
    languages=["en-US", "es-US", "fr-CA"],
    default_language="en-US",
    language_switching="select",
    language_prompt=(                        # the bilingual opener (voice copy: no dashes)
        "Welcome to Acme. Para espanol, marque nueve, or say Spanish. "
        "Otherwise, I'll continue in English. How can I help with your order?"
    ),
)
```

`flows` prepends a **language-menu slot** as the first user slot (with `dtmf_map` so
pressing **9** selects the second language), then locks the chosen language for the whole
call. Pick `select` when you want a phone-IVR-style "choose your language" opener where the
choice must not drift.

---

## Modes reference

| `language_switching` | Caller can switch mid-call? | What is emitted |
|---|---|---|
| `"off"` (default) | n/a | Nothing. If `languages` is set, only `app.json` `languageSettings`. |
| `"explicit"` | Yes, on an explicit request | `update_language` tool + `<language_detection>` block (explicit-only) + `active_language` var |
| `"auto"` | Yes, on explicit request OR a full sentence in another language | same as explicit, with a relaxed switch threshold (less reliable; stress-test it) |
| `"select"` | No — locked after the turn-1 choice | menu slot (`dtmf_map`) + `<language_menu>` + `<language_lock>` + a per-turn lock hook + `active_language` var |

`explicit` is the recommended default for caller-initiated switching: the live model does
**not** reliably auto-detect a language switch, so `auto` is opt-in and should be sim-tested.

---

## Adding it to an EXISTING app

1. Add the three fields to your `flows.App(...)` (see Quick start). Nothing else in your
   flow needs to change to get started.
2. **Localize deterministic text at the tool boundary.** The lock/switch governs
   *model-generated* text. Deterministic strings the engine speaks verbatim —
   `announce` text, `then_say`, and any string a tool returns — stay in the language you
   authored them in. Return tool output in the active language (the
   "translate-around-the-tool" pattern):

   ```python
   @flows.tool(flow="order_status")
   def lookup_order_status(order_number: str, language_choice: str = "") -> OrderStatus:
       low = str(language_choice).strip().lower()
       if low.startswith(("span", "es")):
           return OrderStatus(status_message=f"El pedido {order_number} esta en camino.")
       return OrderStatus(status_message=f"Order {order_number} is out for delivery.")
   ```

   Pass the language into the task so the tool receives it:

   ```python
   flow.task("lookup", "lookup_order_status",
             ["order_number", "language_choice"],   # language_choice is the select-mode menu slot
             "status_message", out_key="status_message", requires=["order_number"])
   ```

   For `explicit`/`auto`, read `context.variables.get("active_language")` inside the tool
   instead (the menu slot only exists in `select`).
3. Re-emit and deploy as usual. Your existing flow, tools, and behavior are unchanged.

Adding language support is additive — see the compatibility guarantees below.

---

## How it works

### `explicit` / `auto`
- `app.json.languageSettings = {defaultLanguageCode, supportedLanguageCodes[], enableMultilingualSupport}`.
- `update_language(new_language)` — a tool the model calls before its first reply in the new
  language; it records the choice and returns an `agent_action` to continue in it.
- `<language_detection>` — an instruction block appended to the agent instruction with
  contextual-inertia / length / cognate / noisy-audio guardrails so isolated foreign words
  (a stray "gracias") don't cause a spurious switch.

### `select` (menu + lock)
- A **menu slot** is prepended as the first user slot: `ask` = your `language_prompt`,
  `dtmf_map={"9": <second language>}` (deterministic keypad selection; the engine also
  accepts "marque nueve" / "press 9").
- `<language_menu>` guidance: record a choice only when the caller clearly picks one, and
  re-offer the menu once if the opener is ambiguous.
- `<language_lock>` + a per-turn **`before_model` hook** that reads the chosen language from
  the slot and injects a concrete `respond ONLY in <language>` directive into the system
  instruction every turn. Enforcing via the system instruction (the same mechanism the
  framework uses) is reliable and **guardrail-safe** — the model generates normally, so
  guardrails apply as usual (no callback text↔tool rewriting).
- A light **`after_model` nudge**: if a reply drifts out of the locked language (cheap
  in-sandbox heuristic), it re-prompts the model to regenerate (via `try_again`, capped,
  fail-open). ~0 token cost on a compliant turn.
- Asking to change language while locked is handled: the agent declines in the locked
  language and keeps going — it never ends, cancels, or transfers because of a language
  request.

---

## Interaction with existing features

- **`route_cues` / `intent_first` (multi-agent routing).** `route_cues` are English keyword
  backstops for lead-in-free sibling switching; `intent_first` (on by default via
  `robust_switching`) routes by **meaning** using a model classifier, which is
  language-agnostic. So a Spanish-speaking caller is routed by the classifier; the English
  `route_cues` simply won't match non-English phrasings (graceful degradation, not a break).
  For multilingual multi-agent apps, rely on `intent_first` and optionally add localized
  `aliases`.
- **DTMF (`dtmf_map`).** Used for the `select` menu (press 9). Deterministic; fills the first
  user slot, not the gate.
- **Read-back (`readback=True`), `no_input`, validation, cancel/escalate.** Unchanged and
  fully compatible. Read-back is recommended for numeric slots so mis-heard digits are
  confirmed before use.
- **Multi-agent.** `explicit` / `auto` work with `host` + `agents`. `select` (menu + lock) is
  currently **single-agent only** (a clear error is raised otherwise); multi-agent select is
  a planned follow-up.

---

## Safety & backward compatibility

- **Authoring-layer only.** The feature lives entirely in `flows.authoring` (+ a small
  `deploy/prep` change). It does **not** touch the slot-filling engine, the four framework
  callbacks, or the blessed control tools — `flows.version()` (the blessed bundle) is
  unchanged, and framework drift verification still passes. Ship it as a normal `flows`
  wheel version; it is **not** an SF-framework change.
- **Opt-in, zero default blast radius.** With `language_switching="off"` (the default) and
  no `languages`, emit output is byte-identical to before (guarded by
  `tests/test_language.py::test_no_language_app_unchanged`). Existing apps are unaffected
  until they opt in.
- **The one shared-path change:** `deploy/prep.merge_live_settings` now keeps the emitted
  `languageSettings` instead of overwriting it from the live target — but only when the
  built app actually declares `languageSettings`. Apps that don't author it are unchanged.
- The emitted language artifacts (the `update_language` tool, the menu slot, the `_03`
  language hooks) are per-app and additive; they never alter the framework's `_01`
  callbacks.

---

## Deploy notes

- **Model:** `gemini-3.1-flash-live` is the multilingual live/voice model. In some
  environments the scrapi bidi harness has flaky end-of-turn with it; `gemini-3.5-flash`
  drives bidi reliably and handles the switching logic. Set `App.model` accordingly.
- **Audio evals** need `audioProcessingConfig.inactivityTimeout` on the app (e.g. `8s`) so
  CES endpoints the caller's audio turn; otherwise a bidi run can hang.
- `languageSettings` is now authored by `flows`, so an `--overwrite` deploy applies your
  languages (the prep change above stops the live target's languages from winning).

---

## Testing

Drive the deployed app with `cxas-scrapi` (see `scratch/lang_demo_evals/`):

- **Text** (`Sessions.run`, assert on `tool_calls` + a language heuristic): explicit switch
  to es/fr; `select` menu choice; DTMF "9"; the hard lock holding when the caller later
  speaks another language; ambiguous opener re-offers; read-back confirms the number.
- **Audio** (`Sessions.async_bidi_run_session` + `AudioTransformer` TTS, LINEAR16): the
  spoken switch/selection path over the real bidi websocket.
- Pull traces with `cxas trace get --with-logs` and root-cause from engine tags.

Reuse the framework's own `flows.authoring.language._response_language_ok(text, lang)`
heuristic in evals so tests judge "is this Spanish?" exactly as the deployed nudge does.
