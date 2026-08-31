"""Pipeline orchestration: DETECT -> DIAGNOSE -> DECIDE -> EXECUTE.

Each stage stays a separate module with its own input and output schema
(``code-standards.md``). This module only chains them and persists what each one
produced, so no stage's logic leaks into another's file.

## Every stage writes an audit entry, always

The bar from ``project-overview.md`` is that a stranger can read the trail for one
event and understand what happened and why, in under 30 seconds. That only holds
if all four stages appear for every event, including the ones where nothing was
sent. A skipped send with a stated reason is far more useful than a gap.

## Degradation is designed, not incidental

If DIAGNOSE cannot reach the classifier, it returns ``unknown`` marked
``classifier_unavailable``. DECIDE maps ``unknown`` to
``escalate_to_human_review``, which EXECUTE records without contacting anyone. So
a quota outage produces a safe, fully audited escalation rather than a crash or a
silently dropped event. That path is exercised by test, not hoped for.

## Two latencies, not one

``architecture.md`` asks for DETECT -> EXECUTE under 60 seconds, but a quiet-hours
deferral correctly schedules a send hours later. Reporting one blended number
would score correct compliance as failure, so this records both:

  ``decision_latency_ms``  received -> decided. The real proof of real-time
                           operation, and what should be held under 60s.
  ``send_latency_ms``      received -> dispatched. Legitimately long when a send
                           is deferred to the next allowed window.

See Known issue A in progress-tracker.md.

## Contact time updates only on a real send

``Customer.last_contacted_at`` moves only when delivery status is ``sent``. A dry
run or a refused send must not consume the customer's 24-hour contact window,
or a configuration gap would silently suppress genuine future sends.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app import audit, decide, detect, diagnose, execute
from app.channels import MessageSender, PaymentLinkFactory
from app.diagnose import CLASSIFIER_UNAVAILABLE_PREFIX, LlmDiagnosis, SupportsGenerate
from app.models import Customer, DecisionRecord, DiagnosisRecord, Event, ExecutionRecord
from app.schemas import (
    Decision,
    DeliveryStatus,
    Diagnosis,
    EventRecord,
    Stage,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PipelineOutcome:
    """What the whole pipeline did with one webhook delivery."""

    event_record: EventRecord
    is_duplicate: bool
    diagnosis: Diagnosis | None = None
    decision: Decision | None = None
    execution: execute.ExecutionOutcome | None = None
    decision_latency_ms: float | None = None
    send_latency_ms: float | None = None
    classifier_unavailable: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "event_id": self.event_record.event_id,
            "duplicate": self.is_duplicate,
            "root_cause": str(self.diagnosis.root_cause) if self.diagnosis else None,
            "classifier_unavailable": self.classifier_unavailable,
            "action": str(self.decision.action) if self.decision else None,
            "blocked": bool(self.decision and self.decision.blocked_reason),
            "delivery_status": (
                str(self.execution.result.delivery_status) if self.execution else None
            ),
            "decision_latency_ms": self.decision_latency_ms,
            "send_latency_ms": self.send_latency_ms,
        }


def _provider_context(event_row: Event) -> diagnose.ProviderContext:
    """The provider error detail ``EventRecord`` has no field for.

    ``error_source`` and ``error_step`` are what separate an opaque "payment
    failed" from an identifiable bank-side or authentication-side fault, and
    Phase 2b kept them on the ``events`` table for exactly this handoff.
    """
    return diagnose.ProviderContext(
        error_code=event_row.error_code,
        error_description=event_row.error_description,
        error_source=event_row.error_source,
        error_step=event_row.error_step,
        payment_method=event_row.payment_method,
    )


def process_event(
    session: Session,
    payload: dict[str, Any],
    *,
    diagnose_client: SupportsGenerate | None = None,
    diagnose_cache: dict[str, LlmDiagnosis] | None = None,
    link_factory: PaymentLinkFactory | None = None,
    sender: MessageSender | None = None,
    now: datetime | None = None,
) -> PipelineOutcome:
    """Run one verified webhook payload through all four stages.

    Caller owns the transaction. Raises only what DETECT raises for input it will
    not accept (:class:`~app.detect.MalformedPayloadError`,
    :class:`~app.detect.UnsupportedEventError`); once an event is detected, the
    remaining stages degrade rather than raise.

    The injectable clients exist so a batch run can share a classification cache
    and so tests can drive every branch without network calls.
    """
    started = time.perf_counter()

    detection = detect.detect_event(session, payload)
    if detection.is_duplicate:
        # Already processed. Re-running the stages would double-count and could
        # message the customer twice.
        return PipelineOutcome(
            event_record=detection.event_record, is_duplicate=True
        )

    event_record = detection.event_record
    event_row = session.get(Event, event_record.event_id)
    customer = session.get(Customer, event_record.customer_id)
    if event_row is None or customer is None:  # pragma: no cover - DETECT guarantees
        raise RuntimeError(f"DETECT did not persist {event_record.event_id}")

    evaluated_at = now or datetime.now(UTC)

    # ---------------------------------------------------------------- DIAGNOSE
    provider_context = _provider_context(event_row)
    diagnosis = diagnose.diagnose_root_cause(
        event_record,
        provider_context,
        client=diagnose_client,
        cache=diagnose_cache,
    )
    classifier_unavailable = diagnosis.reasoning.startswith(
        CLASSIFIER_UNAVAILABLE_PREFIX
    )

    diag_in, diag_out = diagnose.audit_summaries(
        event_record, diagnosis, provider_context
    )
    session.merge(
        DiagnosisRecord(
            event_id=event_record.event_id,
            root_cause=str(diagnosis.root_cause),
            confidence=diagnosis.confidence,
            reasoning=diagnosis.reasoning[:2000],
            model=diag_in.get("model"),
            prompt_version=diag_in.get("prompt_version"),
            classifier_unavailable=classifier_unavailable,
        )
    )
    audit.record(
        session,
        event_id=event_record.event_id,
        stage=Stage.DIAGNOSE,
        input_summary=diag_in,
        output_summary=diag_out,
        notes=(
            "Classifier did not run; escalated as the safe default. This is an "
            "operational failure, not a judgement about the evidence."
            if classifier_unavailable
            else None
        ),
    )

    # ------------------------------------------------------------------ DECIDE
    decision_context = decide.DecisionContext(
        customer_timezone=customer.timezone,
        first_failure_at=event_row.first_failure_at,
        last_contact_at=customer.last_contacted_at,
        now=evaluated_at,
    )
    decision = decide.decide_action(event_record, diagnosis, decision_context)
    decision_latency_ms = (time.perf_counter() - started) * 1000

    dec_in, dec_out = decide.audit_summaries(event_record, diagnosis, decision)
    session.merge(
        DecisionRecord(
            event_id=event_record.event_id,
            action=str(decision.action),
            channel=str(decision.channel),
            scheduled_for=decision.scheduled_for,
            delay_seconds=decision.delay_seconds,
            max_repeats=decision.max_repeats,
            blocked_reason=(
                decision.blocked_reason[:1000] if decision.blocked_reason else None
            ),
            guardrail_checks=decide.guardrail_check_rows(decision),
        )
    )
    audit.record(
        session,
        event_id=event_record.event_id,
        stage=Stage.DECIDE,
        input_summary=dec_in,
        output_summary=dec_out,
        # Constraint #5: every check result, including the ones that passed.
        guardrail_checks=decide.guardrail_check_rows(decision),
    )

    # ----------------------------------------------------------------- EXECUTE
    execution_context = execute.ExecutionContext(
        customer_id=customer.customer_id,
        amount_at_risk_minor=event_row.amount_minor,
        customer_name=None,
        email=customer.email,
        contact=customer.contact,
        now=evaluated_at,
    )
    outcome = execute.execute_action(
        decision,
        execution_context,
        link_factory=link_factory,
        sender=sender,
    )
    send_latency_ms = (time.perf_counter() - started) * 1000

    exec_in, exec_out = execute.audit_summaries(decision, outcome, execution_context)
    link = outcome.recovery_link
    session.merge(
        ExecutionRecord(
            event_id=event_record.event_id,
            action=str(outcome.action),
            channel=str(outcome.channel),
            delivery_status=str(outcome.result.delivery_status),
            customer_outcome=str(outcome.result.customer_outcome),
            # Stays null until a provider webhook confirms payment. Pre-filling it
            # would invent revenue.
            amount_recovered_minor=None,
            recovery_link_id=link.link_id if link else None,
            recovery_link_url=link.url if link else None,
            provider_message_id=outcome.provider_message_id,
            skip_reason=outcome.skip_reason[:512] if outcome.skip_reason else None,
            failure_reason=(
                outcome.failure_reason[:512] if outcome.failure_reason else None
            ),
            requeued=outcome.requeued,
        )
    )
    audit.record(
        session,
        event_id=event_record.event_id,
        stage=Stage.EXECUTE,
        input_summary=exec_in,
        output_summary=exec_out,
        notes=outcome.skip_reason or outcome.failure_reason,
    )

    # Only a genuine send consumes the customer's contact window. A dry run or a
    # refused send must not suppress future real sends.
    if outcome.result.delivery_status is DeliveryStatus.SENT:
        customer.last_contacted_at = evaluated_at

    session.flush()

    return PipelineOutcome(
        event_record=event_record,
        is_duplicate=False,
        diagnosis=diagnosis,
        decision=decision,
        execution=outcome,
        decision_latency_ms=round(decision_latency_ms, 2),
        send_latency_ms=round(send_latency_ms, 2),
        classifier_unavailable=classifier_unavailable,
    )
