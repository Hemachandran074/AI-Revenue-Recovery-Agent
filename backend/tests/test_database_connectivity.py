"""Integration checks against the Postgres container.

Skipped rather than failed when the container is not running, so `pytest` stays
green on a machine without Docker. Run `docker compose up -d --wait` first to
exercise these.

These exist because this project has a specific hazard: a native PostgreSQL
Windows service also runs on this machine on port 5432, while the container
publishes on 55432. Connecting to the wrong one would silently write the audit
log somewhere the metrics never read from, so "can connect" is not enough —
we assert *which* server answered.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app.config import Settings, get_settings

pytestmark = pytest.mark.integration

# The container is pinned to postgres:17-alpine in docker-compose.yml.
EXPECTED_MAJOR_VERSION = 17


@pytest.fixture(scope="module")
def engine():
    settings: Settings = get_settings()
    url = settings.effective_database_url
    if url is None:
        pytest.skip("No database configured: set POSTGRES_PASSWORD in .env")

    # Short connect_timeout so an absent container skips in seconds. Without it
    # the OS-level TCP retry can stall the suite for over two minutes.
    eng = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 3})
    try:
        with eng.connect():
            pass
    except OperationalError as exc:
        eng.dispose()
        pytest.skip(f"Postgres not reachable, run `docker compose up -d --wait`: {exc}")
    return eng


def test_can_connect_with_derived_credentials(engine) -> None:
    """Proves POSTGRES_* -> effective_database_url produces working credentials."""
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar_one() == 1


def test_connected_to_the_container_not_the_native_service(engine) -> None:
    """Guards against silently talking to the native PostgreSQL 18 on port 5432."""
    with engine.connect() as conn:
        version = conn.execute(text("SHOW server_version")).scalar_one()
    major = int(version.split(".")[0])
    assert major == EXPECTED_MAJOR_VERSION, (
        f"Connected to PostgreSQL {major}, expected {EXPECTED_MAJOR_VERSION} "
        "(the container). A different major version means the connection landed "
        "on the native Windows service instead. Check POSTGRES_PORT in .env."
    )


def test_expected_database_and_role(engine) -> None:
    """The container-provisioned role and database are what the app is using."""
    settings = get_settings()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT current_database()")).scalar_one() == (
            settings.postgres_db
        )
        assert conn.execute(text("SELECT current_user")).scalar_one() == (
            settings.postgres_user
        )


def test_can_write_and_read_back(engine) -> None:
    """The audit log needs real write permission, not just connect permission.

    Uses a temporary table so nothing is left behind.
    """
    with engine.begin() as conn:
        conn.execute(text("CREATE TEMPORARY TABLE _probe (id int primary key)"))
        conn.execute(text("INSERT INTO _probe (id) VALUES (1)"))
        assert conn.execute(text("SELECT id FROM _probe")).scalar_one() == 1
