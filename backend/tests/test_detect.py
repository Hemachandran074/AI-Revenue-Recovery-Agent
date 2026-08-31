"""DETECT stage tests.

Split in two:

* Pure parsing/normalization tests, which need no database.
* Persistence tests marked ``integration``, which use the Postgres container and
  skip cleanly when it is not running.

The normalization tests run against payloads from the real Phase 1 generator
rather than hand-written dicts, so DETECT is exercised against the same
Razorpay-shaped input it will see in production.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app import audit, detect
from app.audit import SensitiveCardDataError
from app.config import get_settings
from app.detect import (
    MalformedPayloadError,
    UnsupportedEventError,
    event_id_for,
    parse_envelope,
    to_event_record,
)
from app.models import AuditLogEntry, Base, Customer, Event
from app.schemas import EventRecord, EventType, Stage
from app.simulation.generator import generate_batch

# ---------------------------------------------------------------- pure fixtures


@pytest.fixture(scope="module")
def batch():
    return generate_batch(seed=99, count=40, now=datetime(2026, 8, 30, tzinfo=UTC))


@pytest.fixture(scope="module")
def envelopes(batch):
    """``payment.failed`` envelopes only.

    A batch also contains ``payment_link.expired`` and ``invoice.expired``
    deliveries as of Phase 1b. DETECT does not map those yet (Phase 2b), so they
    are excluded here and their current handling is asserted in
    ``test_webhook_endpoint.py`` instead.
    """
    return [
        e.envelope
        for e in batch.events
        if e.event_type is EventType.PAYMENT_FAILED
    ]


@pytest.fixture
def envelope(envelopes):
    return copy.deepcopy(envelopes[0])


# --------------------------------------------------------------- envelope parsing


def test_parses_every_generated_envelope(envelopes) -> None:
    """The whole Phase 1 batch must survive parsing.

    If the generator and DETECT disagree about shape, one of them is wrong about
    Razorpay and the pipeline breaks on the first real webhook.
    """
    for env in envelopes:
        parsed = parse_envelope(env)
        assert parsed.event_type is EventType.PAYMENT_FAILED
        assert parsed.entity_kind == "payment"
        assert parsed.entity_id.startswith("pay_")
        assert parsed.amount_minor > 0
        assert parsed.currency == "INR"


def test_decline_code_carries_razorpay_error_reason(envelope) -> None:
    """architecture.md's decline_code maps to error_reason, not error_code.

    error_code is almost always BAD_REQUEST_ERROR and carries no diagnostic
    signal, so classifying against it would be useless.
    """
    entity = envelope["payload"]["payment"]["entity"]
    parsed = parse_envelope(envelope)
    assert parsed.decline_code == entity["error_reason"]


def test_created_at_becomes_timezone_aware_utc(envelope) -> None:
    parsed = parse_envelope(envelope)
    assert parsed.created_at.tzinfo is not None
    assert parsed.created_at.utcoffset() == UTC.utcoffset(None)


def test_blank_error_fields_survive_parsing(envelopes) -> None:
    """Razorpay's own card sample has empty/null error fields.

    These must parse into a null decline_code rather than crashing, because they
    are exactly the events that need to escalate to human review.
    """
    blanks = [
        e
        for e in envelopes
        if not e["payload"]["payment"]["entity"].get("error_reason")
    ]
    if not blanks:
        pytest.skip("no blank-error events in this batch")
    for env in blanks:
        assert parse_envelope(env).decline_code is None


def test_empty_list_notes_are_tolerated(envelope) -> None:
    """Razorpay sends notes as [] when empty and an object when populated."""
    envelope["payload"]["payment"]["entity"]["notes"] = []
    assert parse_envelope(envelope).notes == {}


# ------------------------------------------------------------- rejection cases


def test_rejects_non_dict_payload() -> None:
    with pytest.raises(MalformedPayloadError):
        parse_envelope([])  # type: ignore[arg-type]


@pytest.mark.parametrize("missing", ["entity", "event", "payload"])
def test_rejects_envelope_missing_required_keys(envelope, missing) -> None:
    del envelope[missing]
    with pytest.raises(MalformedPayloadError, match="missing keys"):
        parse_envelope(envelope)


def test_rejects_missing_payment_entity(envelope) -> None:
    envelope["payload"] = {"refund": {"entity": {}}}
    with pytest.raises(MalformedPayloadError, match="payload.payment.entity"):
        parse_envelope(envelope)


@pytest.mark.parametrize("missing", ["id", "amount", "currency", "status"])
def test_rejects_entity_missing_required_keys(envelope, missing) -> None:
    del envelope["payload"]["payment"]["entity"][missing]
    with pytest.raises(MalformedPayloadError, match="entity missing keys"):
        parse_envelope(envelope)


@pytest.mark.parametrize("bad_amount", ["149900", 1499.0, True, None])
def test_rejects_non_integer_amount(envelope, bad_amount) -> None:
    """A float or string amount would silently misreport revenue.

    True is included because bool is an int subclass in Python and would
    otherwise slip through as amount=1.
    """
    envelope["payload"]["payment"]["entity"]["amount"] = bad_amount
    with pytest.raises(MalformedPayloadError, match="minor units"):
        parse_envelope(envelope)


def test_rejects_negative_amount(envelope) -> None:
    envelope["payload"]["payment"]["entity"]["amount"] = -100
    with pytest.raises(MalformedPayloadError, match="negative"):
        parse_envelope(envelope)


@pytest.mark.parametrize("bad_created", ["2026-08-30T00:00:00Z", None, 1.5, True])
def test_rejects_non_unix_created_at(envelope, bad_created) -> None:
    envelope["payload"]["payment"]["entity"]["created_at"] = bad_created
    with pytest.raises(MalformedPayloadError, match="UNIX seconds"):
        parse_envelope(envelope)


@pytest.mark.parametrize(
    "event_name",
    # invoice.expired and payment_link.expired are deliberately NOT here: they
    # became supported in Phase 2b and must no longer raise.
    ["payment.captured", "payment.authorized", "refund.created", "order.paid"],
)
def test_unsupported_events_raise_a_distinct_error(envelope, event_name) -> None:
    """Razorpay sends many event types to one URL.

    These must be distinguishable from malformed payloads, because the route
    acknowledges them with a 2xx to stop Razorpay retrying forever.
    """
    envelope["event"] = event_name
    with pytest.raises(UnsupportedEventError) as exc:
        parse_envelope(envelope)
    assert exc.value.event_name == event_name


# ------------------------------------------------------------ customer identity


def test_customer_id_prefers_notes_over_a_hash(envelope) -> None:
    entity = envelope["payload"]["payment"]["entity"]
    entity["notes"] = {"customer_id": "cust_explicit123"}
    assert parse_envelope(envelope).resolved_customer_id == "cust_explicit123"


def test_customer_id_prefers_entity_field_when_present(envelope) -> None:
    entity = envelope["payload"]["payment"]["entity"]
    entity["customer_id"] = "cust_on_entity"
    entity["notes"] = {"customer_id": "cust_in_notes"}
    assert parse_envelope(envelope).resolved_customer_id == "cust_on_entity"


def test_customer_id_falls_back_to_a_stable_hash(envelope) -> None:
    """Without a stable fallback, every event would look like a new customer and
    the max-retries and contact-frequency rules could never fire."""
    entity = envelope["payload"]["payment"]["entity"]
    entity["notes"] = {}
    entity.pop("customer_id", None)
    entity["contact"] = "+919999900001"

    first = parse_envelope(copy.deepcopy(envelope)).resolved_customer_id
    second = parse_envelope(copy.deepcopy(envelope)).resolved_customer_id
    assert first == second
    assert first.startswith("anon_")

    entity["contact"] = "+919999900002"
    assert parse_envelope(envelope).resolved_customer_id != first


def test_hashed_customer_id_does_not_embed_the_contact(envelope) -> None:
    """The derived id must not leak the phone number it came from."""
    entity = envelope["payload"]["payment"]["entity"]
    entity["notes"] = {}
    entity.pop("customer_id", None)
    entity["contact"] = "+919876543210"
    resolved = parse_envelope(envelope).resolved_customer_id
    assert "9876543210" not in resolved


# ---------------------------------------------------------------- normalization


def test_event_id_is_deterministic_and_uuid_shaped() -> None:
    """Idempotency depends on this: a redelivery must map to the same event."""
    a = event_id_for("pay_ABC123")
    assert a == event_id_for("pay_ABC123")
    assert a != event_id_for("pay_ABC124")
    assert len(a) == 36 and a.count("-") == 4


def test_amount_converts_paise_to_exact_rupees(envelope) -> None:
    """Integer paise divided by 100 is exact. Float arithmetic would not be."""
    envelope["payload"]["payment"]["entity"]["amount"] = 149900
    parsed = parse_envelope(envelope)
    record = to_event_record(
        parsed,
        customer_id=parsed.resolved_customer_id,
        tenure_days=10,
        past_failures=1,
        prior_attempts=0,
    )
    assert record.amount == Decimal("1499")
    assert isinstance(record.amount, Decimal)


def test_amount_is_exact_for_awkward_values(envelope) -> None:
    envelope["payload"]["payment"]["entity"]["amount"] = 2999
    parsed = parse_envelope(envelope)
    record = to_event_record(
        parsed,
        customer_id=parsed.resolved_customer_id,
        tenure_days=0,
        past_failures=0,
        prior_attempts=0,
    )
    assert record.amount == Decimal("29.99")


def test_normalized_record_carries_no_card_data(envelopes) -> None:
    """Constraint #1. The payload legitimately contains a card object with
    last4/network/iin; none of it may survive into the EventRecord."""
    forbidden = {"card", "card_id", "last4", "iin", "network", "token_id", "vpa"}
    for env in envelopes:
        parsed = parse_envelope(env)
        record = to_event_record(
            parsed,
            customer_id=parsed.resolved_customer_id,
            tenure_days=0,
            past_failures=0,
            prior_attempts=0,
        )
        dumped = record.model_dump()
        assert forbidden.isdisjoint(dumped)
        # EventRecord's field set is fixed by architecture.md.
        assert set(dumped) == {
            "event_id", "customer_id", "event_type", "decline_code", "amount",
            "currency", "prior_attempts", "customer_history", "detected_at",
        }


def test_event_record_cannot_be_given_a_card_field(envelope) -> None:
    """extra='forbid' is what makes constraint #1 structural rather than a habit."""
    from pydantic import ValidationError

    parsed = parse_envelope(envelope)
    record = to_event_record(
        parsed,
        customer_id=parsed.resolved_customer_id,
        tenure_days=0,
        past_failures=0,
        prior_attempts=0,
    )
    with pytest.raises(ValidationError):
        EventRecord(**record.model_dump(), card_last4="1111")


def test_customer_history_is_carried_through(envelope) -> None:
    parsed = parse_envelope(envelope)
    record = to_event_record(
        parsed,
        customer_id=parsed.resolved_customer_id,
        tenure_days=412,
        past_failures=6,
        prior_attempts=2,
    )
    assert record.customer_history.tenure_days == 412
    assert record.customer_history.past_failures == 6
    assert record.prior_attempts == 2


# --------------------------------------------------- sensitive-data enforcement


def test_sensitive_card_data_is_rejected_before_storage(envelope) -> None:
    """Constraint #1 as an active check, not a comment.

    Razorpay never sends a PAN or CVV. If one ever appeared, storing it would be
    a compliance breach, so detection must abort rather than persist.
    """
    envelope["payload"]["payment"]["entity"]["card"]["number"] = "4111111111111111"
    with pytest.raises(SensitiveCardDataError, match="forbidden key"):
        audit.assert_no_sensitive_card_data(envelope)


@pytest.mark.parametrize(
    "key", ["number", "pan", "cvv", "cvc", "expiry_month", "exp_year", "track_data"]
)
def test_each_forbidden_key_is_caught_at_any_depth(key) -> None:
    payload = {"a": {"b": [{"c": {key: "sensitive"}}]}}
    with pytest.raises(SensitiveCardDataError):
        audit.assert_no_sensitive_card_data(payload)


def test_clean_payloads_pass_the_sensitive_data_check(envelopes) -> None:
    """The generated fixtures must themselves be clean, including last4/iin."""
    for env in envelopes:
        audit.assert_no_sensitive_card_data(env)


# ------------------------------------------------------------------ persistence

pytest_integration = pytest.mark.integration


@pytest.fixture(scope="module")
def db_sessionmaker():
    """Session factory against the container, on an isolated schema.

    Uses a throwaway set of tables so a test run cannot disturb data a demo
    depends on.
    """
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
    """A session whose work is always rolled back, keeping tests independent."""
    db_session = db_sessionmaker()
    transaction = db_session.begin_nested()
    try:
        yield db_session
    finally:
        if transaction.is_active:
            transaction.rollback()
        db_session.rollback()
        db_session.close()


@pytest.mark.integration
def test_detect_persists_event_customer_and_audit_entry(session, envelope) -> None:
    result = detect.detect_event(session, envelope)
    session.flush()

    assert result.is_duplicate is False
    event = session.get(Event, result.event_record.event_id)
    assert event is not None
    assert event.provider == "razorpay"
    assert event.amount_minor == envelope["payload"]["payment"]["entity"]["amount"]

    # Customer profile created, and flagged as an assumption.
    customer = session.get(Customer, event.customer_id)
    assert customer is not None
    assert customer.profile_source == "defaulted"
    assert result.profile_was_defaulted is True

    entries = session.scalars(
        select(AuditLogEntry).where(AuditLogEntry.event_id == event.event_id)
    ).all()
    assert len(entries) == 1
    assert entries[0].stage == str(Stage.DETECT)


@pytest.mark.integration
def test_audit_entry_explains_a_defaulted_profile(session, envelope) -> None:
    """The trail must show the quiet-hours check rests on an assumed timezone,
    rather than presenting a default as known fact."""
    result = detect.detect_event(session, envelope)
    session.flush()
    entry = session.scalars(
        select(AuditLogEntry).where(
            AuditLogEntry.event_id == result.event_record.event_id
        )
    ).one()
    assert entry.notes is not None
    assert "defaulted" in entry.notes or "assumed" in entry.notes
    assert entry.output_summary["customer_profile_source"] == "defaulted"


@pytest.mark.integration
def test_redelivery_is_idempotent(session, envelope) -> None:
    """Razorpay retries deliveries. A second row would double-count revenue and
    corrupt the headline metric."""
    first = detect.detect_event(session, envelope)
    session.flush()
    second = detect.detect_event(session, copy.deepcopy(envelope))
    session.flush()

    assert second.is_duplicate is True
    assert second.event_record.event_id == first.event_record.event_id

    count = len(
        session.scalars(
            select(Event).where(Event.event_id == first.event_record.event_id)
        ).all()
    )
    assert count == 1


@pytest.mark.integration
def test_duplicate_still_leaves_an_audit_record(session, envelope) -> None:
    """A silently-dropped redelivery would be a gap in the trail."""
    result = detect.detect_event(session, envelope)
    session.flush()
    detect.detect_event(session, copy.deepcopy(envelope))
    session.flush()

    entries = session.scalars(
        select(AuditLogEntry).where(
            AuditLogEntry.event_id == result.event_record.event_id
        )
    ).all()
    outcomes = [e.output_summary.get("outcome") for e in entries]
    assert "detected" in outcomes
    assert "duplicate_ignored" in outcomes


@pytest.mark.integration
def test_prior_attempts_accumulate_across_a_retry_chain(session, envelopes) -> None:
    """prior_attempts is derived from our own history, since the webhook has no
    attempt counter. Later attempts on one order must see the earlier ones."""
    base = copy.deepcopy(envelopes[0])
    entity = base["payload"]["payment"]["entity"]
    entity["order_id"] = "order_sharedchain01"
    entity["notes"] = {"customer_id": "cust_chain_test"}

    seen = []
    for i in range(3):
        attempt = copy.deepcopy(base)
        attempt_entity = attempt["payload"]["payment"]["entity"]
        attempt_entity["id"] = f"pay_chaintest{i:010d}"
        attempt_entity["created_at"] = entity["created_at"] + i * 86_400
        result = detect.detect_event(session, attempt)
        session.flush()
        seen.append(result.event_record.prior_attempts)

    assert seen == [0, 1, 2]


@pytest.mark.integration
def test_first_failure_at_tracks_the_earliest_attempt(session, envelopes) -> None:
    """The 7-day hard stop measures from the FIRST failure, so this has to be the
    earliest attempt on the order, not the current one."""
    base = copy.deepcopy(envelopes[1])
    entity = base["payload"]["payment"]["entity"]
    entity["order_id"] = "order_firstfailure1"
    entity["notes"] = {"customer_id": "cust_firstfailure"}
    origin = entity["created_at"]

    for i in range(2):
        attempt = copy.deepcopy(base)
        e = attempt["payload"]["payment"]["entity"]
        e["id"] = f"pay_firstfail{i:010d}"
        e["created_at"] = origin + i * 172_800
        detect.detect_event(session, attempt)
        session.flush()

    rows = session.scalars(
        select(Event).where(Event.provider_order_id == "order_firstfailure1")
    ).all()
    assert len({r.first_failure_at for r in rows}) == 1
    assert min(r.first_failure_at for r in rows) == datetime.fromtimestamp(
        origin, tz=UTC
    )


@pytest.mark.integration
def test_existing_customer_profile_is_used_not_overwritten(session, envelope) -> None:
    """A seeded profile is real data and must win over the defaults."""
    entity = envelope["payload"]["payment"]["entity"]
    entity["notes"] = {"customer_id": "cust_known_profile"}
    session.add(
        Customer(
            customer_id="cust_known_profile",
            email="known@example.com",
            contact="+919000000001",
            timezone="Europe/London",
            tenure_days=730,
            past_failures=4,
            profile_source="seeded",
        )
    )
    session.flush()

    result = detect.detect_event(session, envelope)
    session.flush()

    assert result.profile_was_defaulted is False
    assert result.event_record.customer_history.tenure_days == 730
    assert result.event_record.customer_history.past_failures == 4
    customer = session.get(Customer, "cust_known_profile")
    assert customer.timezone == "Europe/London"


@pytest.mark.integration
def test_identity_reconciles_when_notes_arrive_second(session, envelopes) -> None:
    """The reverse ordering, which a first attempt at this fix missed.

    A hash-derived record is created first, then a later delivery carries an
    explicit notes.customer_id. Without reconciling before every create, that
    produced a second customer row for one person.
    """
    contact = "+919833300011"

    without_notes = copy.deepcopy(envelopes[0])
    e1 = without_notes["payload"]["payment"]["entity"]
    e1["id"] = "pay_reverseorder001"
    e1["order_id"] = "order_reverseorder"
    e1["contact"] = contact
    e1["email"] = "reverse@example.com"
    e1["notes"] = []

    with_notes = copy.deepcopy(envelopes[0])
    e2 = with_notes["payload"]["payment"]["entity"]
    e2["id"] = "pay_reverseorder002"
    e2["order_id"] = "order_reverseorder"
    e2["contact"] = contact
    e2["email"] = "reverse@example.com"
    e2["notes"] = {"customer_id": "cust_arrives_later"}

    first = detect.detect_event(session, without_notes)
    session.flush()
    second = detect.detect_event(session, with_notes)
    session.flush()

    assert first.event_record.customer_id == second.event_record.customer_id
    matching = session.scalars(
        select(Customer).where(Customer.contact == contact)
    ).all()
    assert len(matching) == 1, "one person must not become two customer rows"


@pytest.mark.integration
def test_no_contact_maps_to_more_than_one_customer(session, envelopes) -> None:
    """Batch-level invariant. A split identity silently defeats max_retries and
    contact_frequency, so it must hold across a whole run, not just in pairs."""
    for env in envelopes:
        detect.detect_event(session, copy.deepcopy(env))
    session.flush()

    contacts = [
        e["payload"]["payment"]["entity"]["contact"]
        for e in envelopes
        if e["payload"]["payment"]["entity"].get("contact")
    ]
    for contact in set(contacts):
        rows = session.scalars(
            select(Customer).where(Customer.contact == contact)
        ).all()
        assert len(rows) == 1, f"{contact} maps to {len(rows)} customers"


@pytest.mark.integration
def test_same_person_is_one_customer_whether_notes_are_present(
    session, envelopes
) -> None:
    """Regression test for a bug found during live replay.

    Razorpay sends ``notes`` as ``[]`` about a third of the time, so one person
    arrives sometimes with an explicit customer id and sometimes without. Creating
    two records would split their attempt history and silently defeat
    max_retries and contact_frequency.
    """
    contact = "+919812345678"
    email = "split.identity@example.com"

    with_notes = copy.deepcopy(envelopes[0])
    e1 = with_notes["payload"]["payment"]["entity"]
    e1["id"] = "pay_identitytest001"
    e1["order_id"] = "order_identity01"
    e1["contact"], e1["email"] = contact, email
    e1["notes"] = {"customer_id": "cust_real_merchant_id"}

    without_notes = copy.deepcopy(envelopes[0])
    e2 = without_notes["payload"]["payment"]["entity"]
    e2["id"] = "pay_identitytest002"
    e2["order_id"] = "order_identity01"
    e2["contact"], e2["email"] = contact, email
    e2["notes"] = []

    first = detect.detect_event(session, with_notes)
    session.flush()
    second = detect.detect_event(session, without_notes)
    session.flush()

    assert first.event_record.customer_id == second.event_record.customer_id
    # And the shared history is visible to the second event.
    assert second.event_record.prior_attempts == 1


@pytest.mark.integration
def test_record_and_stored_row_always_name_the_same_customer(
    session, envelopes
) -> None:
    """The invariant a real bug violated.

    ``to_event_record`` used to take the customer id off the parsed payload while
    the stored row used the reconciled one. The row linked correctly but the
    record passed to DIAGNOSE and DECIDE named a different customer, so the
    guardrails would have counted attempts against the wrong person.
    """
    for env in envelopes[:10]:
        result = detect.detect_event(session, copy.deepcopy(env))
        session.flush()
        row = session.get(Event, result.event_record.event_id)
        assert row is not None
        assert row.customer_id == result.event_record.customer_id
        assert row.prior_attempts == result.event_record.prior_attempts


@pytest.mark.integration
def test_shared_email_does_not_merge_two_different_people(session, envelopes) -> None:
    """Distinct contacts must stay distinct even when the email matches.

    Merging them would pool their attempt histories, so max_retries would trip
    for someone who had never been contacted and the agent would abandon
    recoverable revenue. Contact is the authoritative key.
    """
    first = copy.deepcopy(envelopes[0])
    e1 = first["payload"]["payment"]["entity"]
    e1["id"] = "pay_contactkey001"
    e1["contact"] = "+919700000001"
    e1["email"] = "shared@example.com"
    e1["notes"] = {"customer_id": "cust_contact_owner"}
    a = detect.detect_event(session, first)
    session.flush()

    second = copy.deepcopy(envelopes[0])
    e2 = second["payload"]["payment"]["entity"]
    e2["id"] = "pay_contactkey002"
    e2["contact"] = "+919700000002"
    e2["email"] = "shared@example.com"
    e2["notes"] = []
    b = detect.detect_event(session, second)
    session.flush()

    assert a.event_record.customer_id != b.event_record.customer_id


@pytest.mark.integration
def test_email_reconciles_only_when_contact_is_absent(session, envelopes) -> None:
    """With no contact to key on, email is the best remaining signal."""
    first = copy.deepcopy(envelopes[0])
    e1 = first["payload"]["payment"]["entity"]
    e1["id"] = "pay_emailonly001"
    e1["order_id"] = "order_emailonly"
    e1["contact"] = None
    e1["email"] = "only.email@example.com"
    e1["notes"] = {"customer_id": "cust_email_owner"}
    detect.detect_event(session, first)
    session.flush()

    second = copy.deepcopy(envelopes[0])
    e2 = second["payload"]["payment"]["entity"]
    e2["id"] = "pay_emailonly002"
    e2["order_id"] = "order_emailonly"
    e2["contact"] = None
    e2["email"] = "only.email@example.com"
    e2["notes"] = []
    result = detect.detect_event(session, second)
    session.flush()

    assert result.event_record.customer_id == "cust_email_owner"


@pytest.mark.integration
def test_raw_payload_is_retained_for_audit(session, envelope) -> None:
    """The trail needs the original delivery, not just our summary of it."""
    result = detect.detect_event(session, envelope)
    session.flush()
    event = session.get(Event, result.event_record.event_id)
    assert event.raw_payload["event"] == "payment.failed"
    assert (
        event.raw_payload["payload"]["payment"]["entity"]["id"]
        == envelope["payload"]["payment"]["entity"]["id"]
    )


@pytest.mark.integration
def test_error_context_is_retained_beyond_decline_code(session, envelope) -> None:
    """error_source and error_step are the only signal distinguishing an opaque
    'payment_failed' from a bank-side one, and EventRecord has no field for them.
    They must survive in the events table so no fidelity is lost."""
    entity = envelope["payload"]["payment"]["entity"]
    result = detect.detect_event(session, envelope)
    session.flush()
    event = session.get(Event, result.event_record.event_id)
    assert event.error_source == entity["error_source"]
    assert event.error_step == entity["error_step"]
    assert event.payment_method == entity["method"]


@pytest.mark.integration
def test_whole_batch_detects_without_loss(session, envelopes) -> None:
    """End-to-end volume check: every event in a batch lands exactly once, with
    one DETECT audit entry each. This is what '100% audit trail coverage' means."""
    processed = set()
    for env in envelopes:
        result = detect.detect_event(session, copy.deepcopy(env))
        processed.add(result.event_record.event_id)
    session.flush()

    assert len(processed) == len(envelopes)
    for event_id in processed:
        entries = session.scalars(
            select(AuditLogEntry).where(AuditLogEntry.event_id == event_id)
        ).all()
        assert len(entries) == 1, f"{event_id} has {len(entries)} audit entries"
