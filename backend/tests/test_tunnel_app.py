"""Tests for the publicly-exposed webhook-only app.

This app is the single internet-facing surface, so the tests here are about what
must NOT be reachable through it. A route that leaks onto this app leaks to the
whole internet the moment the tunnel is running.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.tunnel import app as tunnel_app

# Routes that exist on app.main and must never appear on the tunnelled app.
LOCAL_ONLY_PATHS = [
    "/health",
    "/readiness",
    "/events/recent",
    "/events/some-event-id/audit",
    "/docs",
    "/redoc",
    "/openapi.json",
    # Phase 6. These are the worst of the set to leak: the dashboard and its JSON
    # counterparts return customer ids, amounts at risk, decline reasons and
    # payment-link ids for every event in the database.
    "/dashboard",
    "/api/metrics",
    "/api/events",
]


@pytest.fixture
def client() -> TestClient:
    return TestClient(tunnel_app)


def test_only_the_webhook_and_liveness_routes_are_mounted() -> None:
    """Whitelist assertion.

    An exact comparison rather than a list of absence checks, so any route added
    later fails here instead of silently becoming internet-facing.

    Reads paths from the OpenAPI schema rather than walking ``app.routes``:
    this FastAPI version wraps included routers in a lazy object whose nested
    paths are not visible on the top-level route list, so introspecting that
    structure would be brittle across versions.
    """
    assert set(tunnel_app.openapi()["paths"]) == {
        "/webhooks/razorpay",
        "/tunnel-health",
    }


def test_tunnel_app_exposes_strictly_less_than_the_local_app() -> None:
    """The public surface must be a subset of the local one, minus the ops routes.

    Guards the actual invariant: everything reachable publicly is either the
    signed webhook or the contentless liveness probe.
    """
    from app.main import app as local_app

    local_paths = set(local_app.openapi()["paths"])
    tunnel_paths = set(tunnel_app.openapi()["paths"])

    assert "/webhooks/razorpay" in tunnel_paths
    assert tunnel_paths - {"/tunnel-health"} <= local_paths
    # None of the unauthenticated ops routes may cross over. /events/recent in
    # particular would let anyone enumerate events and read customer ids.
    assert not tunnel_paths & {
        "/health",
        "/readiness",
        "/events/recent",
        "/events/{event_id}/audit",
    }


@pytest.mark.parametrize("path", LOCAL_ONLY_PATHS)
def test_local_only_endpoints_are_not_reachable(client: TestClient, path: str) -> None:
    """404 because the route does not exist, not because a check refused it.

    Nothing to bypass is stronger than something to bypass.
    """
    assert client.get(path).status_code == 404


def test_readiness_cannot_leak_credential_state(client: TestClient) -> None:
    """/readiness names which keys are missing. Useful locally, not publicly."""
    response = client.get("/readiness")
    assert response.status_code == 404
    assert "GEMINI_API_KEY" not in response.text
    assert "missing_required_keys" not in response.text


def test_liveness_reveals_nothing_beyond_status(client: TestClient) -> None:
    response = client.get("/tunnel-health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_interactive_docs_are_disabled(client: TestClient) -> None:
    """Public API docs would advertise the payload schema to anyone probing."""
    assert tunnel_app.docs_url is None
    assert tunnel_app.openapi_url is None


def test_webhook_route_is_present_and_rejects_unsigned_requests(
    client: TestClient,
) -> None:
    """The one exposed route must still fail closed.

    No dependency overrides here: with no webhook secret configured this must
    reject rather than reach DETECT, which is exactly the posture an
    unconfigured public deployment needs.
    """
    response = client.post(
        "/webhooks/razorpay",
        content=b'{"entity":"event","event":"payment.failed","payload":{}}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 401
    assert response.json()["status"] == "rejected"


def test_unsigned_request_never_reaches_detect(client: TestClient, monkeypatch) -> None:
    """Proves rejection happens before any pipeline work is attempted."""
    called = False

    def _fail(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("detect_event must not run for an unsigned request")

    monkeypatch.setattr("app.webhooks.detect.detect_event", _fail)
    client.post(
        "/webhooks/razorpay",
        content=b'{"entity":"event","event":"payment.failed","payload":{}}',
        headers={"Content-Type": "application/json"},
    )
    assert called is False
