"""Guardrail tests.

``code-standards.md`` requires these tested independently of the DECIDE stage
that calls them, so nothing here imports ``app.decide``.

These four rules are the compliance surface. The headline claim is "0
stopping-rule violations, checked programmatically", and that is only worth
making if violations are impossible by construction rather than improbable in
practice. So the boundaries are tested explicitly, in both directions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app import guardrails
from app.config import Settings
from app.guardrails import (
    FALLBACK_TIMEZONE,
    KIND,
    GuardrailKind,
    check_contact_frequency,
    check_hard_stop,
    check_max_retries,
    check_quiet_hours,
    next_allowed_contact_time,
    resolve_timezone,
    run_all_checks,
)
from app.schemas import CustomerHistory, EventRecord, GuardrailName

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(autouse=True)
def _pinned_settings(monkeypatch):
    """Constraint #4's documented values, pinned so tests do not drift with .env."""
    settings = Settings(
        _env_file=None,
        max_recovery_attempts=3,
        min_hours_between_contacts=24,
        quiet_hours_start_local=9,
        quiet_hours_end_local=20,
        hard_stop_days=7,
    )
    monkeypatch.setattr("app.guardrails.get_settings", lambda: settings)
    return settings


def make_event(prior_attempts: int = 0, customer_id: str = "cust_1") -> EventRecord:
    return EventRecord(
        event_id="11111111-1111-5111-8111-111111111111",
        customer_id=customer_id,
        event_type="payment_failed",
        decline_code="insufficient_funds",
        amount=Decimal("499.00"),
        currency="INR",
        prior_attempts=prior_attempts,
        customer_history=CustomerHistory(tenure_days=100, past_failures=1),
        detected_at=datetime.now(UTC),
    )


def at_ist(hour: int, minute: int = 0, day: int = 15) -> datetime:
    """A UTC instant that is ``hour`` in Indian local time."""
    return datetime(2026, 6, day, hour, minute, tzinfo=IST).astimezone(UTC)


# ------------------------------------------------------------- max retries


@pytest.mark.parametrize(
    ("used", "expected"), [(0, True), (1, True), (2, True), (3, False), (4, False)]
)
def test_max_retries_boundary(used: int, expected: bool) -> None:
    """A limit of 3 means a 4th attempt must not happen.

    prior_attempts counts attempts already made, so reaching the limit fails.
    An off-by-one here is a compliance violation, not a rounding difference.
    """
    assert check_max_retries(make_event(prior_attempts=used)).passed is expected


def test_max_retries_detail_names_the_numbers() -> None:
    check = check_max_retries(make_event(prior_attempts=3))
    assert "3 of 3" in check.detail
    assert check.name is GuardrailName.MAX_RETRIES


# ------------------------------------------------------- contact frequency


def test_no_prior_contact_passes() -> None:
    check = check_contact_frequency("cust_1", None, datetime.now(UTC))
    assert check.passed is True
    assert "No prior contact" in check.detail


@pytest.mark.parametrize(
    ("hours_ago", "expected"),
    [(0.5, False), (12, False), (23.9, False), (24, True), (48, True)],
)
def test_contact_frequency_boundary(hours_ago: float, expected: bool) -> None:
    now = datetime.now(UTC)
    last = now - timedelta(hours=hours_ago)
    assert check_contact_frequency("cust_1", last, now).passed is expected


def test_contact_frequency_failure_states_when_it_reopens() -> None:
    """A deferral is only actionable if the trail says until when."""
    now = datetime.now(UTC)
    check = check_contact_frequency("cust_1", now - timedelta(hours=2), now)
    assert check.passed is False
    assert "deferring until" in check.detail


def test_contact_frequency_is_per_customer_not_per_event() -> None:
    """Someone with three failing subscriptions must not get three messages.

    The rule keys on the customer, so the same last-contact time governs every
    event belonging to them.
    """
    now = datetime.now(UTC)
    last = now - timedelta(hours=1)
    for event_customer in ("cust_a", "cust_a", "cust_a"):
        assert check_contact_frequency(event_customer, last, now).passed is False


# ------------------------------------------------------------- quiet hours


@pytest.mark.parametrize(
    ("local_hour", "expected"),
    [
        (0, False), (3, False), (8, False),
        (9, True), (12, True), (19, True),
        (20, False), (22, False), (23, False),
    ],
)
def test_quiet_hours_boundary_in_customer_local_time(
    local_hour: int, expected: bool
) -> None:
    """9am inclusive, 8pm exclusive. 20:00 is outside the window."""
    check = check_quiet_hours("Asia/Kolkata", at_ist(local_hour))
    assert check.passed is expected, check.detail


def test_quiet_hours_uses_the_customer_clock_not_the_server_clock() -> None:
    """The bug this prevents: a server at 2pm UTC contacting someone at 2am.

    The same instant is inside the window for one customer and outside for
    another, so evaluating in server time would breach the rule for anyone not
    colocated with the server.
    """
    # 15:00 UTC in June is 20:30 in IST (outside the window) but 16:00 in
    # London (inside it). One instant, opposite answers.
    instant = datetime(2026, 6, 15, 15, 0, tzinfo=UTC)
    assert check_quiet_hours("Asia/Kolkata", instant).passed is False
    assert check_quiet_hours("Europe/London", instant).passed is True


def test_unknown_timezone_falls_back_and_says_so() -> None:
    """A bad zone must not crash, and must not pretend to be local time either.

    Silently assuming a zone could contact someone overnight, so the assumption
    is recorded in the audit detail.
    """
    check = check_quiet_hours("Not/AZone", at_ist(3))
    assert check.passed is False
    assert FALLBACK_TIMEZONE in check.detail
    assert "assumption" in check.detail


def test_resolve_timezone_reports_whether_it_substituted() -> None:
    zone, fell_back = resolve_timezone("Asia/Kolkata")
    assert zone.key == "Asia/Kolkata" and fell_back is False
    zone, fell_back = resolve_timezone(None)
    assert zone.key == FALLBACK_TIMEZONE and fell_back is True
    zone, fell_back = resolve_timezone("")
    assert fell_back is True


# --------------------------------------------------------------- hard stop


@pytest.mark.parametrize(
    ("days", "expected"),
    [(0, True), (3, True), (6.9, True), (7, False), (10, False), (30, False)],
)
def test_hard_stop_boundary(days: float, expected: bool) -> None:
    now = datetime.now(UTC)
    assert check_hard_stop(now - timedelta(days=days), now).passed is expected


def test_hard_stop_measures_from_first_failure_not_latest() -> None:
    """Measuring from the most recent attempt would reset the window forever.

    A customer could then be chased indefinitely, one attempt at a time.
    """
    now = datetime.now(UTC)
    first = now - timedelta(days=9)
    latest = now - timedelta(hours=1)

    assert check_hard_stop(first, now).passed is False
    # Demonstrates the mistake: passing the latest attempt would wrongly pass.
    assert check_hard_stop(latest, now).passed is True


# ------------------------------------------------------ terminal vs deferrable


def test_every_guardrail_is_classified() -> None:
    """An unclassified rule would make DECIDE's stop-or-wait choice undefined."""
    assert set(KIND) == set(GuardrailName)


def test_stopping_rules_are_terminal_and_timing_rules_are_deferrable() -> None:
    """Quiet hours means "not now", not "not ever".

    Getting this backwards would either discard recoverable revenue or contact
    someone at 3am.
    """
    assert KIND[GuardrailName.MAX_RETRIES] is GuardrailKind.TERMINAL
    assert KIND[GuardrailName.HARD_STOP_7_DAYS] is GuardrailKind.TERMINAL
    assert KIND[GuardrailName.QUIET_HOURS] is GuardrailKind.DEFERRABLE
    assert KIND[GuardrailName.CONTACT_FREQUENCY] is GuardrailKind.DEFERRABLE


# ------------------------------------------------------------- run_all_checks


def test_all_four_checks_always_run() -> None:
    """Constraint #5: the trail must show each check happened, including passes."""
    checks = run_all_checks(
        make_event(),
        customer_timezone="Asia/Kolkata",
        last_contact_at=None,
        first_failure_at=datetime.now(UTC),
        now=at_ist(12),
    )
    assert {c.name for c in checks} == set(GuardrailName)
    assert all(c.passed for c in checks)
    assert all(c.detail.strip() for c in checks)


def test_passing_checks_are_still_reported() -> None:
    """The specific requirement in constraint #5.

    Reporting only failures would leave a reader unable to tell a check that
    passed from a check that never ran.
    """
    checks = run_all_checks(
        make_event(prior_attempts=3),
        customer_timezone="Asia/Kolkata",
        last_contact_at=None,
        first_failure_at=datetime.now(UTC),
        now=at_ist(12),
    )
    by_name = {c.name: c for c in checks}
    assert by_name[GuardrailName.MAX_RETRIES].passed is False
    assert by_name[GuardrailName.QUIET_HOURS].passed is True
    assert len(checks) == 4


def test_check_order_is_stable() -> None:
    """Fixed order keeps audit entries comparable between events."""
    kwargs = {
        "customer_timezone": "Asia/Kolkata",
        "last_contact_at": None,
        "first_failure_at": datetime.now(UTC),
        "now": at_ist(12),
    }
    first = [c.name for c in run_all_checks(make_event(), **kwargs)]
    second = [c.name for c in run_all_checks(make_event(), **kwargs)]
    assert first == second


def test_multiple_simultaneous_failures_are_all_reported() -> None:
    now = at_ist(3)
    checks = run_all_checks(
        make_event(prior_attempts=5),
        customer_timezone="Asia/Kolkata",
        last_contact_at=now - timedelta(hours=1),
        first_failure_at=now - timedelta(days=10),
        now=now,
    )
    assert len(guardrails.failed(checks)) == 4
    assert len(guardrails.terminal_failures(checks)) == 2
    assert len(guardrails.deferrable_failures(checks)) == 2


# ------------------------------------------------- next allowed contact time


def test_defers_overnight_to_the_morning() -> None:
    scheduled = next_allowed_contact_time(
        customer_timezone="Asia/Kolkata", last_contact_at=None, now=at_ist(3)
    )
    assert scheduled.astimezone(IST).hour == 9
    assert scheduled.astimezone(IST).date() == at_ist(3).astimezone(IST).date()


def test_defers_late_evening_to_the_next_morning() -> None:
    now = at_ist(22)
    scheduled = next_allowed_contact_time(
        customer_timezone="Asia/Kolkata", last_contact_at=None, now=now
    )
    local = scheduled.astimezone(IST)
    assert local.hour == 9
    assert local.date() > now.astimezone(IST).date()


def test_inside_the_window_schedules_immediately() -> None:
    now = at_ist(14)
    assert next_allowed_contact_time(
        customer_timezone="Asia/Kolkata", last_contact_at=None, now=now
    ) == now


def test_frequency_window_is_satisfied_before_quiet_hours() -> None:
    """Order matters.

    Applying quiet hours first can produce a time inside allowed hours that is
    still too soon after the last contact, which would breach the frequency rule.
    """
    now = at_ist(10)
    last = now - timedelta(hours=2)
    scheduled = next_allowed_contact_time(
        customer_timezone="Asia/Kolkata", last_contact_at=last, now=now
    )
    assert scheduled >= last + timedelta(hours=24)
    local = scheduled.astimezone(IST)
    assert 9 <= local.hour < 20


def test_deferred_time_always_lands_inside_allowed_hours() -> None:
    """Whatever the combination, the result must be contactable."""
    for hour in range(24):
        for last_offset in (None, 1, 12, 23):
            now = at_ist(hour)
            last = None if last_offset is None else now - timedelta(hours=last_offset)
            scheduled = next_allowed_contact_time(
                customer_timezone="Asia/Kolkata", last_contact_at=last, now=now
            )
            local_hour = scheduled.astimezone(IST).hour
            assert 9 <= local_hour < 20, f"{hour}h/{last_offset} -> {local_hour}h"
            assert scheduled >= now


def test_deferred_time_respects_a_non_indian_timezone() -> None:
    now = datetime(2026, 6, 15, 2, 0, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)
    scheduled = next_allowed_contact_time(
        customer_timezone="America/New_York", last_contact_at=None, now=now
    )
    assert scheduled.astimezone(ZoneInfo("America/New_York")).hour == 9
