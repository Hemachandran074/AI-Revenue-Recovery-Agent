"""Configuration loading and credential-readiness reporting.

Two jobs beyond plain env parsing:

1. **Enforce test-mode-only credentials in code.** ``code-standards.md`` says
   "Test-mode keys only. No production payment credentials in this project,
   ever." That is enforced here as a startup validation error, not left as a
   comment. A live Stripe/Razorpay key fails fast instead of quietly working.
2. **Report what is missing without leaking values.** Phase 0 has no real
   credentials yet, so the app must boot without them and state plainly which
   capabilities are offline.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"

# Values copied straight out of .env.example that must never be treated as real.
PLACEHOLDER_SECRETS = frozenset({"CHANGEME", "changeme", ""})


class PaymentProvider(StrEnum):
    STRIPE = "stripe"
    RAZORPAY = "razorpay"


class Settings(BaseSettings):
    """Every credential is optional so the app boots in an unconfigured repo.

    Absence is reported through :meth:`credential_report`, never faked. Nothing
    in this file invents a working default for a secret.
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "development"
    # Loopback default: this service has no authentication layer.
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    payment_provider: PaymentProvider | None = None

    stripe_secret_key: str | None = None
    stripe_publishable_key: str | None = None
    stripe_webhook_secret: str | None = None

    razorpay_key_id: str | None = None
    razorpay_key_secret: str | None = None
    razorpay_webhook_secret: str | None = None

    gemini_api_key: str | None = None
    # flash-LITE, not flash. Measured, not preferred on taste: this key's
    # gemini-2.5-flash allowance is 20 requests PER DAY, which cannot cover the
    # ~27 distinct classifications a demo batch needs. Quota is per-model, and
    # flash-lite has its own bucket plus lower latency (1.6s vs 2.1s). Newer
    # 3.x models were tried and returned prose or invented enum values.
    gemini_model: str = "gemini-2.5-flash-lite"
    # Below this, a classification is rerouted to `unknown` and escalated to a
    # human rather than acted on. A probe returned a forced guess at exactly 0.70,
    # so the floor sits above that. `unknown` itself is exempt: certainty that the
    # evidence is insufficient is still certainty.
    diagnose_confidence_threshold: float = Field(default=0.75, ge=0.0, le=1.0)

    # Whether the webhook runs all four stages before responding, or only DETECT.
    #
    # Inline is the default because it is what makes the detect-to-decision
    # latency claim in architecture.md true end to end through the
    # signature-verified path, rather than measured across a gap.
    #
    # The trade-off: DIAGNOSE adds ~2s, so the webhook answers in seconds rather
    # than milliseconds. Razorpay tolerates that, but a production deployment
    # under load would acknowledge first and process on a queue.
    pipeline_run_inline: bool = True

    # SendGrid is intentionally unused in v1 — WhatsApp is the only live channel.
    sendgrid_api_key: str | None = None
    sendgrid_from_email: str | None = None

    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from_number: str | None = None
    twilio_whatsapp_test_recipients: str | None = None

    database_url: str | None = None
    # These configure the docker-compose container, not the app connection.
    postgres_user: str = "recovery"
    postgres_password: str | None = None
    postgres_db: str = "revenue_recovery"
    postgres_port: int = 5432

    # Mirrors architecture.md -> Non-negotiable constraints #4.
    max_recovery_attempts: int = Field(default=3, ge=1)
    # Delay before an insufficient-funds retry. architecture.md asks for
    # payday-aware timing "if data available"; we hold no payday data, so this is
    # a flat interval rather than a guess dressed up as one.
    insufficient_funds_retry_days: int = Field(default=3, ge=1, le=30)
    min_hours_between_contacts: int = Field(default=24, ge=1)
    quiet_hours_start_local: int = Field(default=9, ge=0, le=23)
    quiet_hours_end_local: int = Field(default=20, ge=1, le=24)
    hard_stop_days: int = Field(default=7, ge=1)

    @field_validator("stripe_secret_key")
    @classmethod
    def _stripe_must_be_test_mode(cls, v: str | None) -> str | None:
        if v and not v.startswith("sk_test_"):
            raise ValueError(
                "STRIPE_SECRET_KEY must be a test-mode key (sk_test_...). "
                "Production payment credentials are prohibited in this project "
                "(context/code-standards.md -> Secrets & config)."
            )
        return v

    @field_validator("razorpay_key_id")
    @classmethod
    def _razorpay_must_be_test_mode(cls, v: str | None) -> str | None:
        if v and not v.startswith("rzp_test_"):
            raise ValueError(
                "RAZORPAY_KEY_ID must be a test-mode key (rzp_test_...). "
                "Production payment credentials are prohibited in this project "
                "(context/code-standards.md -> Secrets & config)."
            )
        return v

    @property
    def whatsapp_recipient_allowlist(self) -> list[str]:
        """Opted-in WhatsApp sandbox numbers. Empty means nothing can be delivered."""
        raw = self.twilio_whatsapp_test_recipients or ""
        return [n.strip() for n in raw.split(",") if n.strip()]

    @property
    def effective_database_url(self) -> str | None:
        """The URL the app actually connects with.

        ``POSTGRES_*`` is the single source of truth for the containerised
        database, so the connection URL is derived from it rather than being
        maintained by hand in two places. Setting ``DATABASE_URL`` explicitly
        overrides this, which is what you want when pointing at a Postgres that
        docker-compose does not manage.

        Returns ``None`` when neither is usable, so callers can report the gap
        instead of failing on a placeholder connection string.
        """
        if self.database_url and not self._database_url_is_placeholder:
            return self.database_url
        if not self.postgres_password:
            return None
        # Credentials are percent-encoded: a '@', ':' or '/' in the password
        # would otherwise silently corrupt the URL structure.
        return (
            f"postgresql+psycopg://{quote(self.postgres_user, safe='')}:"
            f"{quote(self.postgres_password, safe='')}"
            f"@127.0.0.1:{self.postgres_port}/{quote(self.postgres_db, safe='')}"
        )

    @property
    def _database_url_is_placeholder(self) -> bool:
        """True when DATABASE_URL is an unedited copy from .env.example."""
        if not self.database_url:
            return False
        try:
            password = urlsplit(self.database_url).password
        except ValueError:
            return False
        return (password or "") in PLACEHOLDER_SECRETS

    def database_url_matches_container(self) -> bool | None:
        """Whether ``DATABASE_URL`` agrees with the ``POSTGRES_*`` container config.

        A password/user/db mismatch between ``.env`` and ``docker-compose.yml`` is
        the most common way this setup fails, and it fails with an opaque auth
        error at first query. Catch it at readiness instead.

        Returns ``None`` when there is not enough configured to compare.
        """
        if (
            not self.database_url
            or self._database_url_is_placeholder
            or not self.postgres_password
        ):
            return None
        try:
            parsed = urlsplit(self.database_url)
        except ValueError:
            return False
        return (
            unquote(parsed.username or "") == self.postgres_user
            and unquote(parsed.password or "") == self.postgres_password
            and parsed.path.lstrip("/") == self.postgres_db
            and (parsed.port or 5432) == self.postgres_port
        )

    def _provider_credentials_present(self) -> bool:
        if self.payment_provider is PaymentProvider.STRIPE:
            return bool(self.stripe_secret_key and self.stripe_webhook_secret)
        if self.payment_provider is PaymentProvider.RAZORPAY:
            return bool(self.razorpay_key_id and self.razorpay_key_secret
                        and self.razorpay_webhook_secret)
        return False

    def credential_report(self) -> dict[str, object]:
        """Which capabilities are configured, and what is needed to enable each.

        Returns booleans and human-readable key names only. Never returns a
        secret value.
        """
        missing: list[str] = []

        if self.payment_provider is None:
            missing.append("PAYMENT_PROVIDER (choose 'stripe' or 'razorpay')")
        elif not self._provider_credentials_present():
            if self.payment_provider is PaymentProvider.STRIPE:
                missing.extend(
                    k for k, present in {
                        "STRIPE_SECRET_KEY": self.stripe_secret_key,
                        "STRIPE_WEBHOOK_SECRET": self.stripe_webhook_secret,
                    }.items() if not present
                )
            else:
                missing.extend(
                    k for k, present in {
                        "RAZORPAY_KEY_ID": self.razorpay_key_id,
                        "RAZORPAY_KEY_SECRET": self.razorpay_key_secret,
                        "RAZORPAY_WEBHOOK_SECRET": self.razorpay_webhook_secret,
                    }.items() if not present
                )

        if not self.gemini_api_key:
            missing.append("GEMINI_API_KEY")
        if not self.effective_database_url:
            missing.append("POSTGRES_PASSWORD (or an explicit DATABASE_URL)")

        twilio_ready = bool(self.twilio_account_sid and self.twilio_auth_token
                            and self.twilio_from_number)
        whatsapp_ready = twilio_ready and bool(
            self.twilio_from_number and self.twilio_from_number.startswith("whatsapp:")
        )

        warnings: list[str] = []
        if twilio_ready and not whatsapp_ready:
            warnings.append(
                "TWILIO_FROM_NUMBER is set but not WhatsApp-formatted; it must look "
                "like 'whatsapp:+14155238886' for the WhatsApp channel to work."
            )
        if whatsapp_ready and not self.whatsapp_recipient_allowlist:
            warnings.append(
                "TWILIO_WHATSAPP_TEST_RECIPIENTS is empty. The Twilio WhatsApp "
                "sandbox only delivers to numbers that have opted in, so a batch "
                "would report sends that never arrive."
            )
        if self._database_url_is_placeholder:
            warnings.append(
                "DATABASE_URL still holds the .env.example placeholder password and "
                "is being ignored. Set POSTGRES_PASSWORD and the connection URL is "
                "derived from it, or delete DATABASE_URL from .env entirely."
            )
        if self.database_url_matches_container() is False:
            warnings.append(
                "DATABASE_URL is set explicitly and does not match the POSTGRES_* "
                "container settings (user/password/db/port). The app will fail to "
                "authenticate. Remove DATABASE_URL to derive it from POSTGRES_*."
            )

        return {
            "payment_provider": self.payment_provider,
            "capabilities": {
                "payment_provider_configured": self._provider_credentials_present(),
                "diagnose_llm_configured": bool(self.gemini_api_key),
                "database_configured": bool(self.effective_database_url),
                "email_configured": bool(self.sendgrid_api_key
                                         and self.sendgrid_from_email),
                "sms_configured": twilio_ready,
                "whatsapp_configured": whatsapp_ready,
            },
            "missing_required_keys": missing,
            "warnings": warnings,
            "env_file_found": ENV_FILE.exists(),
        }

    def guardrail_config(self) -> dict[str, int]:
        """Effective stopping-rule values, surfaced for the audit trail."""
        return {
            "max_recovery_attempts": self.max_recovery_attempts,
            "min_hours_between_contacts": self.min_hours_between_contacts,
            "quiet_hours_start_local": self.quiet_hours_start_local,
            "quiet_hours_end_local": self.quiet_hours_end_local,
            "hard_stop_days": self.hard_stop_days,
        }


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor. Clear with ``get_settings.cache_clear()`` in tests."""
    return Settings()
