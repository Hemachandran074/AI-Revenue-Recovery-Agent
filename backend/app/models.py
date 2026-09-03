"""ORM models for the event pipeline and the audit log.

Three tables in Phase 2:

``customers``  our own customer records. NOT provider data. Exists because
               ``EventRecord`` requires ``tenure_days`` and ``past_failures``,
               and it is where ``timezone`` lives so the quiet-hours rule can be
               evaluated in customer local time without deviating from
               ``architecture.md``'s Event record schema.
``events``     one row per detected event, holding the normalised fields plus
               the full raw payload for audit.
``audit_log``  one row per event per stage. The thing that makes
               "100% audit trail coverage" a query rather than a claim.

Later phases add ``diagnoses``, ``decisions``, ``execution_results`` (3b and 5)
and ``event_latencies`` (6). See the section divider further down for why those
are separate tables rather than columns.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    """Declarative base.

    ``JSONB`` is Postgres-specific and is what we run against; the variant
    below keeps the models importable under SQLite for pure-unit tests that
    never touch a real database.
    """


JSONType = JSONB().with_variant(JSON(), "sqlite")


class Customer(Base):
    """Our own record of a customer.

    ``profile_source`` matters for audit honesty. When a webhook arrives for a
    customer we have no profile for, the quiet-hours rule still has to run, so a
    default timezone gets used. Recording that the profile was defaulted means
    the audit trail shows the check was made on an assumption rather than on
    known data, instead of presenting a guess as fact.
    """

    __tablename__ = "customers"

    __table_args__ = (
        # Contact and email are how a customer is re-identified when a webhook
        # arrives without an explicit customer id. Indexed because that lookup
        # runs on every such event.
        Index("ix_customers_contact", "contact"),
        Index("ix_customers_email", "email"),
    )

    customer_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str | None] = mapped_column(String(320))
    contact: Mapped[str | None] = mapped_column(String(32))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    tenure_days: Mapped[int] = mapped_column(Integer, default=0)
    past_failures: Mapped[int] = mapped_column(Integer, default=0)
    profile_source: Mapped[str] = mapped_column(String(16), default="defaulted")
    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    events: Mapped[list[Event]] = relationship(back_populates="customer")


class Event(Base):
    """A detected revenue-at-risk event.

    ``provider_payment_id`` is UNIQUE. Razorpay retries webhook deliveries, and
    without this constraint a redelivery would create a second event and
    double-count the amount at risk, corrupting the headline metric. Idempotency
    here is a correctness requirement, not a nicety.

    ``amount_minor`` (paise) is the source of truth. Rupees are derived for
    display so no rounding ever enters the stored value.
    """

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("provider_payment_id", name="uq_events_provider_payment_id"),
        Index("ix_events_order", "provider_order_id"),
        Index("ix_events_detected_at", "detected_at"),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider: Mapped[str] = mapped_column(String(16), default="razorpay")
    provider_payment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_order_id: Mapped[str | None] = mapped_column(String(64))
    provider_invoice_id: Mapped[str | None] = mapped_column(String(64))
    provider_event_name: Mapped[str] = mapped_column(String(64))

    customer_id: Mapped[str] = mapped_column(
        ForeignKey("customers.customer_id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)

    # decline_code carries Razorpay's error_reason. The remaining error fields are
    # kept alongside because error_source and error_step are the only signal that
    # distinguishes an opaque "payment_failed" from a bank-side one, and
    # architecture.md's EventRecord has no field for them. Retained here so no
    # fidelity is lost while EventRecord stays a verbatim copy of the doc.
    decline_code: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_description: Mapped[str | None] = mapped_column(String(512))
    error_source: Mapped[str | None] = mapped_column(String(32))
    error_step: Mapped[str | None] = mapped_column(String(64))
    payment_method: Mapped[str | None] = mapped_column(String(16))

    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    prior_attempts: Mapped[int] = mapped_column(Integer, default=0)
    tenure_days: Mapped[int] = mapped_column(Integer, default=0)
    past_failures: Mapped[int] = mapped_column(Integer, default=0)

    first_failure_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Wall-clock arrival, kept separate from the provider's created_at so
    # detect-to-decision latency is measured against when WE learned of it.
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)

    customer: Mapped[Customer] = relationship(back_populates="events")
    audit_entries: Mapped[list[AuditLogEntry]] = relationship(
        back_populates="event", order_by="AuditLogEntry.id"
    )


class AuditLogEntry(Base):
    """One structured entry per event per stage.

    Field set follows ``code-standards.md``: ``event_id``, ``stage``,
    ``timestamp``, ``input_summary``, ``output_summary``, plus
    ``guardrail_checks`` which DECIDE populates with every check result,
    including the ones that passed.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_event_stage", "event_id", "stage"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(
        ForeignKey("events.event_id"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    input_summary: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    output_summary: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    guardrail_checks: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    notes: Mapped[str | None] = mapped_column(String(512))

    event: Mapped[Event] = relationship(back_populates="audit_entries")


# ---------------------------------------------------------------------------
# Stage output records (Phases 3b and 5)
#
# architecture.md defines four record types: Event, Diagnosis, Decision and
# Execution result. Event already exists above; these are the other three.
#
# They are NEW tables rather than columns on `events`, deliberately: this project
# uses create_all() with no migrations, which creates missing tables but cannot
# add columns to an existing one. New tables therefore land without dropping the
# demo data. The trade-off is stated in db.py.
#
# The audit log already carries stage summaries as JSON, so these are partly
# redundant with it. They exist because Phase 6 has to compute money recovered and
# per-cause recovery rates, and a typed column is a far sounder basis for a
# headline number than digging through JSON.
# ---------------------------------------------------------------------------


class DiagnosisRecord(Base):
    """DIAGNOSE output for one event.

    One row per event: a re-diagnosis overwrites rather than accumulating, since
    the pipeline classifies once per event.
    """

    __tablename__ = "diagnoses"

    event_id: Mapped[str] = mapped_column(
        ForeignKey("events.event_id"), primary_key=True
    )
    root_cause: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    # The model's one-sentence justification, kept verbatim per
    # ai-workflow-rules.md so a reader can judge the classification itself.
    reasoning: Mapped[str] = mapped_column(String(2000), nullable=False)
    model: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(16))
    # Separates "we looked and could not tell" from "we never looked". Without
    # this, a quota outage would be indistinguishable from cautious diagnosis and
    # would corrupt any accuracy or escalation figure.
    classifier_unavailable: Mapped[bool] = mapped_column(Boolean, default=False)
    diagnosed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DecisionRecord(Base):
    """DECIDE output for one event, including every guardrail result.

    ``guardrail_checks`` holds all four checks with pass/fail and reason, not only
    the ones that mattered — constraint #5 requires the trail to show the check
    happened, not just its consequence.
    """

    __tablename__ = "decisions"

    event_id: Mapped[str] = mapped_column(
        ForeignKey("events.event_id"), primary_key=True
    )
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delay_seconds: Mapped[int | None] = mapped_column(Integer)
    max_repeats: Mapped[int | None] = mapped_column(Integer)
    # Non-null means a guardrail stopped this action. THE signal EXECUTE keys on;
    # `channel` cannot serve, because escalate_to_human_review legitimately has
    # channel 'none' while still being an action that should happen.
    blocked_reason: Mapped[str | None] = mapped_column(String(1000))
    guardrail_checks: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONType, default=list
    )
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CustomerPayday(Base):
    """When a customer is paid, if anybody has told us (Phase 8).

    ``architecture.md`` asks for an insufficient-funds retry to be "payday-aware
    if data available". This table is the *if available* part, and it is a separate
    table rather than a column on ``customers`` for the reason recorded above:
    ``create_all()`` cannot add a column to an existing table.

    ``source`` exists so a value can never be mistaken for something we worked out
    ourselves. The honest options today are:

    ``merchant_supplied``  the merchant told us, from their own records
    ``customer_stated``    the customer told us

    Nothing infers a payday from payment history, because this system has no
    history of *successful* payments to infer one from. A guess here would move a
    retry to a date chosen by nothing and dress it up as insight.

    Rows are expected to be sparse. Most customers will have none, and the flat
    interval is what actually runs for them.
    """

    __tablename__ = "customer_paydays"

    customer_id: Mapped[str] = mapped_column(
        ForeignKey("customers.customer_id"), primary_key=True
    )
    # 1-31. Clamped to the real last day when a month is shorter, so a payday of
    # 31 still resolves in February instead of falling over.
    day_of_month: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EventLatency(Base):
    """Measured pipeline latency for one event (Phase 6).

    This table exists because the latency metric CANNOT be derived from the
    timestamps on the other tables, which is not obvious and cost a debugging
    session to discover. ``events.received_at``, ``decisions.decided_at`` and
    ``execution_results.executed_at`` all default to ``func.now()``, and Postgres
    ``now()`` is ``transaction_timestamp()`` — stable for the whole transaction.
    The pipeline writes all four stages in ONE transaction, so those three columns
    resolve to the identical instant and any subtraction between them yields
    exactly zero. A dashboard built on that would have reported 0 ms for every
    event and looked like a triumph.

    So the numbers here come from ``perf_counter`` around the real run, recorded
    by ``pipeline.process_event``.

    Two figures, not one, per Known issue A. ``architecture.md`` asks for
    DETECT -> EXECUTE under 60 seconds, but a quiet-hours deferral correctly
    delays a send by hours, and a single blended number would score that
    compliance as a failure.
    """

    __tablename__ = "event_latencies"

    event_id: Mapped[str] = mapped_column(
        ForeignKey("events.event_id"), primary_key=True
    )
    # received -> decided. The real proof of real-time operation, and the figure
    # that should be held under the 60-second target.
    decision_latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    # received -> dispatched. Legitimately long when a send is deferred.
    send_latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ExecutionRecord(Base):
    """EXECUTE output for one event.

    ``amount_recovered_minor`` stays null until a provider webhook confirms
    payment. Defaulting it to the amount at risk would invent revenue, which is
    the one number in this project that must never be optimistic.
    """

    __tablename__ = "execution_results"
    __table_args__ = (Index("ix_execution_outcome", "customer_outcome"),)

    event_id: Mapped[str] = mapped_column(
        ForeignKey("events.event_id"), primary_key=True
    )
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    delivery_status: Mapped[str] = mapped_column(String(16), nullable=False)
    customer_outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_recovered_minor: Mapped[int | None] = mapped_column(BigInteger)
    # The Razorpay-hosted link the customer was sent, if any. Recorded so a
    # reviewer can confirm the customer completed payment on the provider's page
    # rather than anything this system hosted (constraints #1, #2, #6).
    recovery_link_id: Mapped[str | None] = mapped_column(String(64))
    recovery_link_url: Mapped[str | None] = mapped_column(String(512))
    provider_message_id: Mapped[str | None] = mapped_column(String(64))
    # Why nothing was sent, when nothing was sent. Never left blank on a skip.
    skip_reason: Mapped[str | None] = mapped_column(String(512))
    failure_reason: Mapped[str | None] = mapped_column(String(512))
    # A failed dispatch must never vanish (code-standards.md -> Error handling).
    requeued: Mapped[bool] = mapped_column(Boolean, default=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    outcome_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
