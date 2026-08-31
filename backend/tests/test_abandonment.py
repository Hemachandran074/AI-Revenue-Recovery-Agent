"""Tests for the Phase 1b abandonment generators.

Two jobs:

1. **Fidelity.** The ``payment_link`` and ``invoice`` entities must match what
   Razorpay really returns, asserted against
   ``fixtures/reference_real_entities.json`` which was read back from the live
   test API rather than transcribed from docs.
2. **Taxonomy coverage.** Phase 1b exists so ``checkout_friction`` and
   ``genuine_abandonment`` become producible. If that regresses, DIAGNOSE would
   ship with two categories untested.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.schemas import EventType, RootCause
from app.simulation.abandonment_catalog import (
    SCENARIOS,
    AbandonmentSignal,
    covered_root_causes,
    scenarios_by_key,
    scenarios_for_event_type,
)
from app.simulation.generator import generate_batch

REFERENCE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "reference_real_entities.json"
)

ENVELOPE_KEYS = {"entity", "account_id", "event", "contains", "payload", "created_at"}


@pytest.fixture(scope="module")
def batch():
    return generate_batch(seed=77, count=90, now=datetime(2026, 8, 30, tzinfo=UTC))


@pytest.fixture(scope="module")
def checkout_events(batch):
    return [e for e in batch.events if e.event_type is EventType.CHECKOUT_ABANDONED]


@pytest.fixture(scope="module")
def invoice_events(batch):
    return [e for e in batch.events if e.event_type is EventType.INVOICE_OVERDUE]


@pytest.fixture(scope="module")
def reference():
    if not REFERENCE.exists():
        pytest.skip(f"no reference capture at {REFERENCE}")
    return json.loads(REFERENCE.read_text(encoding="utf-8"))


# ------------------------------------------------------------ taxonomy coverage


def test_abandonment_supplies_the_two_checkout_root_causes() -> None:
    """The entire purpose of Phase 1b."""
    assert covered_root_causes() == {
        RootCause.CHECKOUT_FRICTION,
        RootCause.GENUINE_ABANDONMENT,
    }


def test_batch_contains_both_checkout_root_causes(batch) -> None:
    causes = {e.expected_root_cause for e in batch.events}
    assert RootCause.CHECKOUT_FRICTION in causes
    assert RootCause.GENUINE_ABANDONMENT in causes


def test_batch_contains_all_eight_root_causes(batch) -> None:
    """End-to-end proof that DIAGNOSE can be given an example of every category."""
    causes = {e.expected_root_cause for e in batch.events}
    assert causes == set(RootCause), f"missing: {sorted(set(RootCause) - causes)}"


def test_every_abandonment_scenario_appears_in_a_reasonable_batch(batch) -> None:
    """Scenarios are seeded before weighted filling, so none should be missed.

    Pure weighted sampling could drop a scenario entirely in a small batch, which
    would silently reduce coverage.
    """
    keys = {e.scenario_key for e in batch.events}
    for scenario in SCENARIOS:
        assert scenario.key in keys, f"{scenario.key} never generated"


def test_all_three_event_types_are_produced(batch) -> None:
    types = {e.event_type for e in batch.events}
    assert types == {
        EventType.PAYMENT_FAILED,
        EventType.CHECKOUT_ABANDONED,
        EventType.INVOICE_OVERDUE,
    }


def test_payment_failures_remain_the_majority(batch) -> None:
    """project-overview.md locks the direction to payment degradation, so
    abandonment must stay a minority of the batch."""
    payment = sum(
        1 for e in batch.events if e.event_type is EventType.PAYMENT_FAILED
    )
    assert payment / len(batch.events) > 0.6


def test_scenario_keys_are_unique() -> None:
    assert len(scenarios_by_key()) == len(SCENARIOS)


def test_every_scenario_has_a_rationale() -> None:
    for s in SCENARIOS:
        assert s.rationale.strip(), f"{s.key} has no rationale"
        assert s.weight > 0


def test_scenarios_split_across_both_event_types() -> None:
    assert scenarios_for_event_type(EventType.CHECKOUT_ABANDONED)
    assert scenarios_for_event_type(EventType.INVOICE_OVERDUE)


# ----------------------------------------------------- friction vs abandonment


def test_engagement_signal_decides_the_root_cause() -> None:
    """The discriminator must be consistent: evidence of trying means friction.

    Someone who attempted payment and failed needs help; someone who never
    engaged should get one reminder and then be left alone. Mapping these the
    same way would either harass the uninterested or abandon the recoverable.
    """
    for s in SCENARIOS:
        if s.signal in {
            AbandonmentSignal.ATTEMPTED_AND_FAILED,
            AbandonmentSignal.PARTIALLY_PAID,
        }:
            assert s.expected_root_cause is RootCause.CHECKOUT_FRICTION, s.key
        else:
            assert s.expected_root_cause is RootCause.GENUINE_ABANDONMENT, s.key


def test_friction_scenarios_show_evidence_of_trying() -> None:
    for s in SCENARIOS:
        if s.expected_root_cause is RootCause.CHECKOUT_FRICTION:
            assert s.failed_attempts > 0 or s.partial_paid_fraction > 0, s.key


def test_genuine_abandonment_shows_no_engagement() -> None:
    for s in SCENARIOS:
        if s.expected_root_cause is RootCause.GENUINE_ABANDONMENT:
            assert s.failed_attempts == 0
            assert s.partial_paid_fraction == 0.0


# -------------------------------------------------------------- envelope shape


def test_abandonment_envelopes_match_razorpay_wrapper(
    checkout_events, invoice_events
) -> None:
    for event in [*checkout_events, *invoice_events]:
        env = event.envelope
        assert set(env) == ENVELOPE_KEYS
        assert env["entity"] == "event"
        assert isinstance(env["created_at"], int)
        assert len(env["contains"]) == 1
        entity_key = env["contains"][0]
        assert set(env["payload"]) == {entity_key}
        assert set(env["payload"][entity_key]) == {"entity"}


def test_checkout_events_use_the_payment_link_event(checkout_events) -> None:
    assert checkout_events, "no checkout_abandoned events generated"
    for event in checkout_events:
        assert event.envelope["event"] == "payment_link.expired"
        assert event.envelope["contains"] == ["payment_link"]
        assert event.event_id.startswith("plink_")


def test_invoice_events_use_the_invoice_event(invoice_events) -> None:
    assert invoice_events, "no invoice_overdue events generated"
    for event in invoice_events:
        assert event.envelope["event"] == "invoice.expired"
        assert event.envelope["contains"] == ["invoice"]
        assert event.event_id.startswith("inv_")


def test_abandonment_events_carry_no_payment_method(checkout_events) -> None:
    """An expired link has no method, because the customer never chose one.

    Inventing one would imply knowledge the provider never gave us.
    """
    for event in checkout_events:
        assert event.method is None
        assert event.abandonment_signal is not None


# ------------------------------------------------- fidelity to real entities


def test_payment_link_entity_matches_real_field_set(checkout_events, reference) -> None:
    """No field real payment links carry may be missing from ours."""
    real_fields = set(reference["payment_link"])
    for event in checkout_events:
        entity = event.envelope["payload"]["payment_link"]["entity"]
        missing = real_fields - set(entity)
        assert not missing, f"generator omits real link fields {sorted(missing)}"


def test_invoice_entity_matches_real_field_set(invoice_events, reference) -> None:
    real_fields = set(reference["invoice"])
    for event in invoice_events:
        entity = event.envelope["payload"]["invoice"]["entity"]
        missing = real_fields - set(entity)
        assert not missing, f"generator omits real invoice fields {sorted(missing)}"


def test_invoice_line_items_match_real_shape(invoice_events, reference) -> None:
    real_item_fields = set(reference["invoice"]["line_items"][0])
    for event in invoice_events:
        items = event.envelope["payload"]["invoice"]["entity"]["line_items"]
        assert items, "invoice has no line items"
        for item in items:
            assert real_item_fields - set(item) == set()


def test_invoice_customer_details_duplicate_naming_is_preserved(
    invoice_events,
) -> None:
    """Razorpay really does expose name and customer_name, email and
    customer_email. Dropping the duplicates would make DETECT untested against
    the real shape."""
    for event in invoice_events:
        details = event.envelope["payload"]["invoice"]["entity"]["customer_details"]
        assert details["name"] == details["customer_name"]
        assert details["email"] == details["customer_email"]
        assert details["contact"] == details["customer_contact"]


def test_unset_timestamp_conventions_differ_by_entity(
    checkout_events, invoice_events
) -> None:
    """Payment links use 0 for unset timestamps; invoices use null.

    Observed on real entities and easy to get wrong in both directions.
    """
    for event in checkout_events:
        assert event.envelope["payload"]["payment_link"]["entity"]["cancelled_at"] == 0
    for event in invoice_events:
        entity = event.envelope["payload"]["invoice"]["entity"]
        assert entity["cancelled_at"] is None
        assert entity["paid_at"] is None


# ------------------------------------------------------------------- amounts


def test_amounts_are_integer_paise(checkout_events, invoice_events) -> None:
    for event in [*checkout_events, *invoice_events]:
        key = event.envelope["contains"][0]
        entity = event.envelope["payload"][key]["entity"]
        assert isinstance(entity["amount"], int)
        assert not isinstance(entity["amount"], bool)
        assert entity["amount"] > 0
        assert entity["amount"] % 100 == 0


def test_invoice_amounts_reconcile(invoice_events) -> None:
    """amount_paid + amount_due must equal amount, or the recovered-revenue
    metric would be computed from inconsistent figures."""
    for event in invoice_events:
        entity = event.envelope["payload"]["invoice"]["entity"]
        assert entity["amount_paid"] + entity["amount_due"] == entity["amount"]


def test_partial_payments_are_marked_and_non_zero(invoice_events) -> None:
    partials = [
        e
        for e in invoice_events
        if e.abandonment_signal is AbandonmentSignal.PARTIALLY_PAID
    ]
    if not partials:
        pytest.skip("no partially-paid invoices in this batch")
    for event in partials:
        entity = event.envelope["payload"]["invoice"]["entity"]
        assert entity["partial_payment"] is True
        assert 0 < entity["amount_paid"] < entity["amount"]


def test_unpaid_entities_report_nothing_paid(
    checkout_events, invoice_events
) -> None:
    for event in [*checkout_events, *invoice_events]:
        if event.abandonment_signal is AbandonmentSignal.PARTIALLY_PAID:
            continue
        key = event.envelope["contains"][0]
        entity = event.envelope["payload"][key]["entity"]
        assert entity["amount_paid"] == 0


def test_link_payments_array_stays_empty(checkout_events) -> None:
    """Real links returned no payments array even when paid.

    Populating an invented attempts list would let DIAGNOSE be built against
    information Razorpay never sends. The attempt count travels in
    pipeline_context instead.
    """
    for event in checkout_events:
        assert event.envelope["payload"]["payment_link"]["entity"]["payments"] is None


# --------------------------------------------------------------------- timing


def test_expiry_comes_after_creation(checkout_events, invoice_events) -> None:
    for event in [*checkout_events, *invoice_events]:
        assert event.detected_at > event.first_failure_at


def test_no_event_is_dated_in_the_future(batch) -> None:
    """Regression test for a bug found by live replay.

    Expiries were being computed as creation-plus-validity, which put some up to
    7 days ahead of the batch's reference time. That meant receiving an expiry
    webhook for a link that had not expired yet, and it showed up as
    detected_at > received_at in the audit trail. It would also make any
    detect-to-action latency metric negative.
    """
    reference = batch.generated_at
    for event in batch.events:
        assert event.detected_at <= reference, (
            f"{event.event_id} ({event.event_type}) is dated "
            f"{event.detected_at.isoformat()}, after the batch reference time "
            f"{reference.isoformat()}"
        )


def test_abandonment_creation_may_predate_the_window(batch) -> None:
    """A link issued before observation began is legitimate.

    Only the expiry has to be inside the window, so creation timestamps running
    earlier is expected rather than a fault.
    """
    abandonment = [
        e for e in batch.events if e.event_type is not EventType.PAYMENT_FAILED
    ]
    assert abandonment
    for event in abandonment:
        assert event.first_failure_at < event.detected_at


def test_detected_at_is_the_expiry_moment(checkout_events) -> None:
    """We learn of an abandonment when the link expires, not when it was created."""
    for event in checkout_events:
        entity = event.envelope["payload"]["payment_link"]["entity"]
        assert entity["expired_at"] == int(event.detected_at.timestamp())
        assert entity["created_at"] == int(event.first_failure_at.timestamp())


def test_recovery_window_measured_from_creation(invoice_events) -> None:
    """The 7-day hard stop must run from when the money first became at risk."""
    for event in invoice_events:
        entity = event.envelope["payload"]["invoice"]["entity"]
        assert entity["issued_at"] == int(event.first_failure_at.timestamp())


# --------------------------------------------------- ground-truth containment


def test_ground_truth_never_leaks_into_abandonment_payloads(
    checkout_events, invoice_events
) -> None:
    banned = (
        "expected_root_cause", "ground_truth", "scenario_key", "provenance",
        "abandonment_signal", "checkout_friction", "genuine_abandonment",
    )
    for event in [*checkout_events, *invoice_events]:
        blob = json.dumps(event.envelope)
        for term in banned:
            assert term not in blob, f"{term} leaked into the payload"


def test_contact_details_use_reserved_domains(
    checkout_events, invoice_events
) -> None:
    for event in [*checkout_events, *invoice_events]:
        key = event.envelope["contains"][0]
        entity = event.envelope["payload"][key]["entity"]
        holder = entity["customer"] if key == "payment_link" else entity["customer_details"]
        assert holder["email"].endswith(
            ("@example.com", "@example.in", "@example.org")
        )


def test_batch_remains_deterministic_with_abandonment_events() -> None:
    pinned = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    a = generate_batch(seed=99, count=40, now=pinned)
    b = generate_batch(seed=99, count=40, now=pinned)
    assert [e.event_id for e in a.events] == [e.event_id for e in b.events]
    assert [e.event_type for e in a.events] == [e.event_type for e in b.events]
