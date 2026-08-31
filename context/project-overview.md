# Project Overview — AI Revenue Recovery Agent

## One-line pitch
An agent that detects revenue at risk in real time (failed payments, abandoned
checkouts, overdue invoices), diagnoses the root cause, and executes a
**bounded, compliant recovery workflow** that routes the customer to the
fastest legitimate path back to a completed payment — then proves how much
money it recovered.

## Chosen direction (lock this before building)
> **Payment degradation → root cause → recovery action**
> Voice is explicitly out of scope — no voice channel, in v1 or as a stretch goal.

Do not build the other five directions. If scope creep happens, cut back to this one.

## The problem, precisely
Revenue leaks in discrete, detectable events:
- A subscription renewal or one-off payment fails (`payment_intent.payment_failed`)
- The decline has a *cause* (expired card, insufficient funds, bank risk block,
  3DS/SCA abandonment, network error)
- Each cause has a *different correct response* — one-size-fits-all retry/dunning
  emails waste the recovery window or annoy the customer

## What the agent actually does (and does NOT do)
**Does:**
- Listens for failure events in real time (webhook-driven, not polling)
- Classifies the decline into a fixed root-cause taxonomy
- Chooses one action from a small, pre-approved action set
- Executes that action (send link, send message, place voice call, schedule retry)
- Tracks whether the customer completed payment afterward
- Logs every step for audit

**Does NOT do** (see `architecture.md` → Non-Negotiable Constraints):
- Never touches, stores, or resubmits raw card data
- Never bypasses 3D Secure / SCA — the customer always completes auth themselves
- Never retries a charge without a valid retry-eligible path or fresh customer action
- Never contacts a customer outside allowed hours/frequency

## Success metric ("the bar")
Run the full pipeline against a batch of simulated failure events and report:
1. **$ recovered / $ at risk** (headline number)
2. **Time from failure detected → recovery action sent** (real-time proof)
3. **0 compliance/stopping-rule violations** across the batch
4. **100% audit trail coverage** — every event traceable end to end

## Scope for v1 (MVP — build this first, nothing more)
- Payment provider: Stripe test mode (or Razorpay test mode if targeting India/UPI)
- Batch size: 50–100 synthetic failure events across 4–5 decline-code categories
- Channels: email or SMS/WhatsApp stub only — no voice channel
- One dashboard/log view showing the batch results and audit trail

## Stretch goals (only after v1 works end to end)
- Promise-to-pay tracking for B2B-style invoices
- Payday-aware retry timing for insufficient-funds cases

## Definition of done
- [ ] Can replay a batch of N failure events through the full pipeline
- [ ] Every event has a diagnosis, a decision, an action, and a logged outcome
- [ ] Recovery rate and $ recovered are computed and displayed
- [ ] No event violates a stopping rule (checked programmatically, not by eye)
- [ ] A stranger can read the audit log for any single event and understand
      what happened and why, in under 30 seconds
