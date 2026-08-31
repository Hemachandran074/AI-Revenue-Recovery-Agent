"""Razorpay webhook signature verification.

``code-standards.md`` makes this mandatory: unsigned or invalid payloads are
rejected BEFORE they enter DETECT. Without it, anyone who learns the endpoint URL
could POST fabricated ``payment.failed`` events and move the recovery numbers.

Razorpay computes HMAC-SHA256 over the RAW request body, hex-encoded, keyed with
the webhook secret configured in the dashboard, and sends it as
``X-Razorpay-Signature``. Source: https://razorpay.com/docs/webhooks/

Two deliberate choices:

**Fails closed.** With no secret configured, every webhook is rejected rather
than waved through. An unconfigured deployment that silently accepted unsigned
payloads would be worse than one that rejects everything, because it would look
like it was working.

**Constant-time comparison.** ``hmac.compare_digest`` rather than ``==``. A
plain string comparison returns early on the first differing byte, and the timing
difference leaks how much of a guessed signature was correct, which makes
forging one byte-by-byte feasible.

This module only VERIFIES. Signing lives in ``app/simulation/signing.py``. Keeping
them apart matters: if one function both signed and verified, a bug in the shared
implementation would agree with itself and the tests would pass.
"""

from __future__ import annotations

import hashlib
import hmac
from enum import StrEnum

SIGNATURE_HEADER = "X-Razorpay-Signature"


class SignatureFailure(StrEnum):
    """Why verification failed. Logged so rejections are auditable."""

    NO_SECRET_CONFIGURED = "no_secret_configured"
    MISSING_SIGNATURE = "missing_signature"
    MALFORMED_SIGNATURE = "malformed_signature"
    MISMATCH = "mismatch"


class InvalidSignatureError(Exception):
    """Raised when a payload cannot be shown to have come from Razorpay."""

    def __init__(self, reason: SignatureFailure) -> None:
        self.reason = reason
        super().__init__(f"webhook signature verification failed: {reason}")


def expected_signature(body: bytes, secret: str) -> str:
    """The signature Razorpay would send for ``body``."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, provided: str | None, secret: str | None) -> None:
    """Verify ``provided`` against ``body``. Returns None, or raises.

    ``body`` must be the raw bytes as received. Re-serialising parsed JSON
    changes key order and whitespace and will fail against a genuine signature.

    Raising rather than returning a bool is intentional: a caller cannot
    accidentally ignore an exception the way it can forget to check a return
    value, and the failure reason travels with it for the audit log.
    """
    if not secret:
        raise InvalidSignatureError(SignatureFailure.NO_SECRET_CONFIGURED)
    if not provided:
        raise InvalidSignatureError(SignatureFailure.MISSING_SIGNATURE)

    candidate = provided.strip()
    # A hex digest of SHA-256 is exactly 64 hex characters. Checking shape first
    # keeps the error specific and avoids comparing obvious junk.
    if len(candidate) != 64 or not all(c in "0123456789abcdefABCDEF" for c in candidate):
        raise InvalidSignatureError(SignatureFailure.MALFORMED_SIGNATURE)

    if not hmac.compare_digest(candidate.lower(), expected_signature(body, secret)):
        raise InvalidSignatureError(SignatureFailure.MISMATCH)
