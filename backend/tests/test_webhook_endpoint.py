"""Webhook endpoint tests, driven through the real HTTP stack.

Why these exist separately from ``test_signature.py``: unit-testing the HMAC
function proves the maths, but not that the ROUTE hands it the right bytes. The
common production failure is a handler that parses JSON first and verifies a
re-serialised copy, which passes every unit test and rejects every real webhook.
Going through TestClient with genuinely signed bodies is the only way to catch it.

Status codes are asserted deliberately, because they control whether Razorpay
retries a delivery.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_settings
from app.db import get_db
from app.main import app
from app.models import AuditLogEntry, Base, Event
from app.schemas import EventType
from app.simulation import signing
from app.simulation.generator import generate_batch

WEBHOOK_PATH = "/webhooks/razorpay"
TEST_SECRET = "endpoint_test_webhook_secret"

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def batch():
    """Payment-failure events only.

    A batch also contains checkout_abandoned and invoice_overdue deliveries,
    which DETECT does not yet handle (Phase 2b) and which the endpoint therefore
    acknowledges as 'ignored'. Those are covered separately below; the tests here
    are about the detected path.
    """
    full = generate_batch(seed=1234, count=40, now=datetime(2026, 8, 30, tzinfo=UTC))
    full.events = [
        e for e in full.events if e.event_type is EventType.PAYMENT_FAILED
    ]
    return full


@pytest.fixture(scope="module")
def abandonment_events():
    full = generate_batch(seed=1234, count=40, now=datetime(2026, 8, 30, tzinfo=UTC))
    return [e for e in full.events if e.event_type is not EventType.PAYMENT_FAILED]


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
def db_session(db_sessionmaker):
    """Rolled back after each test so the endpoint tests stay independent."""
    session = db_sessionmaker()
    transaction = session.begin_nested()
    try:
        yield session
    finally:
        if transaction.is_active:
            transaction.rollback()
        session.rollback()
        session.close()


def _client(db_session, secret: str | None = TEST_SECRET) -> TestClient:
    """TestClient with the DB session and webhook secret overridden.

    The secret is injected rather than read from .env so these tests neither
    depend on nor reveal the real one.
    """
    settings = Settings(
        _env_file=None,
        payment_provider="razorpay",
        razorpay_key_id="rzp_test_placeholder",
        razorpay_key_secret="placeholder",
        razorpay_webhook_secret=secret,
        # DETECT only. These tests are about signature verification over raw bytes
        # and the status codes that control Razorpay's retries, and they assert the
        # detect-shaped response body.
        #
        # It also keeps them offline. DIAGNOSE and the channel adapters resolve
        # their own settings via get_settings() rather than the injected object, so
        # with the pipeline running inline a developer with a populated .env would
        # spend real Gemini quota and create real test-mode payment links every
        # time this file ran. The inline path is covered in test_pipeline.py, with
        # the clients explicitly faked.
        pipeline_run_inline=False,
    )
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _post(client: TestClient, envelope: dict, secret: str = TEST_SECRET):
    """POST a properly signed delivery, exactly as Razorpay would."""
    body, headers = signing.signed_delivery(envelope, secret)
    return client.post(WEBHOOK_PATH, content=body, headers=headers)


# ------------------------------------------------------------------ happy path


def test_valid_signed_webhook_is_detected(db_session, batch) -> None:
    client = _client(db_session)
    response = _post(client, batch.events[0].envelope)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "detected"
    assert body["event_type"] == "payment_failed"
    assert len(body["event_id"]) == 36


def test_detected_event_reaches_the_database(db_session, batch) -> None:
    client = _client(db_session)
    event_id = _post(client, batch.events[1].envelope).json()["event_id"]
    db_session.flush()

    event = db_session.get(Event, event_id)
    assert event is not None
    entries = db_session.scalars(
        select(AuditLogEntry).where(AuditLogEntry.event_id == event_id)
    ).all()
    assert len(entries) == 1
    assert entries[0].stage == "detect"


def test_raw_body_verification_works_over_http(db_session, batch) -> None:
    """The regression guard for the classic parse-then-verify bug.

    The signature is computed over the exact bytes transmitted. If the route ever
    verified a re-serialised copy instead, this would start failing.
    """
    client = _client(db_session)
    envelope = batch.events[2].envelope
    body, headers = signing.signed_delivery(envelope, TEST_SECRET)

    # Body carries compact separators; a naive json.dumps would not match.
    assert b", " not in body
    assert json.dumps(envelope).encode() != body

    assert client.post(WEBHOOK_PATH, content=body, headers=headers).status_code == 200


# ---------------------------------------------------------------- rejections


def test_missing_signature_header_is_rejected(db_session, batch) -> None:
    client = _client(db_session)
    body = signing.canonical_body(batch.events[0].envelope)
    response = client.post(
        WEBHOOK_PATH, content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 401
    assert response.json()["reason"] == "missing_signature"


def test_wrong_secret_is_rejected(db_session, batch) -> None:
    """An attacker without the shared secret cannot inject events."""
    client = _client(db_session)
    response = _post(client, batch.events[0].envelope, secret="attacker_guess")
    assert response.status_code == 401
    assert response.json()["reason"] == "mismatch"


def test_tampered_body_is_rejected(db_session, batch) -> None:
    """The concrete attack: inflate an amount after signing."""
    client = _client(db_session)
    body, headers = signing.signed_delivery(batch.events[0].envelope, TEST_SECRET)
    tampered = body.replace(b'"amount":', b'"amount":9', 1)
    assert tampered != body

    response = client.post(WEBHOOK_PATH, content=tampered, headers=headers)
    assert response.status_code == 401
    assert response.json()["reason"] == "mismatch"


def test_fails_closed_when_no_secret_is_configured(db_session, batch) -> None:
    """An unconfigured deployment must reject everything, not accept everything."""
    client = _client(db_session, secret=None)
    response = _post(client, batch.events[0].envelope)
    assert response.status_code == 401
    assert response.json()["reason"] == "no_secret_configured"


def test_unsigned_events_never_reach_the_database(db_session, batch) -> None:
    """The point of the whole boundary: a forgery must leave no trace in the
    pipeline or the audit trail."""
    client = _client(db_session)
    envelope = copy.deepcopy(batch.events[3].envelope)
    payment_id = envelope["payload"]["payment"]["entity"]["id"]
    body = signing.canonical_body(envelope)

    client.post(
        WEBHOOK_PATH,
        content=body,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": "0" * 64},
    )
    db_session.flush()

    assert (
        db_session.scalars(
            select(Event).where(Event.provider_payment_id == payment_id)
        ).first()
        is None
    )


def test_invalid_json_is_rejected(db_session) -> None:
    client = _client(db_session)
    body = b"{not valid json"
    headers = {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": signing.sign_body(body, TEST_SECRET),
    }
    response = client.post(WEBHOOK_PATH, content=body, headers=headers)
    assert response.status_code == 400
    assert response.json()["reason"] == "invalid_json"


def test_malformed_but_signed_payload_is_rejected(db_session) -> None:
    """A valid signature proves origin, not shape. Both are checked."""
    client = _client(db_session)
    response = _post(client, {"entity": "event", "event": "payment.failed"})
    assert response.status_code == 400
    assert response.json()["reason"] == "malformed_payload"


# ------------------------------------------------- retry-behaviour semantics


@pytest.mark.parametrize(
    # `payment.captured` used to be in this list and no longer belongs: outcome
    # confirmation handles it now, because it is how the provider tells us money
    # came back. This test failing when that shipped was the tripwire working.
    "event_name", ["refund.processed", "subscription.charged", "payout.processed"]
)
def test_unsupported_events_are_acknowledged_not_retried(
    db_session, batch, event_name
) -> None:
    """Must return 2xx. A non-2xx would make Razorpay retry an event forever that
    we are never going to process."""
    client = _client(db_session)
    envelope = copy.deepcopy(batch.events[0].envelope)
    envelope["event"] = event_name

    response = _post(client, envelope)
    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "event": event_name}


def test_redelivery_returns_duplicate_without_a_second_event(db_session, batch) -> None:
    """Razorpay retries on timeout. Two rows would double-count amount at risk."""
    client = _client(db_session)
    envelope = batch.events[4].envelope

    first = _post(client, envelope)
    db_session.flush()
    second = _post(client, envelope)
    db_session.flush()

    assert first.json()["status"] == "detected"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert second.json()["event_id"] == first.json()["event_id"]

    rows = db_session.scalars(
        select(Event).where(
            Event.provider_payment_id
            == envelope["payload"]["payment"]["entity"]["id"]
        )
    ).all()
    assert len(rows) == 1


# --------------------------------------------------------- no secret leakage


def test_rejection_responses_never_leak_the_secret(db_session, batch) -> None:
    """A 401 body must not hand an attacker the expected signature to replay."""
    client = _client(db_session)
    response = _post(client, batch.events[0].envelope, secret="attacker_guess")
    blob = response.text
    assert TEST_SECRET not in blob
    expected = signing.sign_body(
        signing.canonical_body(batch.events[0].envelope), TEST_SECRET
    )
    assert expected not in blob


# ------------------------------------------------------------ batch end-to-end


def test_abandonment_events_are_now_detected_through_the_endpoint(
    db_session, abandonment_events
) -> None:
    """Phase 2b closed the gap this test was written to guard.

    It previously asserted ``ignored`` and was deliberately built to fail once
    DETECT learned these events. It has been flipped rather than deleted, so the
    endpoint keeps proving the whole path — signature, routing, parsing,
    persistence — for all three event types and not just payment failures.
    """
    assert abandonment_events, "batch produced no abandonment events"
    client = _client(db_session)
    event_ids = set()
    for event in abandonment_events:
        response = _post(client, event.envelope)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "detected", body
        assert body["event_type"] in {"checkout_abandoned", "invoice_overdue"}
        # Nothing failed, so there must be no decline code.
        assert body["decline_code"] is None
        event_ids.add(body["event_id"])
    db_session.flush()

    assert len(event_ids) == len(abandonment_events)
    for event_id in event_ids:
        assert db_session.get(Event, event_id) is not None


def test_genuinely_unsupported_events_are_still_acknowledged(db_session, batch) -> None:
    """Razorpay sends many event types to one URL.

    Widening SUPPORTED_EVENTS must not have turned the ignore path into an error
    path: a non-2xx would make Razorpay retry an event forever.
    """
    client = _client(db_session)
    envelope = copy.deepcopy(batch.events[0].envelope)
    envelope["event"] = "subscription.halted"
    response = _post(client, envelope)
    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "event": "subscription.halted"}


def test_whole_batch_flows_through_the_signed_endpoint(db_session, batch) -> None:
    """The Phase 1 replay path, proven.

    Every fixture event goes through the SAME signature-verified handler a live
    ngrok delivery would hit, so the batch run exercises the real code path rather
    than a test-only shortcut.
    """
    client = _client(db_session)
    event_ids = set()
    for event in batch.events:
        response = _post(client, event.envelope)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "detected"
        event_ids.add(response.json()["event_id"])
    db_session.flush()

    assert len(event_ids) == len(batch.events)
    for event_id in event_ids:
        entries = db_session.scalars(
            select(AuditLogEntry).where(AuditLogEntry.event_id == event_id)
        ).all()
        assert len(entries) == 1
