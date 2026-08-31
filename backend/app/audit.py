"""Structured audit-trail writing.

``code-standards.md``: every stage writes one structured entry per event, to the
audit table, never via print statements. Each entry carries ``event_id``,
``stage``, ``timestamp``, ``input_summary``, ``output_summary``, and for DECIDE
every guardrail check result including the ones that passed.

The bar from ``project-overview.md`` is that a stranger can read the trail for
any single event and understand what happened and why, in under 30 seconds. So
summaries record the values a reader needs to follow the decision, not a dump of
everything available.

Never write raw card data here. ``assert_no_sensitive_card_data`` enforces that
at the point of persistence rather than trusting callers to remember.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLogEntry
from app.schemas import Stage

# PCI-sensitive keys. Razorpay never sends these, so their presence means either
# a payload from somewhere else or a change at the provider. Either way it must
# stop rather than be quietly stored.
FORBIDDEN_KEYS = frozenset(
    {
        "number", "pan", "card_number", "cardnumber", "cvv", "cvc", "cvv2",
        "expiry", "expiry_month", "expiry_year", "exp_month", "exp_year",
        "security_code", "track_data",
    }
)


class SensitiveCardDataError(RuntimeError):
    """Raised when a payload contains data that must never reach storage.

    Implements Non-Negotiable Constraint #1 as code. The constraint says raw card
    data must never touch the system, so this is a hard stop at the boundary, not
    a warning to be triaged later.
    """


def assert_no_sensitive_card_data(payload: Any, _path: str = "$") -> None:
    """Recursively reject any structure containing PCI-sensitive keys."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise SensitiveCardDataError(
                    f"payload contains forbidden key {key!r} at {_path}. "
                    "Raw card data must never be stored (architecture.md "
                    "constraint #1). Rejecting rather than persisting."
                )
            assert_no_sensitive_card_data(value, f"{_path}.{key}")
    elif isinstance(payload, list):
        for i, item in enumerate(payload):
            assert_no_sensitive_card_data(item, f"{_path}[{i}]")


def record(
    session: Session,
    *,
    event_id: str,
    stage: Stage,
    input_summary: dict[str, Any],
    output_summary: dict[str, Any],
    guardrail_checks: list[dict[str, Any]] | None = None,
    notes: str | None = None,
) -> AuditLogEntry:
    """Append one audit entry. Caller owns the transaction."""
    assert_no_sensitive_card_data(input_summary)
    assert_no_sensitive_card_data(output_summary)

    entry = AuditLogEntry(
        event_id=event_id,
        stage=str(stage),
        input_summary=input_summary,
        output_summary=output_summary,
        guardrail_checks=guardrail_checks,
        notes=notes,
    )
    session.add(entry)
    return entry


def trail_for_event(session: Session, event_id: str) -> list[dict[str, Any]]:
    """The full ordered trail for one event, ready to render or serialise."""
    entries = (
        session.query(AuditLogEntry)
        .filter(AuditLogEntry.event_id == event_id)
        .order_by(AuditLogEntry.id)
        .all()
    )
    return [
        {
            "stage": e.stage,
            "timestamp": e.timestamp.isoformat() if e.timestamp else None,
            "input_summary": e.input_summary,
            "output_summary": e.output_summary,
            "guardrail_checks": e.guardrail_checks,
            "notes": e.notes,
        }
        for e in entries
    ]


def as_json_line(entry: AuditLogEntry) -> str:
    """One-line JSON rendering, for streaming a trail to stdout or a file."""
    return json.dumps(
        {
            "event_id": entry.event_id,
            "stage": entry.stage,
            "timestamp": entry.timestamp.isoformat() if entry.timestamp else None,
            "input_summary": entry.input_summary,
            "output_summary": entry.output_summary,
            "guardrail_checks": entry.guardrail_checks,
        },
        separators=(",", ":"),
    )
