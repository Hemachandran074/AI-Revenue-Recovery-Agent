"""Provider webhook routes.

The one place raw request bytes matter. Signature verification runs against the
body exactly as received, before any parsing, because re-serialising parsed JSON
reorders keys and changes whitespace and would fail against a genuine signature.

STATUS CODES ARE CHOSEN AROUND RAZORPAY'S RETRY BEHAVIOUR
Razorpay retries deliveries that do not get a 2xx, so the code returned decides
whether an event comes back:

  200 detected            processed
  200 duplicate           already seen; retrying would double-count
  200 ignored             a real event type we do not handle; never want it again
  200 outcome_confirmed   a payment settled an event we had flagged as at risk
  200 unmatched           a genuine payment that matches nothing we track. Still
                          2xx: the delivery was valid and fully processed, and
                          redelivering it would never produce a match
  400 malformed           shape is wrong, so a retry cannot help, but it is worth
                          surfacing rather than swallowing
  401 bad signature       could not be shown to come from Razorpay. Retryable on
                          purpose: a misconfigured secret is a fixable condition,
                          and legitimate events should arrive once it is fixed
  500 unexpected          retry is appropriate

Rejections are logged with a reason but never echo the expected signature or the
secret.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy.orm import Session

from app import detect, outcomes, pipeline, signature
from app.config import Settings, get_settings
from app.db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/razorpay")
async def razorpay_webhook(
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    x_razorpay_signature: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Receive a Razorpay webhook, verify it, and route it.

    Three destinations: a paid event goes to outcome confirmation, an at-risk
    event goes through the pipeline, and anything else is acknowledged and
    ignored.
    """
    # Raw bytes first. Nothing may parse this body before it is verified.
    body = await request.body()

    try:
        signature.verify_signature(
            body, x_razorpay_signature, settings.razorpay_webhook_secret
        )
    except signature.InvalidSignatureError as exc:
        logger.warning(
            "rejected webhook: signature verification failed (%s)", exc.reason
        )
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return {"status": "rejected", "reason": str(exc.reason)}

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        logger.warning("rejected webhook: body is not valid JSON")
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {"status": "rejected", "reason": "invalid_json"}

    # Paid events are handled before DETECT, which would reject them as an
    # unsupported type. This is architecture.md's closing arrow: the only path
    # allowed to state that money came back.
    event_name = payload.get("event") if isinstance(payload, dict) else None
    if event_name in outcomes.SUPPORTED_OUTCOME_EVENTS:
        try:
            result = outcomes.confirm_outcome(session, payload)
        except outcomes.MalformedOutcomeError as exc:
            logger.warning("rejected outcome webhook: %s", exc)
            response.status_code = status.HTTP_400_BAD_REQUEST
            return {
                "status": "rejected",
                "reason": "malformed_outcome",
                "detail": str(exc),
            }
        if isinstance(result, outcomes.Unmatched):
            # 200 deliberately. The delivery was valid and we processed it; there
            # was simply nothing of ours to credit. A non-2xx would make Razorpay
            # redeliver a payment that will never match.
            return {"status": "unmatched", **result.to_dict()}
        return {"status": "outcome_confirmed", **result.to_dict()}

    try:
        if settings.pipeline_run_inline:
            outcome = pipeline.process_event(session, payload)
            if outcome.is_duplicate:
                return {"status": "duplicate", "event_id": outcome.event_record.event_id}
            return {
                "status": "detected",
                "event_id": outcome.event_record.event_id,
                "event_type": str(outcome.event_record.event_type),
                "decline_code": outcome.event_record.decline_code,
                **{k: v for k, v in outcome.summary().items() if k != "event_id"},
            }
        result = detect.detect_event(session, payload)
    except detect.UnsupportedEventError as exc:
        # Acknowledged deliberately. A non-2xx would make Razorpay keep retrying
        # an event type we are never going to process.
        logger.info("ignoring unsupported webhook event: %s", exc.event_name)
        return {"status": "ignored", "event": exc.event_name}
    except detect.MalformedPayloadError as exc:
        logger.warning("rejected webhook: malformed payload (%s)", exc)
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {"status": "rejected", "reason": "malformed_payload", "detail": str(exc)}

    if result.is_duplicate:
        return {
            "status": "duplicate",
            "event_id": result.event_record.event_id,
        }

    return {
        "status": "detected",
        "event_id": result.event_record.event_id,
        "event_type": str(result.event_record.event_type),
        "decline_code": result.event_record.decline_code,
        "customer_profile_defaulted": result.profile_was_defaulted,
    }
