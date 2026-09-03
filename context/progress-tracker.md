# Progress Tracker — AI Revenue Recovery Agent

> Update this file after every completed task. This is the source of truth
> for what's actually built vs. planned. Don't let it go stale.

## Status: Phases 0-8 COMPLETE (commits still pending)
> One `Definition of done` item is deliberately left open because it needs a human
> judgement, and one stretch goal was declined with reasons. Both below.

## Current phase
**783 tests passing** (0 skipped with the container up), ruff clean. The build is
functionally complete end to end: a signed webhook runs all four stages inline, a
provider webhook can confirm the money came back, and there is a metrics layer and
dashboard over the results. Written up in **`RESULTS.md`**.

**Measured on a 76-event in-hours run through the signed endpoint:**

| metric | result |
|---|---|
| events processed | 76, all detected, 0 failures |
| audit coverage | **76/76 with all four stages (100%)** |
| stopping-rule violations | **0**, re-derived independently of the enforcing code |
| decision latency | mean 6.0s, p95 7.4s, max 12.1s, **0 of 76 over the 60s target** |
| classifier unavailable | **0**, all 8 root causes classified |
| amount at risk | INR 223,975 |
| amount recovered | INR 0 — mechanism built and tested, no link paid yet |
| customers messaged | **1 real WhatsApp delivered**, 8 more prepared and withheld by the opt-in allowlist |

Two things to carry into a demo rather than discover during one:

1. **Recovered is zero because nobody has paid a link, not because the metric is
   broken.** Outcome confirmation is built, wired and covered by 40 tests. The agent
   generated a live test-mode link (`https://rzp.io/rzp/u7hNigG`, INR 499). Paying it
   with `payment_link.paid` subscribed and ngrok pointed at `app.tunnel` turns the
   headline number into something genuinely earned.
2. **Only 1 of 76 customers was messaged**, and the reasons are all correct: 32
   stopped on age (the fixture spreads failures over 14 days against a 7-day hard
   stop), 35 correctly needed no message, and 8 were fully prepared — real hosted
   payment links created — then withheld because the Twilio sandbox only delivers to
   opted-in numbers.

Remaining: the last `Definition of done` item needs a person who has not seen the
code to read one event's trail on `/dashboard` and say whether it lands in under 30
seconds. And commits have still never been authorised.

## Phase checklist

### Phase 0 — Setup
- [x] Repo scaffolded (backend framework chosen and installed)
      FastAPI + Python 3.12.6 venv at `backend/.venv`. One module per pipeline
      stage per `code-standards.md`; all four raise `NotImplementedError`.
      `schemas.py` locks the taxonomy. 25 tests + ruff passing. App boots and
      serves `/health` + `/readiness` (verified against a running server).
- [ ] Razorpay test-mode account created, API keys in `.env`
      **MOSTLY DONE.** Owner supplied `.env` with Razorpay key id + secret
      (passed the `rzp_test_` validator, so genuinely test mode), Gemini key,
      and Twilio credentials. Verified via `credential_report()`, which returns
      booleans and key names only — no secret value was read or echoed.
      Still outstanding: `RAZORPAY_WEBHOOK_SECRET` (owner invents this value
      themselves; Razorpay does not generate it).
- [x] `.env.example` created
      Documents every `Settings` field with clearly-marked non-functional
      placeholders. A test asserts every field is documented.
- [x] Postgres runs as a container (`docker-compose.yml`, `postgres:17-alpine`)
      Loopback-bound, named volume, healthcheck for `--wait`. **Started and
      verified live**: connected via the derived URL, confirmed PostgreSQL 17.11,
      database `revenue_recovery`, role `recovery`, and a write/read-back probe.
      Published on host port **55432**, not 5432 — see decisions log.
- [ ] Six context files reviewed and committed to repo root
      Reviewed: yes, all six (session-start checklist completed). Committed:
      **not yet** — awaiting explicit go-ahead to create commits. `git init`
      done and `.gitignore` verified (`.env` and `.venv/` excluded).
      Note: the docs live in `context/`, not the repo root. Left in place
      rather than moved; confirm if the root location was actually intended.

### Phase 1 — Data simulation layer
- [x] Synthetic event generator built (50–100 events)
      `app/simulation/`: `decline_catalog.py` (29 real Razorpay failure
      scenarios), `generator.py`, `signing.py`, `fixtures.py`, CLI via
      `python -m app.simulation`. 50 tests.
- [x] Events span at least 5 decline-code categories
      **6 of 8** root causes represented: `insufficient_funds`, `sca_abandoned`,
      `network_error`, `bank_risk_block`, `card_expired`, `unknown`. The two
      absent ones are checkout-only and cannot come from a `payment.failed`
      payload — see Phase 1b below. A test asserts the gap is exactly those two.
- [~] ~~Stripe~~ **Razorpay** test-mode triggers wired up for realistic decline
      simulation — **SPLIT, part deferred to Phase 2.** Checklist wording said
      Stripe; provider is Razorpay. Done now: payloads match Razorpay's real
      `payment.failed` shape field-for-field, use real `error_reason` literals,
      and are HMAC-signed exactly as Razorpay signs them so the batch replays
      through the same signature-verified handler as a live webhook. Deferred:
      firing genuine test-mode failures from the Razorpay dashboard, which needs
      the webhook endpoint to exist first (Phase 2) plus the ngrok tunnel.
- [x] Sample batch saved as fixture data for repeatable testing
      `backend/fixtures/payment_failed_seed42_n75.json` (75 events, ~202 KB).
      Deterministic given `seed` + `now`.

### Phase 1b — remaining event types — COMPLETE
- [x] `checkout_abandoned` generator → unlocks `checkout_friction` and
      `genuine_abandonment`, the last 2 of 8 root causes
- [x] `invoice_overdue` generator
- [x] **All 8 root causes now producible.** A test asserts the two catalogues
      together cover the full taxonomy, so DIAGNOSE can have one example per
      category. Seed 42 / n=75 yields: payment_failed 57, checkout_abandoned 14,
      invoice_overdue 4, with all 8 causes present.
- **The original worry was wrong in a useful way.** I expected abandonment to
  have no Razorpay webhook and therefore to require a poller, breaking
  `architecture.md`'s "webhook-driven, not polling" rule. Razorpay does emit
  `payment_link.expired` and `invoice.expired`, so both map to real events and
  the constraint holds with no sweep needed.
- New modules: `app/simulation/abandonment_catalog.py` (8 scenarios),
  `app/simulation/abandonment.py` (entity + envelope builders).
  `tests/test_abandonment.py` (32 tests). Total suite 208 -> 240.

### Phase 2b — DETECT support for the new event types — COMPLETE
- [x] Mapped `payment_link.expired` -> `checkout_abandoned` and
      `invoice.expired` -> `invoice_overdue` in `detect.py`'s `SUPPORTED_EVENTS`
- [x] Parse the `payment_link` and `invoice` entities into `EventRecord`
      `parse_envelope` now dispatches on event name to one of three entity
      parsers, all returning a single `ParsedEvent` shape so persistence and audit
      stay one code path. Payment-only fields are `None` for abandonment, because
      nothing failed — the absence of an error IS the signal.
- [x] **Known issue H resolved.** `_abandonment_attempt_history()` counts
      `payment.failed` events for the customer between the link being issued and
      it expiring. Non-zero means friction, zero means disinterest. Only genuine
      payment failures count, so one expiry cannot inflate another's history.
- [x] **Amount at risk is the outstanding balance, not the gross amount.**
      Links use `amount - amount_paid`, invoices use `amount_due`. Counting gross
      would have overstated the headline "$ at risk" figure on any part-paid item.
- [x] Recovery window starts at issue, not expiry, so the 7-day hard stop
      measures from when money first became at risk.
- [x] `event_id_for()` now takes an entity kind, so a payment and a link sharing
      an id suffix cannot collide. Payment ids are unchanged, so events stored
      before Phase 2b are not re-detected as new.
- [x] Tests: `tests/test_detect_abandonment.py` (28). Suite 240 -> 271.
- [x] **Verified live, not assumed.** Replaying 75 events through the signed
      endpoint: `detected: 75, ignored: 0, failures: 0`, adding 57 payment_failed
      + 14 checkout_abandoned + 4 invoice_overdue rows, every one with an audit
      entry. Before Phase 2b the same batch dropped 18 events.
- Note: the `events.provider_payment_id` column now holds `plink_`/`inv_` ids too.
  Renaming it to `provider_entity_id` needs the table dropped, which would destroy
  the demo data, so the column keeps its name and the audit JSON uses accurate
  keys (`provider_entity_id`, `provider_entity_kind`) instead. Worth renaming
  whenever the database is next reset.

### Phase 2 — DETECT
- [x] Webhook receiver endpoint built
      `POST /webhooks/razorpay` (`app/webhooks.py`). Reads raw bytes before any
      parsing. Status codes chosen around Razorpay's retry behaviour: 200 for
      detected/duplicate/ignored, 400 malformed, 401 bad signature, 500 unexpected.
- [x] Signature verification implemented
      `app/signature.py`: HMAC-SHA256 hex over the raw body, constant-time
      comparison, **fails closed** when no secret is configured. Verification is
      kept in a separate module from the simulator's signing so a shared bug
      cannot agree with itself. Verified live: unsigned and bogus-signature
      requests both rejected 401 against a running server.
- [x] Event normalization into schema from `architecture.md`
      `app/detect.py`. `EventRecord` produced verbatim per the doc — no schema
      deviation. All card fields dropped; `extra="forbid"` makes that structural.
- [x] Unit tests passing
      182 total (was 90). New: `test_detect.py` (63), `test_signature.py` (11),
      `test_webhook_endpoint.py` (16). ruff clean.
- [x] Persistence + audit trail foundation (needed by this stage, not deferred)
      `app/db.py`, `app/models.py` (`customers`, `events`, `audit_log`),
      `app/audit.py`. Every event gets a DETECT audit entry; verified 0 events
      without one across 315 live events.
- [x] Idempotency
      `event_id` is a UUID5 of the provider payment id, and
      `provider_payment_id` is UNIQUE. Verified live: replaying the same batch
      twice left 75 events and amount-at-risk unchanged at Rs 131,776.
- [x] Batch replay through the real signed handler (the item deferred from Phase 1)
      `python -m app.replay`. Verified live: 75/75 detected, 0 failures, detect
      latency mean 56 ms / p95 82 ms. Uses the same endpoint and verification
      path a live ngrok delivery hits, so the batch does not bypass security.

### Phase 3 — DIAGNOSE — COMPLETE
- [x] System prompt written (`prompts/diagnose.md`, **v2**, 8.5 KB)
      Versioned file, never inlined. Documents all 8 causes, the action each
      triggers and the asymmetric cost of a wrong guess, plus which causes are
      possible per event type.
- [x] LLM call wired up, structured output validated against schema
      `app/diagnose.py`. Gemini response schema at the API, then Pydantic
      validation locally — the API constraint is not trusted alone.
- [x] Fixed taxonomy enforced (no free-text categories leaking through)
      Four layers: API schema, local validation, event-type possibility check,
      confidence floor. `low_funds`, `InsufficientFunds` and prose all rejected.
- [x] Low-confidence → `unknown` routing tested
      Default floor 0.75 (`DIAGNOSE_CONFIDENCE_THRESHOLD`). `unknown` is exempt:
      certainty that evidence is insufficient is still certainty. An override
      preserves the discarded classification in the reasoning so a reviewer sees
      what was set aside and why.
- [x] Unit tests: one per taxonomy category
      `tests/test_diagnose.py`, 63 tests, all with a fake client so no quota is
      spent. All 8 categories covered. Suite 283 total.
- [x] **Measured against real Gemini, not assumed.** `app/diagnose_eval.py`
      scores classifications against the Phase 1 ground-truth labels, which are
      never shown to the model. Result on the 75-event batch:
      **55/55 correct (100%) on events the classifier actually reached**, across
      6 of the 8 categories. 20 events hit the daily quota and are reported
      separately as `classifier_unavailable`, not as diagnoses.
      Latency mean 3.0 s, median 2.2 s per distinct evidence set.

### Phase 3 caveats — read before quoting the accuracy number
- **CLOSED in session 15: all 8 categories now have live measurement.** Re-running
  the eval on the new key and `gemini-3.1-flash-lite` gave **30/30 correct (100%),
  0 classifier failures**, covering 7 categories including `genuine_abandonment`,
  which had never been reached before. `card_expired` was confirmed separately by a
  direct probe through the real prompt and validation layers. Report:
  `backend/diagnose_eval_v2_gemini31.json`. The 55/55 figure from session 12 was on
  `gemini-2.5-flash-lite`, which this key can no longer reach; quote the newer run.
  Original caveat retained below for context.
  **2 of 8 categories are unverified against the real model.** `card_expired`
  (2 events) and `genuine_abandonment` (6 events) all hit the quota wall, so they
  have unit-test coverage but no live measurement. Re-run the eval on a fresh
  quota day to close this.
- **One ground-truth label was corrected after the model disagreed with it.**
  `transaction_frequency_limit_exceeded` was labelled `unknown`; the model said
  `bank_risk_block` and its reasoning was better than mine, since a
  network-imposed frequency cap is a restriction and `bank_risk_block` covers
  refusal on "risk, restriction or eligibility" grounds. Both labels route to
  `escalate_to_human_review`, so no action changed. Flagged explicitly because
  adjusting ground truth after seeing predictions is precisely how a benchmark
  gets gamed, even when each individual change is defensible. It happened once,
  with a written justification, and should not become a habit.
- **Accuracy is over classified events only.** Over all 75 events including
  quota failures it is 69%, which measures the quota rather than the prompt.

### Phase 3b — orchestration — COMPLETE
- [x] Call DIAGNOSE after DETECT and persist a `diagnose` audit entry
- [x] **All four stages chained** in `app/pipeline.py`. One function,
      `process_event()`, runs DETECT -> DIAGNOSE -> DECIDE -> EXECUTE, persists a
      typed record per stage (`diagnoses`, `decisions`, `execution_results`) and
      writes an audit entry per stage. No stage's logic moved into this module;
      it only chains them and stores what each produced, per `code-standards.md`.
- [x] Wired into the webhook route behind `PIPELINE_RUN_INLINE` (default on).
      A duplicate delivery returns early and runs no stages, so a Razorpay retry
      cannot double-count or message anyone twice.
- [x] **Both latencies recorded, not one** — `decision_latency_ms` (received ->
      decided) and `send_latency_ms` (received -> dispatched). This is the
      implementation half of Known issue A; how Phase 6 *reports* them still
      needs sign-off.
- [x] Degradation path is exercised by test, not hoped for. A classifier outage
      produces `unknown` -> `escalate_to_human_review` -> recorded skip, with all
      four audit entries still written.
- [x] `Customer.last_contacted_at` moves only on `DeliveryStatus.SENT`, so a dry
      run or a refused send cannot consume the 24-hour contact window and
      silently suppress a later genuine send.
- [x] Tests: `tests/test_pipeline.py` (55), including five driven over HTTP
      through the signed route with the classifier and channel adapters faked.

### Phase 5 — EXECUTE — COMPLETE
- [x] ~~Email/SMS~~ **WhatsApp** channel — real Twilio integration
      `app/channels.py`. `TwilioWhatsAppSender` refuses any recipient not on
      `TWILIO_WHATSAPP_TEST_RECIPIENTS`, so a well-formed synthetic number cannot
      reach a stranger. Falls back to `DryRunSender` when credentials are absent,
      and a dry run is **never** counted as a send.
- [x] Payment link generation (Razorpay)
      `RazorpayPaymentLinkFactory` creates a provider-hosted link, so the customer
      pays on Razorpay's page and the agent never touches card data or submits a
      charge (constraints #1, #2, #6). Provider notifications are disabled
      (`notify.sms/email = false`, `reminder_enable = false`) because this agent
      owns the messaging — letting Razorpay also notify would double-contact the
      customer from outside our guardrails. Refuses a non-`rzp_test_` key on its
      own, independently of the `Settings` validator.
- [x] Delivery status + customer outcome tracking
      `execution_results` table. `amount_recovered_minor` stays NULL until a
      provider webhook confirms payment — a send is not a payment, and
      pre-filling it would invent revenue.
- [x] Error handling: failed execute → retry queue or escalation, never silent drop
      Every path returns a stated reason. A transport failure is `FAILED` +
      `requeued`; an allowlist refusal is `SKIPPED` and deliberately **not**
      requeued, because a number that never opted in will never accept a retry.
- [x] Tests: `tests/test_execute.py` (134), one per action plus the honesty
      properties. Strict test doubles raise on any provider call other than
      "create a link" and "send a message", so a future branch that reached for a
      charge API fails a test rather than a compliance review.
- [x] **Verified live end to end**: 3 events through the signed endpoint, 4 audit
      stages each, 0 violations, nothing charged, nothing marked recovered.

### Phase 4 — DECIDE — COMPLETE
- [x] Rules engine / lookup table implemented (deterministic, no LLM call)
      `app/decide.py`. Pure function: same inputs give the same Decision, which is
      what makes a violation count meaningful. A test reads the module source and
      asserts no LLM symbol is reachable from it, rather than trusting that it
      stays that way.
- [x] All 8 action-set entries from `architecture.md` implemented
      `action_table()` holds all 8 rows. A contract test parses
      `architecture.md`'s table, so adding a row without updating the doc first
      fails the build.
- [x] Guardrails module built: max retries, quiet hours, contact frequency, 7-day
      hard stop — `app/guardrails.py`, each a function returning a pass/fail
      `GuardrailCheck` with a human-readable reason.
- [x] Guardrail checks logged even when passing
      All four always run and are always recorded (constraint #5). Verified across
      the batch: 0 of 75 decisions missing a check.
- [x] Unit tests: one per action, one per guardrail
      `tests/test_guardrails.py` (45) and `tests/test_decide.py` (39). Guardrails
      are tested without importing `app.decide`, per `code-standards.md`. Suite
      283 -> 365.
- [x] **Verified against the real 75-event batch, not just unit tests.**
      **0 stopping-rule violations**, checked programmatically by re-deriving each
      rule independently of the code that enforced it. Decisions identical on
      re-run. Action mix: schedule_retry 20, escalate_to_human_review 19,
      send_reminder 18, send_fresh_auth_link 16,
      send_update_payment_method_link 2.

### Phase 4 — what the batch outcome actually means
Of 75 events: 36 chose a no-contact action (retry or escalation), 22 were stopped
by a guardrail, 12 send immediately and 5 are deferred to a later allowed window.

So only 17 events result in a customer being messaged. That is the honest picture
and it needs saying before Phase 6 computes a recovery rate: a large share of the
batch is *correctly* not actioned. See Known issue M.

### Phase 6 — Audit trail + metrics — COMPLETE
- [x] Structured logging in place for all four stages
      `app/logging_setup.py`. One compact JSON line per event per stage, emitted
      from **`audit.record`** rather than by each stage: if a stage logged for
      itself it could log without auditing or audit without logging, and the two
      accounts of the same event would drift. Emitting from the one function that
      writes the audit row makes them the same event by construction. Non-pipeline
      records stay human-readable, because a demo with an unreadable console is
      worse than one with two formats. Verified live in the server output.
- [x] Audit log queryable per event_id
      `GET /events/{event_id}/audit` (built in Phase 2, now exercised properly),
      returning the ordered stage-by-stage trail. Confirmed live: 4 stages for a
      real batch event.
- [x] Metrics computed: $ recovered / $ at risk, detect→execute latency,
      guardrail violations (should be 0) — `app/metrics.py`, plus
      `python -m app.metrics` with `--json`, `--out` and `--fail-on-violation`.
- [x] **Violations are RE-DERIVED, not read back.** The obvious implementation
      counts decisions whose recorded checks all passed, which is circular: it
      asks the enforcing code to grade itself, so a bug in `guardrails.py` would
      be invisible in exactly the metric meant to catch it. Instead each rule is
      reconstructed from raw data and tested against what actually happened —
      including converting each send time into the *customer's* timezone, since
      checking quiet hours in UTC would wave through a 3am message in Kolkata.
      A test feeds the checker a row whose recorded flags all claim success while
      the raw data shows a contact 9 days late, and asserts it is still caught.
- [x] **Two extra violation classes the guardrails cannot see.**
      `contact_frequency` is cross-event by nature, so a per-event check cannot
      notice the same person being messaged twice; and `sent_before_due` catches
      the end-to-end form of the session-14 deferral bug, where DECIDE deferred
      correctly and EXECUTE dispatched anyway.
- [x] Simple dashboard or report view built
      `GET /dashboard` (`app/dashboard.py`), with `/api/metrics` and `/api/events`
      as JSON counterparts. Server-rendered HTML, no build step, **no JavaScript
      at all** — the drilldowns are `<details>` elements with the data already
      inside them, so nothing has to fetch successfully while somebody is
      watching, and they are keyboard-accessible for free.
- [x] Every element `ui-context.md` asks for: headline metrics bar, batch table
      with the specified columns, and a per-event detail view showing all four
      stages including **every** guardrail result, passes included (constraint #5
      made visible). Colour is never the only signal — each tinted pill also
      states its meaning in words.
- [x] Tests: `tests/test_metrics.py` (53), `tests/test_dashboard.py` (45),
      `tests/test_logging_setup.py` (8). Suite 608 -> 708.
- [x] **Verified on a real 75-event batch**, not unit tests alone: 100% audit
      coverage, 0 violations, 0 classifier outages, 0 events over the 60s decision
      budget, and a 648 KB dashboard rendering it all with no `<script>` tag.

### Phase 6 — what the numbers do and do not say
- **INR 0 recovered is correct, not a failure.** `amount_recovered` is only ever
  set by a provider webhook confirming a payment, and no such webhook has arrived
  because these are synthetic events whose links nobody clicks. The dashboard says
  "not confirmed (awaiting a provider webhook)" rather than showing 0.00, which
  would read as a measurement.
- **40 of 75 events were withheld by a guardrail, nearly all on age.** A generated
  batch deliberately spreads failures across a 14-day window while the hard stop is
  7 days, so over half the batch is *correctly* stopped before any contact. That is
  the rule working, but it makes for a thin demo. For Phase 7, generate a batch
  whose events fall mostly inside the recovery window, or state the split up front.
- **The batch ran at 23:15 IST, outside the 09:00-20:00 contact window**, so all 7
  contactable events were deferred rather than sent and no real payment link or
  message was created. A daytime run would show `contacted` instead. Worth
  repeating the run in-hours before the demo.
- **Decision latency is dominated by the Gemini call**: mean 9.4s against a 60s
  budget. Comfortable, but it is one model round trip per distinct evidence set,
  not our own processing, so a slower model would eat the margin quickly.

### Phase 7 — Full batch run (v1 done)
- [x] Full synthetic batch run end to end
      75 events generated fresh, signed, and replayed through the real endpoint:
      75 detected, 0 failures, all four stages each. Done as Phase 6's verification
      rather than as a separate exercise.
- [~] All `Definition of done` items from `project-overview.md` — 4 of 5 provable,
      the fifth needs a human:
  - [x] Can replay a batch of N failure events through the full pipeline
  - [x] Every event has a diagnosis, a decision, an action, and a logged outcome
        (100% audit coverage over 75 events, asserted by query not by eye)
  - [x] Recovery rate and $ recovered are computed and displayed — with the
        denominator split per Known issue M, so correct restraint is not scored as
        failure. The headline is honestly INR 0 recovered; see the note above.
  - [x] No event violates a stopping rule, checked programmatically — 0 of 75,
        re-derived independently rather than read back from the enforcing code
  - [ ] **A stranger can read the audit log for any single event and understand
        what happened and why, in under 30 seconds.** The trail is built and
        rendered, but this one is a judgement about whether it *reads* well, and
        self-certifying it would be worthless. Needs someone who has not seen the
        code to open `/dashboard`, expand an event, and say.
- [x] Results written up with headline numbers
      **`RESULTS.md`** at the repo root. Numbers in `backend/phase7_results.json`,
      reproducible with `python -m app.metrics --limit 76`.
- [x] **Batch re-run in-hours**, so the contact path is exercised rather than
      deferred. 76 events: 32 withheld by guardrail, 20 escalated, 15 retry
      scheduled, 8 send refused (not opted in), **1 genuinely contacted**. All 8
      root causes classified, **0 classifier outages**, **0 violations**,
      **100% audit coverage**, **0 events over the 60s decision budget**.

### Phase 7 addition — outcome confirmation (logged scope change)
Not on the Phase 7 checklist. Built because success metric #1 could not otherwise
exist: `amount_recovered_minor` had no writer anywhere in the codebase, so
"$ recovered / $ at risk" was structurally zero and `project-overview.md`'s claim
"then proves how much money it recovered" could not be made at all.
`architecture.md`'s pipeline diagram has this arrow — *webhook confirms outcome ->
audit log + recovered-$ counter updated* — and it had never been implemented.
Flagged as a split rather than folded in silently.
- [x] `app/outcomes.py` handles `payment_link.paid`, `invoice.paid`, `order.paid`
      and `payment.captured`, wired into the webhook route **ahead of DETECT**,
      which would otherwise acknowledge a paid event as "unsupported" with a 200
      and silently never credit the money.
- [x] Attribution recorded at three strengths, so the figure can be discounted
      rather than trusted flat: `recovery_link_paid` (unambiguous — that link exists
      only because of the recovery action), `same_invoice_paid`, and
      `same_order_captured` (the customer may have retried unprompted).
- [x] **One payment credits exactly one event.** A retry chain holds several at-risk
      events for one order; crediting all of them would multiply the headline figure
      by the length of the chain. The newest at-risk event wins.
- [x] Redelivery cannot double-count: the amount is *assigned*, never incremented,
      so idempotency holds by construction rather than by a guard someone must
      remember.
- [x] An unmatched payment is returned, never guessed at, and gets a 200. Somebody
      paying normally must not become recovered revenue.
- [x] Tests: `tests/test_outcomes.py` (40), including 8 driven over HTTP with signed
      bodies, one asserting an unsigned body credits nothing, and one asserting a
      confirmation lifts the headline metric off zero.

### Phase 8 — Stretch goals — payday DONE, promise-to-pay declined
- [~] **Promise-to-pay tracker — NOT BUILT, deliberately.** It requires a customer
      to *make* a promise, which requires an inbound channel: a Twilio inbound
      webhook plus intent parsing over free text. Neither is wired, so a `promises`
      table would have no writer — dead code presented as a feature, which
      `code-standards.md`'s no-over-engineering rule exists to prevent. Logged
      rather than half-built. The plug-in point already exists: the session-14
      deferral gate that holds a send until its due time is exactly what a promise
      would drive. Cost to build properly: inbound webhook, intent extraction, a
      promise table, and a DECIDE rule suppressing contact until the promised date.
- [x] **Payday-aware retry timing — BUILT, without fabricating data.**
      `architecture.md` asks for it "if data available". `customer_paydays` is the
      *if available* half, with a `source` column so a value can never be mistaken
      for something we inferred. **Nothing infers a payday from payment history**,
      because this system has no history of *successful* payments to infer one from.
      With no payday on record the behaviour is the flat interval — which is what
      runs for every customer in the demo, and a test asserts that default path is
      unchanged.
      Two rules stop the payday path doing harm: the retry lands the day **after**
      payday (salary credited on the 1st is not reliably spendable at 00:01, and an
      early retry burns one of only three permitted attempts), and a payday falling
      **beyond the hard stop is ignored**, since targeting it would schedule a retry
      that can never run. Day-of-month clamps to the real month length, so a payday
      of the 31st resolves in February instead of raising.
      Tests: `tests/test_payday_retry.py` (27), including one asserting DECIDE still
      contains no LLM call after the change.

- Note: voice channel is out of scope — do not add it without an explicit decision logged below

## Decisions log
> Record any decision that deviates from or clarifies the other five docs,
> with a one-line reason, so future-you (or the AI assistant) doesn't
> relitigate it.

- **Action enum carries verbs; parameters are separate fields.**
  `architecture.md`'s action table writes parameters inline
  (`schedule_retry(+N days)`, `send_reminder(1x), then stop`), which cannot be
  literal enum values. `Action` holds 5 verbs; `delay_seconds` and
  `max_repeats` live on `Decision`. All 8 table rows survive as 8
  root_cause -> action mappings, and a contract test asserts that.
  *Needs owner confirmation.*
- **`Decision.guardrail_checks` added; `guardrail_checks_passed` derived.**
  The doc's `guardrail_checks_passed` list holds only passing check names, so
  it structurally cannot record a failure — which contradicts constraint #5
  ("every guardrail check result is logged, even when it passes"). Full
  pass/fail results live in `guardrail_checks`; `guardrail_checks_passed` is now
  a derived property so the two can never disagree. Constraint outranks example.
- **Fourth guardrail name added: `hard_stop_7_days`.** Constraint #4 lists four
  stopping rules; the Decision example listed only three. Enum carries all four.
- **Money is `Decimal`, not `float`.** Doc says "number"; float loses precision
  on currency, and this project's headline metric is a money total.
- **RESOLVED — payment provider is Razorpay.** Owner decision, session 2.
  Matches the buildathon brief and the India/UPI path `project-overview.md`
  permits. `stripe` SDK stays installed but unused; Stripe env keys remain
  documented in case of a swap.
- **WhatsApp (Twilio) is the only live channel for v1.** Owner decision: no
  SendGrid. `Channel.EMAIL` stays in the enum because `architecture.md` defines
  it, but it is unreachable in practice. WhatsApp messages carry a
  Razorpay-hosted payment link the customer taps — the agent never processes
  the payment, which keeps constraints #1/#2/#6 intact.
- **Postgres runs in a container, pinned to `postgres:17-alpine`.** Owner asked
  for a container. Pinned to 17 rather than current 18 because the PG18 official
  image changed its data-directory layout. 17 is supported to 2029. This turned
  out to be useful beyond caution: the machine also runs a **native
  PostgreSQL 18** Windows service, so the major version is now an unambiguous
  signal of which server a connection reached, and a test asserts it.
- **Container publishes on host port 55432, not 5432.** The native
  `postgresql-x64-18` service already owns 5432. On Windows the collision
  surfaces as "socket in a way forbidden by its access permissions", which reads
  like a privilege problem rather than a port conflict. `POSTGRES_PORT` drives
  both the compose port mapping and the derived connection URL, so one value
  changes both. The native service was left running rather than stopped — a
  non-destructive fix that does not disturb whatever else depends on it.
- **DB tests are marked `integration` and skip when the container is down.**
  Keeps `pytest` green without Docker. An explicit 3s `connect_timeout` was
  needed: without it the skip path stalled for 2m13s on OS-level TCP retries.
- **Readiness reports warnings, not just missing keys.** Added checks for
  `DATABASE_URL` vs `POSTGRES_*` drift, non-WhatsApp-formatted Twilio sender,
  and an empty WhatsApp opt-in allowlist. All three fail opaquely at runtime
  otherwise, and the third would inflate the recovery metric with sends that
  never arrived.
- **`DATABASE_URL` is now DERIVED from `POSTGRES_*`, not maintained separately.**
  Owner asked why both existed when the container defines the database. Fair
  challenge: they were two hand-maintained copies of one fact, and the drift
  between them was a footgun the readiness check was papering over rather than
  removing. `POSTGRES_*` is now the single source of truth;
  `Settings.effective_database_url` builds the URL (percent-encoding credentials
  so a `@` or `/` in the password cannot corrupt it). `DATABASE_URL` survives as
  an optional override for a Postgres that docker-compose does not manage, and a
  leftover `CHANGEME` placeholder is detected and ignored rather than used.
- **Test-mode enforcement is code.** `config.py` raises on `sk_live_`/`rzp_live_`
  keys rather than trusting the standard as documentation.
- **Postgres deferred to Phase 2.** Phase 0 needs no persistence; `db.py` and
  ORM models are not written yet. `DATABASE_URL` is configured but unused.

### Session 16 (Phases 7 + 8)
- **Outcome confirmation was built as a logged scope addition, because the headline
  metric could not otherwise exist.** `amount_recovered_minor` had no writer
  anywhere, so "$ recovered / $ at risk" was structurally zero — not "zero because
  nothing was recovered", but zero because no code path was capable of setting it.
  `architecture.md`'s diagram has the arrow and it had never been implemented.
  Building a dashboard that reports a permanently-zero headline would have been
  delivering a broken metric with a straight face.
- **Only a signed provider webhook may write a recovered amount.** EXECUTE knows it
  sent a message; it does not know whether anyone paid. Letting any earlier stage
  set the number would turn a delivery statistic into a revenue claim.
- **Attribution is stored at three strengths rather than blended.** Paying through a
  link the agent created and sent is unambiguous. An order being captured later is
  genuine recovered revenue but the customer may have retried unprompted, and a
  reader should be able to discount that separately instead of trusting one figure.
- **One payment credits exactly one event.** The bug this prevents is specific and
  nasty: a retry chain holds several at-risk events for one order, so crediting each
  would multiply the headline by the length of the chain — inflating the metric by
  precisely the behaviour the agent exists to handle.
- **Idempotency by construction, not by guard.** The recovered amount is assigned
  rather than incremented, so a redelivered webhook is a no-op even if someone later
  forgets why. Only the first confirmation writes an audit entry; two would read as
  two separate payments.
- **Confirmation is routed BEFORE DETECT.** DETECT would classify a paid event as
  unsupported and answer 200 "ignored" — a completely silent failure in which every
  request looks successful while the money is never credited. A test asserts the
  status is not "ignored", specifically to pin that.
- **`payment.captured` was removed from the "unsupported events" test list.** That
  test failing when confirmation shipped was the tripwire working as designed, not
  a regression.
- **Filed the confirmation audit entry under `Stage.EXECUTE` rather than adding a
  fifth stage.** `Stage` mirrors architecture.md's four pipeline stages, and
  confirming the outcome of an action belongs to that action's story. Avoids a
  taxonomy deviation and keeps the "all four stages" coverage metric meaningful.
- **A refused send is now its own disposition, not a generic skip.**
  `send_refused_not_opted_in`. The batch showed 8 events where the agent diagnosed
  the cause, chose an action and created a *real* Razorpay-hosted payment link, and
  only the final delivery was withheld because the number never opted in. Reporting
  that as a plain skip hid the difference between an environment limitation of a
  messaging sandbox and a decision about the customer.
- **Payday-aware retry built without inventing the data.** `customer_paydays` with a
  `source` column, sparse by design. Nothing infers a payday from payment history
  because there are no successful payments to infer from. Two rules stop it doing
  harm: the retry lands the day *after* payday, and a payday beyond the hard stop is
  ignored rather than scheduling a retry that can never run. The no-payday path is
  asserted to be identical to the old flat interval, since that is what actually
  runs.
- **Promise-to-pay declined rather than half-built.** No inbound channel exists, so
  a promise has no source and the table would have no writer. Documented what it
  would take and where it would plug in (the session-14 deferral gate).
- **A real WhatsApp message was sent end to end**, to the one opted-in sandbox
  number: DIAGNOSE `card_expired` -> DECIDE `send_update_payment_method_link` ->
  EXECUTE created a real test-mode link and Twilio delivered it
  (`SM381dde1f021d3dc3da05ea9f0a18e269`). That is the `contacted` disposition being
  earned rather than simulated.
- **Two arithmetic errors of mine, caught by tests rather than shipped.** A
  days-until-payday expectation (Sept 20 -> Oct 5 is 15 days, not 16) and, in
  session 15's dashboard work, a timezone assumption (June in Auckland is UTC+12, so
  03:30 UTC is mid-afternoon, not late evening). Both were wrong test expectations
  against correct code.
- **`ExecutionRecord` has no ORM `relationship()` to `Event`**, only a bare foreign
  key, so SQLAlchemy's unit of work does not know it must insert the event first.
  Tests seeding both must flush between them. Worth knowing before writing more
  fixtures.

### Session 15 (Phase 6)
- **Latency CANNOT be derived from the stored timestamps, so it is persisted.**
  This is the finding of the session and it is not obvious. `events.received_at`,
  `decisions.decided_at` and `execution_results.executed_at` all default to
  `func.now()`, and Postgres `now()` is `transaction_timestamp()` — stable for the
  whole transaction. The pipeline writes all four stages in ONE transaction, so
  those three columns resolve to the *identical* instant and any subtraction
  between them yields exactly zero. A dashboard built on that would have proudly
  reported 0 ms for every event. The measured `perf_counter` figures now go to a
  new `event_latencies` table. Use `clock_timestamp()` if a real DB-side wall clock
  is ever wanted.
- **`event_latencies` is a new table, not new columns.** Same reasoning already
  recorded for the Phase 3b/5 tables: this project uses `create_all()` with no
  migrations, which creates missing tables but cannot add a column to an existing
  one. A new table lands without dropping the demo data.
- **BUG FIXED — `ExecutionRecord.executed_at` was falling through to its column
  default** instead of being set from the outcome, so the row disagreed with the
  time the audit trail reported for the same action. One line, no schema change.
- **Violations are re-derived from raw data, never read back from the recorded
  flags.** Counting decisions whose checks all passed would ask the enforcing code
  to grade itself, making a `guardrails.py` bug invisible in precisely the metric
  meant to catch it. Two rules can only be seen this way at all: contact frequency
  is cross-event, and `sent_before_due` is the end-to-end form of the session-14
  deferral bug. The recorded flags are consulted for exactly one thing — that all
  four results are *present*, which is constraint #5 and is a question about the
  trail rather than about the rule.
- **Quiet hours are re-derived in the CUSTOMER's timezone.** Checking UTC hours
  would pass a message sent at 3am in Kolkata. A test pins the same instant against
  two zones and asserts it is a breach for one and clean for the other.
- **A stopping rule only binds when somebody was actually contacted.** A 9-day-old
  event that escalated to a human or scheduled a silent provider-side retry
  disturbs nobody, so counting it as a violation would make correct behaviour look
  unsafe. Tested per action.
- **Server-rendered HTML, not React — a deliberate documented deviation.**
  `architecture.md` and `code-standards.md` both name React, but under *Suggested*
  stack, while `ui-context.md` says plainly that "a simple HTML page or a
  Streamlit/Gradio app is enough" and "don't spend build time on visual polish
  here; spend it on the pipeline". A Node toolchain for one static table is the
  over-investment that doc explicitly warns against. Flagged to the owner at the
  start of the phase rather than decided quietly.
- **No JavaScript at all on the dashboard.** Drilldowns are `<details>` elements
  rendered with their data already inside, so nothing needs to fetch successfully
  mid-demo, and they are keyboard-accessible without any work. Colour is never the
  only signal: every tinted pill also states its meaning in words, so the page
  survives greyscale and a screen reader.
- **Model-generated text is treated as untrusted input.** DIAGNOSE's `reasoning`
  and provider `error_description` strings are rendered into HTML, so everything is
  escaped. Tests inject `<script>` and `<img onerror>` through both paths.
- **The dashboard sharpens the existing no-auth decision rather than changing it.**
  `/health` leaked nothing; `/dashboard`, `/api/metrics` and `/api/events` return
  customer ids, amounts at risk, decline reasons and payment-link ids for every
  event. It stays unauthenticated because `ui-context.md` scopes this as a local
  demo tool, and it is kept off `app.tunnel` — the three new paths were added to
  `test_tunnel_app.py`'s local-only whitelist so a future mistake fails a test
  instead of quietly becoming internet-facing.
- **The escaping test caught a missing requirement.** `ui-context.md` asks the
  detail view to show DIAGNOSE's root cause, confidence *and* one-line reasoning;
  the reasoning was not being rendered at all. Found because the XSS test could not
  locate its escaped payload on the page.
- **Model changed to `gemini-3.1-flash-lite`, forced by the new key.** The
  replacement key 404s on `gemini-2.5-flash-lite` ("no longer available to new
  users"). Candidates were probed through the real prompt and validation layers
  rather than trusting the API's own suggestion: `gemini-3.5-flash-lite` and
  `gemini-flash-lite-latest` both return 400 INVALID_ARGUMENT against our
  response-schema request, and 3.5's apparent pass was just the
  classifier-unavailable fallback rather than an answer. 3.1-flash-lite classifies
  correctly *and* still answers `unknown` on an opaque decline, which is the
  behaviour session 12 had to fight for. `code-standards.md` calling the model
  swappable rather than load-bearing is what made this a config change.
- **Session 12's note that 3.1-flash-lite "invented `expired_card`" no longer
  holds.** That predated the v2 prompt and the response-schema enum; retested, it
  returns the correct enum value. Superseded rather than deleted, so the reasoning
  trail stays readable.

### Session 14 (Phases 3b + 5)
- **BUG FOUND AND FIXED — EXECUTE was discarding DECIDE's deferrals, which broke
  constraint #4 end to end.** Session 13 decided that `quiet_hours` and
  `contact_frequency` are *deferrable*: DECIDE leaves `blocked_reason` empty and
  moves `scheduled_for` to the next permitted moment. Session 13 also decided that
  `blocked_reason is not None` is the signal EXECUTE keys on. Both decisions are
  right on their own and wrong together — a deferred contact has no
  `blocked_reason`, so EXECUTE dispatched it immediately and would have sent a
  WhatsApp message at 3am, which is precisely what the rule exists to prevent.
  EXECUTE now has **two** stop gates: `blocked_reason` (terminal, cancelled) and
  `is_deferred()` (not yet due, postponed). No link is created for a deferred
  send, since one minted hours early is wasted and could expire.
  **This also qualifies the Phase 4 claim.** "0 stopping-rule violations across 75
  events" was measured at the DECIDE layer, where the violation genuinely does not
  exist. It was never an end-to-end measurement, and end to end it did not hold
  until this fix. The claim stands for DECIDE; Phase 6 should measure violations
  against what EXECUTE actually did, not against what DECIDE chose.
- **A deferral is a skip with a due time, not a failure.** It is not `requeued`,
  because `requeued` means a dispatch failed and must be retried. The due time
  lives on `decisions.scheduled_for`, which is what a due-work scan should read.
  No scanner exists yet — deferred sends are correctly recorded and correctly not
  sent, but nothing comes back for them. Logged as Known issue N.
- **The decision-level gates run before the customer-record checks.** A send that
  is not due yet has not been attempted, so reporting "no contact number" would be
  asserting the outcome of an attempt that never happened; the contact may well be
  filled in before the due time. The data gap surfaces when the send comes due.
- **Escalation is never deferred.** It is internal, involves no contact, and
  delaying it would leave a risky event sitting with nobody informed.
- **BUG FOUND AND FIXED — the existing webhook endpoint tests would have made
  live API calls.** `pipeline_run_inline` defaults to true, and `diagnose`,
  `channels`, `decide` and `guardrails` each resolve settings by calling
  `get_settings()` themselves rather than reading the object injected through
  FastAPI's dependency override. So once the pipeline was wired in, running
  `test_webhook_endpoint.py` on a machine with a populated `.env` would have spent
  real Gemini quota and created real test-mode payment links on every run. Those
  tests now pin `pipeline_run_inline=False` and stay what they were about —
  signature verification over raw bytes and Razorpay's retry status codes — while
  `test_pipeline.py` covers the inline path with every client explicitly faked.
- **`execute_action` now catches exceptions from the sender.** Link creation was
  already guarded but the send was not, contradicting the module's own documented
  promise that no dispatch problem escapes. The senders in `channels` catch their
  own transport errors, so this only bites a future or third-party sender — but
  one raising mid-batch would have aborted every event queued behind it with the
  link already created and no record of why.
- **The replay client's timeout was shorter than DIAGNOSE's own backoff.** At 10s
  it gave up mid-retry and reported `transport_error` for requests the server went
  on to answer 200, which reads as a broken endpoint rather than an exhausted
  quota. Raised to 45s and exposed as `--timeout`. Worth noting as a verification
  hazard: the tool reported failure while the system was behaving correctly.
- **Tests assert the two modules agree on the allowlist wording, rather than
  copying the string.** EXECUTE classifies a refusal as a skip by matching text
  that `channels` produces. Reword it in one place and refusals silently become
  requeued failures that retry forever, so the test drives the real sender to
  produce the real refusal and feeds that through EXECUTE.
- **Fixture events had to be re-dated for the pipeline tests.** A generated batch
  spreads failures over a fortnight, so most fixture events are already past the
  7-day hard stop and get blocked before any of the behaviour under test can run.
  The test helper pins each event a few minutes before the evaluation clock; the
  hard stop itself stays covered by the guardrail tests. Same trap as Known issue
  E, met from the other direction.
- **Customer identity is set explicitly in the pipeline tests.** DETECT resolves it
  from `notes.customer_id` when present and a contact hash otherwise, so the first
  attempt at "give this event a fresh customer" silently kept reusing one. Tests
  now state outright whether two events belong to the same person.

### Session 13 (Phase 4)
- **Guardrails split into terminal and deferrable.** `max_retries` and the 7-day
  `hard_stop` mean stop; `quiet_hours` and `contact_frequency` mean wait. Treating
  all four as blocks would discard recoverable revenue, since quiet hours does not
  mean "never contact this person", it means "not at 3am". `GuardrailKind` records
  which is which so DECIDE cannot mistake a deferral for an abandonment.
- **Guardrail applicability is per action, but every check still runs.**
  `quiet_hours` and `contact_frequency` exist to protect a human from being
  disturbed, so they have no bearing on a silent provider retry or an internal
  escalation — nobody is woken by a charge attempt at 3am. `max_retries` and
  `hard_stop` govern anything with an external effect but not an internal handoff.
  All four are still evaluated and recorded for every event, because constraint #5
  requires the trail to show the check happened. Applicability decides whether a
  *failure* changes the outcome, never whether the check runs.
- **Escalation deliberately survives terminal guardrails.** Handing an exhausted
  case to a person involves no contact and no charge, and is most warranted
  precisely when automation has run out. Suppressing it would leave revenue
  unattended with nobody informed.
- **`blocked_reason is not None` is the signal EXECUTE must key on, not
  `channel`.** `escalate_to_human_review` legitimately carries `channel: none`
  while still being an action that should happen, so channel alone is ambiguous.
- **A blocked decision keeps the action it would have taken.** The trail should
  say what was prevented, not merely that nothing happened.
- **Action-level repeat limits are kept separate from the four guardrails.**
  "single quiet retry, then stop" and "send_reminder(1x)" are properties of the
  action table, not of constraint #4, so they live in `max_repeats` and surface
  through `blocked_reason` rather than inflating `GuardrailName` — which must keep
  matching the doc.
- **Payday-aware retry timing was NOT faked.** `architecture.md` asks for it "if
  data available"; no payday data exists, so `insufficient_funds` uses a flat
  configurable interval (default 3 days) and payday awareness stays a Phase 8
  stretch goal. A guess dressed as insight would be worse than an honest default.
- **Only `whatsapp` and `none` are ever chosen as channels.** There is no live
  email integration, so selecting `email` would produce a decision EXECUTE cannot
  honour. A test enforces it.
- **Unknown customer timezone falls back to IST and records the assumption** in
  the check detail, rather than crashing or silently pretending to know. Quiet
  hours evaluated against the wrong clock could contact someone overnight.
- **Two structural tests guard the stage's defining properties**: a contract test
  parses `architecture.md`'s action table, and another reads `decide.py`'s source
  to assert no LLM symbol is reachable from it.

### Session 12 (Phase 3)
- **Model changed to `gemini-2.5-flash-lite`, on measurement not preference.**
  `gemini-2.5-flash` allows 20 requests per day on this key. `code-standards.md`
  called the model swappable rather than load-bearing, and that turned out to be
  the thing that saved the phase. Newer 3.x models were tried and returned prose
  in `root_cause` or invented `expired_card`; flash-lite classified correctly at
  1.6 s. `.env` was updated too, since it pinned the old model and would have
  overridden the new default.
- **The prompt teaches the taxonomy rather than relying on a confidence floor.**
  A probe returned `bank_risk_block` at confidence 0.7 for a decline whose only
  stated reason was `payment_failed`, admitting it had picked the more common
  possibility. A numeric threshold alone cannot fix that, because the same guess
  later came back at 0.90. Rule 1 and a worked counter-example in the prompt make
  `unknown` an explicitly correct answer.
- **`unknown` from an outage is separated from `unknown` from thin evidence.**
  `CLASSIFIER_UNAVAILABLE_PREFIX` marks the former, and `audit_summaries()`
  surfaces it as `classifier_unavailable`. Without this, a quota failure would
  appear in metrics as cautious escalation — an unpaced first run produced 24 such
  events and an apparent accuracy of 43% that measured nothing but the quota.
- **Rate limits are retried with the delay the API asks for.** Retrying a
  per-minute quota immediately cannot succeed. A malformed response still retries
  immediately, since sleeping there would only waste batch time.
- **The model is sent only fields some rule actually uses.** An earlier version
  also sent `amount`, `tenure_days` and `past_failures` on the vague grounds that
  they "colour plausibility". No rule referenced them, so they invited invented
  correlations, and because they vary per event they made 75 events produce 74
  distinct evidence sets. Removing them cut that to 27 and made caching work.
  `prior_attempts` is sent only for abandonment, where Rule 2 uses it.
- **`event_id` is never requested from the model.** Asking for a value already
  held only creates a way to corrupt it.
- **No silent fallback when the LLM is unconfigured.** `build_client()` raises. A
  demo that quietly skipped the stage would look like it had run.
- **Ground-truth label corrected once, with justification.**
  `transaction_frequency_limit_exceeded` moved from `unknown` to
  `bank_risk_block`. Flagged prominently under Phase 3 caveats because changing
  labels after seeing predictions is how benchmarks get gamed.

### Session 10 (Phase 1b)
- **Abandonment maps onto REAL Razorpay events, so no poller is needed.**
  `payment_link.expired` for an abandoned checkout, `invoice.expired` for an
  overdue invoice. This removed a tension I had flagged: abandonment looked like
  it would force polling, since you cannot be notified that nothing happened.
- **Entity shapes were read back from the live API, not transcribed from docs.**
  The docs pages would not fetch reliably, and inventing field names was the one
  unacceptable outcome. Creating a real test-mode invoice and reading it back gave
  40 invoice fields and 26 payment-link fields, saved redacted to
  `fixtures/reference_real_entities.json` with fidelity tests against it. Details
  no amount of guessing would have produced: payment links use `0` for unset
  timestamps while invoices use `null`; invoices duplicate customer fields as both
  `name` and `customer_name`; invoices carry `currency_symbol` and
  `idempotency_key`.
- **Friction vs abandonment is decided by evidence of trying.**
  `architecture.md` names both causes but not how to separate them. Attempted-and-
  failed or part-paid means `checkout_friction`; never engaged means
  `genuine_abandonment`. That split matters because the two deserve opposite
  treatment — help versus one reminder then stop. A test enforces the mapping.
- **Abandonment scenarios are seeded before weighted filling.** Pure weighted
  sampling can miss a scenario entirely in a small batch, and a batch silently
  missing `checkout_friction` would let DIAGNOSE ship untested against it.
- **Abandonment events carry `method=None`.** An expired link has no payment
  method because the customer never chose one; inventing one would imply
  knowledge the provider never gave us. Required making `GeneratedEvent.method`
  optional and scoping the payment-specific tests.
- **Fixture renamed to `batch_seed42_n75.json`.** The old
  `payment_failed_seed42_n75.json` name became a lie once a batch spanned three
  event types.

### Session 9 (real provider events confirmed — Phase 2 fully closed)
- **7 genuine Razorpay `payment.failed` events reached the pipeline**, from real
  account `acc_TVyhpQlwZfwE8a`, all returning HTTP 200. All `payment_cancelled`
  at `payment_authentication`, across wallet and netbanking, Rs 100 each, with
  `prior_attempts` correctly accumulating 0 -> 6 across the same order. HTTP 200
  is itself proof that signature verification passed against the real dashboard
  secret, so the registered webhook and `.env` agree.
- **CORRECTION to a claim made in session 8.** I told the owner that in test mode
  a cancellation registers as a SUCCESS and would not fire `payment.failed`. That
  is true for **UPI only**. Live capture shows wallet and netbanking cancellations
  do emit `payment.failed` with `error_reason=payment_cancelled`. The owner had in
  fact already triggered 7 real failures while believing they had not. README and
  `trigger_failure.py` corrected.
- **Real payloads captured as a reference fixture**
  (`fixtures/reference_real_payment_failed.json`), redacted for account id, email,
  contact, vpa and cardholder name; field structure untouched.
  `tests/test_generator_fidelity.py` (10 tests) now asserts the synthetic
  generator against genuine provider output rather than against my reading of the
  docs. Result: envelope keys match exactly, and **no field present in a real
  payload is missing from our synthetic data** — the direction that matters, since
  a real-only field would be one DETECT is never tested against.
- **Catalogue gap found by live capture, now fixed.** `payment_cancelled` was
  catalogued for cards only, but the identical error tuple arrives on wallet and
  netbanking. Added `wallet_customer_cancelled_at_auth` and
  `netbanking_customer_cancelled_at_auth`, both marked DOCUMENTED because they
  were directly observed. Batch composition re-checked afterwards and still
  balanced.
- **Test-mode payments do not all appear in `payment.all`.** The API listed only
  the 1 captured payment while 7 failed ones existed. Do not use that endpoint to
  judge whether failures occurred; query our own `events` table or the ngrok
  inspector.
- **Not every checkout failure produces a `payment.failed` webhook.** A card
  attempt showing "declined by the bank" in the browser produced no webhook
  delivery and no payment attempt recorded against the Payment Link
  (`status=created`, empty `payments`). Wallet and netbanking cancellations are
  the empirically reliable triggers, confirmed 7 times. Use those; treat card
  declines as unreliable for generating live events.
- **Diagnosis method worth reusing.** To tell "our receiving path is broken" from
  "the provider never sent anything", push a synthetic signed delivery through
  the PUBLIC url. It returned 200/detected while the real attempt produced
  nothing, which isolated the gap to Razorpay's side rather than ours.

### Session 8 (triggering a real provider event)
- **Payment Links are created via the API, not the dashboard**
  (`app/trigger_failure.py`). The dashboard route depends on the Test/Live toggle,
  and a link created in one mode cannot be paid against the other. That produced
  Razorpay's misleading `"The id provided does not exist"` at
  `payment_initiation`. Diagnosis: the credentials authenticated fine in TEST mode
  but the account held 0 payments and 0 payment links, so the id being paid did
  not exist in the account those keys belong to. Creating the link from the same
  keys the project uses eliminates the mismatch entirely.
- **`trigger_failure.py` refuses to run with a non-`rzp_test_` key** and disables
  both SMS and email notification, so it cannot create a payable link or message
  a real person. It sets `notes.customer_id`, so DETECT resolves a real customer
  id from the live webhook rather than falling back to a contact hash.
- **Razorpay rejects contacts with repeated digit runs.** `+919999999999` was
  refused as obviously fake. Placeholder contacts must look plausible even when
  notifications are off.
- **In test mode, cancelling a payment records as SUCCESS.** So cancellation
  cannot be used to trigger `payment.failed`. Use `failure@razorpay` for UPI or
  the netbanking mock page's Failure option.
- **Added `/events/recent` to the local app only.** Answers "did that webhook
  arrive?" without needing an event id. Surfaces `provider_account_id`, which is
  what distinguishes a genuine Razorpay delivery (your real `acc_...`) from a
  replayed fixture (a generated one). Explicitly asserted absent from
  `app.tunnel`, since publicly it would let anyone enumerate events and read
  customer ids.

### Session 7 (public exposure)
- **Public webhook URL is live and verified end to end.**
  `https://prospectless-carlotta-unboding.ngrok-free.dev` -> `127.0.0.1:8001`
  (`app.tunnel`). Confirmed over the public internet: `/tunnel-health` 200,
  `/health` `/readiness` `/docs` `/openapi.json` all 404, unsigned POST 401
  `missing_signature`, and 60/60 signed events detected with 60 new event rows,
  60 new audit entries, 0 events missing an audit entry and 0 new split
  identities. Suffix is `.ngrok-free.dev`, not `.ngrok-free.app`.
- **Public round-trip latency: mean ~250 ms, p95 ~572 ms** (vs ~56 ms local).
  Comfortably inside the 60-second real-time target in `architecture.md`, so the
  tunnel is not a risk to that metric. Useful context for Known issue A: transport
  is negligible next to quiet-hours deferral, which is measured in hours.
- **`ngrok config add-authtoken` is a separate step from creating an account.**
  The agent was installed but unconfigured, which is why no tunnel would open.
  Worth remembering if the tunnel is ever set up on another machine.
- **README now includes a negative check for the tunnel.** A 200 from
  `/readiness` on the public URL means it is pointed at port 8000 and publishing
  the unauthenticated ops endpoints. Documented as an explicit "only the first of
  these should succeed" pair, because getting the port wrong is silent otherwise.

### Session 6 (Phase 2)
- **Customer timezone lives in `customers`, not on `EventRecord`.** Resolves
  Known issue B with no deviation from `architecture.md`. See above.
- **`decline_code` carries Razorpay's `error_reason`, not `error_code`.**
  `error_code` is almost always `BAD_REQUEST_ERROR` and carries no diagnostic
  signal. `error_source`, `error_step` and `error_description` are kept as
  columns on `events` because they are the only thing distinguishing an opaque
  `payment_failed` from a bank-side one, and `EventRecord` has no field for them.
  Nothing is lost, and whether DIAGNOSE reads them is a Phase 3 decision.
- **Signature verification fails closed and lives apart from signing.** With no
  secret configured every webhook is rejected. Verification (`app/signature.py`)
  is a separate module from the simulator's signing (`app/simulation/signing.py`)
  so a bug in one cannot be validated by the other.
- **Idempotency is a correctness requirement, not a nicety.** `event_id` is a
  UUID5 of the provider payment id and `provider_payment_id` is UNIQUE, because
  Razorpay retries deliveries and a duplicate row would double-count the headline
  amount-at-risk figure.
- **Unsupported Razorpay events are acknowledged with 200, not rejected.**
  Razorpay sends many event types to one URL; a non-2xx would make it retry an
  event we will never process. Distinguished in code from a malformed payload,
  which does get a 400.
- **Persistence was built in Phase 2 rather than deferred to Phase 6.** The
  checklist did not list it, but `EventRecord` cannot be populated without
  customer enrichment and `prior_attempts` cannot be derived without event
  history. Phase 6 still owns querying, metrics and the dashboard.
- **Schema is created with `create_all()`, not Alembic.** Fine for a disposable
  demo database; would not survive a real deployment, where a schema change
  against live data needs a migration and a rollback path.
- **BUG FOUND AND FIXED — split customer identity.** Razorpay omits `notes` on
  roughly a third of deliveries, so the same person arrived sometimes with an
  explicit `notes.customer_id` and sometimes with a contact-derived hash,
  creating two customer rows for one person. That silently defeats `max_retries`
  and `contact_frequency`, the rules that depend on seeing every attempt. Fixed
  by reconciling on contact before creating any customer, in both orderings. The
  first attempt only handled one direction and live replay caught it.
- **BUG FOUND AND FIXED — record/row customer mismatch.** `to_event_record` took
  the customer id from the parsed payload while the stored row used the reconciled
  one, so the row linked correctly but the record handed to DIAGNOSE and DECIDE
  named a different customer. `customer_id` is now an explicit argument, and a
  test asserts the record and the row always agree.
- **Email is only used to re-identify a customer when contact is absent.** Two
  people can share an email; merging them would pool attempt histories so
  `max_retries` would trip for someone never contacted, and the agent would
  abandon recoverable revenue.

### Session 5 (Phase 1)
- **Scenario provenance is recorded per scenario.** Razorpay documents the
  `error_reason` vocabulary but not which code/source/step accompany most
  reasons, so every scenario is tagged `documented` or `inferred`. Prevents an
  inferred combination later being cited as provider behaviour.
- **Guardrail ground truth is DERIVED from event data, not recorded on
  injection.** Found a batch containing an event with 5 prior attempts and zero
  elapsed time. Deriving means a label can never contradict the event, and
  events that trip two rules are captured correctly. A test recomputes the
  derivation independently.
- **`expected_guardrail_blocks` renamed to `expected_guardrail_failures`.** A
  failed quiet-hours check means *defer to the next allowed window*, not abandon;
  only `max_retries` and the hard stop mean stop. The old name would have pushed
  Phase 4 toward dropping recoverable revenue.
- **Method is chosen before the failure reason.** Sampling scenarios directly
  made cards 59% of an India-focused batch, because the mix was following
  catalogue size rather than market share. Verified against target across 60
  seeds.
- **`PaymentMethod` added to `schemas.py` in a separate provider-level section.**
  It comes from the Razorpay payment entity, not `architecture.md`. Kept below a
  divider so the architecture-mirroring section stays a faithful copy, and the
  contract tests continue to police only that section.
- **The generator's clock is injectable.** Timestamps are relative to `now`, so
  seed alone did not pin output. Also required for testing time-based guardrails
  in Phase 4.
- **Fixtures deliberately include a `card` sub-object and uninformative
  declines.** The card object carries `last4`/`network`/`iin`, which DETECT must
  strip; omitting it would leave constraint #1 untested. Blank-error and
  `payment_failed`-only events are included so the escalation path is exercised
  rather than bypassed. No PAN, CVV or expiry appears anywhere, and a test
  enforces that.
- **Contact details use reserved example domains.** Phone numbers are well-formed
  but invented, so the `TWILIO_WHATSAPP_TEST_RECIPIENTS` allowlist is what
  actually prevents messaging a stranger. A test asserts the domains.

## Known issues / open questions

### Raised in session 5 (Phase 1) — need decisions

O. **The headline recovered figure is INR 0 until somebody pays a link.** Not a
   defect and not a measurement: outcome confirmation is built, wired and covered by
   40 tests, and `amount_recovered` is deliberately writable only by a signed
   provider webhook. Nobody has paid a synthetic link, so there is nothing to
   report. The dashboard says "not confirmed (awaiting a provider webhook)" rather
   than showing 0.00.
   **To close it:** pay the live test-mode link the agent generated
   (`https://rzp.io/rzp/u7hNigG`, INR 499), with `payment_link.paid` subscribed in
   the Razorpay dashboard and ngrok pointed at `app.tunnel` on 8001 so the callback
   can reach us. Until then the honest phrasing is "the mechanism is proven, the
   money is not asserted".

P. **The demo batch is a 14-day backlog, not a live stream, and that is why 32 of
   76 events are withheld on age.** The generator spreads failures across a 14-day
   window while the hard stop is 7 days. In live operation an event arrives seconds
   after the failure, so the hard stop cannot fire on a first attempt — meaning the
   withheld share reflects fixture design rather than the agent's behaviour on real
   traffic. Either say so up front when presenting, or generate a batch with
   `window_days` inside the recovery window (the generator takes the parameter) and
   accept that the hard-stop scenario then stops being exercised. Do not quietly
   pick the flattering option without stating which was used.

N. **PARTLY ADDRESSED in session 6 — deferred sends are reported as their own
   category, but still nothing sweeps for them.** Phase 6 took the second of the
   two options: `deferred_to_allowed_window` is a distinct disposition in the
   metrics and on the dashboard, so a deferred send is visible rather than hidden
   inside a skip count. The gap that remains is real — no scanner queries
   `decisions.scheduled_for` for due work, so a deferred send is never actually
   dispatched later. For a batch demo this is the safe failure direction (nothing
   goes out at the wrong time), but it must not be described as "deferred to the
   next window" without adding that the second half is not built. The last batch
   run had 7 events in this state.

A. **RESOLVED in sessions 14-15 — two latency metrics, both implemented and
   reported.** `pipeline.process_event()` measures `decision_latency_ms` and
   `send_latency_ms` separately, `event_latencies` persists them, and both the
   metrics report and the dashboard show them side by side with only decision
   latency held against the 60-second target. Measured on 75 events: decision mean
   9.4s, p95 26.2s, max 30.3s, **0 over budget**. The original note is retained
   below because the reasoning still explains why there are two numbers.
   `architecture.md` → Real-time requirement asks for DETECT → EXECUTE (action
   sent) under 60 seconds. But ~36% of generated events arrive outside
   9am-8pm customer local time, and for those the *correct* behaviour is to
   defer the send to the next allowed window, not to hit 60 seconds. Measuring a
   single "detect→execute" number would score correct deferrals as failures and
   push toward violating a Non-Negotiable Constraint to make a metric look good.
   Proposed fix, needs sign-off: report two metrics —
   **decision latency** (detect → decision recorded), which should be <60s for
   every event and is the real proof of real-time operation, and
   **send latency** (detect → message actually sent), which is legitimately long
   when deferred. Phase 6 depends on this choice.

B. **RESOLVED in session 6 — customer timezone lives in the `customers` table,
   `EventRecord` unchanged.** Chose the option that needs no schema deviation,
   since `ai-workflow-rules.md` forbids improvising the schema. It turned out not
   to be optional anyway: `EventRecord` requires `tenure_days` and
   `past_failures`, which Razorpay does not send, so DETECT had to enrich from our
   own records regardless. Timezone rides along in the same place. When no profile
   exists, `profile_source="defaulted"` is recorded and the audit note states that
   quiet-hours evaluation rests on an assumed timezone.
   Original note retained below.
   **`EventRecord` has no field for customer timezone.** The quiet-hours rule is
   specified in *customer local time*, and `guardrails.check_quiet_hours()`
   takes a timezone argument, but `architecture.md`'s Event record schema has
   nowhere to carry one — and Razorpay's webhook does not send it. The
   simulation layer currently keeps it on its own customer profile, outside the
   webhook envelope, which is the honest modelling. Phase 2 needs a decision:
   add `customer_timezone` to `EventRecord` (a documented deviation from
   `architecture.md`), or have DETECT enrich from a customer table.

C. **~23% of events are expected to diagnose as `unknown`.** Deliberate, not a
   defect: Razorpay's own samples show failures whose only stated reason is
   `payment_failed`, and one card sample has every error field blank. Forcing
   those into a specific cause would be the exact guess the taxonomy exists to
   prevent. It does mean roughly a quarter of the batch escalates to human review
   instead of being auto-actioned. Confirm that is acceptable for the demo
   narrative before Phase 3; the alternative is lowering those scenario weights,
   which would make the batch less honest.

D. **22 of 75 events use fully documented Razorpay field tuples; 53 are
   inferred.** Razorpay publishes the `error_reason` vocabulary and the payload
   shape, but not which `error_code`/`error_source`/`error_step` accompany most
   reasons. Inferred combinations are realistic input, not evidence of provider
   behaviour, and every scenario records which it is. Do not cite an inferred
   tuple as "how Razorpay behaves".

M. **RESOLVED in session 15 — the denominator is split, not blended.**
   `EventRow.disposition` puts every event in exactly one mutually exclusive
   bucket (`contacted`, `retry_scheduled`, `deferred_to_allowed_window`,
   `withheld_by_guardrail`, `escalated_to_human`, `classifier_unavailable`,
   `dispatch_failed`), and the recovery rate is reported against both the whole
   batch and the actioned subset. Precedence is deliberate: a classifier outage is
   reported as itself rather than as the escalation it produced, so an operational
   failure stays distinguishable from a judgement. Last run: 40 withheld, 18
   escalated, 10 retry scheduled, 7 deferred, 0 outages. Original note retained
   below for the reasoning.
   **"Not actioned" is not the same as "not recovered", and Phase 6 must not
   conflate them.** Running the batch through DECIDE, only 17 of 75 events result
   in a customer being messaged. 22 are stopped by a guardrail, 19 escalate to a
   human, and 20 schedule a provider-side retry with no message at all. Every one
   of those is correct behaviour, but a naive "$ recovered / $ at risk" would read
   as a 77% failure.
   The denominator needs splitting at least three ways:
   - **actionable and actioned** — a message sent or a retry scheduled
   - **correctly withheld** — a stopping rule fired, or the cause was unknown and
     it went to a human. Compliance working, not revenue lost.
   - **classifier unavailable** — an operational failure (Known issue K), which
     belongs in neither of the above.
   Reporting one blended percentage would either overstate performance or make
   compliance look like failure. Needs a decision alongside Known issue A.

K. **RESOLVED IN PRACTICE in session 15 — the owner replaced the Gemini key.**
   On the new key with `gemini-3.1-flash-lite`, a full 75-event batch classified
   with **0 classifier outages**, and the eval scored 30/30 with 0 failures. The
   old key's 20-requests-per-day ceiling is simply gone. Two things from the
   original analysis still stand and are worth keeping: the evidence cache means
   API calls scale with scenario *variety* rather than batch size (75 events needed
   ~27 distinct classifications, and the eval showed 30 events costing 15 calls),
   and a quota failure is still marked `classifier_unavailable` so an outage can
   never be reported as cautious diagnosis. The new key's actual daily ceiling has
   not been measured, so this is "no longer biting" rather than "known to be
   large". Check before a demo. Original note retained below.
   **Gemini free-tier quota is the biggest risk to the final demo.** Measured,
   not guessed: the OLD key allowed **20 `gemini-2.5-flash` requests per DAY**
   (`GenerateRequestsPerDayPerProjectPerModel-FreeTier`, quotaValue 20), plus a
   5/minute cap. That is far below the ~27 distinct classifications one batch
   needs. Mitigations already in place:
   - Switched to `gemini-2.5-flash-lite`, which has a separate per-model bucket
     and lower latency. Even so, 20 of 75 events hit the wall on one run.
   - Caching on rendered evidence: 75 events need only 27 distinct
     classifications, so **API calls scale with scenario variety, not batch
     size**. A 100-event batch costs no more than a 75-event one.
   - Rate-limit-aware retry that honours the delay the API states, capped at 65 s.
   - A quota failure is marked `classifier_unavailable` in the audit trail so an
     outage can never be reported as cautious diagnosis.
   **Decision needed before Phase 7:** enable billing for the demo run, accept a
   batch small enough to fit the daily allowance, or pre-compute the
   classification cache ahead of the demo and replay from it. Without one of
   those, a live 75-event run will show a large block of events escalated for
   operational reasons.

L. **`checkout_friction` / `genuine_abandonment` accuracy depends on DETECT's
   enrichment, not on the model.** The prompt's Rule 2 keys entirely on
   `prior_attempts`, which Phase 2b derives from our own event history. If that
   enrichment is wrong the classification will be confidently wrong, and the model
   has no way to notice. Worth remembering when reading the 100% figure: those
   categories test the enrichment as much as the prompt.

H. **RESOLVED in Phase 2b — friction vs abandonment is enriched from our own
   history.** The finding stands: a `payment_link.expired` payload cannot
   distinguish the two, because the `payments` array was empty on every real link
   inspected including one that had been paid. DETECT now counts `payment.failed`
   events for the customer during the link's lifetime and puts the total in
   `prior_attempts`, with an audit note stating where the number came from and
   what it implies. DIAGNOSE (Phase 3) therefore reads `event_type` plus
   `prior_attempts`, and needs no schema change. Original note retained below.
   **`checkout_friction` and `genuine_abandonment` cannot be told apart from a
   `payment_link.expired` payload.** Measured, not assumed. Invoices are easier:
   `partial_payment` / `amount_paid` / `amount_due` are on the entity.

J. **8 future-dated events remain from before the timestamp fix.** Rows where
   `detected_at > received_at`, created by the pre-fix generator. Harmless to the
   code but they would make any detect-to-action latency negative for those
   events in Phase 6. Same disposition as Known issue F: clear them when the
   database is next reset. New batches add none — verified as a +0 delta.

I. **The abandonment envelope wrapper is INFERRED, not observed.** The entity
   shapes come from real API read-back, but `contains` and the
   `payload.<entity>.entity` nesting for `payment_link.expired` / `invoice.expired`
   follow the pattern documented for payment events. Confirming it means waiting
   for a real expiry. Worth verifying opportunistically: the invoice created
   during the shape probe expires in 3 days and will emit a real
   `invoice.expired` if that event is subscribed in the dashboard.

F. **32 duplicate customer rows remain from before the identity fix.** Created by
   live replays run while the split-identity bug was present. Harmless to the
   code, but they would skew Phase 6 per-customer metrics and make a demo show
   split identities. Not deleted unilaterally since it is data removal. Reset with:
   `docker compose down -v; docker compose up -d --wait` then
   `python -c "from app.db import init_db; init_db()"`, or truncate
   `audit_log`, `events`, `customers` in that order. Regenerating the batch
   afterwards is a single command, so nothing of value is lost.

G. **RESOLVED in session 7 — separate webhook-only app for public exposure.**
   `ngrok http 8000` would have published `/health`, `/readiness` and
   `/events/{id}/audit`, none of which have auth, and `/readiness` reports which
   credentials are configured. Rather than add auth to a demo tool or rely on
   spoofable forwarded headers, `app/tunnel.py` serves ONLY the
   signature-verified webhook route plus a contentless liveness probe, with
   interactive docs disabled. A request for `/readiness` there 404s because the
   route does not exist, so there is nothing to bypass. Tunnel port 8001
   (`app.tunnel`); keep `app.main` on loopback 8000. Verified live: ops paths
   404, unsigned webhook 401. Tests in `test_tunnel_app.py` assert the public
   path set exactly, so a route added later fails the build instead of quietly
   becoming internet-facing.

E. **Fixtures go stale.** Event timestamps are relative to generation time. A
   fixture older than 7 days trips the hard stop on every event and would report
   zero recovery while the pipeline works correctly. Regenerate before any demo
   run; `--now` pins the clock when a byte-stable file is wanted. A test asserts
   a fresh batch is not mostly outside the window.

### Earlier
1. ~~OPEN DECISION — payment provider~~ **RESOLVED: Razorpay.** See decisions log.
2. ~~Postgres availability~~ **RESOLVED: container via `docker-compose.yml`.**
   Not yet started/verified — needs `POSTGRES_PASSWORD` and a running daemon.
3. **RESOLVED — ngrok approved for the public webhook URL** (owner, session 5).
   Razorpay rejects `localhost` at webhook-setup time, so Phase 2 fronts
   `127.0.0.1:8000` with an ngrok tunnel for live test-mode failures, and replays
   the bulk batch through the same signature-verified handler locally. One code
   path, not two. Original note retained below for context.
   **Razorpay webhooks require a PUBLIC URL — localhost is rejected at setup.**
   Razorpay's dashboard will not save a `localhost` endpoint, so a genuinely
   webhook-driven demo needs a tunnel (`cloudflared` or `ngrok`) fronting
   `127.0.0.1:8000`. This matters because `architecture.md` -> Real-time
   requirement specifies webhook-driven, not polling. Proposed approach, needs
   owner sign-off: run the tunnel for a small number of *live* test-mode
   failures to prove the webhook path end to end, and replay the bulk 50-100
   batch through the same signature-verified handler locally. That keeps the
   path identical rather than building a second, non-webhook code path.
4. **Razorpay's decline-code simulation is thinner than Stripe's.**
   `architecture.md` picked Stripe partly for its decline tooling, and the
   Phase 1 checklist literally says "Stripe test-mode triggers". Razorpay test
   mode offers test cards and the `failure@razorpay` UPI id to force a failure,
   but not an on-demand generator for 5+ distinct decline reasons. Since
   `project-overview.md` already scopes the batch as **synthetic** events, the
   compliant equivalent is: synthetic fixtures carry the decline-code spread,
   plus a handful of real provider-generated failures to prove the live path.
   The Phase 1 wording needs updating to say Razorpay, not Stripe.
5. **Twilio WhatsApp sandbox only delivers to opted-in numbers.** Recipients
   must message the sandbox join code first. Readiness now warns when the
   allowlist is empty, because otherwise a batch reports sends that never
   arrived — which would overstate the headline recovery number.
6. ~~`TWILIO_FROM_NUMBER` not WhatsApp-formatted~~ **RESOLVED** by owner;
   `whatsapp_configured: true`.
7. **`POSTGRES_PASSWORD` is read only on FIRST boot of the volume.** Changing it
   in `.env` afterwards does not change the real database password. Recreate the
   volume (`docker compose down -v`, which destroys the audit log) or
   `ALTER USER`. Worth knowing before Phase 6 metrics depend on that data.
8. **Commit permission not granted.** `git init` done, nothing committed.

## What's needed from the repo owner
> Everything here requires an account or key that only the owner can create.
> Nothing below was faked, stubbed with a fictitious value, or skipped silently.

### Done — verified via `credential_report()`, no secret value read or echoed
- [x] `PAYMENT_PROVIDER=razorpay`
- [x] `RAZORPAY_KEY_ID` + `RAZORPAY_KEY_SECRET` (test mode, validator-confirmed)
- [x] `RAZORPAY_WEBHOOK_SECRET` → `payment_provider_configured: true`
- [x] `GEMINI_API_KEY` → `diagnose_llm_configured: true`
- [x] `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN`
- [x] `TWILIO_FROM_NUMBER=whatsapp:+14155238886` → `whatsapp_configured: true`
- [x] Postgres container decision + `docker-compose.yml`

- [x] `TWILIO_WHATSAPP_TEST_RECIPIENTS` (owner's own opted-in sandbox number)
- [x] `POSTGRES_PASSWORD` set, stale `DATABASE_URL` line removed
- [x] Container started and connection verified

Readiness now returns `missing_required_keys: []` and `warnings: []`.

### Still outstanding
| # | Needed | Unblocks |
|---|---|---|
| 1 | ~~Sign-off on synthetic fixtures carrying the decline spread~~ **APPROVED** | done |
| 2 | ~~Sign-off on the tunnel approach~~ **APPROVED — ngrok** | done |
| 3 | ~~Decision on where customer timezone lives~~ **RESOLVED, no schema change** | done |
| 4 | ~~Decision on split latency metrics (Known issue A)~~ **BUILT BOTH WAYS, no decision needed.** Decision and send latency are measured, stored and reported separately; only decision latency is held against the 60s target. 0 of 75 over budget | done |
| 5 | Confirm the ~23% `unknown` rate is acceptable (Known issue C). Last batch: 14 of 75 `unknown`, all escalated to a human, none from an outage | Phase 7 write-up |
| 9 | **Read one event's audit trail on `/dashboard` and say whether it lands in under 30 seconds.** The last `Definition of done` box; self-certifying it would be worthless | Phase 7 close-out |
| 10 | ~~Re-run the batch in-hours~~ **DONE.** 76-event run at 09:30 IST produced 1 genuinely contacted event with a real WhatsApp delivery | done |
| 11 | **Pay the live test-mode link** (`https://rzp.io/rzp/u7hNigG`) with `payment_link.paid` subscribed and ngrok on `app.tunnel`, to turn the recovered figure into an earned number (Known issue O) | headline metric |
| 12 | Decide whether to present the 14-day backlog batch as-is or generate one inside the recovery window — and say which was used (Known issue P) | demo narrative |
| 6 | ~~ngrok tunnel + real provider event~~ **DONE & VERIFIED.** 7 genuine `payment.failed` events from `acc_TVyhpQlwZfwE8a` detected via `https://prospectless-carlotta-unboding.ngrok-free.dev/webhooks/razorpay`, all HTTP 200 | done |
| 7 | Clean the 32 pre-fix duplicate customer rows before any demo (see Known issue F) | Phase 6 metrics |
| 8 | Go-ahead to create git commits | Phase 0 close-out |

Reminder: test-mode keys only. `config.py` rejects live keys at startup.

## Latest session summary
> Overwrite this each session: what was done, what's next.

**Session 1 — Phase 0 setup.**

Completed the `ai-workflow-rules.md` session-start checklist (all six context
docs read) before writing code, then scaffolded the backend.

Built: FastAPI app with `/health` and `/readiness`; `config.py` with test-mode
key enforcement and a credential-readiness report that names missing keys
without exposing values; `schemas.py` with all Pydantic models and the fixed
taxonomy; stub modules for DETECT/DIAGNOSE/DECIDE/EXECUTE and `guardrails.py`
that raise `NotImplementedError` naming their owning phase; placeholder
`prompts/diagnose.md`; `.env.example`, `.gitignore`, `README.md`, pinned
`requirements.txt`.

Verified: 25 tests pass, ruff clean, server boots and both endpoints return
correct payloads live. Included contract tests that parse `architecture.md` and
fail if the enums or the 8-row action table drift from the code.

Three spec ambiguities surfaced rather than silently resolved: the
parameterized action names, the `guardrail_checks_passed` vs constraint #5
conflict, and the Stripe/Razorpay provider choice. All logged above.

**Session 2 — credentials wired, Postgres containerised.**

Owner supplied `.env`: Razorpay test keys, Gemini key, Twilio credentials, no
SendGrid. Verified through `credential_report()` so no secret value was read or
echoed. Provider decision resolved to Razorpay; WhatsApp is the only live channel.

Added `docker-compose.yml` (Postgres 17-alpine, loopback-bound, healthchecked)
and extended readiness with three warning checks — `DATABASE_URL` vs `POSTGRES_*`
drift, non-WhatsApp Twilio sender, empty WhatsApp opt-in allowlist. The second
check immediately caught a real problem in the current `.env`. Tests 25 -> 29,
ruff clean.

Two Razorpay-specific constraints surfaced and logged (Known issues 3 and 4):
webhooks need a public URL, and Razorpay cannot generate 5+ decline reasons on
demand the way Stripe can. Neither breaks a Non-Negotiable Constraint, but both
change how Phase 1 and Phase 2 get built, and Phase 1's checklist wording still
says "Stripe".

**Session 3 — config simplified, Razorpay + WhatsApp fully configured.**

Owner completed `RAZORPAY_WEBHOOK_SECRET` and the WhatsApp sender prefix.
Readiness now reports `payment_provider_configured`, `diagnose_llm_configured`
and `whatsapp_configured` all true.

Owner challenged why both `POSTGRES_PASSWORD` and `DATABASE_URL` were needed
given the container defines the database. The challenge was correct — they were
duplicate copies of one fact. `DATABASE_URL` is now derived from `POSTGRES_*`
and only needed as an explicit override. Tests 29 -> 34, ruff clean. Also
documented that Postgres reads `POSTGRES_PASSWORD` only on first volume boot.

**Session 4 — Phase 0 closed out. Database live and verified.**

Owner finished the remaining credentials. `docker compose up -d` initially failed
to bind 5432; diagnosed as the machine's native `postgresql-x64-18` service
holding the port (not a Hyper-V reserved range, which was the other candidate).
Fixed by publishing the container on 55432 via `POSTGRES_PORT`, leaving the
native service untouched.

Verified live, not just assumed: connected through the derived URL to
PostgreSQL 17.11, correct database and role, write/read-back probe passed.
Added `tests/test_database_connectivity.py` (4 tests, `integration` marker) so
that check is permanent, including an assertion that the connection reached the
container rather than the native PG18. Tests 34 -> 38, ruff clean.

**Session 5 — Phase 1 data simulation layer built.**

Owner approved ngrok for the public webhook URL and asked for synthetic events
that reflect real ones. Researched Razorpay's published payload and error docs
first rather than inventing a plausible shape.

Built `app/simulation/`: a 29-scenario decline catalogue using Razorpay's real
`error_reason` literals, a generator producing envelopes that match the provider
field-for-field (paise amounts, unix timestamps, 14-char base62 ids,
method-specific `vpa`/`bank`/`card`/`acquirer_data` variants), HMAC signing so
replays go through the same verification path as live webhooks, and a fixture
writer that keeps the Razorpay envelope, our customer context, and ground truth
in three separate places. 50 tests, 90 total.

Realism is in the joint distribution, not just the field values: weighted price
points rather than random integers, UPI-dominant method mix, timestamps skewed to
waking hours, retry chains that keep one order and one amount with monotonic
attempt counts, and customer history correlated with tenure.

Three defects found and fixed during verification rather than after:
cards were 59% of an India-focused batch because the method mix was being decided
by catalogue size (now the method is chosen first, verified against target over
60 seeds); one event claimed 5 prior attempts with zero elapsed time (guardrail
labels are now *derived* from event data, so they cannot disagree with it); and
batches were only reproducible within a single second (the clock is now
injectable).

Five things logged for decision, two of which affect Non-Negotiable Constraints
or the metric definition: the 60s latency target contradicts quiet-hours
deferral, and `EventRecord` has nowhere to carry the customer timezone that the
quiet-hours rule depends on.

**Session 6 — Phase 2 DETECT built and verified end to end.**

Resolved Known issue B first, choosing the option that needs no schema deviation:
customer timezone lives in a `customers` table, and `EventRecord` stays a verbatim
copy of `architecture.md`. That turned out to be forced rather than optional,
since `EventRecord` requires tenure and past-failure counts the webhook does not
carry.

Built the webhook receiver, signature verification, normalization, and the
persistence plus audit-trail foundation the stage needs: `signature.py`, `db.py`,
`models.py`, `audit.py`, `detect.py`, `webhooks.py`, plus `replay.py` which
completes the batch-replay item deferred from Phase 1. Tests 90 -> 182.

Verified against a running server and the real Postgres container, not just in
unit tests: 75/75 events detected through the signed endpoint (mean 56 ms, p95
82 ms), a second replay returned 75 duplicates with amount-at-risk unchanged at
Rs 131,776, unsigned and bogus-signature requests both rejected 401, and 0 of 315
events lack an audit entry.

Two real bugs, both caught by live verification rather than by unit tests:
customer identity was splitting into two rows per person because Razorpay omits
`notes` on about a third of deliveries, which would have silently defeated
`max_retries` and `contact_frequency`; and `to_event_record` was reading the
customer id from the payload while the stored row used the reconciled one, so the
record handed downstream named a different customer than the row. The first fix
also only handled one ordering and needed a second pass. Both now have tests,
including a batch-level invariant that no contact maps to more than one customer.

A third issue worth remembering: the first live replay appeared to show the fix
not working, because uvicorn was started without `--reload` and was serving stale
code. Restarting it, and measuring duplicates as a before/after delta rather than
a time window, showed 0 new duplicates.

**Next:** Phase 1b (`checkout_abandoned`, `invoice_overdue`) to unlock the last
two taxonomy categories, then Phase 3 DIAGNOSE. Build order forbids skipping
ahead. Known issue A still needs a decision before Phase 6.
**Sessions 7-9 — public exposure and real provider events.**

Summarised in the decisions log above rather than here. In short: ngrok tunnel
stood up against a webhook-only app (`app/tunnel.py`) so the unauthenticated ops
endpoints stay private; 7 genuine Razorpay `payment.failed` events confirmed
end to end; real payloads captured as a reference fixture with fidelity tests.

**Session 10 — Phase 1b complete. All 8 root causes now producible.**

Built the `checkout_abandoned` and `invoice_overdue` generators on top of two real
Razorpay events, `payment_link.expired` and `invoice.expired`. That resolved a
constraint tension flagged in Phase 1: abandonment appeared to require polling,
since you cannot be notified that nothing happened, but because the provider emits
expiry events, `architecture.md`'s webhook-driven rule holds with no sweep.

Entity shapes came from creating a real test-mode invoice and reading it back
through the API, because the doc pages would not fetch and guessing field names
was not acceptable. Saved redacted as `fixtures/reference_real_entities.json` with
fidelity tests, which captured details guessing would have missed: payment links
use `0` for unset timestamps where invoices use `null`, and invoices duplicate
every customer field under a second name.

One finding shapes Phase 3. `checkout_friction` and `genuine_abandonment` cannot
be separated from a `payment_link.expired` payload, because the `payments` array
was empty on every real link inspected including one that had been paid. The
signal must be enriched from our own event history. Logged as Known issue H rather
than papered over with an invented attempts array.

Suite 208 -> 240 tests, ruff clean. Verified against the live endpoint rather than
assumed: `detected: 54, ignored: 18, duplicate: 3, failures: 0`, where the 18
ignored are exactly the new event types DETECT does not map yet.

**Next:** Phase 2b — teach DETECT the two new events plus the history enrichment —
then Phase 3 DIAGNOSE. Build order forbids skipping ahead.

**Housekeeping note:** this section says "overwrite each session" but has been
accumulating a per-session history instead. That history has proved useful for
tracing why decisions were made, but the file is now long. Say the word and it can
be collapsed to current-state-only.

**Session 11 — Phase 2b complete. All three event types now reach the pipeline.**

DETECT learned `payment_link.expired` and `invoice.expired`. `parse_envelope`
dispatches on event name to one of three entity parsers, all returning a single
`ParsedEvent` shape so persistence and audit remain one code path. Payment-only
fields are `None` for abandonment events, because nothing failed — the absence of
an error reason is itself the signal, and inventing a decline code would have told
DIAGNOSE a lie about what happened.

Known issue H is resolved. `_abandonment_attempt_history()` counts
`payment.failed` events for the customer between the link being issued and it
expiring, and that count lands in `prior_attempts`. Non-zero means the customer
tried and could not complete, which is friction; zero means they never engaged.
The audit note states where the number came from and what it implies, so a reader
can see it was inferred from our own history rather than read off the payload.
No schema change was needed: DIAGNOSE reads `event_type` plus `prior_attempts`.

Two correctness points worth recording. Amount at risk is now the outstanding
balance — `amount - amount_paid` for links, `amount_due` for invoices — because
counting the gross figure would have overstated the headline metric on any
part-paid item. And the recovery window starts at issue rather than expiry, so the
7-day hard stop measures from when money first became at risk instead of
restarting the clock.

A bug surfaced again through live replay rather than unit tests: an abandonment
event was dated in the future, because expiry was computed as creation plus
validity while creation was drawn from inside the observation window. Fixing it
exposed the same class of fault in the payment retry chains, which had been adding
gap hours without checking the result stayed inside the window. Both fixed, with a
regression test asserting no event is dated after the batch reference time. That
would otherwise have produced negative latency in Phase 6.

Verified live, not assumed: 75 events replayed through the signed endpoint gave
`detected: 75, ignored: 0, failures: 0`, adding 57 payment_failed, 14
checkout_abandoned and 4 invoice_overdue rows, every one with an audit entry. The
same batch dropped 18 events before Phase 2b. Suite 240 -> 271 tests, ruff clean.

The tripwire test written in Phase 1b did its job: it asserted `ignored` and was
built to fail once DETECT gained support. It failed, and has been flipped to assert
`detected` rather than deleted, so the endpoint keeps proving the whole path for
all three event types.

**Next:** Phase 3 — DIAGNOSE. Worth confirming Known issue C (the ~23% `unknown`
rate) before starting, since it sets how much of the batch escalates rather than
being auto-actioned.

**Session 12 — Phase 3 complete. DIAGNOSE measured at 100% on classified events.**

Wrote `prompts/diagnose.md` (v2) and `app/diagnose.py`. Four independent layers
stop a bad classification reaching DECIDE: the Gemini response schema, local
Pydantic validation, an event-type possibility check, and a confidence floor.
Retry once, then degrade to `unknown` — never raise, because dropping an event
would lose revenue with no trace.

The phase turned on one observation from an early probe. Asked to classify a
decline whose only stated reason was `payment_failed`, the model answered
`bank_risk_block` at confidence 0.7 and said in its own reasoning that it had
picked the more common possibility. That is a guess wearing a confidence score,
and had it guessed `insufficient_funds` the pipeline would have scheduled a retry
against a card the issuer refused. A numeric threshold cannot fix it, because the
same guess later returned at 0.90. The prompt had to teach that `unknown` is a
correct answer, with a worked counter-example.

Two things had to be fixed before any measurement meant anything. The free tier
allows 20 `gemini-2.5-flash` requests per DAY on this key, so an unpaced 30-event
run produced 24 rate-limit failures and an apparent 43% accuracy that measured the
quota rather than the prompt. And those failures were indistinguishable from
genuine escalations, which would have let an outage look like cautious behaviour.
Now a quota failure is marked `classifier_unavailable`, rate limits are retried
with the delay the API asks for, and the model was switched to
`gemini-2.5-flash-lite` for its separate quota bucket.

Caching needed a correction of my own making. I had been sending `amount`,
`tenure_days` and `past_failures` to the model on the vague grounds that they
"colour plausibility". No rule in the prompt referenced them, so they invited
invented correlations, and because they vary per event they made 75 events produce
74 distinct evidence sets. Removing them cut that to 27, which means API calls
scale with scenario variety rather than batch size.

Measured result: **55/55 correct on events the classifier reached**, across 6 of
8 categories, at a median 2.2 s. Two caveats stated in full above: `card_expired`
and `genuine_abandonment` were never reached because of quota and remain unverified
live, and one ground-truth label was corrected after the model disagreed with it,
which is defensible here but is exactly how benchmarks get gamed.

Suite 271 -> 283 tests, ruff clean.

**Next:** Phase 3b, wire DIAGNOSE in after DETECT so the audit trail gains a
`diagnose` entry — nothing calls it in the pipeline yet. Then Phase 4, DECIDE.
Known issue K (quota) needs a decision before the Phase 7 batch run.

**Session 13 — Phase 4 complete. DECIDE produces 0 stopping-rule violations.**

Built `app/guardrails.py` and `app/decide.py`. The action table holds all 8 rows
from `architecture.md`, and two structural tests guard the properties that define
the stage: one parses the doc's table so the code cannot drift from it, and one
reads `decide.py`'s own source to assert no LLM symbol is reachable from it. A
deterministic stage that could quietly become non-deterministic would be worse
than one that was never claimed to be deterministic.

The design question that took the most thought was what a guardrail failure
actually means. Treating all four as blocks would have discarded recoverable
revenue, because quiet hours does not mean "never contact this person", it means
"not at 3am". So `max_retries` and the 7-day `hard_stop` are terminal, while
`quiet_hours` and `contact_frequency` move `scheduled_for` to the next allowed
moment. `GuardrailKind` records which is which so the two cannot be confused.

A second distinction: not every rule governs every action. Quiet hours protects a
human from being disturbed, so it has no bearing on a silent provider-side retry
or an internal escalation. All four checks still run and are still recorded for
every event, because constraint #5 requires the trail to show the check happened —
applicability decides whether a failure changes the outcome, never whether the
check runs. Escalation to a human deliberately survives terminal guardrails, since
it involves no contact and no charge and is most warranted exactly when automation
has run out.

Payday-aware retry timing was not faked. `architecture.md` asks for it "if data
available", no payday data exists, so `insufficient_funds` uses a flat configurable
interval and payday awareness stays a Phase 8 stretch goal.

Verified against the real 75-event batch rather than unit tests alone:
**0 stopping-rule violations**, re-derived independently of the code that enforced
them, and byte-identical decisions on re-run. Suite 283 -> 365 tests, ruff clean.

One finding that shapes Phase 6, logged as Known issue M: only 17 of 75 events
result in a customer being messaged. 22 are stopped by a guardrail, 19 escalate to
a human, 20 schedule a silent retry. All correct behaviour, but a naive
"$ recovered / $ at risk" would read that as a 77% failure. The denominator has to
separate "correctly withheld" from "failed to recover".

**Next:** `Phase 3b` — orchestration — should come before Phase 5. Nothing yet
chains DETECT -> DIAGNOSE -> DECIDE, so the audit table still holds only `detect`
entries and the end-to-end trail the demo depends on does not exist yet.

**Session 14 — Phases 3b and 5 complete. All four stages now chained.**

Built `app/pipeline.py` (orchestration), `app/channels.py` (Twilio WhatsApp and
Razorpay payment links, each with a dry-run fallback) and `app/execute.py`, plus
three tables so every stage persists its own record. A signed webhook delivery now
runs all four stages inline and writes four audit entries, which is the end-to-end
trail the demo depends on and which did not exist before this session.

The session's main finding was a contradiction between two decisions that were
each correct in isolation. Session 13 made `quiet_hours` and `contact_frequency`
*deferrable*, meaning DECIDE leaves `blocked_reason` empty and moves
`scheduled_for` forward. Session 13 also made `blocked_reason` the signal EXECUTE
keys on. Together they meant a deferred contact had nothing to stop it, so EXECUTE
would have sent the message immediately — at 3am if that is when the event
arrived, which is exactly what the rule prevents. EXECUTE now has two gates:
cancelled, and not yet due. This also qualifies a claim from Phase 4: "0
stopping-rule violations" was measured at the DECIDE layer, and end to end it did
not hold until this fix. The claim is true of DECIDE and was never an end-to-end
measurement.

Two smaller correctness problems came out of writing the tests rather than the
code. The existing webhook endpoint tests would have started making real Gemini,
Razorpay and Twilio calls the moment the pipeline was wired in, because those
modules resolve settings by calling `get_settings()` themselves and ignore
FastAPI's dependency override — so a populated `.env` would have spent live quota
on every test run. And `execute_action` guarded link creation but not the send,
contradicting its own documented promise that no dispatch problem escapes.

EXECUTE is tested mostly for what it must *not* do. Strict test doubles raise on
any provider call beyond "create a hosted link" and "send a message", so a branch
that reached for a charge API fails a test rather than a compliance review. A dry
run is never counted as a send, `amount_recovered` stays null until a provider
webhook confirms payment, and an allowlist refusal is a skip rather than a
requeued failure, because a number that never opted in will not accept a retry.

Verified live rather than assumed, and the verification tool itself misled first:
the replay client's 10-second timeout was shorter than DIAGNOSE's own rate-limit
backoff, so it reported `transport_error` for three requests the server answered
200. Raised to 45s. The rows themselves were clean — 3 events, 4 audit stages
each, 0 violations, nothing charged, nothing marked recovered. One event received
a genuine `insufficient_funds` classification at 0.9 confidence; the other two hit
the exhausted daily quota and degraded to audited escalations, so the degradation
path fired for real and not only under test.

Suite 419 -> 608 tests (0 skipped with the container up), ruff clean.

**Next:** Phase 6 — audit trail and metrics. Three decisions are now blocking it:
Known issue A (how to report the two latencies, now that both are measured),
Known issue M (splitting the recovery denominator so correct restraint is not
scored as failure) and Known issue N (deferred sends are recorded but nothing
sweeps for them yet). Known issue K, the Gemini daily quota, needs a decision
before any live batch run — today's quota is spent, so a full run right now would
classify roughly one event and escalate the rest for operational reasons.

**Session 15 — Phase 6 complete. Metrics, structured logging and a dashboard.**

Built `app/metrics.py`, `app/dashboard.py` and `app/logging_setup.py`, plus an
`event_latencies` table. Suite 608 -> 708 tests, ruff clean.

The finding of the session was that **the latency metric could not be derived from
the data we were already storing**, and would have silently reported a perfect
score. `events.received_at`, `decisions.decided_at` and
`execution_results.executed_at` all default to `func.now()`, which Postgres
resolves to the *transaction* start time, and the pipeline writes all four stages
in one transaction. So all three columns hold the identical instant and any
subtraction between them is exactly zero. A dashboard built on that would have
announced 0 ms end-to-end latency and looked like a triumph. The measured
`perf_counter` figures are now persisted instead.

The design decision that took the most thought was how to count violations. The
easy implementation counts decisions whose recorded guardrail checks all passed,
which is circular — it asks the enforcing code to grade itself, so a bug in
`guardrails.py` would be invisible in exactly the metric meant to catch it. So each
rule is reconstructed from raw data and tested against what actually happened,
including converting every send time into the *customer's* timezone, because
checking quiet hours in UTC would wave through a 3am message in Kolkata. A test
feeds the checker a row whose recorded flags all claim success while the raw data
shows a contact nine days late, and asserts it is still caught. Two violation
classes only exist this way at all: contact frequency is cross-event, and
`sent_before_due` is the end-to-end form of the session-14 deferral bug.

The owner replaced the Gemini API key mid-session, which forced a model change and
paid off. The new key 404s on `gemini-2.5-flash-lite` ("no longer available to new
users"). Candidates were probed through the real prompt and validation layers
rather than trusting the API's own recommendation: `gemini-3.5-flash-lite` and
`gemini-flash-lite-latest` both return 400 against our response-schema request, and
3.5's apparent pass on the hard case was just the classifier-unavailable fallback
rather than an answer. `gemini-3.1-flash-lite` classifies correctly *and* still
answers `unknown` on an opaque decline, which is the behaviour session 12 had to
fight for. It scored **30/30 with 0 failures** on the eval, which closes the Phase 3
caveat that two categories had never been verified live, and Known issue K goes from
biggest demo risk to no longer biting.

Two smaller fixes: `ExecutionRecord.executed_at` was falling through to its column
default rather than being set from the outcome, so the row disagreed with the audit
trail about when the action ran. And the dashboard's XSS test could not find its
escaped payload, which turned out to be because the DIAGNOSE reasoning
`ui-context.md` asks for was not being rendered at all.

Verified on a fresh 75-event batch replayed through the signed endpoint:
**100% audit coverage** (75/75 with all four stages), **0 stopping-rule
violations**, **0 classifier outages**, decision latency mean 9.4s against a 60s
budget with **0 events over**, and a 648 KB dashboard rendering all of it with no
`<script>` tag anywhere.

Two things about that batch are worth stating rather than glossing. INR 0 recovered
is correct: `amount_recovered` is only ever set by a provider webhook confirming a
payment, and nobody clicks a synthetic link. And 40 of 75 events were withheld by a
guardrail, almost all on age, because a generated batch spreads failures over 14
days while the hard stop is 7 — the rule working, but it makes a thin demo. The run
also happened at 23:15 IST, outside the contact window, so all 7 contactable events
were deferred and no real link or message was created.

**Next:** Phase 7 — the write-up. Four of the five `Definition of done` boxes are
provable now; the fifth asks whether a stranger can read one event's trail and
understand it in under 30 seconds, and that needs a person who has not seen the code
to open `/dashboard` and say. Before the write-up, re-run the batch in-hours so it
reports contacted events, and consider generating one whose events sit inside the
7-day window.

**Session 16 — Phases 7 and 8 complete. The build is functionally done.**

Wrote `RESULTS.md`, ran the batch in-hours, built outcome confirmation and
payday-aware retry timing, and declined promise-to-pay with reasons. Suite
708 -> 783 tests, ruff clean.

The session turned on something found while writing the results document rather
than while writing code: **the headline metric had no writer.** "$ recovered / $ at
risk" is the first of `project-overview.md`'s four success metrics and the whole
pitch ends "then proves how much money it recovered" — but `amount_recovered_minor`
was set by nothing, anywhere. Not zero because nothing was recovered; zero because
no code path was capable of changing it. `architecture.md`'s pipeline diagram has
the closing arrow, *webhook confirms outcome -> recovered-$ counter updated*, and it
had simply never been implemented. Publishing a dashboard whose headline figure was
permanently zero would have been shipping a broken metric with a straight face, so
it was built as a logged scope addition.

Three decisions in it are load-bearing. Only a signed provider webhook may write
that number, because EXECUTE knows it sent a message but not whether anyone paid.
Attribution is stored at three strengths rather than blended, since paying through a
link the agent sent is a far stronger claim than an order being captured later.
And one payment credits exactly one event — a retry chain holds several at-risk
events for one order, so crediting each would have multiplied the headline by the
length of the chain, inflating the metric by precisely the behaviour the agent
exists to handle.

The routing detail matters more than it looks: confirmation runs *before* DETECT.
DETECT would have classified a paid event as unsupported and answered 200 "ignored",
which fails completely silently — every request looks successful while the money is
never credited. There is now a test whose only job is to assert that status is not
"ignored".

Reporting got one thing more honest. The batch showed 8 events where the agent
diagnosed the cause, chose an action and created a *real* Razorpay-hosted payment
link, and only the final delivery was withheld because the number is not on the
Twilio sandbox allowlist. Those were landing in a generic "skipped" bucket, which
hid the difference between an environment limitation and a decision about the
customer. They now have their own disposition.

An in-hours run produced the first genuinely earned `contacted` event: DIAGNOSE said
`card_expired`, DECIDE chose `send_update_payment_method_link`, EXECUTE created a
real test-mode link and Twilio delivered the message. Final numbers on 76 events:
**100% audit coverage, 0 stopping-rule violations, 0 classifier outages, all 8 root
causes classified, 0 events over the 60-second decision budget**, INR 223,975 at
risk, 1 customer messaged and 8 more prepared and withheld.

For Phase 8, payday-aware retry was built without inventing the data it needs — a
sparse `customer_paydays` table with a `source` column, nothing inferred from
payment history because there are no successful payments to infer from, and a test
asserting the no-payday path is identical to the old flat interval since that is
what actually runs. Promise-to-pay was declined rather than half-built: it needs a
customer to make a promise, which needs an inbound channel that does not exist, so
the table would have had no writer.

**Next:** two things, neither of which I can do alone. Someone who has not seen the
code needs to read one event's trail on `/dashboard` and say whether it lands in
under 30 seconds — the last `Definition of done` box, and self-certifying it would
be worthless. And paying the live test-mode link the agent generated
(`https://rzp.io/rzp/u7hNigG`) would turn the recovered figure from a proven
mechanism into an earned number. Commits have still never been authorised.
