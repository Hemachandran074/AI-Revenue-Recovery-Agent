"""Pipeline tests: the four stages chained, and how the chain degrades.

The unit tests for each stage prove each stage is right in isolation. These prove
the properties that only exist once they are wired together, and that no earlier
claim quietly stops being true at the seams:

* **Four audit entries per event, always.** ``project-overview.md`` asks that a
  stranger can read one event's trail and understand what happened in under 30
  seconds. That holds only if every stage appears even when nothing was sent.
* **A classifier outage produces a safe, audited escalation.** Not a crash, not a
  dropped event, and not an outage mislabelled as a cautious judgement.
* **The contact window is consumed only by a real send.** A dry run that moved
  ``last_contacted_at`` would silently suppress genuine later sends.
* **A deferral survives the whole chain.** DECIDE deferring a contact for quiet
  hours is only worth anything if EXECUTE actually holds it, which is a property
  of the pair, not of either module.
* **Nothing is charged and nothing is recovered on paper.** End to end, not just
  in the EXECUTE unit tests.

Every test injects its classifier and channel adapters, so this file never opens a
socket, never spends Gemini quota, and never creates a real payment link. Each
test mints a unique payment, order and contact so it is independent of whatever
demo data the shared database already holds.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app import channels, decide, diagnose, guardrails, pipeline
from app.channels import DryRunSender, MessageResult, PaymentLink
from app.config import Settings, get_settings
from app.db import get_db
from app.main import app
from app.models import (
    AuditLogEntry,
    Base,
    Customer,
    DecisionRecord,
    DiagnosisRecord,
    ExecutionRecord,
)
from app.schemas import (
    Action,
    Channel,
    DeliveryStatus,
    EventType,
    GuardrailName,
    RootCause,
    Stage,
)
from app.simulation import signing
from app.simulation.generator import generate_batch

pytestmark = pytest.mark.integration

WEBHOOK_PATH = "/webhooks/razorpay"
TEST_SECRET = "pipeline_test_webhook_secret"

# 12:30 UTC is 18:00 in Asia/Kolkata, inside the 09:00-20:00 contact window, so
# the default path is a real send rather than a quiet-hours deferral.
FIXED_NOW = datetime(2026, 6, 15, 12, 30, tzinfo=UTC)

STAGE_ORDER = [Stage.DETECT, Stage.DIAGNOSE, Stage.DECIDE, Stage.EXECUTE]


# --------------------------------------------------------------------- doubles


class FakeClassifier:
    """Returns a fixed classification instead of calling Gemini.

    The free-tier key allows 20 requests a day (Known issue K), so a test suite
    that called the real model would exhaust it and then start failing for a
    reason that has nothing to do with the code.
    """

    def __init__(
        self,
        root_cause: RootCause = RootCause.CARD_EXPIRED,
        confidence: float = 0.92,
        reasoning: str = "Fixture classification.",
        error: Exception | None = None,
        raw: str | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._root_cause = root_cause
        self._confidence = confidence
        self._reasoning = reasoning
        self._error = error
        self._raw = raw

    def generate(self, *, system_prompt: str, user_content: str) -> str:
        self.calls.append(user_content)
        if self._error is not None:
            raise self._error
        if self._raw is not None:
            return self._raw
        return json.dumps(
            {
                "root_cause": str(self._root_cause),
                "confidence": self._confidence,
                "reasoning": self._reasoning,
            }
        )


class RecordingFactory:
    """Creates no real link and records what it was asked for."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(
        self, *, amount_minor: int, customer_id: str, email: str | None,
        contact: str | None, description: str,
    ) -> PaymentLink:
        self.calls.append({"amount_minor": amount_minor, "customer_id": customer_id})
        return PaymentLink(
            link_id=f"plink_PIPE{len(self.calls):02d}",
            url=f"https://rzp.io/i/pipe{len(self.calls):02d}",
            amount_minor=amount_minor,
        )

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - fails the test
        raise AssertionError(
            f"pipeline called payment-provider method {name!r}; the only permitted "
            "call is creating a hosted payment link"
        )


class RecordingSender:
    """Reports delivery without a network call, and records the send."""

    def __init__(self, result: MessageResult | None = None) -> None:
        self.calls: list[dict[str, str]] = []
        self._result = result or MessageResult(
            delivered=True, provider_message_id="SM_pipe_0001"
        )

    def send(self, *, to: str, body: str) -> MessageResult:
        self.calls.append({"to": to, "body": body})
        return self._result

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - fails the test
        raise AssertionError(f"pipeline called messaging method {name!r}")


# -------------------------------------------------------------------- fixtures


def pinned_settings(**overrides: Any) -> Settings:
    """Guardrail settings fixed in the test, not read from a local ``.env``."""
    base: dict[str, Any] = {
        "_env_file": None,
        "max_recovery_attempts": 3,
        "min_hours_between_contacts": 24,
        "quiet_hours_start_local": 9,
        "quiet_hours_end_local": 20,
        "hard_stop_days": 7,
        "insufficient_funds_retry_days": 3,
        "diagnose_confidence_threshold": 0.75,
        "gemini_model": "gemini-2.5-flash-lite",
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _pin_settings(monkeypatch):
    """Pin every module that resolves settings for itself.

    ``guardrails``, ``decide``, ``diagnose`` and ``channels`` each call
    ``get_settings()`` directly rather than receiving the injected object, so
    without this a developer's ``.env`` would change what these tests assert, and
    the channel builders could pick up real credentials.
    """
    settings = pinned_settings()
    for module in (guardrails, decide, diagnose, channels):
        monkeypatch.setattr(module, "get_settings", lambda s=settings: s)
    return settings


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
    """Rolled back after each test, so tests do not see each other's rows."""
    db_session = db_sessionmaker()
    transaction = db_session.begin_nested()
    try:
        yield db_session
    finally:
        if transaction.is_active:
            transaction.rollback()
        db_session.rollback()
        db_session.close()


@pytest.fixture(scope="module")
def raw_envelopes():
    batch = generate_batch(seed=777001, count=30, now=FIXED_NOW)
    envelopes = [
        e.envelope for e in batch.events if e.event_type is EventType.PAYMENT_FAILED
    ]
    assert envelopes, "generator produced no payment.failed events"
    return envelopes


@pytest.fixture
def envelope(raw_envelopes):
    return fresh(raw_envelopes[0])


def fresh(
    envelope: dict[str, Any],
    *,
    customer: str | None = None,
    at: datetime = FIXED_NOW,
) -> dict[str, Any]:
    """A copy that is a brand new event, by default for a brand new customer.

    Three things are pinned deliberately rather than taken from the generator:

    * **Provider ids.** The shared database already holds a few hundred demo
      events, and reusing a payment id would make DETECT report a duplicate.
    * **Customer identity.** DETECT resolves this from an explicit
      ``notes.customer_id`` when present and otherwise from a hash of the contact,
      so setting it outright is the only way a test can state plainly whether two
      events belong to the same person. ``customer`` shares one on purpose.
    * **Age.** A batch spreads failures over a fortnight, so most fixture events
      are already past the 7-day hard stop and would be blocked before any of the
      behaviour under test could run. Pinning the event a few minutes before
      ``at`` keeps the recovery window open, which is the case worth exercising;
      the hard stop itself is covered in the guardrail tests.
    """
    fresh_envelope = copy.deepcopy(envelope)
    unique = uuid.uuid4().hex[:12]
    customer_id = customer or f"cust_T{unique}"
    digits = f"{int(hashlib.sha256(customer_id.encode()).hexdigest()[:8], 16):08d}"[:8]

    entity = fresh_envelope["payload"]["payment"]["entity"]
    entity["id"] = f"pay_T{unique}"
    entity["order_id"] = f"order_T{unique}"
    entity["invoice_id"] = f"inv_T{unique}"
    entity["notes"] = {"customer_id": customer_id}
    entity["contact"] = f"+9198{digits}"
    entity["email"] = f"{customer_id.lower()}@example.invalid"
    entity["created_at"] = int(at.timestamp()) - 300
    fresh_envelope["created_at"] = int(at.timestamp()) - 240
    return fresh_envelope


def run(
    session: Session,
    envelope: dict[str, Any],
    *,
    root_cause: RootCause = RootCause.CARD_EXPIRED,
    confidence: float = 0.92,
    reasoning: str = "Fixture classification.",
    classifier_error: Exception | None = None,
    raw: str | None = None,
    factory: Any | None = None,
    sender: Any | None = None,
    now: datetime = FIXED_NOW,
):
    """Run one delivery through all four stages with everything faked."""
    classifier = FakeClassifier(
        root_cause, confidence, reasoning, error=classifier_error, raw=raw
    )
    used_factory = RecordingFactory() if factory is None else factory
    used_sender = RecordingSender() if sender is None else sender

    outcome = pipeline.process_event(
        session,
        envelope,
        diagnose_client=classifier,
        link_factory=used_factory,
        sender=used_sender,
        now=now,
    )
    session.flush()
    return outcome, classifier, used_factory, used_sender


def audit_entries(session: Session, event_id: str) -> list[AuditLogEntry]:
    return list(
        session.scalars(
            select(AuditLogEntry)
            .where(AuditLogEntry.event_id == event_id)
            .order_by(AuditLogEntry.id)
        )
    )


def stages(session: Session, event_id: str) -> list[str]:
    return [entry.stage for entry in audit_entries(session, event_id)]


# ------------------------------------------------------------ the audit trail


def test_all_four_stages_write_an_audit_entry(session, envelope) -> None:
    outcome, *_ = run(session, envelope)

    assert stages(session, outcome.event_record.event_id) == [
        str(stage) for stage in STAGE_ORDER
    ]


@pytest.mark.parametrize(
    "root_cause",
    [
        RootCause.CARD_EXPIRED,
        RootCause.INSUFFICIENT_FUNDS,
        RootCause.BANK_RISK_BLOCK,
        RootCause.SCA_ABANDONED,
        RootCause.NETWORK_ERROR,
        RootCause.UNKNOWN,
    ],
)
def test_four_stages_appear_even_when_nothing_is_sent(
    session, raw_envelopes, root_cause: RootCause
) -> None:
    """A skipped send with a stated reason beats a gap in the trail.

    Half of these root causes contact nobody, and those are exactly the events a
    gap would hide.
    """
    outcome, *_ = run(session, fresh(raw_envelopes[0]), root_cause=root_cause)

    assert len(audit_entries(session, outcome.event_record.event_id)) == 4


def test_every_audit_entry_carries_both_summaries(session, envelope) -> None:
    outcome, *_ = run(session, envelope)

    for entry in audit_entries(session, outcome.event_record.event_id):
        assert entry.input_summary, f"{entry.stage} logged no input"
        assert entry.output_summary, f"{entry.stage} logged no output"


def test_decide_entry_records_all_four_guardrail_results(session, envelope) -> None:
    """Constraint #5: every check, including the ones that passed.

    A passed-only list cannot express a failure, so it cannot be audited.
    """
    outcome, *_ = run(session, envelope)
    decide_entry = next(
        e
        for e in audit_entries(session, outcome.event_record.event_id)
        if e.stage == str(Stage.DECIDE)
    )

    assert decide_entry.guardrail_checks is not None
    recorded = {check["name"] for check in decide_entry.guardrail_checks}
    assert recorded == {str(name) for name in GuardrailName}
    for check in decide_entry.guardrail_checks:
        assert isinstance(check["passed"], bool)
        assert check["detail"]


def test_audit_trail_holds_no_raw_card_data(session, envelope) -> None:
    """Constraint #1, checked on what actually landed in the table."""
    outcome, *_ = run(session, envelope)

    for entry in audit_entries(session, outcome.event_record.event_id):
        serialised = json.dumps(
            [entry.input_summary, entry.output_summary, entry.guardrail_checks]
        ).lower()
        for forbidden in ("cvv", '"pan"', "card_number", "full_number"):
            assert forbidden not in serialised


def test_execute_entry_explains_a_skip(session, raw_envelopes) -> None:
    """The note is what a reviewer reads first, so a skip must justify itself."""
    outcome, *_ = run(
        session, fresh(raw_envelopes[0]), root_cause=RootCause.BANK_RISK_BLOCK
    )
    execute_entry = next(
        e
        for e in audit_entries(session, outcome.event_record.event_id)
        if e.stage == str(Stage.EXECUTE)
    )

    assert execute_entry.notes
    assert "human review" in execute_entry.notes.lower()


# ---------------------------------------------------------- persisted records


def test_each_stage_persists_its_own_record(session, envelope) -> None:
    outcome, *_ = run(session, envelope)
    event_id = outcome.event_record.event_id

    assert session.get(DiagnosisRecord, event_id) is not None
    assert session.get(DecisionRecord, event_id) is not None
    assert session.get(ExecutionRecord, event_id) is not None


def test_diagnosis_record_matches_the_diagnosis_returned(session, envelope) -> None:
    outcome, *_ = run(
        session, envelope, root_cause=RootCause.CARD_EXPIRED, confidence=0.91
    )
    row = session.get(DiagnosisRecord, outcome.event_record.event_id)

    assert outcome.diagnosis is not None
    assert row.root_cause == str(outcome.diagnosis.root_cause)
    assert row.confidence == pytest.approx(outcome.diagnosis.confidence)
    assert row.classifier_unavailable is False
    assert row.model == "gemini-2.5-flash-lite"
    assert row.prompt_version


def test_decision_record_matches_the_decision_returned(session, envelope) -> None:
    outcome, *_ = run(session, envelope)
    row = session.get(DecisionRecord, outcome.event_record.event_id)

    assert outcome.decision is not None
    assert row.action == str(outcome.decision.action)
    assert row.channel == str(outcome.decision.channel)
    assert row.blocked_reason == outcome.decision.blocked_reason
    assert {c["name"] for c in row.guardrail_checks} == {
        str(name) for name in GuardrailName
    }


def test_execution_record_matches_the_outcome_returned(session, envelope) -> None:
    outcome, _, factory, sender = run(session, envelope)
    row = session.get(ExecutionRecord, outcome.event_record.event_id)

    assert outcome.execution is not None
    assert row.delivery_status == str(outcome.execution.result.delivery_status)
    assert row.action == str(outcome.execution.action)
    assert row.provider_message_id == outcome.execution.provider_message_id
    assert row.requeued is outcome.execution.requeued


def test_execution_record_stores_the_link_id_and_url(session, envelope) -> None:
    """The url is kept on the record but not in the audit summary.

    Support needs to resend the exact link; the audit trail does not need a
    working payment url sitting in it.
    """
    outcome, *_ = run(session, envelope)
    row = session.get(ExecutionRecord, outcome.event_record.event_id)

    assert row.recovery_link_id == "plink_PIPE01"
    assert row.recovery_link_url == "https://rzp.io/i/pipe01"


# ---------------------------------------------------- root cause to action


def test_card_expired_sends_an_update_link(session, raw_envelopes) -> None:
    outcome, _, factory, sender = run(
        session, fresh(raw_envelopes[0]), root_cause=RootCause.CARD_EXPIRED
    )

    assert outcome.decision is not None
    assert outcome.decision.action is Action.SEND_UPDATE_PAYMENT_METHOD_LINK
    assert outcome.decision.blocked_reason is None
    assert outcome.execution is not None
    assert outcome.execution.result.delivery_status is DeliveryStatus.SENT
    assert len(factory.calls) == 1
    assert len(sender.calls) == 1


def test_sca_abandoned_sends_a_fresh_auth_link(session, raw_envelopes) -> None:
    outcome, _, _, sender = run(
        session, fresh(raw_envelopes[0]), root_cause=RootCause.SCA_ABANDONED
    )

    assert outcome.decision is not None
    assert outcome.decision.action is Action.SEND_FRESH_AUTH_LINK
    assert len(sender.calls) == 1


def test_insufficient_funds_records_a_retry_and_charges_nothing(
    session, raw_envelopes
) -> None:
    """Constraint #6: the agent records a due time, it does not re-submit."""
    outcome, _, factory, sender = run(
        session, fresh(raw_envelopes[0]), root_cause=RootCause.INSUFFICIENT_FUNDS
    )
    row = session.get(ExecutionRecord, outcome.event_record.event_id)

    assert outcome.decision is not None
    assert outcome.decision.action is Action.SCHEDULE_RETRY
    assert outcome.decision.scheduled_for == FIXED_NOW + timedelta(days=3)
    assert row.delivery_status == str(DeliveryStatus.SKIPPED)
    assert "No charge submitted" in row.skip_reason
    assert factory.calls == []
    assert sender.calls == []


def test_bank_risk_block_escalates_without_contacting_anyone(
    session, raw_envelopes
) -> None:
    """A bank-side block is not something a customer message can fix."""
    outcome, _, factory, sender = run(
        session, fresh(raw_envelopes[0]), root_cause=RootCause.BANK_RISK_BLOCK
    )

    assert outcome.decision is not None
    assert outcome.decision.action is Action.ESCALATE_TO_HUMAN_REVIEW
    assert outcome.decision.channel is Channel.NONE
    assert factory.calls == []
    assert sender.calls == []


def test_network_error_schedules_a_retry_within_the_hour(
    session, raw_envelopes
) -> None:
    outcome, *_ = run(
        session, fresh(raw_envelopes[0]), root_cause=RootCause.NETWORK_ERROR
    )

    assert outcome.decision is not None
    assert outcome.decision.action is Action.SCHEDULE_RETRY
    assert outcome.decision.scheduled_for == FIXED_NOW + timedelta(hours=1)


# --------------------------------------------------- designed degradation


def test_classifier_outage_becomes_an_audited_escalation(
    session, raw_envelopes
) -> None:
    """The whole point of the degradation path.

    A quota outage must produce a safe, fully audited escalation. Not a crash,
    and not a dropped event.
    """
    outcome, classifier, factory, sender = run(
        session,
        fresh(raw_envelopes[0]),
        classifier_error=RuntimeError("upstream connection reset"),
    )
    event_id = outcome.event_record.event_id

    assert outcome.classifier_unavailable is True
    assert outcome.diagnosis is not None
    assert outcome.diagnosis.root_cause is RootCause.UNKNOWN
    assert outcome.decision is not None
    assert outcome.decision.action is Action.ESCALATE_TO_HUMAN_REVIEW
    assert outcome.execution is not None
    assert outcome.execution.result.delivery_status is DeliveryStatus.SKIPPED
    # Still four stages, and still nobody contacted.
    assert len(audit_entries(session, event_id)) == 4
    assert factory.calls == []
    assert sender.calls == []
    # It really did try before giving up.
    assert len(classifier.calls) >= 1


def test_outage_is_recorded_as_an_operational_failure_not_a_judgement(
    session, raw_envelopes
) -> None:
    """Reporting an outage as a cautious ``unknown`` would overstate the model.

    The eval numbers are only honest if the events the classifier never saw are
    distinguishable from the ones it saw and could not call.
    """
    outcome, *_ = run(
        session,
        fresh(raw_envelopes[0]),
        classifier_error=RuntimeError("upstream connection reset"),
    )
    event_id = outcome.event_record.event_id
    row = session.get(DiagnosisRecord, event_id)
    diagnose_entry = next(
        e
        for e in audit_entries(session, event_id)
        if e.stage == str(Stage.DIAGNOSE)
    )

    assert row.classifier_unavailable is True
    assert diagnose_entry.notes is not None
    assert "operational failure" in diagnose_entry.notes


def test_a_cautious_unknown_is_not_flagged_as_an_outage(
    session, raw_envelopes
) -> None:
    """Low confidence is the model declining to guess. It reached the model."""
    outcome, *_ = run(
        session,
        fresh(raw_envelopes[0]),
        root_cause=RootCause.CARD_EXPIRED,
        confidence=0.20,
    )
    row = session.get(DiagnosisRecord, outcome.event_record.event_id)

    assert outcome.diagnosis is not None
    assert outcome.diagnosis.root_cause is RootCause.UNKNOWN
    assert outcome.classifier_unavailable is False
    assert row.classifier_unavailable is False


def test_an_unparseable_response_still_produces_a_full_trail(
    session, raw_envelopes
) -> None:
    outcome, *_ = run(session, fresh(raw_envelopes[0]), raw="not json at all")

    assert outcome.diagnosis is not None
    assert outcome.diagnosis.root_cause is RootCause.UNKNOWN
    assert len(audit_entries(session, outcome.event_record.event_id)) == 4


def test_a_dispatch_failure_is_requeued_not_lost(session, raw_envelopes) -> None:
    outcome, *_ = run(
        session,
        fresh(raw_envelopes[0]),
        sender=RecordingSender(
            MessageResult(delivered=False, error="HTTPError: 503 from Twilio")
        ),
    )
    row = session.get(ExecutionRecord, outcome.event_record.event_id)

    assert row.delivery_status == str(DeliveryStatus.FAILED)
    assert row.requeued is True
    assert "503" in row.failure_reason


# ------------------------------------------------- the contact window feedback


def test_a_real_send_consumes_the_contact_window(session, envelope) -> None:
    outcome, *_ = run(session, envelope)
    customer = session.get(Customer, outcome.event_record.customer_id)

    assert outcome.execution is not None
    assert outcome.execution.result.delivery_status is DeliveryStatus.SENT
    assert customer.last_contacted_at == FIXED_NOW


def test_a_dry_run_does_not_consume_the_contact_window(session, envelope) -> None:
    """Otherwise a missing Twilio key would silently suppress later real sends."""
    outcome, *_ = run(session, envelope, sender=DryRunSender())
    customer = session.get(Customer, outcome.event_record.customer_id)

    assert outcome.execution is not None
    assert outcome.execution.result.delivery_status is DeliveryStatus.SKIPPED
    assert customer.last_contacted_at is None


def test_a_failed_send_does_not_consume_the_contact_window(
    session, envelope
) -> None:
    outcome, *_ = run(
        session,
        envelope,
        sender=RecordingSender(MessageResult(delivered=False, error="HTTPError: 503")),
    )
    customer = session.get(Customer, outcome.event_record.customer_id)

    assert customer.last_contacted_at is None


def test_a_recent_contact_defers_the_next_one_end_to_end(
    session, raw_envelopes
) -> None:
    """The full loop: EXECUTE writes the contact time, DECIDE reads it back.

    Two failures for the same customer an hour apart must not produce two
    messages. This is the one-contact-per-24h rule working across stages rather
    than only inside the guardrail unit tests.
    """
    shared = f"cust_SHARED{uuid.uuid4().hex[:8]}"
    first, *_ = run(session, fresh(raw_envelopes[0], customer=shared))
    assert first.execution is not None
    assert first.execution.result.delivery_status is DeliveryStatus.SENT

    later = FIXED_NOW + timedelta(hours=1)
    second, _, factory, sender = run(
        session, fresh(raw_envelopes[1], customer=shared), now=later
    )

    assert second.event_record.customer_id == first.event_record.customer_id
    assert second.decision is not None
    frequency = next(
        c
        for c in second.decision.guardrail_checks
        if c.name is GuardrailName.CONTACT_FREQUENCY
    )
    assert frequency.passed is False
    # Deferrable, so the action is postponed rather than cancelled...
    assert second.decision.blocked_reason is None
    assert second.decision.scheduled_for is not None
    assert second.decision.scheduled_for > later
    # ...and EXECUTE must actually hold it. Sending now would breach the rule
    # DECIDE just deferred the action to respect.
    assert second.execution is not None
    assert second.execution.result.delivery_status is DeliveryStatus.SKIPPED
    assert "Deferred until" in (second.execution.skip_reason or "")
    assert factory.calls == []
    assert sender.calls == []


def test_a_deferred_send_leaves_the_window_untouched(
    session, raw_envelopes
) -> None:
    """A deferral must not push the window out again, or it never comes due."""
    shared = f"cust_SHARED{uuid.uuid4().hex[:8]}"
    run(session, fresh(raw_envelopes[0], customer=shared))
    later = FIXED_NOW + timedelta(hours=1)
    second, *_ = run(session, fresh(raw_envelopes[1], customer=shared), now=later)

    customer = session.get(Customer, second.event_record.customer_id)
    assert customer.last_contacted_at == FIXED_NOW


def test_the_deferred_time_is_persisted_for_a_later_run(
    session, raw_envelopes
) -> None:
    """The due time is the queue. If it is not stored, the send is lost."""
    shared = f"cust_SHARED{uuid.uuid4().hex[:8]}"
    run(session, fresh(raw_envelopes[0], customer=shared))
    later = FIXED_NOW + timedelta(hours=1)
    second, *_ = run(session, fresh(raw_envelopes[1], customer=shared), now=later)

    row = session.get(DecisionRecord, second.event_record.event_id)
    assert row.scheduled_for is not None
    assert row.delay_seconds and row.delay_seconds > 0


def test_quiet_hours_defer_a_contact_rather_than_dropping_it(
    session, raw_envelopes
) -> None:
    """03:00 IST. The message waits for morning instead of waking the customer."""
    small_hours = datetime(2026, 6, 15, 21, 30, tzinfo=UTC)  # 03:00 next day, IST

    outcome, _, factory, sender = run(
        session, fresh(raw_envelopes[0]), now=small_hours
    )

    assert outcome.decision is not None
    quiet = next(
        c
        for c in outcome.decision.guardrail_checks
        if c.name is GuardrailName.QUIET_HOURS
    )
    assert quiet.passed is False
    assert outcome.decision.blocked_reason is None
    assert outcome.execution is not None
    assert outcome.execution.result.delivery_status is DeliveryStatus.SKIPPED
    assert sender.calls == []
    assert factory.calls == []


# ------------------------------------------------------------------ latencies


def test_both_latencies_are_recorded(session, envelope) -> None:
    """One blended number would score a correct deferral as a failure."""
    outcome, *_ = run(session, envelope)

    assert outcome.decision_latency_ms is not None
    assert outcome.send_latency_ms is not None
    assert outcome.decision_latency_ms > 0
    assert outcome.send_latency_ms >= outcome.decision_latency_ms


def test_decision_latency_is_well_inside_the_real_time_budget(
    session, envelope
) -> None:
    """``architecture.md`` asks for under 60 seconds received to decided.

    With the classifier faked this measures our own orchestration and database
    work, which is the part the budget is actually about.
    """
    outcome, *_ = run(session, envelope)

    assert outcome.decision_latency_ms < 60_000


# ---------------------------------------------------------------- duplicates


def test_a_duplicate_delivery_runs_no_further_stages(session, envelope) -> None:
    """Razorpay retries deliveries. Re-running would double-count and re-message."""
    first, *_ = run(session, envelope)
    event_id = first.event_record.event_id
    stage_count_before = len(audit_entries(session, event_id))

    second, _, factory, sender = run(session, copy.deepcopy(envelope))

    assert second.is_duplicate is True
    assert second.diagnosis is None
    assert second.decision is None
    assert second.execution is None
    assert factory.calls == []
    assert sender.calls == []
    entries = audit_entries(session, event_id)
    assert [e.stage for e in entries].count(str(Stage.DIAGNOSE)) == 1
    assert [e.stage for e in entries].count(str(Stage.DECIDE)) == 1
    assert [e.stage for e in entries].count(str(Stage.EXECUTE)) == 1
    assert len(entries) >= stage_count_before


def test_a_duplicate_does_not_move_the_contact_window(session, envelope) -> None:
    first, *_ = run(session, envelope)
    customer = session.get(Customer, first.event_record.customer_id)
    assert customer.last_contacted_at == FIXED_NOW

    run(session, copy.deepcopy(envelope), now=FIXED_NOW + timedelta(hours=5))

    session.refresh(customer)
    assert customer.last_contacted_at == FIXED_NOW


# ------------------------------------------------------------ money honesty


@pytest.mark.parametrize(
    "root_cause",
    [
        RootCause.CARD_EXPIRED,
        RootCause.INSUFFICIENT_FUNDS,
        RootCause.BANK_RISK_BLOCK,
        RootCause.UNKNOWN,
    ],
)
def test_nothing_is_recorded_as_recovered(
    session, raw_envelopes, root_cause: RootCause
) -> None:
    """Only a provider webhook may set this. A send is not a payment."""
    outcome, *_ = run(session, fresh(raw_envelopes[0]), root_cause=root_cause)
    row = session.get(ExecutionRecord, outcome.event_record.event_id)

    assert row.amount_recovered_minor is None
    assert row.customer_outcome == "pending"


def test_the_link_is_for_the_amount_on_the_event(session, envelope) -> None:
    outcome, _, factory, _ = run(session, envelope)

    entity = envelope["payload"]["payment"]["entity"]
    assert factory.calls[0]["amount_minor"] == entity["amount"]


@pytest.mark.parametrize(
    "root_cause",
    [
        RootCause.CARD_EXPIRED,
        RootCause.INSUFFICIENT_FUNDS,
        RootCause.BANK_RISK_BLOCK,
        RootCause.SCA_ABANDONED,
        RootCause.NETWORK_ERROR,
        RootCause.UNKNOWN,
    ],
)
def test_no_root_cause_leads_to_a_charge(
    session, raw_envelopes, root_cause: RootCause
) -> None:
    """The recording doubles raise on any provider call beyond link and send."""
    outcome, *_ = run(session, fresh(raw_envelopes[0]), root_cause=root_cause)

    assert outcome.execution is not None


# ------------------------------------------------------------------- summary


def test_summary_is_json_serialisable(session, envelope) -> None:
    """It is returned as an HTTP response body, so it has to serialise."""
    outcome, *_ = run(session, envelope)

    body = json.loads(json.dumps(outcome.summary()))

    assert body["event_id"] == outcome.event_record.event_id
    assert body["root_cause"] == "card_expired"
    assert body["action"] == "send_update_payment_method_link"
    assert body["delivery_status"] == "sent"
    assert body["blocked"] is False
    assert body["duplicate"] is False


def test_summary_reports_a_block_distinctly_from_a_skip(
    session, raw_envelopes
) -> None:
    outcome, *_ = run(
        session, fresh(raw_envelopes[0]), root_cause=RootCause.BANK_RISK_BLOCK
    )

    summary = outcome.summary()
    assert summary["delivery_status"] == "skipped"
    # Escalation is a deliberate action, not a guardrail cancellation.
    assert summary["blocked"] is False


# ------------------------------------------------- through the webhook route


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def route_client(session, monkeypatch, *, classifier: FakeClassifier, **overrides):
    """TestClient with the inline pipeline on and every client faked.

    Quiet hours are opened to the full day here because the route uses the real
    clock, and a test that only passes between 09:00 and 20:00 IST is not a test.
    """
    settings = pinned_settings(
        razorpay_key_id="rzp_test_placeholder",
        razorpay_key_secret="placeholder",
        razorpay_webhook_secret=TEST_SECRET,
        pipeline_run_inline=True,
        quiet_hours_start_local=0,
        quiet_hours_end_local=24,
        **overrides,
    )
    for module in (guardrails, decide, diagnose, channels):
        monkeypatch.setattr(module, "get_settings", lambda s=settings: s)

    factory, sender = RecordingFactory(), RecordingSender()
    monkeypatch.setattr(diagnose, "build_client", lambda: classifier)
    monkeypatch.setattr(
        channels, "build_payment_link_factory", lambda s=None: factory
    )
    monkeypatch.setattr(channels, "build_message_sender", lambda s=None: sender)

    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app), factory, sender


def post_signed(client: TestClient, envelope: dict[str, Any]):
    body, headers = signing.signed_delivery(envelope, TEST_SECRET)
    return client.post(WEBHOOK_PATH, content=body, headers=headers)


@pytest.fixture
def route_envelope(raw_envelopes):
    """Dated against the real clock.

    The route does not take an injected ``now``, so an event pinned to the
    fixtures' 2026-06 date would be months past the 7-day hard stop and get
    blocked before reaching the behaviour under test.
    """
    return fresh(raw_envelopes[0], at=datetime.now(UTC))


def test_inline_route_runs_all_four_stages(
    session, route_envelope, monkeypatch
) -> None:
    """The wiring the demo actually uses, driven over HTTP with a signed body."""
    client, factory, sender = route_client(
        session, monkeypatch, classifier=FakeClassifier(RootCause.CARD_EXPIRED)
    )

    response = post_signed(client, route_envelope)
    session.flush()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "detected"
    assert body["root_cause"] == "card_expired"
    assert body["action"] == "send_update_payment_method_link"
    assert body["delivery_status"] == "sent"
    assert body["decision_latency_ms"] < 60_000
    assert stages(session, body["event_id"]) == [str(s) for s in STAGE_ORDER]
    assert len(sender.calls) == 1


def test_inline_route_uses_the_injected_classifier_not_a_real_one(
    session, route_envelope, monkeypatch
) -> None:
    """Proof the route test is offline, so it cannot spend the daily quota."""
    classifier = FakeClassifier(RootCause.SCA_ABANDONED)
    client, _, sender = route_client(session, monkeypatch, classifier=classifier)

    body = post_signed(client, route_envelope).json()

    assert len(classifier.calls) == 1
    assert body["root_cause"] == "sca_abandoned"
    assert len(sender.calls) == 1


def test_inline_route_reports_the_degraded_path(
    session, route_envelope, monkeypatch
) -> None:
    """A classifier outage still acknowledges the webhook.

    Returning an error would make Razorpay retry a delivery we already stored.
    """
    client, factory, sender = route_client(
        session,
        monkeypatch,
        classifier=FakeClassifier(error=RuntimeError("upstream connection reset")),
    )

    response = post_signed(client, route_envelope)
    session.flush()

    assert response.status_code == 200
    body = response.json()
    assert body["classifier_unavailable"] is True
    assert body["root_cause"] == "unknown"
    assert body["action"] == "escalate_to_human_review"
    assert body["delivery_status"] == "skipped"
    assert sender.calls == []
    assert len(audit_entries(session, body["event_id"])) == 4


def test_inline_route_acknowledges_a_duplicate_without_re_sending(
    session, route_envelope, monkeypatch
) -> None:
    """Razorpay retries deliveries, and a retry must not message twice.

    A 2xx is deliberate: a non-2xx would make it keep retrying an event we have
    already fully processed.
    """
    client, factory, sender = route_client(
        session, monkeypatch, classifier=FakeClassifier(RootCause.CARD_EXPIRED)
    )

    first = post_signed(client, route_envelope)
    second = post_signed(client, copy.deepcopy(route_envelope))
    session.flush()

    assert first.json()["status"] == "detected"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert second.json()["event_id"] == first.json()["event_id"]
    assert len(sender.calls) == 1


def test_inline_route_still_rejects_a_bad_signature(
    session, route_envelope, monkeypatch
) -> None:
    """The pipeline must never run on a body that was not verified first.

    401 rather than 400 is deliberate: a wrong secret is fixable, so the delivery
    should be retried once it is corrected.
    """
    client, factory, sender = route_client(
        session, monkeypatch, classifier=FakeClassifier()
    )
    body, headers = signing.signed_delivery(route_envelope, "the_wrong_secret")

    response = client.post(WEBHOOK_PATH, content=body, headers=headers)

    assert response.status_code == 401
    assert factory.calls == []
    assert sender.calls == []
