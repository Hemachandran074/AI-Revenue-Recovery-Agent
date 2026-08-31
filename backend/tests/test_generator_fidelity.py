"""Assert the synthetic generator matches REAL Razorpay payloads.

Every other test checks the generator against my reading of Razorpay's docs.
This one checks it against payloads Razorpay actually sent, captured from live
test-mode webhook deliveries through the ngrok tunnel and redacted.

That distinction matters. Documentation can be incomplete or out of date; a real
delivery cannot. If the provider changes its payload, or the generator drifts,
this is the test that notices — and it fails loudly rather than letting DETECT be
built against a fiction.

Reference data: ``fixtures/reference_real_payment_failed.json``. Account id,
email, contact, vpa and cardholder name are redacted; the field structure is
untouched.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.detect import parse_envelope
from app.schemas import RootCause
from app.simulation.decline_catalog import SCENARIOS
from app.simulation.generator import generate_batch

REFERENCE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "reference_real_payment_failed.json"
)

ENVELOPE_KEYS = {"entity", "account_id", "event", "contains", "payload", "created_at"}


@pytest.fixture(scope="module")
def real_samples() -> dict[str, dict]:
    if not REFERENCE.exists():
        pytest.skip(f"no reference capture at {REFERENCE}")
    return json.loads(REFERENCE.read_text(encoding="utf-8"))["samples_by_method"]


@pytest.fixture(scope="module")
def synthetic_by_method() -> dict[str, dict]:
    """One generated envelope per payment method, from a large batch."""
    batch = generate_batch(seed=11, count=100, now=datetime(2026, 8, 30, tzinfo=UTC))
    by_method: dict[str, dict] = {}
    for event in batch.events:
        by_method.setdefault(str(event.method), event.envelope)
    return by_method


def test_reference_capture_contains_real_methods(real_samples) -> None:
    assert real_samples, "reference capture is empty"
    for method, envelope in real_samples.items():
        assert envelope["event"] == "payment.failed"
        assert envelope["payload"]["payment"]["entity"]["method"] == method


def test_real_envelope_keys_are_exactly_what_we_generate(real_samples) -> None:
    """The outer envelope has no surprises in either direction."""
    for method, envelope in real_samples.items():
        assert set(envelope) == ENVELOPE_KEYS, f"{method}: {sorted(envelope)}"


def test_generator_produces_every_field_real_payloads_carry(
    real_samples, synthetic_by_method
) -> None:
    """The important direction: nothing real is missing from our synthetic data.

    A field present in production but absent from fixtures is a field DETECT is
    never tested against.
    """
    for method, real_envelope in real_samples.items():
        synthetic = synthetic_by_method.get(method)
        if synthetic is None:
            pytest.skip(f"batch produced no {method} events")
        real_fields = set(real_envelope["payload"]["payment"]["entity"])
        synth_fields = set(synthetic["payload"]["payment"]["entity"])
        missing = real_fields - synth_fields
        assert not missing, f"{method}: generator omits real fields {sorted(missing)}"


def test_real_payloads_parse_through_detect(real_samples) -> None:
    """DETECT must handle the genuine article, not just our approximation of it."""
    for method, envelope in real_samples.items():
        parsed = parse_envelope(envelope)
        assert parsed.entity_id.startswith("pay_")
        assert parsed.currency == "INR"
        assert isinstance(parsed.amount_minor, int)
        assert parsed.method == method
        assert parsed.created_at.tzinfo is not None


def test_real_amounts_are_integer_minor_units(real_samples) -> None:
    """Confirms against real data that amounts are paise integers, never floats."""
    for envelope in real_samples.values():
        amount = envelope["payload"]["payment"]["entity"]["amount"]
        assert isinstance(amount, int) and not isinstance(amount, bool)
        assert amount > 0


def test_real_created_at_is_unix_seconds(real_samples) -> None:
    for envelope in real_samples.values():
        created = envelope["payload"]["payment"]["entity"]["created_at"]
        assert isinstance(created, int)
        assert 1_600_000_000 < created < 2_000_000_000


def test_real_error_tuples_exist_in_the_decline_catalogue(real_samples) -> None:
    """Every real failure we have observed must be a scenario we can generate.

    Ignores payment method deliberately: the same error tuple occurs across
    methods, which live capture confirmed. What matters is that the
    code/source/step/reason combination is one the catalogue knows about, so
    DIAGNOSE will have been trained and tested on it.
    """
    catalogued = {
        (s.error_code, s.error_source, s.error_step, s.error_reason)
        for s in SCENARIOS
    }
    for method, envelope in real_samples.items():
        entity = envelope["payload"]["payment"]["entity"]
        observed = (
            entity.get("error_code"),
            entity.get("error_source"),
            entity.get("error_step"),
            entity.get("error_reason"),
        )
        assert observed in catalogued, (
            f"{method}: real failure {observed} is not in the decline catalogue. "
            "Add it, so the pipeline is exercised on a failure mode production "
            "actually produces."
        )


def test_observed_cancellation_maps_to_sca_abandoned(real_samples) -> None:
    """A customer cancelling at the authentication step is an abandoned auth.

    The recovery action is a fresh link the customer completes themselves, never
    an automated retry of the same session.
    """
    by_reason = {
        s.error_reason: s.expected_root_cause for s in SCENARIOS if s.error_reason
    }
    for envelope in real_samples.values():
        reason = envelope["payload"]["payment"]["entity"].get("error_reason")
        if reason == "payment_cancelled":
            assert by_reason[reason] is RootCause.SCA_ABANDONED


def test_reference_capture_is_redacted(real_samples) -> None:
    """The committed fixture must not carry the account owner's details."""
    blob = json.dumps(real_samples)
    for term in ("@gmail", "@yahoo", "@outlook", "@hotmail"):
        assert term not in blob
    for envelope in real_samples.values():
        assert envelope["account_id"] == "acc_REDACTED000000"
        entity = envelope["payload"]["payment"]["entity"]
        assert entity["email"].endswith("@example.com")


def test_reference_capture_carries_no_card_secrets(real_samples) -> None:
    """Constraint #1, verified against real provider output."""
    forbidden = {"number", "pan", "cvv", "cvc", "expiry_month", "expiry_year"}
    blob = json.dumps(real_samples).lower()
    for term in forbidden:
        assert f'"{term}"' not in blob
