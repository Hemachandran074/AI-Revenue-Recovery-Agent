"""DIAGNOSE — stage 2. LLM classification into the fixed root-cause taxonomy.

Contract:
    EventRecord (+ optional provider context) -> Diagnosis

The only stage that calls an LLM. It classifies; it never acts, and it never
touches a payment-provider API.

## Four layers stop a bad classification reaching DECIDE

A language model asked to pick a category will pick one, whether or not the
evidence supports it. The probe that preceded this module returned
``bank_risk_block`` at confidence 0.7 for a decline whose only stated reason was
``payment_failed``, and said in its own reasoning that it had chosen the more
common possibility. That is a guess wearing a confidence score, and if it had
guessed ``insufficient_funds`` instead the pipeline would have scheduled a retry
against a card the issuer refused. So:

1. **Schema constraint at the API.** Gemini is given a response schema, so the
   category comes back as an enum member rather than free text.
2. **Pydantic validation after the call.** The API constraint is not trusted on
   its own; the response is re-validated locally.
3. **Event-type consistency.** A cause impossible for the event type is rejected.
   A failed payment cannot be ``checkout_friction``; an abandoned checkout cannot
   be ``card_expired``.
4. **Confidence floor.** Anything below the threshold is rerouted to ``unknown``,
   which routes to human review. The original classification is preserved in the
   reasoning so the audit trail shows what was overridden and why.

On invalid output: retry once, then fall back to ``unknown``. Never raise a
classification failure into the pipeline, because dropping an event silently
would lose revenue with no trace.

## What the model is NOT asked for

``event_id`` is attached from the event record rather than requested, so a value
already known cannot be corrupted. Nor is the model asked what to do: DECIDE owns
that and is deterministic by design.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError

from app.config import get_settings
from app.schemas import Diagnosis, EventRecord, EventType, RootCause

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "diagnose.md"
PROMPT_VERSION = "v2"

# Marks an `unknown` caused by the classifier being unreachable rather than by the
# evidence being thin. Those two must never be conflated: one is a correct
# diagnosis, the other is an outage. Reporting an outage as "escalated to human
# review" would let a broken run look like cautious behaviour.
CLASSIFIER_UNAVAILABLE_PREFIX = "Classifier unavailable"

# Free-tier Gemini allows only a handful of requests per minute, so a 429 is
# routine rather than exceptional. Retrying it immediately is guaranteed to fail;
# the API states how long to wait and that is worth honouring, up to a cap.
_RATE_LIMIT_MARKERS = ("429", "RESOURCE_EXHAUSTED", "rate limit", "quota")
_RETRY_DELAY_PATTERN = re.compile(r"retry(?:Delay|.{0,3}in)\D{0,4}(\d+(?:\.\d+)?)")
MAX_RATE_LIMIT_WAIT_SECONDS = 65.0

# Causes that make sense for each event type. Layer 3 above: the taxonomy implies
# these pairings even though architecture.md does not spell them out, and letting
# a mismatch through would hand DECIDE an action that contradicts the event.
ALLOWED_CAUSES: dict[EventType, frozenset[RootCause]] = {
    EventType.PAYMENT_FAILED: frozenset(
        {
            RootCause.CARD_EXPIRED,
            RootCause.INSUFFICIENT_FUNDS,
            RootCause.BANK_RISK_BLOCK,
            RootCause.SCA_ABANDONED,
            RootCause.NETWORK_ERROR,
            RootCause.UNKNOWN,
        }
    ),
    EventType.CHECKOUT_ABANDONED: frozenset(
        {
            RootCause.CHECKOUT_FRICTION,
            RootCause.GENUINE_ABANDONMENT,
            RootCause.UNKNOWN,
        }
    ),
    EventType.INVOICE_OVERDUE: frozenset(
        {
            RootCause.CHECKOUT_FRICTION,
            RootCause.GENUINE_ABANDONMENT,
            RootCause.UNKNOWN,
        }
    ),
}


class DiagnoseNotConfiguredError(RuntimeError):
    """Raised when no Gemini API key is available.

    Deliberately explicit rather than falling back to a rules engine. A silent
    downgrade would make a demo look like the LLM stage worked when it never ran.
    """


@dataclass(frozen=True, slots=True)
class ProviderContext:
    """Provider detail we hold but ``EventRecord`` has no field for.

    ``architecture.md``'s Event record carries only ``decline_code``, yet
    ``error_source`` and ``error_step`` are the difference between an opaque
    "payment failed" and an identifiable bank-side or authentication-side fault.
    They are stored on the ``events`` table (see ``models.Event``), so they are
    passed alongside rather than smuggled into ``EventRecord``, which stays a
    verbatim copy of the doc.
    """

    error_code: str | None = None
    error_description: str | None = None
    error_source: str | None = None
    error_step: str | None = None
    payment_method: str | None = None


class LlmDiagnosis(BaseModel):
    """Exactly what the model is asked to return.

    Narrower than :class:`~app.schemas.Diagnosis` on purpose: no ``event_id``,
    because asking for a value we already hold only creates a way to corrupt it.
    """

    root_cause: RootCause
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1)


class SupportsGenerate(Protocol):
    """The slice of the Gemini client this module uses.

    Narrow on purpose so unit tests can supply a fake without importing the SDK
    or spending quota, and so a provider swap touches one adapter.
    """

    def generate(self, *, system_prompt: str, user_content: str) -> str: ...


@lru_cache
def load_prompt() -> str:
    """Read the versioned system prompt from disk.

    Kept in ``prompts/diagnose.md`` rather than inlined so it can be reviewed and
    diffed like any other artifact (``code-standards.md`` -> LLM calls).
    """
    if not PROMPT_PATH.exists():
        raise DiagnoseNotConfiguredError(f"system prompt missing at {PROMPT_PATH}")
    text = PROMPT_PATH.read_text(encoding="utf-8").strip()
    if not text:
        raise DiagnoseNotConfiguredError(f"system prompt at {PROMPT_PATH} is empty")
    return text


class GeminiClient:
    """Thin adapter over ``google-genai``.

    Thinking is disabled: this is bounded classification, not open-ended
    reasoning, and the probe showed it doubling token use for no change in the
    answer. Temperature is 0 so the same event classifies the same way twice,
    which matters for a reproducible metrics run.
    """

    def __init__(self, api_key: str, model: str) -> None:
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model

    def generate(self, *, system_prompt: str, user_content: str) -> str:
        from google.genai import types

        response = self._client.models.generate_content(
            model=self._model,
            contents=user_content,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema=LlmDiagnosis,
                temperature=0.0,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        return response.text or ""


def build_client() -> SupportsGenerate:
    settings = get_settings()
    if not settings.gemini_api_key:
        raise DiagnoseNotConfiguredError(
            "GEMINI_API_KEY is not set, so DIAGNOSE cannot run. Set it in .env; "
            "see /readiness for what is missing."
        )
    return GeminiClient(settings.gemini_api_key, settings.gemini_model)


def render_event(event: EventRecord, context: ProviderContext | None = None) -> str:
    """Render the event as the model's input.

    Deliberately narrow: only evidence that some rule in the prompt actually uses.

    An earlier version also sent ``amount``, ``tenure_days`` and
    ``past_failures``, on the vague grounds that they "colour plausibility". That
    was wrong twice over. No rule in the prompt refers to them, so they gave the
    model room to invent correlations — "many past failures suggests a risk block"
    is exactly the unprincipled inference the fixed taxonomy exists to prevent.
    And because they differ per event, they made 75 events produce 74 distinct
    evidence sets, destroying the cache that keeps a batch inside a per-minute
    quota.

    ``prior_attempts`` is included only for abandonment events, where Rule 2 uses
    it to separate friction from disinterest. For a payment failure it does not
    bear on the cause at all: a card is expired regardless of how many times it
    was tried. It matters to DECIDE's retry limits, not to diagnosis.

    Customer email and contact are never sent. They cannot inform a root cause,
    so there is no reason to hand personal data to a third-party API.

    The consequence worth knowing: API calls are bounded by how many distinct
    failure scenarios exist, not by batch size. A larger batch costs no more.
    """
    ctx = context or ProviderContext()
    lines = [
        f"event_type: {event.event_type}",
        f"decline_code: {event.decline_code or 'null'}",
        f"error_code: {ctx.error_code or 'null'}",
        f"error_description: {ctx.error_description or 'null'}",
        f"error_source: {ctx.error_source or 'null'}",
        f"error_step: {ctx.error_step or 'null'}",
        f"payment_method: {ctx.payment_method or 'null'}",
    ]
    if event.event_type is not EventType.PAYMENT_FAILED:
        lines.append(f"prior_attempts: {event.prior_attempts}")
    return "\n".join(lines)


def _parse_response(raw: str) -> LlmDiagnosis:
    """Validate the model's reply locally.

    The API-level schema is not trusted on its own. Structured output has failure
    modes — truncation, an empty body, a fenced block — and a malformed reply must
    be caught here rather than surfacing as an odd classification downstream.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("model returned an empty response")

    # Strip a markdown fence if one appears despite the JSON mime type.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"model response was not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("model response was not a JSON object")

    try:
        return LlmDiagnosis.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"model response failed schema validation: {exc}") from exc


def is_rate_limited(exc: Exception) -> bool:
    """Whether a failure is a quota/rate-limit refusal rather than a real fault.

    Detected by message content instead of an SDK exception type, so this module
    stays importable and testable without the Gemini SDK installed.
    """
    text = str(exc)
    return any(marker.lower() in text.lower() for marker in _RATE_LIMIT_MARKERS)


def suggested_retry_delay(exc: Exception) -> float | None:
    """The wait the API asked for, in seconds, if it stated one."""
    match = _RETRY_DELAY_PATTERN.search(str(exc))
    if not match:
        return None
    try:
        return min(float(match.group(1)), MAX_RATE_LIMIT_WAIT_SECONDS)
    except ValueError:
        return None


def _unknown(event_id: str, reasoning: str, confidence: float = 1.0) -> Diagnosis:
    """An explicit, auditable ``unknown``.

    Confidence defaults high because certainty that the evidence is insufficient
    is still certainty. Low confidence on ``unknown`` would mean doubting our own
    doubt, which tells a reviewer nothing.
    """
    return Diagnosis(
        event_id=event_id,
        root_cause=RootCause.UNKNOWN,
        confidence=confidence,
        reasoning=reasoning,
    )


def diagnose_root_cause(
    event: EventRecord,
    context: ProviderContext | None = None,
    client: SupportsGenerate | None = None,
    cache: dict[str, LlmDiagnosis] | None = None,
) -> Diagnosis:
    """Classify ``event`` into the fixed root-cause taxonomy.

    Never raises on a classification problem. An unclassifiable event becomes
    ``unknown`` and is escalated to a human, because dropping it would lose
    revenue with no record of why. An ``unknown`` caused by the classifier being
    unreachable is marked with :data:`CLASSIFIER_UNAVAILABLE_PREFIX` so an outage
    is never reported as cautious diagnosis.

    ``client`` is injectable so tests can run every taxonomy category without
    calling the API. ``cache`` maps rendered input to a previous classification;
    pass a shared dict across a batch to avoid re-asking identical questions,
    which matters because free-tier quota is measured in requests per minute.
    """
    settings = get_settings()
    try:
        active = client or build_client()
        prompt = load_prompt()
    except (DiagnoseNotConfiguredError, OSError) as exc:
        # Missing key or unreadable prompt. `build_client()` still raises for
        # callers that want fail-fast at startup, but a per-event failure must not
        # drop the event or 500 a webhook. Marked unavailable, so the trail shows
        # the classifier never ran rather than implying a cautious judgement.
        logger.warning("DIAGNOSE unavailable for %s: %s", event.event_id, exc)
        return _unknown(
            event.event_id,
            (
                f"{CLASSIFIER_UNAVAILABLE_PREFIX}: {exc}. Escalated to human "
                "review as the safe default."
            ),
        )
    user_content = render_event(event, context)

    if cache is not None:
        hit = cache.get(user_content)
        if hit is not None:
            # Same evidence, same classification. Sound because temperature is 0,
            # and it is what makes a batch affordable under a per-minute quota.
            return Diagnosis(
                event_id=event.event_id,
                root_cause=hit.root_cause,
                confidence=hit.confidence,
                reasoning=hit.reasoning,
            )

    last_error: str | None = None
    last_was_rate_limit = False
    # One retry, per code-standards.md. A second failure means the problem is not
    # transient, and burning more quota on it delays every other event.
    for attempt in (1, 2):
        try:
            raw = active.generate(system_prompt=prompt, user_content=user_content)
            parsed = _parse_response(raw)
        except Exception as exc:  # noqa: BLE001 - any failure must degrade, not crash
            last_error = f"{type(exc).__name__}: {exc}"
            last_was_rate_limit = is_rate_limited(exc)
            logger.warning(
                "DIAGNOSE attempt %s failed for %s: %s",
                attempt,
                event.event_id,
                # Quota messages are enormous; the useful part is that it happened.
                "rate limited" if last_was_rate_limit else exc,
            )
            if last_was_rate_limit and attempt == 1:
                # Honour the wait the API asked for. Retrying a per-minute quota
                # immediately cannot succeed, which is what made a 30-event
                # measurement collapse into 24 spurious escalations.
                delay = suggested_retry_delay(exc) or 5.0
                logger.info("DIAGNOSE waiting %.1fs for quota to reset", delay)
                time.sleep(delay)
            continue

        allowed = ALLOWED_CAUSES[event.event_type]
        if parsed.root_cause not in allowed:
            last_error = (
                f"{parsed.root_cause} is not possible for event_type "
                f"{event.event_type}"
            )
            logger.warning(
                "DIAGNOSE attempt %s returned an impossible cause for %s: %s",
                attempt,
                event.event_id,
                last_error,
            )
            continue

        if (
            parsed.root_cause is not RootCause.UNKNOWN
            and parsed.confidence < settings.diagnose_confidence_threshold
        ):
            # Preserve what was overridden. A reviewer needs to see that the model
            # did have an opinion and that it was set aside for being too weak,
            # rather than being told only that the cause is unknown.
            return _unknown(
                event.event_id,
                (
                    f"Escalated to unknown: model proposed {parsed.root_cause} at "
                    f"confidence {parsed.confidence:.2f}, below the "
                    f"{settings.diagnose_confidence_threshold:.2f} threshold. "
                    f"Model reasoning: {parsed.reasoning}"
                ),
            )

        if cache is not None:
            cache[user_content] = parsed

        return Diagnosis(
            event_id=event.event_id,
            root_cause=parsed.root_cause,
            confidence=parsed.confidence,
            reasoning=parsed.reasoning,
        )

    if last_was_rate_limit:
        # Distinct from an evidence-based unknown. Escalating to a human is still
        # the safe outcome, but the trail must show the classifier never ran, or a
        # quota outage would be indistinguishable from cautious diagnosis.
        return _unknown(
            event.event_id,
            (
                f"{CLASSIFIER_UNAVAILABLE_PREFIX}: the classifier was rate limited "
                "and did not run, so no diagnosis was made. Escalated to human "
                "review as the safe default. This is an operational failure, not a "
                "judgement about the evidence."
            ),
        )

    return _unknown(
        event.event_id,
        (
            f"{CLASSIFIER_UNAVAILABLE_PREFIX}: the classifier did not return a "
            f"usable result after two attempts. Last error: {last_error}"
        ),
    )


def audit_summaries(
    event: EventRecord, diagnosis: Diagnosis, context: ProviderContext | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Input and output summaries for the DIAGNOSE audit entry.

    The model's one-sentence reasoning is recorded verbatim, per
    ``ai-workflow-rules.md``, so a reader can judge the classification and not
    just read the label.
    """
    ctx = context or ProviderContext()
    return (
        {
            "event_type": str(event.event_type),
            "decline_code": event.decline_code,
            "error_source": ctx.error_source,
            "error_step": ctx.error_step,
            "payment_method": ctx.payment_method,
            "prior_attempts": event.prior_attempts,
            "prompt_version": PROMPT_VERSION,
            "model": get_settings().gemini_model,
        },
        {
            "root_cause": str(diagnosis.root_cause),
            "confidence": round(diagnosis.confidence, 4),
            "reasoning": diagnosis.reasoning,
            "escalated_to_human_review": diagnosis.root_cause is RootCause.UNKNOWN,
            # Separates "we looked and could not tell" from "we never looked".
            # Metrics must not count an outage as a diagnosis.
            "classifier_unavailable": diagnosis.reasoning.startswith(
                CLASSIFIER_UNAVAILABLE_PREFIX
            ),
        },
    )
