"""DECIDE tests: one per action, plus the stop-or-defer logic.

Two properties matter most here, because DECIDE's output is an action rather than
a label:

* **Determinism.** Same inputs, same Decision. A reproducible metrics run and a
  meaningful violation count both depend on it, and it is the reason this stage
  is a table rather than a model call.
* **No drift from the doc.** ``test_action_table_matches_architecture_doc``
  parses ``architecture.md``, so adding a row to the table without updating the
  doc fails the build.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import decide as decide_module
from app.config import Settings
from app.decide import (
    ATTEMPTS_CHARGE,
    CONTACTS_CUSTOMER,
    INTERNAL_ONLY,
    DecisionContext,
    action_table,
    applies_to_action,
    audit_summaries,
    decide_action,
    guardrail_check_rows,
)
from app.schemas import (
    Action,
    Channel,
    CustomerHistory,
    Diagnosis,
    EventRecord,
    EventType,
    GuardrailName,
    RootCause,
)

IST = ZoneInfo("Asia/Kolkata")
ARCHITECTURE = (
    Path(__file__).resolve().parents[2] / "context" / "architecture.md"
)


@pytest.fixture(autouse=True)
def _pinned_settings(monkeypatch):
    settings = Settings(
        _env_file=None,
        max_recovery_attempts=3,
        min_hours_between_contacts=24,
        quiet_hours_start_local=9,
        quiet_hours_end_local=20,
        hard_stop_days=7,
        insufficient_funds_retry_days=3,
    )
    monkeypatch.setattr("app.guardrails.get_settings", lambda: settings)
    monkeypatch.setattr("app.decide.get_settings", lambda: settings)
    return settings


def at_ist(hour: int, day: int = 15) -> datetime:
    return datetime(2026, 6, day, hour, 0, tzinfo=IST).astimezone(UTC)


def make_event(
    prior_attempts: int = 0,
    event_type: EventType = EventType.PAYMENT_FAILED,
) -> EventRecord:
    return EventRecord(
        event_id="11111111-1111-5111-8111-111111111111",
        customer_id="cust_1",
        event_type=event_type,
        decline_code="insufficient_funds",
        amount=Decimal("499.00"),
        currency="INR",
        prior_attempts=prior_attempts,
        customer_history=CustomerHistory(tenure_days=100, past_failures=1),
        detected_at=datetime.now(UTC),
    )


def make_diagnosis(cause: RootCause) -> Diagnosis:
    return Diagnosis(
        event_id="11111111-1111-5111-8111-111111111111",
        root_cause=cause,
        confidence=0.92,
        reasoning="test",
    )


def clean_context(now: datetime | None = None) -> DecisionContext:
    """A context where every guardrail passes."""
    moment = now or at_ist(12)
    return DecisionContext(
        customer_timezone="Asia/Kolkata",
        first_failure_at=moment - timedelta(hours=2),
        last_contact_at=None,
        now=moment,
    )


def decide(cause: RootCause, event=None, context=None):
    return decide_action(
        event or make_event(), make_diagnosis(cause), context or clean_context()
    )


# ------------------------------------------------------- one test per action


EXPECTED_ACTIONS = {
    RootCause.CARD_EXPIRED: Action.SEND_UPDATE_PAYMENT_METHOD_LINK,
    RootCause.INSUFFICIENT_FUNDS: Action.SCHEDULE_RETRY,
    RootCause.BANK_RISK_BLOCK: Action.ESCALATE_TO_HUMAN_REVIEW,
    RootCause.SCA_ABANDONED: Action.SEND_FRESH_AUTH_LINK,
    RootCause.NETWORK_ERROR: Action.SCHEDULE_RETRY,
    RootCause.CHECKOUT_FRICTION: Action.SEND_REMINDER,
    RootCause.GENUINE_ABANDONMENT: Action.SEND_REMINDER,
    RootCause.UNKNOWN: Action.ESCALATE_TO_HUMAN_REVIEW,
}


@pytest.mark.parametrize(("cause", "action"), sorted(EXPECTED_ACTIONS.items()))
def test_each_root_cause_maps_to_its_documented_action(
    cause: RootCause, action: Action
) -> None:
    assert decide(cause).action is action


def test_all_eight_rows_are_present() -> None:
    assert set(action_table()) == set(RootCause)
    assert len(action_table()) == 8


def test_action_table_matches_architecture_doc() -> None:
    """The table and the doc must not drift.

    ai-workflow-rules.md forbids adding an action without updating
    architecture.md first. This makes that mechanical rather than remembered.
    """
    doc = ARCHITECTURE.read_text(encoding="utf-8")
    valid_causes = {c.value for c in RootCause}
    documented: dict[str, str] = {}
    for line in doc.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2 or cells[0] not in valid_causes:
            continue
        # Strip inline parameters: schedule_retry(+N days) -> schedule_retry
        documented[cells[0]] = re.sub(r"\(.*?\)", "", cells[1]).split(",")[0].strip()

    assert len(documented) == 8, f"parsed {len(documented)} rows from the doc"
    for cause, action in documented.items():
        assert str(action_table()[RootCause(cause)].action) == action, cause


def test_no_llm_is_reachable_from_this_stage() -> None:
    """architecture.md: no LLM call inside DECIDE.

    Asserted on the module source rather than trusted, because the whole point of
    a deterministic stage is that it cannot quietly become non-deterministic.
    """
    source = Path(decide_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("genai", "gemini", "diagnose_root_cause", "generate_content"):
        assert forbidden not in source.replace("DIAGNOSE", ""), forbidden


def test_decisions_are_deterministic() -> None:
    """Reproducibility is what makes a violation count meaningful."""
    context = clean_context()
    event = make_event(prior_attempts=1)
    first = decide_action(event, make_diagnosis(RootCause.CARD_EXPIRED), context)
    second = decide_action(event, make_diagnosis(RootCause.CARD_EXPIRED), context)
    assert first.model_dump() == second.model_dump()


# ------------------------------------------------------ action parameters


def test_insufficient_funds_retries_after_a_delay() -> None:
    decision = decide(RootCause.INSUFFICIENT_FUNDS)
    assert decision.action is Action.SCHEDULE_RETRY
    assert decision.delay_seconds == 3 * 86400
    assert decision.channel is Channel.NONE


def test_network_error_retries_after_one_hour_only_once() -> None:
    """architecture.md: single quiet retry, then stop."""
    decision = decide(RootCause.NETWORK_ERROR)
    assert decision.delay_seconds == 3600
    assert decision.max_repeats == 1

    second = decide(RootCause.NETWORK_ERROR, event=make_event(prior_attempts=1))
    assert second.blocked_reason is not None
    assert "Action limit reached" in second.blocked_reason


def test_card_expired_never_schedules_a_retry() -> None:
    """A retry cannot succeed on an expired card, so attempting one is waste."""
    decision = decide(RootCause.CARD_EXPIRED)
    assert decision.action is not Action.SCHEDULE_RETRY
    assert decision.action is Action.SEND_UPDATE_PAYMENT_METHOD_LINK


def test_bank_risk_block_never_retries_the_instrument() -> None:
    """Constraint #3. Re-presenting an instrument the issuer refused is the one
    action that must never be automated."""
    decision = decide(RootCause.BANK_RISK_BLOCK)
    assert decision.action is Action.ESCALATE_TO_HUMAN_REVIEW
    assert decision.action not in ATTEMPTS_CHARGE


def test_unknown_never_guesses_an_action() -> None:
    decision = decide(RootCause.UNKNOWN)
    assert decision.action is Action.ESCALATE_TO_HUMAN_REVIEW


def test_sca_abandoned_sends_a_link_rather_than_completing_auth() -> None:
    """Constraint #2: the customer completes 3DS themselves.

    Any action implying we authenticate on their behalf would breach it.
    """
    decision = decide(RootCause.SCA_ABANDONED)
    assert decision.action is Action.SEND_FRESH_AUTH_LINK
    assert decision.channel is Channel.WHATSAPP


def test_reminders_are_capped_at_one() -> None:
    for cause in (RootCause.CHECKOUT_FRICTION, RootCause.GENUINE_ABANDONMENT):
        first = decide(cause, event=make_event(event_type=EventType.CHECKOUT_ABANDONED))
        assert first.max_repeats == 1
        assert first.blocked_reason is None

        repeat = decide(
            cause,
            event=make_event(prior_attempts=1, event_type=EventType.CHECKOUT_ABANDONED),
        )
        assert repeat.blocked_reason is not None


def test_only_whatsapp_or_none_is_ever_chosen() -> None:
    """Email has no live integration, so choosing it would produce a decision
    EXECUTE cannot honour. Voice is not a channel in this project at all."""
    for cause in RootCause:
        assert decide(cause).channel in {Channel.WHATSAPP, Channel.NONE}


# ------------------------------------------------- guardrails are recorded


def test_every_decision_records_all_four_checks() -> None:
    """Constraint #5, at the DECIDE boundary."""
    for cause in RootCause:
        decision = decide(cause)
        assert {c.name for c in decision.guardrail_checks} == set(GuardrailName)


def test_passing_checks_appear_in_the_derived_list() -> None:
    decision = decide(RootCause.CARD_EXPIRED)
    assert set(decision.guardrail_checks_passed) == set(GuardrailName)


def test_derived_passed_list_cannot_disagree_with_the_checks() -> None:
    """guardrail_checks_passed is a property, not a stored field.

    architecture.md's Decision example lists only passing names, which cannot
    record a failure. Deriving it keeps the two consistent by construction.
    """
    decision = decide(
        RootCause.CARD_EXPIRED, event=make_event(prior_attempts=3)
    )
    failed = {c.name for c in decision.guardrail_checks if not c.passed}
    assert GuardrailName.MAX_RETRIES in failed
    assert GuardrailName.MAX_RETRIES not in decision.guardrail_checks_passed


def test_guardrail_rows_state_whether_each_check_applied() -> None:
    """A reader needs to see that a check ran and why it did or did not matter."""
    rows = guardrail_check_rows(decide(RootCause.INSUFFICIENT_FUNDS))
    by_name = {r["name"]: r for r in rows}
    assert len(rows) == 4
    assert by_name["quiet_hours"]["applied_to_action"] is False
    assert by_name["max_retries"]["applied_to_action"] is True
    assert all(r["detail"] for r in rows)


# --------------------------------------------------------- terminal stops


def test_max_retries_stops_a_contacting_action() -> None:
    decision = decide(RootCause.CARD_EXPIRED, event=make_event(prior_attempts=3))
    assert decision.blocked_reason is not None
    assert "max_retries" in decision.blocked_reason
    assert decision.channel is Channel.NONE
    assert decision.scheduled_for is None


def test_hard_stop_stops_a_retry() -> None:
    now = at_ist(12)
    context = DecisionContext(
        customer_timezone="Asia/Kolkata",
        first_failure_at=now - timedelta(days=9),
        last_contact_at=None,
        now=now,
    )
    decision = decide(RootCause.INSUFFICIENT_FUNDS, context=context)
    assert decision.blocked_reason is not None
    assert "hard_stop_7_days" in decision.blocked_reason


def test_blocked_decision_preserves_the_action_it_would_have_taken() -> None:
    """The trail should say what was prevented, not just that nothing happened."""
    decision = decide(RootCause.SCA_ABANDONED, event=make_event(prior_attempts=3))
    assert decision.action is Action.SEND_FRESH_AUTH_LINK
    assert decision.blocked_reason is not None


def test_escalation_survives_terminal_guardrails() -> None:
    """Handing an exhausted case to a person is safe and is the point.

    Suppressing the handoff exactly when automation has run out would leave the
    revenue unattended with nobody informed.
    """
    now = at_ist(3)
    context = DecisionContext(
        customer_timezone="Asia/Kolkata",
        first_failure_at=now - timedelta(days=30),
        last_contact_at=now - timedelta(minutes=5),
        now=now,
    )
    for cause in (RootCause.BANK_RISK_BLOCK, RootCause.UNKNOWN):
        decision = decide(cause, event=make_event(prior_attempts=9), context=context)
        assert decision.action is Action.ESCALATE_TO_HUMAN_REVIEW
        assert decision.blocked_reason is None, decision.blocked_reason
        # All four checks are still recorded even though none applied.
        assert len(decision.guardrail_checks) == 4


# ------------------------------------------------------------- deferrals


def test_quiet_hours_defers_rather_than_abandons() -> None:
    """The distinction that stops recoverable revenue being discarded."""
    decision = decide(RootCause.CARD_EXPIRED, context=clean_context(at_ist(3)))
    assert decision.blocked_reason is None
    assert decision.scheduled_for is not None
    assert decision.scheduled_for.astimezone(IST).hour == 9
    assert decision.channel is Channel.WHATSAPP


def test_contact_frequency_defers_to_the_next_window() -> None:
    now = at_ist(12)
    context = DecisionContext(
        customer_timezone="Asia/Kolkata",
        first_failure_at=now - timedelta(hours=3),
        last_contact_at=now - timedelta(hours=2),
        now=now,
    )
    decision = decide(RootCause.SCA_ABANDONED, context=context)
    assert decision.blocked_reason is None
    assert decision.scheduled_for >= now + timedelta(hours=21)


def test_deferred_send_always_lands_inside_allowed_hours() -> None:
    for hour in range(24):
        decision = decide(RootCause.CHECKOUT_FRICTION, context=clean_context(at_ist(hour)))
        if decision.blocked_reason is not None:
            continue
        local = decision.scheduled_for.astimezone(IST).hour
        assert 9 <= local < 20, f"{hour}h -> {local}h"


def test_quiet_hours_does_not_delay_a_silent_retry() -> None:
    """Nobody is disturbed by a charge attempt at 3am.

    Deferring it would delay recovery for no compliance benefit, so the rule is
    recorded as failing but does not apply.
    """
    decision = decide(RootCause.INSUFFICIENT_FUNDS, context=clean_context(at_ist(3)))
    assert decision.blocked_reason is None
    assert decision.delay_seconds == 3 * 86400
    quiet = next(
        c for c in decision.guardrail_checks if c.name is GuardrailName.QUIET_HOURS
    )
    assert quiet.passed is False


def test_quiet_hours_does_not_delay_an_escalation() -> None:
    decision = decide(RootCause.UNKNOWN, context=clean_context(at_ist(3)))
    assert decision.blocked_reason is None
    assert decision.delay_seconds == 0


# ------------------------------------------------------- applicability rules


def test_contact_rules_apply_only_to_contacting_actions() -> None:
    from app.schemas import GuardrailCheck

    quiet = GuardrailCheck(name=GuardrailName.QUIET_HOURS, passed=False, detail="x")
    for action in CONTACTS_CUSTOMER:
        assert applies_to_action(quiet, action) is True
    for action in ATTEMPTS_CHARGE | INTERNAL_ONLY:
        assert applies_to_action(quiet, action) is False


def test_stopping_rules_apply_to_anything_with_an_external_effect() -> None:
    from app.schemas import GuardrailCheck

    stop = GuardrailCheck(name=GuardrailName.MAX_RETRIES, passed=False, detail="x")
    for action in CONTACTS_CUSTOMER | ATTEMPTS_CHARGE:
        assert applies_to_action(stop, action) is True
    for action in INTERNAL_ONLY:
        assert applies_to_action(stop, action) is False


def test_action_categories_cover_every_action() -> None:
    assert CONTACTS_CUSTOMER | ATTEMPTS_CHARGE | INTERNAL_ONLY == set(Action)
    assert not CONTACTS_CUSTOMER & ATTEMPTS_CHARGE
    assert not CONTACTS_CUSTOMER & INTERNAL_ONLY
    assert not ATTEMPTS_CHARGE & INTERNAL_ONLY


# ------------------------------------------------------------- audit trail


def test_audit_summary_records_the_decision_and_every_check() -> None:
    event = make_event()
    diagnosis = make_diagnosis(RootCause.CARD_EXPIRED)
    decision = decide_action(event, diagnosis, clean_context())
    inputs, outputs = audit_summaries(event, diagnosis, decision)

    assert inputs["root_cause"] == "card_expired"
    assert outputs["action"] == "send_update_payment_method_link"
    assert outputs["blocked"] is False
    assert len(outputs["guardrail_checks_passed"]) == 4


def test_audit_summary_explains_a_block() -> None:
    event = make_event(prior_attempts=3)
    diagnosis = make_diagnosis(RootCause.CARD_EXPIRED)
    decision = decide_action(event, diagnosis, clean_context())
    _, outputs = audit_summaries(event, diagnosis, decision)

    assert outputs["blocked"] is True
    assert "max_retries" in outputs["blocked_reason"]
    assert outputs["scheduled_for"] is None


def test_unknown_timezone_still_produces_a_decision() -> None:
    """A bad profile must not stop the pipeline; the assumption is recorded."""
    context = DecisionContext(
        customer_timezone="Not/AZone",
        first_failure_at=at_ist(12) - timedelta(hours=1),
        now=at_ist(12),
    )
    decision = decide(RootCause.CARD_EXPIRED, context=context)
    quiet = next(
        c for c in decision.guardrail_checks if c.name is GuardrailName.QUIET_HOURS
    )
    assert "assumption" in quiet.detail
