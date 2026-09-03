"""Payday-aware retry timing (Phase 8 stretch goal).

``architecture.md`` asks for the insufficient-funds retry to be "payday-aware if
data available". These tests care about the *if available* half as much as the
arithmetic:

* **With no payday on record the behaviour is the flat interval, unchanged.** That
  is what runs for essentially every customer, because nothing in this system
  infers a payday and no demo data supplies one. A stretch goal that quietly
  changed the default path would be a regression dressed as a feature.
* **The retry lands the day after payday, not on it.** Salary credited on the 1st
  is not reliably spendable at 00:01, and an early retry burns one of only three
  permitted attempts.
* **A payday beyond the hard stop is ignored.** Targeting a date after the 7-day
  window closes would schedule a retry that can never run, which is worse than a
  flat interval that at least fires.
* **The date is the customer's local date.** Same reasoning as quiet hours: our
  clock says nothing about what day it is for them.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app import decide as decide_module
from app.config import Settings
from app.decide import (
    PAYDAY_BUFFER_DAYS,
    DecisionContext,
    _days_until_day_of_month,
    _retry_delay_days,
    action_table,
    decide_action,
)
from app.schemas import (
    Action,
    CustomerHistory,
    Diagnosis,
    EventRecord,
    EventType,
    RootCause,
)

# The 10th of the month, mid-afternoon UTC.
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
FLAT_DAYS = 3
HARD_STOP_DAYS = 7


@pytest.fixture(autouse=True)
def _pinned_settings(monkeypatch):
    settings = Settings(
        _env_file=None,
        max_recovery_attempts=3,
        min_hours_between_contacts=24,
        quiet_hours_start_local=9,
        quiet_hours_end_local=20,
        hard_stop_days=HARD_STOP_DAYS,
        insufficient_funds_retry_days=FLAT_DAYS,
    )
    monkeypatch.setattr("app.guardrails.get_settings", lambda: settings)
    monkeypatch.setattr("app.decide.get_settings", lambda: settings)
    return settings


# ------------------------------------------------------- the date arithmetic


@pytest.mark.parametrize(
    ("today", "payday", "expected"),
    [
        # Later this month.
        (date(2026, 9, 10), 11, 1),
        (date(2026, 9, 10), 20, 10),
        (date(2026, 9, 10), 30, 20),
        # Today already is payday, so the next one is next month.
        (date(2026, 9, 10), 10, 30),
        # Already past it this month. September has 30 days: 10 to month end, +5.
        (date(2026, 9, 20), 5, 15),
        # Year rollover. December has 31 days: 11 to month end, +5.
        (date(2026, 12, 20), 5, 16),
    ],
)
def test_days_until_the_next_payday(today: date, payday: int, expected: int) -> None:
    assert _days_until_day_of_month(today, payday) == expected


@pytest.mark.parametrize(
    ("today", "payday", "expected"),
    [
        # September has 30 days, so the 31st means the 30th.
        (date(2026, 9, 10), 31, 20),
        # February 2026 has 28 days.
        (date(2026, 2, 5), 31, 23),
        (date(2026, 2, 5), 30, 23),
        # Last day of a 31-day month, targeting the 31st: next stop is Feb 28.
        (date(2026, 1, 31), 31, 28),
    ],
)
def test_a_payday_later_than_the_month_clamps_to_its_last_day(
    today: date, payday: int, expected: int
) -> None:
    """A payday of the 31st must resolve in February rather than raising."""
    assert _days_until_day_of_month(today, payday) == expected


def test_every_day_of_month_resolves_in_every_month() -> None:
    """No combination may raise. A metrics or decision run must not die on one."""
    for month in range(1, 13):
        for day in range(1, 32):
            result = _days_until_day_of_month(date(2026, month, 15), day)
            assert result > 0


# ----------------------------------------------------------- the delay itself


def test_no_payday_on_record_uses_the_flat_interval() -> None:
    """The path that runs for almost every customer."""
    assert _retry_delay_days(None, now=NOW) == timedelta(days=FLAT_DAYS)


def test_a_known_payday_can_retry_sooner_than_the_flat_interval() -> None:
    """The point of knowing the payday: on the 11th, retry on the 12th."""
    delay = _retry_delay_days(11, now=NOW, customer_timezone="UTC")

    assert delay == timedelta(days=1 + PAYDAY_BUFFER_DAYS)
    assert delay < timedelta(days=FLAT_DAYS)


def test_the_retry_lands_the_day_after_payday() -> None:
    delay = _retry_delay_days(14, now=NOW, customer_timezone="UTC")

    # 10th -> 14th is 4 days, plus the buffer.
    assert delay == timedelta(days=4 + PAYDAY_BUFFER_DAYS)


def test_a_payday_beyond_the_hard_stop_falls_back_to_the_flat_interval() -> None:
    """Scheduling past the closing window would be a retry that never runs."""
    # 10th -> 17th is 7 days, so with the buffer it lands outside a 7-day stop.
    delay = _retry_delay_days(17, now=NOW, customer_timezone="UTC")

    assert delay == timedelta(days=FLAT_DAYS)


def test_a_payday_today_falls_back_because_the_next_one_is_next_month() -> None:
    delay = _retry_delay_days(10, now=NOW, customer_timezone="UTC")

    assert delay == timedelta(days=FLAT_DAYS)


def test_the_delay_is_never_zero_or_negative() -> None:
    for payday in range(1, 32):
        delay = _retry_delay_days(payday, now=NOW, customer_timezone="UTC")
        assert delay > timedelta(0)


def test_the_payday_is_read_in_the_customers_local_date() -> None:
    """Same reasoning as quiet hours: our date is not necessarily their date.

    20:00 UTC is already past midnight in Kolkata, so the local date is the 11th
    while ours is still the 10th. A payday on the 12th is therefore one day away
    for them and two for us.
    """
    late = datetime(2026, 9, 10, 20, 0, tzinfo=UTC)

    utc_delay = _retry_delay_days(12, now=late, customer_timezone="UTC")
    ist_delay = _retry_delay_days(12, now=late, customer_timezone="Asia/Kolkata")

    assert utc_delay == timedelta(days=2 + PAYDAY_BUFFER_DAYS)
    assert ist_delay == timedelta(days=1 + PAYDAY_BUFFER_DAYS)


def test_an_unresolvable_timezone_does_not_crash_the_decision() -> None:
    delay = _retry_delay_days(11, now=NOW, customer_timezone="Mars/Olympus_Mons")

    assert delay > timedelta(0)


# ------------------------------------------------- through the action table


def context(payday: int | None) -> DecisionContext:
    return DecisionContext(
        customer_timezone="UTC",
        first_failure_at=NOW - timedelta(hours=1),
        last_contact_at=None,
        now=NOW,
        payday_day_of_month=payday,
    )


def test_the_action_table_without_a_context_still_works() -> None:
    """The contract test compares the table against the doc with no context."""
    plan = action_table()[RootCause.INSUFFICIENT_FUNDS]

    assert plan.delay == timedelta(days=FLAT_DAYS)
    assert plan.action is Action.SCHEDULE_RETRY


def test_the_action_table_uses_the_payday_when_one_is_supplied() -> None:
    plan = action_table(context(11))[RootCause.INSUFFICIENT_FUNDS]

    assert plan.delay == timedelta(days=1 + PAYDAY_BUFFER_DAYS)


def test_a_payday_changes_nothing_for_any_other_root_cause() -> None:
    """It is an insufficient-funds refinement, not a global timing change."""
    with_payday = action_table(context(11))
    without = action_table(context(None))

    for cause in RootCause:
        if cause is RootCause.INSUFFICIENT_FUNDS:
            continue
        assert with_payday[cause] == without[cause]


# ------------------------------------------------------ through the decision


def event() -> EventRecord:
    return EventRecord(
        event_id="11111111-1111-5111-8111-111111111111",
        customer_id="cust_payday",
        event_type=EventType.PAYMENT_FAILED,
        decline_code="insufficient_funds",
        amount=Decimal("499.00"),
        currency="INR",
        prior_attempts=0,
        customer_history=CustomerHistory(tenure_days=200, past_failures=1),
        detected_at=NOW,
    )


def diagnosis() -> Diagnosis:
    return Diagnosis(
        event_id="11111111-1111-5111-8111-111111111111",
        root_cause=RootCause.INSUFFICIENT_FUNDS,
        confidence=0.95,
        reasoning="The issuer reported insufficient funds.",
    )


def test_the_decision_schedules_the_retry_for_the_day_after_payday() -> None:
    decision = decide_action(event(), diagnosis(), context(11))

    assert decision.action is Action.SCHEDULE_RETRY
    assert decision.scheduled_for == NOW + timedelta(days=1 + PAYDAY_BUFFER_DAYS)
    assert decision.delay_seconds == int(
        timedelta(days=1 + PAYDAY_BUFFER_DAYS).total_seconds()
    )


def test_the_decision_falls_back_cleanly_with_no_payday() -> None:
    decision = decide_action(event(), diagnosis(), context(None))

    assert decision.scheduled_for == NOW + timedelta(days=FLAT_DAYS)


def test_a_payday_retry_still_submits_no_charge() -> None:
    """Constraint #6 is untouched by better timing: DECIDE records a due time."""
    decision = decide_action(event(), diagnosis(), context(11))

    assert decision.action is Action.SCHEDULE_RETRY
    assert str(decision.channel) == "none"


def test_the_decision_stays_deterministic_with_a_payday() -> None:
    first = decide_action(event(), diagnosis(), context(11))
    second = decide_action(event(), diagnosis(), context(11))

    assert first == second


def test_decide_still_contains_no_llm_call_after_the_stretch_goal() -> None:
    """The property that makes a violation count meaningful must survive Phase 8."""
    import inspect

    source = inspect.getsource(decide_module)

    for symbol in ("gemini", "genai", "diagnose_root_cause", "build_client"):
        assert symbol not in source.lower()
