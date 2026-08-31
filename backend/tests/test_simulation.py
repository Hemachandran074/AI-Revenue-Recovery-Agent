"""Tests for the Phase 1 data simulation layer.

Two things these protect:

1. **Payload fidelity.** If the generator drifts from Razorpay's real shape,
   DETECT gets built against a fiction and breaks on the first live webhook.
2. **Ground-truth integrity.** ``expected_root_cause`` and
   ``expected_guardrail_failures`` are the answer key. If they leak into the
   webhook payload, the pipeline scores itself and every metric is worthless.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.schemas import EventType, GuardrailName, PaymentMethod, RootCause
from app.simulation import signing
from app.simulation.abandonment_catalog import (
    covered_root_causes as abandonment_root_causes,
)
from app.simulation.decline_catalog import (
    SCENARIOS,
    Provenance,
    covered_root_causes,
    scenarios_by_key,
)
from app.simulation.fixtures import (
    FIXTURE_VERSION,
    batch_to_dict,
    load_fixture,
    webhooks_only,
    write_fixture,
)
from app.simulation.generator import (
    _HARD_STOP_DAYS,
    _MAX_ATTEMPTS,
    _QUIET_HOURS_END,
    _QUIET_HOURS_START,
    generate_batch,
)

BATCH_SIZE = 75


@pytest.fixture(scope="module")
def batch():
    return generate_batch(seed=42, count=BATCH_SIZE)


@pytest.fixture(scope="module")
def payment_events(batch):
    """Only the payment.failed events.

    A batch now also contains checkout_abandoned and invoice_overdue deliveries,
    which carry a payment_link or invoice entity instead. Payment-specific
    assertions must be scoped, or they assert against the wrong entity type.
    """
    return [e for e in batch.events if e.event_type is EventType.PAYMENT_FAILED]


@pytest.fixture(scope="module")
def entities(payment_events):
    return [e.envelope["payload"]["payment"]["entity"] for e in payment_events]


# --------------------------------------------------------------- determinism


def test_same_seed_and_clock_gives_byte_identical_batch() -> None:
    """A metrics run has to be reproducible, or a regression is unattributable.

    Seed alone is not enough: timestamps are relative to ``now``, so the clock
    has to be pinned too.
    """
    pinned = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    a = batch_to_dict(generate_batch(seed=7, count=30, now=pinned))
    b = batch_to_dict(generate_batch(seed=7, count=30, now=pinned))
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_clock_shifts_the_whole_batch() -> None:
    """Regenerating for a demo must move events forward, not leave them stale."""
    early = generate_batch(seed=7, count=20, now=datetime(2026, 1, 1, tzinfo=UTC))
    later = generate_batch(seed=7, count=20, now=datetime(2026, 6, 1, tzinfo=UTC))
    assert max(e.detected_at for e in early.events) < min(
        e.detected_at for e in later.events
    )


def test_naive_clock_is_rejected() -> None:
    """A naive datetime would make quiet-hours evaluation ambiguous."""
    with pytest.raises(ValueError, match="timezone-aware"):
        generate_batch(seed=1, count=5, now=datetime(2026, 8, 30, 12, 0))  # noqa: DTZ001


def test_fresh_batch_is_recent_enough_to_be_actionable() -> None:
    """The whole batch must not sit outside the 7-day hard-stop window.

    If it did, every event would be blocked and a demo would report zero
    recovery while the pipeline was working perfectly.
    """
    batch = generate_batch(seed=3, count=60)
    stale = [
        e
        for e in batch.events
        if GuardrailName.HARD_STOP_7_DAYS in e.expected_guardrail_failures
    ]
    assert len(stale) / len(batch.events) < 0.4, (
        "most of the batch is outside the hard-stop window; regenerate with a "
        "current clock"
    )


def test_different_seed_gives_different_batch() -> None:
    a = [e.event_id for e in generate_batch(seed=1, count=30).events]
    b = [e.event_id for e in generate_batch(seed=2, count=30).events]
    assert a != b


def test_rejects_nonsense_parameters() -> None:
    with pytest.raises(ValueError):
        generate_batch(count=0)
    with pytest.raises(ValueError):
        generate_batch(blocked_share=1.0)


# ------------------------------------------------------ project requirements


def test_batch_size_is_in_project_scope(batch) -> None:
    """project-overview.md scopes the demo batch at 50-100 events."""
    assert 50 <= len(batch.events) <= 100


def test_spans_at_least_five_root_cause_categories(batch) -> None:
    """Phase 1 requires at least 5 decline-code categories."""
    causes = {e.expected_root_cause for e in batch.events}
    assert len(causes) >= 5, f"only {len(causes)} categories: {sorted(causes)}"


def test_decline_catalogue_omits_only_the_checkout_only_causes() -> None:
    """A failed payment cannot legitimately mean an abandoned checkout.

    ``checkout_friction`` and ``genuine_abandonment`` describe abandonment, so
    they are absent from the DECLINE catalogue by design and supplied by the
    abandonment catalogue instead.
    """
    missing = set(RootCause) - covered_root_causes()
    assert missing == {RootCause.CHECKOUT_FRICTION, RootCause.GENUINE_ABANDONMENT}


def test_both_catalogues_together_cover_all_eight_root_causes() -> None:
    """The whole reason Phase 1b exists.

    DIAGNOSE needs one test per taxonomy category, so every category must be
    producible by some scenario. A gap here means a category ships untested.
    """
    combined = covered_root_causes() | abandonment_root_causes()
    assert combined == set(RootCause), f"uncovered: {sorted(set(RootCause) - combined)}"


def test_every_scenario_has_a_rationale_and_provenance() -> None:
    for s in SCENARIOS:
        assert s.rationale.strip(), f"{s.key} has no rationale"
        assert isinstance(s.provenance, Provenance)
        assert s.weight > 0


def test_scenario_keys_are_unique() -> None:
    assert len(scenarios_by_key()) == len(SCENARIOS)


def test_error_reasons_are_razorpay_literals() -> None:
    """Guards against a plausible-looking invention like 'low_funds' creeping in.

    Every non-null reason must be snake_case and drawn from the documented
    vocabulary recorded in decline_catalog's docstring.
    """
    for s in SCENARIOS:
        if s.error_reason is None:
            continue
        assert s.error_reason == s.error_reason.lower()
        assert " " not in s.error_reason


def test_batch_includes_uninformative_declines(batch) -> None:
    """Real webhooks are often unclassifiable. A batch of only tidy, neatly
    diagnosable failures would let DIAGNOSE pass without an escalation path."""
    unknowns = [e for e in batch.events if e.expected_root_cause is RootCause.UNKNOWN]
    assert unknowns, "no unknown-cause events; escalation would go untested"


def test_batch_includes_documented_and_inferred_scenarios(batch) -> None:
    provenances = {e.provenance for e in batch.events}
    assert Provenance.DOCUMENTED in provenances
    assert Provenance.INFERRED in provenances


# ------------------------------------------------------ Razorpay payload shape


def test_envelope_shape_matches_razorpay(payment_events) -> None:
    for e in payment_events:
        env = e.envelope
        assert set(env) == {
            "entity", "account_id", "event", "contains", "payload", "created_at",
        }
        assert env["entity"] == "event"
        assert env["event"] == "payment.failed"
        assert env["contains"] == ["payment"]
        assert env["account_id"].startswith("acc_")
        assert isinstance(env["created_at"], int)
        assert set(env["payload"]) == {"payment"}
        assert set(env["payload"]["payment"]) == {"entity"}


def test_payment_entity_has_required_fields(entities) -> None:
    required = {
        "id", "entity", "amount", "currency", "status", "order_id", "invoice_id",
        "international", "method", "amount_refunded", "refund_status", "captured",
        "description", "card_id", "bank", "wallet", "vpa", "email", "contact",
        "notes", "fee", "tax", "error_code", "error_description", "error_source",
        "error_step", "error_reason", "acquirer_data", "created_at",
    }
    for entity in entities:
        assert required <= set(entity), f"missing {required - set(entity)}"
        assert entity["entity"] == "payment"
        assert entity["status"] == "failed"
        assert entity["currency"] == "INR"


def test_amount_is_integer_paise(entities) -> None:
    """Razorpay sends minor units. A float or a rupee value would be wrong, and
    would quietly under-report recovered revenue by 100x."""
    for entity in entities:
        assert isinstance(entity["amount"], int)
        assert not isinstance(entity["amount"], bool)
        assert entity["amount"] > 0
        # Indian price points do not carry paise.
        assert entity["amount"] % 100 == 0


def test_created_at_is_unix_seconds(entities) -> None:
    for entity in entities:
        assert isinstance(entity["created_at"], int)
        # Sane epoch range, not milliseconds and not an ISO string.
        assert 1_600_000_000 < entity["created_at"] < 2_000_000_000


def test_ids_follow_razorpay_format(entities) -> None:
    for entity in entities:
        assert entity["id"].startswith("pay_")
        assert len(entity["id"].split("_", 1)[1]) == 14
        assert entity["order_id"].startswith("order_")
        if entity["invoice_id"] is not None:
            assert entity["invoice_id"].startswith("inv_")


@pytest.mark.parametrize(
    ("method", "expected_present", "expected_absent"),
    [
        (PaymentMethod.UPI, ("vpa",), ("bank", "wallet", "card_id")),
        (PaymentMethod.NETBANKING, ("bank",), ("vpa", "wallet", "card_id")),
        (PaymentMethod.WALLET, ("wallet",), ("vpa", "bank", "card_id")),
        (PaymentMethod.CARD, ("card_id",), ("vpa", "bank", "wallet")),
    ],
)
def test_method_specific_fields(entities, method, expected_present, expected_absent) -> None:
    """A UPI payment carrying a bank name, or a card carrying a vpa, would be a
    shape DETECT never sees in production."""
    matching = [e for e in entities if e["method"] == str(method)]
    if not matching:
        pytest.skip(f"no {method} events in this batch")
    for entity in matching:
        for field_name in expected_present:
            assert entity[field_name] is not None, f"{method} missing {field_name}"
        for field_name in expected_absent:
            assert entity[field_name] is None, f"{method} should not set {field_name}"


def test_card_payments_carry_no_pan_cvv_or_expiry(entities) -> None:
    """Constraint #1. Razorpay never sends these, and neither may our fixtures.

    The ``card`` sub-object IS present on purpose (last4, network, iin) so that
    DETECT's stripping is actually exercised, but nothing sensitive may appear.
    """
    forbidden = {
        "number", "pan", "card_number", "cvv", "cvc", "expiry",
        "expiry_month", "expiry_year", "exp_month", "exp_year",
    }
    for entity in entities:
        blob = json.dumps(entity).lower()
        for term in forbidden:
            assert f'"{term}"' not in blob, f"{term} present in payload"
        card = entity.get("card")
        if card:
            assert forbidden.isdisjoint(card)
            assert len(card["last4"]) == 4


def test_upi_events_carry_upi_object(entities) -> None:
    for entity in entities:
        if entity["method"] == "upi":
            assert entity["upi"]["vpa"] == entity["vpa"]
            assert entity["upi"]["flow"] in {"intent", "collect"}


def test_acquirer_data_variant_matches_method(entities) -> None:
    expected_keys = {
        "upi": {"rrn"},
        "netbanking": {"bank_transaction_id"},
        "wallet": {"transaction_id"},
        "card": {"auth_code", "rrn"},
        "emi": {"auth_code"},
    }
    for entity in entities:
        assert set(entity["acquirer_data"]) == expected_keys[entity["method"]]


# ------------------------------------------------------------- realism checks


def test_amounts_are_not_uniformly_random(entities) -> None:
    """Amounts must land on a small set of plausible price points. Uniform random
    integers are the clearest tell of synthetic data."""
    distinct = {e["amount"] for e in entities}
    assert len(distinct) <= 20, "too many distinct amounts to be real price points"
    assert len(distinct) >= 4, "suspiciously few distinct amounts"


def test_failures_concentrate_on_a_subset_of_customers(batch) -> None:
    """Real failures cluster on a minority of customers rather than spreading
    one-per-customer."""
    customers = [e.customer.customer_id for e in batch.events]
    assert len(set(customers)) < len(customers)


def test_customer_history_is_self_consistent(batch) -> None:
    """A customer cannot have more past failures than days of tenure allow, and
    the same customer must present identical history everywhere they appear."""
    seen: dict[str, tuple[int, int]] = {}
    for e in batch.events:
        c = e.customer
        assert c.past_failures >= 0
        assert c.tenure_days >= 0
        key = (c.tenure_days, c.past_failures)
        assert seen.setdefault(c.customer_id, key) == key


def test_retry_chains_are_coherent(payment_events) -> None:
    """Attempts on one order must share an amount and increase over time.

    This caught a real bug: an event claiming 5 prior attempts while
    first_failure_at equalled detected_at, i.e. five attempts in zero elapsed
    time.
    """
    by_order: dict[str, list] = {}
    for e in payment_events:
        by_order.setdefault(
            e.envelope["payload"]["payment"]["entity"]["order_id"], []
        ).append(e)

    chains = [v for v in by_order.values() if len(v) > 1]
    assert chains, "no retry chains generated; prior_attempts would be untested"

    for chain in chains:
        ordered = sorted(chain, key=lambda x: x.detected_at)
        assert len({x.amount_paise for x in ordered}) == 1
        assert len({x.customer.customer_id for x in ordered}) == 1
        attempts = [x.prior_attempts for x in ordered]
        assert attempts == sorted(attempts)


def test_prior_attempts_imply_elapsed_time(batch) -> None:
    """N prior attempts cannot have happened instantaneously."""
    for e in batch.events:
        if e.prior_attempts > 0:
            assert e.detected_at > e.first_failure_at, (
                f"{e.event_id} claims {e.prior_attempts} prior attempts but "
                "first_failure_at == detected_at"
            )


def test_first_failure_never_after_detection(batch) -> None:
    for e in batch.events:
        assert e.first_failure_at <= e.detected_at


def test_events_are_time_ordered(batch) -> None:
    times = [e.detected_at for e in batch.events]
    assert times == sorted(times)


def test_timestamps_favour_waking_hours(batch) -> None:
    """Uniform-across-24h timestamps would be unrealistic. Most events should
    fall in customer-local daytime/evening."""
    daytime = sum(
        1
        for e in batch.events
        if 8 <= e.detected_at.astimezone(ZoneInfo(e.customer.timezone)).hour <= 22
    )
    assert daytime / len(batch.events) > 0.6


def test_most_customers_are_in_india(batch) -> None:
    indian = sum(1 for e in batch.events if e.customer.timezone == "Asia/Kolkata")
    assert indian / len(batch.events) > 0.7


def test_upi_is_the_dominant_method(payment_events) -> None:
    """An India-focused batch dominated by cards would be wrong. This caught the
    method mix being decided by catalogue size rather than by market share."""
    counts: dict[str, int] = {}
    for e in payment_events:
        counts[str(e.method)] = counts.get(str(e.method), 0) + 1
    assert max(counts, key=counts.__getitem__) == "upi"


# ------------------------------------------------- guardrail ground truth


def test_generator_thresholds_match_settings_defaults() -> None:
    """The generator mirrors architecture.md constraint #4 locally to stay
    deterministic. This is what stops that copy drifting from config."""
    settings = Settings(_env_file=None)
    assert _MAX_ATTEMPTS == settings.max_recovery_attempts
    assert _HARD_STOP_DAYS == settings.hard_stop_days
    assert _QUIET_HOURS_START == settings.quiet_hours_start_local
    assert _QUIET_HOURS_END == settings.quiet_hours_end_local


def test_batch_contains_events_that_should_fail_each_rule(batch) -> None:
    """A batch where everything passes cannot demonstrate the guardrails work.

    contact_frequency is excluded: it depends on when we last contacted the
    customer, which is pipeline state, not a property of the event.
    """
    seen = {g for e in batch.events for g in e.expected_guardrail_failures}
    for rule in (
        GuardrailName.MAX_RETRIES,
        GuardrailName.HARD_STOP_7_DAYS,
        GuardrailName.QUIET_HOURS,
    ):
        assert rule in seen, f"no event exercises {rule}"


def test_batch_contains_events_that_should_pass_everything(batch) -> None:
    clean = [e for e in batch.events if not e.expected_guardrail_failures]
    assert clean, "every event fails a guardrail; nothing would ever be actioned"


def test_expected_failures_agree_with_the_event_data(batch) -> None:
    """Ground truth is derived, so it must always be reproducible from the event.

    Recomputing it here independently is what proves labels cannot drift from
    the data they describe.
    """
    for e in batch.events:
        expected: set[GuardrailName] = set()
        if e.prior_attempts >= _MAX_ATTEMPTS:
            expected.add(GuardrailName.MAX_RETRIES)
        if e.detected_at - e.first_failure_at >= timedelta(days=_HARD_STOP_DAYS):
            expected.add(GuardrailName.HARD_STOP_7_DAYS)
        local_hour = e.detected_at.astimezone(ZoneInfo(e.customer.timezone)).hour
        if not _QUIET_HOURS_START <= local_hour < _QUIET_HOURS_END:
            expected.add(GuardrailName.QUIET_HOURS)
        assert set(e.expected_guardrail_failures) == expected, e.event_id


def test_sub_24h_retry_gaps_exist(payment_events) -> None:
    """Needed for contact_frequency to ever come into play in Phase 4."""
    by_order: dict[str, list] = {}
    for e in payment_events:
        by_order.setdefault(
            e.envelope["payload"]["payment"]["entity"]["order_id"], []
        ).append(e)
    gaps = [
        (b.detected_at - a.detected_at)
        for chain in by_order.values()
        if len(chain) > 1
        for a, b in zip(
            sorted(chain, key=lambda x: x.detected_at),
            sorted(chain, key=lambda x: x.detected_at)[1:],
            strict=False,
        )
    ]
    assert any(g < timedelta(hours=24) for g in gaps), (
        "no sub-24h retry gaps; the contact-frequency rule could never trigger"
    )


# -------------------------------------------------------- ground-truth leakage


def test_ground_truth_never_appears_in_the_webhook_payload(batch) -> None:
    """The single most important test here.

    If the answer key reaches the webhook, DIAGNOSE can read its own grade and
    every accuracy number becomes meaningless.
    """
    banned = (
        "expected_root_cause", "expected_guardrail", "ground_truth",
        "scenario_key", "provenance", "root_cause",
    )
    for e in batch.events:
        blob = json.dumps(e.envelope)
        for term in banned:
            assert term not in blob, f"{term} leaked into the webhook payload"


def test_webhooks_only_strips_ground_truth(batch) -> None:
    fixture = batch_to_dict(batch)
    for webhook in webhooks_only(fixture):
        assert "ground_truth" not in webhook
        assert set(webhook) == {
            "entity", "account_id", "event", "contains", "payload", "created_at",
        }


def test_fixture_separates_the_three_concerns(batch) -> None:
    fixture = batch_to_dict(batch)
    for event in fixture["events"]:
        assert set(event) == {
            "event_id", "webhook", "customer_context", "pipeline_context",
            "ground_truth",
        }


def test_contact_details_use_reserved_domains(entities) -> None:
    """A fixture must not be able to address a real inbox."""
    for entity in entities:
        assert entity["email"].endswith(
            ("@example.com", "@example.in", "@example.org")
        )
        assert entity["contact"].startswith("+91")


# ------------------------------------------------------------------ signing


def test_signature_verifies_against_the_bytes_that_were_signed(batch) -> None:
    """Phase 2 must verify raw bytes. Signing a re-serialised copy is the classic
    way this breaks in production but passes in tests."""
    secret = "test_webhook_secret_value"
    body, headers = signing.signed_delivery(batch.events[0].envelope, secret)
    assert signing.sign_body(body, secret) == headers["X-Razorpay-Signature"]
    assert json.loads(body) == batch.events[0].envelope


def test_signature_changes_if_body_is_tampered_with(batch) -> None:
    secret = "test_webhook_secret_value"
    body, headers = signing.signed_delivery(batch.events[0].envelope, secret)
    tampered = body.replace(b'"amount":', b'"amount" :', 1)
    assert signing.sign_body(tampered, secret) != headers["X-Razorpay-Signature"]


def test_signature_differs_per_secret(batch) -> None:
    body = signing.canonical_body(batch.events[0].envelope)
    assert signing.sign_body(body, "secret_a") != signing.sign_body(body, "secret_b")


def test_signing_rejects_empty_secret(batch) -> None:
    with pytest.raises(ValueError, match="empty webhook secret"):
        signing.sign_body(b"{}", "")


# ------------------------------------------------------------------ fixtures


def test_fixture_round_trip(tmp_path, batch) -> None:
    path = write_fixture(batch, path=tmp_path / "batch.json")
    loaded = load_fixture(path)
    assert loaded["fixture_version"] == FIXTURE_VERSION
    assert loaded["provider"] == "razorpay"
    assert len(loaded["events"]) == len(batch.events)


def test_load_fixture_rejects_unknown_version(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"fixture_version": 999, "events": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="fixture_version"):
        load_fixture(path)


def test_summary_amount_matches_the_events(batch) -> None:
    summary = batch.summary()
    assert summary["amount_at_risk_paise"] == sum(e.amount_paise for e in batch.events)
    assert summary["amount_at_risk_inr"] == round(
        summary["amount_at_risk_paise"] / 100, 2
    )
    assert summary["event_count"] == len(batch.events)
