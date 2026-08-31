"""Phase 6 metrics: the four numbers ``project-overview.md`` calls "the bar".

    1. $ recovered / $ at risk
    2. time from detected -> action sent
    3. 0 compliance/stopping-rule violations across the batch
    4. 100% audit trail coverage

Three design points, because each is the difference between a number that means
something and a number that merely looks good.

## Violations are RE-DERIVED, never read back

The easy way to report "0 violations" is to count decisions whose recorded
guardrail checks all passed. That is circular: it asks the enforcing code to
grade itself, so a bug in ``guardrails.py`` would be invisible in precisely the
metric meant to catch it. Instead each stopping rule is reconstructed from raw
data and asked of what actually happened — was a message sent at 3am in the
customer's own timezone, were two messages sent to one person inside 24 hours,
did anything go out after the 7-day window shut. The recorded flags are used for
exactly one check: that all four results are *present*, which is constraint #5
and is a question about the trail, not about the rule.

## The denominator is split, not blended

Known issue M. In a typical batch only a minority of events end with a customer
being messaged; the rest schedule a provider-side retry, escalate to a human, or
are stopped by a guardrail. All of that is correct behaviour, so dividing
recovered by everything at risk would report compliance as failure. Events go
into mutually exclusive dispositions, and the recovery rate is reported against
both the whole batch and the actioned subset.

## Two latencies, not one

Known issue A. ``architecture.md`` wants DETECT -> EXECUTE under 60 seconds, but a
quiet-hours deferral correctly delays a send by hours. Decision latency is held
against the budget; send latency sits beside it and is legitimately long when a
send was deferred.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    AuditLogEntry,
    Customer,
    DecisionRecord,
    DiagnosisRecord,
    Event,
    EventLatency,
    ExecutionRecord,
)
from app.schemas import Action, GuardrailName, Stage

# Actions that put a message in front of a person. Constraint #4's stopping rules
# are about contacting people, so these are what the re-derived checks apply to; a
# silent provider-side retry or an internal escalation disturbs nobody.
CONTACT_ACTIONS = frozenset(
    {
        str(Action.SEND_UPDATE_PAYMENT_METHOD_LINK),
        str(Action.SEND_FRESH_AUTH_LINK),
        str(Action.SEND_REMINDER),
    }
)

REQUIRED_STAGES: tuple[str, ...] = tuple(
    str(s) for s in (Stage.DETECT, Stage.DIAGNOSE, Stage.DECIDE, Stage.EXECUTE)
)

# architecture.md -> Real-time requirement.
LATENCY_BUDGET_MS = 60_000.0

DEFAULT_TIMEZONE = "Asia/Kolkata"


@dataclass(frozen=True, slots=True)
class Violation:
    """One stopping-rule breach, described so someone could act on it."""

    event_id: str
    rule: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LatencyStats:
    """Distribution of one latency measure, in milliseconds."""

    count: int
    mean_ms: float | None = None
    p50_ms: float | None = None
    p95_ms: float | None = None
    max_ms: float | None = None
    over_budget: int = 0

    @classmethod
    def of(cls, values: list[float]) -> LatencyStats:
        if not values:
            # Nothing measured. Zeros here would claim instant processing of an
            # empty batch, which is a different statement from "no data".
            return cls(count=0)
        ordered = sorted(values)
        return cls(
            count=len(ordered),
            mean_ms=round(statistics.fmean(ordered), 2),
            p50_ms=round(_percentile(ordered, 50), 2),
            p95_ms=round(_percentile(ordered, 95), 2),
            max_ms=round(ordered[-1], 2),
            over_budget=sum(1 for value in ordered if value > LATENCY_BUDGET_MS),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _percentile(ordered: list[float], pct: float) -> float:
    """Nearest-rank percentile. Exact and obvious beats interpolated here."""
    if not ordered:
        raise ValueError("no values to take a percentile of")
    rank = max(1, min(len(ordered), int(pct / 100 * len(ordered) + 0.5)))
    return ordered[rank - 1]


@dataclass(frozen=True, slots=True)
class MoneyMetrics:
    """The headline figure, plus enough context to read it honestly."""

    at_risk_minor: int
    recovered_minor: int
    actioned_at_risk_minor: int
    currency: str = "INR"

    @property
    def recovery_rate(self) -> float | None:
        """Recovered over everything at risk.

        ``None`` rather than 0.0 for an empty batch: "0% of nothing" and "0% of
        what was at stake" are different claims and should not render the same.
        """
        if self.at_risk_minor <= 0:
            return None
        return round(self.recovered_minor / self.at_risk_minor, 4)

    @property
    def actioned_recovery_rate(self) -> float | None:
        """Recovered over the subset a recovery action was actually taken on.

        The fairer denominator per Known issue M: it says something about whether
        the recovery worked, rather than about how much of the batch correctly
        needed no contact at all.
        """
        if self.actioned_at_risk_minor <= 0:
            return None
        return round(self.recovered_minor / self.actioned_at_risk_minor, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "at_risk": round(self.at_risk_minor / 100, 2),
            "recovered": round(self.recovered_minor / 100, 2),
            "actioned_at_risk": round(self.actioned_at_risk_minor / 100, 2),
            "recovery_rate": self.recovery_rate,
            "actioned_recovery_rate": self.actioned_recovery_rate,
        }


@dataclass(frozen=True, slots=True)
class AuditCoverage:
    """Whether every event can actually be traced end to end."""

    events: int
    fully_covered: int
    incomplete: list[dict[str, Any]] = field(default_factory=list)

    @property
    def rate(self) -> float | None:
        if self.events <= 0:
            return None
        return round(self.fully_covered / self.events, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "events": self.events,
            "fully_covered": self.fully_covered,
            "rate": self.rate,
            "required_stages": list(REQUIRED_STAGES),
            "incomplete": self.incomplete,
        }


@dataclass(slots=True)
class EventRow:
    """One event with every stage's result joined onto it.

    Used by both the metrics and the dashboard table, so the two can never
    disagree about what happened to an event.
    """

    event_id: str
    customer_id: str
    event_type: str
    decline_code: str | None
    amount_minor: int
    currency: str
    prior_attempts: int
    first_failure_at: datetime
    received_at: datetime | None
    customer_timezone: str
    root_cause: str | None = None
    confidence: float | None = None
    reasoning: str | None = None
    classifier_unavailable: bool = False
    action: str | None = None
    channel: str | None = None
    scheduled_for: datetime | None = None
    blocked_reason: str | None = None
    guardrail_checks: list[dict[str, Any]] = field(default_factory=list)
    delivery_status: str | None = None
    customer_outcome: str | None = None
    amount_recovered_minor: int | None = None
    recovery_link_id: str | None = None
    skip_reason: str | None = None
    failure_reason: str | None = None
    requeued: bool = False
    executed_at: datetime | None = None
    decision_latency_ms: float | None = None
    send_latency_ms: float | None = None
    stages: list[str] = field(default_factory=list)

    @property
    def was_contacted(self) -> bool:
        return self.delivery_status == "sent"

    @property
    def was_deferred(self) -> bool:
        return bool(self.skip_reason and self.skip_reason.startswith("Deferred until"))

    @property
    def disposition(self) -> str:
        """Which mutually exclusive bucket this event falls in.

        Precedence is deliberate. A classifier outage is reported as itself rather
        than as the escalation it produced, because Known issue M requires an
        operational failure to stay distinguishable from a judgement. A guardrail
        stop is reported ahead of the delivery status, because "we chose not to"
        is what explains the skip that follows.
        """
        if self.action is None:
            return "not_processed"
        if self.classifier_unavailable:
            return "classifier_unavailable"
        if self.blocked_reason:
            return "withheld_by_guardrail"
        if self.delivery_status == "failed":
            return "dispatch_failed"
        if self.was_contacted:
            return "contacted"
        if self.was_deferred:
            return "deferred_to_allowed_window"
        if self.action == str(Action.SCHEDULE_RETRY):
            return "retry_scheduled"
        if self.action == str(Action.ESCALATE_TO_HUMAN_REVIEW):
            return "escalated_to_human"
        return "skipped_other"

    @property
    def is_actioned(self) -> bool:
        """Whether a recovery action was actually put in motion for this event.

        The numerator side of Known issue M's split. A scheduled retry counts: the
        recovery is under way even though nobody was messaged. A guardrail stop,
        an escalation and a classifier outage do not.
        """
        return self.disposition in {"contacted", "retry_scheduled", "dispatch_failed"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "customer_id": self.customer_id,
            "event_type": self.event_type,
            "decline_code": self.decline_code,
            "amount_minor": self.amount_minor,
            "amount": round(self.amount_minor / 100, 2),
            "currency": self.currency,
            "prior_attempts": self.prior_attempts,
            "root_cause": self.root_cause,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "classifier_unavailable": self.classifier_unavailable,
            "action": self.action,
            "channel": self.channel,
            "scheduled_for": (
                self.scheduled_for.isoformat() if self.scheduled_for else None
            ),
            "blocked_reason": self.blocked_reason,
            "guardrail_checks": self.guardrail_checks,
            "delivery_status": self.delivery_status,
            "customer_outcome": self.customer_outcome,
            "amount_recovered": (
                None
                if self.amount_recovered_minor is None
                else round(self.amount_recovered_minor / 100, 2)
            ),
            "recovery_link_id": self.recovery_link_id,
            "skip_reason": self.skip_reason,
            "failure_reason": self.failure_reason,
            "requeued": self.requeued,
            "disposition": self.disposition,
            "decision_latency_ms": self.decision_latency_ms,
            "send_latency_ms": self.send_latency_ms,
            "executed_at": self.executed_at.isoformat() if self.executed_at else None,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "stages": self.stages,
            "stage_count": len(self.stages),
        }


@dataclass(frozen=True, slots=True)
class BatchMetrics:
    """Everything the dashboard and the final write-up need."""

    events_total: int
    money: MoneyMetrics
    decision_latency: LatencyStats
    send_latency: LatencyStats
    violations: list[Violation]
    audit: AuditCoverage
    disposition: dict[str, int]
    by_root_cause: dict[str, int]
    by_action: dict[str, int]
    by_delivery_status: dict[str, int]
    by_customer_outcome: dict[str, int]
    by_event_type: dict[str, int]
    classifier_unavailable: int
    guardrail_config: dict[str, Any]

    @property
    def violation_count(self) -> int:
        return len(self.violations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "events_total": self.events_total,
            "money": self.money.to_dict(),
            "decision_latency": self.decision_latency.to_dict(),
            "send_latency": self.send_latency.to_dict(),
            "latency_budget_ms": LATENCY_BUDGET_MS,
            "violations": [v.to_dict() for v in self.violations],
            "violation_count": self.violation_count,
            "audit": self.audit.to_dict(),
            "disposition": self.disposition,
            "by_root_cause": self.by_root_cause,
            "by_action": self.by_action,
            "by_delivery_status": self.by_delivery_status,
            "by_customer_outcome": self.by_customer_outcome,
            "by_event_type": self.by_event_type,
            "classifier_unavailable": self.classifier_unavailable,
            "guardrail_config": self.guardrail_config,
        }


# --------------------------------------------------------------- row assembly


def load_rows(session: Session, *, limit: int | None = None) -> list[EventRow]:
    """Join every stage's output per event, newest first.

    One query per table rather than a single wide join: the audit stages are
    one-to-many, so joining them in would multiply every other row.
    """
    event_query = select(Event).order_by(Event.received_at.desc())
    if limit is not None:
        event_query = event_query.limit(limit)
    events = list(session.scalars(event_query))
    if not events:
        return []

    ids = [event.event_id for event in events]
    customer_ids = {event.customer_id for event in events}

    timezones = {
        customer.customer_id: customer.timezone
        for customer in session.scalars(
            select(Customer).where(Customer.customer_id.in_(customer_ids))
        )
    }
    diagnoses = {
        row.event_id: row
        for row in session.scalars(
            select(DiagnosisRecord).where(DiagnosisRecord.event_id.in_(ids))
        )
    }
    decisions = {
        row.event_id: row
        for row in session.scalars(
            select(DecisionRecord).where(DecisionRecord.event_id.in_(ids))
        )
    }
    executions = {
        row.event_id: row
        for row in session.scalars(
            select(ExecutionRecord).where(ExecutionRecord.event_id.in_(ids))
        )
    }
    latencies = {
        row.event_id: row
        for row in session.scalars(
            select(EventLatency).where(EventLatency.event_id.in_(ids))
        )
    }
    stages: dict[str, list[str]] = defaultdict(list)
    for event_id, stage in session.execute(
        select(AuditLogEntry.event_id, AuditLogEntry.stage)
        .where(AuditLogEntry.event_id.in_(ids))
        .order_by(AuditLogEntry.id)
    ):
        stages[event_id].append(stage)

    rows: list[EventRow] = []
    for event in events:
        row = EventRow(
            event_id=event.event_id,
            customer_id=event.customer_id,
            event_type=event.event_type,
            decline_code=event.decline_code,
            amount_minor=event.amount_minor,
            currency=event.currency,
            prior_attempts=event.prior_attempts,
            first_failure_at=event.first_failure_at,
            received_at=event.received_at,
            customer_timezone=timezones.get(event.customer_id, DEFAULT_TIMEZONE),
            stages=stages.get(event.event_id, []),
        )
        if (diagnosis := diagnoses.get(event.event_id)) is not None:
            row.root_cause = diagnosis.root_cause
            row.confidence = diagnosis.confidence
            row.reasoning = diagnosis.reasoning
            row.classifier_unavailable = diagnosis.classifier_unavailable
        if (decision := decisions.get(event.event_id)) is not None:
            row.action = decision.action
            row.channel = decision.channel
            row.scheduled_for = decision.scheduled_for
            row.blocked_reason = decision.blocked_reason
            row.guardrail_checks = decision.guardrail_checks or []
        if (execution := executions.get(event.event_id)) is not None:
            row.delivery_status = execution.delivery_status
            row.customer_outcome = execution.customer_outcome
            row.amount_recovered_minor = execution.amount_recovered_minor
            row.recovery_link_id = execution.recovery_link_id
            row.skip_reason = execution.skip_reason
            row.failure_reason = execution.failure_reason
            row.requeued = execution.requeued
            row.executed_at = execution.executed_at
        if (latency := latencies.get(event.event_id)) is not None:
            row.decision_latency_ms = latency.decision_latency_ms
            row.send_latency_ms = latency.send_latency_ms
        rows.append(row)
    return rows


# --------------------------------------------- independent rule re-derivation


def resolve_zone(name: str) -> ZoneInfo:
    """Customer timezone, falling back to IST rather than raising.

    A metrics run must not die because one customer row holds a bad zone. The
    fallback matches what ``guardrails.py`` does when it cannot resolve one.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def find_violations(rows: list[EventRow]) -> list[Violation]:
    """Reconstruct constraint #4 from raw data and test what actually happened.

    Nothing here consults the pass/fail flags the guardrails recorded, except the
    final check, which is about those flags being present at all (constraint #5).
    """
    settings = get_settings()
    violations: list[Violation] = []

    contacted = [
        row for row in rows if row.was_contacted and row.executed_at is not None
    ]

    for row in contacted:
        sent_at = row.executed_at
        assert sent_at is not None  # noqa: S101 - filtered above

        # Rule: no contact outside 9am-8pm CUSTOMER LOCAL time. Checking this in
        # UTC would wave through a message sent at 3am in Kolkata, which is the
        # entire point of the rule, so the send time is converted first.
        local = sent_at.astimezone(resolve_zone(row.customer_timezone))
        start = settings.quiet_hours_start_local
        end = settings.quiet_hours_end_local
        if not start <= local.hour < end:
            violations.append(
                Violation(
                    event_id=row.event_id,
                    rule=str(GuardrailName.QUIET_HOURS),
                    detail=(
                        f"contacted at {local.strftime('%H:%M')} "
                        f"{row.customer_timezone}, outside "
                        f"{start:02d}:00-{end:02d}:00 local"
                    ),
                )
            )

        # Rule: hard stop 7 days after the first failure.
        elapsed = sent_at - row.first_failure_at
        if elapsed >= timedelta(days=settings.hard_stop_days):
            violations.append(
                Violation(
                    event_id=row.event_id,
                    rule=str(GuardrailName.HARD_STOP_7_DAYS),
                    detail=(
                        f"contacted {elapsed.total_seconds() / 86400:.1f} days "
                        f"after first failure, past the "
                        f"{settings.hard_stop_days}-day stop"
                    ),
                )
            )

        # Rule: max 3 recovery attempts per event.
        if row.prior_attempts >= settings.max_recovery_attempts:
            violations.append(
                Violation(
                    event_id=row.event_id,
                    rule=str(GuardrailName.MAX_RETRIES),
                    detail=(
                        f"contacted with {row.prior_attempts} prior attempts "
                        f"against a limit of {settings.max_recovery_attempts}"
                    ),
                )
            )

    # Rule: max 1 contact per 24 hours per customer. Cross-event by nature, which
    # is exactly why re-deriving it is worth doing: a per-event check cannot see
    # a second message to the same person.
    window = timedelta(hours=settings.min_hours_between_contacts)
    by_customer: dict[str, list[EventRow]] = defaultdict(list)
    for row in contacted:
        by_customer[row.customer_id].append(row)
    for customer_id, customer_rows in by_customer.items():
        ordered = sorted(customer_rows, key=lambda r: r.executed_at)  # type: ignore[arg-type]
        for previous, current in zip(ordered, ordered[1:], strict=False):
            assert previous.executed_at and current.executed_at  # noqa: S101
            gap = current.executed_at - previous.executed_at
            if gap < window:
                violations.append(
                    Violation(
                        event_id=current.event_id,
                        rule=str(GuardrailName.CONTACT_FREQUENCY),
                        detail=(
                            f"customer {customer_id} contacted again after "
                            f"{gap.total_seconds() / 3600:.1f}h, inside the "
                            f"{settings.min_hours_between_contacts}h window "
                            f"(previous event {previous.event_id})"
                        ),
                    )
                )

    for row in rows:
        # A guardrail cancelled the action, yet something went out anyway.
        if row.blocked_reason and row.was_contacted:
            violations.append(
                Violation(
                    event_id=row.event_id,
                    rule="blocked_but_sent",
                    detail=f"sent despite blocked_reason: {row.blocked_reason[:160]}",
                )
            )

        # A deferral is only worth something if the send actually waited. This is
        # the end-to-end form of the bug fixed in session 14, where DECIDE
        # deferred correctly and EXECUTE dispatched anyway.
        if (
            row.was_contacted
            and row.action in CONTACT_ACTIONS
            and row.scheduled_for is not None
            and row.executed_at is not None
            and row.executed_at < row.scheduled_for
        ):
            violations.append(
                Violation(
                    event_id=row.event_id,
                    rule="sent_before_due",
                    detail=(
                        f"sent at {row.executed_at.isoformat()} but was "
                        f"scheduled for {row.scheduled_for.isoformat()}"
                    ),
                )
            )

        # Constraint #5: every check result recorded, pass or fail.
        if row.action is not None:
            recorded = {check.get("name") for check in row.guardrail_checks}
            missing = sorted({str(name) for name in GuardrailName} - recorded)
            if missing:
                violations.append(
                    Violation(
                        event_id=row.event_id,
                        rule="guardrail_checks_incomplete",
                        detail=f"no recorded result for {missing}",
                    )
                )

    return violations


def audit_coverage(rows: list[EventRow]) -> AuditCoverage:
    """How many events carry all four stages in the trail."""
    incomplete: list[dict[str, Any]] = []
    covered = 0
    for row in rows:
        present = set(row.stages)
        missing = [stage for stage in REQUIRED_STAGES if stage not in present]
        if missing:
            incomplete.append(
                {"event_id": row.event_id, "missing": missing, "present": row.stages}
            )
        else:
            covered += 1
    return AuditCoverage(
        events=len(rows), fully_covered=covered, incomplete=incomplete
    )


def _counts(values: list[str | None], *, missing: str = "not_processed") -> dict[str, int]:
    counter = Counter(value if value is not None else missing for value in values)
    return dict(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))


def compute_batch_metrics(
    session: Session, *, limit: int | None = None
) -> tuple[BatchMetrics, list[EventRow]]:
    """Compute every headline metric, and return the rows they came from.

    The rows come back too so a caller (the dashboard, the report) renders exactly
    the data the numbers were computed from, rather than issuing its own query
    that could drift.
    """
    rows = load_rows(session, limit=limit)

    at_risk = sum(row.amount_minor for row in rows)
    recovered = sum(row.amount_recovered_minor or 0 for row in rows)
    actioned_at_risk = sum(row.amount_minor for row in rows if row.is_actioned)

    metrics = BatchMetrics(
        events_total=len(rows),
        money=MoneyMetrics(
            at_risk_minor=at_risk,
            recovered_minor=recovered,
            actioned_at_risk_minor=actioned_at_risk,
            currency=rows[0].currency if rows else "INR",
        ),
        decision_latency=LatencyStats.of(
            [r.decision_latency_ms for r in rows if r.decision_latency_ms is not None]
        ),
        send_latency=LatencyStats.of(
            [r.send_latency_ms for r in rows if r.send_latency_ms is not None]
        ),
        violations=find_violations(rows),
        audit=audit_coverage(rows),
        disposition=_counts([row.disposition for row in rows]),
        by_root_cause=_counts([row.root_cause for row in rows], missing="not_diagnosed"),
        by_action=_counts([row.action for row in rows], missing="not_decided"),
        by_delivery_status=_counts(
            [row.delivery_status for row in rows], missing="not_executed"
        ),
        by_customer_outcome=_counts(
            [row.customer_outcome for row in rows], missing="not_executed"
        ),
        by_event_type=_counts([row.event_type for row in rows]),
        classifier_unavailable=sum(1 for row in rows if row.classifier_unavailable),
        guardrail_config=get_settings().guardrail_config(),
    )
    return metrics, rows


# ------------------------------------------------------------------- reporting


def format_report(metrics: BatchMetrics) -> str:
    """Plain-text report for a terminal or a write-up."""
    money = metrics.money
    lines: list[str] = [
        "AI Revenue Recovery Agent - batch metrics",
        "=" * 62,
        f"Events processed          {metrics.events_total}",
        "",
        "1. MONEY",
        f"   at risk                {money.currency} {money.at_risk_minor / 100:,.2f}",
        f"   recovered              {money.currency} {money.recovered_minor / 100:,.2f}",
        f"   recovery rate          {_pct(money.recovery_rate)} of everything at risk",
        f"   actioned recovery rate {_pct(money.actioned_recovery_rate)} of the "
        f"{money.currency} {money.actioned_at_risk_minor / 100:,.2f} actioned",
        "",
        "2. LATENCY (two figures, per Known issue A)",
        f"   decision  mean {_ms(metrics.decision_latency.mean_ms)}  "
        f"p95 {_ms(metrics.decision_latency.p95_ms)}  "
        f"max {_ms(metrics.decision_latency.max_ms)}  "
        f"over 60s: {metrics.decision_latency.over_budget}",
        f"   send      mean {_ms(metrics.send_latency.mean_ms)}  "
        f"p95 {_ms(metrics.send_latency.p95_ms)}  "
        f"max {_ms(metrics.send_latency.max_ms)}  "
        f"over 60s: {metrics.send_latency.over_budget}",
        "",
        "3. STOPPING-RULE VIOLATIONS (re-derived, not read back)",
        f"   {metrics.violation_count}",
    ]
    for violation in metrics.violations[:20]:
        lines.append(f"     - [{violation.rule}] {violation.event_id}: {violation.detail}")
    if metrics.violation_count > 20:
        lines.append(f"     ... and {metrics.violation_count - 20} more")

    lines += [
        "",
        "4. AUDIT COVERAGE",
        f"   {metrics.audit.fully_covered}/{metrics.audit.events} events have all "
        f"four stages ({_pct(metrics.audit.rate)})",
    ]
    for gap in metrics.audit.incomplete[:10]:
        lines.append(f"     - {gap['event_id']} missing {gap['missing']}")

    lines += ["", "DISPOSITION (mutually exclusive, per Known issue M)"]
    for name, count in metrics.disposition.items():
        lines.append(f"   {name:30} {count}")
    lines += ["", "ROOT CAUSE"]
    for name, count in metrics.by_root_cause.items():
        lines.append(f"   {name:30} {count}")
    lines += ["", "ACTION"]
    for name, count in metrics.by_action.items():
        lines.append(f"   {name:30} {count}")

    if metrics.classifier_unavailable:
        lines += [
            "",
            f"NOTE: {metrics.classifier_unavailable} event(s) had no classifier "
            "available. That is an operational failure, not a diagnosis, and it is "
            "excluded from the actioned denominator.",
        ]
    return "\n".join(lines)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.0f}ms"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.metrics",
        description="Compute Phase 6 batch metrics from the database.",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Only the most recent N events."
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of a text report."
    )
    parser.add_argument("--out", type=str, default=None, help="Write JSON here too.")
    parser.add_argument(
        "--fail-on-violation",
        action="store_true",
        help="Exit non-zero if any stopping rule was breached. For CI use.",
    )
    args = parser.parse_args(argv)

    from app.db import session_scope

    with session_scope() as session:
        metrics, _ = compute_batch_metrics(session, limit=args.limit)

    payload = metrics.to_dict()
    print(json.dumps(payload, indent=2) if args.json else format_report(metrics))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        print(f"\nJSON report -> {args.out}", file=sys.stderr)

    if args.fail_on_violation and metrics.violation_count:
        print(
            f"\n{metrics.violation_count} stopping-rule violation(s)", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
