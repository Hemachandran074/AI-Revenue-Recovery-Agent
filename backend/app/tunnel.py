"""Webhook-only ASGI app, for pointing a public tunnel at.

    python -m uvicorn app.tunnel:app --port 8001
    ngrok http 8001

WHY THIS EXISTS

``app.main`` also serves ``/health``, ``/readiness`` and
``/events/{id}/audit``, none of which have authentication — a deliberate choice
for a local tool (``ui-context.md`` -> "No auth/login system"). Running
``ngrok http 8000`` would publish all of them, and ``/readiness`` reports which
credentials are configured.

Restricting by path at the tunnel, or trusting a forwarded header, are both
weaker: header checks are spoofable and path rules are easy to get wrong. Serving
a different app that simply does not contain those routes means there is nothing
to leak. A request for ``/readiness`` here gets a 404 because the route does not
exist, not because something decided to refuse it.

Every route on this app is signature-verified, and verification fails closed when
no secret is configured, so this is safe to expose.

Run it alongside ``app.main`` on a separate port: keep ops endpoints on the
loopback-only 8000 for yourself, and tunnel 8001 for Razorpay.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.webhooks import router as webhooks_router

app = FastAPI(
    title="AI Revenue Recovery Agent — webhook intake",
    version="0.2.0",
    summary="Signature-verified Razorpay webhook receiver. Safe to expose.",
    # No interactive docs on a public surface; they would advertise the schema.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.include_router(webhooks_router)


@app.get("/tunnel-health", tags=["ops"])
def tunnel_health() -> dict[str, str]:
    """Liveness only.

    Deliberately reveals nothing about configuration or data — no credential
    state, no counts, no event ids. Named distinctly from ``/health`` so it is
    obvious which app answered.
    """
    return {"status": "ok"}
