"""DIAGNOSE tests.

All of these use a fake client. Real API calls would be slow, cost quota and
return different text run to run, so the behaviour that matters — taxonomy
enforcement, the confidence floor, retry and fallback — is tested against
controlled responses. Accuracy against the real model is measured separately by
``python -m app.diagnose_eval``, which is a measurement rather than a test.

``code-standards.md`` asks for one test per taxonomy category; those are the
parametrised cases in the first section.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app import diagnose
from app.config import Settings, get_settings
from app.diagnose import (
    ALLOWED_CAUSES,
    PROMPT_VERSION,
    DiagnoseNotConfiguredError,
    ProviderContext,
    audit_summaries,
    diagnose_root_cause,
    load_prompt,
    render_event,
)
from app.schemas import CustomerHistory, Diagnosis, EventRecord, EventType, RootCause


class FakeClient:
    """Returns queued responses and records what it was asked.

    A list rather than one value so retry behaviour can be exercised: the first
    reply can be broken and the second sound.
    """

    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, str]] = []

    def generate(self, *, system_prompt: str, user_content: str) -> str:
        self.calls.append({"system_prompt": system_prompt, "user_content": user_content})
        if not self.responses:
            raise AssertionError("FakeClient ran out of queued responses")
        reply = self.responses.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class ExplodingClient:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def generate(self, *, system_prompt: str, user_content: str) -> str:
        self.calls += 1
        raise self.exc


def reply(root_cause: str, confidence: float = 0.95, reasoning: str = "because") -> str:
    return json.dumps(
        {"root_cause": root_cause, "confidence": confidence, "reasoning": reasoning}
    )


def make_event(
    *,
    event_type: EventType = EventType.PAYMENT_FAILED,
    decline_code: str | None = "insufficient_funds",
    prior_attempts: int = 0,
    event_id: str = "11111111-1111-5111-8111-111111111111",
) -> EventRecord:
    return EventRecord(
        event_id=event_id,
        customer_id="cust_test",
        event_type=event_type,
        decline_code=decline_code,
        amount=Decimal("499.00"),
        currency="INR",
        prior_attempts=prior_attempts,
        customer_history=CustomerHistory(tenure_days=120, past_failures=1),
        detected_at=datetime.now(UTC),
    )


@pytest.fixture(autouse=True)
def _pinned_settings(monkeypatch):
    """Fixed threshold and no .env dependency, so tests do not drift with config."""
    settings = Settings(
        _env_file=None,
        gemini_api_key="test-key-not-used",
        diagnose_confidence_threshold=0.75,
    )
    monkeypatch.setattr("app.diagnose.get_settings", lambda: settings)
    return settings


# ------------------------------------------------- one test per taxonomy category


PAYMENT_CAUSES = [
    RootCause.CARD_EXPIRED,
    RootCause.INSUFFICIENT_FUNDS,
    RootCause.BANK_RISK_BLOCK,
    RootCause.SCA_ABANDONED,
    RootCause.NETWORK_ERROR,
    RootCause.UNKNOWN,
]
ABANDONMENT_CAUSES = [
    RootCause.CHECKOUT_FRICTION,
    RootCause.GENUINE_ABANDONMENT,
    RootCause.UNKNOWN,
]


@pytest.mark.parametrize("cause", PAYMENT_CAUSES)
def test_each_payment_failure_category_round_trips(cause: RootCause) -> None:
    client = FakeClient(reply(cause.value))
    result = diagnose_root_cause(make_event(), client=client)
    assert result.root_cause is cause
    assert isinstance(result, Diagnosis)


@pytest.mark.parametrize("cause", ABANDONMENT_CAUSES)
def test_each_abandonment_category_round_trips(cause: RootCause) -> None:
    client = FakeClient(reply(cause.value))
    result = diagnose_root_cause(
        make_event(event_type=EventType.CHECKOUT_ABANDONED, decline_code=None),
        client=client,
    )
    assert result.root_cause is cause


def test_all_eight_categories_are_reachable() -> None:
    """Every category in the taxonomy must be producible by some event type.

    A category no event can ever receive would be dead weight in the action table.
    """
    reachable = set()
    for allowed in ALLOWED_CAUSES.values():
        reachable |= set(allowed)
    assert reachable == set(RootCause)


# ------------------------------------------------------------ taxonomy enforcement


def test_free_text_category_is_rejected_and_escalated() -> None:
    """The exact drift the fixed taxonomy exists to prevent.

    'low_funds' is plausible-looking and wrong. It must not be coerced into
    insufficient_funds.
    """
    client = FakeClient(reply("low_funds"), reply("low_funds"))
    result = diagnose_root_cause(make_event(), client=client)
    assert result.root_cause is RootCause.UNKNOWN
    assert len(client.calls) == 2


@pytest.mark.parametrize(
    "cause", ["InsufficientFunds", "INSUFFICIENT_FUNDS", "insufficient funds", ""]
)
def test_near_miss_category_spellings_are_rejected(cause: str) -> None:
    client = FakeClient(reply(cause), reply(cause))
    result = diagnose_root_cause(make_event(), client=client)
    assert result.root_cause is RootCause.UNKNOWN


def test_cause_impossible_for_the_event_type_is_rejected() -> None:
    """A failed payment cannot be checkout_friction.

    Without this, DECIDE would be handed an action that contradicts the event.
    """
    client = FakeClient(reply("checkout_friction"), reply("checkout_friction"))
    result = diagnose_root_cause(make_event(), client=client)
    assert result.root_cause is RootCause.UNKNOWN
    assert "not possible" in result.reasoning


def test_abandonment_cannot_be_classified_as_a_card_problem() -> None:
    """No card was ever charged on an abandoned checkout."""
    client = FakeClient(reply("card_expired"), reply("card_expired"))
    result = diagnose_root_cause(
        make_event(event_type=EventType.CHECKOUT_ABANDONED, decline_code=None),
        client=client,
    )
    assert result.root_cause is RootCause.UNKNOWN


def test_impossible_cause_is_retried_before_giving_up() -> None:
    """A recoverable slip should not cost the event its classification."""
    client = FakeClient(reply("checkout_friction"), reply("insufficient_funds"))
    result = diagnose_root_cause(make_event(), client=client)
    assert result.root_cause is RootCause.INSUFFICIENT_FUNDS
    assert len(client.calls) == 2


# --------------------------------------------------------------- confidence floor


def test_low_confidence_is_rerouted_to_unknown() -> None:
    """The probe's forced guess came back at 0.70. This is what catches it."""
    client = FakeClient(reply("bank_risk_block", confidence=0.70))
    result = diagnose_root_cause(make_event(), client=client)
    assert result.root_cause is RootCause.UNKNOWN


def test_override_preserves_what_the_model_proposed() -> None:
    """A reviewer must see the discarded opinion, not just 'unknown'.

    Otherwise the trail hides that a judgement was made and set aside.
    """
    client = FakeClient(
        reply("bank_risk_block", confidence=0.61, reasoning="bank refused it")
    )
    result = diagnose_root_cause(make_event(), client=client)
    assert result.root_cause is RootCause.UNKNOWN
    assert "bank_risk_block" in result.reasoning
    assert "0.61" in result.reasoning
    assert "bank refused it" in result.reasoning
    assert "0.75" in result.reasoning


def test_confidence_at_the_threshold_is_accepted() -> None:
    """The floor is inclusive, so a boundary value is not silently escalated."""
    client = FakeClient(reply("card_expired", confidence=0.75))
    assert diagnose_root_cause(make_event(), client=client).root_cause is (
        RootCause.CARD_EXPIRED
    )


def test_unknown_is_exempt_from_the_confidence_floor() -> None:
    """Rerouting a low-confidence unknown to unknown would be a no-op that
    destroyed the model's own reasoning."""
    client = FakeClient(
        reply("unknown", confidence=0.2, reasoning="reason code says only that it failed")
    )
    result = diagnose_root_cause(make_event(), client=client)
    assert result.root_cause is RootCause.UNKNOWN
    assert result.confidence == pytest.approx(0.2)
    assert result.reasoning == "reason code says only that it failed"


def test_threshold_is_configurable(monkeypatch) -> None:
    lenient = Settings(
        _env_file=None, gemini_api_key="k", diagnose_confidence_threshold=0.5
    )
    monkeypatch.setattr("app.diagnose.get_settings", lambda: lenient)
    client = FakeClient(reply("bank_risk_block", confidence=0.6))
    assert diagnose_root_cause(make_event(), client=client).root_cause is (
        RootCause.BANK_RISK_BLOCK
    )


def test_out_of_range_confidence_is_rejected() -> None:
    """A model reporting 1.4 is malfunctioning; accepting it would corrupt routing."""
    client = FakeClient(reply("card_expired", confidence=1.4), reply("card_expired", 1.4))
    assert diagnose_root_cause(make_event(), client=client).root_cause is (
        RootCause.UNKNOWN
    )


# ----------------------------------------------------------- malformed responses


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "not json at all",
        "[1, 2, 3]",
        '{"root_cause": "card_expired"}',
        '{"confidence": 0.9, "reasoning": "x"}',
        '{"root_cause": "card_expired", "confidence": 0.9, "reasoning": ""}',
    ],
)
def test_malformed_responses_escalate_rather_than_crash(bad: str) -> None:
    """An unclassifiable event must never be dropped.

    Raising here would lose revenue with no audit record of why.
    """
    client = FakeClient(bad, bad)
    result = diagnose_root_cause(make_event(), client=client)
    assert result.root_cause is RootCause.UNKNOWN
    assert "two attempts" in result.reasoning


def test_markdown_fenced_json_is_still_accepted() -> None:
    """Models sometimes fence JSON despite a JSON mime type. Rejecting a
    well-formed answer over formatting would waste a good classification."""
    fenced = "```json\n" + reply("network_error") + "\n```"
    result = diagnose_root_cause(make_event(), client=FakeClient(fenced))
    assert result.root_cause is RootCause.NETWORK_ERROR


def test_transient_error_is_retried_then_escalated() -> None:
    client = ExplodingClient(TimeoutError("upstream timeout"))
    result = diagnose_root_cause(make_event(), client=client)
    assert result.root_cause is RootCause.UNKNOWN
    assert client.calls == 2
    assert "TimeoutError" in result.reasoning


def test_recovers_when_the_second_attempt_succeeds() -> None:
    client = FakeClient("garbage", reply("sca_abandoned", confidence=0.9))
    result = diagnose_root_cause(make_event(), client=client)
    assert result.root_cause is RootCause.SCA_ABANDONED


def test_only_one_retry_is_made() -> None:
    """Quota spent on a persistent failure delays every other event."""
    client = ExplodingClient(RuntimeError("boom"))
    diagnose_root_cause(make_event(), client=client)
    assert client.calls == 2


# ------------------------------------------------------------------ event id


def test_event_id_comes_from_the_record_not_the_model() -> None:
    """The model is never asked for an id it could get wrong."""
    spoofed = json.dumps(
        {
            "event_id": "99999999-9999-4999-8999-999999999999",
            "root_cause": "card_expired",
            "confidence": 0.9,
            "reasoning": "x",
        }
    )
    event = make_event(event_id="22222222-2222-5222-8222-222222222222")
    result = diagnose_root_cause(event, client=FakeClient(spoofed))
    assert result.event_id == event.event_id


# ------------------------------------------------------------------- prompt


def test_prompt_is_loaded_from_disk() -> None:
    """code-standards.md requires a versioned file, not an inlined string."""
    text = load_prompt()
    assert len(text) > 500
    for cause in RootCause:
        assert cause.value in text, f"{cause.value} is undocumented in the prompt"


def test_prompt_states_that_unknown_is_acceptable() -> None:
    """The behaviour the probe showed missing. Without it the model guesses."""
    text = load_prompt().lower()
    assert "unknown" in text
    assert "payment_failed" in text


def test_prompt_is_sent_as_the_system_instruction() -> None:
    client = FakeClient(reply("card_expired"))
    diagnose_root_cause(make_event(), client=client)
    assert client.calls[0]["system_prompt"] == load_prompt()


# -------------------------------------------------------------- rendered input


def test_rendered_input_carries_the_classification_signals() -> None:
    event = make_event(decline_code="card_expired")
    rendered = render_event(
        event,
        ProviderContext(
            error_source="issuer_bank",
            error_step="payment_authorization",
            payment_method="card",
        ),
    )
    assert "decline_code: card_expired" in rendered
    assert "error_source: issuer_bank" in rendered
    assert "error_step: payment_authorization" in rendered
    assert "event_type: payment_failed" in rendered


def test_rendered_input_omits_personal_data() -> None:
    """Contact details cannot inform a root cause, so there is no reason to send
    them to a third-party API."""
    rendered = render_event(make_event())
    assert "@" not in rendered
    assert "+91" not in rendered
    assert "cust_test" not in rendered


def test_rendered_input_omits_fields_no_rule_uses() -> None:
    """Amount, tenure and past failures were removed deliberately.

    Sending a model fields its instructions never reference invites invented
    correlations, and they varied per event badly enough to defeat the cache.
    """
    event = make_event()
    rendered = render_event(event)
    assert "amount" not in rendered
    assert "tenure" not in rendered
    assert "past_failures" not in rendered
    assert "499" not in rendered


def test_prior_attempts_is_sent_only_for_abandonment() -> None:
    """Rule 2 uses it to separate friction from disinterest.

    A payment failure's cause does not depend on attempt count — a card is expired
    however often it was tried — so including it there would only fragment the
    cache.
    """
    payment = render_event(make_event(prior_attempts=3))
    assert "prior_attempts" not in payment

    abandoned = render_event(
        make_event(
            event_type=EventType.CHECKOUT_ABANDONED,
            decline_code=None,
            prior_attempts=3,
        )
    )
    assert "prior_attempts: 3" in abandoned


def test_events_differing_only_in_irrelevant_fields_share_a_cache_entry() -> None:
    """Two failures with identical evidence must cost one API call between them.

    This is what bounds API calls by scenario variety rather than batch size.
    """
    cache: dict[str, Any] = {}
    client = FakeClient(reply("card_expired"))

    a = EventRecord(
        event_id="55555555-5555-5555-8555-555555555555",
        customer_id="cust_a",
        event_type=EventType.PAYMENT_FAILED,
        decline_code="card_expired",
        amount=Decimal("199.00"),
        currency="INR",
        prior_attempts=0,
        customer_history=CustomerHistory(tenure_days=10, past_failures=0),
        detected_at=datetime.now(UTC),
    )
    b = EventRecord(
        event_id="66666666-6666-5666-8666-666666666666",
        customer_id="cust_b",
        event_type=EventType.PAYMENT_FAILED,
        decline_code="card_expired",
        amount=Decimal("9999.00"),
        currency="INR",
        prior_attempts=2,
        customer_history=CustomerHistory(tenure_days=900, past_failures=7),
        detected_at=datetime.now(UTC),
    )

    ctx = ProviderContext(error_source="customer", error_step="payment_authorization")
    diagnose_root_cause(a, ctx, client=client, cache=cache)
    diagnose_root_cause(b, ctx, client=client, cache=cache)
    assert len(client.calls) == 1


def test_missing_fields_render_as_null_not_none() -> None:
    """`None` leaking into the prompt reads as Python, not data."""
    rendered = render_event(make_event(decline_code=None))
    assert "decline_code: null" in rendered
    assert "None" not in rendered


# ------------------------------------------------------------------ audit trail


def test_audit_summary_records_reasoning_model_and_prompt_version() -> None:
    """ai-workflow-rules.md requires the one-sentence reasoning to be logged.

    Model and prompt version travel with it so a behaviour change can be traced
    to whichever of the two moved.
    """
    event = make_event()
    result = diagnose_root_cause(
        event, client=FakeClient(reply("card_expired", 0.93, "card is past expiry"))
    )
    inputs, outputs = audit_summaries(event, result)

    assert inputs["prompt_version"] == PROMPT_VERSION
    assert inputs["model"]
    assert inputs["decline_code"] == "insufficient_funds"
    assert outputs["root_cause"] == "card_expired"
    assert outputs["reasoning"] == "card is past expiry"
    assert outputs["escalated_to_human_review"] is False


def test_audit_summary_flags_escalation() -> None:
    event = make_event()
    result = diagnose_root_cause(event, client=FakeClient(reply("unknown")))
    _, outputs = audit_summaries(event, result)
    assert outputs["escalated_to_human_review"] is True


# ------------------------------------------------------------------ configuration


def test_missing_api_key_fails_loudly(monkeypatch) -> None:
    """No silent fallback to a rules engine.

    A demo that quietly skipped the LLM would look like the stage worked when it
    never ran.
    """
    monkeypatch.setattr(
        "app.diagnose.get_settings",
        lambda: Settings(_env_file=None, gemini_api_key=None),
    )
    with pytest.raises(DiagnoseNotConfiguredError, match="GEMINI_API_KEY"):
        diagnose.build_client()


def test_real_settings_expose_the_threshold() -> None:
    """The configured default must match what .env.example documents."""
    get_settings.cache_clear()
    assert 0.0 <= get_settings().diagnose_confidence_threshold <= 1.0


# --------------------------------------------- rate limits vs real uncertainty


RATE_LIMIT_MESSAGE = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your "
    "current quota... Please retry in 35.48754161s.', 'status': 'RESOURCE_EXHAUSTED', "
    "'details': [{'@type': 'type.googleapis.com/google.rpc.RetryInfo', "
    "'retryDelay': '35s'}]}}"
)


def test_rate_limit_errors_are_recognised() -> None:
    assert diagnose.is_rate_limited(RuntimeError(RATE_LIMIT_MESSAGE))
    assert diagnose.is_rate_limited(RuntimeError("Quota exceeded for metric"))
    assert not diagnose.is_rate_limited(ValueError("malformed JSON"))


def test_suggested_delay_is_read_from_the_error() -> None:
    """Retrying a per-minute quota immediately cannot succeed, so the stated
    wait has to be honoured."""
    delay = diagnose.suggested_retry_delay(RuntimeError(RATE_LIMIT_MESSAGE))
    assert delay is not None
    assert 30 <= delay <= diagnose.MAX_RATE_LIMIT_WAIT_SECONDS


def test_suggested_delay_is_capped() -> None:
    """An absurd delay must not stall a batch indefinitely."""
    delay = diagnose.suggested_retry_delay(RuntimeError("retryDelay: '99999s'"))
    assert delay == diagnose.MAX_RATE_LIMIT_WAIT_SECONDS


def test_no_delay_when_the_error_states_none() -> None:
    assert diagnose.suggested_retry_delay(RuntimeError("429 quota")) is None


def test_rate_limited_unknown_is_marked_as_unavailable(monkeypatch) -> None:
    """The distinction that keeps metrics honest.

    A quota outage escalating to human review is the safe outcome, but reporting
    it as cautious diagnosis would let a broken run look like good judgement.
    """
    monkeypatch.setattr(diagnose.time, "sleep", lambda _s: None)
    client = ExplodingClient(RuntimeError(RATE_LIMIT_MESSAGE))
    result = diagnose_root_cause(make_event(), client=client)

    assert result.root_cause is RootCause.UNKNOWN
    assert result.reasoning.startswith(diagnose.CLASSIFIER_UNAVAILABLE_PREFIX)
    assert "rate limited" in result.reasoning
    assert "operational failure" in result.reasoning


def test_rate_limited_run_waits_before_retrying(monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(diagnose.time, "sleep", lambda s: slept.append(s))
    diagnose_root_cause(
        make_event(), client=ExplodingClient(RuntimeError(RATE_LIMIT_MESSAGE))
    )
    assert slept, "a rate-limited attempt must wait before retrying"
    assert slept[0] >= 30


def test_malformed_response_does_not_wait(monkeypatch) -> None:
    """A broken reply is not a quota problem; sleeping would waste batch time."""
    slept: list[float] = []
    monkeypatch.setattr(diagnose.time, "sleep", lambda s: slept.append(s))
    diagnose_root_cause(make_event(), client=FakeClient("garbage", "garbage"))
    assert slept == []


def test_evidence_based_unknown_is_not_marked_unavailable() -> None:
    """A real diagnosis of 'cannot tell' must be distinguishable from an outage."""
    client = FakeClient(reply("unknown", 1.0, "reason code says only that it failed"))
    result = diagnose_root_cause(make_event(), client=client)
    assert not result.reasoning.startswith(diagnose.CLASSIFIER_UNAVAILABLE_PREFIX)


def test_audit_summary_separates_outage_from_uncertainty(monkeypatch) -> None:
    monkeypatch.setattr(diagnose.time, "sleep", lambda _s: None)
    event = make_event()

    outage = diagnose_root_cause(
        event, client=ExplodingClient(RuntimeError(RATE_LIMIT_MESSAGE))
    )
    _, outage_out = audit_summaries(event, outage)
    assert outage_out["classifier_unavailable"] is True
    assert outage_out["escalated_to_human_review"] is True

    genuine = diagnose_root_cause(event, client=FakeClient(reply("unknown")))
    _, genuine_out = audit_summaries(event, genuine)
    assert genuine_out["classifier_unavailable"] is False
    assert genuine_out["escalated_to_human_review"] is True


# ------------------------------------------------------------------- caching


def test_identical_evidence_reuses_a_classification() -> None:
    """Free-tier quota is per minute, so asking the same question twice is waste.

    Sound because temperature is 0: identical evidence should classify identically.
    """
    cache: dict[str, Any] = {}
    client = FakeClient(reply("card_expired"))
    first = diagnose_root_cause(make_event(), client=client, cache=cache)
    second = diagnose_root_cause(make_event(), client=client, cache=cache)

    assert len(client.calls) == 1
    assert first.root_cause is second.root_cause is RootCause.CARD_EXPIRED


def test_cached_result_still_carries_the_right_event_id() -> None:
    """A cache hit must not leak the first event's identity onto the second."""
    cache: dict[str, Any] = {}
    client = FakeClient(reply("card_expired"))
    a = make_event(event_id="33333333-3333-5333-8333-333333333333")
    b = make_event(event_id="44444444-4444-5444-8444-444444444444")
    diagnose_root_cause(a, client=client, cache=cache)
    result = diagnose_root_cause(b, client=client, cache=cache)
    assert result.event_id == b.event_id


def test_different_evidence_is_not_served_from_cache() -> None:
    cache: dict[str, Any] = {}
    client = FakeClient(reply("card_expired"), reply("insufficient_funds"))
    diagnose_root_cause(make_event(decline_code="card_expired"), client=client, cache=cache)
    second = diagnose_root_cause(
        make_event(decline_code="insufficient_funds"), client=client, cache=cache
    )
    assert len(client.calls) == 2
    assert second.root_cause is RootCause.INSUFFICIENT_FUNDS


def test_failures_are_not_cached(monkeypatch) -> None:
    """Caching an outage would poison every later event with the same evidence."""
    monkeypatch.setattr(diagnose.time, "sleep", lambda _s: None)
    cache: dict[str, Any] = {}
    diagnose_root_cause(
        make_event(),
        client=ExplodingClient(RuntimeError(RATE_LIMIT_MESSAGE)),
        cache=cache,
    )
    assert cache == {}

    client = FakeClient(reply("card_expired"))
    result = diagnose_root_cause(make_event(), client=client, cache=cache)
    assert result.root_cause is RootCause.CARD_EXPIRED


def test_prompt_version_is_recorded_and_current() -> None:
    """The audit trail must name the prompt that produced a classification, so a
    behaviour change can be traced to a prompt change."""
    assert PROMPT_VERSION in load_prompt()
