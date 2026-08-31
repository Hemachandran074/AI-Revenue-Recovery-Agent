"""Dashboard tests.

The dashboard is the artefact a reviewer actually looks at, so the tests are about
whether it tells the truth rather than whether it looks nice:

* **A violation cannot hide.** ``ui-context.md`` asks for a violation count that is
  visually obvious when nonzero. A page that rendered "0" while violations existed
  would be worse than no page.
* **All four guardrail results are shown, passes included.** Constraint #5 made
  visible. Rendering only failures would look tidier and would defeat the point.
* **Colour is never the only signal.** Every tinted element also states its meaning
  in words, so the page survives greyscale and screen readers.
* **Model output is escaped.** DIAGNOSE's ``reasoning`` is generated text and
  provider error descriptions are third-party strings; both are untrusted input
  being rendered into HTML.
* **Nothing claims recovered money.** ``amount_recovered`` is null until a provider
  webhook confirms payment, and the page must say so rather than showing 0.00 as
  though it were a measurement.

Rendering is tested directly against ``render_page`` with hand-built rows, so these
need no database. The HTTP layer is covered separately and does.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import dashboard
from app import metrics as metrics_module
from app.config import Settings, get_settings
from app.dashboard import render_page
from app.db import get_db
from app.main import app
from app.metrics import (
    BatchMetrics,
    EventRow,
    LatencyStats,
    MoneyMetrics,
    Violation,
    audit_coverage,
    find_violations,
)
from app.models import AuditLogEntry
from app.schemas import Action, GuardrailName, Stage

NOW = datetime(2026, 6, 15, 12, 30, tzinfo=UTC)
ALL_GUARDRAILS = [
    {"name": str(name), "passed": True, "detail": "within limits"}
    for name in GuardrailName
]


@pytest.fixture(autouse=True)
def _pin_settings(monkeypatch):
    settings = Settings(
        _env_file=None,
        quiet_hours_start_local=9,
        quiet_hours_end_local=20,
        min_hours_between_contacts=24,
        max_recovery_attempts=3,
        hard_stop_days=7,
    )
    monkeypatch.setattr(metrics_module, "get_settings", lambda: settings)
    return settings


def row(**overrides: Any) -> EventRow:
    base: dict[str, Any] = {
        "event_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "customer_id": "cust_dash1",
        "event_type": "payment_failed",
        "decline_code": "card_expired",
        "amount_minor": 49900,
        "currency": "INR",
        "prior_attempts": 0,
        "first_failure_at": NOW - timedelta(hours=1),
        "received_at": NOW,
        "customer_timezone": "Asia/Kolkata",
        "root_cause": "card_expired",
        "confidence": 0.95,
        "reasoning": "The decline code states the card has expired.",
        "action": str(Action.SEND_UPDATE_PAYMENT_METHOD_LINK),
        "channel": "whatsapp",
        "scheduled_for": NOW,
        "guardrail_checks": list(ALL_GUARDRAILS),
        "delivery_status": "sent",
        "customer_outcome": "pending",
        "amount_recovered_minor": None,
        "recovery_link_id": "plink_DASH01",
        "executed_at": NOW,
        "decision_latency_ms": 118.4,
        "send_latency_ms": 342.9,
        "stages": ["detect", "diagnose", "decide", "execute"],
    }
    base.update(overrides)
    return EventRow(**base)


def batch(rows: list[EventRow], violations: list[Violation] | None = None):
    found = violations if violations is not None else find_violations(rows)
    at_risk = sum(r.amount_minor for r in rows)
    return BatchMetrics(
        events_total=len(rows),
        money=MoneyMetrics(
            at_risk_minor=at_risk,
            recovered_minor=sum(r.amount_recovered_minor or 0 for r in rows),
            actioned_at_risk_minor=sum(
                r.amount_minor for r in rows if r.is_actioned
            ),
        ),
        decision_latency=LatencyStats.of(
            [r.decision_latency_ms for r in rows if r.decision_latency_ms is not None]
        ),
        send_latency=LatencyStats.of(
            [r.send_latency_ms for r in rows if r.send_latency_ms is not None]
        ),
        violations=found,
        audit=audit_coverage(rows),
        disposition={r.disposition: 1 for r in rows},
        by_root_cause={"card_expired": len(rows)},
        by_action={str(Action.SEND_UPDATE_PAYMENT_METHOD_LINK): len(rows)},
        by_delivery_status={"sent": len(rows)},
        by_customer_outcome={"pending": len(rows)},
        by_event_type={"payment_failed": len(rows)},
        classifier_unavailable=sum(1 for r in rows if r.classifier_unavailable),
        guardrail_config={},
    )


def trail(event_id: str) -> dict[str, list[AuditLogEntry]]:
    """Four audit entries, as the pipeline would have written them."""
    entries = []
    for stage, output in (
        (Stage.DETECT, {"event_type": "payment_failed", "amount_minor": 49900}),
        (Stage.DIAGNOSE, {"root_cause": "card_expired", "confidence": 0.95}),
        (Stage.DECIDE, {"action": "send_update_payment_method_link"}),
        (Stage.EXECUTE, {"delivery_status": "sent"}),
    ):
        entry = AuditLogEntry(
            event_id=event_id,
            stage=str(stage),
            input_summary={"stage_input": str(stage)},
            output_summary=output,
        )
        entry.timestamp = NOW
        entries.append(entry)
    return {event_id: entries}


def render(rows: list[EventRow], violations: list[Violation] | None = None) -> str:
    trails: dict[str, list[AuditLogEntry]] = {}
    for r in rows:
        trails.update(trail(r.event_id))
    return render_page(batch(rows, violations), rows, trails)


# ------------------------------------------------------------ headline block


def test_page_is_well_formed_html() -> None:
    html = render([row()])

    assert html.startswith("<!DOCTYPE html>")
    assert html.rstrip().endswith("</html>")
    assert '<html lang="en">' in html


def test_headline_shows_recovered_over_at_risk() -> None:
    html = render([row()])

    assert "0.00 / 499.00" in html
    assert "Recovered / at risk" in html


def test_headline_shows_both_latencies_separately() -> None:
    """Known issue A. One blended figure would score a deferral as failure."""
    html = render([row()])

    assert "Decision latency" in html
    assert "Send latency" in html
    assert "118 ms" in html
    assert "343 ms" in html


def test_headline_states_the_sixty_second_target() -> None:
    html = render([row()])

    assert "over the 60s target" in html


def test_headline_reports_audit_coverage() -> None:
    html = render([row()])

    assert "Audit coverage" in html
    assert "all four stages" in html


def test_incomplete_coverage_is_not_shown_as_good() -> None:
    html = render([row(stages=["detect", "diagnose"])])

    assert "metric warn" in html
    assert "2/4" in html


# ---------------------------------------------------------------- violations


def test_zero_violations_reads_as_zero_and_explains_the_method() -> None:
    html = render([row()])

    assert "ok-banner" in html
    assert "grading itself" in html


def test_a_violation_is_impossible_to_miss() -> None:
    """The count, the banner and the rule name all have to appear."""
    stale = row(first_failure_at=NOW - timedelta(days=9))

    html = render([stale])

    assert "bad-banner" in html
    assert "1 violation(s) found" in html
    assert "hard_stop_7_days" in html
    assert "metric bad" in html


def test_every_violation_is_listed_with_its_event_and_reason() -> None:
    stale = row(first_failure_at=NOW - timedelta(days=9), prior_attempts=5)

    html = render([stale])

    assert "hard_stop_7_days" in html
    assert "max_retries" in html
    assert "2 violation(s) found" in html


def test_a_violation_links_to_the_offending_events_trail() -> None:
    stale = row(first_failure_at=NOW - timedelta(days=9))

    html = render([stale])

    assert f'href="#event-{stale.event_id}"' in html
    assert f'id="event-{stale.event_id}"' in html


# ------------------------------------------------- constraint #5 made visible


def test_all_four_guardrail_results_are_rendered_including_passes() -> None:
    html = render([row()])

    for name in GuardrailName:
        assert str(name) in html
    assert html.count(">PASS<") >= 4


def test_a_failed_check_is_rendered_as_failed() -> None:
    checks = [
        {"name": str(name), "passed": name is not GuardrailName.QUIET_HOURS,
         "detail": "detail text"}
        for name in GuardrailName
    ]

    html = render(
        [
            row(
                guardrail_checks=checks,
                delivery_status="skipped",
                skip_reason="Deferred until 2026-06-16T03:30:00+00:00: ...",
            )
        ]
    )

    assert ">FAIL<" in html
    assert ">PASS<" in html


def test_the_blocking_reason_is_shown_when_one_exists() -> None:
    html = render(
        [
            row(
                blocked_reason="Stopped by max_retries. 3 of 3 attempts used.",
                delivery_status="skipped",
            )
        ]
    )

    assert "Stopped by max_retries" in html
    assert "Blocked:" in html


def test_no_block_is_stated_rather_than_left_blank() -> None:
    html = render([row()])

    assert "No guardrail cancelled this action" in html


# --------------------------------------------------------- money honesty


def test_unconfirmed_recovery_is_stated_not_shown_as_zero() -> None:
    """0.00 would read as a measured result. It is an absence of one."""
    html = render([row()])

    assert "not confirmed (awaiting a provider webhook)" in html


def test_footer_states_why_recovered_is_zero() -> None:
    html = render([row()])

    assert "a delivered message is not a payment" in html


def test_a_confirmed_recovery_renders_the_amount() -> None:
    html = render([row(amount_recovered_minor=49900, customer_outcome="recovered")])

    assert "499.00" in html


# ----------------------------------------------------- escaping and a11y


def test_model_generated_reasoning_is_escaped() -> None:
    """DIAGNOSE reasoning is generated text rendered into HTML."""
    html = render([row(reasoning="<script>alert('xss')</script>")])

    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_provider_error_text_is_escaped() -> None:
    html = render([row(decline_code="<img src=x onerror=alert(1)>")])

    assert "<img src=x" not in html
    assert "&lt;img" in html


def test_blocked_reason_is_escaped() -> None:
    html = render(
        [row(blocked_reason="<b>bold</b>", delivery_status="skipped")]
    )

    assert "<b>bold</b>" not in html
    assert "&lt;b&gt;" in html


def test_guardrail_detail_is_escaped() -> None:
    checks = [
        {"name": str(name), "passed": True, "detail": "<i>x</i>"}
        for name in GuardrailName
    ]

    html = render([row(guardrail_checks=checks)])

    assert "<i>x</i>" not in html


def test_every_coloured_pill_also_carries_text() -> None:
    """Colour is never the only signal, per ui-context.md and for a11y."""
    html = render([row()])

    assert "Contacted" in html
    # A pill with no text between the tags would be colour-only.
    assert '"pill good"></span>' not in html
    assert '"pill bad"></span>' not in html


def test_tables_use_scoped_headers_and_captions() -> None:
    html = render([row()])

    assert 'scope="col"' in html
    assert 'scope="row"' in html
    assert "sr-only" in html  # visually hidden captions for screen readers


def test_details_elements_provide_the_drilldown_without_javascript() -> None:
    """No script tag at all: nothing to fail mid-demo."""
    html = render([row()])

    assert "<details" in html
    assert "<summary>" in html
    assert "<script" not in html


# ------------------------------------------------------------- batch table


def test_batch_table_has_every_column_ui_context_asks_for() -> None:
    html = render([row()])

    for header in (
        "Event", "Type", "Root cause", "Action", "Channel", "Outcome",
        "Amount", "Send latency",
    ):
        assert f">{header}<" in html


def test_all_four_stages_appear_in_the_trail() -> None:
    html = render([row()])

    for stage in ("DETECT", "DIAGNOSE", "DECIDE", "EXECUTE"):
        assert f">{stage} <" in html


def test_an_empty_batch_says_so_and_says_how_to_fix_it() -> None:
    html = render([])

    assert "No events yet" in html
    assert "python -m app.replay" in html


def test_an_empty_batch_reports_unknown_rather_than_perfect() -> None:
    html = render([])

    assert "n/a" in html


def test_a_missing_trail_is_flagged_rather_than_silently_empty() -> None:
    metrics = batch([row()])
    html = render_page(metrics, [row()], {})

    assert "No audit entries for this event" in html


def test_classifier_outage_is_labelled_as_such_in_the_table() -> None:
    html = render(
        [
            row(
                classifier_unavailable=True,
                root_cause="unknown",
                action=str(Action.ESCALATE_TO_HUMAN_REVIEW),
                delivery_status="skipped",
                channel="none",
            )
        ]
    )

    assert "Classifier unavailable" in html


def test_audit_notes_are_rendered_when_present() -> None:
    entries = trail(row().event_id)
    entries[row().event_id][1].notes = "Classifier did not run; escalated."

    html = render_page(batch([row()]), [row()], entries)

    assert "Classifier did not run" in html
    assert "Note:" in html


# ----------------------------------------------------------------- HTTP layer

pytestmark_integration = pytest.mark.integration


@pytest.fixture
def client(monkeypatch):
    """TestClient with the DB session faked out by a stub returning no rows."""

    class EmptySession:
        def scalars(self, *_args, **_kwargs):
            return iter([])

        def execute(self, *_args, **_kwargs):
            return iter([])

    settings = Settings(_env_file=None)
    monkeypatch.setattr(metrics_module, "get_settings", lambda: settings)
    app.dependency_overrides[get_db] = lambda: EmptySession()
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_dashboard_route_serves_html(client) -> None:
    response = client.get("/dashboard")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "AI Revenue Recovery Agent" in response.text


def test_metrics_api_returns_the_documented_shape(client) -> None:
    body = client.get("/api/metrics").json()

    for key in (
        "events_total", "money", "decision_latency", "send_latency",
        "violations", "violation_count", "audit", "disposition",
        "latency_budget_ms",
    ):
        assert key in body
    assert body["violation_count"] == 0
    assert body["latency_budget_ms"] == 60000.0


def test_events_api_returns_a_list(client) -> None:
    body = client.get("/api/events").json()

    assert body == {"count": 0, "events": []}


def test_page_size_is_capped(client) -> None:
    """A demo box should not be asked to render ten thousand rows."""
    assert dashboard._cap(10_000) == 500
    assert dashboard._cap(0) == 1
    assert dashboard._cap(-5) == 1
    assert dashboard._cap(50) == 50
    assert client.get("/dashboard?limit=10000").status_code == 200


def test_dashboard_is_not_exposed_on_the_public_tunnel_app() -> None:
    """The tunnel publishes only the signature-verified webhook.

    The dashboard returns customer ids, amounts and decline reasons for every
    event, so it must never be reachable from the public URL.
    """
    from app.tunnel import app as tunnel_app

    # Read from the OpenAPI schema, not app.routes: this FastAPI version wraps
    # included routers in a lazy object whose nested paths are not visible on the
    # top-level route list. Same approach as test_tunnel_app.py.
    paths = set(tunnel_app.openapi()["paths"])

    assert "/dashboard" not in paths
    assert "/api/metrics" not in paths
    assert "/api/events" not in paths
    assert "/webhooks/razorpay" in paths


def test_dashboard_returns_404_on_the_tunnel_app() -> None:
    """Not merely unrouted: actually unreachable over HTTP."""
    from app.tunnel import app as tunnel_app

    with TestClient(tunnel_app) as tunnel_client:
        for path in ("/dashboard", "/api/metrics", "/api/events"):
            assert tunnel_client.get(path).status_code == 404
