"""Drive one failure through the whole pipeline to your own WhatsApp.

    python -m app.demo_recovery                      # expired card -> update-card link
    python -m app.demo_recovery --cause sca          # abandoned 3-D Secure
    python -m app.demo_recovery --cause friction     # abandoned checkout
    python -m app.demo_recovery --amount 1250
    python -m app.demo_recovery --dry-run            # show what would be sent

WHY THIS EXISTS

``trigger_failure.py`` creates a real Payment Link so a genuine provider webhook
can be produced by failing a payment in the browser. That is the honest end-to-end
path, but it needs ngrok, a registered webhook, and a couple of minutes of
clicking.

This is the short path for checking the *messaging* leg: it synthesises one
``payment.failed`` event addressed to the number in
``TWILIO_WHATSAPP_TEST_RECIPIENTS``, signs it exactly as Razorpay would, and POSTs
it to the running endpoint. Everything after that point is the real thing — real
classifier call, real guardrails, real Razorpay-hosted payment link, real WhatsApp
delivery. Only the arrival of the failure is simulated.

YOU GET ONE REAL DELIVERY PER 24 HOURS, AND THAT IS NOT A BUG

A fresh ``--customer-id`` does **not** get you a fresh customer. DETECT reconciles
identity on contact number before it creates anything, because Razorpay omits
``notes`` on roughly a third of deliveries and without that reconciliation the same
person would split across two customer rows — silently defeating the two rules that
depend on seeing every attempt. So your phone number is one customer, permanently,
whatever id the payload carries.

Which means the second run inside 24 hours is correctly deferred by
``contact_frequency``, and no message arrives. To test repeatedly, lower
``MIN_HOURS_BETWEEN_CONTACTS`` in ``.env`` (the rule still runs and is still
recorded, you are just narrowing the window), or use a second opted-in number.

The event is dated a few minutes ago so the 7-day hard stop cannot fire, and the
tool warns when you are outside contact hours, because a deferral is the correct
outcome then and no message will arrive.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.schemas import EventType
from app.simulation.generator import generate_batch
from app.simulation.signing import signed_delivery

DEFAULT_ENDPOINT = "http://127.0.0.1:8000/webhooks/razorpay"

# Each entry is a decline the classifier should read unambiguously, paired with
# the action it ought to produce. Kept explicit rather than sampled, so a demo
# shows the intended branch instead of whatever the generator happened to pick.
CAUSES: dict[str, dict[str, Any]] = {
    "card": {
        "label": "expired card",
        "expect": "send_update_payment_method_link",
        "method": "card",
        "error_reason": "card_expired",
        "error_description": "Your card has expired. Please use a different card.",
        "error_source": "customer",
        "error_step": "payment_authentication",
    },
    "sca": {
        "label": "abandoned bank verification (3-D Secure)",
        "expect": "send_fresh_auth_link",
        "method": "card",
        "error_reason": "payment_failed",
        "error_description": (
            "Customer did not complete the authentication step on the bank page."
        ),
        "error_source": "customer",
        "error_step": "payment_authentication",
    },
    "friction": {
        "label": "checkout friction",
        "expect": "send_reminder",
        "method": "upi",
        "error_reason": "payment_failed",
        "error_description": (
            "Payment could not be completed at the provider's checkout."
        ),
        "error_source": "gateway",
        "error_step": "payment_initiation",
    },
    "funds": {
        "label": "insufficient funds (no message, retry only)",
        "expect": "schedule_retry",
        "method": "card",
        "error_reason": "insufficient_funds",
        "error_description": "Your card has insufficient balance to complete this payment.",
        "error_source": "issuer",
        "error_step": "authorization",
    },
}


def build_envelope(
    *, cause: dict[str, Any], amount_paise: int, contact: str, customer_id: str
) -> dict[str, Any]:
    """A real-shaped ``payment.failed`` envelope aimed at one phone number."""
    now = datetime.now(UTC)
    batch = generate_batch(seed=int(now.timestamp()) % 100_000, count=6, now=now)
    envelope = next(
        e.envelope for e in batch.events if e.event_type is EventType.PAYMENT_FAILED
    )

    unique = uuid.uuid4().hex[:12]
    entity = envelope["payload"]["payment"]["entity"]
    entity["id"] = f"pay_DEMO{unique}"
    entity["order_id"] = f"order_DEMO{unique}"
    entity["invoice_id"] = f"inv_DEMO{unique}"
    entity["notes"] = {"customer_id": customer_id}
    entity["contact"] = contact
    entity["email"] = "recovery.demo@example.invalid"
    entity["amount"] = amount_paise
    entity["method"] = cause["method"]
    entity["error_code"] = "BAD_REQUEST_ERROR"
    entity["error_reason"] = cause["error_reason"]
    entity["error_description"] = cause["error_description"]
    entity["error_source"] = cause["error_source"]
    entity["error_step"] = cause["error_step"]

    # A few minutes old, so the 7-day hard stop cannot fire on a fresh demo.
    entity["created_at"] = int(now.timestamp()) - 300
    envelope["created_at"] = int(now.timestamp()) - 240
    return envelope


def warn_if_outside_contact_hours(timezone: str = "Asia/Kolkata") -> None:
    settings = get_settings()
    local = datetime.now(ZoneInfo(timezone))
    start, end = settings.quiet_hours_start_local, settings.quiet_hours_end_local
    if not start <= local.hour < end:
        print(
            f"NOTE: it is {local:%H:%M} in {timezone}, outside the "
            f"{start:02d}:00-{end:02d}:00 contact window. The send will be "
            "DEFERRED, not delivered. That is the quiet-hours guardrail working "
            "correctly, not a fault. Re-run in-hours to see a real delivery.\n",
            file=sys.stderr,
        )


def warn_if_contacted_recently(contact: str) -> None:
    """Say up front if the 24h rule will defer this, rather than after the fact.

    Looked up by contact rather than by customer id, because that is how DETECT
    resolves identity: the same number is the same person regardless of what
    ``notes.customer_id`` says.
    """
    from sqlalchemy import select

    from app.db import get_session_factory
    from app.models import Customer

    settings = get_settings()
    session = get_session_factory()()
    try:
        customer = session.scalars(
            select(Customer).where(Customer.contact == contact)
        ).first()
        if customer is None or customer.last_contacted_at is None:
            return
        hours = (
            datetime.now(UTC) - customer.last_contacted_at
        ).total_seconds() / 3600
        if hours < settings.min_hours_between_contacts:
            print(
                f"NOTE: this number was last contacted {hours:.1f}h ago, inside the "
                f"{settings.min_hours_between_contacts}h minimum, so the send will be "
                "DEFERRED. A fresh --customer-id does not help: DETECT resolves "
                "identity by contact number. Lower MIN_HOURS_BETWEEN_CONTACTS in "
                ".env to test repeatedly.\n",
                file=sys.stderr,
            )
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.demo_recovery",
        description="Send one synthetic failure through the pipeline to your phone.",
    )
    parser.add_argument(
        "--cause", choices=sorted(CAUSES), default="card",
        help="Which failure to simulate (default: card).",
    )
    parser.add_argument("--amount", type=float, default=499.0, help="Rupees.")
    parser.add_argument(
        "--customer-id", default=None,
        help=(
            "Written to notes.customer_id. Note that DETECT reconciles identity on "
            "CONTACT, so this does not give you a fresh customer, and the 24h "
            "contact rule still applies to the phone number."
        ),
    )
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument(
        "--contact", default=None,
        help="Override the recipient. Must be on the Twilio allowlist to deliver.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the envelope and stop, without POSTing it.",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    if not settings.razorpay_webhook_secret:
        print(
            "RAZORPAY_WEBHOOK_SECRET is not set, so the endpoint will reject this "
            "as unsigned. Verification fails closed by design.",
            file=sys.stderr,
        )
        return 2

    contact = args.contact
    if contact is None:
        allowlist = settings.whatsapp_recipient_allowlist
        if not allowlist:
            print(
                "TWILIO_WHATSAPP_TEST_RECIPIENTS is empty, so nothing can be "
                "delivered. Add your opted-in sandbox number, or pass --contact "
                "to watch the refusal path instead.",
                file=sys.stderr,
            )
            return 2
        contact = allowlist[0].replace("whatsapp:", "")

    cause = CAUSES[args.cause]
    customer_id = args.customer_id or f"cust_demo_{uuid.uuid4().hex[:10]}"
    amount_paise = int(round(args.amount * 100))

    print(f"simulating   {cause['label']}")
    print(f"expecting    {cause['expect']}")
    print(f"recipient    {contact[:6]}...{contact[-2:]}  (masked)")
    print(f"customer     {customer_id}")
    print(f"amount       Rs {amount_paise / 100:,.2f}\n")

    if args.cause != "funds":
        warn_if_outside_contact_hours()
        warn_if_contacted_recently(contact)

    envelope = build_envelope(
        cause=cause,
        amount_paise=amount_paise,
        contact=contact,
        customer_id=customer_id,
    )

    if args.dry_run:
        print(json.dumps(envelope, indent=2))
        return 0

    body, headers = signed_delivery(envelope, settings.razorpay_webhook_secret)
    request = urllib.request.Request(
        args.endpoint, data=body, headers=headers, method="POST"
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            status, result = response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        status, result = exc.code, json.loads(exc.read())
    except urllib.error.URLError as exc:
        print(
            f"Could not reach {args.endpoint}: {exc.reason}\n"
            "Is the server running?  python -m uvicorn app.main:app --port 8000",
            file=sys.stderr,
        )
        return 1
    elapsed = (time.perf_counter() - started) * 1000

    print(f"HTTP {status} in {elapsed:,.0f} ms")
    print(json.dumps(result, indent=2))

    event_id = result.get("event_id")
    if not event_id:
        return 0 if 200 <= status < 300 else 1

    from app.db import get_session_factory
    from app.models import ExecutionRecord

    session = get_session_factory()()
    try:
        row = session.get(ExecutionRecord, event_id)
        if row is None:
            return 0
        print("\nwhat EXECUTE did")
        print(f"  action           {row.action}")
        print(f"  delivery_status  {row.delivery_status}")
        print(f"  twilio message   {row.provider_message_id or '-'}")
        if row.skip_reason:
            print(f"  skip reason      {row.skip_reason}")
        if row.failure_reason:
            print(f"  failure          {row.failure_reason}")
        if row.recovery_link_url:
            print(f"\n  PAY THIS LINK -> {row.recovery_link_url}")
            print("  Paying it fires payment_link.paid, which is the only thing")
            print("  allowed to record recovered revenue.")
        if row.delivery_status == "sent":
            print("\n  Check your phone. The WhatsApp message should be there.")
        print(f"\n  full trail       {args.endpoint.rsplit('/webhooks', 1)[0]}"
              f"/events/{event_id}/audit")
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
