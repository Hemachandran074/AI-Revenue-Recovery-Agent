"""Razorpay webhook signature generation, for replaying fixtures realistically.

Phase 2's DETECT must reject unsigned or badly-signed payloads before anything
enters the pipeline. If the batch replay bypassed that check, the 50-100 event
run would exercise a different code path from the live ngrok webhook, and the
signature verification would effectively be untested. So the simulator signs its
payloads exactly the way Razorpay does and replays through the same endpoint.

Razorpay computes HMAC-SHA256 over the RAW request body, hex-encoded, keyed with
the webhook secret you set in the dashboard, and sends it as
``X-Razorpay-Signature``.
Source: https://razorpay.com/docs/webhooks/

This module only SIGNS. Verification belongs to DETECT (Phase 2) and must not be
implemented here, or the two would share a bug and agree with each other.
"""

from __future__ import annotations

import hashlib
import hmac
import json


def canonical_body(payload: dict[str, object]) -> bytes:
    """Serialise a payload to the exact bytes that will be signed and POSTed.

    The signature covers raw bytes, so the bytes sent over the wire and the
    bytes fed to the HMAC must be identical. Re-serialising a parsed dict
    reorders keys and changes whitespace, which is the single most common cause
    of "signature valid in tests, invalid in production". Callers must sign and
    send the output of this function, never a re-dumped version of it.
    """
    return json.dumps(payload, separators=(",", ":"), sort_keys=False).encode("utf-8")


def sign_body(body: bytes, secret: str) -> str:
    """Return the hex ``X-Razorpay-Signature`` for ``body``."""
    if not secret:
        raise ValueError(
            "Cannot sign with an empty webhook secret. Set RAZORPAY_WEBHOOK_SECRET "
            "in .env to the same value configured in the Razorpay dashboard."
        )
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def signed_delivery(
    payload: dict[str, object], secret: str, event_id: str | None = None
) -> tuple[bytes, dict[str, str]]:
    """Build the body bytes and headers for one simulated webhook delivery.

    Returns ``(body, headers)`` so a caller POSTs them together and the signature
    is guaranteed to match the bytes actually transmitted.
    """
    body = canonical_body(payload)
    headers = {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": sign_body(body, secret),
        "User-Agent": "Razorpay-Webhook/1.0",
    }
    if event_id:
        headers["X-Razorpay-Event-Id"] = event_id
    return body, headers
