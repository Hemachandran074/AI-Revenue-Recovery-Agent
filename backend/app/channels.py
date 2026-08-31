"""Outbound adapters: Razorpay payment links and Twilio WhatsApp.

Kept apart from ``execute.py`` so the action logic can be tested without either
SDK, and so a provider swap touches one file.

## The compliance shape of a recovery link

Every recovery action hands the customer a **Razorpay-hosted** payment link. That
is what satisfies four Non-negotiable Constraints at once:

  #1 no card data reaches us — the customer types it on Razorpay's page
  #2 no 3DS/SCA bypass — the customer authenticates themselves, on that page
  #3 no silent retry — the customer initiates by tapping the link
  #6 we never hold or resume a payment session — a link is a fresh session

A link is created for the **outstanding** amount, not the gross. Re-charging the
full figure on a part-paid invoice would overcharge the customer.

## The allowlist is a safety mechanism, not a convenience

Synthetic fixtures carry well-formed Indian mobile numbers. They are invented, but
"invented" and "unused by any real person" are different claims. Sending to one
would message a stranger. So a send is refused unless the recipient appears in
``TWILIO_WHATSAPP_TEST_RECIPIENTS``. The Twilio sandbox also silently drops
messages to numbers that never opted in, which would inflate a delivery metric
with sends that never arrived.

## Dry run is explicit, never accidental

With credentials absent the adapters report ``dry_run`` and say so. They do not
pretend to have sent anything, because a demo that looked like it messaged
customers while doing nothing would be worse than one that admitted it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


def normalise_whatsapp_number(number: str | None) -> str | None:
    """Canonical ``whatsapp:+E164`` form, for comparing against the allowlist.

    Without this a number allowlisted as ``+919812345670`` would not match a
    recipient rendered as ``whatsapp:+919812345670``, and every send would be
    refused for the wrong reason.
    """
    if not number:
        return None
    trimmed = number.strip().replace(" ", "").replace("-", "")
    if not trimmed:
        return None
    if trimmed.startswith("whatsapp:"):
        trimmed = trimmed[len("whatsapp:") :]
    if not trimmed.startswith("+"):
        return None
    return f"whatsapp:{trimmed}"


@dataclass(frozen=True, slots=True)
class PaymentLink:
    """A provider-hosted link the customer completes payment on."""

    link_id: str
    url: str
    amount_minor: int
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class MessageResult:
    """Outcome of one outbound message attempt."""

    delivered: bool
    provider_message_id: str | None = None
    dry_run: bool = False
    error: str | None = None


class PaymentLinkFactory(Protocol):
    def create(
        self, *, amount_minor: int, customer_id: str, email: str | None,
        contact: str | None, description: str,
    ) -> PaymentLink: ...


class MessageSender(Protocol):
    def send(self, *, to: str, body: str) -> MessageResult: ...


class RazorpayPaymentLinkFactory:
    """Creates a real test-mode Razorpay payment link.

    Refuses to run on a non-test key. This project is test-mode only
    (``code-standards.md`` -> Secrets & config), and a live key here would create a
    genuinely payable link.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        key_id = self._settings.razorpay_key_id
        if not key_id or not self._settings.razorpay_key_secret:
            raise ValueError("Razorpay credentials are not configured")
        if not key_id.startswith("rzp_test_"):
            raise ValueError(
                "Refusing to create payment links with a non-test-mode key."
            )
        import razorpay

        self._client = razorpay.Client(
            auth=(key_id, self._settings.razorpay_key_secret)
        )

    def create(
        self, *, amount_minor: int, customer_id: str, email: str | None,
        contact: str | None, description: str,
    ) -> PaymentLink:
        payload: dict[str, object] = {
            "amount": amount_minor,
            "currency": "INR",
            "accept_partial": False,
            "description": description[:255],
            # Off deliberately: this agent controls the messaging, so letting
            # Razorpay also notify would double-contact the customer and could
            # breach the one-contact-per-24h rule from outside our guardrails.
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "notes": {"customer_id": customer_id, "source": "revenue_recovery"},
        }
        customer: dict[str, str] = {}
        if email:
            customer["email"] = email
        if contact:
            customer["contact"] = contact
        if customer:
            payload["customer"] = customer

        link = self._client.payment_link.create(payload)
        return PaymentLink(
            link_id=str(link.get("id")),
            url=str(link.get("short_url")),
            amount_minor=amount_minor,
        )


class DryRunPaymentLinkFactory:
    """Deterministic stand-in used when Razorpay is not configured."""

    def create(
        self, *, amount_minor: int, customer_id: str, email: str | None,
        contact: str | None, description: str,
    ) -> PaymentLink:
        digest = abs(hash((customer_id, amount_minor))) % 10**10
        return PaymentLink(
            link_id=f"plink_dryrun{digest:010d}",
            url=f"https://example.invalid/dry-run/{digest:010d}",
            amount_minor=amount_minor,
            dry_run=True,
        )


class TwilioWhatsAppSender:
    """Sends WhatsApp messages, but only to opted-in allowlisted numbers.

    The allowlist check happens here rather than at the call site so it cannot be
    bypassed by a new caller forgetting it.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        s = self._settings
        if not (s.twilio_account_sid and s.twilio_auth_token and s.twilio_from_number):
            raise ValueError("Twilio credentials are not configured")
        self._from = normalise_whatsapp_number(s.twilio_from_number)
        if not self._from:
            raise ValueError(
                "TWILIO_FROM_NUMBER must be a WhatsApp sender, e.g. "
                "whatsapp:+14155238886"
            )
        self._allowlist = {
            normalise_whatsapp_number(n) for n in s.whatsapp_recipient_allowlist
        }
        from twilio.rest import Client

        self._client = Client(s.twilio_account_sid, s.twilio_auth_token)

    def send(self, *, to: str, body: str) -> MessageResult:
        target = normalise_whatsapp_number(to)
        if target is None:
            return MessageResult(
                delivered=False, error=f"unusable recipient number: {to!r}"
            )
        if target not in self._allowlist:
            # Refused, not failed. A synthetic fixture number could belong to a
            # real person, and the sandbox would drop it silently anyway.
            return MessageResult(
                delivered=False,
                error=(
                    "recipient is not in TWILIO_WHATSAPP_TEST_RECIPIENTS; refusing "
                    "to message a number that has not opted in"
                ),
            )
        try:
            message = self._client.messages.create(
                from_=self._from, to=target, body=body
            )
        except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
            logger.warning("WhatsApp send failed for %s: %s", target, exc)
            return MessageResult(delivered=False, error=f"{type(exc).__name__}: {exc}")
        return MessageResult(delivered=True, provider_message_id=str(message.sid))


class DryRunSender:
    """Records what would have been sent, and is honest that nothing was."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, *, to: str, body: str) -> MessageResult:
        self.sent.append((to, body))
        logger.info("dry run: would send to %s (%d chars)", to, len(body))
        return MessageResult(delivered=False, dry_run=True)


def build_payment_link_factory(settings: Settings | None = None) -> PaymentLinkFactory:
    s = settings or get_settings()
    try:
        return RazorpayPaymentLinkFactory(s)
    except (ValueError, ImportError) as exc:
        logger.info("Razorpay links unavailable (%s); using dry run", exc)
        return DryRunPaymentLinkFactory()


def build_message_sender(settings: Settings | None = None) -> MessageSender:
    s = settings or get_settings()
    try:
        return TwilioWhatsAppSender(s)
    except (ValueError, ImportError) as exc:
        logger.info("Twilio unavailable (%s); using dry run", exc)
        return DryRunSender()
