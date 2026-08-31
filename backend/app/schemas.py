"""Shared Pydantic schemas for the AI Revenue Recovery Agent.

Every enum value here is copied from ``context/architecture.md`` ("Data schema"
and "Fixed action set"). Per ``context/ai-workflow-rules.md`` -> Scope
discipline: to add a root cause, action, or channel, update
``architecture.md`` FIRST, then mirror it here. Code and docs must not drift.

``tests/test_schema_contract.py`` parses ``architecture.md`` and fails if these
enums stop matching the doc, so the drift rule is enforced rather than trusted.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EventType(StrEnum):
    """``event_type`` values from the Event record schema."""

    PAYMENT_FAILED = "payment_failed"
    CHECKOUT_ABANDONED = "checkout_abandoned"
    INVOICE_OVERDUE = "invoice_overdue"


class RootCause(StrEnum):
    """Fixed DIAGNOSE taxonomy. The LLM may not invent categories.

    Low confidence routes to ``UNKNOWN`` -> ``escalate_to_human_review``
    rather than forcing a guess into a specific category.
    """

    CARD_EXPIRED = "card_expired"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    BANK_RISK_BLOCK = "bank_risk_block"
    SCA_ABANDONED = "sca_abandoned"
    NETWORK_ERROR = "network_error"
    CHECKOUT_FRICTION = "checkout_friction"
    GENUINE_ABANDONMENT = "genuine_abandonment"
    UNKNOWN = "unknown"


class Action(StrEnum):
    """Action verbs DECIDE may choose from.

    ``architecture.md``'s table writes some entries with their parameters
    inline (``schedule_retry(+N days)``, ``send_reminder(1x), then stop``).
    Those parentheticals are arguments, not identifiers, so they live on
    :class:`Decision` as ``delay_seconds`` / ``max_repeats`` instead of being
    baked into enum names. All 8 table rows survive as 8 root_cause -> action
    mappings; see the decisions log in ``context/progress-tracker.md``.
    """

    SEND_UPDATE_PAYMENT_METHOD_LINK = "send_update_payment_method_link"
    SCHEDULE_RETRY = "schedule_retry"
    ESCALATE_TO_HUMAN_REVIEW = "escalate_to_human_review"
    SEND_FRESH_AUTH_LINK = "send_fresh_auth_link"
    SEND_REMINDER = "send_reminder"


class Channel(StrEnum):
    """Contact channels. There is deliberately no voice channel."""

    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"
    NONE = "none"


class DeliveryStatus(StrEnum):
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class CustomerOutcome(StrEnum):
    RECOVERED = "recovered"
    PENDING = "pending"
    FAILED = "failed"
    EXPIRED = "expired"


class GuardrailName(StrEnum):
    """The four stopping rules from Non-negotiable constraint #4.

    ``architecture.md``'s Decision example lists only the first three; the
    constraint itself mandates a fourth (7-day hard stop). The constraint wins.
    """

    MAX_RETRIES = "max_retries"
    QUIET_HOURS = "quiet_hours"
    CONTACT_FREQUENCY = "contact_frequency"
    HARD_STOP_7_DAYS = "hard_stop_7_days"


class Stage(StrEnum):
    """Pipeline stage, used as the ``stage`` field on audit log entries."""

    DETECT = "detect"
    DIAGNOSE = "diagnose"
    DECIDE = "decide"
    EXECUTE = "execute"


# ---------------------------------------------------------------------------
# Provider-level types
#
# Everything above mirrors architecture.md. Everything below describes the
# Razorpay payment entity instead, and is shared by the simulation layer and
# (from Phase 2) DETECT. Kept separate so the architecture-mirroring section
# stays a faithful copy of the doc. The contract tests only police the section
# above, because only that section is supposed to match architecture.md.
# ---------------------------------------------------------------------------


class PaymentMethod(StrEnum):
    """``method`` on the Razorpay payment entity.

    These five are the documented values for the payment entity itself. The
    longer list of checkout method codes (``debit``, ``credit``, ``paylater``,
    ``cardless_emi`` and so on) is a different vocabulary and must not be
    conflated with this one.

    Source: https://razorpay.com/docs/api/payments/entity/
    """

    CARD = "card"
    NETBANKING = "netbanking"
    WALLET = "wallet"
    EMI = "emi"
    UPI = "upi"


class CustomerHistory(BaseModel):
    """``customer_history`` sub-object on the Event record."""

    model_config = ConfigDict(extra="forbid")

    tenure_days: int = Field(ge=0)
    past_failures: int = Field(ge=0)


class EventRecord(BaseModel):
    """Created at DETECT. Normalized shape for every provider and event type.

    Carries no card data of any kind: no PAN, no CVV, no expiry, no raw
    provider payment-method object. Non-negotiable constraint #1.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str
    customer_id: str
    event_type: EventType
    decline_code: str | None = None
    amount: Decimal = Field(ge=0)
    currency: str
    prior_attempts: int = Field(ge=0)
    customer_history: CustomerHistory
    detected_at: datetime


class Diagnosis(BaseModel):
    """Created at DIAGNOSE. This is the Pydantic model the LLM output is
    validated against before anything downstream is allowed to use it."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    root_cause: RootCause
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1)


class GuardrailCheck(BaseModel):
    """One guardrail evaluation. Recorded whether it passed or failed, so the
    audit trail shows the check ran (constraint #5)."""

    model_config = ConfigDict(extra="forbid")

    name: GuardrailName
    passed: bool
    detail: str = Field(min_length=1)


class Decision(BaseModel):
    """Created at DECIDE by deterministic rules code. Never by an LLM call."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    action: Action
    channel: Channel
    scheduled_for: datetime | None = None
    guardrail_checks: list[GuardrailCheck]
    blocked_reason: str | None = None

    # Parameters for the actions architecture.md writes with inline args.
    delay_seconds: int | None = Field(default=None, ge=0)
    max_repeats: int | None = Field(default=None, ge=0)

    @property
    def guardrail_checks_passed(self) -> list[GuardrailName]:
        """Verbatim field name from ``architecture.md``'s Decision schema.

        Derived from :attr:`guardrail_checks` so the two can never disagree.
        """
        return [c.name for c in self.guardrail_checks if c.passed]


class ExecutionResult(BaseModel):
    """Created at EXECUTE, then updated when the provider confirms an outcome."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    executed_at: datetime
    delivery_status: DeliveryStatus
    customer_outcome: CustomerOutcome
    amount_recovered: Decimal | None = Field(default=None, ge=0)
