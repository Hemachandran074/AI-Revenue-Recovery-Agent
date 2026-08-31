"""FastAPI application entrypoint.

Exposes health/readiness plus the Razorpay webhook receiver (Phase 2, DETECT).

SECURITY NOTE: there is no authentication on the ops endpoints. That is an
intentional, documented choice for a local demo tool
(``context/ui-context.md`` -> "No auth/login system"), and it is bound to
127.0.0.1 by default for that reason. Do not expose it to a network or deploy it
without adding an auth layer first.

The webhook route is the exception and is NOT unauthenticated: every request must
carry a valid ``X-Razorpay-Signature`` computed with the shared webhook secret,
and it fails closed when no secret is configured. That route is safe to expose
through the ngrok tunnel; the ops endpoints are not, so keep the tunnel pointed
at the webhook path.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.config import get_settings
from app.webhooks import router as webhooks_router

app = FastAPI(
    title="AI Revenue Recovery Agent",
    version="0.2.0",
    summary="Detects revenue at risk, diagnoses root cause, executes a bounded "
            "recovery workflow. Test-mode/demo build only.",
)

app.include_router(webhooks_router)


@app.get("/health", tags=["ops"])
def health() -> dict[str, str]:
    """Liveness check. No dependencies, so it works in an unconfigured repo."""
    return {"status": "ok", "service": "revenue-recovery-agent", "phase": "2-detect"}


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
