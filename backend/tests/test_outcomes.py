"""Outcome confirmation tests.

This module writes the one number that must never be optimistic, so the tests are
mostly about refusing to credit things:

* **A payment that matches nothing is not credited.** Somebody paying normally
  must not become recovered revenue because the agent happened to be running.
* **One payment credits exactly one event.** A retry chain holds several at-risk
  events for one order; crediting all of them would multiply the headline figure
  by the length of the chain.
* **A redelivery cannot double-count.** Razorpay retries deliveries, and the
  amount is assigned rather than incremented so this holds by construction.
* **A partial payment records what arrived, not what was asked for.** Using the
  requested amount would overstate recovery.
* **Attribution is recorded.** Paying through a link the agent sent is a stronger
  claim than an order being captured later, and the difference is stored so a
  reader can discount the weaker kind.

``parse_outcome`` is covered without a database. The matching rules need real rows
and are marked integration.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app import outcomes
from app.config import get_settings
from app.models import AuditLogEntry, Base, Customer, Event, ExecutionRecord
from app.outcomes import (
    ATTRIBUTION_INVOICE,
    ATTRIBUTION_ORDER,
    ATTRIBUTION_RECOVERY_LINK,
    Confirmation,
    MalformedOutcomeError,
    Unmatched,
    confirm_outcome,
    parse_outcome,
)
from app.schemas import CustomerOutcome, Stage

NOW = datetime(2026, 9, 1, 9, 30, tzinfo=UTC)


# --------------------------------------------------------------- payloads


def link_paid(
    *,
    link_id: str = "plink_TEST01",
    payment_id: str = "pay_TEST01",
    order_id: str = "order_TEST01",
    amount: int = 49900,
    amount_paid: int | None = None,
) -> dict[str, Any]:
    """A ``payment_link.paid`` envelope, shaped as Razorpay sends it."""
    return {
        "entity": "event",
        "event": "payment_link.paid",
        "contains": ["payment_link", "payment", "order"],
        "payload": {
            "payment_link": {
                "entity": {
                    "id": link_id,
                    "entity": "payment_link",
                    "amount": amount,
                    "amount_paid": amount if amount_paid is None else amount_paid,
                    "status": "paid",
                    "currency": "INR",
                }
            },
            "payment": {
                "entity": {
                    "id": payment_id,
                    "entity": "payment",
                    "amount": amount if amount_paid is None else amount_paid,
                    "order_id": order_id,
                    "status": "captured",
                    "currency": "INR",
                }
            },
        },
        "created_at": int(NOW.timestamp()),
    }


def payment_captured(
    *,
    payment_id: str = "pay_TEST02",
    order_id: str = "order_TEST01",
    invoice_id: str | None = None,
    amount: int = 49900,
) -> dict[str, Any]:
    return {
        "entity": "event",
        "event": "payment.captured",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "entity": "payment",
                    "amount": amount,
                    "order_id": order_id,
                    "invoice_id": invoice_id,
                    "status": "captured",
                    "currency": "INR",
                }
            }
        },
        "created_at": int(NOW.timestamp()),
    }


def invoice_paid(
    *,
    invoice_id: str = "inv_TEST01",
    payment_id: str = "pay_TEST03",
    amount: int = 49900,
) -> dict[str, Any]:
    return {
        "entity": "event",
        "event": "invoice.paid",
        "contains": ["invoice", "payment"],
        "payload": {
            "invoice": {
                "entity": {
                    "id": invoice_id,
                    "entity": "invoice",
                    "amount": amount,
                    "amount_paid": amount,
                    "status": "paid",
                }
            },
            "payment": {
                "entity": {
                    "id": payment_id,
                    "entity": "payment",
                    "amount": amount,
                    "invoice_id": invoice_id,
                    "status": "captured",
                }
            },
        },
        "created_at": int(NOW.timestamp()),
    }


def order_paid(
    *, order_id: str = "order_TEST01", amount_paid: int = 49900
) -> dict[str, Any]:
    """``order.paid`` without a payment block, to exercise the amount fallback."""
    return {
        "entity": "event",
        "event": "order.paid",
        "contains": ["order"],
        "payload": {
            "order": {
                "entity": {
                    "id": order_id,
                    "entity": "order",
                    "amount": amount_paid,
                    "amount_paid": amount_paid,
                    "status": "paid",
                }
            }
        },
        "created_at": int(NOW.timestamp()),
    }


# ------------------------------------------------------------------ parsing


@pytest.mark.parametrize(
    "payload",
    [link_paid(), payment_captured(), invoice_paid(), order_paid()],
)
def test_every_supported_event_parses(payload: dict[str, Any]) -> None:
    parsed = parse_outcome(payload)

    assert parsed.event_name in outcomes.SUPPORTED_OUTCOME_EVENTS
    assert parsed.amount_paid_minor == 49900


def test_parsing_pulls_out_every_id_that_could_match() -> None:
    parsed = parse_outcome(
        link_paid(link_id="plink_A", payment_id="pay_A", order_id="order_A")
    )

    assert parsed.payment_link_id == "plink_A"
    assert parsed.provider_payment_id == "pay_A"
    assert parsed.order_id == "order_A"


def test_the_captured_amount_wins_over_the_requested_amount() -> None:
    """A partial payment must record what arrived, not what was asked for."""
    parsed = parse_outcome(link_paid(amount=100_000, amount_paid=40_000))

    assert parsed.amount_paid_minor == 40_000


def test_amount_falls_back_to_amount_paid_when_no_payment_block() -> None:
    parsed = parse_outcome(order_paid(amount_paid=12_345))

    assert parsed.amount_paid_minor == 12_345


def test_a_non_outcome_event_is_refused() -> None:
    with pytest.raises(MalformedOutcomeError, match="not an outcome event"):
        parse_outcome({"event": "payment.failed", "payload": {}})


def test_a_missing_event_name_is_refused() -> None:
    with pytest.raises(MalformedOutcomeError):
        parse_outcome({"payload": {}})


def test_a_non_object_payload_is_refused() -> None:
    with pytest.raises(MalformedOutcomeError, match="not an object"):
        parse_outcome(["not", "a", "dict"])  # type: ignore[arg-type]


def test_an_event_with_no_amount_is_refused_rather_than_guessed() -> None:
    """Inventing an amount here would invent revenue."""
    naked = {"event": "payment.captured", "payload": {"payment": {"entity": {}}}}

    with pytest.raises(MalformedOutcomeError, match="refusing to guess"):
        parse_outcome(naked)


def test_the_requested_amount_is_never_used_as_a_fallback() -> None:
    """``amount`` is what was asked for. Only ``amount_paid`` says what arrived."""
    asked_only = {
        "event": "order.paid",
        "payload": {"order": {"entity": {"id": "order_X", "amount": 99900}}},
    }

    with pytest.raises(MalformedOutcomeError, match="refusing to guess"):
        parse_outcome(asked_only)


@pytest.mark.parametrize("amount", [0, -100])
def test_a_non_positive_amount_is_refused(amount: int) -> None:
    with pytest.raises(MalformedOutcomeError, match="non-positive"):
        parse_outcome(payment_captured(amount=amount))


def test_a_boolean_is_not_accepted_as_an_amount() -> None:
    """``True`` is an int in Python, and would otherwise become 1 paise."""
    payload = payment_captured()
    payload["payload"]["payment"]["entity"]["amount"] = True

    with pytest.raises(MalformedOutcomeError):
        parse_outcome(payload)


# ------------------------------------------------------- matching (needs DB)

integration = pytest.mark.integration


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


@pytest.fixture
def ids() -> dict[str, str]:
    """Unique provider ids, so tests never collide with demo data."""
    unique = uuid.uuid4().hex[:12]
    return {
        "customer": f"cust_OUT{unique}",
        "event": str(uuid.uuid5(uuid.NAMESPACE_URL, f"outcome-test-{unique}")),
        "payment": f"pay_T{unique}",
        "order": f"order_T{unique}",
        "invoice": f"inv_T{unique}",
        "link": f"plink_T{unique}",
    }


def seed(
    session: Session,
    ids: dict[str, str],
    *,
    amount_minor: int = 49900,
    with_execution: bool = True,
    recovery_link_id: str | None = None,
    delivery_status: str = "sent",
    detected_at: datetime | None = None,
    event_id: str | None = None,
    payment_id: str | None = None,
) -> Event:
    """One at-risk event, optionally with the execution record EXECUTE wrote."""
    session.merge(Customer(customer_id=ids["customer"], timezone="Asia/Kolkata"))
    session.flush()
    event = Event(
        event_id=event_id or ids["event"],
        provider="razorpay",
        provider_payment_id=payment_id or ids["payment"],
        provider_order_id=ids["order"],
        provider_invoice_id=ids["invoice"],
        provider_event_name="payment.failed",
        customer_id=ids["customer"],
        event_type="payment_failed",
        decline_code="card_expired",
        amount_minor=amount_minor,
        currency="INR",
        prior_attempts=0,
        first_failure_at=NOW - timedelta(hours=2),
        detected_at=detected_at or (NOW - timedelta(hours=2)),
        raw_payload={"event": "payment.failed"},
    )
    session.add(event)
    # Flushed before the execution record is added. ExecutionRecord has no ORM
    # relationship() to Event, only a bare foreign-key column, so SQLAlchemy's
    # unit of work does not know it must insert the event first and may order the
    # statements the other way round.
    session.flush()
    if with_execution:
        session.add(
            ExecutionRecord(
                event_id=event.event_id,
                action="send_update_payment_method_link",
                channel="whatsapp",
                delivery_status=delivery_status,
                customer_outcome=str(CustomerOutcome.PENDING),
                recovery_link_id=recovery_link_id,
            )
        )
        session.flush()
    return event


@integration
def test_a_link_we_sent_being_paid_is_the_strongest_attribution(
    session, ids
) -> None:
    seed(session, ids, recovery_link_id=ids["link"])

    result = confirm_outcome(
        session, link_paid(link_id=ids["link"], order_id=ids["order"]), now=NOW
    )

    assert isinstance(result, Confirmation)
    assert result.attribution == ATTRIBUTION_RECOVERY_LINK
    assert result.event_id == ids["event"]
    assert result.amount_recovered_minor == 49900


@integration
def test_confirmation_writes_the_recovered_amount_and_outcome(session, ids) -> None:
    seed(session, ids, recovery_link_id=ids["link"])

    confirm_outcome(session, link_paid(link_id=ids["link"]), now=NOW)

    row = session.get(ExecutionRecord, ids["event"])
    assert row.customer_outcome == str(CustomerOutcome.RECOVERED)
    assert row.amount_recovered_minor == 49900
    assert row.outcome_updated_at == NOW


@integration
def test_an_invoice_being_paid_is_attributed_to_the_invoice(session, ids) -> None:
    seed(session, ids)

    result = confirm_outcome(
        session, invoice_paid(invoice_id=ids["invoice"]), now=NOW
    )

    assert isinstance(result, Confirmation)
    assert result.attribution == ATTRIBUTION_INVOICE


@integration
def test_the_same_order_being_captured_is_the_weaker_attribution(
    session, ids
) -> None:
    """Real recovered revenue, but the customer may have retried unprompted.

    Recorded distinctly so the number can be discounted rather than trusted flat.
    """
    seed(session, ids)

    result = confirm_outcome(
        session, payment_captured(order_id=ids["order"]), now=NOW
    )

    assert isinstance(result, Confirmation)
    assert result.attribution == ATTRIBUTION_ORDER


@integration
def test_a_sent_link_outranks_the_order_match(session, ids) -> None:
    """Both signals are present; the stronger one must be the one recorded."""
    seed(session, ids, recovery_link_id=ids["link"])

    result = confirm_outcome(
        session, link_paid(link_id=ids["link"], order_id=ids["order"]), now=NOW
    )

    assert isinstance(result, Confirmation)
    assert result.attribution == ATTRIBUTION_RECOVERY_LINK
    assert result.matched_on == ids["link"]


# ----------------------------------------------- refusing to invent revenue


@integration
def test_a_payment_matching_nothing_is_not_credited(session, ids) -> None:
    """Somebody paying normally must not become recovered revenue."""
    seed(session, ids)

    result = confirm_outcome(
        session, payment_captured(order_id="order_UNRELATED"), now=NOW
    )

    assert isinstance(result, Unmatched)
    assert "invent recovered revenue" in result.reason
    row = session.get(ExecutionRecord, ids["event"])
    assert row.amount_recovered_minor is None
    assert row.customer_outcome == str(CustomerOutcome.PENDING)


@integration
def test_an_unmatched_payment_writes_no_audit_entry(session, ids) -> None:
    seed(session, ids)

    confirm_outcome(session, payment_captured(order_id="order_UNRELATED"), now=NOW)

    entries = session.scalars(
        select(AuditLogEntry).where(AuditLogEntry.event_id == ids["event"])
    ).all()
    assert entries == []


@integration
def test_an_event_with_no_execution_record_is_not_credited(session, ids) -> None:
    """No action was taken, so there is nothing to attribute the money to."""
    seed(session, ids, with_execution=False)

    result = confirm_outcome(
        session, payment_captured(order_id=ids["order"]), now=NOW
    )

    assert isinstance(result, Unmatched)
    assert "no execution record" in result.reason


@integration
def test_one_payment_credits_exactly_one_event_of_a_retry_chain(
    session, ids
) -> None:
    """The multiplication bug this guards against would inflate the headline.

    Three failures on one order is one recovery, not three. Crediting each event
    would report three times the money that actually came back.
    """
    events = []
    for index in range(3):
        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"chain-{ids['event']}-{index}"))
        events.append(
            seed(
                session,
                ids,
                event_id=event_id,
                payment_id=f"{ids['payment']}{index}",
                detected_at=NOW - timedelta(hours=3 - index),
            )
        )

    result = confirm_outcome(
        session, payment_captured(order_id=ids["order"]), now=NOW
    )

    assert isinstance(result, Confirmation)
    credited = [
        session.get(ExecutionRecord, event.event_id).amount_recovered_minor
        for event in events
    ]
    assert credited.count(49900) == 1
    assert credited.count(None) == 2
    # The newest at-risk event is the one credited.
    assert result.event_id == events[-1].event_id


@integration
def test_a_redelivered_confirmation_does_not_double_count(session, ids) -> None:
    """Razorpay retries deliveries. The amount is assigned, never incremented."""
    seed(session, ids, recovery_link_id=ids["link"])
    payload = link_paid(link_id=ids["link"])

    first = confirm_outcome(session, payload, now=NOW)
    second = confirm_outcome(session, payload, now=NOW + timedelta(minutes=5))

    assert isinstance(first, Confirmation)
    assert isinstance(second, Confirmation)
    assert first.already_confirmed is False
    assert second.already_confirmed is True
    row = session.get(ExecutionRecord, ids["event"])
    assert row.amount_recovered_minor == 49900


@integration
def test_a_redelivery_adds_no_second_audit_entry(session, ids) -> None:
    """Two entries would read as two separate payments."""
    seed(session, ids, recovery_link_id=ids["link"])
    payload = link_paid(link_id=ids["link"])

    confirm_outcome(session, payload, now=NOW)
    confirm_outcome(session, payload, now=NOW + timedelta(minutes=5))

    entries = session.scalars(
        select(AuditLogEntry).where(AuditLogEntry.event_id == ids["event"])
    ).all()
    assert len(entries) == 1


# ------------------------------------------------------- partial payments


@integration
def test_a_partial_payment_records_only_what_arrived(session, ids) -> None:
    seed(session, ids, amount_minor=100_000, recovery_link_id=ids["link"])

    result = confirm_outcome(
        session,
        link_paid(link_id=ids["link"], amount=100_000, amount_paid=40_000),
        now=NOW,
    )

    assert isinstance(result, Confirmation)
    assert result.amount_recovered_minor == 40_000
    assert session.get(ExecutionRecord, ids["event"]).amount_recovered_minor == 40_000


@integration
def test_a_partial_payment_is_flagged_in_the_audit_trail(session, ids) -> None:
    seed(session, ids, amount_minor=100_000, recovery_link_id=ids["link"])

    confirm_outcome(
        session,
        link_paid(link_id=ids["link"], amount=100_000, amount_paid=40_000),
        now=NOW,
    )

    entry = session.scalars(
        select(AuditLogEntry).where(AuditLogEntry.event_id == ids["event"])
    ).one()
    assert entry.output_summary["partial"] is True


@integration
def test_a_full_payment_is_not_flagged_as_partial(session, ids) -> None:
    seed(session, ids, recovery_link_id=ids["link"])

    confirm_outcome(session, link_paid(link_id=ids["link"]), now=NOW)

    entry = session.scalars(
        select(AuditLogEntry).where(AuditLogEntry.event_id == ids["event"])
    ).one()
    assert entry.output_summary["partial"] is False


# ----------------------------------------------------------- audit content


@integration
def test_the_audit_entry_names_the_webhook_and_the_attribution(
    session, ids
) -> None:
    """A reader must be able to see why money was credited, and how strongly."""
    seed(session, ids, recovery_link_id=ids["link"])

    confirm_outcome(
        session, link_paid(link_id=ids["link"], payment_id="pay_AUDIT"), now=NOW
    )

    entry = session.scalars(
        select(AuditLogEntry).where(AuditLogEntry.event_id == ids["event"])
    ).one()
    assert entry.stage == str(Stage.EXECUTE)
    assert entry.input_summary["confirmation_event"] == "payment_link.paid"
    assert entry.input_summary["provider_payment_id"] == "pay_AUDIT"
    assert entry.output_summary["attribution"] == ATTRIBUTION_RECOVERY_LINK
    assert "only source permitted to set a recovered amount" in entry.notes


@integration
def test_the_audit_entry_records_the_amount_at_risk_for_comparison(
    session, ids
) -> None:
    seed(session, ids, amount_minor=75_000, recovery_link_id=ids["link"])

    confirm_outcome(
        session, link_paid(link_id=ids["link"], amount=75_000), now=NOW
    )

    entry = session.scalars(
        select(AuditLogEntry).where(AuditLogEntry.event_id == ids["event"])
    ).one()
    assert entry.input_summary["amount_at_risk_minor"] == 75_000
    assert entry.output_summary["amount_recovered_minor"] == 75_000


@integration
def test_a_confirmation_shows_up_in_the_metrics_as_recovered(session, ids) -> None:
    """The whole point: the headline figure can now be non-zero."""
    from app.metrics import load_rows

    seed(session, ids, recovery_link_id=ids["link"])
    confirm_outcome(session, link_paid(link_id=ids["link"]), now=NOW)

    row = next(r for r in load_rows(session) if r.event_id == ids["event"])

    assert row.amount_recovered_minor == 49900
    assert row.customer_outcome == str(CustomerOutcome.RECOVERED)


# ------------------------------------------------ through the webhook route
#
# The route has to recognise a paid event and hand it to confirmation BEFORE
# DETECT sees it, because DETECT would reject it as an unsupported type and
# acknowledge it with 200. That failure mode is silent: the money would simply
# never be credited and the headline metric would stay at zero while every
# request looked successful.

ROUTE_SECRET = "outcome_route_test_secret"


@pytest.fixture
def route_client(session, monkeypatch):
    """TestClient sharing the test's rolled-back session."""
    from fastapi.testclient import TestClient

    from app import channels, decide, diagnose, guardrails
    from app.config import Settings
    from app.config import get_settings as real_get_settings
    from app.db import get_db
    from app.main import app

    settings = Settings(
        _env_file=None,
        razorpay_key_id="rzp_test_placeholder",
        razorpay_key_secret="placeholder",
        razorpay_webhook_secret=ROUTE_SECRET,
    )
    for module in (guardrails, decide, diagnose, channels):
        monkeypatch.setattr(module, "get_settings", lambda s=settings: s)

    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[real_get_settings] = lambda: settings
    yield TestClient(app)
    app.dependency_overrides.clear()


def post_outcome(client, payload: dict[str, Any]):
    from app.simulation.signing import signed_delivery

    body, headers = signed_delivery(payload, ROUTE_SECRET)
    return client.post("/webhooks/razorpay", content=body, headers=headers)


@integration
def test_the_route_confirms_a_paid_link(route_client, session, ids) -> None:
    seed(session, ids, recovery_link_id=ids["link"])

    response = post_outcome(route_client, link_paid(link_id=ids["link"]))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "outcome_confirmed"
    assert body["event_id"] == ids["event"]
    assert body["amount_recovered_minor"] == 49900
    assert body["amount_recovered"] == 499.0
    assert body["attribution"] == ATTRIBUTION_RECOVERY_LINK


@integration
def test_the_route_writes_the_recovered_amount_to_the_database(
    route_client, session, ids
) -> None:
    seed(session, ids, recovery_link_id=ids["link"])

    post_outcome(route_client, link_paid(link_id=ids["link"]))

    row = session.get(ExecutionRecord, ids["event"])
    assert row.customer_outcome == str(CustomerOutcome.RECOVERED)
    assert row.amount_recovered_minor == 49900


@integration
def test_a_paid_event_is_not_swallowed_as_unsupported(
    route_client, session, ids
) -> None:
    """The regression that would silently keep the headline metric at zero.

    If the route let DETECT see a paid event it would answer 200 "ignored", which
    looks like success from the provider's side while the money is never credited.
    """
    seed(session, ids, recovery_link_id=ids["link"])

    body = post_outcome(route_client, link_paid(link_id=ids["link"])).json()

    assert body["status"] != "ignored"
    assert body["status"] == "outcome_confirmed"


@integration
def test_the_route_acknowledges_an_unmatched_payment_with_200(
    route_client, session, ids
) -> None:
    """A non-2xx would make Razorpay redeliver a payment that can never match."""
    seed(session, ids)

    response = post_outcome(
        route_client, payment_captured(order_id="order_NOTHING_OF_OURS")
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unmatched"
    assert body["matched"] is False


@integration
def test_the_route_rejects_a_paid_event_with_no_amount(route_client) -> None:
    naked = {"event": "payment.captured", "payload": {"payment": {"entity": {}}}}

    response = post_outcome(route_client, naked)

    assert response.status_code == 400
    assert response.json()["reason"] == "malformed_outcome"


@integration
def test_an_unsigned_paid_event_is_rejected_before_anything_is_credited(
    route_client, session, ids
) -> None:
    """Confirmation is the one path that writes money. It must never run on an
    unverified body, or anyone could POST themselves a recovered amount."""
    seed(session, ids, recovery_link_id=ids["link"])
    from app.simulation.signing import signed_delivery

    body, headers = signed_delivery(link_paid(link_id=ids["link"]), "the_wrong_secret")
    response = route_client.post("/webhooks/razorpay", content=body, headers=headers)

    assert response.status_code == 401
    row = session.get(ExecutionRecord, ids["event"])
    assert row.amount_recovered_minor is None
    assert row.customer_outcome == str(CustomerOutcome.PENDING)


@integration
def test_a_redelivery_through_the_route_reports_already_confirmed(
    route_client, session, ids
) -> None:
    seed(session, ids, recovery_link_id=ids["link"])
    payload = link_paid(link_id=ids["link"])

    first = post_outcome(route_client, payload).json()
    second = post_outcome(route_client, payload).json()

    assert first["already_confirmed"] is False
    assert second["already_confirmed"] is True
    assert session.get(ExecutionRecord, ids["event"]).amount_recovered_minor == 49900


@integration
def test_the_route_lifts_the_headline_metric_off_zero(
    route_client, session, ids
) -> None:
    """The whole reason this module exists.

    Before confirmation the recovery rate is 0 by construction, because nothing is
    permitted to set a recovered amount except a provider webhook.
    """
    from app.metrics import compute_batch_metrics

    seed(session, ids, recovery_link_id=ids["link"], amount_minor=49900)

    before, _ = compute_batch_metrics(session, limit=1)
    post_outcome(route_client, link_paid(link_id=ids["link"]))
    after, _ = compute_batch_metrics(session, limit=1)

    assert before.money.recovered_minor == 0
    assert after.money.recovered_minor == 49900
