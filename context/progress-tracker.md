# Progress Tracker — AI Revenue Recovery Agent

> Update this file after every completed task. This is the source of truth
> for what's actually built vs. planned. Don't let it go stale.

## Status: Phases 0, 1, 1b, 2, 2b, 3, 3b, 4 and 5 COMPLETE (commits still pending)

## Current phase
608 tests passing (0 skipped with the container up), ruff clean. **All four stages
are now chained.** A signed webhook delivery runs DETECT -> DIAGNOSE -> DECIDE ->
EXECUTE inline and writes four audit entries, so the end-to-end trail the demo
depends on exists for the first time.

Verified live against the running server, not only in tests: 3 events through the
signed endpoint, 4 audit stages each, **0 stopping-rule violations**, nothing
charged and nothing marked recovered. One event got a genuine
`gemini-2.5-flash-lite` classification (`insufficient_funds`, 0.9); the other two
hit the exhausted daily quota and degraded to audited escalations, which is the
designed path firing for real rather than in a test.

Next up is `Phase 6 — audit trail + metrics`. Known issue K (Gemini quota) is
still the main demo risk and now needs a decision, since a live batch run today
would classify roughly one event and escalate the rest for operational reasons.
Known issues A and M both still gate how Phase 6 computes its headline numbers,
though A's implementation half is now done.

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
- **2 of 8 categories are unverified against the real model.** `card_expired`
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

### Phase 6 — Audit trail + metrics
- [ ] Structured logging in place for all four stages
- [ ] Audit log queryable per event_id
- [ ] Metrics computed: $ recovered / $ at risk, detect→execute latency, guardrail violations (should be 0)
- [ ] Simple dashboard or report view built

### Phase 7 — Full batch run (v1 done)
- [ ] Full synthetic batch run end to end
- [ ] All 4 `Definition of done` items from `project-overview.md` checked off
- [ ] Results written up with headline numbers

### Phase 8 — Stretch goals (only after Phase 7)
- [ ] Promise-to-pay tracker
- [ ] Payday-aware retry timing
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

N. **Deferred sends are recorded but nothing picks them up.** EXECUTE now
   correctly holds a contact whose `scheduled_for` is in the future (session 14),
   and the due time is persisted on `decisions.scheduled_for`. But no scanner
   queries for due work, so a deferred send is never actually dispatched. For a
   batch demo this is arguably fine and is certainly the safe failure direction —
   nothing is sent at the wrong time — but it must not be described as "deferred
   to the next window" without saying that the second half is not built. Phase 6
   should either add a due-work sweep or report deferred events as their own
   category, alongside Known issue M's split denominator.

A. **The 60-second latency target conflicts with the quiet-hours rule.**
   **Implementation half DONE in session 14** — `pipeline.process_event()` records
   `decision_latency_ms` and `send_latency_ms` separately, so nothing now forces a
   blended number. The reporting decision below is still open.
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

M. **"Not actioned" is not the same as "not recovered", and Phase 6 must not
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

K. **Gemini free-tier quota is the biggest risk to the final demo.** Measured,
   not guessed: this key allows **20 `gemini-2.5-flash` requests per DAY**
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
| 4 | Decision on split latency metrics (Known issue A) | Phase 6 |
| 5 | Confirm the ~23% `unknown` rate is acceptable (Known issue C) | Phase 3 |
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
