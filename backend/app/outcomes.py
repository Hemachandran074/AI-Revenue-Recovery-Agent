"""Outcome confirmation: the only thing allowed to say money came back.

``architecture.md``'s pipeline diagram ends with an arrow this codebase did not
have: *webhook confirms outcome -> audit log + recovered-$ counter updated*.
Without it ``amount_recovered_minor`` stays null forever, so the headline metric
in ``project-overview.md`` -- $ recovered / $ at risk -- is structurally zero and
the claim "then proves how much money it recovered" cannot be made. This module
is that arrow.

## Why this is the only writer of that number

EXECUTE knows it sent a message. It does not know whether anyone paid. Letting
any earlier stage set a recovered amount would turn a delivery statistic into a
revenue claim, which is the one number in this project that must never be
optimistic. So the value comes from a signed provider webhook stating that money
moved, and from nowhere else.

## Attribution is recorded, not assumed

Two different things can produce a payment after a failure, and they are not
equally attributable to the agent:

``recovery_link_paid``     The customer paid through the Razorpay-hosted link
                          this agent created and sent. Unambiguous: that link
                          exists only because of the recovery action.
``same_invoice_paid``      The invoice we flagged as overdue was paid.
``same_order_captured``    The order that failed was later captured. Genuinely
                          recovered revenue, but the customer may have retried on
                          their own and we cannot prove the nudge caused it.

The distinction is stored on every confirmation so a reader can discount the
weaker kind rather than having to trust one blended figure.

## One payment credits exactly one event

A retry chain produces several at-risk events for a single order. If a payment
credited all of them, the recovered total would be multiplied by the length of
the chain -- inflating the headline metric by the very behaviour the agent exists
to handle. Exactly one event is credited: the most recent at-risk event for that
order or invoice.

## Redelivery cannot double-count

Razorpay retries webhook deliveries. ``amount_recovered_minor`` is *assigned* the
captured amount rather than incremented, so re-applying the same confirmation is
a no-op by construction rather than by a guard that could be forgotten.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.models import Event, ExecutionRecord
from app.schemas import CustomerOutcome, Stage

logger = logging.getLogger(__name__)

# Razorpay events that mean money arrived. Anything else is not our business
# here; DETECT owns the at-risk events and the route ignores the rest.
SUPPORTED_OUTCOME_EVENTS = frozenset(
    {"payment_link.paid", "invoice.paid", "order.paid", "payment.captured"}
)

# Strongest to weakest. Recorded on the confirmation so the number can be read
# with the right amount of confidence.
ATTRIBUTION_RECOVERY_LINK = "recovery_link_paid"
ATTRIBUTION_INVOICE = "same_invoice_paid"
ATTRIBUTION_ORDER = "same_order_captured"


class MalformedOutcomeError(ValueError):
    """The payload is authentic but not a shape we can read."""


@dataclass(frozen=True, slots=True)
class Confirmation:
    """A payment matched to an event we had flagged as at risk."""

    event_id: str
    amount_recovered_minor: int
    attribution: str
    provider_payment_id: str | None
    matched_on: str
    already_confirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "amount_recovered_minor": self.amount_recovered_minor,
            "amount_recovered": round(self.amount_recovered_minor / 100, 2),
            "attribution": self.attribution,
            "provider_payment_id": self.provider_payment_id,
            "matched_on": self.matched_on,
            "already_confirmed": self.already_confirmed,
        }


@dataclass(frozen=True, slots=True)
class Unmatched:
    """A genuine payment that belongs to nothing we were tracking.

    Returned rather than raised, and never guessed at. A payment we cannot tie to
    an at-risk event is somebody paying for something normally, and crediting the
    agent for it would be inventing recovered revenue.
    """

    reason: str
    provider_payment_id: str | None = None
    order_id: str | None = None
    invoice_id: str | None = None
    payment_link_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched": False,
            "reason": self.reason,
            "provider_payment_id": self.provider_payment_id,
            "order_id": self.order_id,
            "invoice_id": self.invoice_id,
            "payment_link_id": self.payment_link_id,
        }


@dataclass(frozen=True, slots=True)
class ParsedPayment:
    """The fields a confirmation needs, pulled out of any of the four events."""

    event_name: str
    provider_payment_id: str | None
    order_id: str | None
    invoice_id: str | None
    payment_link_id: str | None
    amount_paid_minor: int


def _entity(payload: dict[str, Any], key: str) -> dict[str, Any]:
    container = payload.get("payload") or {}
    block = container.get(key) or {}
    entity = block.get("entity") or {}
    return entity if isinstance(entity, dict) else {}


def _as_minor(value: Any) -> int | None:
    """Razorpay sends amounts as integer paise. Anything else is not an amount."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def parse_outcome(payload: dict[str, Any]) -> ParsedPayment:
    """Read a paid-event envelope into the fields matching needs.

    All four supported events carry a ``payment`` entity, which is the
    authoritative record of what was actually captured. The container entity
    (link, invoice, order) is used for the ids and as an amount fallback.
    """
    if not isinstance(payload, dict):
        raise MalformedOutcomeError("payload is not an object")
    event_name = payload.get("event")
    if not isinstance(event_name, str) or event_name not in SUPPORTED_OUTCOME_EVENTS:
        raise MalformedOutcomeError(f"not an outcome event: {event_name!r}")

    payment = _entity(payload, "payment")
    link = _entity(payload, "payment_link")
    invoice = _entity(payload, "invoice")
    order = _entity(payload, "order")

    # The captured payment amount is the truth about how much money moved. The
    # container's amount_paid is a fallback for the events that omit the payment
    # block, and `amount` is deliberately NOT used as a further fallback: that is
    # what was ASKED for, not what was received, and confusing the two would
    # overstate recovery on a partial payment.
    amount = _as_minor(payment.get("amount"))
    if amount is None:
        for entity in (link, invoice, order):
            amount = _as_minor(entity.get("amount_paid"))
            if amount is not None:
                break
    if amount is None:
        raise MalformedOutcomeError(
            f"{event_name} carries no captured amount; refusing to guess one"
        )
    if amount <= 0:
        raise MalformedOutcomeError(f"{event_name} reports a non-positive amount")

    return ParsedPayment(
        event_name=event_name,
        provider_payment_id=payment.get("id") or None,
        order_id=payment.get("order_id") or order.get("id") or None,
        invoice_id=payment.get("invoice_id") or invoice.get("id") or None,
        payment_link_id=link.get("id") or None,
        amount_paid_minor=amount,
    )


def _newest_event_for(
    session: Session, *, column: Any, value: str
) -> Event | None:
    """The most recent at-risk event matching a provider id.

    Newest wins because a retry chain holds several events for one order and only
    one recovery happened. Crediting them all would multiply the headline figure.
    """
    return session.scalars(
        select(Event).where(column == value).order_by(Event.detected_at.desc()).limit(1)
    ).first()


def _match(
    session: Session, parsed: ParsedPayment
) -> tuple[Event, str, str] | None:
    """Find the at-risk event this payment settles, strongest signal first."""
    if parsed.payment_link_id:
        execution = session.scalars(
            select(ExecutionRecord).where(
                ExecutionRecord.recovery_link_id == parsed.payment_link_id
            )
        ).first()
        if execution is not None:
            event = session.get(Event, execution.event_id)
            if event is not None:
                return event, ATTRIBUTION_RECOVERY_LINK, parsed.payment_link_id

    if parsed.invoice_id:
        event = _newest_event_for(
            session, column=Event.provider_invoice_id, value=parsed.invoice_id
        )
        if event is not None:
            return event, ATTRIBUTION_INVOICE, parsed.invoice_id

    if parsed.order_id:
        event = _newest_event_for(
            session, column=Event.provider_order_id, value=parsed.order_id
        )
        if event is not None:
            return event, ATTRIBUTION_ORDER, parsed.order_id

    return None


def confirm_outcome(
    session: Session, payload: dict[str, Any], *, now: datetime | None = None
) -> Confirmation | Unmatched:
    """Record that a payment settled an event we had flagged as at risk.

    Caller owns the transaction. Raises :class:`MalformedOutcomeError` only for a
    payload shape we cannot read; an authentic payment that matches nothing comes
    back as :class:`Unmatched` rather than an error, because it is a normal
    occurrence and not a fault.
    """
    parsed = parse_outcome(payload)
    confirmed_at = now or datetime.now(UTC)

    matched = _match(session, parsed)
    if matched is None:
        logger.info(
            "outcome webhook %s matched no at-risk event (order=%s invoice=%s link=%s)",
            parsed.event_name,
            parsed.order_id,
            parsed.invoice_id,
            parsed.payment_link_id,
        )
        return Unmatched(
            reason=(
                "no at-risk event matches this payment. Not credited: a payment we "
                "cannot tie to a tracked failure is somebody paying normally, and "
                "counting it would invent recovered revenue."
            ),
            provider_payment_id=parsed.provider_payment_id,
            order_id=parsed.order_id,
            invoice_id=parsed.invoice_id,
            payment_link_id=parsed.payment_link_id,
        )

    event, attribution, matched_value = matched
    execution = session.get(ExecutionRecord, event.event_id)
    if execution is None:
        # EXECUTE never ran for this event, so there is no row to carry the
        # outcome. Recording the money anyway would leave a recovered amount with
        # no action to attribute it to.
        return Unmatched(
            reason=(
                f"event {event.event_id} has no execution record, so there is no "
                "recovery action to attribute this payment to"
            ),
            provider_payment_id=parsed.provider_payment_id,
            order_id=parsed.order_id,
            invoice_id=parsed.invoice_id,
            payment_link_id=parsed.payment_link_id,
        )

    already = execution.customer_outcome == str(CustomerOutcome.RECOVERED)

    # Assigned, never incremented, so a redelivered webhook is a no-op.
    execution.customer_outcome = str(CustomerOutcome.RECOVERED)
    execution.amount_recovered_minor = parsed.amount_paid_minor
    execution.outcome_updated_at = confirmed_at

    confirmation = Confirmation(
        event_id=event.event_id,
        amount_recovered_minor=parsed.amount_paid_minor,
        attribution=attribution,
        provider_payment_id=parsed.provider_payment_id,
        matched_on=matched_value,
        already_confirmed=already,
    )

    if not already:
        # Only the first confirmation gets an audit entry. A redelivery adds no
        # information and would make the trail look like two separate payments.
        #
        # Filed under EXECUTE rather than a fifth stage: `Stage` mirrors
        # architecture.md's four pipeline stages, and this is the outcome of the
        # execute action rather than a new stage of the pipeline. The notes say
        # plainly which entry is which.
        audit.record(
            session,
            event_id=event.event_id,
            stage=Stage.EXECUTE,
            input_summary={
                "confirmation_event": parsed.event_name,
                "provider_payment_id": parsed.provider_payment_id,
                "matched_on": matched_value,
                "amount_at_risk_minor": event.amount_minor,
            },
            output_summary={
                "customer_outcome": str(CustomerOutcome.RECOVERED),
                "amount_recovered_minor": parsed.amount_paid_minor,
                "attribution": attribution,
                "partial": parsed.amount_paid_minor < event.amount_minor,
                "confirmed_at": confirmed_at.isoformat(),
            },
            notes=(
                f"Outcome confirmed by provider webhook {parsed.event_name}. "
                f"Attribution: {attribution}. This is the only source permitted to "
                "set a recovered amount."
            ),
        )

    session.flush()
    logger.info(
        "outcome confirmed for %s: %s paise via %s",
        event.event_id,
        parsed.amount_paid_minor,
        attribution,
    )
    return confirmation
