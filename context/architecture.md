# Architecture — AI Revenue Recovery Agent

## Pipeline (four stages, each independently testable)

```
Stripe/Razorpay webhook (payment_intent.payment_failed)
        |  arrives in real time, seconds after failure
        v
┌───────────────┐
│  1. DETECT    │  webhook receiver -> normalized event record
└───────────────┘
        v
┌───────────────┐
│  2. DIAGNOSE  │  LLM classifies decline into fixed root-cause taxonomy
└───────────────┘
        v
┌───────────────┐
│  3. DECIDE    │  rules engine maps (cause, context) -> ONE action from
│               │  a small enumerated action set, after guardrail checks
└───────────────┘
        v
┌───────────────┐
│  4. EXECUTE   │  fires the action (link/message/call/scheduled retry)
└───────────────┘
        v
   webhook confirms outcome -> audit log + recovered-$ counter updated
```

Each stage is a separate function/module with its own input/output schema.
No stage calls the payment provider's charge API directly except EXECUTE,
and EXECUTE never resubmits a card — see Non-Negotiable Constraints.

## Data schema

### Event record (created at DETECT)
```json
{
  "event_id": "uuid",
  "customer_id": "string",
  "event_type": "payment_failed | checkout_abandoned | invoice_overdue",
  "decline_code": "string | null",
  "amount": "number",
  "currency": "string",
  "prior_attempts": "int",
  "customer_history": { "tenure_days": "int", "past_failures": "int" },
  "detected_at": "iso8601 timestamp"
}
```

### Diagnosis (created at DIAGNOSE) — fixed taxonomy, do not let the LLM invent categories
```json
{
  "event_id": "uuid",
  "root_cause": "card_expired | insufficient_funds | bank_risk_block | sca_abandoned | network_error | checkout_friction | genuine_abandonment | unknown",
  "confidence": "0.0-1.0",
  "reasoning": "string, one sentence, logged for audit"
}
```

### Decision (created at DECIDE)
```json
{
  "event_id": "uuid",
  "action": "one of the enumerated ACTION_SET (below)",
  "channel": "email | sms | whatsapp | none",
  "scheduled_for": "iso8601 timestamp",
  "guardrail_checks_passed": ["max_retries", "quiet_hours", "contact_frequency"],
  "blocked_reason": "string | null"
}
```

### Execution result (created at EXECUTE)
```json
{
  "event_id": "uuid",
  "executed_at": "iso8601 timestamp",
  "delivery_status": "sent | failed | skipped",
  "customer_outcome": "recovered | pending | failed | expired",
  "amount_recovered": "number | null"
}
```

## Fixed action set (DECIDE may only choose from this list)
| root_cause | action | notes |
|---|---|---|
| card_expired | send_update_payment_method_link | no retry attempt |
| insufficient_funds | schedule_retry(+N days) | payday-aware if data available |
| bank_risk_block | escalate_to_human_review | never auto-retry same card |
| sca_abandoned | send_fresh_auth_link | customer must complete 3DS themselves |
| network_error | schedule_retry(+1 hour) | single quiet retry, then stop |
| checkout_friction | send_reminder(1x) | no repeat unless customer re-engages |
| genuine_abandonment | send_reminder(1x), then stop | do not chase further |
| unknown | escalate_to_human_review | never guess an action |

## Non-negotiable constraints (build guardrails around these, not just documentation)
1. **No raw card data ever touches your system.** All charges/retries go
   through the provider's tokenized APIs only.
2. **No bypass of 3D Secure / SCA.** Any action that requires authentication
   must produce a link/prompt for the *customer* to complete, never an
   automated completion.
3. **No silent retries.** Every retry must be either provider-sanctioned
   (e.g., Stripe's own smart retry logic you're allowed to configure) or
   preceded by fresh customer action (they clicked a link, approved a mandate).
4. **Stopping rules are enforced in code, not by prompt instruction.**
   - Max 3 recovery attempts per event
   - Max 1 contact per 24 hours per customer
   - No contact outside 9am–8pm customer local time
   - Hard stop after 7 days from first failure
5. **Every guardrail check result is logged**, even when it passes — the
   audit trail needs to show the check happened, not just the outcome.
6. **"Agent-controlled" means the agent controls the decision and routing,
   not the payment session.** The agent never holds, resumes, or re-submits
   a transaction itself. Every recovery action hands off to either the
   provider's own sanctioned retry mechanism (e.g., Stripe Smart Retries) or
   a fresh, customer-initiated action (clicking a link, approving a mandate
   in their bank app). If a design ever implies the agent is "resuming" a
   transaction on the customer's behalf, that's a signal the design has
   drifted from this constraint — stop and re-read this section.

## Real-time requirement
"Real time" = webhook-driven, not polling. Target: DETECT → EXECUTE
(action sent) in under 60 seconds for the demo batch. Log latency per event
so you can report it as a metric.

## Suggested stack
- **Backend**: Python 3.11+ (FastAPI)
- **Payment provider**: Stripe test mode (best decline-code simulation tooling)
- **LLM**: Gemini API for DIAGNOSE stage only — keep DECIDE as
  deterministic rules code, not an LLM call, so it's auditable and bounded.
  Using free-tier API credits for this build; swappable later since the
  prompt lives in its own versioned file and output is schema-validated
  (see `code-standards.md`)
- **Storage**: Postgres for event/audit log — must be queryable for
  your final metrics report
- **Frontend**: React for the batch dashboard (see `ui-context.md`)
- **Messaging**: SendGrid/Twilio (free-tier credits) for email/SMS stubs —
  no voice channel

## What NOT to build
- No custom card-capture UI (use provider's hosted Checkout/Payment Links)
- No LLM call inside DECIDE — keep the mapping table auditable
- No cross-customer "campaigns" — this is per-event recovery, not marketing
