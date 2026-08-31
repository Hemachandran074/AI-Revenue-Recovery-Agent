"""DETECT — stage 1. Webhook receiver support and event normalization.

Contract: a signature-verified Razorpay webhook payload becomes one
:class:`~app.schemas.EventRecord`, persisted with an audit entry.

Structured as pure functions plus a thin persistence layer, so normalization is
testable without a database and the rules below are readable in one place.

FOUR THINGS THIS STAGE IS RESPONSIBLE FOR

1. **Rejecting anything unverified.** Signature checking happens before this
   module is reached (``app/signature.py``, called by the route). DETECT assumes
   its input is authentic and validates only shape.

2. **Dropping every card field.** ``EventRecord`` has nowhere to put card data
   and must never gain one. The incoming payload legitimately carries a ``card``
   object with ``last4``/``network``/``iin``; none of it survives normalization.
   Constraint #1.

3. **Enriching from our own records.** ``EventRecord`` requires
   ``customer_history.tenure_days`` and ``past_failures``, and Razorpay sends
   neither. They come from the ``customers`` table. When no profile exists the
   event is still processed, with the gap recorded rather than papered over.

4. **Deriving attempt history.** ``prior_attempts`` and the first-failure
   timestamp are counted from our own prior events for the same order. Razorpay
   does not tell us how many times we have already tried.

IDEMPOTENCY
Razorpay retries deliveries. ``event_id`` is a UUID5 derived from the provider
payment id, so a redelivery maps to the same event and is recognised as a
duplicate instead of creating a second row and double-counting revenue.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.models import Customer, Event
from app.schemas import CustomerHistory, EventRecord, EventType, Stage

# Stable namespace so event ids are reproducible across processes and restarts.
EVENT_ID_NAMESPACE = uuid.UUID("6f9b1a52-0c3d-5e47-9a21-7c8d4e6b1f30")

DEFAULT_TIMEZONE = "Asia/Kolkata"

# Razorpay delivers many event types to one URL. Only these map to a pipeline
# event type; anything else is acknowledged and ignored rather than mishandled.
#
# The abandonment events are real provider events, not inventions:
# `payment_link.expired` is a checkout the customer never completed, and
# `invoice.expired` is a receivable that lapsed. Because Razorpay emits both, no
# polling or sweep is needed and architecture.md's webhook-driven rule holds.
SUPPORTED_EVENTS: dict[str, EventType] = {
    "payment.failed": EventType.PAYMENT_FAILED,
    "payment_link.expired": EventType.CHECKOUT_ABANDONED,
    "invoice.expired": EventType.INVOICE_OVERDUE,
}

# Which key inside `payload` carries the entity, per event.
_ENTITY_KEY_FOR_EVENT: dict[str, str] = {
    "payment.failed": "payment",
    "payment_link.expired": "payment_link",
    "invoice.expired": "invoice",
}

_REQUIRED_ENVELOPE_KEYS = frozenset({"entity", "event", "payload"})
_REQUIRED_ENTITY_KEYS = frozenset({"id", "amount", "currency", "status"})


class MalformedPayloadError(ValueError):
    """The payload is authentic but not a shape we can normalize.

    Distinct from an unsupported event: this means the structure is wrong, which
    is a client error worth surfacing rather than silently accepting.
    """


class UnsupportedEventError(Exception):
    """A genuine Razorpay event we do not process.

    Not an error condition. The route acknowledges these with a 2xx, because a
    non-2xx would make Razorpay retry an event we are never going to want.
    """

    def __init__(self, event_name: str) -> None:
        self.event_name = event_name
        super().__init__(f"unsupported event type: {event_name}")


@dataclass(frozen=True, slots=True)
class ParsedEvent:
    """The fields we care about, lifted out of any supported Razorpay envelope.

    One shape for all three event types so the persistence and audit code stays
    single-path. Payment-only fields (``method``, the ``error_*`` set) are simply
    ``None`` for abandonment events, because an expired link has no decline
    reason — nothing failed, the customer just never finished.
    """

    event_name: str
    event_type: EventType
    # "payment" | "payment_link" | "invoice"
    entity_kind: str
    entity_id: str
    order_id: str | None
    invoice_id: str | None
    # The amount genuinely AT RISK, which is not always the entity's `amount`:
    # a part-paid invoice or link only risks the outstanding balance. Using the
    # gross amount would overstate the headline "$ at risk" figure.
    amount_minor: int
    currency: str
    method: str | None
    decline_code: str | None
    error_code: str | None
    error_description: str | None
    error_source: str | None
    error_step: str | None
    email: str | None
    contact: str | None
    notes: dict[str, Any]
    # When the event happened: the failure, or the expiry.
    created_at: datetime
    # When the money first became at risk: the failure, or when the link/invoice
    # was issued. This is what the 7-day hard stop must measure from.
    window_start_at: datetime
    resolved_customer_id: str


# Retained so older call sites and tests keep working after the rename.
ParsedPayment = ParsedEvent


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """Outcome of detecting one webhook delivery."""

    event_record: EventRecord
    is_duplicate: bool
    profile_was_defaulted: bool
    received_at: datetime


def _resolve_customer_id(
    *,
    explicit_id: Any,
    email: str | None,
    contact: str | None,
    fallback_id: str,
) -> str:
    """Work out which customer an event belongs to.

    Razorpay's failed-payment entity does not reliably carry a customer id, so
    the chain is: an explicit ``customer_id`` (on the entity or in ``notes``),
    then a stable hash of contact or email.

    The hash fallback keeps the same person mapping to the same customer across
    events, which the contact-frequency and max-retries rules depend on. Without
    it every event would look like a brand new customer and the guardrails would
    never fire.

    Invoices are the easy case: they carry a genuine ``customer_id``.
    """
    if explicit_id:
        return str(explicit_id)

    identifier = contact or email
    if identifier:
        digest = hashlib.sha256(str(identifier).encode("utf-8")).hexdigest()[:20]
        return f"anon_{digest}"

    # Last resort: tie it to the entity so the event is still processable.
    return f"unlinked_{fallback_id}"


def event_id_for(entity_id: str, entity_kind: str = "payment") -> str:
    """Deterministic event id for a provider entity.

    Same entity always yields the same id, which is what makes webhook
    redelivery idempotent. ``entity_kind`` is part of the seed so a payment and a
    payment link could never collide, and defaults to ``payment`` so ids minted
    before abandonment support was added stay stable.
    """
    return str(uuid.uuid5(EVENT_ID_NAMESPACE, f"razorpay:{entity_kind}:{entity_id}"))


def _require_int(entity: dict[str, Any], key: str, label: str) -> int:
    value = entity.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedPayloadError(
            f"{label} {key} must be an integer in minor units (paise)"
        )
    if value < 0:
        raise MalformedPayloadError(f"{label} {key} cannot be negative")
    return value


def _require_unix(entity: dict[str, Any], key: str, label: str) -> datetime:
    value = entity.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MalformedPayloadError(f"{label} {key} must be UNIX seconds")
    return datetime.fromtimestamp(value, tz=UTC)


def _normalise_notes(raw: Any) -> dict[str, Any]:
    """Razorpay sends ``notes`` as an object when populated and ``[]`` when not."""
    return raw if isinstance(raw, dict) else {}


def parse_envelope(payload: dict[str, Any]) -> ParsedEvent:
    """Validate and flatten a Razorpay webhook envelope.

    Dispatches on the event name to the right entity parser. Raises
    :class:`MalformedPayloadError` for a bad shape and
    :class:`UnsupportedEventError` for an event type we do not handle.
    """
    if not isinstance(payload, dict):
        raise MalformedPayloadError("payload must be a JSON object")

    missing = _REQUIRED_ENVELOPE_KEYS - set(payload)
    if missing:
        raise MalformedPayloadError(f"envelope missing keys: {sorted(missing)}")

    event_name = payload.get("event")
    if not isinstance(event_name, str):
        raise MalformedPayloadError("envelope 'event' must be a string")
    if event_name not in SUPPORTED_EVENTS:
        raise UnsupportedEventError(event_name)

    entity_key = _ENTITY_KEY_FOR_EVENT[event_name]
    try:
        entity = payload["payload"][entity_key]["entity"]
    except (KeyError, TypeError) as exc:
        raise MalformedPayloadError(
            f"expected payload.{entity_key}.entity in the envelope"
        ) from exc
    if not isinstance(entity, dict):
        raise MalformedPayloadError(f"payload.{entity_key}.entity must be an object")

    entity_missing = _REQUIRED_ENTITY_KEYS - set(entity)
    if entity_missing:
        raise MalformedPayloadError(
            f"{entity_key} entity missing keys: {sorted(entity_missing)}"
        )

    if entity_key == "payment":
        return _parse_payment_failed(event_name, entity)
    if entity_key == "payment_link":
        return _parse_payment_link_expired(event_name, entity)
    return _parse_invoice_expired(event_name, entity)


def _parse_payment_failed(event_name: str, entity: dict[str, Any]) -> ParsedEvent:
    """A ``payment.failed`` payment entity."""
    amount = _require_int(entity, "amount", "payment")
    created_at = _require_unix(entity, "created_at", "payment")
    notes = _normalise_notes(entity.get("notes"))

    return ParsedEvent(
        event_name=event_name,
        event_type=SUPPORTED_EVENTS[event_name],
        entity_kind="payment",
        entity_id=str(entity["id"]),
        order_id=entity.get("order_id"),
        invoice_id=entity.get("invoice_id"),
        amount_minor=amount,
        currency=str(entity["currency"]),
        method=entity.get("method"),
        # Razorpay's error_reason is the categorical decline signal, so it is what
        # maps to architecture.md's decline_code. error_code is too coarse
        # (almost always BAD_REQUEST_ERROR) to classify against.
        decline_code=entity.get("error_reason"),
        error_code=entity.get("error_code") or None,
        error_description=entity.get("error_description") or None,
        error_source=entity.get("error_source"),
        error_step=entity.get("error_step"),
        email=entity.get("email"),
        contact=entity.get("contact"),
        notes=notes,
        created_at=created_at,
        window_start_at=created_at,
        resolved_customer_id=_resolve_customer_id(
            explicit_id=entity.get("customer_id") or notes.get("customer_id"),
            email=entity.get("email"),
            contact=entity.get("contact"),
            fallback_id=str(entity["id"]),
        ),
    )


def _parse_payment_link_expired(event_name: str, entity: dict[str, Any]) -> ParsedEvent:
    """A ``payment_link.expired`` entity: a checkout the customer never finished.

    There is no decline reason, because nothing failed. That is the point — the
    absence of an error is what distinguishes an abandonment from a degradation.

    Amount at risk is the unpaid balance. A part-paid link only risks what is
    still outstanding, and counting the gross amount would inflate the headline
    figure.
    """
    amount = _require_int(entity, "amount", "payment_link")
    amount_paid = _require_int(entity, "amount_paid", "payment_link")
    if amount_paid > amount:
        raise MalformedPayloadError("payment_link amount_paid exceeds amount")

    created_at = _require_unix(entity, "created_at", "payment_link")
    # Payment links use 0 rather than null for unset timestamps, so fall back to
    # created_at when the expiry stamp is absent.
    expired_raw = entity.get("expired_at") or entity.get("updated_at")
    expired_at = (
        datetime.fromtimestamp(expired_raw, tz=UTC)
        if isinstance(expired_raw, int) and not isinstance(expired_raw, bool)
        and expired_raw > 0
        else created_at
    )

    notes = _normalise_notes(entity.get("notes"))
    customer = entity.get("customer") if isinstance(entity.get("customer"), dict) else {}
    email = customer.get("email")
    contact = customer.get("contact")

    return ParsedEvent(
        event_name=event_name,
        event_type=SUPPORTED_EVENTS[event_name],
        entity_kind="payment_link",
        entity_id=str(entity["id"]),
        order_id=entity.get("order_id"),
        invoice_id=None,
        amount_minor=amount - amount_paid,
        currency=str(entity["currency"]),
        method=None,
        decline_code=None,
        error_code=None,
        error_description=None,
        error_source=None,
        error_step=None,
        email=email,
        contact=contact,
        notes=notes,
        created_at=expired_at,
        window_start_at=created_at,
        resolved_customer_id=_resolve_customer_id(
            explicit_id=notes.get("customer_id"),
            email=email,
            contact=contact,
            fallback_id=str(entity["id"]),
        ),
    )


def _parse_invoice_expired(event_name: str, entity: dict[str, Any]) -> ParsedEvent:
    """An ``invoice.expired`` entity: a receivable that lapsed unpaid.

    Invoices are the well-behaved case. They carry a real ``customer_id``, and
    ``amount_due`` states the outstanding balance directly, so the at-risk figure
    needs no arithmetic and a part-settled invoice is visible in the payload.
    """
    amount = _require_int(entity, "amount", "invoice")
    # amount_due is authoritative for what is still at risk.
    amount_due = entity.get("amount_due")
    if isinstance(amount_due, bool) or not isinstance(amount_due, int):
        amount_due = amount - _require_int(entity, "amount_paid", "invoice")
    if amount_due < 0 or amount_due > amount:
        raise MalformedPayloadError("invoice amount_due is inconsistent with amount")

    issued_at = _require_unix(entity, "issued_at", "invoice")
    expired_raw = entity.get("expired_at") or entity.get("expire_by")
    expired_at = (
        datetime.fromtimestamp(expired_raw, tz=UTC)
        if isinstance(expired_raw, int) and not isinstance(expired_raw, bool)
        and expired_raw > 0
        else issued_at
    )

    notes = _normalise_notes(entity.get("notes"))
    details = (
        entity.get("customer_details")
        if isinstance(entity.get("customer_details"), dict)
        else {}
    )
    email = details.get("email") or details.get("customer_email")
    contact = details.get("contact") or details.get("customer_contact")

    return ParsedEvent(
        event_name=event_name,
        event_type=SUPPORTED_EVENTS[event_name],
        entity_kind="invoice",
        entity_id=str(entity["id"]),
        order_id=entity.get("order_id"),
        invoice_id=str(entity["id"]),
        amount_minor=amount_due,
        currency=str(entity["currency"]),
        method=None,
        decline_code=None,
        error_code=None,
        error_description=None,
        error_source=None,
        error_step=None,
        email=email,
        contact=contact,
        notes=notes,
        created_at=expired_at,
        window_start_at=issued_at,
        resolved_customer_id=_resolve_customer_id(
            explicit_id=entity.get("customer_id") or notes.get("customer_id"),
            email=email,
            contact=contact,
            fallback_id=str(entity["id"]),
        ),
    )


def to_event_record(
    parsed: ParsedPayment,
    *,
    customer_id: str,
    tenure_days: int,
    past_failures: int,
    prior_attempts: int,
) -> EventRecord:
    """Build the normalized :class:`EventRecord`.

    ``customer_id`` is passed in rather than read off ``parsed`` because identity
    reconciliation happens against the database (see
    :func:`_get_or_create_customer`) and can resolve to an established record
    whose id differs from the one derived from this payload. Taking it from
    ``parsed`` here was a real bug: the stored row linked to the right customer
    while the record handed to DIAGNOSE and DECIDE carried a different id, so the
    guardrails would have keyed on the wrong person.

    Amount converts paise to rupees as an exact ``Decimal``; dividing an integer
    number of paise by 100 is lossless, unlike float arithmetic.

    Nothing card-related is carried across. ``EventRecord`` has no field for it,
    and ``extra='forbid'`` on the model means an attempt to add one fails loudly.
    """
    return EventRecord(
        event_id=event_id_for(parsed.entity_id, parsed.entity_kind),
        customer_id=customer_id,
        event_type=parsed.event_type,
        decline_code=parsed.decline_code,
        amount=Decimal(parsed.amount_minor) / 100,
        currency=parsed.currency,
        prior_attempts=prior_attempts,
        customer_history=CustomerHistory(
            tenure_days=tenure_days, past_failures=past_failures
        ),
        detected_at=parsed.created_at,
    )


def _get_or_create_customer(
    session: Session, parsed: ParsedPayment
) -> tuple[Customer, bool]:
    """Fetch the customer profile, creating a defaulted one if absent.

    Returns ``(customer, was_defaulted)``. A defaulted profile means the
    quiet-hours rule will run against an assumed timezone, which the audit trail
    records rather than presenting as known.

    IDENTITY RECONCILIATION. Razorpay sends ``notes`` as an empty list roughly a
    third of the time, so the same person arrives with an explicit
    ``notes.customer_id`` on some events and without it on others. Keying purely
    on the resolved id would create two records for one customer and split their
    history, which would quietly defeat ``max_retries`` and
    ``contact_frequency`` — the very rules that depend on seeing every attempt.

    So when the resolved id is a derived hash, fall back to matching on contact
    or email before creating anything. Consistency of identity matters more than
    which label wins, so an established record keeps its id even if the merchant's
    own id shows up later. Merging two pre-existing records is deliberately not
    attempted; that needs a decision about which profile data survives.
    """
    customer = session.get(Customer, parsed.resolved_customer_id)
    if customer is not None:
        return customer, customer.profile_source == "defaulted"

    # Reconcile before creating anything, in BOTH directions. Restricting this to
    # derived ids was not enough: an explicit notes.customer_id arriving after a
    # hash-derived record already existed for the same contact still produced a
    # second row. Since Razorpay omits notes on roughly a third of deliveries,
    # both orderings occur inside a single batch.
    existing = _find_customer_by_contact(session, parsed)
    if existing is not None:
        return existing, existing.profile_source == "defaulted"

    customer = Customer(
        customer_id=parsed.resolved_customer_id,
        email=parsed.email,
        contact=parsed.contact,
        timezone=DEFAULT_TIMEZONE,
        tenure_days=0,
        past_failures=0,
        profile_source="defaulted",
    )
    session.add(customer)
    session.flush()
    return customer, True


def _find_customer_by_contact(
    session: Session, parsed: ParsedPayment
) -> Customer | None:
    """Re-identify a customer by contact, falling back to email only if absent.

    Email is deliberately NOT tried while a contact is available. Two different
    people can share an email address, and merging them would pool their attempt
    histories — making ``max_retries`` trip for someone who had not been
    contacted, so the agent would stop chasing recoverable revenue. Contact is the
    stronger key and Razorpay sends it on essentially every payment.
    """
    if parsed.contact:
        return session.scalars(
            select(Customer).where(Customer.contact == parsed.contact).limit(1)
        ).first()
    if parsed.email:
        return session.scalars(
            select(Customer).where(Customer.email == parsed.email).limit(1)
        ).first()
    return None


def _attempt_history(
    session: Session, parsed: ParsedEvent, fallback_at: datetime
) -> tuple[int, datetime]:
    """Count prior attempts for this order and find its first failure time.

    Derived from our own event history, because the webhook carries no attempt
    counter. Falls back to the payment itself when this is the first sighting.
    """
    if not parsed.order_id:
        return 0, fallback_at

    row = session.execute(
        select(func.count(Event.event_id), func.min(Event.first_failure_at)).where(
            Event.provider_order_id == parsed.order_id
        )
    ).one()
    count, earliest = row[0] or 0, row[1]
    return count, min(earliest, fallback_at) if earliest else fallback_at


def _abandonment_attempt_history(
    session: Session, parsed: ParsedEvent, customer_id: str
) -> int:
    """How many payment attempts this customer failed while the link was live.

    THIS IS THE ENRICHMENT KNOWN ISSUE H CALLS FOR.

    A ``payment_link.expired`` payload cannot distinguish ``checkout_friction``
    from ``genuine_abandonment``: the ``payments`` array was empty on every real
    link inspected, including one that had been paid. So the evidence has to come
    from what we already received — ``payment.failed`` events for this customer
    between the link being issued and it expiring.

    A non-zero count means the customer tried and could not get through, which is
    friction worth removing. Zero means they never engaged, which is disinterest
    to accept after one reminder. Getting this backwards would either harass
    people who were never interested or abandon revenue that was recoverable.

    Only genuine payment failures count. Counting other abandonment events would
    let one expiry inflate the apparent attempt history of another.
    """
    return (
        session.scalar(
            select(func.count(Event.event_id)).where(
                Event.customer_id == customer_id,
                Event.event_type == str(EventType.PAYMENT_FAILED),
                Event.detected_at >= parsed.window_start_at,
                Event.detected_at <= parsed.created_at,
            )
        )
        or 0
    )


def detect_event(session: Session, payload: dict[str, Any]) -> DetectionResult:
    """Normalize and persist one verified webhook delivery.

    Caller owns the transaction. Raises :class:`MalformedPayloadError` or
    :class:`UnsupportedEventError` for input this stage will not accept.
    """
    received_at = datetime.now(UTC)
    parsed = parse_envelope(payload)

    # Constraint #1 enforced at the storage boundary, before anything is written.
    audit.assert_no_sensitive_card_data(payload)

    event_id = event_id_for(parsed.entity_id, parsed.entity_kind)
    existing = session.get(Event, event_id)
    if existing is not None:
        # Redelivery. Return the original rather than writing a second row.
        record = to_event_record(
            parsed,
            customer_id=existing.customer_id,
            tenure_days=existing.tenure_days,
            past_failures=existing.past_failures,
            prior_attempts=existing.prior_attempts,
        )
        audit.record(
            session,
            event_id=event_id,
            stage=Stage.DETECT,
            input_summary={
                "provider_entity_id": parsed.entity_id,
                "provider_entity_kind": parsed.entity_kind,
                "provider_event": parsed.event_name,
            },
            output_summary={"outcome": "duplicate_ignored"},
            notes=(
                f"Webhook redelivered for an already-detected {parsed.entity_kind}. "
                "No new event created, so amount at risk is not double-counted."
            ),
        )
        return DetectionResult(
            event_record=record,
            is_duplicate=True,
            profile_was_defaulted=False,
            received_at=received_at,
        )

    customer, was_defaulted = _get_or_create_customer(session, parsed)

    if parsed.event_type is EventType.PAYMENT_FAILED:
        prior_attempts, first_failure_at = _attempt_history(
            session, parsed, parsed.created_at
        )
    else:
        # Abandonment: the attempt count is the friction signal, enriched from our
        # own history because the payload does not carry it (Known issue H). The
        # recovery window starts when the link or invoice was issued, not when it
        # expired, so the 7-day hard stop measures from when money first became
        # at risk.
        prior_attempts = _abandonment_attempt_history(
            session, parsed, customer.customer_id
        )
        first_failure_at = parsed.window_start_at

    record = to_event_record(
        parsed,
        customer_id=customer.customer_id,
        tenure_days=customer.tenure_days,
        past_failures=customer.past_failures,
        prior_attempts=prior_attempts,
    )

    session.add(
        Event(
            event_id=event_id,
            provider="razorpay",
            # Column name predates abandonment support: it holds the provider's
            # primary entity id for the event, which is a pay_ id for failures and
            # a plink_ / inv_ id for expiries. Renaming it needs the table dropped,
            # so the audit summary below uses accurate keys instead.
            provider_payment_id=parsed.entity_id,
            provider_order_id=parsed.order_id,
            provider_invoice_id=parsed.invoice_id,
            provider_event_name=parsed.event_name,
            customer_id=customer.customer_id,
            event_type=str(parsed.event_type),
            decline_code=parsed.decline_code,
            error_code=parsed.error_code,
            error_description=parsed.error_description,
            error_source=parsed.error_source,
            error_step=parsed.error_step,
            payment_method=parsed.method,
            amount_minor=parsed.amount_minor,
            currency=parsed.currency,
            prior_attempts=prior_attempts,
            tenure_days=customer.tenure_days,
            past_failures=customer.past_failures,
            first_failure_at=first_failure_at,
            detected_at=parsed.created_at,
            received_at=received_at,
            raw_payload=payload,
        )
    )
    session.flush()

    audit.record(
        session,
        event_id=event_id,
        stage=Stage.DETECT,
        input_summary={
            "provider_entity_id": parsed.entity_id,
            "provider_entity_kind": parsed.entity_kind,
            "provider_order_id": parsed.order_id,
            "provider_event": parsed.event_name,
            "payment_method": parsed.method,
            "error_reason": parsed.decline_code,
            "error_source": parsed.error_source,
            "error_step": parsed.error_step,
        },
        output_summary={
            "outcome": "detected",
            "event_type": str(parsed.event_type),
            "decline_code": parsed.decline_code,
            "amount_at_risk_minor": parsed.amount_minor,
            "currency": parsed.currency,
            "prior_attempts": prior_attempts,
            "attempt_source": (
                "order_history"
                if parsed.event_type is EventType.PAYMENT_FAILED
                else "customer_history_during_link_lifetime"
            ),
            "customer_id": customer.customer_id,
            "customer_timezone": customer.timezone,
            "customer_profile_source": customer.profile_source,
            "recovery_window_started_at": first_failure_at.isoformat(),
            "detected_at": parsed.created_at.isoformat(),
            "received_at": received_at.isoformat(),
        },
        notes="; ".join(
            note
            for note in (
                (
                    "Customer profile was not on record; tenure and past failures "
                    "defaulted to 0 and timezone assumed "
                    f"{DEFAULT_TIMEZONE}. Quiet-hours evaluation for this event "
                    "rests on that assumption."
                    if was_defaulted
                    else None
                ),
                (
                    "Abandonment event: the expiry payload carries no attempt "
                    "record, so prior_attempts was counted from payment.failed "
                    "events for this customer while the link/invoice was live. "
                    f"Found {prior_attempts}, meaning "
                    + (
                        "the customer tried and could not complete (friction)."
                        if prior_attempts
                        else "no attempt was ever made (disinterest)."
                    )
                    if parsed.event_type is not EventType.PAYMENT_FAILED
                    else None
                ),
            )
            if note
        )
        or None,
    )

    return DetectionResult(
        event_record=record,
        is_duplicate=False,
        profile_was_defaulted=was_defaulted,
        received_at=received_at,
    )
