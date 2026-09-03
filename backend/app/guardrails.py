"""Guardrails — the stopping rules from Non-negotiable constraint #4.

Each rule is a real function returning a :class:`~app.schemas.GuardrailCheck`
(pass/fail plus a human-readable reason) — never a comment saying "remember to
check this" (``code-standards.md`` -> Guardrails are code, not comments).

The four rules:
  1. ``check_max_retries``      — max 3 recovery attempts per event
  2. ``check_contact_frequency``— max 1 contact per 24h per customer
  3. ``check_quiet_hours``      — no contact outside 9am-8pm customer local time
  4. ``check_hard_stop``        — hard stop 7 days after first failure

Thresholds come from :meth:`app.config.Settings.guardrail_config` so they are
visible in the audit trail. They exist to be enforced, not tuned to make a demo
look better.

## Two rules stop, two rules defer

Treating all four as blocks would throw away recoverable revenue. Quiet hours
does not mean "never contact this customer", it means "not at 3am" — the correct
response is to schedule for the next allowed window. Same for contact frequency:
"not yet" rather than "not ever".

  ``max_retries``      terminal. We have tried enough.
  ``hard_stop``        terminal. The recovery window has closed.
  ``quiet_hours``      deferrable. Send at the next allowed local hour.
  ``contact_frequency``deferrable. Send once the window reopens.

:attr:`GuardrailKind` records which is which, so DECIDE cannot accidentally
treat a deferral as an abandonment.

## Why every check runs even when it cannot matter

Constraint #5 requires every result logged, including passes. So all four always
run and all four are always recorded. Whether a *failure* affects the chosen
action is a separate question, answered by ``applies_to_action`` in
``decide.py``: quiet hours governs contacting a human, so it has no bearing on a
silent provider-side retry. Skipping the check entirely would leave a hole in the
audit trail; running it and recording that it did not apply does not.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import get_settings
from app.schemas import EventRecord, GuardrailCheck, GuardrailName

# Used when a customer profile has no usable timezone. Recorded in the check
# detail so the audit trail shows the evaluation rested on an assumption.
FALLBACK_TIMEZONE = "Asia/Kolkata"


class GuardrailKind(StrEnum):
    """Whether failing this rule stops recovery or merely delays it."""

    TERMINAL = "terminal"
    DEFERRABLE = "deferrable"


KIND: dict[GuardrailName, GuardrailKind] = {
    GuardrailName.MAX_RETRIES: GuardrailKind.TERMINAL,
    GuardrailName.HARD_STOP_7_DAYS: GuardrailKind.TERMINAL,
    GuardrailName.QUIET_HOURS: GuardrailKind.DEFERRABLE,
    GuardrailName.CONTACT_FREQUENCY: GuardrailKind.DEFERRABLE,
}


def resolve_timezone(customer_timezone: str | None) -> tuple[ZoneInfo, bool]:
    """Return the customer's zone, plus whether a fallback was substituted.

    An unknown zone must not crash the pipeline, but it must also not silently
    pretend to be the customer's local time — quiet hours would then be evaluated
    against the wrong clock and could contact someone overnight.
    """
    if customer_timezone:
        try:
            return ZoneInfo(customer_timezone), False
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return ZoneInfo(FALLBACK_TIMEZONE), True


def check_max_retries(event: EventRecord) -> GuardrailCheck:
    """Fail if this event has already used its allotted recovery attempts.

    ``prior_attempts`` counts attempts already made, so the limit is reached when
    it equals the maximum — a 4th attempt on a limit of 3 must not happen.
    """
    limit = get_settings().max_recovery_attempts
    used = event.prior_attempts
    passed = used < limit
    return GuardrailCheck(
        name=GuardrailName.MAX_RETRIES,
        passed=passed,
        detail=(
            f"{used} of {limit} recovery attempts used; "
            + ("within limit." if passed else "limit reached, stopping.")
        ),
    )


def check_contact_frequency(
    customer_id: str, last_contact_at: datetime | None, now: datetime
) -> GuardrailCheck:
    """Fail if this customer was contacted within the minimum window.

    Keyed on the customer, not the event: someone with three failing
    subscriptions must not receive three messages in one hour.
    """
    window_hours = get_settings().min_hours_between_contacts
    if last_contact_at is None:
        return GuardrailCheck(
            name=GuardrailName.CONTACT_FREQUENCY,
            passed=True,
            detail=f"No prior contact on record for {customer_id}.",
        )
    if window_hours == 0:
        return GuardrailCheck(
            name=GuardrailName.CONTACT_FREQUENCY,
            passed=True,
            detail=f"Immediate contact enabled (0h delay required).",
        )

    elapsed = now - last_contact_at
    window = timedelta(hours=window_hours)
    passed = elapsed >= window
    hours = elapsed.total_seconds() / 3600
    return GuardrailCheck(
        name=GuardrailName.CONTACT_FREQUENCY,
        passed=passed,
        detail=(
            f"Last contacted {hours:.1f}h ago against a {window_hours}h minimum; "
            + (
                "window has elapsed."
                if passed
                else f"deferring until {(last_contact_at + window).isoformat()}."
            )
        ),
    )


def check_quiet_hours(customer_timezone: str, now: datetime) -> GuardrailCheck:
    """Fail if the current customer-local time is outside allowed contact hours.

    Evaluated in the CUSTOMER's timezone, not the server's. A server in UTC
    deciding it is 2pm says nothing about whether it is 2am for the recipient.
    """
    settings = get_settings()
    start, end = settings.quiet_hours_start_local, settings.quiet_hours_end_local
    zone, used_fallback = resolve_timezone(customer_timezone)
    local = now.astimezone(zone)
    passed = (start == 0 and end >= 24) or (start <= local.hour < end)

    detail = (
        f"Customer-local time {local.strftime('%H:%M')} ({zone.key}) against an "
        f"allowed window of {start:02d}:00-{end:02d}:00; "
        + ("inside the window." if passed else "outside it, deferring.")
    )
    if used_fallback:
        detail += (
            f" Timezone was unknown, so {FALLBACK_TIMEZONE} was assumed — this "
            "result rests on that assumption."
        )
    return GuardrailCheck(
        name=GuardrailName.QUIET_HOURS, passed=passed, detail=detail
    )


def check_hard_stop(first_failure_at: datetime, now: datetime) -> GuardrailCheck:
    """Fail if the recovery window since first failure has elapsed.

    Measured from the FIRST failure, not the most recent one. Measuring from the
    latest attempt would let the window reset forever and a customer be chased
    indefinitely.
    """
    limit_days = get_settings().hard_stop_days
    elapsed = now - first_failure_at
    passed = elapsed < timedelta(days=limit_days)
    days = elapsed.total_seconds() / 86400
    return GuardrailCheck(
        name=GuardrailName.HARD_STOP_7_DAYS,
        passed=passed,
        detail=(
            f"{days:.1f} days since first failure against a {limit_days}-day "
            "limit; "
            + ("window still open." if passed else "window closed, stopping.")
        ),
    )


def run_all_checks(
    event: EventRecord,
    *,
    customer_timezone: str,
    last_contact_at: datetime | None,
    first_failure_at: datetime,
    now: datetime,
) -> list[GuardrailCheck]:
    """Run every guardrail and return all results, passes included.

    DECIDE calls this rather than individual checks, so no rule can be skipped by
    omission and the audit trail always shows all four ran. Order is fixed so
    audit entries are comparable between events.
    """
    return [
        check_max_retries(event),
        check_hard_stop(first_failure_at, now),
        check_quiet_hours(customer_timezone, now),
        check_contact_frequency(event.customer_id, last_contact_at, now),
    ]


def next_allowed_contact_time(
    *,
    customer_timezone: str,
    last_contact_at: datetime | None,
    now: datetime,
) -> datetime:
    """Earliest moment a customer may legitimately be contacted.

    Satisfies the frequency window first, then pushes into allowed local hours.
    Order matters: doing it the other way round can produce a time inside quiet
    hours but still too soon after the last contact.
    """
    settings = get_settings()
    zone, _ = resolve_timezone(customer_timezone)

    candidate = now
    if last_contact_at is not None:
        window_end = last_contact_at + timedelta(
            hours=settings.min_hours_between_contacts
        )
        candidate = max(candidate, window_end)

    start, end = settings.quiet_hours_start_local, settings.quiet_hours_end_local
    local = candidate.astimezone(zone)

    if local.hour < start:
        local = local.replace(hour=start, minute=0, second=0, microsecond=0)
    elif local.hour >= end:
        local = (local + timedelta(days=1)).replace(
            hour=start, minute=0, second=0, microsecond=0
        )

    return local.astimezone(now.tzinfo or local.tzinfo)


def failed(checks: list[GuardrailCheck]) -> list[GuardrailCheck]:
    return [c for c in checks if not c.passed]


def terminal_failures(checks: list[GuardrailCheck]) -> list[GuardrailCheck]:
    """Failures that mean stop, as opposed to failures that mean wait."""
    return [c for c in failed(checks) if KIND[c.name] is GuardrailKind.TERMINAL]


def deferrable_failures(checks: list[GuardrailCheck]) -> list[GuardrailCheck]:
    return [c for c in failed(checks) if KIND[c.name] is GuardrailKind.DEFERRABLE]
