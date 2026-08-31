"""Envelope builders for ``payment_link.expired`` and ``invoice.expired``.

Entity shapes are copied from ``fixtures/reference_real_entities.json``, which was
read back from the live Razorpay test API rather than transcribed from docs. That
includes details nobody would guess:

  - Payment links use ``0`` for unset timestamps (``expire_by``, ``expired_at``,
    ``cancelled_at``); invoices use ``null`` for the same idea.
  - Invoice ``notes`` is ``[]`` when empty but an object when populated, matching
    the payment entity's quirk.
  - Invoices carry a ``currency_symbol`` and a duplicated set of customer fields
    (``name`` and ``customer_name``, ``email`` and ``customer_email``, ...).
  - Amounts are paise on both.

A CONSTRAINT FOUND BY MEASUREMENT, NOT ASSUMPTION
``checkout_friction`` and ``genuine_abandonment`` cannot be told apart from a
``payment_link.expired`` payload. The ``payments`` array was empty on every real
link inspected, including one that had been paid, so the payload carries no record
of attempts. The signal has to come from our own event history: whether we
received ``payment.failed`` events for that customer while the link was live. The
7 genuine failures captured earlier all shared a single ``order_id``, which is what
makes that correlation possible.

Invoices are better off: ``partial_payment``, ``amount_paid`` and ``amount_due``
are on the entity, so a part-settled invoice is visible in the payload itself.

Consequence for Phase 3: DIAGNOSE cannot classify a link expiry from the webhook
alone. ``prior_failed_attempts`` is therefore carried in the fixture's
``pipeline_context`` — representing what DETECT would supply by enrichment — and
never inside the webhook envelope, because Razorpay does not put it there.

The envelope wrapper (``contains``, and the ``payload.<entity>.entity`` nesting)
follows the pattern documented for payment events. That specific detail is
INFERRED from convention rather than observed, since triggering a real expiry
means waiting for one.
"""

from __future__ import annotations

import random
from datetime import datetime

from app.simulation.abandonment_catalog import AbandonmentScenario, AbandonmentSignal

_BASE62_SOURCE = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _rzp_id(rng: random.Random, prefix: str) -> str:
    body = "".join(rng.choice(_BASE62_SOURCE) for _ in range(14))
    return f"{prefix}_{body}"


def build_payment_link_entity(
    rng: random.Random,
    *,
    scenario: AbandonmentScenario,
    customer_id: str,
    name: str,
    email: str,
    contact: str,
    amount_paise: int,
    created_at: datetime,
    expired_at: datetime,
) -> dict:
    """A Razorpay Payment Link entity in the ``expired`` state.

    ``payments`` is left ``None`` and ``amount_paid`` at 0 for unpaid links,
    because that is exactly what real links return. Deliberately not padded with
    an invented attempts list: doing so would let DIAGNOSE be built against
    information production never sends.
    """
    partial = scenario.signal is AbandonmentSignal.PARTIALLY_PAID
    amount_paid = int(amount_paise * scenario.partial_paid_fraction) if partial else 0
    # Round to whole rupees; Razorpay amounts for these price points never carry paise.
    amount_paid -= amount_paid % 100

    return {
        "accept_partial": partial,
        "allow_full_payment": False,
        "amount": amount_paise,
        "amount_paid": amount_paid,
        "cancelled_at": 0,
        "created_at": int(created_at.timestamp()),
        "currency": "INR",
        "customer": {"contact": contact, "email": email, "name": name},
        "description": "Subscription renewal",
        "expire_by": int(expired_at.timestamp()),
        "expired_at": int(expired_at.timestamp()),
        "first_min_partial_amount": amount_paid if partial else 0,
        "id": _rzp_id(rng, "plink"),
        "notes": {"customer_id": customer_id, "source": "revenue_recovery_demo"},
        "notify": {"email": True, "sms": True, "whatsapp": False},
        "payment_plan": False,
        # Empty on every real link inspected, including a paid one.
        "payments": None,
        "reference_id": "",
        "reminder_enable": True,
        "reminders": [],
        "short_url": f"https://rzp.io/rzp/{''.join(rng.choice(_BASE62_SOURCE) for _ in range(7))}",
        "status": scenario.entity_status,
        "updated_at": int(expired_at.timestamp()),
        "upi_link": False,
        "user_id": "",
        "whatsapp_link": False,
    }


def build_invoice_entity(
    rng: random.Random,
    *,
    scenario: AbandonmentScenario,
    customer_id: str,
    name: str,
    email: str,
    contact: str,
    amount_paise: int,
    created_at: datetime,
    expired_at: datetime,
    order_id: str,
) -> dict:
    """A Razorpay Invoice entity in the ``expired`` state.

    Unlike payment links, an invoice exposes ``partial_payment``, ``amount_paid``
    and ``amount_due``, so a part-settled invoice is diagnosable from the payload.
    """
    partial = scenario.signal is AbandonmentSignal.PARTIALLY_PAID
    amount_paid = int(amount_paise * scenario.partial_paid_fraction) if partial else 0
    amount_paid -= amount_paid % 100
    amount_due = amount_paise - amount_paid

    created_unix = int(created_at.timestamp())
    expired_unix = int(expired_at.timestamp())
    line_item_id = _rzp_id(rng, "li")

    return {
        "id": _rzp_id(rng, "inv"),
        "entity": "invoice",
        "receipt": None,
        "invoice_number": None,
        "customer_id": customer_id,
        "customer_details": {
            "id": customer_id,
            "name": name,
            "email": email,
            "contact": contact,
            "gstin": None,
            "billing_address": None,
            "shipping_address": None,
            # Razorpay really does duplicate these three under a second naming.
            "customer_name": name,
            "customer_email": email,
            "customer_contact": contact,
        },
        "order_id": order_id,
        "line_items": [
            {
                "id": line_item_id,
                "item_id": None,
                "ref_id": None,
                "ref_type": None,
                "name": "Subscription renewal",
                "description": None,
                "amount": amount_paise,
                "unit_amount": amount_paise,
                "gross_amount": amount_paise,
                "tax_amount": 0,
                "taxable_amount": amount_paise,
                "net_amount": amount_paise,
                "currency": "INR",
                "type": "invoice",
                "tax_inclusive": False,
                "hsn_code": None,
                "sac_code": None,
                "tax_rate": None,
                "unit": None,
                "quantity": 1,
                "taxes": [],
            }
        ],
        "payment_id": None,
        "status": scenario.entity_status,
        "expire_by": expired_unix,
        "issued_at": created_unix,
        "paid_at": None,
        "cancelled_at": None,
        "expired_at": expired_unix,
        "sms_status": "sent",
        "email_status": "sent",
        "date": created_unix,
        "terms": None,
        "partial_payment": partial,
        "gross_amount": amount_paise,
        "tax_amount": 0,
        "taxable_amount": amount_paise,
        "amount": amount_paise,
        "amount_paid": amount_paid,
        "amount_due": amount_due,
        "currency": "INR",
        "currency_symbol": "\u20b9",
        "description": "Subscription renewal",
        "notes": {"customer_id": customer_id, "source": "revenue_recovery_demo"},
        "comment": None,
        "short_url": f"https://rzp.io/rzp/{''.join(rng.choice(_BASE62_SOURCE) for _ in range(7))}",
        "view_less": True,
        "billing_start": None,
        "billing_end": None,
        "type": "invoice",
        "group_taxes_discounts": False,
        "created_at": created_unix,
        "idempotency_key": None,
        "ref_num": None,
    }


def build_envelope(
    *,
    razorpay_event: str,
    entity_key: str,
    entity: dict,
    account_id: str,
    created_at: datetime,
) -> dict:
    """Wrap an entity in Razorpay's webhook envelope.

    ``contains`` and the ``payload.<entity_key>.entity`` nesting follow the
    pattern documented for payment events. INFERRED from convention, not observed
    on a real expiry delivery — see this module's docstring.
    """
    return {
        "entity": "event",
        "account_id": account_id,
        "event": razorpay_event,
        "contains": [entity_key],
        "payload": {entity_key: {"entity": entity}},
        "created_at": int(created_at.timestamp()),
    }
