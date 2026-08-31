"""Synthetic Razorpay ``payment.failed`` event generator.

Goal: events that are indistinguishable in shape from production Razorpay
deliveries, so DETECT is built and tested against the real thing rather than a
convenient simplification.

WHAT IS FAITHFUL TO RAZORPAY
  - The envelope: six top-level keys, ``payload.payment.entity`` nesting.
  - Every field name on the payment entity, including the ones we do not use.
  - ``amount`` in paise (minor units), never rupees.
  - ``created_at`` as integer UNIX seconds, not ISO strings.
  - Method-specific fields: ``vpa``/``upi`` only for UPI, ``bank`` only for
    netbanking, ``card``/``token_id`` only for cards, and the correct
    ``acquirer_data`` variant per method.
  - IDs as ``prefix_`` plus 14 base62 characters.
  - Real ``error_reason`` literals (see ``decline_catalog``).

WHAT IS DELIBERATELY NOT UNIFORM RANDOM
Realism is mostly in the joint distribution, not the field values:
  - Amounts land on plausible Indian subscription price points, not random ints.
  - Decline reasons are weighted, and a real share of them are uninformative.
  - Timestamps cluster in Indian waking hours, plus a nightly renewal-run spike.
  - Retry attempts belong to the SAME customer and order as their first failure,
    days apart, with ``prior_attempts`` incrementing.
  - Customers with long tenure and past failures are likelier to fail again, so
    failures cluster on a minority of customers rather than spreading evenly.

WHY THE BATCH INCLUDES EVENTS THAT SHOULD BE BLOCKED
A batch where every event sails through the guardrails would let us report
"0 violations" while proving nothing. So a deliberate share of events are built
to trip a specific stopping rule: a 4th attempt, a first failure more than 7 days
old, a customer contacted an hour ago, a 3am local timestamp. Each carries the
guardrail it should trip in ``expected_guardrail_failures``, as ground truth for
Phase 4.

TWO THINGS THAT ARE GROUND TRUTH, NOT INPUT
``expected_root_cause`` and ``expected_guardrail_failures`` exist to score the
pipeline. Feeding either into DETECT/DIAGNOSE/DECIDE would make the pipeline
grade its own homework. They live outside the Razorpay envelope for that reason.

CONTACT DETAILS ARE SYNTHETIC AND MUST NOT BE MESSAGED
Emails use reserved example domains. Phone numbers are well-formed but invented,
so a real person could in principle hold one. EXECUTE must send only to
``TWILIO_WHATSAPP_TEST_RECIPIENTS``; that allowlist is the safeguard, and it is
why it exists.
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.schemas import EventType, GuardrailName, PaymentMethod, RootCause
from app.simulation import abandonment
from app.simulation.abandonment_catalog import (
    SCENARIOS as ABANDONMENT_SCENARIOS,
)
from app.simulation.abandonment_catalog import (
    AbandonmentScenario,
    AbandonmentSignal,
)
from app.simulation.decline_catalog import SCENARIOS, DeclineScenario, Provenance

_BASE62 = string.ascii_letters + string.digits
_RZP_ID_LENGTH = 14

# Stopping-rule thresholds from architecture.md constraint #4, mirrored here so
# the generator stays deterministic and independent of a local .env. They are the
# spec, not a tuning knob. `test_simulation.py` asserts they still match the
# Settings defaults, so the two cannot drift apart silently.
_MAX_ATTEMPTS = 3
_HARD_STOP_DAYS = 7
_QUIET_HOURS_START = 9
_QUIET_HOURS_END = 20

# Indian subscription and invoice price points, in paise. Weighted toward the
# lower consumer tiers, with a thin tail of B2B invoice amounts.
_AMOUNTS_PAISE: tuple[tuple[int, float], ...] = (
    (14_900, 8.0),      # Rs 149
    (19_900, 12.0),     # Rs 199
    (29_900, 14.0),     # Rs 299
    (49_900, 16.0),     # Rs 499
    (79_900, 9.0),      # Rs 799
    (99_900, 11.0),     # Rs 999
    (149_900, 8.0),     # Rs 1,499
    (249_900, 6.0),     # Rs 2,499
    (499_900, 4.0),     # Rs 4,999
    (999_900, 2.5),     # Rs 9,999
    # A thin B2B invoice tail. Revenue at risk really is concentrated, so some
    # concentration is realistic, but the tail is capped: if one event could
    # dominate the headline metric, recovery rate would swing on a single
    # outcome and stop measuring the pipeline.
    (1_800_000, 1.0),   # Rs 18,000
    (3_500_000, 0.6),   # Rs 35,000
    (6_000_000, 0.3),   # Rs 60,000
)

# Method mix for the Indian market, UPI-dominant. Deliberately independent of
# how many scenarios the catalogue happens to contain per method: the mix should
# reflect the market, not the size of a lookup table.
# Razorpay publishes no volume-share figures, so these are a stated assumption.
_METHOD_MARKET_MIX: tuple[tuple[PaymentMethod, float], ...] = (
    (PaymentMethod.UPI, 56.0),
    (PaymentMethod.CARD, 26.0),
    (PaymentMethod.NETBANKING, 9.0),
    (PaymentMethod.WALLET, 7.0),
    (PaymentMethod.EMI, 2.0),
)

# Hour-of-day weights in customer local time. Indian consumer payment activity
# is low overnight, climbs through the morning, and peaks in the evening.
_HOUR_WEIGHTS: tuple[float, ...] = (
    0.4, 0.25, 0.2, 0.2, 0.3, 0.6,    # 00-05
    1.2, 2.2, 3.4, 4.6, 5.4, 5.6,     # 06-11
    5.0, 4.4, 4.6, 5.0, 5.6, 6.4,     # 12-17
    7.6, 8.2, 7.4, 5.4, 3.2, 1.4,     # 18-23
)

_CARD_NETWORKS: tuple[tuple[str, float], ...] = (
    ("Visa", 34.0), ("MasterCard", 30.0), ("RuPay", 26.0),
    ("American Express", 5.0), ("Maestro", 3.0), ("Diners Club", 2.0),
)
_CARD_TYPES: tuple[tuple[str, float], ...] = (
    ("debit", 58.0), ("credit", 38.0), ("prepaid", 4.0),
)
_ISSUERS: tuple[str, ...] = (
    "HDFC", "ICIC", "SBIN", "UTIB", "KKBK", "PUNB", "BARB", "IDFB", "YESB", "INDB",
)
_UPI_HANDLES: tuple[tuple[str, float], ...] = (
    ("okhdfcbank", 14.0), ("okicici", 13.0), ("oksbi", 15.0), ("okaxis", 11.0),
    ("ybl", 16.0), ("paytm", 14.0), ("apl", 9.0), ("ibl", 8.0),
)
_NETBANKING_BANKS: tuple[str, ...] = ("HDFC", "ICIC", "SBIN", "UTIB", "KKBK", "PUNB")
_WALLETS: tuple[str, ...] = ("payzapp", "phonepe", "amazonpay", "freecharge", "mobikwik")

# Reserved-for-documentation domains, so a fixture can never address a real inbox.
_EMAIL_DOMAINS: tuple[str, ...] = ("example.com", "example.in", "example.org")
_FIRST_NAMES: tuple[str, ...] = (
    "aarav", "diya", "vihaan", "ananya", "arjun", "ishani", "kabir", "meera",
    "rohan", "saanvi", "aditya", "nisha", "farhan", "leela", "tanvi", "yash",
)
_LAST_NAMES: tuple[str, ...] = (
    "sharma", "iyer", "patel", "reddy", "nair", "bose", "gupta", "menon",
    "khan", "desai", "rao", "chatterjee", "singh", "pillai",
)

_PLAN_NAMES: tuple[str, ...] = (
    "starter-monthly", "growth-monthly", "growth-annual", "pro-monthly",
    "pro-annual", "team-monthly", "scale-monthly",
)

# Almost all customers are in India; a thin slice elsewhere exercises the
# quiet-hours guardrail across timezones and the `international` card flag.
_TIMEZONES: tuple[tuple[str, float], ...] = (
    ("Asia/Kolkata", 88.0), ("Asia/Dubai", 4.0), ("Europe/London", 3.0),
    ("America/New_York", 3.0), ("Asia/Singapore", 2.0),
)


def _weighted_choice(rng: random.Random, options: tuple[tuple[object, float], ...]):
    values = [v for v, _ in options]
    weights = [w for _, w in options]
    return rng.choices(values, weights=weights, k=1)[0]


def _rzp_id(rng: random.Random, prefix: str) -> str:
    """Razorpay-style identifier: ``prefix_`` plus 14 base62 characters."""
    body = "".join(rng.choice(_BASE62) for _ in range(_RZP_ID_LENGTH))
    return f"{prefix}_{body}"


@dataclass(frozen=True, slots=True)
class SyntheticCustomer:
    """A customer with internally consistent attributes.

    ``timezone`` is not part of any Razorpay webhook. It lives here because the
    quiet-hours guardrail is specified in *customer local time*, and
    ``architecture.md``'s Event record has nowhere to put it. See the schema-gap
    note in progress-tracker.md.
    """

    customer_id: str
    name: str
    email: str
    contact: str
    tenure_days: int
    past_failures: int
    timezone: str
    failure_propensity: float
    preferred_method: PaymentMethod


@dataclass(frozen=True, slots=True)
class GeneratedEvent:
    """One simulated webhook delivery plus the truth we score it against."""

    event_id: str
    envelope: dict
    customer: SyntheticCustomer
    scenario_key: str
    provenance: Provenance
    expected_root_cause: RootCause
    expected_guardrail_failures: tuple[GuardrailName, ...]
    prior_attempts: int
    first_failure_at: datetime
    detected_at: datetime
    amount_paise: int
    event_type: EventType = EventType.PAYMENT_FAILED
    # None for abandonment events: an expired link or invoice has no single
    # payment method, because the customer never settled on one.
    method: PaymentMethod | None = None
    # Only meaningful for checkout_abandoned / invoice_overdue.
    abandonment_signal: AbandonmentSignal | None = None
    razorpay_event: str = "payment.failed"

    @property
    def amount_rupees(self) -> float:
        return self.amount_paise / 100


@dataclass(slots=True)
class BatchFixture:
    """A repeatable batch, self-describing enough to audit without the generator."""

    seed: int
    generated_at: datetime
    events: list[GeneratedEvent] = field(default_factory=list)

    def summary(self) -> dict:
        by_cause: dict[str, int] = {}
        by_method: dict[str, int] = {}
        by_provenance: dict[str, int] = {}
        by_guardrail: dict[str, int] = {}
        by_event_type: dict[str, int] = {}
        for e in self.events:
            by_cause[e.expected_root_cause] = by_cause.get(e.expected_root_cause, 0) + 1
            if e.method is not None:
                by_method[e.method] = by_method.get(e.method, 0) + 1
            by_provenance[e.provenance] = by_provenance.get(e.provenance, 0) + 1
            by_event_type[e.event_type] = by_event_type.get(e.event_type, 0) + 1
            for g in e.expected_guardrail_failures:
                by_guardrail[g] = by_guardrail.get(g, 0) + 1
        total_paise = sum(e.amount_paise for e in self.events)
        return {
            "seed": self.seed,
            "generated_at": self.generated_at.isoformat(),
            "event_count": len(self.events),
            "distinct_customers": len({e.customer.customer_id for e in self.events}),
            "amount_at_risk_paise": total_paise,
            "amount_at_risk_inr": round(total_paise / 100, 2),
            "by_event_type": dict(sorted(by_event_type.items())),
            "by_expected_root_cause": dict(sorted(by_cause.items())),
            "by_payment_method": dict(sorted(by_method.items())),
            "by_scenario_provenance": dict(sorted(by_provenance.items())),
            "expected_guardrail_failures": dict(sorted(by_guardrail.items())),
            "events_with_guardrail_failures": sum(
                1 for e in self.events if e.expected_guardrail_failures
            ),
        }


def _build_customer(rng: random.Random) -> SyntheticCustomer:
    first = rng.choice(_FIRST_NAMES)
    last = rng.choice(_LAST_NAMES)

    # Tenure is right-skewed: lots of recent signups, a long thin tail of
    # long-standing customers. A flat 0-1000 spread would be a giveaway.
    tenure_days = min(int(rng.lognormvariate(4.9, 1.05)), 2_600)

    propensity = rng.betavariate(1.6, 5.0)
    # Past failures scale with both exposure (tenure) and propensity, so the
    # two fields agree with each other instead of being independently random.
    expected = (tenure_days / 90.0) * propensity * 1.7
    past_failures = min(int(rng.gauss(expected, 0.9) + 0.5), 14)

    return SyntheticCustomer(
        customer_id=_rzp_id(rng, "cust"),
        name=f"{first.capitalize()} {last.capitalize()}",
        email=f"{first}.{last}{rng.randint(1, 99)}@{rng.choice(_EMAIL_DOMAINS)}",
        contact=f"+91{rng.choice('6789')}{rng.randint(0, 999_999_999):09d}",
        tenure_days=tenure_days,
        past_failures=max(past_failures, 0),
        timezone=str(_weighted_choice(rng, _TIMEZONES)),
        failure_propensity=propensity,
        preferred_method=_weighted_choice(rng, _METHOD_MARKET_MIX),  # type: ignore[arg-type]
    )


_SCENARIOS_BY_METHOD: dict[PaymentMethod, tuple[DeclineScenario, ...]] = {
    method: tuple(s for s in SCENARIOS if s.method is method)
    for method in PaymentMethod
    if any(s.method is method for s in SCENARIOS)
}


def _pick_scenario(rng: random.Random, customer: SyntheticCustomer) -> DeclineScenario:
    """Pick the METHOD first, then a failure reason within that method.

    Choosing the method first keeps the batch's method mix faithful to
    ``_METHOD_MARKET_MIX``. Sampling scenarios directly would instead let the mix
    be decided by how many scenarios happen to be catalogued per method, which
    made cards 59% of an India-focused batch on the first attempt.

    Customers stay on their usual method most of the time, so a retry chain does
    not implausibly hop between UPI and card.
    """
    method = customer.preferred_method
    if rng.random() >= 0.8 or method not in _SCENARIOS_BY_METHOD:
        candidates = [m for m, _ in _METHOD_MARKET_MIX if m in _SCENARIOS_BY_METHOD]
        weights = [w for m, w in _METHOD_MARKET_MIX if m in _SCENARIOS_BY_METHOD]
        method = rng.choices(candidates, weights=weights, k=1)[0]
    pool = _SCENARIOS_BY_METHOD[method]
    return rng.choices(pool, weights=[s.weight for s in pool], k=1)[0]


def _local_timestamp(
    rng: random.Random, window_start: datetime, window_days: int, timezone: str
) -> datetime:
    """A UTC instant whose CUSTOMER-LOCAL hour follows ``_HOUR_WEIGHTS``."""
    tz = ZoneInfo(timezone)
    day_offset = rng.randrange(window_days)
    hour = rng.choices(range(24), weights=_HOUR_WEIGHTS, k=1)[0]
    local_day = (window_start.astimezone(tz) + timedelta(days=day_offset)).date()
    local_dt = datetime(
        local_day.year, local_day.month, local_day.day,
        hour, rng.randrange(60), rng.randrange(60), tzinfo=tz,
    )
    return local_dt.astimezone(UTC)


def _force_local_hour(dt: datetime, timezone: str, hour: int) -> datetime:
    """Move ``dt`` to a specific customer-local hour, preserving the date."""
    tz = ZoneInfo(timezone)
    local = dt.astimezone(tz).replace(hour=hour, minute=17, second=3, microsecond=0)
    return local.astimezone(UTC)


def _derive_expected_failures(
    *,
    prior_attempts: int,
    first_failure_at: datetime,
    detected_at: datetime,
    timezone: str,
) -> tuple[GuardrailName, ...]:
    """Which guardrail checks this event should fail, read off the event itself.

    A failing check does not always mean "do nothing". A quiet-hours failure
    means defer to the next allowed window; ``max_retries`` and the 7-day hard
    stop mean stop. Choosing between those is DECIDE's job (Phase 4); this only
    states which checks should come back false.

    Ground truth is computed from the data, never from whichever rule the
    generator intended to trip, so the label and the event cannot disagree.

    ``contact_frequency`` is deliberately absent: it depends on when the customer
    was last CONTACTED, which is pipeline state rather than a property of the
    event. The generator makes it reachable by producing sub-24h retry gaps, but
    it cannot be pre-labelled here.
    """
    blocks: list[GuardrailName] = []

    if prior_attempts >= _MAX_ATTEMPTS:
        blocks.append(GuardrailName.MAX_RETRIES)

    if detected_at - first_failure_at >= timedelta(days=_HARD_STOP_DAYS):
        blocks.append(GuardrailName.HARD_STOP_7_DAYS)

    local_hour = detected_at.astimezone(ZoneInfo(timezone)).hour
    if not _QUIET_HOURS_START <= local_hour < _QUIET_HOURS_END:
        blocks.append(GuardrailName.QUIET_HOURS)

    return tuple(blocks)


def _payment_entity(
    rng: random.Random,
    customer: SyntheticCustomer,
    scenario: DeclineScenario,
    amount_paise: int,
    created_at: datetime,
    order_id: str,
    invoice_id: str | None,
) -> dict:
    """A Razorpay payment entity in the ``failed`` state.

    Field set and nesting follow Razorpay's published ``payment.failed`` samples,
    including fields this project never reads. DETECT has to cope with the real
    payload, not a trimmed one.

    Note the ``card`` sub-object on card payments. It is included ON PURPOSE:
    it carries ``last4``, ``network`` and ``iin``, and DETECT is required to drop
    all of it when normalising to an EventRecord. If fixtures omitted it, that
    stripping would never be exercised and constraint #1 would be untested.
    There is still no PAN, CVV or expiry here, because Razorpay never sends them.
    """
    method = scenario.method
    entity: dict = {
        "id": _rzp_id(rng, "pay"),
        "entity": "payment",
        "amount": amount_paise,
        "currency": "INR",
        "status": "failed",
        "order_id": order_id,
        "invoice_id": invoice_id,
        "international": customer.timezone != "Asia/Kolkata" and rng.random() < 0.35,
        "method": str(method),
        "amount_refunded": 0,
        "refund_status": None,
        "captured": False,
        "description": f"#{invoice_id}" if invoice_id else None,
        "card_id": None,
        "bank": None,
        "wallet": None,
        "vpa": None,
        "email": customer.email,
        "contact": customer.contact,
        "notes": (
            {"plan": rng.choice(_PLAN_NAMES), "customer_id": customer.customer_id}
            if rng.random() < 0.7
            else []
        ),
        "fee": None,
        "tax": None,
        "error_code": scenario.error_code,
        "error_description": scenario.error_description,
        "error_source": scenario.error_source,
        "error_step": scenario.error_step,
        "error_reason": scenario.error_reason,
        "acquirer_data": {},
        "created_at": int(created_at.timestamp()),
    }

    if method is PaymentMethod.CARD:
        card_id = _rzp_id(rng, "card")
        entity["card_id"] = card_id
        entity["amount_transferred"] = 0
        entity["token_id"] = _rzp_id(rng, "token")
        entity["card"] = {
            "id": card_id,
            "entity": "card",
            "name": customer.name,
            # Last-4 values from Razorpay's documented test cards.
            "last4": rng.choice(("1111", "5449", "0002", "3335", "8888")),
            "network": str(_weighted_choice(rng, _CARD_NETWORKS)),
            "type": str(_weighted_choice(rng, _CARD_TYPES)),
            "issuer": rng.choice(_ISSUERS),
            "iin": rng.choice(("401200", "521234", "607384", "411111", "555555")),
            "international": entity["international"],
            "emi": False,
            "sub_type": "consumer",
        }
        entity["acquirer_data"] = {
            "auth_code": None,
            "rrn": f"{rng.randrange(10**11, 10**12)}",
        }
    elif method is PaymentMethod.UPI:
        handle = str(_weighted_choice(rng, _UPI_HANDLES))
        vpa = f"{customer.name.split()[0].lower()}{rng.randint(1, 999)}@{handle}"
        entity["vpa"] = vpa
        entity["upi"] = {
            "payer_account_type": "bank_account",
            "vpa": vpa,
            "flow": rng.choice(("intent", "collect")),
        }
        entity["acquirer_data"] = {"rrn": f"{rng.randrange(10**11, 10**12)}"}
    elif method is PaymentMethod.NETBANKING:
        entity["bank"] = rng.choice(_NETBANKING_BANKS)
        entity["acquirer_data"] = {"bank_transaction_id": None}
    elif method is PaymentMethod.WALLET:
        entity["wallet"] = rng.choice(_WALLETS)
        entity["acquirer_data"] = {"transaction_id": None}
    elif method is PaymentMethod.EMI:
        entity["card_id"] = _rzp_id(rng, "card")
        entity["acquirer_data"] = {"auth_code": None}

    return entity


def _envelope(rng: random.Random, entity: dict, account_id: str) -> dict:
    """The Razorpay webhook envelope wrapping a payment entity."""
    return {
        "entity": "event",
        "account_id": account_id,
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {"payment": {"entity": entity}},
        "created_at": entity["created_at"] + rng.randint(0, 3),
    }


def _build_abandonment_event(
    rng: random.Random,
    *,
    customer: SyntheticCustomer,
    scenario: AbandonmentScenario,
    account_id: str,
    window_start: datetime,
    window_days: int,
) -> GeneratedEvent:
    """One ``payment_link.expired`` or ``invoice.expired`` delivery.

    Timing is modelled the way the recovery window actually works: the link or
    invoice is created first, sits for a validity period, then expires. The expiry
    is when we learn about it, so ``detected_at`` is the expiry and
    ``first_failure_at`` is the creation — which is what the 7-day hard stop
    should measure from, since that is when the money first became at risk.
    """
    amount_paise = int(_weighted_choice(rng, _AMOUNTS_PAISE))  # type: ignore[arg-type]
    # The EXPIRY is the observable event, so that is what must fall inside the
    # window; the link was created earlier, possibly before observation began.
    #
    # Doing this the other way round — picking creation inside the window and
    # adding validity — put expiries up to 7 days in the FUTURE, which meant
    # receiving an expiry webhook for something that had not expired yet. Live
    # replay surfaced it as detected_at > received_at.
    expired_at = _local_timestamp(rng, window_start, window_days, customer.timezone)
    validity_hours = rng.choice((24, 48, 72, 96, 120, 168))
    created_at = expired_at - timedelta(hours=validity_hours)

    is_invoice = scenario.event_type is EventType.INVOICE_OVERDUE
    if is_invoice:
        entity = abandonment.build_invoice_entity(
            rng,
            scenario=scenario,
            customer_id=customer.customer_id,
            name=customer.name,
            email=customer.email,
            contact=customer.contact,
            amount_paise=amount_paise,
            created_at=created_at,
            expired_at=expired_at,
            order_id=_rzp_id(rng, "order"),
        )
        entity_key = "invoice"
    else:
        entity = abandonment.build_payment_link_entity(
            rng,
            scenario=scenario,
            customer_id=customer.customer_id,
            name=customer.name,
            email=customer.email,
            contact=customer.contact,
            amount_paise=amount_paise,
            created_at=created_at,
            expired_at=expired_at,
        )
        entity_key = "payment_link"

    envelope = abandonment.build_envelope(
        razorpay_event=scenario.razorpay_event,
        entity_key=entity_key,
        entity=entity,
        account_id=account_id,
        created_at=expired_at,
    )

    return GeneratedEvent(
        event_id=entity["id"],
        envelope=envelope,
        customer=customer,
        scenario_key=scenario.key,
        provenance=scenario.provenance,
        expected_root_cause=scenario.expected_root_cause,
        expected_guardrail_failures=_derive_expected_failures(
            prior_attempts=scenario.failed_attempts,
            first_failure_at=created_at,
            detected_at=expired_at,
            timezone=customer.timezone,
        ),
        prior_attempts=scenario.failed_attempts,
        first_failure_at=created_at,
        detected_at=expired_at,
        amount_paise=amount_paise,
        event_type=scenario.event_type,
        method=None,
        abandonment_signal=scenario.signal,
        razorpay_event=scenario.razorpay_event,
    )


def generate_batch(
    seed: int = 42,
    count: int = 75,
    window_days: int = 14,
    blocked_share: float = 0.18,
    now: datetime | None = None,
    abandonment_share: float = 0.24,
) -> BatchFixture:
    """Generate ``count`` synthetic deliveries across all three event types.

    Deterministic for a given ``seed`` AND ``now``. Both matter: event timestamps
    are relative to ``now``, so seed alone does not pin the output.

    ``now`` defaults to the current time, which is what a demo wants — events
    must look recent, because a batch dated more than 7 days ago would trip the
    hard-stop guardrail on every single event and recover nothing. Pass an
    explicit ``now`` to pin a batch for byte-stable fixtures or for testing
    time-dependent guardrails.

    ``blocked_share`` is the fraction of events nudged toward failing a stopping
    rule. Set it to 0 only if you want a batch that cannot demonstrate the
    guardrails do anything.

    ``abandonment_share`` is the fraction that are ``checkout_abandoned`` or
    ``invoice_overdue`` rather than ``payment_failed``. Kept a minority because
    `project-overview.md` locks the direction to payment degradation; it is
    non-zero because two of the eight root causes can only arise from an
    abandonment, and DIAGNOSE needs an example of each.
    """
    if count < 1:
        raise ValueError("count must be at least 1")
    if not 0.0 <= blocked_share < 1.0:
        raise ValueError("blocked_share must be in [0.0, 1.0)")
    if not 0.0 <= abandonment_share < 1.0:
        raise ValueError("abandonment_share must be in [0.0, 1.0)")
    if window_days < 1:
        raise ValueError("window_days must be at least 1")

    rng = random.Random(seed)
    account_id = _rzp_id(rng, "acc")
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        raise ValueError("now must be timezone-aware to avoid ambiguous timestamps")
    now = now.astimezone(UTC).replace(microsecond=0)
    window_start = now - timedelta(days=window_days)

    # Fewer customers than events, so retries and repeat failures concentrate on
    # a subset, the way they do in production.
    pool_size = max(6, int(count * 0.62))
    customers = [_build_customer(rng) for _ in range(pool_size)]
    # Weight selection by propensity so a minority of customers generate a
    # disproportionate share of failures.
    customer_weights = [0.35 + c.failure_propensity * 2.2 for c in customers]

    batch = BatchFixture(seed=seed, generated_at=now)
    # Tracks per-(customer, order) retry chains so prior_attempts is coherent.
    chains: dict[tuple[str, str], dict] = {}
    n_blocked_target = int(count * blocked_share)
    blocked_assigned = 0

    # Plan which slots are abandonment events up front, then shuffle, so they
    # interleave with payment failures instead of clustering at the start.
    #
    # Every abandonment scenario is seeded at least once before the remainder is
    # filled by weight. Pure weighted sampling can miss a scenario entirely in a
    # small batch, and Phase 3 needs one example per taxonomy category — a batch
    # that silently omits `checkout_friction` would let DIAGNOSE ship untested
    # against it.
    n_abandonment = min(int(count * abandonment_share), count)
    abandonment_plan: list[AbandonmentScenario | None] = []
    if n_abandonment:
        guaranteed = list(ABANDONMENT_SCENARIOS)[:n_abandonment]
        abandonment_plan.extend(guaranteed)
        remaining = n_abandonment - len(guaranteed)
        if remaining > 0:
            abandonment_plan.extend(
                rng.choices(
                    ABANDONMENT_SCENARIOS,
                    weights=[a.weight for a in ABANDONMENT_SCENARIOS],
                    k=remaining,
                )
            )
    plan: list[AbandonmentScenario | None] = abandonment_plan + [None] * (
        count - len(abandonment_plan)
    )
    rng.shuffle(plan)

    for slot in range(count):
        customer = rng.choices(customers, weights=customer_weights, k=1)[0]

        # Abandonment events take a separate, simpler path: a link or invoice
        # expires once, so there is no retry chain to continue.
        planned = plan[slot]
        if planned is not None:
            batch.events.append(
                _build_abandonment_event(
                    rng,
                    customer=customer,
                    scenario=planned,
                    account_id=account_id,
                    window_start=window_start,
                    window_days=window_days,
                )
            )
            continue

        scenario = _pick_scenario(rng, customer)

        # Continue an existing retry chain roughly a third of the time, so
        # attempts belong to the same order rather than appearing from nowhere.
        chain_key = None
        chain_detected_at: datetime | None = None
        existing = [k for k in chains if k[0] == customer.customer_id]
        if existing and rng.random() < 0.34:
            candidate_key = rng.choice(existing)
            # Retry gaps include some under 24 hours. Those are what let the
            # contact-frequency rule actually come into play: unlike the other
            # three guardrails, it depends on when we last CONTACTED the customer,
            # which is pipeline state rather than anything the event carries. So
            # it cannot be pre-labelled in ground truth, only made reachable.
            gap_hours = rng.choice((5, 9, 14, 20, 26, 31, 48, 55, 72))
            candidate_at = chains[candidate_key]["last_at"] + timedelta(
                hours=gap_hours, minutes=rng.randrange(60)
            )
            # Only continue the chain if the next attempt still lands at or before
            # the reference time. Adding the gap unconditionally produced events
            # dated in the future, which a live replay exposed as
            # detected_at > received_at.
            if candidate_at <= now:
                chain_key = candidate_key
                chain_detected_at = candidate_at

        if chain_key is None:
            order_id = _rzp_id(rng, "order")
            amount_paise = int(_weighted_choice(rng, _AMOUNTS_PAISE))  # type: ignore[arg-type]
            invoice_id = _rzp_id(rng, "inv") if rng.random() < 0.45 else None
            first_failure_at = _local_timestamp(
                rng, window_start, window_days, customer.timezone
            )
            detected_at = first_failure_at
            prior_attempts = 0
            chain_key = (customer.customer_id, order_id)
        else:
            chain = chains[chain_key]
            order_id = chain_key[1]
            # Same order means the same amount. Varying it would be a tell.
            amount_paise = chain["amount_paise"]
            invoice_id = chain["invoice_id"]
            first_failure_at = chain["first_failure_at"]
            prior_attempts = chain["attempts"]
            assert chain_detected_at is not None
            detected_at = chain_detected_at

        # Deliberately push a share of events past a stopping rule.
        if blocked_assigned < n_blocked_target and rng.random() < 0.5:
            # Cycled, not random: with only ~13 blocked events, random choice
            # left max_retries with 2 examples, too thin to test against.
            which = ("max_retries", "hard_stop", "quiet_hours")[blocked_assigned % 3]
            if which == "max_retries":
                prior_attempts = max(prior_attempts, rng.choice((3, 3, 4, 5)))
            elif which == "hard_stop":
                first_failure_at = detected_at - timedelta(
                    days=rng.choice((8, 9, 11, 14)), hours=rng.randrange(0, 12)
                )
            else:
                detected_at = _force_local_hour(
                    detected_at, customer.timezone, rng.choice((2, 3, 4, 23))
                )
            blocked_assigned += 1

        # Keep history coherent: N prior attempts cannot have happened in zero
        # elapsed time. Space them plausibly (dunning retries run 1-2 days apart)
        # and let first_failure_at fall where that puts it, even if the result
        # also crosses the 7-day line. Real dunning cycles do run past a week,
        # which is precisely why the hard stop exists.
        if prior_attempts > 0:
            implied_span = timedelta(
                hours=sum(rng.randrange(20, 38) for _ in range(prior_attempts))
            )
            first_failure_at = min(first_failure_at, detected_at - implied_span)

        # Final guard. The quiet-hours injection above moves an event to a given
        # customer-local hour on the same date, which can land after the reference
        # time. Walk it back a day at a time rather than clamping, so the forced
        # local hour is preserved — that hour is the whole point of the injection.
        while detected_at > now:
            detected_at -= timedelta(days=1)

        if detected_at < first_failure_at:
            first_failure_at = detected_at

        # DERIVE the expected blocks from the finished event rather than
        # recording whichever rule we meant to trip. Deriving means ground truth
        # cannot disagree with the data it describes, and it correctly catches
        # events that end up tripping two rules at once.
        expected_failures = _derive_expected_failures(
            prior_attempts=prior_attempts,
            first_failure_at=first_failure_at,
            detected_at=detected_at,
            timezone=customer.timezone,
        )

        entity = _payment_entity(
            rng, customer, scenario, amount_paise, detected_at, order_id, invoice_id
        )
        envelope = _envelope(rng, entity, account_id)

        chains[chain_key] = {
            "amount_paise": amount_paise,
            "invoice_id": invoice_id,
            "first_failure_at": first_failure_at,
            "last_at": detected_at,
            "attempts": prior_attempts + 1,
        }

        batch.events.append(
            GeneratedEvent(
                event_id=entity["id"],
                envelope=envelope,
                customer=customer,
                scenario_key=scenario.key,
                provenance=scenario.provenance,
                expected_root_cause=scenario.expected_root_cause,
                expected_guardrail_failures=expected_failures,
                prior_attempts=prior_attempts,
                first_failure_at=first_failure_at,
                detected_at=detected_at,
                amount_paise=amount_paise,
                method=scenario.method,
            )
        )

    batch.events.sort(key=lambda e: e.detected_at)
    return batch
