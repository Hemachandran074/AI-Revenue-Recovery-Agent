"""DETECT tests for the Phase 2b abandonment event types.

The interesting part is not the parsing, it is the enrichment. A
``payment_link.expired`` payload cannot say whether the customer ever tried to
pay, so DETECT counts ``payment.failed`` events for that customer during the
link's lifetime. Getting that wrong in either direction is expensive: treating
friction as disinterest abandons recoverable revenue, and treating disinterest as
friction harasses people who were never going to buy.

Persistence tests use the Postgres container and skip when it is absent, because
the enrichment is a timezone-aware time-window query and SQLite does not preserve
timezone information.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app import detect
from app.config import get_settings
from app.detect import MalformedPayloadError, event_id_for, parse_envelope
from app.models import AuditLogEntry, Base, Customer, Event
from app.schemas import EventType, Stage
from app.simulation.generator import generate_batch

PINNED_NOW = datetime(2026, 8, 30, tzinfo=UTC)


@pytest.fixture(scope="module")
def batch():
    return generate_batch(seed=2024, count=90, now=PINNED_NOW)


@pytest.fixture(scope="module")
def link_envelopes(batch):
    return [
        e.envelope
        for e in batch.events
        if e.event_type is EventType.CHECKOUT_ABANDONED
    ]


@pytest.fixture(scope="module")
def invoice_envelopes(batch):
    return [
        e.envelope for e in batch.events if e.event_type is EventType.INVOICE_OVERDUE
    ]


# --------------------------------------------------------------------- parsing


def test_payment_link_expired_is_now_supported(link_envelopes) -> None:
    assert link_envelopes, "batch produced no checkout_abandoned events"
    for env in link_envelopes:
        parsed = parse_envelope(env)
        assert parsed.event_type is EventType.CHECKOUT_ABANDONED
        assert parsed.entity_kind == "payment_link"
        assert parsed.entity_id.startswith("plink_")


def test_invoice_expired_is_now_supported(invoice_envelopes) -> None:
    assert invoice_envelopes, "batch produced no invoice_overdue events"
    for env in invoice_envelopes:
        parsed = parse_envelope(env)
        assert parsed.event_type is EventType.INVOICE_OVERDUE
        assert parsed.entity_kind == "invoice"
        assert parsed.entity_id.startswith("inv_")


def test_abandonment_events_have_no_decline_code(
    link_envelopes, invoice_envelopes
) -> None:
    """Nothing failed, so there is no reason code.

    Inventing one would tell DIAGNOSE a lie about what happened.
    """
    for env in [*link_envelopes, *invoice_envelopes]:
        parsed = parse_envelope(env)
        assert parsed.decline_code is None
        assert parsed.error_code is None
        assert parsed.error_source is None
        assert parsed.method is None


def test_recovery_window_starts_at_issue_not_expiry(
    link_envelopes, invoice_envelopes
) -> None:
    """The 7-day hard stop must run from when money became at risk.

    Measuring from the expiry would restart the clock and let an event be chased
    long past the cut-off.
    """
    for env in [*link_envelopes, *invoice_envelopes]:
        parsed = parse_envelope(env)
        assert parsed.window_start_at < parsed.created_at


def test_event_ids_cannot_collide_across_entity_kinds() -> None:
    """A payment and a link sharing a suffix must not map to one event."""
    assert event_id_for("X1", "payment") != event_id_for("X1", "payment_link")
    assert event_id_for("X1", "payment_link") != event_id_for("X1", "invoice")


def test_payment_event_ids_are_unchanged_by_the_new_signature() -> None:
    """Ids minted before Phase 2b must stay stable, or previously stored events
    would be re-detected as new ones and double-count."""
    assert event_id_for("pay_ABC123") == event_id_for("pay_ABC123", "payment")


# ------------------------------------------------------------ amount at risk


def test_link_amount_at_risk_excludes_what_was_already_paid(link_envelopes) -> None:
    """A part-paid link only risks the outstanding balance.

    Counting the gross amount would overstate the headline "$ at risk" number.
    """
    for env in link_envelopes:
        entity = env["payload"]["payment_link"]["entity"]
        parsed = parse_envelope(env)
        assert parsed.amount_minor == entity["amount"] - entity["amount_paid"]


def test_invoice_amount_at_risk_is_amount_due(invoice_envelopes) -> None:
    for env in invoice_envelopes:
        entity = env["payload"]["invoice"]["entity"]
        parsed = parse_envelope(env)
        assert parsed.amount_minor == entity["amount_due"]


def test_partially_paid_entities_report_less_than_gross(
    link_envelopes, invoice_envelopes
) -> None:
    partials = []
    for env in [*link_envelopes, *invoice_envelopes]:
        key = env["contains"][0]
        entity = env["payload"][key]["entity"]
        if entity["amount_paid"] > 0:
            partials.append(env)
    if not partials:
        pytest.skip("no partially paid entities in this batch")
    for env in partials:
        key = env["contains"][0]
        entity = env["payload"][key]["entity"]
        parsed = parse_envelope(env)
        assert 0 < parsed.amount_minor < entity["amount"]


def test_rejects_link_paid_more_than_its_amount(link_envelopes) -> None:
    env = copy.deepcopy(link_envelopes[0])
    entity = env["payload"]["payment_link"]["entity"]
    entity["amount_paid"] = entity["amount"] + 100
    with pytest.raises(MalformedPayloadError, match="exceeds amount"):
        parse_envelope(env)


def test_rejects_invoice_with_inconsistent_amount_due(invoice_envelopes) -> None:
    env = copy.deepcopy(invoice_envelopes[0])
    env["payload"]["invoice"]["entity"]["amount_due"] = -500
    with pytest.raises(MalformedPayloadError, match="inconsistent"):
        parse_envelope(env)


def test_rejects_missing_issue_timestamp(invoice_envelopes) -> None:
    env = copy.deepcopy(invoice_envelopes[0])
    env["payload"]["invoice"]["entity"]["issued_at"] = None
    with pytest.raises(MalformedPayloadError, match="UNIX seconds"):
        parse_envelope(env)


def test_wrong_entity_key_is_rejected(link_envelopes) -> None:
    """A payment_link.expired carrying a payment entity is malformed."""
    env = copy.deepcopy(link_envelopes[0])
    env["payload"] = {"payment": env["payload"]["payment_link"]}
    with pytest.raises(MalformedPayloadError, match="payload.payment_link.entity"):
        parse_envelope(env)


# ------------------------------------------------------------ customer identity


def test_invoice_customer_id_comes_from_the_entity(invoice_envelopes) -> None:
    """Invoices carry a genuine customer_id, so no hashing is needed."""
    for env in invoice_envelopes:
        entity = env["payload"]["invoice"]["entity"]
        parsed = parse_envelope(env)
        assert parsed.resolved_customer_id == entity["customer_id"]
        assert not parsed.resolved_customer_id.startswith("anon_")


def test_link_customer_id_comes_from_notes(link_envelopes) -> None:
    for env in link_envelopes:
        entity = env["payload"]["payment_link"]["entity"]
        parsed = parse_envelope(env)
        assert parsed.resolved_customer_id == entity["notes"]["customer_id"]


def test_link_without_notes_falls_back_to_contact_hash(link_envelopes) -> None:
    env = copy.deepcopy(link_envelopes[0])
    entity = env["payload"]["payment_link"]["entity"]
    entity["notes"] = []
    parsed = parse_envelope(env)
    assert parsed.resolved_customer_id.startswith("anon_")
    assert entity["customer"]["contact"] not in parsed.resolved_customer_id


# ------------------------------------------------------------------ persistence


@pytest.fixture(scope="module")
def db_sessionmaker():
    url = get_settings().effective_database_url
    if url is None:
        pytest.skip("No database configured; set POSTGRES_PASSWORD in .env")
    engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 3})
    try:
        with engine.connect():
            pass
    except OperationalError as exc:
        engine.dispose()
        pytest.skip(f"Postgres unreachable, run `docker compose up -d --wait`: {exc}")
    Base.metadata.create_all(bind=engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, future=True)
    engine.dispose()


@pytest.fixture
def session(db_sessionmaker) -> Session:
    db_session = db_sessionmaker()
    transaction = db_session.begin_nested()
    try:
        yield db_session
    finally:
        if transaction.is_active:
            transaction.rollback()
        db_session.rollback()
        db_session.close()


pytestmark_integration = pytest.mark.integration


@pytest.mark.integration
def test_link_expiry_is_detected_and_persisted(session, link_envelopes) -> None:
    result = detect.detect_event(session, copy.deepcopy(link_envelopes[0]))
    session.flush()

    assert result.is_duplicate is False
    assert result.event_record.event_type is EventType.CHECKOUT_ABANDONED
    assert result.event_record.decline_code is None

    row = session.get(Event, result.event_record.event_id)
    assert row is not None
    assert row.event_type == str(EventType.CHECKOUT_ABANDONED)
    assert row.provider_event_name == "payment_link.expired"
    assert row.payment_method is None


@pytest.mark.integration
def test_invoice_expiry_is_detected_and_persisted(session, invoice_envelopes) -> None:
    result = detect.detect_event(session, copy.deepcopy(invoice_envelopes[0]))
    session.flush()

    row = session.get(Event, result.event_record.event_id)
    assert row is not None
    assert row.event_type == str(EventType.INVOICE_OVERDUE)
    assert row.provider_event_name == "invoice.expired"
    assert row.provider_invoice_id == row.provider_payment_id


@pytest.mark.integration
def test_abandonment_events_get_an_audit_entry(session, link_envelopes) -> None:
    result = detect.detect_event(session, copy.deepcopy(link_envelopes[0]))
    session.flush()
    entries = session.scalars(
        select(AuditLogEntry).where(
            AuditLogEntry.event_id == result.event_record.event_id
        )
    ).all()
    assert len(entries) == 1
    assert entries[0].stage == str(Stage.DETECT)
    assert entries[0].input_summary["provider_entity_kind"] == "payment_link"


@pytest.mark.integration
def test_link_redelivery_is_idempotent(session, link_envelopes) -> None:
    env = copy.deepcopy(link_envelopes[1])
    first = detect.detect_event(session, env)
    session.flush()
    second = detect.detect_event(session, copy.deepcopy(env))
    session.flush()

    assert second.is_duplicate is True
    assert second.event_record.event_id == first.event_record.event_id
    rows = session.scalars(
        select(Event).where(Event.event_id == first.event_record.event_id)
    ).all()
    assert len(rows) == 1


# ---------------------------------------- the enrichment (Known issue H)


def _link_envelope_for(
    template: dict, *, customer_id: str, contact: str, created: datetime, expired: datetime
) -> dict:
    env = copy.deepcopy(template)
    entity = env["payload"]["payment_link"]["entity"]
    entity["id"] = f"plink_{customer_id[-10:]:>10}".replace(" ", "0")
    entity["notes"] = {"customer_id": customer_id}
    entity["customer"]["contact"] = contact
    entity["created_at"] = int(created.timestamp())
    entity["expired_at"] = int(expired.timestamp())
    entity["updated_at"] = int(expired.timestamp())
    entity["amount_paid"] = 0
    env["created_at"] = int(expired.timestamp())
    return env


def _payment_envelope_for(
    template: dict, *, payment_id: str, customer_id: str, contact: str, at: datetime
) -> dict:
    env = copy.deepcopy(template)
    entity = env["payload"]["payment"]["entity"]
    entity["id"] = payment_id
    entity["order_id"] = f"order_{payment_id[-8:]}"
    entity["notes"] = {"customer_id": customer_id}
    entity["contact"] = contact
    entity["created_at"] = int(at.timestamp())
    env["created_at"] = int(at.timestamp())
    return env


@pytest.mark.integration
def test_no_prior_failures_means_genuine_abandonment(
    session, batch, link_envelopes
) -> None:
    """Nobody tried, so prior_attempts must be 0.

    That is the signal DIAGNOSE uses to conclude disinterest and send a single
    reminder rather than a help flow.
    """
    created = PINNED_NOW - timedelta(days=5)
    expired = PINNED_NOW - timedelta(days=1)
    env = _link_envelope_for(
        link_envelopes[0],
        customer_id="cust_never_tried_01",
        contact="+919700000101",
        created=created,
        expired=expired,
    )

    result = detect.detect_event(session, env)
    session.flush()
    assert result.event_record.prior_attempts == 0


@pytest.mark.integration
def test_prior_failures_during_the_link_lifetime_signal_friction(
    session, batch, link_envelopes
) -> None:
    """THE Phase 2b behaviour.

    Three payment failures arrive while the link is live, then the link expires.
    DETECT must surface those attempts on the expiry event, because the payload
    itself says nothing about them.
    """
    payment_template = next(
        e.envelope for e in batch.events if e.event_type is EventType.PAYMENT_FAILED
    )
    created = PINNED_NOW - timedelta(days=6)
    expired = PINNED_NOW - timedelta(days=1)
    customer_id = "cust_tried_and_failed_01"
    contact = "+919700000202"

    for i in range(3):
        detect.detect_event(
            session,
            _payment_envelope_for(
                payment_template,
                payment_id=f"pay_frictiontest{i:04d}",
                customer_id=customer_id,
                contact=contact,
                at=created + timedelta(days=i + 1),
            ),
        )
    session.flush()

    result = detect.detect_event(
        session,
        _link_envelope_for(
            link_envelopes[0],
            customer_id=customer_id,
            contact=contact,
            created=created,
            expired=expired,
        ),
    )
    session.flush()

    assert result.event_record.prior_attempts == 3
    assert result.event_record.event_type is EventType.CHECKOUT_ABANDONED


@pytest.mark.integration
def test_failures_outside_the_link_lifetime_are_not_counted(
    session, batch, link_envelopes
) -> None:
    """Attempts from before the link existed must not be attributed to it.

    Otherwise an unrelated older failure would make a genuine abandonment look
    like friction, and the customer would get chased when they never engaged.
    """
    payment_template = next(
        e.envelope for e in batch.events if e.event_type is EventType.PAYMENT_FAILED
    )
    customer_id = "cust_old_failure_01"
    contact = "+919700000303"
    link_created = PINNED_NOW - timedelta(days=4)
    link_expired = PINNED_NOW - timedelta(days=1)

    # Long before the link was issued.
    detect.detect_event(
        session,
        _payment_envelope_for(
            payment_template,
            payment_id="pay_outsidewindow1",
            customer_id=customer_id,
            contact=contact,
            at=PINNED_NOW - timedelta(days=30),
        ),
    )
    session.flush()

    result = detect.detect_event(
        session,
        _link_envelope_for(
            link_envelopes[0],
            customer_id=customer_id,
            contact=contact,
            created=link_created,
            expired=link_expired,
        ),
    )
    session.flush()
    assert result.event_record.prior_attempts == 0


@pytest.mark.integration
def test_audit_note_explains_the_enrichment(session, batch, link_envelopes) -> None:
    """The trail must say where prior_attempts came from and what it means.

    A reader has to be able to see that the number was inferred from our own
    history, not read off the provider payload.
    """
    payment_template = next(
        e.envelope for e in batch.events if e.event_type is EventType.PAYMENT_FAILED
    )
    customer_id = "cust_audit_note_01"
    contact = "+919700000404"
    created = PINNED_NOW - timedelta(days=5)
    expired = PINNED_NOW - timedelta(days=1)

    detect.detect_event(
        session,
        _payment_envelope_for(
            payment_template,
            payment_id="pay_auditnote0001",
            customer_id=customer_id,
            contact=contact,
            at=created + timedelta(days=1),
        ),
    )
    session.flush()

    result = detect.detect_event(
        session,
        _link_envelope_for(
            link_envelopes[0],
            customer_id=customer_id,
            contact=contact,
            created=created,
            expired=expired,
        ),
    )
    session.flush()

    entry = session.scalars(
        select(AuditLogEntry).where(
            AuditLogEntry.event_id == result.event_record.event_id
        )
    ).one()
    assert "friction" in (entry.notes or "")
    assert entry.output_summary["attempt_source"] == (
        "customer_history_during_link_lifetime"
    )
    assert entry.output_summary["prior_attempts"] == 1


@pytest.mark.integration
def test_recovery_window_recorded_from_issue_time(session, link_envelopes) -> None:
    created = PINNED_NOW - timedelta(days=5)
    expired = PINNED_NOW - timedelta(days=1)
    env = _link_envelope_for(
        link_envelopes[0],
        customer_id="cust_window_01",
        contact="+919700000505",
        created=created,
        expired=expired,
    )
    result = detect.detect_event(session, env)
    session.flush()

    row = session.get(Event, result.event_record.event_id)
    assert row.first_failure_at == created
    assert row.detected_at == expired


@pytest.mark.integration
def test_amount_at_risk_reaches_the_event_record(session, invoice_envelopes) -> None:
    env = copy.deepcopy(invoice_envelopes[0])
    entity = env["payload"]["invoice"]["entity"]
    result = detect.detect_event(session, env)
    session.flush()
    assert result.event_record.amount == Decimal(entity["amount_due"]) / 100


@pytest.mark.integration
def test_whole_batch_including_abandonment_detects_without_loss(
    session, batch
) -> None:
    """Every event of every type lands exactly once with one audit entry.

    Before Phase 2b, 18 of these were acknowledged and dropped.
    """
    detected = set()
    for event in batch.events:
        result = detect.detect_event(session, copy.deepcopy(event.envelope))
        detected.add(result.event_record.event_id)
    session.flush()

    assert len(detected) == len(batch.events)
    for event_id in detected:
        entries = session.scalars(
            select(AuditLogEntry).where(AuditLogEntry.event_id == event_id)
        ).all()
        assert len(entries) == 1

    types = session.scalars(
        select(Event.event_type).where(Event.event_id.in_(detected))
    ).all()
    assert set(types) == {
        str(EventType.PAYMENT_FAILED),
        str(EventType.CHECKOUT_ABANDONED),
        str(EventType.INVOICE_OVERDUE),
    }


@pytest.mark.integration
def test_customers_are_created_for_abandonment_events(session, invoice_envelopes) -> None:
    result = detect.detect_event(session, copy.deepcopy(invoice_envelopes[0]))
    session.flush()
    customer = session.get(Customer, result.event_record.customer_id)
    assert customer is not None
    assert customer.timezone  # needed by the quiet-hours rule
