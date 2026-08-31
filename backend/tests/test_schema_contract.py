"""Contract tests: code must not drift from ``context/architecture.md``.

``code-standards.md`` requires enum values to match the doc verbatim, and
``ai-workflow-rules.md`` forbids adding an action without updating the doc
first. These tests parse the doc and enforce both mechanically, so the rule
holds even when nobody remembers it.

If a test here fails, the fix is a decision, not a code tweak: either the doc
changed and code must follow, or code drifted and must be reverted.
"""

from __future__ import annotations

import re

import pytest

from app.schemas import (
    Action,
    Channel,
    CustomerOutcome,
    DeliveryStatus,
    EventType,
    GuardrailName,
    RootCause,
)


def _union_values(doc: str, field: str) -> set[str]:
    """Pull ``"field": "a | b | c"`` out of the doc's JSON schema blocks."""
    match = re.search(rf'"{field}":\s*"([^"]+)"', doc)
    assert match, f'could not find "{field}" union in architecture.md'
    return {part.strip() for part in match.group(1).split("|")}


def _action_table_rows(doc: str) -> list[tuple[str, str]]:
    """Parse the "Fixed action set" markdown table into (root_cause, action) pairs."""
    rows: list[tuple[str, str]] = []
    valid_causes = {c.value for c in RootCause}
    for line in doc.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2 or cells[0] not in valid_causes:
            continue
        rows.append((cells[0], cells[1]))
    return rows


def _action_verb(cell: str) -> str:
    """Strip inline parameters: ``schedule_retry(+N days)`` -> ``schedule_retry``."""
    return re.sub(r"\(.*?\)", "", cell).split(",")[0].strip()


@pytest.mark.parametrize(
    ("field", "enum_cls"),
    [
        ("event_type", EventType),
        ("root_cause", RootCause),
        ("channel", Channel),
        ("delivery_status", DeliveryStatus),
        ("customer_outcome", CustomerOutcome),
    ],
)
def test_enum_matches_architecture_doc(architecture_doc, field, enum_cls) -> None:
    assert {m.value for m in enum_cls} == _union_values(architecture_doc, field)


def test_action_table_covers_every_root_cause(architecture_doc) -> None:
    """All 8 rows present, exactly one action per root cause, no cause missing."""
    rows = _action_table_rows(architecture_doc)
    assert len(rows) == len(RootCause) == 8
    assert {cause for cause, _ in rows} == {c.value for c in RootCause}


def test_every_documented_action_exists_in_code(architecture_doc) -> None:
    """No action in the doc is unimplementable, and code invents no extra verb."""
    documented = {_action_verb(action) for _, action in _action_table_rows(architecture_doc)}
    assert documented == {a.value for a in Action}


def test_high_risk_causes_escalate_to_human(architecture_doc) -> None:
    """bank_risk_block and unknown must never map to an automated action."""
    table = {cause: _action_verb(action) for cause, action in _action_table_rows(architecture_doc)}
    assert table["bank_risk_block"] == Action.ESCALATE_TO_HUMAN_REVIEW
    assert table["unknown"] == Action.ESCALATE_TO_HUMAN_REVIEW


def test_all_four_stopping_rules_are_represented(architecture_doc) -> None:
    """Constraint #4 lists four stopping rules; the enum must carry all four.

    The Decision *example* in the doc lists only three guardrail names. The
    constraint is authoritative — see the decisions log in progress-tracker.md.
    """
    assert {g.value for g in GuardrailName} == {
        "max_retries",
        "quiet_hours",
        "contact_frequency",
        "hard_stop_7_days",
    }


def test_no_voice_channel_anywhere() -> None:
    """Voice is explicitly out of scope in project-overview.md. Enforce it in code."""
    assert "voice" not in {c.value for c in Channel}
    assert not any("voice" in a.value or "call" in a.value for a in Action)
