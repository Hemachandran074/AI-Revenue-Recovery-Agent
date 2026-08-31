"""Signature verification tests.

This is the security boundary of the whole system. If it can be bypassed, anyone
who learns the endpoint URL can post fabricated failures and move the recovery
numbers, and the audit trail records forgeries as fact.
"""

from __future__ import annotations

import pytest

from app.signature import (
    InvalidSignatureError,
    SignatureFailure,
    expected_signature,
    verify_signature,
)
from app.simulation import signing

SECRET = "a_test_webhook_secret"
BODY = b'{"entity":"event","event":"payment.failed"}'


def test_accepts_a_correctly_signed_body() -> None:
    verify_signature(BODY, expected_signature(BODY, SECRET), SECRET)


def test_agrees_with_the_independent_signing_implementation() -> None:
    """The simulator signs, this module verifies, and they are separate code.

    If one function did both, a bug in the shared implementation would agree with
    itself and this suite would pass while production rejected every real webhook.
    """
    body, headers = signing.signed_delivery({"event": "payment.failed"}, SECRET)
    verify_signature(body, headers["X-Razorpay-Signature"], SECRET)


def test_rejects_when_no_secret_is_configured() -> None:
    """Must fail closed. An unconfigured deployment that accepted unsigned
    payloads would look like it was working."""
    with pytest.raises(InvalidSignatureError) as exc:
        verify_signature(BODY, expected_signature(BODY, SECRET), None)
    assert exc.value.reason is SignatureFailure.NO_SECRET_CONFIGURED

    with pytest.raises(InvalidSignatureError):
        verify_signature(BODY, expected_signature(BODY, SECRET), "")


def test_rejects_missing_signature_header() -> None:
    with pytest.raises(InvalidSignatureError) as exc:
        verify_signature(BODY, None, SECRET)
    assert exc.value.reason is SignatureFailure.MISSING_SIGNATURE


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not-hex-at-all",
        "abc123",
        "z" * 64,
        expected_signature(BODY, SECRET)[:-1],
        expected_signature(BODY, SECRET) + "0",
    ],
)
def test_rejects_malformed_signatures(bad: str) -> None:
    with pytest.raises(InvalidSignatureError):
        verify_signature(BODY, bad, SECRET)


def test_rejects_signature_from_a_different_secret() -> None:
    with pytest.raises(InvalidSignatureError) as exc:
        verify_signature(BODY, expected_signature(BODY, "other_secret"), SECRET)
    assert exc.value.reason is SignatureFailure.MISMATCH


def test_rejects_tampered_body() -> None:
    """The exact attack this exists to stop: inflating a recovered amount."""
    good = expected_signature(BODY, SECRET)
    tampered = BODY.replace(b"payment.failed", b"payment.captured")
    with pytest.raises(InvalidSignatureError) as exc:
        verify_signature(tampered, good, SECRET)
    assert exc.value.reason is SignatureFailure.MISMATCH


def test_whitespace_and_case_are_tolerated() -> None:
    """Hex digests are case-insensitive and headers pick up stray whitespace.

    Being strict about either would reject genuine deliveries.
    """
    sig = expected_signature(BODY, SECRET)
    verify_signature(BODY, f"  {sig.upper()}  ", SECRET)


def test_signature_is_body_specific() -> None:
    a = expected_signature(b'{"a":1}', SECRET)
    b = expected_signature(b'{"a":2}', SECRET)
    assert a != b


def test_reserialised_json_fails_verification() -> None:
    """Documents why the route reads raw bytes before parsing.

    Parsing and re-dumping changes separators, so the signature no longer matches
    even though the payload is semantically identical. This is the single most
    common way webhook verification breaks in production.
    """
    import json

    original = b'{"amount":149900,"currency":"INR"}'
    sig = expected_signature(original, SECRET)
    reserialised = json.dumps(json.loads(original)).encode()  # adds spaces
    assert reserialised != original
    with pytest.raises(InvalidSignatureError):
        verify_signature(reserialised, sig, SECRET)
