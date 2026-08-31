# Code Standards — AI Revenue Recovery Agent

## Language & structure
- Backend: Python 3.11+ (FastAPI)
- Database: Postgres (via SQLAlchemy or your ORM of choice) — used for both
  the event pipeline tables and the audit log, so it's queryable for metrics
- Frontend: React, single dashboard page (see `ui-context.md`) — no need for
  a heavy framework layer (Next.js etc.) unless you already have one set up
- One module per pipeline stage: `detect.py`, `diagnose.py`, `decide.py`,
  `execute.py`. No stage's logic lives inside another stage's file.
- Shared schemas live in `schemas.py` (or `schemas/` if it grows) — use
  Pydantic models matching `architecture.md`'s JSON schemas exactly.

## Naming
- Root-cause enum values, action names, and event types must match
  `architecture.md` **verbatim** (e.g., `insufficient_funds`, not `low_funds`
  or `InsufficientFunds`). Copy-paste from the doc, don't retype.
- Function names describe the stage + verb: `detect_event()`,
  `diagnose_root_cause()`, `decide_action()`, `execute_action()`.

## Guardrails are code, not comments
- Every guardrail in `architecture.md` (max retries, quiet hours, contact
  frequency, hard stop after 7 days) must be an actual function that returns
  pass/fail — never a comment saying "remember to check this."
- Guardrail functions live in `guardrails.py` and are unit-tested independently
  of the DECIDE stage that calls them.

## Logging & audit trail
- Every stage writes a structured log entry (JSON, one line per event per
  stage) to the audit table/log — not print statements.
- Log entries must include: `event_id`, `stage`, `timestamp`, `input_summary`,
  `output_summary`, and for DECIDE specifically, every guardrail check result.
- Never log raw card data, full card numbers, or CVVs — this should never
  exist in your system, but the rule is here as a hard stop if it ever
  almost does.

## LLM calls (DIAGNOSE stage only)
- Provider: Gemini API (e.g. `gemini-2.5-flash` or current free-tier flash
  model) — fast/cheap is correct here since DIAGNOSE is bounded
  classification, not open-ended reasoning. Using free-tier API credits for
  this build; treat the model choice as swappable, not load-bearing.
- Use Gemini's structured-output / JSON-mode features to constrain the
  response shape at the API level where possible, in addition to validating
  against the Pydantic model after the call.
- Use a fixed, versioned system prompt stored in `prompts/diagnose.md` — not
  inlined as a string in code, so it can be reviewed and diffed like any
  other artifact.
- Always request structured output (JSON matching the Diagnosis schema) and
  validate it against the Pydantic model before proceeding. Reject and retry
  once on invalid output; escalate to `unknown` on second failure.
- Never let the LLM call any payment-provider API directly. It classifies;
  it does not act.

## Error handling
- A failed EXECUTE (e.g., email provider down) must not silently drop the
  event — it goes to a retry queue or `escalate_to_human_review`, logged
  either way.
- Webhook signature verification is mandatory for all provider webhooks
  (Stripe/Razorpay) — reject unsigned/invalid payloads before they enter
  DETECT.

## Testing
- `pytest` for unit tests, one test file per module (`test_diagnose.py`, etc.)
- At minimum: one test per taxonomy category in DIAGNOSE, one test per action
  in DECIDE, one test per guardrail in `guardrails.py`.
- One integration test that runs a synthetic event through all four stages
  and asserts an audit log entry exists for each stage.

## Secrets & config
- All API keys (Stripe, Twilio/SendGrid, Gemini) in `.env`, never
  committed. `.env.example` documents required keys with placeholder values.
- Test-mode keys only. No production payment credentials in this project,
  ever — this is explicitly a test-mode/demo build (see `project-overview.md`).

## Commit hygiene
- One pipeline stage or one clearly-scoped fix per commit.
- Commit message references the stage: `[diagnose] add sca_abandoned category`
