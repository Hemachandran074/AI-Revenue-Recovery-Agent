"""Phase 0 scaffold tests: the app boots, config is safe, stubs fail loudly."""

from __future__ import annotations

import inspect
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app import config as config_module
from app.config import Settings
from app.main import app
from app.schemas import CustomerHistory, Diagnosis, EventRecord, RootCause

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def client(monkeypatch) -> TestClient:
    """Client whose settings ignore any local ``.env``, for determinism."""
    monkeypatch.setattr(config_module, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr("app.main.get_settings", lambda: Settings(_env_file=None))
    return TestClient(app)


def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readiness_reports_missing_keys_without_leaking_values(client: TestClient) -> None:
    """An unconfigured repo must boot and say what it needs, naming keys only."""
    body = client.get("/readiness").json()

    assert body["capabilities"]["payment_provider_configured"] is False
    assert body["capabilities"]["diagnose_llm_configured"] is False
    assert "GEMINI_API_KEY" in body["missing_required_keys"]
    assert any("PAYMENT_PROVIDER" in k for k in body["missing_required_keys"])

    # Guardrail thresholds match architecture.md constraint #4.
    assert body["guardrail_config"] == {
        "max_recovery_attempts": 3,
        "min_hours_between_contacts": 24,
        "quiet_hours_start_local": 9,
        "quiet_hours_end_local": 20,
        "hard_stop_days": 7,
    }


def test_live_payment_keys_are_rejected() -> None:
    """Production credentials must fail at startup, not be quietly accepted."""
    with pytest.raises(ValidationError, match="test-mode"):
        Settings(_env_file=None, stripe_secret_key="sk_live_not_allowed")
    with pytest.raises(ValidationError, match="test-mode"):
        Settings(_env_file=None, razorpay_key_id="rzp_live_not_allowed")


def test_test_mode_keys_are_accepted() -> None:
    settings = Settings(
        _env_file=None,
        stripe_secret_key="sk_test_placeholder",
        razorpay_key_id="rzp_test_placeholder",
    )
    assert settings.stripe_secret_key.startswith("sk_test_")
    assert settings.razorpay_key_id.startswith("rzp_test_")


def test_database_url_container_mismatch_is_detected() -> None:
    """A password mismatch between .env and docker-compose fails opaquely at
    first query, so readiness must catch it up front."""
    matching = Settings(
        _env_file=None,
        postgres_user="recovery",
        postgres_password="s3cret",
        postgres_db="revenue_recovery",
        postgres_port=5432,
        database_url="postgresql+psycopg://recovery:s3cret@localhost:5432/revenue_recovery",
    )
    assert matching.database_url_matches_container() is True

    mismatched = matching.model_copy(
        update={
            "database_url": "postgresql+psycopg://recovery:WRONG@localhost:5432/revenue_recovery"
        }
    )
    assert mismatched.database_url_matches_container() is False
    assert any("does not match the POSTGRES_*" in w
               for w in mismatched.credential_report()["warnings"])


def test_database_url_check_is_inconclusive_without_container_password() -> None:
    """No POSTGRES_PASSWORD means nothing to compare — must not false-alarm."""
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://recovery:x@localhost:5432/revenue_recovery",
    )
    assert settings.database_url_matches_container() is None


def test_database_url_is_derived_from_postgres_settings() -> None:
    """POSTGRES_* is the single source of truth; no hand-maintained duplicate."""
    settings = Settings(
        _env_file=None,
        postgres_user="recovery",
        postgres_password="s3cret",
        postgres_db="revenue_recovery",
        postgres_port=5432,
    )
    assert settings.database_url is None
    assert settings.effective_database_url == (
        "postgresql+psycopg://recovery:s3cret@127.0.0.1:5432/revenue_recovery"
    )
    assert settings.credential_report()["capabilities"]["database_configured"] is True


def test_derived_url_percent_encodes_special_characters() -> None:
    """An '@' or '/' in the password would otherwise corrupt the URL structure."""
    settings = Settings(
        _env_file=None,
        postgres_user="recovery",
        postgres_password="p@ss/w:rd",
        postgres_port=5432,
    )
    url = settings.effective_database_url
    assert url is not None
    assert "p%40ss%2Fw%3Ard" in url
    # Assert against SQLAlchemy, which is what actually parses this at runtime:
    # it must recover the original password verbatim.
    assert make_url(url).password == "p@ss/w:rd"
    assert make_url(url).host == "127.0.0.1"


def test_explicit_database_url_overrides_derivation() -> None:
    """Needed for pointing at a Postgres docker-compose does not manage."""
    settings = Settings(
        _env_file=None,
        postgres_password="s3cret",
        database_url="postgresql+psycopg://other:pw@db.example.internal:5432/other",
    )
    assert settings.effective_database_url == (
        "postgresql+psycopg://other:pw@db.example.internal:5432/other"
    )


def test_placeholder_database_url_is_ignored_and_warned() -> None:
    """An unedited .env.example URL must not be treated as a real connection."""
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://recovery:CHANGEME@localhost:5432/revenue_recovery",
    )
    assert settings.effective_database_url is None
    report = settings.credential_report()
    assert report["capabilities"]["database_configured"] is False
    assert any("placeholder" in w for w in report["warnings"])
    assert any("POSTGRES_PASSWORD" in k for k in report["missing_required_keys"])


def test_placeholder_url_falls_back_to_derived_when_password_set() -> None:
    """Setting POSTGRES_PASSWORD is enough; the stale placeholder is bypassed.

    Every field is pinned explicitly because `_env_file=None` only disables the
    .env file, not OS environment variables. Leaving the port unpinned made this
    fail whenever POSTGRES_PORT was exported in the shell.
    """
    settings = Settings(
        _env_file=None,
        postgres_user="recovery",
        postgres_password="s3cret",
        postgres_db="revenue_recovery",
        postgres_port=5432,
        database_url="postgresql+psycopg://recovery:CHANGEME@localhost:5432/revenue_recovery",
    )
    assert settings.effective_database_url == (
        "postgresql+psycopg://recovery:s3cret@127.0.0.1:5432/revenue_recovery"
    )


def test_plain_phone_number_does_not_count_as_whatsapp_ready() -> None:
    """Twilio needs a 'whatsapp:' prefixed sender for the WhatsApp channel."""
    plain = Settings(
        _env_file=None,
        twilio_account_sid="AC_test",
        twilio_auth_token="tok",
        twilio_from_number="+14155238886",
    )
    caps = plain.credential_report()["capabilities"]
    assert caps["sms_configured"] is True
    assert caps["whatsapp_configured"] is False

    prefixed = plain.model_copy(update={"twilio_from_number": "whatsapp:+14155238886"})
    assert prefixed.credential_report()["capabilities"]["whatsapp_configured"] is True


def test_whatsapp_without_optin_allowlist_warns() -> None:
    """Sandbox sends to non-opted-in numbers silently never arrive, which would
    inflate the recovery metric with sends that did not happen."""
    settings = Settings(
        _env_file=None,
        twilio_account_sid="AC_test",
        twilio_auth_token="tok",
        twilio_from_number="whatsapp:+14155238886",
    )
    assert any("opted in" in w for w in settings.credential_report()["warnings"])

    with_allowlist = settings.model_copy(
        update={"twilio_whatsapp_test_recipients": "whatsapp:+919999999999, +918888888888"}
    )
    assert len(with_allowlist.whatsapp_recipient_allowlist) == 2
    assert not any("opted in" in w
                   for w in with_allowlist.credential_report()["warnings"])


def test_env_example_documents_every_setting() -> None:
    """Every Settings field must be documented in .env.example."""
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    # `#?` allows optional keys to be documented commented-out (e.g. DATABASE_URL,
    # which is derived from POSTGRES_* unless deliberately overridden).
    documented = set(re.findall(r"^#?\s*([A-Z0-9_]+)=", env_example, flags=re.MULTILINE))
    expected = {name.upper() for name in Settings.model_fields}
    assert expected - documented == set()


def test_env_is_gitignored() -> None:
    """Guard against ever committing real credentials."""
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in [line.strip() for line in gitignore]


def test_no_real_env_file_committed() -> None:
    """A tracked .env would mean secrets in git history."""
    assert not (REPO_ROOT / ".env").exists() or ".env" in (
        REPO_ROOT / ".gitignore"
    ).read_text(encoding="utf-8")


def test_event_record_rejects_unknown_fields() -> None:
    """extra='forbid' is what stops a raw card object riding along in an event."""
    valid = {
        "event_id": "evt_1",
        "customer_id": "cus_1",
        "event_type": "payment_failed",
        "decline_code": "expired_card",
        "amount": Decimal("49.00"),
        "currency": "INR",
        "prior_attempts": 0,
        "customer_history": CustomerHistory(tenure_days=120, past_failures=1),
        "detected_at": datetime.now(UTC),
    }
    assert EventRecord(**valid).event_type == "payment_failed"

    with pytest.raises(ValidationError):
        EventRecord(**valid, card_number="4242424242424242")


def test_diagnosis_rejects_invented_root_cause() -> None:
    """The LLM cannot smuggle a free-text category past validation."""
    with pytest.raises(ValidationError):
        Diagnosis(event_id="evt_1", root_cause="low_funds", confidence=0.9, reasoning="x")

    ok = Diagnosis(
        event_id="evt_1",
        root_cause="insufficient_funds",
        confidence=0.91,
        reasoning="Decline code indicates the account lacked available balance.",
    )
    assert ok.root_cause is RootCause.INSUFFICIENT_FUNDS


def test_diagnosis_confidence_is_bounded() -> None:
    with pytest.raises(ValidationError):
        Diagnosis(event_id="e", root_cause="unknown", confidence=1.4, reasoning="x")


@pytest.mark.parametrize(
    ("module_name", "func_name"),
    [
        ("app.detect", "detect_event"),
        ("app.diagnose", "diagnose_root_cause"),
        ("app.decide", "decide_action"),
        ("app.execute", "execute_action"),
        ("app.guardrails", "run_all_checks"),
    ],
)
def test_no_pipeline_stage_is_still_a_stub(module_name: str, func_name: str) -> None:
    """All four stages plus the guardrails are implemented as of Phase 5.

    This test used to assert the inverse — that unbuilt stages raise
    NotImplementedError rather than returning a plausible-looking fake. Now that
    every stage is built it asserts the opposite, so a stage silently reverting to
    a stub would fail the build instead of quietly returning nothing.
    """
    module = __import__(module_name, fromlist=[func_name])
    func = getattr(module, func_name)
    source = inspect.getsource(func)
    assert "NotImplementedError" not in source, (
        f"{module_name}.{func_name} is still a stub"
    )
