"""DECIDE — stage 3. Deterministic rules engine.

Contract:
    (EventRecord, Diagnosis, DecisionContext) -> Decision

**No LLM call in this stage.** It is a lookup table plus guardrail checks, so the
mapping stays auditable and bounded. If this ever feels like it needs a model
call, re-read ``architecture.md``'s action-set table instead.

The reason is asymmetry of cost. DIAGNOSE emits a label, and a wrong label
degrades to human review. DECIDE emits an *action*: money movement and customer
contact. Phase 3 measured the classifier returning a confident wrong answer at
0.90 and, on other models, inventing an enum value outright. Every one of those
is survivable when the output is a label and none is survivable when the output is
an action.

## Two kinds of "no"

Treating every guardrail failure as an abandonment would discard recoverable
revenue. Quiet hours does not mean "never contact this person", it means "not at
3am", so the right answer is a later ``scheduled_for``. See
``guardrails.GuardrailKind``.

  terminal failure   -> ``blocked_reason`` set, nothing will be sent
  deferrable failure -> ``scheduled_for`` moved to the next allowed moment

## Which guardrails bear on which action

Not every rule governs every action. ``quiet_hours`` and ``contact_frequency``
exist to protect a human from being disturbed, so they have no bearing on a
silent provider-side retry or an internal escalation. ``max_retries`` and the
7-day ``hard_stop`` govern anything that contacts the customer or attempts a
charge, but not an internal handoff — escalating to a person is a safe, useful
thing to do precisely *because* the automated options are exhausted.

All four checks still RUN and are still RECORDED for every event, because
constraint #5 requires it. Applicability decides whether a failure changes the
outcome, not whether the check happens.

## The signal EXECUTE must key on

``blocked_reason is not None`` means do nothing. ``channel`` cannot be used for
this, because ``escalate_to_human_review`` legitimately has ``channel: none``
while still being an action that should happen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app import guardrails
from app.config import get_settings
from app.schemas import (
    Action,
    Channel,
    Decision,
    Diagnosis,
    EventRecord,
    GuardrailCheck,
    RootCause,
)


@dataclass(frozen=True, slots=True)
class ActionPlan:
    """One row of ``architecture.md``'s fixed action set.

    ``architecture.md`` writes some entries with their parameters inline —
    ``schedule_retry(+N days)``, ``send_reminder(1x), then stop``. Those
    parentheticals are arguments, not identifiers, so they live here as
    ``delay`` and ``max_repeats`` rather than being baked into enum names. All 8
    rows survive as 8 mappings; see the decisions log in progress-tracker.md.
    """

    action: Action
    channel: Channel
    delay: timedelta
    max_repeats: int
    note: str


def _retry_delay_days() -> timedelta:
    """Delay for an insufficient-funds retry.

    ``architecture.md`` says "payday-aware if data available". We hold no payday
    data, so this is a flat interval and payday awareness stays a Phase 8 stretch
    goal rather than being faked with a guess about pay cycles.
    """
    return timedelta(days=get_settings().insufficient_funds_retry_days)


# Copied from architecture.md's "Fixed action set". DECIDE may only choose from
# this table. Adding a row requires updating architecture.md FIRST
# (ai-workflow-rules.md -> Scope discipline); a contract test enforces that the
# table and the doc agree.
def action_table() -> dict[RootCause, ActionPlan]:
    return {
        RootCause.CARD_EXPIRED: ActionPlan(
            action=Action.SEND_UPDATE_PAYMENT_METHOD_LINK,
            channel=Channel.WHATSAPP,
            delay=timedelta(0),
            max_repeats=1,
            note="No retry attempt: a retry cannot succeed on an expired card.",
        ),
        RootCause.INSUFFICIENT_FUNDS: ActionPlan(
            action=Action.SCHEDULE_RETRY,
            channel=Channel.NONE,
            delay=_retry_delay_days(),
            max_repeats=get_settings().max_recovery_attempts,
            note=(
                "Provider-sanctioned retry after a delay. The one cause where "
                "waiting genuinely helps. Payday-aware timing is a stretch goal."
            ),
        ),
        RootCause.BANK_RISK_BLOCK: ActionPlan(
            action=Action.ESCALATE_TO_HUMAN_REVIEW,
            channel=Channel.NONE,
            delay=timedelta(0),
            max_repeats=1,
            note="Never auto-retry the same instrument after a risk refusal.",
        ),
        RootCause.SCA_ABANDONED: ActionPlan(
            action=Action.SEND_FRESH_AUTH_LINK,
            channel=Channel.WHATSAPP,
            delay=timedelta(0),
            max_repeats=1,
            note=(
                "The customer completes 3DS themselves. We never complete "
                "authentication on their behalf (constraint #2)."
            ),
        ),
        RootCause.NETWORK_ERROR: ActionPlan(
            action=Action.SCHEDULE_RETRY,
            channel=Channel.NONE,
            delay=timedelta(hours=1),
            max_repeats=1,
            note="Single quiet retry, then stop.",
        ),
        RootCause.CHECKOUT_FRICTION: ActionPlan(
            action=Action.SEND_REMINDER,
            channel=Channel.WHATSAPP,
            delay=timedelta(0),
            max_repeats=1,
            note="One reminder; no repeat unless the customer re-engages.",
        ),
        RootCause.GENUINE_ABANDONMENT: ActionPlan(
            action=Action.SEND_REMINDER,
            channel=Channel.WHATSAPP,
            delay=timedelta(0),
            max_repeats=1,
            note="One reminder, then stop. Do not chase further.",
        ),
        RootCause.UNKNOWN: ActionPlan(
            action=Action.ESCALATE_TO_HUMAN_REVIEW,
            channel=Channel.NONE,
            delay=timedelta(0),
            max_repeats=1,
            note="Never guess an action.",
        ),
    }


# Actions that put a message in front of a person.
CONTACTS_CUSTOMER = frozenset(
    {
        Action.SEND_UPDATE_PAYMENT_METHOD_LINK,
        Action.SEND_FRESH_AUTH_LINK,
        Action.SEND_REMINDER,
    }
)
# Actions that ask the provider to move money.
ATTEMPTS_CHARGE = frozenset({Action.SCHEDULE_RETRY})
# Actions with no external effect at all.
INTERNAL_ONLY = frozenset({Action.ESCALATE_TO_HUMAN_REVIEW})


@dataclass(frozen=True, slots=True)
class DecisionContext:
    """State DECIDE needs that ``EventRecord`` has no field for.

    ``architecture.md``'s Event record carries neither a timezone nor a
    last-contacted timestamp, and both are required by constraint #4. They live
    on the ``customers`` table (see Phase 2b) and are passed alongside rather than
    bolted onto ``EventRecord``, which stays a verbatim copy of the doc.
    """

    customer_timezone: str
    first_failure_at: datetime
    last_contact_at: datetime | None = None
    now: datetime | None = None

    def evaluated_at(self) -> datetime:
        return self.now or datetime.now(UTC)


def applies_to_action(check: GuardrailCheck, action: Action) -> bool:
    """Whether failing ``check`` should change what we do about ``action``.

    Deliberately narrow. Applying quiet hours to a silent provider retry would
    delay recovery for no compliance benefit, since nobody is disturbed by a
    charge attempt at 3am. Applying max_retries to an internal escalation would
    suppress the handoff exactly when it is most warranted.
    """
    from app.schemas import GuardrailName

    if check.name in {GuardrailName.QUIET_HOURS, GuardrailName.CONTACT_FREQUENCY}:
        return action in CONTACTS_CUSTOMER
    # max_retries and hard_stop: everything with an external effect.
    return action in CONTACTS_CUSTOMER | ATTEMPTS_CHARGE


def decide_action(
    event: EventRecord, diagnosis: Diagnosis, context: DecisionContext
) -> Decision:
    """Map a diagnosis to exactly one pre-approved action, after guardrail checks.

    Pure and deterministic: same inputs give the same Decision, which is what
    makes a metrics run reproducible and a violation count meaningful. No network
    calls, no clock reads unless ``context.now`` is omitted, no LLM.
    """
    now = context.evaluated_at()
    plan = action_table()[diagnosis.root_cause]

    checks = guardrails.run_all_checks(
        event,
        customer_timezone=context.customer_timezone,
        last_contact_at=context.last_contact_at,
        first_failure_at=context.first_failure_at,
        now=now,
    )

    relevant_failures = [
        c for c in checks if not c.passed and applies_to_action(c, plan.action)
    ]
    terminal = [
        c
        for c in relevant_failures
        if guardrails.KIND[c.name] is guardrails.GuardrailKind.TERMINAL
    ]
    deferrable = [
        c
        for c in relevant_failures
        if guardrails.KIND[c.name] is guardrails.GuardrailKind.DEFERRABLE
    ]

    # Action-level repeat limit, from the action table rather than constraint #4:
    # "single quiet retry, then stop" and "send_reminder(1x)". Kept separate from
    # the four guardrails because it is a property of the chosen action, not a
    # global stopping rule, and GuardrailName must keep matching the doc.
    over_repeat_limit = (
        plan.action not in INTERNAL_ONLY and event.prior_attempts >= plan.max_repeats
    )

    if terminal:
        return Decision(
            event_id=event.event_id,
            action=plan.action,
            channel=Channel.NONE,
            scheduled_for=None,
            guardrail_checks=checks,
            blocked_reason=(
                "Stopped by "
                + ", ".join(str(c.name) for c in terminal)
                + ". "
                + " ".join(c.detail for c in terminal)
            ),
            delay_seconds=None,
            max_repeats=plan.max_repeats,
        )

    if over_repeat_limit:
        return Decision(
            event_id=event.event_id,
            action=plan.action,
            channel=Channel.NONE,
            scheduled_for=None,
            guardrail_checks=checks,
            blocked_reason=(
                f"Action limit reached: {plan.action} permits {plan.max_repeats} "
                f"attempt(s) for root cause {diagnosis.root_cause} and "
                f"{event.prior_attempts} have been made. {plan.note}"
            ),
            delay_seconds=None,
            max_repeats=plan.max_repeats,
        )

    if deferrable:
        scheduled_for = guardrails.next_allowed_contact_time(
            customer_timezone=context.customer_timezone,
            last_contact_at=context.last_contact_at,
            now=now,
        )
    else:
        scheduled_for = now + plan.delay

    return Decision(
        event_id=event.event_id,
        action=plan.action,
        channel=plan.channel,
        scheduled_for=scheduled_for,
        guardrail_checks=checks,
        blocked_reason=None,
        delay_seconds=int((scheduled_for - now).total_seconds()),
        max_repeats=plan.max_repeats,
    )


def audit_summaries(
    event: EventRecord, diagnosis: Diagnosis, decision: Decision
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Input and output summaries for the DECIDE audit entry.

    ``guardrail_checks`` carries every check with its pass/fail and reason, not
    just the ones that mattered — constraint #5 requires the trail to show the
    check happened, not only its consequence.
    """
    return (
        {
            "root_cause": str(diagnosis.root_cause),
            "confidence": round(diagnosis.confidence, 4),
            "prior_attempts": event.prior_attempts,
            "event_type": str(event.event_type),
        },
        {
            "action": str(decision.action),
            "channel": str(decision.channel),
            "scheduled_for": (
                decision.scheduled_for.isoformat() if decision.scheduled_for else None
            ),
            "delay_seconds": decision.delay_seconds,
            "max_repeats": decision.max_repeats,
            "blocked": decision.blocked_reason is not None,
            "blocked_reason": decision.blocked_reason,
            "guardrail_checks_passed": [str(n) for n in decision.guardrail_checks_passed],
        },
    )


def guardrail_check_rows(decision: Decision) -> list[dict[str, Any]]:
    """Every guardrail result, for the audit entry's dedicated column."""
    return [
        {
            "name": str(c.name),
            "passed": c.passed,
            "detail": c.detail,
            "kind": str(guardrails.KIND[c.name]),
            "applied_to_action": applies_to_action(c, decision.action),
        }
        for c in decision.guardrail_checks
    ]
