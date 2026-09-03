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


app = FastAPI(
    title="AI Revenue Recovery Agent",
    version="0.6.0",
    summary="Detects revenue at risk, diagnoses root cause, executes a bounded "
            "recovery workflow. Test-mode/demo build only.",
    lifespan=lifespan,
)

app.include_router(webhooks_router)
app.include_router(dashboard_router)


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
