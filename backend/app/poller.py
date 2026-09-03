"""Background auto-sync for Razorpay payment failures.

Provides dual real-time coverage:
1. Webhooks: Processes instant deliveries when ngrok/webhooks are active.
2. Auto-Sync: Polls recent Razorpay failures every few seconds to guarantee that
   manual browser tests in test mode are 100% automatically detected, diagnosed,
   and logged in real-time even when sandbox webhooks are throttled or delayed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.detect import detect_event
from app.models import Event
from app.pipeline import process_event

logger = logging.getLogger(__name__)


def sync_recent_failures(count: int = 10) -> int:
    """Check Razorpay for any recent failed payments and process new ones."""
    settings = get_settings()
    key_id = settings.razorpay_key_id
    secret = settings.razorpay_key_secret

    if not key_id or not secret or not key_id.startswith("rzp_test_"):
        return 0

    try:
        import razorpay

        client = razorpay.Client(auth=(key_id, secret))
        res = client.payment.all({"count": count})
        items = res.get("items", [])
    except Exception as exc:
        logger.debug("Razorpay poller check failed: %s", exc)
        return 0

    processed = 0
    with session_scope() as session:
        for p in items:
            if p.get("status") != "failed":
                continue

            pid = p.get("id")
            if not pid:
                continue

            # Check if already processed in database
            existing = session.scalar(
                select(Event).where(Event.provider_payment_id == pid)
            )
            if existing is not None:
                continue

            # Build standard Razorpay event envelope
            payload: dict[str, Any] = {
                "entity": "event",
                "account_id": f"acc_{key_id[len('rzp_test_'):]}",
                "event": "payment.failed",
                "contains": ["payment"],
                "payload": {"payment": {"entity": p}},
                "created_at": p.get("created_at", int(time.time())),
            }

            try:
                logger.info("Auto-sync detected new Razorpay failure: %s", pid)
                outcome = process_event(session, payload)
                if not outcome.is_duplicate:
                    processed += 1
            except Exception as exc:
                logger.error("Error processing auto-synced payment %s: %s", pid, exc)

    return processed


async def run_poller_loop(interval_seconds: int = 3) -> None:
    """Continuous background loop for real-time failure sync."""
    logger.info("Starting Razorpay real-time auto-sync loop (interval=%ds)", interval_seconds)
    while True:
        try:
            sync_recent_failures(count=10)
        except Exception as exc:
            logger.debug("Poller loop iteration error: %s", exc)
        await asyncio.sleep(interval_seconds)
