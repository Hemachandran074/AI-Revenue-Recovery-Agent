"""Phase 6 metrics tests.

The tests that matter most here are the ones proving the violation detector can
actually FIND a violation. "0 violations" is a headline claim, and a checker that
returns an empty list because it is broken looks identical to a clean batch. So
every rule gets a case that breaches it and a case that does not, and the
breaching cases are built from raw data rather than by setting a flag — the whole
point of re-derivation is that it does not trust the recorded flags.

The rest covers the arithmetic that could quietly mislead: an empty batch must not
report 0% recovery as though it had tried, a dry run must not count as recovered,
and a guardrail failing on an action that contacts nobody must not count as a
violation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app import metrics as metrics_module
from app.config import Settings
from app.metrics import (
    LATENCY_BUDGET_MS,
    AuditCoverage,
    EventRow,
    LatencyStats,
    MoneyMetrics,
    audit_coverage,
    find_violations,
    format_report,
)
from app.schemas import Action, GuardrailName

NOW = datetime(2026, 6, 15, 12, 30, tzinfo=UTC)  # 18:00 IST, inside 09:00-20:00
ALL_GUARDRAILS = [
    {"name": str(name), "passed": True, "detail": "ok"} for name in GuardrailName
]


@pytest.fixture(autouse=True)
def _pin_settings(monkeypatch):
    """Guardrail thresholds fixed in the test, not read from a local ``.env``."""
    settings = Settings(
        _env_file=None,
        max_recovery_attempts=3,
        min_hours_between_contacts=24,
        quiet_hours_start_local=9,
        quiet_hours_end_local=20,
        hard_stop_days=7,
    )
    monkeypatch.setattr(metrics_module, "get_settings", lambda: settings)
    return settings


def row(**overrides: Any) -> EventRow:
    """A clean, fully-processed, contacted event that breaches nothing."""
    base: dict[str, Any] = {
        "event_id": "evt-0001",
        "customer_id": "cust-1",
        "event_type": "payment_failed",
        "decline_code": "card_expired",
        "amount_minor": 49900,
        "currency": "INR",
        "prior_attempts": 0,
        "first_failure_at": NOW - timedelta(hours=1),
        "received_at": NOW,
        "customer_timezone": "Asia/Kolkata",
        "root_cause": "card_expired",
        "confidence": 0.95,
        "reasoning": "The card has expired.",
        "classifier_unavailable": False,
        "action": str(Action.SEND_UPDATE_PAYMENT_METHOD_LINK),
        "channel": "whatsapp",
        "scheduled_for": NOW,
        "blocked_reason": None,
        "guardrail_checks": list(ALL_GUARDRAILS),
        "delivery_status": "sent",
        "customer_outcome": "pending",
        "amount_recovered_minor": None,
        "executed_at": NOW,
        "decision_latency_ms": 120.0,
        "send_latency_ms": 350.0,
        "stages": ["detect", "diagnose", "decide", "execute"],
    }
    base.update(overrides)
    return EventRow(**base)


# ------------------------------------------------- the detector can detect


def test_a_clean_contacted_event_is_not_a_violation() -> None:
    assert find_violations([row()]) == []


def test_quiet_hours_breach_is_caught() -> None:
    """21:30 UTC is 03:00 next day in Kolkata."""
    sent_at = datetime(2026, 6, 15, 21, 30, tzinfo=UTC)

    found = find_violations(
        [row(executed_at=sent_at, first_failure_at=sent_at - timedelta(hours=1))]
    )

    assert [v.rule for v in found] == [str(GuardrailName.QUIET_HOURS)]
    assert "03:00" in found[0].detail


def test_quiet_hours_is_evaluated_in_the_customers_timezone_not_utc() -> None:
    """The same instant is a breach for one customer and fine for another.

    This is the test that fails if the check ever compares UTC hours. 03:30 UTC is
    09:00 in Kolkata, right at the start of the allowed window, and 23:30 the
    previous evening in New York, which is not allowed.
    """
    sent_at = datetime(2026, 6, 15, 3, 30, tzinfo=UTC)
    kwargs = {
        "executed_at": sent_at,
        "scheduled_for": sent_at,
        "first_failure_at": sent_at - timedelta(hours=1),
    }

    kolkata = find_violations([row(customer_timezone="Asia/Kolkata", **kwargs)])
    new_york = find_violations([row(customer_timezone="America/New_York", **kwargs)])

    assert kolkata == []
    assert [v.rule for v in new_york] == [str(GuardrailName.QUIET_HOURS)]
    assert "23:30" in new_york[0].detail


def test_an_unresolvable_timezone_falls_back_instead_of_crashing() -> None:
    """A metrics run must not die because one customer row holds a bad zone."""
    found = find_violations([row(customer_timezone="Mars/Olympus_Mons")])

    assert found == []


def test_hard_stop_breach_is_caught() -> None:
    found = find_violations([row(first_failure_at=NOW - timedelta(days=8))])

    assert [v.rule for v in found] == [str(GuardrailName.HARD_STOP_7_DAYS)]
    assert "8.0 days" in found[0].detail


def test_hard_stop_boundary_is_a_breach_at_exactly_seven_days() -> None:
    """The limit is a stop, not a target. Day 7 is already outside the window."""
    found = find_violations([row(first_failure_at=NOW - timedelta(days=7))])

    assert [v.rule for v in found] == [str(GuardrailName.HARD_STOP_7_DAYS)]


def test_hard_stop_just_inside_the_window_is_clean() -> None:
    found = find_violations(
        [row(first_failure_at=NOW - timedelta(days=6, hours=23))]
    )

    assert found == []


def test_max_retries_breach_is_caught() -> None:
    found = find_violations([row(prior_attempts=3)])

    assert [v.rule for v in found] == [str(GuardrailName.MAX_RETRIES)]


def test_max_retries_boundary_allows_the_third_attempt() -> None:
    """A limit of 3 permits attempts 1, 2 and 3, so 2 prior attempts is fine."""
    assert find_violations([row(prior_attempts=2)]) == []


def test_contact_frequency_breach_is_caught_across_two_events() -> None:
    """Cross-event by nature, which is why re-deriving it is worth doing.

    A per-event check cannot see that the same person was messaged twice.
    """
    # An hour apart, and both still inside the 09:00-20:00 window in IST, so
    # contact frequency is the only rule in play.
    later = NOW + timedelta(hours=1)
    first = row(event_id="evt-a", executed_at=NOW)
    second = row(event_id="evt-b", executed_at=later, scheduled_for=later)

    found = find_violations([first, second])

    assert [v.rule for v in found] == [str(GuardrailName.CONTACT_FREQUENCY)]
    assert found[0].event_id == "evt-b"
    assert "1.0h" in found[0].detail
    assert "evt-a" in found[0].detail


def test_two_contacts_more_than_a_day_apart_are_clean() -> None:
    first = row(event_id="evt-a", executed_at=NOW)
    second = row(
        event_id="evt-b",
        executed_at=NOW + timedelta(hours=25),
        first_failure_at=NOW + timedelta(hours=24),
    )

    assert find_violations([first, second]) == []


def test_contact_frequency_is_per_customer_not_global() -> None:
    """Two different people messaged minutes apart is normal operation."""
    first = row(event_id="evt-a", customer_id="cust-1", executed_at=NOW)
    second = row(
        event_id="evt-b",
        customer_id="cust-2",
        executed_at=NOW + timedelta(minutes=5),
    )

    assert find_violations([first, second]) == []


def test_a_blocked_decision_that_still_sent_is_caught() -> None:
    found = find_violations(
        [row(blocked_reason="Stopped by hard_stop_7_days", delivery_status="sent")]
    )

    assert "blocked_but_sent" in {v.rule for v in found}


def test_sending_before_the_due_time_is_caught() -> None:
    """The end-to-end form of the deferral bug fixed in session 14.

    DECIDE moving a contact to a later window is only worth something if the send
    actually waited, and that is a property of the pair of stages, not of either.
    """
    found = find_violations(
        [row(executed_at=NOW, scheduled_for=NOW + timedelta(hours=9))]
    )

    assert [v.rule for v in found] == ["sent_before_due"]


def test_sending_at_or_after_the_due_time_is_clean() -> None:
    assert find_violations([row(executed_at=NOW, scheduled_for=NOW)]) == []
    assert (
        find_violations(
            [row(executed_at=NOW, scheduled_for=NOW - timedelta(hours=2))]
        )
        == []
    )


def test_a_missing_guardrail_result_is_caught() -> None:
    """Constraint #5: the trail must show every check happened, pass or fail."""
    partial = [
        check
        for check in ALL_GUARDRAILS
        if check["name"] != str(GuardrailName.QUIET_HOURS)
    ]

    found = find_violations([row(guardrail_checks=partial)])

    assert [v.rule for v in found] == ["guardrail_checks_incomplete"]
    assert "quiet_hours" in found[0].detail


def test_no_guardrail_results_at_all_is_caught() -> None:
    found = find_violations([row(guardrail_checks=[])])

    assert [v.rule for v in found] == ["guardrail_checks_incomplete"]


def test_an_undecided_event_is_not_faulted_for_missing_checks() -> None:
    """DECIDE never ran, so there is nothing it failed to record."""
    found = find_violations(
        [row(action=None, guardrail_checks=[], delivery_status=None, executed_at=None)]
    )

    assert found == []


def test_violations_are_detected_without_reading_the_recorded_flags() -> None:
    """The checker must not be able to be talked out of a real breach.

    Here every recorded flag says the guardrails passed, while the raw data shows
    a contact 9 days after the first failure. Trusting the flags would report a
    clean batch, which is exactly the failure mode re-derivation exists to stop.
    """
    lying = row(
        first_failure_at=NOW - timedelta(days=9),
        guardrail_checks=[
            {"name": str(name), "passed": True, "detail": "all good, honestly"}
            for name in GuardrailName
        ],
    )

    found = find_violations([lying])

    assert [v.rule for v in found] == [str(GuardrailName.HARD_STOP_7_DAYS)]


def test_multiple_breaches_on_one_event_are_all_reported() -> None:
    found = find_violations(
        [row(first_failure_at=NOW - timedelta(days=9), prior_attempts=5)]
    )

    assert {v.rule for v in found} == {
        str(GuardrailName.HARD_STOP_7_DAYS),
        str(GuardrailName.MAX_RETRIES),
    }


# --------------------------- a rule only binds when somebody was contacted


@pytest.mark.parametrize(
    "action",
    [str(Action.SCHEDULE_RETRY), str(Action.ESCALATE_TO_HUMAN_REVIEW)],
)
def test_a_stale_event_that_contacted_nobody_is_not_a_violation(action: str) -> None:
    """The stopping rules protect people from being disturbed.

    An internal escalation or a silent provider-side retry on a 9-day-old event
    disturbs no one, and counting it would make correct behaviour look unsafe.
    """
    found = find_violations(
        [
            row(
                action=action,
                channel="none",
                delivery_status="skipped",
                first_failure_at=NOW - timedelta(days=9),
                prior_attempts=5,
            )
        ]
    )

    assert found == []


def test_a_skipped_send_at_3am_is_not_a_violation() -> None:
    """Nothing was delivered, so quiet hours were not broken. That is the fix
    from session 14 working: the send was held rather than sent."""
    sent_at = datetime(2026, 6, 15, 21, 30, tzinfo=UTC)

    found = find_violations(
        [
            row(
                executed_at=sent_at,
                delivery_status="skipped",
                skip_reason="Deferred until 2026-06-16T03:30:00+00:00: ...",
            )
        ]
    )

    assert found == []


def test_a_failed_dispatch_is_not_a_violation() -> None:
    found = find_violations(
        [
            row(
                delivery_status="failed",
                failure_reason="HTTPError: 503",
                first_failure_at=NOW - timedelta(days=9),
            )
        ]
    )

    assert found == []


# ------------------------------------------------------------- disposition


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "contacted"),
        (
            {"delivery_status": "failed", "failure_reason": "boom"},
            "dispatch_failed",
        ),
        (
            {"blocked_reason": "Stopped by max_retries", "delivery_status": "skipped"},
            "withheld_by_guardrail",
        ),
        (
            {"classifier_unavailable": True, "root_cause": "unknown"},
            "classifier_unavailable",
        ),
        (
            {
                "action": str(Action.SCHEDULE_RETRY),
                "delivery_status": "skipped",
                "skip_reason": "Scheduled for later. No charge submitted.",
            },
            "retry_scheduled",
        ),
        (
            {
                "action": str(Action.ESCALATE_TO_HUMAN_REVIEW),
                "delivery_status": "skipped",
                "skip_reason": "Queued for human review.",
            },
            "escalated_to_human",
        ),
        (
            {
                "delivery_status": "skipped",
                "skip_reason": "Deferred until 2026-06-16T03:30:00+00:00: ...",
            },
            "deferred_to_allowed_window",
        ),
        (
            {"action": None, "delivery_status": None, "root_cause": None},
            "not_processed",
        ),
    ],
)
def test_disposition_buckets(overrides: dict[str, Any], expected: str) -> None:
    assert row(**overrides).disposition == expected


def test_an_outage_outranks_the_escalation_it_produced() -> None:
    """Known issue M. An operational failure must stay distinguishable from a
    judgement, or the batch reports an outage as cautious behaviour."""
    outage = row(
        classifier_unavailable=True,
        root_cause="unknown",
        action=str(Action.ESCALATE_TO_HUMAN_REVIEW),
        delivery_status="skipped",
    )

    assert outage.disposition == "classifier_unavailable"


def test_a_classifier_outage_is_not_counted_as_actioned() -> None:
    assert row(classifier_unavailable=True).is_actioned is False


def test_a_guardrail_stop_is_not_counted_as_actioned() -> None:
    assert row(blocked_reason="Stopped by max_retries").is_actioned is False


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {
            "action": str(Action.SCHEDULE_RETRY),
            "delivery_status": "skipped",
            "skip_reason": "Scheduled. No charge submitted.",
        },
    ],
)
def test_contact_and_scheduled_retry_both_count_as_actioned(
    overrides: dict[str, Any],
) -> None:
    """A scheduled retry is a recovery in motion even though nobody was messaged."""
    assert row(**overrides).is_actioned is True


# ------------------------------------------------------------------- money


def test_recovery_rate_is_none_when_nothing_was_at_risk() -> None:
    """Not 0.0. "0% of nothing" and "0% of what was at stake" differ."""
    money = MoneyMetrics(
        at_risk_minor=0, recovered_minor=0, actioned_at_risk_minor=0
    )

    assert money.recovery_rate is None
    assert money.actioned_recovery_rate is None


def test_recovery_rate_uses_both_denominators() -> None:
    money = MoneyMetrics(
        at_risk_minor=100_000, recovered_minor=25_000, actioned_at_risk_minor=50_000
    )

    assert money.recovery_rate == 0.25
    assert money.actioned_recovery_rate == 0.5


def test_money_renders_major_units_from_minor() -> None:
    money = MoneyMetrics(
        at_risk_minor=13_177_600, recovered_minor=0, actioned_at_risk_minor=0
    )

    assert money.to_dict()["at_risk"] == 131776.0


# ---------------------------------------------------------------- latency


def test_latency_stats_on_an_empty_batch_report_no_data() -> None:
    """Zeros would claim instant processing of a batch that never ran."""
    stats = LatencyStats.of([])

    assert stats.count == 0
    assert stats.mean_ms is None
    assert stats.max_ms is None
    assert stats.over_budget == 0


def test_latency_stats_summarise_a_distribution() -> None:
    stats = LatencyStats.of([100.0, 200.0, 300.0, 400.0])

    assert stats.count == 4
    assert stats.mean_ms == 250.0
    assert stats.max_ms == 400.0
    assert stats.p50_ms == 200.0


def test_latency_counts_events_over_the_sixty_second_budget() -> None:
    stats = LatencyStats.of([100.0, LATENCY_BUDGET_MS + 1, LATENCY_BUDGET_MS * 3])

    assert stats.over_budget == 2


def test_a_latency_exactly_at_the_budget_is_not_over_it() -> None:
    assert LatencyStats.of([LATENCY_BUDGET_MS]).over_budget == 0


def test_percentile_is_nearest_rank() -> None:
    values = [float(n) for n in range(1, 101)]

    assert metrics_module._percentile(values, 50) == 50.0
    assert metrics_module._percentile(values, 95) == 95.0
    assert metrics_module._percentile(values, 100) == 100.0


def test_percentile_handles_a_single_value() -> None:
    assert metrics_module._percentile([42.0], 95) == 42.0


# --------------------------------------------------------- audit coverage


def test_audit_coverage_counts_only_events_with_all_four_stages() -> None:
    complete = row(event_id="evt-a")
    partial = row(event_id="evt-b", stages=["detect", "diagnose"])

    coverage = audit_coverage([complete, partial])

    assert coverage.events == 2
    assert coverage.fully_covered == 1
    assert coverage.rate == 0.5
    assert coverage.incomplete[0]["event_id"] == "evt-b"
    assert coverage.incomplete[0]["missing"] == ["decide", "execute"]


def test_audit_coverage_of_an_empty_batch_is_unknown_not_perfect() -> None:
    coverage = audit_coverage([])

    assert coverage.rate is None
    assert coverage.events == 0


def test_full_coverage_reports_one() -> None:
    coverage = audit_coverage([row(event_id="a"), row(event_id="b")])

    assert coverage.rate == 1.0
    assert coverage.incomplete == []


def test_extra_stage_entries_still_count_as_covered() -> None:
    """A redelivery can add a second detect entry; that is not a coverage gap."""
    coverage = audit_coverage(
        [row(stages=["detect", "detect", "diagnose", "decide", "execute"])]
    )

    assert coverage.fully_covered == 1


# ---------------------------------------------------------------- report


def test_report_states_zero_violations_plainly() -> None:
    batch = metrics_module.BatchMetrics(
        events_total=1,
        money=MoneyMetrics(49900, 0, 49900),
        decision_latency=LatencyStats.of([120.0]),
        send_latency=LatencyStats.of([350.0]),
        violations=[],
        audit=audit_coverage([row()]),
        disposition={"contacted": 1},
        by_root_cause={"card_expired": 1},
        by_action={str(Action.SEND_UPDATE_PAYMENT_METHOD_LINK): 1},
        by_delivery_status={"sent": 1},
        by_customer_outcome={"pending": 1},
        by_event_type={"payment_failed": 1},
        classifier_unavailable=0,
        guardrail_config={},
    )

    text = format_report(batch)

    assert "VIOLATIONS" in text
    assert "recovery rate" in text
    assert "0.0%" in text
    assert "100.0%" in text  # audit coverage


def test_report_lists_each_violation_with_its_rule() -> None:
    found = find_violations([row(first_failure_at=NOW - timedelta(days=9))])
    batch = metrics_module.BatchMetrics(
        events_total=1,
        money=MoneyMetrics(49900, 0, 49900),
        decision_latency=LatencyStats.of([]),
        send_latency=LatencyStats.of([]),
        violations=found,
        audit=audit_coverage([row()]),
        disposition={},
        by_root_cause={},
        by_action={},
        by_delivery_status={},
        by_customer_outcome={},
        by_event_type={},
        classifier_unavailable=0,
        guardrail_config={},
    )

    text = format_report(batch)

    assert "hard_stop_7_days" in text
    assert "n/a" in text  # no latency measured, not "0ms"


def test_report_flags_classifier_outages_as_operational() -> None:
    batch = metrics_module.BatchMetrics(
        events_total=2,
        money=MoneyMetrics(99800, 0, 0),
        decision_latency=LatencyStats.of([120.0]),
        send_latency=LatencyStats.of([350.0]),
        violations=[],
        audit=AuditCoverage(events=2, fully_covered=2),
        disposition={"classifier_unavailable": 2},
        by_root_cause={"unknown": 2},
        by_action={str(Action.ESCALATE_TO_HUMAN_REVIEW): 2},
        by_delivery_status={"skipped": 2},
        by_customer_outcome={"pending": 2},
        by_event_type={"payment_failed": 2},
        classifier_unavailable=2,
        guardrail_config={},
    )

    text = format_report(batch)

    assert "operational failure, not a diagnosis" in text
