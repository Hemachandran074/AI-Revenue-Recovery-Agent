"""Catalogue of real Razorpay payment-failure scenarios.

Every ``error_reason`` literal here is taken from Razorpay's published error
vocabulary. The `payment.failed` payload shape these feed is likewise taken from
Razorpay's own webhook samples, so DETECT can be built against something that
matches production field-for-field.

PROVENANCE MATTERS HERE. Razorpay publishes the full `error_reason` vocabulary
and a handful of complete sample payloads, but it does NOT publish which
``error_code`` / ``error_source`` / ``error_step`` co-occur with most reasons.
So each scenario records how trustworthy its field combination is:

  DOCUMENTED  the whole tuple appears verbatim in a Razorpay sample payload
  INFERRED    the ``error_reason`` is real and documented, but the accompanying
              code/source/step are our best reading of the docs' prose

Never present an INFERRED tuple as evidence of how Razorpay behaves. It is
realistic input for our pipeline, not a claim about the provider.

``expected_root_cause`` is GROUND TRUTH FOR EVALUATION ONLY. It is what a
correct DIAGNOSE should conclude. It must never be fed into the pipeline, or
DIAGNOSE would be scoring itself against its own input. It exists so Phase 3 can
measure classification accuracy per taxonomy category.

Sources:
  https://razorpay.com/docs/errors/payments/list
  https://razorpay.com/docs/errors/payments/cards/
  https://razorpay.com/docs/errors/payments/upi/
  https://razorpay.com/docs/errors/payments/payment-methods-error-parameters/
  https://razorpay.com/docs/webhooks/payments/
Content was rephrased for compliance with licensing restrictions; field names,
enum values and error strings are reproduced as identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.schemas import PaymentMethod, RootCause


class Provenance(StrEnum):
    """How much of a scenario's field combination is documented fact."""

    DOCUMENTED = "documented"
    INFERRED = "inferred"


@dataclass(frozen=True, slots=True)
class DeclineScenario:
    """One realistic way a Razorpay payment fails.

    ``weight`` is a relative frequency used when sampling a batch. See
    ``WEIGHTS_ARE_AN_ASSUMPTION`` below.
    """

    key: str
    method: PaymentMethod
    error_code: str | None
    error_description: str | None
    error_source: str | None
    error_step: str | None
    error_reason: str | None
    provenance: Provenance
    expected_root_cause: RootCause
    weight: float
    rationale: str


# Razorpay publishes no decline-mix statistics, so these weights are NOT sourced
# from the provider. They are a deliberately shaped, plausible distribution:
# balance and authentication failures dominate, infrastructure errors are
# middling, risk blocks are rare, and a meaningful slice is uninformative.
# That last point is the realistic part most synthetic data gets wrong — real
# webhooks frequently say nothing more useful than "Payment failed".
# Tune these to change batch composition; do not mistake them for measurements.
WEIGHTS_ARE_AN_ASSUMPTION = True


SCENARIOS: tuple[DeclineScenario, ...] = (
    # ---------------------------------------------------------------- DOCUMENTED
    # Complete tuples lifted from Razorpay's own webhook / API samples.
    DeclineScenario(
        key="wallet_insufficient_funds_documented",
        method=PaymentMethod.WALLET,
        error_code="BAD_REQUEST_ERROR",
        error_description="Payment failed due to insufficient funds",
        error_source="customer",
        error_step="payment_authentication",
        error_reason="insufficient_funds",
        provenance=Provenance.DOCUMENTED,
        expected_root_cause=RootCause.INSUFFICIENT_FUNDS,
        weight=4.0,
        rationale="Explicit insufficient_funds reason. Retry when funds likely present.",
    ),
    DeclineScenario(
        key="card_customer_cancelled_at_auth",
        method=PaymentMethod.CARD,
        error_code="BAD_REQUEST_ERROR",
        error_description=(
            "Your payment has been cancelled. Try again or complete the payment later."
        ),
        error_source="customer",
        error_step="payment_authentication",
        error_reason="payment_cancelled",
        provenance=Provenance.DOCUMENTED,
        expected_root_cause=RootCause.SCA_ABANDONED,
        weight=6.0,
        rationale=(
            "Customer walked away mid-authentication. Needs a fresh auth link they "
            "complete themselves; we must never complete 3DS on their behalf."
        ),
    ),
    # These two were added after OBSERVING them arrive from Razorpay test mode
    # through the live webhook, not from reading the docs. The identical error
    # tuple occurs across payment methods, which the catalogue originally missed
    # by tying customer cancellation to cards only. See
    # fixtures/reference_real_payment_failed.json and test_generator_fidelity.py.
    DeclineScenario(
        key="wallet_customer_cancelled_at_auth",
        method=PaymentMethod.WALLET,
        error_code="BAD_REQUEST_ERROR",
        error_description=(
            "Your payment has been cancelled. Try again or complete the payment later."
        ),
        error_source="customer",
        error_step="payment_authentication",
        error_reason="payment_cancelled",
        provenance=Provenance.DOCUMENTED,
        expected_root_cause=RootCause.SCA_ABANDONED,
        weight=3.0,
        rationale=(
            "Observed live: a wallet payment cancelled at the authentication step. "
            "Customer was present and walked away, so a fresh link they complete "
            "themselves is the correct action."
        ),
    ),
    DeclineScenario(
        key="netbanking_customer_cancelled_at_auth",
        method=PaymentMethod.NETBANKING,
        error_code="BAD_REQUEST_ERROR",
        error_description=(
            "Your payment has been cancelled. Try again or complete the payment later."
        ),
        error_source="customer",
        error_step="payment_authentication",
        error_reason="payment_cancelled",
        provenance=Provenance.DOCUMENTED,
        expected_root_cause=RootCause.SCA_ABANDONED,
        weight=2.5,
        rationale=(
            "Observed live: a netbanking payment cancelled at the bank page. Same "
            "handling as the wallet and card cases."
        ),
    ),
    DeclineScenario(
        key="card_invalid_otp_documented",
        method=PaymentMethod.CARD,
        error_code="BAD_REQUEST_ERROR",
        error_description="Authentication failed due to incorrect otp",
        error_source="customer",
        error_step="payment_authentication",
        error_reason="invalid_otp",
        provenance=Provenance.DOCUMENTED,
        expected_root_cause=RootCause.SCA_ABANDONED,
        weight=3.0,
        rationale="Authentication step failed. Fresh auth link, customer-completed.",
    ),
    DeclineScenario(
        key="card_incorrect_otp_documented",
        method=PaymentMethod.CARD,
        error_code="BAD_REQUEST_ERROR",
        error_description="Payment processing failed because of incorrect OTP",
        error_source="customer",
        error_step="payment_authentication",
        error_reason="incorrect_otp",
        provenance=Provenance.DOCUMENTED,
        expected_root_cause=RootCause.SCA_ABANDONED,
        weight=2.5,
        rationale=(
            "Distinct literal from invalid_otp; Razorpay uses both. Same handling."
        ),
    ),
    DeclineScenario(
        key="netbanking_opaque_bank_decline",
        method=PaymentMethod.NETBANKING,
        error_code="BAD_REQUEST_ERROR",
        error_description="Payment failed",
        error_source="bank",
        error_step="payment_authorization",
        error_reason="payment_failed",
        provenance=Provenance.DOCUMENTED,
        expected_root_cause=RootCause.UNKNOWN,
        weight=4.0,
        rationale=(
            "Genuinely uninformative: reason 'payment_failed' says only that it "
            "failed. Forcing this into a specific cause would be a guess, so it "
            "must escalate. Common in production and the main reason a real "
            "recovery agent needs an escalation path."
        ),
    ),
    DeclineScenario(
        key="wallet_opaque_issuer_decline",
        method=PaymentMethod.WALLET,
        error_code="BAD_REQUEST_ERROR",
        error_description="Payment failed",
        error_source="issuer",
        error_step="payment_authorization",
        error_reason="payment_failed",
        provenance=Provenance.DOCUMENTED,
        expected_root_cause=RootCause.UNKNOWN,
        weight=2.0,
        rationale="Opaque issuer decline. No basis for a specific cause.",
    ),
    DeclineScenario(
        key="upi_opaque_issuer_decline",
        method=PaymentMethod.UPI,
        error_code="BAD_REQUEST_ERROR",
        error_description="Payment failed",
        error_source="issuer",
        error_step="payment_authorization",
        error_reason="payment_failed",
        provenance=Provenance.DOCUMENTED,
        expected_root_cause=RootCause.UNKNOWN,
        weight=3.5,
        rationale="Opaque issuer decline on UPI. Escalate rather than guess.",
    ),
    DeclineScenario(
        key="card_blank_error_fields",
        method=PaymentMethod.CARD,
        error_code="",
        error_description="",
        error_source=None,
        error_step=None,
        error_reason=None,
        provenance=Provenance.DOCUMENTED,
        expected_root_cause=RootCause.UNKNOWN,
        weight=2.0,
        rationale=(
            "Razorpay's own card payment.failed sample carries empty/null error "
            "fields. Included deliberately: a generator that always supplies a "
            "tidy reason would let DIAGNOSE and DETECT pass without ever handling "
            "the real case where there is nothing to classify."
        ),
    ),
    # ------------------------------------------------------------------ INFERRED
    # error_reason is documented; code/source/step are our reading of the docs.
    DeclineScenario(
        key="card_insufficient_funds",
        method=PaymentMethod.CARD,
        error_code="BAD_REQUEST_ERROR",
        error_description="Your payment failed as the account had insufficient balance.",
        error_source="issuer_bank",
        error_step="payment_authorization",
        error_reason="insufficient_funds",
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.INSUFFICIENT_FUNDS,
        weight=7.0,
        rationale="Payday-aware retry is the highest-value action for this cause.",
    ),
    DeclineScenario(
        key="upi_insufficient_funds",
        method=PaymentMethod.UPI,
        error_code="BAD_REQUEST_ERROR",
        error_description="Your payment failed as the account had insufficient balance.",
        error_source="issuer_bank",
        error_step="payment_debit_request",
        error_reason="insufficient_funds",
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.INSUFFICIENT_FUNDS,
        weight=8.0,
        rationale="Balance failures are the single most common UPI decline class.",
    ),
    DeclineScenario(
        key="card_expired",
        method=PaymentMethod.CARD,
        error_code="BAD_REQUEST_ERROR",
        error_description="Your payment failed as the card has expired.",
        error_source="customer",
        error_step="payment_authorization",
        error_reason="card_expired",
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.CARD_EXPIRED,
        weight=8.0,
        rationale=(
            "Classic subscription-renewal failure. Retrying is pointless; the "
            "customer must update the payment method themselves. Weighted high "
            "because this batch is subscription-flavoured, where expired cards "
            "are among the leading causes, not because it demos well."
        ),
    ),
    DeclineScenario(
        key="card_declined_by_issuer",
        method=PaymentMethod.CARD,
        error_code="BAD_REQUEST_ERROR",
        error_description="Your card was declined by the issuing bank.",
        error_source="issuer_bank",
        error_step="payment_authorization",
        error_reason="card_declined",
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.BANK_RISK_BLOCK,
        weight=3.0,
        rationale=(
            "Issuer refused without stating why. Treated as a risk block because "
            "re-presenting the same card is the one thing we must not automate."
        ),
    ),
    DeclineScenario(
        key="card_risk_check_failed",
        method=PaymentMethod.CARD,
        error_code="BAD_REQUEST_ERROR",
        error_description="Payment failed because the risk check was not cleared.",
        error_source="gateway",
        error_step="payment_authorization",
        error_reason="payment_risk_check_failed",
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.BANK_RISK_BLOCK,
        weight=1.5,
        rationale="Explicit risk decline. Human review only, never an auto-retry.",
    ),
    DeclineScenario(
        key="card_blocked_for_online",
        method=PaymentMethod.CARD,
        error_code="BAD_REQUEST_ERROR",
        error_description="This card is not enabled for online transactions.",
        error_source="issuer_bank",
        error_step="payment_authorization",
        error_reason="card_disabled_for_online_payments",
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.BANK_RISK_BLOCK,
        weight=1.5,
        rationale="Issuer-side block. Customer must act with their bank.",
    ),
    DeclineScenario(
        key="upi_vpa_restricted",
        method=PaymentMethod.UPI,
        error_code="BAD_REQUEST_ERROR",
        error_description="Transactions on this VPA are restricted.",
        error_source="customer_psp",
        error_step="payment_initiation",
        error_reason="transaction_on_vpa_restricted",
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.BANK_RISK_BLOCK,
        weight=1.0,
        rationale="PSP-side restriction. Retrying the same VPA will keep failing.",
    ),
    DeclineScenario(
        key="card_international_not_allowed",
        method=PaymentMethod.CARD,
        error_code="BAD_REQUEST_ERROR",
        error_description="International transactions are not allowed on this card.",
        error_source="issuer_bank",
        error_step="payment_authorization",
        error_reason="international_transaction_not_allowed",
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.BANK_RISK_BLOCK,
        weight=1.0,
        rationale="Issuer restriction; needs customer action with their bank.",
    ),
    DeclineScenario(
        key="card_auth_failed",
        method=PaymentMethod.CARD,
        error_code="BAD_REQUEST_ERROR",
        error_description="Payment failed because authentication could not be completed.",
        error_source="customer",
        error_step="payment_authentication",
        error_reason="authentication_failed",
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.SCA_ABANDONED,
        weight=4.0,
        rationale="3DS/SCA not completed. Fresh customer-completed auth link.",
    ),
    DeclineScenario(
        key="card_otp_expired",
        method=PaymentMethod.CARD,
        error_code="BAD_REQUEST_ERROR",
        error_description="Payment failed because the OTP expired.",
        error_source="customer",
        error_step="payment_authentication",
        error_reason="otp_expired",
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.SCA_ABANDONED,
        weight=2.5,
        rationale="Customer was present but did not finish in time.",
    ),
    DeclineScenario(
        key="card_payment_timed_out",
        method=PaymentMethod.CARD,
        error_code="BAD_REQUEST_ERROR",
        error_description="Payment failed as it was not completed in time.",
        error_source="customer",
        error_step="payment_authentication",
        error_reason="payment_timed_out",
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.SCA_ABANDONED,
        weight=3.0,
        rationale=(
            "Razorpay documents no dedicated 3DS-timeout reason; this is the "
            "closest real literal for an authentication window elapsing."
        ),
    ),
    DeclineScenario(
        key="upi_collect_request_expired",
        method=PaymentMethod.UPI,
        error_code="BAD_REQUEST_ERROR",
        error_description="The UPI collect request expired before approval.",
        error_source="customer",
        error_step="payment_authentication",
        error_reason="payment_collect_request_expired",
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.SCA_ABANDONED,
        weight=4.5,
        rationale=(
            "Customer never approved the mandate in their UPI app. The recovery "
            "action is a fresh request they approve themselves."
        ),
    ),
    DeclineScenario(
        key="card_gateway_technical_error",
        method=PaymentMethod.CARD,
        error_code="BAD_REQUEST_ERROR",
        error_description="Payment failed due to a technical error at the gateway.",
        error_source="gateway",
        error_step="payment_authorization",
        error_reason="gateway_technical_error",
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.NETWORK_ERROR,
        weight=3.0,
        rationale="Transient infrastructure fault. One quiet retry is legitimate.",
    ),
    DeclineScenario(
        key="upi_bank_technical_error",
        method=PaymentMethod.UPI,
        error_code="BAD_REQUEST_ERROR",
        error_description="Payment failed due to a technical error at the bank.",
        error_source="issuer_bank",
        error_step="payment_debit_request",
        error_reason="bank_technical_error",
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.NETWORK_ERROR,
        weight=3.5,
        rationale="Bank-side downtime. Transient, so a single retry is appropriate.",
    ),
    DeclineScenario(
        key="upi_app_technical_error",
        method=PaymentMethod.UPI,
        error_code="BAD_REQUEST_ERROR",
        error_description="The UPI app reported a technical error.",
        error_source="customer_psp",
        error_step="payment_initiation",
        error_reason="upi_app_technical_error",
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.NETWORK_ERROR,
        weight=2.0,
        rationale="Customer PSP app fault. Transient.",
    ),
    DeclineScenario(
        key="netbanking_bank_technical_error",
        method=PaymentMethod.NETBANKING,
        error_code="BAD_REQUEST_ERROR",
        error_description="Payment failed due to a technical error at the bank.",
        error_source="issuer_bank",
        error_step="payment_authorization",
        error_reason="bank_technical_error",
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.NETWORK_ERROR,
        weight=1.5,
        rationale="Netbanking downtime. Transient.",
    ),
    DeclineScenario(
        key="card_server_error",
        method=PaymentMethod.CARD,
        error_code="SERVER_ERROR",
        error_description="Payment failed due to a server error.",
        error_source="internal",
        error_step="payment_authorization",
        error_reason="server_error",
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.NETWORK_ERROR,
        weight=1.0,
        rationale=(
            "SERVER_ERROR is one of only two non-empty error_code literals seen "
            "in Razorpay's payments docs. Transient by nature."
        ),
    ),
    DeclineScenario(
        key="card_incorrect_cvv",
        method=PaymentMethod.CARD,
        error_code="BAD_REQUEST_ERROR",
        error_description="Payment failed because the CVV was incorrect.",
        error_source="customer",
        error_step="payment_authorization",
        error_reason="incorrect_cvv",
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.UNKNOWN,
        weight=1.5,
        rationale=(
            "A data-entry error, which the fixed taxonomy has no category for. "
            "Mapped to unknown on purpose: inventing a category, or bending this "
            "into card_expired, is exactly the drift the taxonomy exists to stop."
        ),
    ),
    DeclineScenario(
        key="upi_invalid_vpa",
        method=PaymentMethod.UPI,
        error_code="BAD_REQUEST_ERROR",
        error_description="The VPA entered is not valid.",
        error_source="customer",
        error_step="payment_initiation",
        error_reason="invalid_vpa",
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.UNKNOWN,
        weight=1.5,
        rationale="Bad customer input; no taxonomy category fits. Escalate.",
    ),
    DeclineScenario(
        key="upi_frequency_limit_exceeded",
        method=PaymentMethod.UPI,
        error_code="BAD_REQUEST_ERROR",
        error_description="The permitted number of transactions per day was exceeded.",
        error_source="network",
        error_step="payment_debit_request",
        error_reason="transaction_frequency_limit_exceeded",
        provenance=Provenance.INFERRED,
        expected_root_cause=RootCause.BANK_RISK_BLOCK,
        weight=1.0,
        rationale=(
            "A network-imposed cap on transaction frequency. Originally labelled "
            "`unknown` on the reasoning that an NPCI limit is neither a funds nor "
            "a risk problem. Corrected after a measured run: bank_risk_block "
            "covers refusal on risk, restriction OR eligibility grounds, and a "
            "frequency cap is a restriction imposed by the network. Both labels "
            "route to escalate_to_human_review, so the action is unchanged either "
            "way — this is a labelling correction, not a behaviour change."
        ),
    ),
)


def scenarios_for_root_cause(root_cause: RootCause) -> tuple[DeclineScenario, ...]:
    """All scenarios whose correct diagnosis is ``root_cause``."""
    return tuple(s for s in SCENARIOS if s.expected_root_cause is root_cause)


def covered_root_causes() -> frozenset[RootCause]:
    """Root causes this catalogue can produce.

    ``checkout_friction`` and ``genuine_abandonment`` are absent by design: they
    describe abandoned checkouts, not failed payments, so no Razorpay
    ``payment.failed`` payload can legitimately represent them. They arrive with
    the ``checkout_abandoned`` generator (Phase 1b).
    """
    return frozenset(s.expected_root_cause for s in SCENARIOS)


def scenarios_by_key() -> dict[str, DeclineScenario]:
    return {s.key: s for s in SCENARIOS}
