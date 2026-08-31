"""Database engine, session handling, and schema creation.

Postgres per ``code-standards.md``, holding both the event pipeline tables and
the audit log so the final metrics are a query rather than a log-scrape.

TRADE-OFF, stated plainly: schema is created with ``create_all()`` rather than
Alembic migrations. Fine for a demo whose database is disposable, and it keeps
Phase 2 focused. It would not survive a real deployment, where a schema change
against existing data needs a migration with a rollback path. If this project
ever outlives its fixtures, that is the first thing to replace.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Base

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


class DatabaseNotConfiguredError(RuntimeError):
    """Raised when no usable connection URL is available.

    Deliberately explicit rather than falling back to SQLite. A silent downgrade
    would mean the demo's audit log lands somewhere the metrics never read from,
    and everything would look like it worked.
    """


def get_engine() -> Engine:
    """Process-wide engine, created on first use."""
    global _engine
    if _engine is None:
        url = get_settings().effective_database_url
        if url is None:
            raise DatabaseNotConfiguredError(
                "No database configured. Set POSTGRES_PASSWORD in .env and run "
                "`docker compose up -d --wait`. See /readiness for what is missing."
            )
        _engine = create_engine(
            url,
            pool_pre_ping=True,
            # Short timeout so a stopped container fails fast instead of hanging
            # the request for minutes on OS-level TCP retries.
            connect_args={"connect_timeout": 5},
            future=True,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), expire_on_commit=False, future=True
        )
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commits on success, rolls back on any exception."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    with session_scope() as session:
        yield session


def init_db() -> None:
    """Create any missing tables.

    Called explicitly, never as an import side effect, so importing ``app`` does
    not silently touch a database.
    """
    Base.metadata.create_all(bind=get_engine())


def reset_engine() -> None:
    """Drop cached engine and session factory. For tests that change config."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
