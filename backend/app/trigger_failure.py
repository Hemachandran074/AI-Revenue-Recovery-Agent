"""Create a test-mode Razorpay Payment Link, to trigger a genuine provider webhook.

    python -m app.trigger_failure              # create a link, print how to fail it
    python -m app.trigger_failure --amount 999
    python -m app.trigger_failure --list       # links already in this mode
    python -m app.trigger_failure --status <payment_link_id>

WHY THIS EXISTS RATHER THAN USING THE DASHBOARD

Creating the link in the dashboard depends on the Test/Live toggle being set
correctly, and a link created in one mode cannot be paid against the other. That
mismatch surfaces as a confusing Razorpay error:

    "The id provided does not exist" / step: payment_initiation

Building the link through the API with the same keys the rest of this project
uses removes that class of error entirely: the link is guaranteed to live in the
account and mode those keys authenticate against.

SAFETY

  - Refuses to run unless the key is ``rzp_test_``. A live key would create a
    real, payable link.
  - ``notify`` is disabled for both SMS and email, so Razorpay does not message
    anyone. Open the printed URL yourself.
  - ``notes.customer_id`` is set so DETECT resolves a real customer id from the
    webhook instead of falling back to a contact hash.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import razorpay

from app.config import get_settings

# Test-mode ways to force a failure. The cancellation entries are confirmed by
# live capture, not just docs: cancelling a wallet or netbanking payment really
# does emit payment.failed with error_reason=payment_cancelled. Razorpay's docs
# note that cancellation registers as SUCCESS specifically for UPI, so use
# failure@razorpay there instead of cancelling.
FAILURE_RECIPES = (
    ("Netbanking", "Pick any bank. Test mode shows a mock page instead of the "
                   "real bank portal; choose Failure, or just cancel."),
    ("Wallet", "Pick any wallet, then cancel on the mock page. Confirmed to "
               "emit payment.failed / payment_cancelled."),
    ("UPI", "Enter  failure@razorpay  as the UPI ID. Do NOT cancel a UPI "
            "payment: for UPI specifically, cancelling registers as success."),
    ("Card", "Use a documented test card, then fail authentication on the mock "
             "OTP page."),
)


class NotTestModeError(RuntimeError):
    """Raised when the configured key is not a test-mode key."""


def _client() -> razorpay.Client:
    settings = get_settings()
    key_id = settings.razorpay_key_id
    secret = settings.razorpay_key_secret
    if not key_id or not secret:
        raise SystemExit(
            "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set in .env."
        )
    if not key_id.startswith("rzp_test_"):
        raise NotTestModeError(
            "Refusing to run: RAZORPAY_KEY_ID is not a test-mode key. This "
            "creates a payable Payment Link, and this project is test-mode only."
        )
    return razorpay.Client(auth=(key_id, secret))


def create_link(
    *,
    amount_paise: int,
    description: str,
    customer_id: str,
    email: str,
    contact: str,
) -> dict:
    """Create a test-mode Payment Link with notifications disabled."""
    return _client().payment_link.create(
        {
            "amount": amount_paise,
            "currency": "INR",
            "accept_partial": False,
            "description": description,
            "customer": {"name": "Recovery Demo", "email": email, "contact": contact},
            # Off on purpose: Razorpay must not message a real inbox or handset.
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "notes": {
                "customer_id": customer_id,
                "source": "revenue_recovery_live_test",
            },
        }
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.trigger_failure",
        description="Create a test-mode Payment Link to trigger a real webhook.",
    )
    parser.add_argument("--amount", type=float, default=499.0,
                        help="Amount in rupees (default 499).")
    parser.add_argument("--customer-id", default=None,
                        help="Written to notes.customer_id, which DETECT reads.")
    parser.add_argument("--email", default="recovery.demo@example.com")
    # Razorpay rejects contacts with repeating digit runs (e.g. +919999999999)
    # as obviously fake, so the placeholder has to look like a plausible number.
    # Notifications are disabled, so nothing is ever sent to it.
    parser.add_argument("--contact", default="+919812345670")
    parser.add_argument("--list", action="store_true",
                        help="List Payment Links in this mode and exit.")
    parser.add_argument("--status", metavar="PLINK_ID",
                        help="Show one Payment Link and its payments.")
    args = parser.parse_args(argv)

    try:
        client = _client()
    except NotTestModeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.list:
        links = client.payment_link.all()
        items = links.get("payment_links", links.get("items", []))
        print(f"{len(items)} payment link(s) in this (test) mode:")
        for link in items:
            print(f"  {link.get('id')}  {link.get('status'):<10} "
                  f"Rs {link.get('amount', 0) / 100:<10} {link.get('short_url')}")
        return 0

    if args.status:
        link = client.payment_link.fetch(args.status)
        print(json.dumps(
            {
                "id": link.get("id"),
                "status": link.get("status"),
                "amount_inr": link.get("amount", 0) / 100,
                "short_url": link.get("short_url"),
                "payments": link.get("payments"),
            },
            indent=2,
        ))
        return 0

    amount_paise = int(round(args.amount * 100))
    customer_id = args.customer_id or f"cust_live_{int(time.time()) % 1000000}"
    link_url = ""
    link_id = ""
    try:
        link = create_link(
            amount_paise=amount_paise,
            description="Revenue recovery agent - deliberate failure test",
            customer_id=customer_id,
            email=args.email,
            contact=args.contact,
        )
        link_id = str(link.get("id"))
        link_url = str(link.get("short_url"))
    except razorpay.errors.BadRequestError as exc:
        print(f"Razorpay rejected the request: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        if "test mode limit of 30 reached" in str(exc).lower():
            order = client.order.create({
                "amount": amount_paise,
                "currency": "INR",
                "receipt": f"rcpt_{customer_id[:16]}",
                "notes": {"customer_id": customer_id, "source": "revenue_recovery_live_test"},
            })
            link_id = str(order.get("id"))
            link_url = (
                f"http://127.0.0.1:8000/test-checkout?order_id={link_id}"
                f"&amount={amount_paise}&contact={args.contact}&email={args.email}"
            )
        else:
            raise

    print("Created test-mode Payment Session")
    print(f"  id          : {link_id}")
    print(f"  amount      : Rs {amount_paise / 100}")
    print(f"  customer_id : {customer_id}  (DETECT reads notes.customer_id)")
    print()
    print(f"  OPEN THIS -> {link_url}")
    print()
    print("Then make it FAIL:")
    for method, how in FAILURE_RECIPES:
        print(f"  {method:<12} {how}")
    print()
    print("Watch it arrive:")
    print("  ngrok inspector      http://127.0.0.1:4040")
    print("  detected events      http://127.0.0.1:8000/events/recent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
