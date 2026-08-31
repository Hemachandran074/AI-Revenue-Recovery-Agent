"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTEXT_DIR = REPO_ROOT / "context"


@pytest.fixture(scope="session")
def architecture_doc() -> str:
    """Raw text of ``context/architecture.md``.

    The schema-contract tests read the doc directly so code and docs cannot
    drift apart silently.
    """
    return (CONTEXT_DIR / "architecture.md").read_text(encoding="utf-8")


@pytest.fixture
def clean_settings() -> Settings:
    """Settings with no ``.env`` loaded, so tests do not depend on local secrets."""
    return Settings(_env_file=None)
