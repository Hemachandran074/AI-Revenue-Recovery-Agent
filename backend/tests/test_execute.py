"""EXECUTE tests: one per action, plus the honesty properties.

EXECUTE is the only stage that can touch the outside world, so the tests that
matter most here are about what it must NOT do:

* **Nothing is charged.** No branch may submit a payment. The strict spies below
  raise on any provider call other than "create a hosted link" and "send a
  message", so a future branch that reached for a charge API would fail a test
  rather than a compliance review (constraints #1, #2, #3, #6).
* **Nothing is silently dropped.** Every path returns a delivery status with a
  stated reason. ``code-standards.md`` requires a failed dispatch to reach a
  retry queue or an escalation, logged either way.
* **Nothing is inflated.** A dry run is not a send, and ``amount_recovered``
  stays null until a provider webhook confirms payment.
* **A refusal is not a failure.** A send refused because the recipient never
  opted in must be a skip; requeuing it would retry forever.

The allowlist wording is asserted against ``channels`` rather than hard-coded in
both places, because EXECUTE classifies a refusal by matching on that string. If
the message were reworded, refusals would silently become requeued failures, and
that coupling is exactly what ``test_real_allowlist_refusal_is_classified_as_a_skip``
catches.

No test here opens a socket or needs a database.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app import audit, channels, execute
from app.channels import (
    DryRunPaymentLinkFactory,
    DryRunSender,
    MessageResult,
    PaymentLink,
    RazorpayPaymentLinkFactory,
    TwilioWhatsAppSender,
    normalise_whatsapp_number,
)
from app.config import Settings
from app.execute import (
    LINK_ACTIONS,
    MESSAGE_TEMPLATES,
    ExecutionContext,
    audit_summaries,
    execute_action,
    render_message,
)
from app.schemas import (
    Action,
    Channel,
    CustomerOutcome,
    Decision,
    DeliveryStatus,
    GuardrailCheck,
    GuardrailName,
)

EVENT_ID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
FIXED_NOW = datetime(2026, 6, 15, 12, 30, tzinfo=UTC)
CONTACT = "+919812345670"

NON_LINK_ACTIONS = (Action.SCHEDULE_RETRY, Action.ESCALATE_TO_HUMAN_REVIEW)


# --------------------------------------------------------------------- doubles


class StrictFactory:
    """Link factory that records calls and refuses any other provider call.

    ``__getattr__`` failing loudly is the point: it turns "EXECUTE must never
    submit a charge" into something a test can prove, instead of a claim resting
    on having read the module.
    """

    def __init__(
        self, link: PaymentLink | None = None, error: Exception | None = None
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._link = link
        self._error = error

    def create(
        self, *, amount_minor: int, customer_id: str, email: str | None,
        contact: str | None, description: str,
    ) -> PaymentLink:
        self.calls.append(
            {
                "amount_minor": amount_minor,
                "customer_id": customer_id,
                "email": email,
                "contact": contact,
                "description": description,
            }
        )
        if self._error is not None:
            raise self._error
        return self._link or PaymentLink(
            link_id="plink_TESTLINK01",
            url="https://rzp.io/i/testlink01",
            amount_minor=amount_minor,
        )

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - fails the test
        raise AssertionError(
            f"EXECUTE called payment-provider method {name!r}. The only permitted "
            "provider call is creating a hosted payment link."
        )


class StrictSender:
    """Message sender that records calls and refuses any other method."""

    def __init__(
        self, result: MessageResult | None = None, error: Exception | None = None
    ) -> None:
        self.calls: list[dict[str, str]] = []
        self._result = result or MessageResult(
            delivered=True, provider_message_id="SM_test_0001"
        )
        self._error = error

    def send(self, *, to: str, body: str) -> MessageResult:
        self.calls.append({"to": to, "body": body})
        if self._error is not None:
            raise self._error
        return self._result

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - fails the test
        raise AssertionError(f"EXECUTE called messaging method {name!r}.")


def make_decision(
    action: Action,
    channel: Channel = Channel.WHATSAPP,
    *,
    blocked_reason: str | None = None,
    scheduled_for: datetime | None = None,
    delay_seconds: int | None = None,
    max_repeats: int | None = None,
) -> Decision:
    return Decision(
        event_id=EVENT_ID,
        action=action,
        channel=channel,
        scheduled_for=scheduled_for,
        guardrail_checks=[
            GuardrailCheck(
                name=name,
                passed=blocked_reason is None,
                detail="fixture check",
            )
            for name in GuardrailName
        ],
        blocked_reason=blocked_reason,
        delay_seconds=delay_seconds,
        max_repeats=max_repeats,
    )


def make_context(**overrides: Any) -> ExecutionContext:
    base: dict[str, Any] = {
        "customer_id": "cust_TEST01",
        "amount_at_risk_minor": 49900,
        "customer_name": "Priya Sharma",
        "email": "priya@example.invalid",
        "contact": CONTACT,
        "now": FIXED_NOW,
    }
    base.update(overrides)
    return ExecutionContext(**base)


# ------------------------------------------------------- link actions dispatch


@pytest.mark.parametrize("action", sorted(LINK_ACTIONS))
def test_link_action_sends_and_records_the_hosted_link(action: Action) -> None:
    factory, sender = StrictFactory(), StrictSender()

    outcome = execute_action(
        make_decision(action), make_context(), link_factory=factory, sender=sender
    )

    assert outcome.result.delivery_status is DeliveryStatus.SENT
    assert outcome.action is action
    assert outcome.channel is Channel.WHATSAPP
    assert outcome.recovery_link is not None
    assert outcome.provider_message_id == "SM_test_0001"
    assert outcome.skip_reason is None
    assert outcome.failure_reason is None
    assert outcome.requeued is False
    assert len(factory.calls) == 1
    assert len(sender.calls) == 1


@pytest.mark.parametrize("action", sorted(LINK_ACTIONS))
def test_link_is_created_for_the_amount_at_risk(action: Action) -> None:
    """Not the gross figure. Re-charging a part-paid invoice would overcharge."""
    factory = StrictFactory()

    execute_action(
        make_decision(action),
        make_context(amount_at_risk_minor=125050),
        link_factory=factory,
        sender=StrictSender(),
    )

    assert factory.calls[0]["amount_minor"] == 125050


@pytest.mark.parametrize("action", sorted(LINK_ACTIONS))
def test_message_carries_the_provider_hosted_url(action: Action) -> None:
    """The customer's only route to pay is Razorpay's own page (#1, #2, #6)."""
    link = PaymentLink(
        link_id="plink_HOSTED", url="https://rzp.io/i/hosted01", amount_minor=49900
    )
    sender = StrictSender()

    execute_action(
        make_decision(action),
        make_context(),
        link_factory=StrictFactory(link=link),
        sender=sender,
    )

    assert "https://rzp.io/i/hosted01" in sender.calls[0]["body"]


def test_link_is_sent_to_the_customer_on_record() -> None:
    sender = StrictSender()

    execute_action(
        make_decision(Action.SEND_REMINDER),
        make_context(contact="+919000000001"),
        link_factory=StrictFactory(),
        sender=sender,
    )

    assert sender.calls[0]["to"] == "+919000000001"


def test_link_description_identifies_the_event() -> None:
    """A reviewer looking at a link in the Razorpay dashboard can trace it back."""
    factory = StrictFactory()

    execute_action(
        make_decision(Action.SEND_REMINDER),
        make_context(),
        link_factory=factory,
        sender=StrictSender(),
    )

    assert EVENT_ID in factory.calls[0]["description"]


# ------------------------------------------------- nothing is ever charged here


@pytest.mark.parametrize("action", list(Action))
def test_no_action_calls_anything_beyond_link_creation_and_send(
    action: Action,
) -> None:
    """The strict doubles raise on any other provider method.

    Constraint #6 forbids this agent re-submitting a transaction, and #3 forbids a
    silent retry. Both hold only if no branch reaches for a charge API.
    """
    channel = Channel.WHATSAPP if action in LINK_ACTIONS else Channel.NONE

    outcome = execute_action(
        make_decision(action, channel, scheduled_for=FIXED_NOW + timedelta(days=3)),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(),
    )

    assert outcome.action is action


@pytest.mark.parametrize("action", NON_LINK_ACTIONS)
def test_non_contact_actions_create_no_link_and_send_nothing(action: Action) -> None:
    factory, sender = StrictFactory(), StrictSender()

    outcome = execute_action(
        make_decision(action, Channel.NONE, scheduled_for=FIXED_NOW),
        make_context(),
        link_factory=factory,
        sender=sender,
    )

    assert factory.calls == []
    assert sender.calls == []
    assert outcome.result.delivery_status is DeliveryStatus.SKIPPED
    assert outcome.channel is Channel.NONE


def test_schedule_retry_records_the_due_time_without_submitting_a_charge() -> None:
    due = FIXED_NOW + timedelta(days=3)
    factory, sender = StrictFactory(), StrictSender()

    outcome = execute_action(
        make_decision(Action.SCHEDULE_RETRY, Channel.NONE, scheduled_for=due),
        make_context(),
        link_factory=factory,
        sender=sender,
    )

    assert outcome.scheduled_for == due
    assert due.isoformat() in (outcome.skip_reason or "")
    assert "No charge submitted" in (outcome.skip_reason or "")
    assert factory.calls == []
    assert sender.calls == []


def test_schedule_retry_states_the_constraints_it_is_honouring() -> None:
    """The reason has to be readable by a reviewer, not just machine-parsable."""
    outcome = execute_action(
        make_decision(
            Action.SCHEDULE_RETRY, Channel.NONE, scheduled_for=FIXED_NOW
        ),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(),
    )

    assert "#3" in (outcome.skip_reason or "")
    assert "#6" in (outcome.skip_reason or "")


def test_schedule_retry_without_a_due_time_says_so_rather_than_crashing() -> None:
    outcome = execute_action(
        make_decision(Action.SCHEDULE_RETRY, Channel.NONE, scheduled_for=None),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(),
    )

    assert outcome.result.delivery_status is DeliveryStatus.SKIPPED
    assert "unset" in (outcome.skip_reason or "")


def test_escalation_queues_for_a_person_with_no_external_call() -> None:
    outcome = execute_action(
        make_decision(Action.ESCALATE_TO_HUMAN_REVIEW, Channel.NONE),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(),
    )

    assert outcome.result.delivery_status is DeliveryStatus.SKIPPED
    assert "human review" in (outcome.skip_reason or "").lower()


# ------------------------------------------------------- the guardrail stop gate


def test_blocked_decision_is_skipped_and_names_the_guardrail() -> None:
    factory, sender = StrictFactory(), StrictSender()

    outcome = execute_action(
        make_decision(
            Action.SEND_UPDATE_PAYMENT_METHOD_LINK,
            blocked_reason="max_retries: 3 of 3 attempts already made",
        ),
        make_context(),
        link_factory=factory,
        sender=sender,
    )

    assert outcome.result.delivery_status is DeliveryStatus.SKIPPED
    assert "Blocked by guardrail" in (outcome.skip_reason or "")
    assert "max_retries" in (outcome.skip_reason or "")
    assert outcome.channel is Channel.NONE
    assert factory.calls == []
    assert sender.calls == []


@pytest.mark.parametrize("action", list(Action))
def test_a_block_stops_every_action(action: Action) -> None:
    """Including the internal-only ones. A stop is a stop."""
    factory, sender = StrictFactory(), StrictSender()

    outcome = execute_action(
        make_decision(action, blocked_reason="quiet_hours: 02:00 local"),
        make_context(),
        link_factory=factory,
        sender=sender,
    )

    assert outcome.result.delivery_status is DeliveryStatus.SKIPPED
    assert factory.calls == []
    assert sender.calls == []


def test_channel_none_alone_does_not_stop_an_action() -> None:
    """``blocked_reason`` is the stop signal, not ``channel``.

    ``escalate_to_human_review`` legitimately carries ``channel: none`` while
    still being an action that must happen, so keying on channel would silently
    turn every escalation into a guardrail block.
    """
    blocked = execute_action(
        make_decision(
            Action.ESCALATE_TO_HUMAN_REVIEW, Channel.NONE, blocked_reason="hard_stop"
        ),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(),
    )
    allowed = execute_action(
        make_decision(Action.ESCALATE_TO_HUMAN_REVIEW, Channel.NONE),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(),
    )

    assert "Blocked by guardrail" in (blocked.skip_reason or "")
    assert "Blocked by guardrail" not in (allowed.skip_reason or "")
    assert "human review" in (allowed.skip_reason or "").lower()


# ------------------------------------------------------------ dry-run behaviour


def test_dry_run_send_is_skipped_not_counted_as_sent() -> None:
    """A run without messaging credentials must not inflate a delivery metric."""
    outcome = execute_action(
        make_decision(Action.SEND_REMINDER),
        make_context(),
        link_factory=StrictFactory(),
        sender=DryRunSender(),
    )

    assert outcome.result.delivery_status is DeliveryStatus.SKIPPED
    assert outcome.result.delivery_status is not DeliveryStatus.SENT
    assert "Dry run" in (outcome.skip_reason or "")
    assert outcome.provider_message_id is None
    # The link was still created, so the trail shows how far the attempt got.
    assert outcome.recovery_link is not None


def test_dry_run_sender_records_what_it_would_have_sent() -> None:
    sender = DryRunSender()

    execute_action(
        make_decision(Action.SEND_REMINDER),
        make_context(),
        link_factory=StrictFactory(),
        sender=sender,
    )

    assert len(sender.sent) == 1
    assert sender.sent[0][0] == CONTACT


def test_dry_run_link_is_flagged_in_the_audit_trail() -> None:
    """A reviewer must be able to tell a real link from a placeholder."""
    context = make_context()
    decision = make_decision(Action.SEND_REMINDER)

    outcome = execute_action(
        decision,
        context,
        link_factory=DryRunPaymentLinkFactory(),
        sender=DryRunSender(),
    )
    _, output = audit_summaries(decision, outcome, context)

    assert output["recovery_link_is_dry_run"] is True
    assert output["provider_message_id"] is None


def test_real_link_is_not_flagged_as_dry_run() -> None:
    context = make_context()
    decision = make_decision(Action.SEND_REMINDER)

    outcome = execute_action(
        decision, context, link_factory=StrictFactory(), sender=StrictSender()
    )
    _, output = audit_summaries(decision, outcome, context)

    assert output["recovery_link_is_dry_run"] is False


# ------------------------------------------------- failures reach a retry queue


def test_link_creation_failure_is_failed_and_requeued() -> None:
    sender = StrictSender()

    outcome = execute_action(
        make_decision(Action.SEND_FRESH_AUTH_LINK),
        make_context(),
        link_factory=StrictFactory(error=RuntimeError("razorpay 502")),
        sender=sender,
    )

    assert outcome.result.delivery_status is DeliveryStatus.FAILED
    assert outcome.requeued is True
    assert "razorpay 502" in (outcome.failure_reason or "")
    assert "RuntimeError" in (outcome.failure_reason or "")
    # No point messaging a customer a link we never created.
    assert sender.calls == []


def test_transport_failure_is_failed_and_requeued() -> None:
    outcome = execute_action(
        make_decision(Action.SEND_REMINDER),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(
            MessageResult(delivered=False, error="HTTPError: 503 from Twilio")
        ),
    )

    assert outcome.result.delivery_status is DeliveryStatus.FAILED
    assert outcome.requeued is True
    assert "503" in (outcome.failure_reason or "")


def test_sender_that_raises_is_requeued_rather_than_crashing_the_batch() -> None:
    """One broken send must not lose every event queued behind it."""
    outcome = execute_action(
        make_decision(Action.SEND_REMINDER),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(error=ConnectionResetError("connection reset")),
    )

    assert outcome.result.delivery_status is DeliveryStatus.FAILED
    assert outcome.requeued is True
    assert "ConnectionResetError" in (outcome.failure_reason or "")


def test_failure_with_no_stated_error_still_records_a_reason() -> None:
    """A blank reason would be indistinguishable from a dropped event."""
    outcome = execute_action(
        make_decision(Action.SEND_REMINDER),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(MessageResult(delivered=False)),
    )

    assert outcome.result.delivery_status is DeliveryStatus.FAILED
    assert outcome.failure_reason
    assert outcome.requeued is True


@pytest.mark.parametrize("action", list(Action))
def test_no_path_returns_without_a_stated_reason(action: Action) -> None:
    """Every non-sent outcome explains itself. Nothing is dropped in silence."""
    outcome = execute_action(
        make_decision(action, scheduled_for=FIXED_NOW),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(),
    )

    if outcome.result.delivery_status is not DeliveryStatus.SENT:
        assert outcome.skip_reason or outcome.failure_reason


# ---------------------------------------------- a refusal is not a failure


def test_allowlist_refusal_is_skipped_not_requeued() -> None:
    """Requeuing a refusal would retry forever; the number will never opt in."""
    refusal = (
        "recipient is not in TWILIO_WHATSAPP_TEST_RECIPIENTS; refusing to "
        "message a number that has not opted in"
    )

    outcome = execute_action(
        make_decision(Action.SEND_REMINDER),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(MessageResult(delivered=False, error=refusal)),
    )

    assert outcome.result.delivery_status is DeliveryStatus.SKIPPED
    assert outcome.requeued is False
    assert outcome.skip_reason == refusal
    assert outcome.failure_reason is None


def test_unusable_number_is_skipped_not_requeued() -> None:
    outcome = execute_action(
        make_decision(Action.SEND_REMINDER),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(
            MessageResult(delivered=False, error="unusable recipient number: '9812'")
        ),
    )

    assert outcome.result.delivery_status is DeliveryStatus.SKIPPED
    assert outcome.requeued is False


def test_real_allowlist_refusal_is_classified_as_a_skip(monkeypatch) -> None:
    """Closes the loop between the two modules.

    ``execute`` recognises a refusal by matching on the wording ``channels``
    produces. If that wording changed, refusals would become requeued failures
    and the queue would fill with sends that can never succeed. So the refusal
    here comes from the real sender rather than a copied literal.
    """
    sender = _twilio_sender(monkeypatch, allowlist="+919999999999")
    real = sender.send(to=CONTACT, body="hello")
    assert real.delivered is False

    outcome = execute_action(
        make_decision(Action.SEND_REMINDER),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(real),
    )

    assert outcome.result.delivery_status is DeliveryStatus.SKIPPED
    assert outcome.requeued is False


# ----------------------------------------------------- skips for missing inputs


def test_missing_contact_is_skipped_before_a_link_is_created() -> None:
    factory = StrictFactory()

    outcome = execute_action(
        make_decision(Action.SEND_REMINDER),
        make_context(contact=None),
        link_factory=factory,
        sender=StrictSender(),
    )

    assert outcome.result.delivery_status is DeliveryStatus.SKIPPED
    assert "No contact number" in (outcome.skip_reason or "")
    assert factory.calls == []


@pytest.mark.parametrize("amount", [0, -1])
def test_non_positive_amount_is_skipped(amount: int) -> None:
    """Nothing to recover, so no link is created and no message is sent."""
    factory = StrictFactory()

    outcome = execute_action(
        make_decision(Action.SEND_REMINDER),
        make_context(amount_at_risk_minor=amount),
        link_factory=factory,
        sender=StrictSender(),
    )

    assert outcome.result.delivery_status is DeliveryStatus.SKIPPED
    assert "nothing to" in (outcome.skip_reason or "")
    assert factory.calls == []


# ------------------------------------------------------------ outcome honesty


@pytest.mark.parametrize("action", list(Action))
def test_amount_recovered_is_never_prefilled(action: Action) -> None:
    """Only a provider webhook may set this. Defaulting it invents revenue."""
    outcome = execute_action(
        make_decision(action, scheduled_for=FIXED_NOW),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(),
    )

    assert outcome.result.amount_recovered is None
    assert outcome.result.customer_outcome is CustomerOutcome.PENDING


def test_a_successful_send_is_still_only_pending() -> None:
    """Delivery is not recovery. The customer has not paid yet."""
    outcome = execute_action(
        make_decision(Action.SEND_REMINDER),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(),
    )

    assert outcome.result.delivery_status is DeliveryStatus.SENT
    assert outcome.result.customer_outcome is CustomerOutcome.PENDING
    assert outcome.result.amount_recovered is None


def test_executed_at_uses_the_supplied_clock() -> None:
    outcome = execute_action(
        make_decision(Action.SEND_REMINDER),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(),
    )

    assert outcome.result.executed_at == FIXED_NOW


def test_executed_at_defaults_to_now_when_no_clock_is_supplied() -> None:
    before = datetime.now(UTC)

    outcome = execute_action(
        make_decision(Action.SEND_REMINDER),
        make_context(now=None),
        link_factory=StrictFactory(),
        sender=StrictSender(),
    )

    assert before <= outcome.result.executed_at <= datetime.now(UTC)


def test_result_carries_the_event_id_it_was_given() -> None:
    outcome = execute_action(
        make_decision(Action.SEND_REMINDER),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(),
    )

    assert outcome.result.event_id == EVENT_ID


# ---------------------------------------------------------------- audit entries


def test_audit_summaries_omit_the_message_body_and_link_url() -> None:
    """The trail is readable by anyone with ops access.

    The body carries the customer's name and a URL that opens a payment page, so
    the id is recorded and the URL is not.
    """
    context = make_context()
    decision = make_decision(Action.SEND_UPDATE_PAYMENT_METHOD_LINK)
    link = PaymentLink(
        link_id="plink_SECRET01",
        url="https://rzp.io/i/secret01",
        amount_minor=49900,
    )

    outcome = execute_action(
        decision,
        context,
        link_factory=StrictFactory(link=link),
        sender=StrictSender(),
    )
    serialised = json.dumps(audit_summaries(decision, outcome, context))

    assert "plink_SECRET01" in serialised
    assert "https://rzp.io/i/secret01" not in serialised
    assert "Priya" not in serialised
    assert outcome.message_body is not None
    assert outcome.message_body not in serialised


def test_audit_input_records_whether_a_contact_was_held() -> None:
    context = make_context(contact=None)
    decision = make_decision(Action.SEND_REMINDER)

    outcome = execute_action(
        decision, context, link_factory=StrictFactory(), sender=StrictSender()
    )
    input_summary, _ = audit_summaries(decision, outcome, context)

    assert input_summary["has_contact"] is False
    assert input_summary["amount_at_risk_minor"] == 49900


def test_audit_records_the_scheduled_time_for_a_retry() -> None:
    due = FIXED_NOW + timedelta(days=3)
    context = make_context()
    decision = make_decision(Action.SCHEDULE_RETRY, Channel.NONE, scheduled_for=due)

    outcome = execute_action(
        decision, context, link_factory=StrictFactory(), sender=StrictSender()
    )
    input_summary, output_summary = audit_summaries(decision, outcome, context)

    assert input_summary["scheduled_for"] == due.isoformat()
    assert output_summary["skip_reason"]


def test_audit_output_reports_a_null_recovered_amount() -> None:
    context = make_context()
    decision = make_decision(Action.SEND_REMINDER)

    outcome = execute_action(
        decision, context, link_factory=StrictFactory(), sender=StrictSender()
    )
    _, output_summary = audit_summaries(decision, outcome, context)

    assert output_summary["amount_recovered_minor"] is None


@pytest.mark.parametrize("action", list(Action))
def test_audit_summaries_are_persistable_json(action: Action) -> None:
    """They land in a JSONB column, so anything unserialisable breaks the write."""
    context = make_context()
    decision = make_decision(action, scheduled_for=FIXED_NOW)

    outcome = execute_action(
        decision, context, link_factory=StrictFactory(), sender=StrictSender()
    )
    input_summary, output_summary = audit_summaries(decision, outcome, context)

    json.dumps(input_summary)
    json.dumps(output_summary)
    # Constraint #1 is enforced at persistence; prove these pass that gate.
    audit.assert_no_sensitive_card_data(input_summary)
    audit.assert_no_sensitive_card_data(output_summary)


@pytest.mark.parametrize("action", list(Action))
def test_audit_output_always_states_the_delivery_status(action: Action) -> None:
    context = make_context()
    decision = make_decision(action, scheduled_for=FIXED_NOW)

    outcome = execute_action(
        decision, context, link_factory=StrictFactory(), sender=StrictSender()
    )
    _, output_summary = audit_summaries(decision, outcome, context)

    assert output_summary["delivery_status"] in {"sent", "failed", "skipped"}
    assert output_summary["customer_outcome"] == "pending"


# ------------------------------------------------------------------- rendering


def test_every_link_action_has_a_message_template() -> None:
    """A missing template would raise KeyError mid-dispatch."""
    assert set(MESSAGE_TEMPLATES) == set(LINK_ACTIONS)


def test_link_actions_and_internal_actions_partition_the_enum() -> None:
    """Guards against a new action silently falling through with no dispatch."""
    assert LINK_ACTIONS | set(NON_LINK_ACTIONS) == set(Action)
    assert not LINK_ACTIONS & set(NON_LINK_ACTIONS)


@pytest.mark.parametrize("action", sorted(LINK_ACTIONS))
def test_rendered_message_states_the_amount_and_the_link(action: Action) -> None:
    link = PaymentLink(
        link_id="plink_R", url="https://rzp.io/i/render01", amount_minor=49900
    )

    body = render_message(action, make_context(), link)

    assert "INR 499.00" in body
    assert "https://rzp.io/i/render01" in body
    assert "Priya" in body


@pytest.mark.parametrize(
    ("minor", "expected"),
    [
        (49900, "INR 499.00"),
        (100, "INR 1.00"),
        (1, "INR 0.01"),
        (123456789, "INR 1,234,567.89"),
    ],
)
def test_amount_is_displayed_in_rupees_from_paise(minor: int, expected: str) -> None:
    """Paise are the stored truth; rupees are derived only for display."""
    assert make_context(amount_at_risk_minor=minor).display_amount() == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [("Priya Sharma", "Priya"), ("Priya", "Priya"), (None, "there"), ("", "there")],
)
def test_display_name_uses_the_first_name_or_a_neutral_fallback(
    name: str | None, expected: str
) -> None:
    assert make_context(customer_name=name).display_name() == expected


def test_templates_never_mention_card_details() -> None:
    """Constraint #1: we never ask for card data over a message."""
    forbidden = ("cvv", "card number", "expiry", "pin", "otp")
    for template in MESSAGE_TEMPLATES.values():
        lowered = template.lower()
        for word in forbidden:
            assert word not in lowered


# ------------------------------------------------------ channel adapter: links


def _razorpay_factory(monkeypatch, *, created: dict[str, Any] | None = None):
    """A ``RazorpayPaymentLinkFactory`` with the SDK swapped for a recorder."""
    calls: list[dict[str, Any]] = []

    class FakePaymentLink:
        def create(self, payload: dict[str, Any]) -> dict[str, Any]:
            calls.append(payload)
            return created or {
                "id": "plink_FAKE01",
                "short_url": "https://rzp.io/i/fake01",
            }

    class FakeClient:
        def __init__(self, auth: tuple[str, str]) -> None:
            self.auth = auth
            self.payment_link = FakePaymentLink()

    monkeypatch.setattr("razorpay.Client", FakeClient)
    settings = Settings(
        _env_file=None,
        razorpay_key_id="rzp_test_fake",
        razorpay_key_secret="fake_secret",
    )
    return RazorpayPaymentLinkFactory(settings), calls


def test_razorpay_factory_refuses_a_live_mode_key() -> None:
    """Defence in depth. A live key here would create a genuinely payable link.

    ``Settings`` also rejects live keys, so a bare namespace is used to prove the
    adapter refuses on its own rather than relying on the outer validator.
    """
    live = SimpleNamespace(
        razorpay_key_id="rzp_live_realkey", razorpay_key_secret="secret"
    )

    with pytest.raises(ValueError, match="test"):
        RazorpayPaymentLinkFactory(live)  # type: ignore[arg-type]


def test_razorpay_factory_requires_credentials() -> None:
    blank = SimpleNamespace(razorpay_key_id=None, razorpay_key_secret=None)

    with pytest.raises(ValueError, match="not configured"):
        RazorpayPaymentLinkFactory(blank)  # type: ignore[arg-type]


def test_razorpay_link_disables_provider_notifications(monkeypatch) -> None:
    """This agent owns the messaging.

    Letting Razorpay also notify would double-contact the customer from outside
    our guardrails and could breach the one-contact-per-24h rule.
    """
    factory, calls = _razorpay_factory(monkeypatch)

    factory.create(
        amount_minor=49900,
        customer_id="cust_1",
        email="a@example.invalid",
        contact=CONTACT,
        description="Recovery for evt",
    )

    assert calls[0]["notify"] == {"sms": False, "email": False}
    assert calls[0]["reminder_enable"] is False


def test_razorpay_link_requests_the_exact_amount_and_no_partial_payment(
    monkeypatch,
) -> None:
    factory, calls = _razorpay_factory(monkeypatch)

    link = factory.create(
        amount_minor=125050,
        customer_id="cust_1",
        email=None,
        contact=CONTACT,
        description="Recovery for evt",
    )

    assert calls[0]["amount"] == 125050
    assert calls[0]["currency"] == "INR"
    assert calls[0]["accept_partial"] is False
    assert link.amount_minor == 125050
    assert link.dry_run is False


def test_razorpay_link_tags_the_customer_for_traceability(monkeypatch) -> None:
    factory, calls = _razorpay_factory(monkeypatch)

    factory.create(
        amount_minor=49900,
        customer_id="cust_TRACE",
        email=None,
        contact=None,
        description="Recovery for evt",
    )

    assert calls[0]["notes"]["customer_id"] == "cust_TRACE"
    # No contact and no email means no customer block at all, rather than one
    # containing nulls the API would reject.
    assert "customer" not in calls[0]


def test_razorpay_description_is_truncated_to_the_api_limit(monkeypatch) -> None:
    factory, calls = _razorpay_factory(monkeypatch)

    factory.create(
        amount_minor=49900,
        customer_id="cust_1",
        email=None,
        contact=CONTACT,
        description="x" * 400,
    )

    assert len(calls[0]["description"]) == 255


def test_dry_run_link_is_deterministic_and_clearly_marked() -> None:
    factory = DryRunPaymentLinkFactory()
    kwargs = {
        "amount_minor": 49900,
        "customer_id": "cust_1",
        "email": None,
        "contact": CONTACT,
        "description": "d",
    }

    first, second = factory.create(**kwargs), factory.create(**kwargs)

    assert first == second
    assert first.dry_run is True
    assert "dryrun" in first.link_id
    # An unroutable host, so a placeholder link can never be mistaken for real.
    assert ".invalid" in first.url


def test_build_link_factory_falls_back_to_dry_run_without_credentials() -> None:
    factory = channels.build_payment_link_factory(Settings(_env_file=None))

    assert isinstance(factory, DryRunPaymentLinkFactory)


# --------------------------------------------------- channel adapter: WhatsApp


def _twilio_sender(monkeypatch, *, allowlist: str, error: Exception | None = None):
    """A ``TwilioWhatsAppSender`` with the SDK swapped for a recorder."""
    sent: list[dict[str, str]] = []

    class FakeMessages:
        def create(self, *, from_: str, to: str, body: str):
            if error is not None:
                raise error
            sent.append({"from_": from_, "to": to, "body": body})
            return SimpleNamespace(sid="SM_fake_sid")

    class FakeClient:
        def __init__(self, sid: str, token: str) -> None:
            self.messages = FakeMessages()

    monkeypatch.setattr("twilio.rest.Client", FakeClient)
    settings = Settings(
        _env_file=None,
        twilio_account_sid="AC_fake",
        twilio_auth_token="token_fake",
        twilio_from_number="whatsapp:+14155238886",
        twilio_whatsapp_test_recipients=allowlist,
    )
    sender = TwilioWhatsAppSender(settings)
    sender.sent_messages = sent  # type: ignore[attr-defined]
    return sender


def test_whatsapp_send_is_refused_for_a_number_not_on_the_allowlist(
    monkeypatch,
) -> None:
    """Synthetic fixture numbers are well-formed and could belong to a stranger."""
    sender = _twilio_sender(monkeypatch, allowlist="+919999999999")

    result = sender.send(to=CONTACT, body="hello")

    assert result.delivered is False
    assert result.dry_run is False
    assert "TWILIO_WHATSAPP_TEST_RECIPIENTS" in (result.error or "")
    assert sender.sent_messages == []  # type: ignore[attr-defined]


def test_whatsapp_send_reaches_an_allowlisted_number(monkeypatch) -> None:
    sender = _twilio_sender(monkeypatch, allowlist=CONTACT)

    result = sender.send(to=CONTACT, body="hello")

    assert result.delivered is True
    assert result.provider_message_id == "SM_fake_sid"
    sent = sender.sent_messages[0]  # type: ignore[attr-defined]
    assert sent["to"] == f"whatsapp:{CONTACT}"
    assert sent["from_"] == "whatsapp:+14155238886"


def test_allowlist_matches_across_formatting_differences(monkeypatch) -> None:
    """A number allowlisted bare must match a recipient in ``whatsapp:`` form.

    Otherwise every send is refused for the wrong reason and looks like a policy
    decision rather than a formatting bug.
    """
    sender = _twilio_sender(monkeypatch, allowlist=" +91-98123-45670 ")

    assert sender.send(to=f"whatsapp:{CONTACT}", body="hi").delivered is True


def test_empty_allowlist_delivers_nothing(monkeypatch) -> None:
    sender = _twilio_sender(monkeypatch, allowlist="")

    assert sender.send(to=CONTACT, body="hi").delivered is False


def test_unusable_recipient_is_reported_before_any_send(monkeypatch) -> None:
    sender = _twilio_sender(monkeypatch, allowlist=CONTACT)

    result = sender.send(to="9812345670", body="hi")

    assert result.delivered is False
    assert "unusable recipient" in (result.error or "")
    assert sender.sent_messages == []  # type: ignore[attr-defined]


def test_twilio_transport_error_is_surfaced_not_swallowed(monkeypatch) -> None:
    sender = _twilio_sender(
        monkeypatch, allowlist=CONTACT, error=RuntimeError("twilio 500")
    )

    result = sender.send(to=CONTACT, body="hi")

    assert result.delivered is False
    assert "RuntimeError" in (result.error or "")
    assert "twilio 500" in (result.error or "")


def test_sender_requires_a_whatsapp_formatted_from_number() -> None:
    """An SMS-formatted sender would silently send over the wrong channel."""
    settings = Settings(
        _env_file=None,
        twilio_account_sid="AC_fake",
        twilio_auth_token="token_fake",
        twilio_from_number="14155238886",
    )

    with pytest.raises(ValueError, match="whatsapp"):
        TwilioWhatsAppSender(settings)


def test_sender_requires_credentials() -> None:
    with pytest.raises(ValueError, match="not configured"):
        TwilioWhatsAppSender(Settings(_env_file=None))


def test_build_message_sender_falls_back_to_dry_run_without_credentials() -> None:
    sender = channels.build_message_sender(Settings(_env_file=None))

    assert isinstance(sender, DryRunSender)


def test_dry_run_sender_never_claims_delivery() -> None:
    result = DryRunSender().send(to=CONTACT, body="hi")

    assert result.delivered is False
    assert result.dry_run is True
    assert result.provider_message_id is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("+919812345670", "whatsapp:+919812345670"),
        ("whatsapp:+919812345670", "whatsapp:+919812345670"),
        ("+91 98123 45670", "whatsapp:+919812345670"),
        ("+91-98123-45670", "whatsapp:+919812345670"),
        ("  whatsapp:+91 98123-45670 ", "whatsapp:+919812345670"),
        ("9812345670", None),
        ("whatsapp:9812345670", None),
    ],
)
def test_number_normalisation(raw: str | None, expected: str | None) -> None:
    assert normalise_whatsapp_number(raw) == expected


# ------------------------------------------------------------------ defaults


def test_execute_without_injected_adapters_uses_the_dry_run_fallbacks(
    monkeypatch,
) -> None:
    """The default path must not require credentials or reach the network.

    Both builders are pointed at clean settings so this test cannot pick up a
    real key from a local ``.env`` and message anyone.
    """
    clean = Settings(_env_file=None)
    monkeypatch.setattr(channels, "get_settings", lambda: clean)
    monkeypatch.setattr(execute.channels, "get_settings", lambda: clean)

    outcome = execute_action(make_decision(Action.SEND_REMINDER), make_context())

    assert outcome.result.delivery_status is DeliveryStatus.SKIPPED
    assert "Dry run" in (outcome.skip_reason or "")
    assert outcome.recovery_link is not None
    assert outcome.recovery_link.dry_run is True


# ------------------------------------------------------- the deferral stop gate
#
# Quiet hours and contact frequency are DEFERRABLE guardrails: DECIDE leaves
# blocked_reason empty and moves scheduled_for to the next permitted moment
# instead. Honouring only blocked_reason would dispatch those immediately and
# message the customer inside the window the rule exists to protect, so
# constraint #4 holds end to end only if EXECUTE also respects the due time.


def test_contact_action_due_in_the_future_is_held(monkeypatch) -> None:
    factory, sender = StrictFactory(), StrictSender()

    outcome = execute_action(
        make_decision(
            Action.SEND_REMINDER, scheduled_for=FIXED_NOW + timedelta(hours=9)
        ),
        make_context(),
        link_factory=factory,
        sender=sender,
    )

    assert outcome.result.delivery_status is DeliveryStatus.SKIPPED
    assert "Deferred until" in (outcome.skip_reason or "")
    assert outcome.scheduled_for == FIXED_NOW + timedelta(hours=9)
    # Nothing created early: a hosted link minted hours ahead is wasted work.
    assert factory.calls == []
    assert sender.calls == []


@pytest.mark.parametrize("action", sorted(LINK_ACTIONS))
def test_every_contact_action_respects_a_deferral(action: Action) -> None:
    sender = StrictSender()

    outcome = execute_action(
        make_decision(action, scheduled_for=FIXED_NOW + timedelta(minutes=1)),
        make_context(),
        link_factory=StrictFactory(),
        sender=sender,
    )

    assert outcome.result.delivery_status is DeliveryStatus.SKIPPED
    assert sender.calls == []


def test_a_deferral_is_a_skip_not_a_failure() -> None:
    """It will be sent later. Requeuing it as a failure would misreport it."""
    outcome = execute_action(
        make_decision(
            Action.SEND_REMINDER, scheduled_for=FIXED_NOW + timedelta(hours=9)
        ),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(),
    )

    assert outcome.requeued is False
    assert outcome.failure_reason is None
    assert outcome.skip_reason


def test_deferral_reason_names_the_due_time_and_the_constraint() -> None:
    """A reviewer has to see that the delay was deliberate, not a fault."""
    due = FIXED_NOW + timedelta(hours=9)

    outcome = execute_action(
        make_decision(Action.SEND_REMINDER, scheduled_for=due),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(),
    )

    assert due.isoformat() in (outcome.skip_reason or "")
    assert "#4" in (outcome.skip_reason or "")


def test_contact_action_due_now_is_sent() -> None:
    """The normal path. Every contact action in the table has a zero delay."""
    outcome = execute_action(
        make_decision(Action.SEND_REMINDER, scheduled_for=FIXED_NOW),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(),
    )

    assert outcome.result.delivery_status is DeliveryStatus.SENT


def test_contact_action_already_due_is_sent() -> None:
    """A deferred send picked up after its due time goes out, not held forever."""
    outcome = execute_action(
        make_decision(
            Action.SEND_REMINDER, scheduled_for=FIXED_NOW - timedelta(hours=2)
        ),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(),
    )

    assert outcome.result.delivery_status is DeliveryStatus.SENT


def test_contact_action_with_no_due_time_is_sent() -> None:
    outcome = execute_action(
        make_decision(Action.SEND_REMINDER, scheduled_for=None),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(),
    )

    assert outcome.result.delivery_status is DeliveryStatus.SENT


def test_escalation_is_not_deferred_by_its_scheduled_time() -> None:
    """Human review is internal and should reach the queue now.

    Deferring it would leave a risky event sitting with nobody told about it.
    """
    outcome = execute_action(
        make_decision(
            Action.ESCALATE_TO_HUMAN_REVIEW,
            Channel.NONE,
            scheduled_for=FIXED_NOW + timedelta(days=2),
        ),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(),
    )

    assert "human review" in (outcome.skip_reason or "").lower()
    assert "Deferred until" not in (outcome.skip_reason or "")


def test_schedule_retry_reports_its_own_due_time_not_a_deferral() -> None:
    """It is already a record-only action; a due time is its normal state."""
    due = FIXED_NOW + timedelta(days=3)

    outcome = execute_action(
        make_decision(Action.SCHEDULE_RETRY, Channel.NONE, scheduled_for=due),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(),
    )

    assert "No charge submitted" in (outcome.skip_reason or "")
    assert "Deferred until" not in (outcome.skip_reason or "")


def test_a_terminal_block_outranks_a_deferral() -> None:
    """Cancelled beats postponed, and the reason must say cancelled."""
    outcome = execute_action(
        make_decision(
            Action.SEND_REMINDER,
            scheduled_for=FIXED_NOW + timedelta(hours=9),
            blocked_reason="hard_stop_7_days: 8 days since first failure",
        ),
        make_context(),
        link_factory=StrictFactory(),
        sender=StrictSender(),
    )

    assert "Blocked by guardrail" in (outcome.skip_reason or "")
    assert "Deferred until" not in (outcome.skip_reason or "")


def test_deferral_is_reported_before_a_missing_contact() -> None:
    """The gates on the decision run before the checks on the customer record.

    A send that is not due yet has not been attempted, so "no contact number" would
    be asserting the outcome of an attempt that has not happened. The contact may
    well be filled in before the due time, and the gap surfaces then.
    """
    outcome = execute_action(
        make_decision(
            Action.SEND_REMINDER, scheduled_for=FIXED_NOW + timedelta(hours=9)
        ),
        make_context(contact=None),
        link_factory=StrictFactory(),
        sender=StrictSender(),
    )

    assert "Deferred until" in (outcome.skip_reason or "")


def test_missing_contact_surfaces_once_the_send_is_due() -> None:
    outcome = execute_action(
        make_decision(Action.SEND_REMINDER, scheduled_for=FIXED_NOW),
        make_context(contact=None),
        link_factory=StrictFactory(),
        sender=StrictSender(),
    )

    assert "No contact number" in (outcome.skip_reason or "")


@pytest.mark.parametrize(
    ("action", "offset", "expected"),
    [
        (Action.SEND_REMINDER, timedelta(hours=1), True),
        (Action.SEND_REMINDER, timedelta(0), False),
        (Action.SEND_REMINDER, timedelta(hours=-1), False),
        (Action.SCHEDULE_RETRY, timedelta(days=3), False),
        (Action.ESCALATE_TO_HUMAN_REVIEW, timedelta(days=3), False),
    ],
)
def test_is_deferred_only_holds_contact_actions_not_yet_due(
    action: Action, offset: timedelta, expected: bool
) -> None:
    decision = make_decision(action, scheduled_for=FIXED_NOW + offset)

    assert execute.is_deferred(decision, FIXED_NOW) is expected


def test_deferred_send_is_recorded_in_the_audit_trail() -> None:
    """A held send must be visible, or it looks like the event was dropped."""
    due = FIXED_NOW + timedelta(hours=9)
    context = make_context()
    decision = make_decision(Action.SEND_REMINDER, scheduled_for=due)

    outcome = execute_action(
        decision, context, link_factory=StrictFactory(), sender=StrictSender()
    )
    input_summary, output_summary = audit_summaries(decision, outcome, context)

    assert input_summary["scheduled_for"] == due.isoformat()
    assert output_summary["delivery_status"] == "skipped"
    assert "Deferred until" in output_summary["skip_reason"]
    assert output_summary["recovery_link_id"] is None
