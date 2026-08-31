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

from app import detect, pipeline, signature
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
    """Receive a Razorpay webhook, verify it, and run DETECT."""
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
