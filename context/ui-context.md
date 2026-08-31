# UI Context — AI Revenue Recovery Agent

## Scope reminder
This is primarily a backend/agent project. The UI is a thin reporting layer,
not a product surface — don't over-invest here. One page is enough for v1.

## Who sees this UI
You, in a demo. Possibly judges/reviewers. Not real end customers — customer-
facing surfaces are just provider-hosted payment links and messages (see
`architecture.md`), not custom UI you build.

## v1 UI: single-page batch dashboard

### Purpose
Show the headline numbers and let someone drill into any single event's
audit trail, to prove the pipeline actually ran and did the right thing.

### Layout (top to bottom)

**1. Headline metrics bar**
- $ recovered / $ at risk (with %)
- Total events processed
- Detect → execute latency (avg, and max)
- Guardrail violations (should read 0 — make this visually obvious if nonzero)

**2. Batch table**
One row per event, columns:
- Event ID (short/truncated)
- Event type (payment_failed / checkout_abandoned / invoice_overdue)
- Root cause (from DIAGNOSE)
- Action taken (from DECIDE)
- Channel
- Outcome (recovered / pending / failed / expired)
- Amount
- Latency (detect → execute, in seconds)

Sortable/filterable by outcome and root cause if time allows; static table
is fine for v1.

**3. Event detail view (click a row)**
Shows the full audit trail for that one event, stage by stage:
- DETECT: raw event summary, timestamp
- DIAGNOSE: root cause, confidence, one-line reasoning
- DECIDE: action chosen, guardrail checks (all of them, pass/fail, not just
  the ones that mattered)
- EXECUTE: delivery status, customer outcome, amount recovered

This view is the actual proof of "audit trail" — it needs to read clearly
to someone who has never seen the code.

## Visual style
- No design system needed. Plain, readable, functional — think internal
  ops tool, not consumer product. A simple HTML page or a Streamlit/
  Gradio app is enough. Don't spend build time on visual polish here;
  spend it on the pipeline.
- Use color only functionally: green/red for recovered/failed outcomes,
  a clear red flag for any guardrail violation. Nothing decorative.

## What NOT to build for v1
- No customer-facing UI (payment pages, email templates beyond plain text/
  basic HTML) — use the provider's hosted Checkout/Payment Link pages
- No auth/login system — this is a local demo tool
- No real-time-updating dashboard (websockets etc.) — a "run batch, then
  view results" flow is sufficient; real-time-ness is proven by the
  latency metric, not by the UI updating live
- No mobile responsiveness — desktop demo only

## Stretch (only after v1 dashboard works)
- Live-updating view if you do build a true webhook-driven real-time demo
- Simple charts (recovery rate by root cause, by channel)

Note: voice is not a channel in this project — don't add a voice row/filter.
