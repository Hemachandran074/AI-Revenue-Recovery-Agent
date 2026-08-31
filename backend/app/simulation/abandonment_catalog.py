"""Catalogue of abandoned-checkout and overdue-invoice scenarios.

WHY THESE ARE SEPARATE FROM `decline_catalog`
A failed payment and an abandoned checkout are not the same shape of event. A
decline carries an error reason explaining what went wrong; an abandonment is
defined by nothing having happened. They arrive on different Razorpay events with
different entities, so forcing them through one catalogue would have meant
pretending an abandonment has a decline code.

THE EVENTS ARE REAL, NOT INVENTED
Razorpay publishes no checkout-abandonment or cart-abandonment webhook. Rather
than fabricate one, these map onto events the provider genuinely emits:

  ``payment_link.expired``  a link sent to a customer that lapsed unpaid. That IS
                            an abandoned checkout, and it arrives as a webhook.
  ``invoice.expired``       an invoice that passed its ``expire_by`` unpaid.

That matters for more than tidiness. `architecture.md`'s real-time requirement
says webhook-driven, not polling, and abandonment looked like it would have to
break that rule — you cannot be notified that nothing happened. Because Razorpay
emits an expiry event, no sweep or poller is needed and the constraint holds.

HOW THE TWO CHECKOUT ROOT CAUSES ARE TOLD APART
`architecture.md` distinguishes ``checkout_friction`` from
``genuine_abandonment`` but does not say how. The discriminator used here is
whether the customer ever tried to pay, which the real entity actually reveals:

  tried and failed / part-paid  -> ``checkout_friction``   (something blocked them)
  never attempted at all        -> ``genuine_abandonment`` (never engaged)

That distinction drives different actions, which is the point: someone who tried
three times and failed deserves help, someone who never opened the link should get
one reminder and then be left alone.

SCOPE NOTE ON INVOICES
`project-overview.md` locks the direction to payment degradation and says not to
build the other five, one of which is the B2B receivables chaser. Overdue invoices
are included here only because ``invoice_overdue`` is in `architecture.md`'s
EventType enum, so the schema demands the event type exist. They are weighted low
and no receivables-specific behaviour is built.

Entity shapes come from `fixtures/reference_real_entities.json`, read back from
the live Razorpay test API rather than transcribed from docs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.schemas import EventType, RootCause
from app.simulation.decline_catalog import Provenance


class AbandonmentSignal(StrEnum):
    """What the expired entity tells us about customer engagement.

    This is the evidence a diagnosis rests on, kept explicit so the audit trail
    can show why one expiry was treated as friction and another as disinterest.
    """

    NEVER_OPENED = "never_opened"
    OPENED_NO_ATTEMPT = "opened_no_attempt"
    ATTEMPTED_AND_FAILED = "attempted_and_failed"
    PARTIALLY_PAID = "partially_paid"


@dataclass(frozen=True, slots=True)
class AbandonmentScenario:
    """One realistic way a checkout or invoice is abandoned."""

    key: str
    event_type: EventType
    razorpay_event: str
    entity_status: str
    signal: AbandonmentSignal
    failed_attempts: int
    partial_paid_fraction: float
    provenance: Provenance
    expected_root_cause: RootCause
    weight: float
    rationale: str


# Weights are an assumption, not a measurement: Razorpay publishes no abandonment
# statistics. Shaped so most expiries are genuine disinterest, because that is the
# common case, while leaving a solid minority of friction cases to act on.
SCENARIOS: tuple[AbandonmentScenario, ...] = (
    # ------------------------------------------------- checkout (payment links)
    AbandonmentScenario(
        key="link_expired_never_opened",
        event_type=EventType.CHECKOUT_ABANDONED,
        razorpay_event="payment_link.expired",
        entity_status="expired",
        signal=AbandonmentSignal.NEVER_OPENED,
        failed_attempts=0,
        partial_paid_fraction=0.0,
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.GENUINE_ABANDONMENT,
        weight=9.0,
        rationale=(
            "Link lapsed with no payment attempt at all. Nothing went wrong "
            "technically; the customer simply did not engage. One reminder, then "
            "stop chasing."
        ),
    ),
    AbandonmentScenario(
        key="link_expired_opened_no_attempt",
        event_type=EventType.CHECKOUT_ABANDONED,
        razorpay_event="payment_link.expired",
        entity_status="expired",
        signal=AbandonmentSignal.OPENED_NO_ATTEMPT,
        failed_attempts=0,
        partial_paid_fraction=0.0,
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.GENUINE_ABANDONMENT,
        weight=5.0,
        rationale=(
            "Reached the page but never started a payment. Still disinterest "
            "rather than friction: nothing blocked them."
        ),
    ),
    AbandonmentScenario(
        key="link_expired_after_failed_attempts",
        event_type=EventType.CHECKOUT_ABANDONED,
        razorpay_event="payment_link.expired",
        entity_status="expired",
        signal=AbandonmentSignal.ATTEMPTED_AND_FAILED,
        failed_attempts=2,
        partial_paid_fraction=0.0,
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.CHECKOUT_FRICTION,
        weight=6.0,
        rationale=(
            "Customer tried more than once and could not get through. Intent was "
            "there, so this is friction worth removing, not disinterest to accept."
        ),
    ),
    AbandonmentScenario(
        key="link_expired_single_failed_attempt",
        event_type=EventType.CHECKOUT_ABANDONED,
        razorpay_event="payment_link.expired",
        entity_status="expired",
        signal=AbandonmentSignal.ATTEMPTED_AND_FAILED,
        failed_attempts=1,
        partial_paid_fraction=0.0,
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.CHECKOUT_FRICTION,
        weight=4.0,
        rationale="One failed attempt then gave up. Friction, on weaker evidence.",
    ),
    AbandonmentScenario(
        key="link_expired_partially_paid",
        event_type=EventType.CHECKOUT_ABANDONED,
        razorpay_event="payment_link.expired",
        entity_status="expired",
        signal=AbandonmentSignal.PARTIALLY_PAID,
        failed_attempts=0,
        partial_paid_fraction=0.4,
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.CHECKOUT_FRICTION,
        weight=1.5,
        rationale=(
            "Part-paid then stalled. Strongest possible intent signal, since they "
            "parted with money before stopping."
        ),
    ),
    # -------------------------------------------------------------- invoices
    AbandonmentScenario(
        key="invoice_expired_unpaid",
        event_type=EventType.INVOICE_OVERDUE,
        razorpay_event="invoice.expired",
        entity_status="expired",
        signal=AbandonmentSignal.NEVER_OPENED,
        failed_attempts=0,
        partial_paid_fraction=0.0,
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.GENUINE_ABANDONMENT,
        weight=3.0,
        rationale=(
            "Invoice passed expire_by with nothing paid and no attempt. Treated as "
            "disinterest; a receivables-specific escalation would be a different "
            "project direction, which project-overview.md excludes."
        ),
    ),
    AbandonmentScenario(
        key="invoice_expired_after_failed_attempts",
        event_type=EventType.INVOICE_OVERDUE,
        razorpay_event="invoice.expired",
        entity_status="expired",
        signal=AbandonmentSignal.ATTEMPTED_AND_FAILED,
        failed_attempts=2,
        partial_paid_fraction=0.0,
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.CHECKOUT_FRICTION,
        weight=2.0,
        rationale=(
            "Payer tried to settle the invoice and failed. Same reasoning as a "
            "checkout: demonstrated intent means friction, not disinterest."
        ),
    ),
    AbandonmentScenario(
        key="invoice_expired_partially_paid",
        event_type=EventType.INVOICE_OVERDUE,
        razorpay_event="invoice.expired",
        entity_status="expired",
        signal=AbandonmentSignal.PARTIALLY_PAID,
        failed_attempts=1,
        partial_paid_fraction=0.55,
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.CHECKOUT_FRICTION,
        weight=1.0,
        rationale=(
            "Part-settled invoice. Razorpay models this with partial_payment plus "
            "amount_paid/amount_due, so the remaining balance is explicit."
        ),
    ),
)


def scenarios_for_event_type(event_type: EventType) -> tuple[AbandonmentScenario, ...]:
    return tuple(s for s in SCENARIOS if s.event_type is event_type)


def covered_root_causes() -> frozenset[RootCause]:
    """Root causes this catalogue can produce.

    Together with ``decline_catalog.covered_root_causes()`` this must cover all
    eight, which is the whole reason Phase 1b exists.
    """
    return frozenset(s.expected_root_cause for s in SCENARIOS)


def scenarios_by_key() -> dict[str, AbandonmentScenario]:
    return {s.key: s for s in SCENARIOS}
