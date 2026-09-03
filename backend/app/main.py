"""FastAPI application entrypoint.

Serves health/readiness, the Razorpay webhook receiver (DETECT), and the Phase 6
batch dashboard at ``/dashboard`` with its JSON counterparts under ``/api``.

SECURITY NOTE: there is no authentication on the ops endpoints. That is an
intentional, documented choice for a local demo tool
(``context/ui-context.md`` -> "No auth/login system"), and it is bound to
127.0.0.1 by default for that reason. Do not expose it to a network or deploy it
without adding an auth layer first.

The dashboard raises the stakes of that note rather than changing it. ``/health``
leaked nothing; ``/dashboard``, ``/api/metrics`` and ``/api/events`` return
customer ids, amounts at risk, decline reasons and payment-link ids for every
event in the database. On a shared or port-forwarded machine that is a real
disclosure. It stays unauthenticated because ui-context.md scopes this as a local
demo tool, and it is kept off ``app.tunnel`` for exactly this reason — the public
surface contains only the signature-verified webhook route.

The webhook route is the exception and is NOT unauthenticated: every request must
carry a valid ``X-Razorpay-Signature`` computed with the shared webhook secret,
and it fails closed when no secret is configured. That route is safe to expose
through the ngrok tunnel; the ops endpoints are not, so keep the tunnel pointed
at the webhook path.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
from typing import Any
from fastapi import FastAPI

from app.config import get_settings
from app.dashboard import router as dashboard_router
from app.logging_setup import configure_logging
from app.webhooks import router as webhooks_router

# Structured JSON for pipeline-stage records, plain text for everything else.
# Called at import so a stage log line emitted during startup is formatted too.
configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background auto-sync worker on application startup."""
    from app.poller import run_poller_loop

    task = asyncio.create_task(run_poller_loop(interval_seconds=3))
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(
    title="AI Revenue Recovery Agent",
    version="0.6.0",
    summary="Detects revenue at risk, diagnoses root cause, executes a bounded "
            "recovery workflow. Test-mode/demo build only.",
    lifespan=lifespan,
)

# Enable CORS for React frontend (Vite dev server)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webhooks_router)
app.include_router(dashboard_router)


class SimulateRequest(BaseModel):
    cause: str = "card"
    amount: float = 499.0
    contact: str = "+919566687795"
    customer_id: str | None = None


@app.post("/api/simulate", tags=["ops"])
def api_simulate(req: SimulateRequest) -> dict[str, Any]:
    """Trigger a real-time recovery pipeline simulation from the UI."""
    import time
    from app.db import session_scope
    from app.pipeline import process_event
    from app.demo_recovery import CAUSES, build_envelope

    cause_map = {
        "card": "card",
        "card_expired": "card",
        "sca": "sca",
        "sca_abandoned": "sca",
        "friction": "friction",
        "checkout_friction": "friction",
        "funds": "funds",
        "insufficient_funds": "funds",
    }
    canonical_cause = cause_map.get(req.cause.lower(), "card")
    customer_id = req.customer_id or f"cust_ui_{int(time.time()) % 1000000}"
    amount_minor = int(round(req.amount * 100))

    envelope = build_envelope(
        cause=CAUSES[canonical_cause],
        amount_paise=amount_minor,
        contact=req.contact,
        customer_id=customer_id,
    )

    with session_scope() as session:
        outcome = process_event(session, envelope)
        summary = outcome.summary()
        summary["amount_inr"] = req.amount
        summary["customer_id"] = customer_id
        summary["contact"] = req.contact
        if outcome.execution:
            summary["recovery_link_url"] = (
                outcome.execution.recovery_link.short_url
                if outcome.execution.recovery_link
                else None
            )
            summary["provider_message_id"] = outcome.execution.provider_message_id
            summary["skip_reason"] = outcome.execution.skip_reason
            summary["failure_reason"] = outcome.execution.failure_reason
        if outcome.diagnosis:
            summary["reasoning"] = outcome.diagnosis.reasoning
            summary["confidence"] = outcome.diagnosis.confidence
        return summary


class CreateOrderRequest(BaseModel):
    amount: float = 199.0
    contact: str = "+919566687795"
    email: str = "recovery.demo@example.com"
    customer_id: str | None = None


@app.post("/api/create-order", tags=["ops"])
def api_create_order(req: CreateOrderRequest) -> dict[str, Any]:
    """Create an order for real interactive browser checkout simulation."""
    import time
    import razorpay
    from app.config import get_settings

    settings = get_settings()
    client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
    customer_id = req.customer_id or f"cust_live_{int(time.time()) % 1000000}"
    amount_paise = int(round(req.amount * 100))

    order = client.order.create({
        "amount": amount_paise,
        "currency": "INR",
        "receipt": f"rcpt_{customer_id[:16]}",
        "notes": {"customer_id": customer_id, "source": "revenue_recovery_live_test"},
    })
    order_id = str(order.get("id"))
    checkout_url = f"http://127.0.0.1:8000/test-checkout?order_id={order_id}&amount={amount_paise}&contact={req.contact}&email={req.email}"

    return {
        "order_id": order_id,
        "amount_inr": req.amount,
        "customer_id": customer_id,
        "checkout_url": checkout_url,
        "key_id": settings.razorpay_key_id,
    }


@app.get("/health", tags=["ops"])
def health() -> dict[str, str]:
    """Liveness check. No dependencies, so it works in an unconfigured repo."""
    return {
        "status": "ok",
        "service": "revenue-recovery-agent",
        "phase": "6-audit-and-metrics",
    }


@app.get("/events/recent", tags=["ops"])
def recent_events(limit: int = 10) -> dict[str, object]:
    """Most recently received events, newest first.

    Exists to answer "did that webhook actually arrive?" without needing an event
    id in hand. ``provider_account_id`` is surfaced because it distinguishes a
    genuine Razorpay delivery, which carries your real ``acc_...``, from a
    replayed fixture, which carries a generated one.

    Local app only. Never mounted on ``app.tunnel``: it would let anyone
    enumerate events and read customer ids off a public URL.
    """
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Event

    capped = max(1, min(limit, 100))
    with session_scope() as session:
        rows = session.scalars(
            select(Event).order_by(Event.received_at.desc()).limit(capped)
        ).all()
        return {
            "count": len(rows),
            "events": [
                {
                    "event_id": r.event_id,
                    "provider_payment_id": r.provider_payment_id,
                    "provider_account_id": r.raw_payload.get("account_id"),
                    "decline_code": r.decline_code,
                    "payment_method": r.payment_method,
                    "amount_inr": r.amount_minor / 100,
                    "prior_attempts": r.prior_attempts,
                    "received_at": r.received_at.isoformat() if r.received_at else None,
                }
                for r in rows
            ],
        }


@app.get("/events/{event_id}/audit", tags=["ops"])
def event_audit_trail(event_id: str) -> dict[str, object]:
    """The full audit trail for one event, stage by stage.

    ``project-overview.md``'s bar is that a stranger can read this for any single
    event and understand what happened and why in under 30 seconds. Phase 6 gives
    it a UI; this is the queryable form underneath.
    """
    from app import audit
    from app.db import session_scope

    with session_scope() as session:
        trail = audit.trail_for_event(session, event_id)
    return {"event_id": event_id, "stages": trail, "stage_count": len(trail)}


from fastapi.responses import HTMLResponse


@app.get("/test-checkout", response_class=HTMLResponse, tags=["ops"])
def test_checkout(
    order_id: str = "",
    amount: int = 19900,
    contact: str = "+919566687795",
    email: str = "recovery.demo@example.com",
) -> HTMLResponse:
    """Interactive Razorpay Standard Checkout modal for real browser testing."""
    settings = get_settings()
    key_id = settings.razorpay_key_id or "rzp_test_TVX4xqAbr7KZPk"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Razorpay Test Checkout - Real Simulation</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script src="https://checkout.razorpay.com/v1/checkout.js"></script>
  <style>
    body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; background: #0f172a; color: #f8fafc; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }}
    .card {{ background: #1e293b; padding: 32px; border-radius: 12px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); max-width: 440px; width: 100%; border: 1px solid #334155; text-align: center; }}
    h2 {{ margin-top: 0; color: #38bdf8; }}
    p {{ color: #94a3b8; font-size: 14px; line-height: 1.5; }}
    .btn {{ background: #0284c7; color: #fff; border: none; padding: 14px 28px; border-radius: 8px; font-size: 16px; font-weight: 600; cursor: pointer; width: 100%; margin-top: 16px; transition: background 0.2s; }}
    .btn:hover {{ background: #0369a1; }}
  </style>
</head>
<body>
  <div class="card">
    <h2>Razorpay Test Checkout</h2>
    <p>Amount: <strong>₹{amount / 100:.2f}</strong></p>
    <p>Contact: <code>{contact}</code></p>
    <p>Click below to open the Razorpay Payment Gateway. Choose any payment method and test failure/cancellation.</p>
    <button id="pay-btn" class="btn">Open Razorpay Checkout</button>
  </div>
  <script>
    var options = {{
      "key": "{key_id}",
      "amount": "{amount}",
      "currency": "INR",
      "name": "Revenue Recovery Agent",
      "description": "Real Test Simulation",
      "order_id": "{order_id}",
      "prefill": {{
        "contact": "{contact}",
        "email": "{email}"
      }},
      "theme": {{ "color": "#0284c7" }}
    }};
    var rzp = new Razorpay(options);
    document.getElementById('pay-btn').onclick = function(e) {{
      rzp.open();
      e.preventDefault();
    }};
    window.onload = function() {{
      setTimeout(function() {{ rzp.open(); }}, 500);
    }};
  </script>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/readiness", tags=["ops"])
def readiness() -> dict[str, object]:
    """What is wired up and what is still missing.

    Reports key *names* that need values, never key values.
    """
    settings = get_settings()
    report = settings.credential_report()
    return {
        "app_env": settings.app_env,
        "guardrail_config": settings.guardrail_config(),
        **report,
    }
