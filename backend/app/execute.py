"""EXECUTE — stage 4. Action dispatch.

Contract:
    (Decision, ExecutionContext) -> ExecutionResult

The only stage permitted to touch a payment-provider API, and even then it never
resubmits a card.

## How each action stays inside the constraints

  ``send_update_payment_method_link``  Razorpay-hosted link; the customer enters
                                       new card details on Razorpay's page.
  ``send_fresh_auth_link``             Razorpay-hosted link; the customer
                                       completes 3DS themselves (#2).
  ``send_reminder``                    Razorpay-hosted link for the outstanding
                                       balance.
  ``schedule_retry``                   Records a due time. No charge is submitted
                                       from here (#3, #6).
  ``escalate_to_human_review``         Queues for a person. No external call.

No branch of this module submits a payment. The most it ever does is create a
hosted link and send its URL, which is a *fresh customer-initiated action* — the
handoff constraint #6 requires.

## Why schedule_retry does not call the provider

Constraint #3 permits a retry only when it is provider-sanctioned or preceded by
fresh customer action. Firing a charge from here would be neither: it would be
this agent re-submitting a transaction, which constraint #6 explicitly forbids.
So the decision and its due time are recorded, and handing them to Razorpay's own
retry mechanism is left as an integration a human enables deliberately. Recording
a retry we did not make is honest; making one we are not permitted to make is not.

## There are two stop gates, not one

``blocked_reason`` is a terminal stop: a guardrail cancelled the action. But
quiet hours and contact frequency are DEFERRABLE — DECIDE leaves
``blocked_reason`` empty and sets ``scheduled_for`` to the next permitted moment
instead. Keying only on ``blocked_reason`` would dispatch those immediately and
message the customer inside the window the rule exists to protect, so a contact
action whose ``scheduled_for`` is still in the future is held (see
:func:`is_deferred`). Constraint #4 holds end to end only with both gates.

A deferral is a skip with a due time, not a failure. The due time lives on the
decision record, which is what a due-work scan reads.

## Nothing is ever silently dropped

``code-standards.md`` requires a failed dispatch to reach a retry queue or an
escalation, logged either way. Every path here returns an ExecutionResult with a
delivery status and a stated reason. A send that is refused is ``skipped`` with a
``skip_reason``; a send that breaks is ``failed`` with a ``failure_reason`` and
``requeued`` set.

## The outcome is never assumed

``customer_outcome`` starts as ``pending`` and ``amount_recovered`` stays null
until a provider webhook confirms payment. Defaulting recovery to the amount at
risk would invent revenue, and revenue is the one number here that must never be
optimistic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app import channels
from app.channels import MessageSender, PaymentLink, PaymentLinkFactory
from app.schemas import (
    Action,
    Channel,
    CustomerOutcome,
    Decision,
    DeliveryStatus,
    ExecutionResult,
)

logger = logging.getLogger(__name__)

# Actions that put a hosted payment link in front of the customer.
LINK_ACTIONS = frozenset(
    {
        Action.SEND_UPDATE_PAYMENT_METHOD_LINK,
        Action.SEND_FRESH_AUTH_LINK,
        Action.SEND_REMINDER,
    }
)

# Deliberately plain text. ui-context.md scopes customer-facing surfaces to
# provider-hosted pages and plain messages, so no templating engine is involved.
MESSAGE_TEMPLATES: dict[Action, str] = {
    Action.SEND_UPDATE_PAYMENT_METHOD_LINK: (
        "Hi {name}, your payment of {amount} could not be completed because the "
        "card on file has expired. You can update your payment details here: "
        "{link}"
    ),
    Action.SEND_FRESH_AUTH_LINK: (
        "Hi {name}, your payment of {amount} was not completed because the bank "
        "verification step was not finished. You can complete it securely here: "
        "{link}"
    ),
    Action.SEND_REMINDER: (
        "Hi {name}, your payment of {amount} is still outstanding. You can "
        "complete it here whenever you are ready: {link}"
    ),
}


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Everything EXECUTE needs that ``Decision`` does not carry.

    ``Decision`` deliberately holds no customer details — it is a routing record.
    Contact details, the amount and the name live here.
    """

    customer_id: str
    amount_at_risk_minor: int
    customer_name: str | None = None
    email: str | None = None
    contact: str | None = None
    now: datetime | None = None

    def executed_at(self) -> datetime:
        return self.now or datetime.now(UTC)

    def display_name(self) -> str:
        return (self.customer_name or "there").split(" ")[0]

    def display_amount(self) -> str:
        return f"INR {Decimal(self.amount_at_risk_minor) / 100:,.2f}"


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """The ExecutionResult plus the operational detail worth persisting.

    ``ExecutionResult`` matches ``architecture.md``'s schema exactly and has no
    field for a link id or a skip reason, so those travel alongside rather than
    being bolted onto the documented shape.
    """

    result: ExecutionResult
    action: Action
    channel: Channel
    recovery_link: PaymentLink | None = None
    provider_message_id: str | None = None
    skip_reason: str | None = None
    failure_reason: str | None = None
    requeued: bool = False
    message_body: str | None = None
    scheduled_for: datetime | None = None


def _result(
    decision: Decision,
    executed_at: datetime,
    delivery: DeliveryStatus,
    outcome: CustomerOutcome = CustomerOutcome.PENDING,
) -> ExecutionResult:
    return ExecutionResult(
        event_id=decision.event_id,
        executed_at=executed_at,
        delivery_status=delivery,
        customer_outcome=outcome,
        # Never pre-filled. Only a provider webhook may set this.
        amount_recovered=None,
    )


def is_deferred(decision: Decision, at: datetime) -> bool:
    """Whether a contact action is not yet due.

    Only meaningful for the actions that contact the customer. ``schedule_retry``
    carries a due time by design and is recorded rather than dispatched, and
    ``escalate_to_human_review`` should reach the queue immediately.
    """
    return (
        decision.action in LINK_ACTIONS
        and decision.scheduled_for is not None
        and decision.scheduled_for > at
    )


def render_message(action: Action, context: ExecutionContext, link: PaymentLink) -> str:
    template = MESSAGE_TEMPLATES[action]
    return template.format(
        name=context.display_name(),
        amount=context.display_amount(),
        link=link.url,
    )


def execute_action(
    decision: Decision,
    context: ExecutionContext,
    *,
    link_factory: PaymentLinkFactory | None = None,
    sender: MessageSender | None = None,
) -> ExecutionOutcome:
    """Dispatch the decided action and record delivery status.

    Never raises on a dispatch problem. Every path returns an outcome with a
    stated reason, because a dropped event is lost revenue with no trace.

    ``link_factory`` and ``sender`` are injectable so tests exercise every action
    without a network call or a real message.
    """
    executed_at = context.executed_at()

    # A guardrail already stopped this. Recording it as skipped, with the reason,
    # is what makes "0 violations" auditable rather than merely asserted.
    if decision.blocked_reason is not None:
        return ExecutionOutcome(
            result=_result(decision, executed_at, DeliveryStatus.SKIPPED),
            action=decision.action,
            channel=Channel.NONE,
            skip_reason=f"Blocked by guardrail: {decision.blocked_reason}",
        )

    if decision.action is Action.ESCALATE_TO_HUMAN_REVIEW:
        # No external call. Queued for a person, which is the action itself.
        return ExecutionOutcome(
            result=_result(decision, executed_at, DeliveryStatus.SKIPPED),
            action=decision.action,
            channel=Channel.NONE,
            skip_reason=(
                "Queued for human review; no automated contact or charge is "
                "appropriate for this cause."
            ),
        )

    if decision.action is Action.SCHEDULE_RETRY:
        # Recorded, not fired. Submitting a charge from here would be this agent
        # re-submitting a transaction, which constraint #6 forbids.
        return ExecutionOutcome(
            result=_result(decision, executed_at, DeliveryStatus.SKIPPED),
            action=decision.action,
            channel=Channel.NONE,
            scheduled_for=decision.scheduled_for,
            skip_reason=(
                "Retry recorded as due at "
                f"{decision.scheduled_for.isoformat() if decision.scheduled_for else 'unset'}"
                ". No charge submitted from here: a retry must be "
                "provider-sanctioned or customer-initiated (constraints #3, #6)."
            ),
        )

    if decision.action not in LINK_ACTIONS:
        # Unreachable while Action and LINK_ACTIONS agree, but an unhandled action
        # must escalate rather than fall through as a silent success.
        return ExecutionOutcome(
            result=_result(decision, executed_at, DeliveryStatus.FAILED),
            action=decision.action,
            channel=decision.channel,
            failure_reason=f"No dispatch path for action {decision.action}",
            requeued=True,
        )

    if is_deferred(decision, executed_at):
        # A deferrable guardrail did not stop this action, it MOVED it. Quiet hours
        # and contact frequency both fail this way: DECIDE leaves blocked_reason
        # empty and sets scheduled_for to the next permitted moment. Dispatching
        # now would put a message in front of the customer inside the exact window
        # those rules exist to protect, so honouring blocked_reason alone is not
        # enough to satisfy constraint #4 end to end.
        #
        # No link is created yet. A hosted link minted hours early is wasted work,
        # and the due time is already on the decision record for a scanner to pick
        # up when it comes round.
        assert decision.scheduled_for is not None  # noqa: S101 - implied by is_deferred
        return ExecutionOutcome(
            result=_result(decision, executed_at, DeliveryStatus.SKIPPED),
            action=decision.action,
            channel=decision.channel,
            scheduled_for=decision.scheduled_for,
            skip_reason=(
                "Deferred until "
                f"{decision.scheduled_for.isoformat()}: a stopping rule moved this "
                "contact rather than cancelling it, and sending now would breach "
                "it (constraint #4). Nothing was created or sent."
            ),
        )

    if not context.contact:
        return ExecutionOutcome(
            result=_result(decision, executed_at, DeliveryStatus.SKIPPED),
            action=decision.action,
            channel=decision.channel,
            skip_reason="No contact number on record for this customer.",
        )

    if context.amount_at_risk_minor <= 0:
        return ExecutionOutcome(
            result=_result(decision, executed_at, DeliveryStatus.SKIPPED),
            action=decision.action,
            channel=decision.channel,
            skip_reason=(
                f"Amount at risk is {context.amount_at_risk_minor}; nothing to "
                "recover, so no link was created."
            ),
        )

    factory = link_factory or channels.build_payment_link_factory()
    active_sender = sender or channels.build_message_sender()

    try:
        link = factory.create(
            amount_minor=context.amount_at_risk_minor,
            customer_id=context.customer_id,
            email=context.email,
            contact=context.contact,
            description=f"Recovery for {decision.event_id}",
        )
    except Exception as exc:  # noqa: BLE001 - must degrade, never crash the batch
        logger.warning("payment link creation failed for %s: %s", decision.event_id, exc)
        return ExecutionOutcome(
            result=_result(decision, executed_at, DeliveryStatus.FAILED),
            action=decision.action,
            channel=decision.channel,
            failure_reason=f"Payment link creation failed: {type(exc).__name__}: {exc}",
            requeued=True,
        )

    body = render_message(decision.action, context, link)
    try:
        send = active_sender.send(to=context.contact, body=body)
    except Exception as exc:  # noqa: BLE001 - must requeue, never crash the batch
        # The senders in ``channels`` catch their own transport errors, but this
        # module's contract is that no dispatch problem escapes. A sender that
        # raises would otherwise abort the whole batch and lose every event after
        # it, with the link already created and no record of why.
        logger.warning("message send raised for %s: %s", decision.event_id, exc)
        return ExecutionOutcome(
            result=_result(decision, executed_at, DeliveryStatus.FAILED),
            action=decision.action,
            channel=decision.channel,
            recovery_link=link,
            message_body=body,
            failure_reason=f"Message send raised: {type(exc).__name__}: {exc}",
            requeued=True,
        )

    if send.delivered:
        return ExecutionOutcome(
            result=_result(decision, executed_at, DeliveryStatus.SENT),
            action=decision.action,
            channel=decision.channel,
            recovery_link=link,
            provider_message_id=send.provider_message_id,
            message_body=body,
        )

    if send.dry_run:
        # Honest about doing nothing. Not counted as sent, so no delivery metric
        # is inflated by a run with no messaging credentials.
        return ExecutionOutcome(
            result=_result(decision, executed_at, DeliveryStatus.SKIPPED),
            action=decision.action,
            channel=decision.channel,
            recovery_link=link,
            message_body=body,
            skip_reason=(
                "Dry run: messaging is not configured, so the link was created "
                "but nothing was sent."
            ),
        )

    # A refusal (not allowlisted, unusable number) is a skip; a genuine transport
    # failure is requeued. Both are recorded either way.
    refused = bool(send.error and "not in TWILIO_WHATSAPP_TEST_RECIPIENTS" in send.error)
    if refused or (send.error and "unusable recipient" in send.error):
        return ExecutionOutcome(
            result=_result(decision, executed_at, DeliveryStatus.SKIPPED),
            action=decision.action,
            channel=decision.channel,
            recovery_link=link,
            message_body=body,
            skip_reason=send.error,
        )

    return ExecutionOutcome(
        result=_result(decision, executed_at, DeliveryStatus.FAILED),
        action=decision.action,
        channel=decision.channel,
        recovery_link=link,
        message_body=body,
        failure_reason=send.error or "message send failed for an unstated reason",
        requeued=True,
    )


def audit_summaries(
    decision: Decision, outcome: ExecutionOutcome, context: ExecutionContext
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Input and output summaries for the EXECUTE audit entry.

    The message body is NOT recorded: it contains the customer's name and a link
    that grants access to a payment page, and the audit trail is readable by
    anyone with access to the ops endpoints.
    """
    return (
        {
            "action": str(decision.action),
            "channel": str(decision.channel),
            "amount_at_risk_minor": context.amount_at_risk_minor,
            "has_contact": bool(context.contact),
            "scheduled_for": (
                decision.scheduled_for.isoformat() if decision.scheduled_for else None
            ),
        },
        {
            "delivery_status": str(outcome.result.delivery_status),
            "customer_outcome": str(outcome.result.customer_outcome),
            "recovery_link_id": (
                outcome.recovery_link.link_id if outcome.recovery_link else None
            ),
            "recovery_link_is_dry_run": (
                outcome.recovery_link.dry_run if outcome.recovery_link else None
            ),
            "provider_message_id": outcome.provider_message_id,
            "skip_reason": outcome.skip_reason,
            "failure_reason": outcome.failure_reason,
            "requeued": outcome.requeued,
            "amount_recovered_minor": None,
        },
    )
