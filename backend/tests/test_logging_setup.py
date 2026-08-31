"""Structured logging tests.

``code-standards.md`` asks for a structured log entry, JSON, one line per event per
stage, never print statements. Two properties are worth defending:

* **One line, valid JSON, with the required fields.** A multi-line or
  almost-JSON record defeats the point, which is that these can be grepped and
  piped into jq while a batch runs.
* **The line comes from ``audit.record``.** If each stage logged for itself, a
  stage could log without auditing or audit without logging, and the two accounts
  of what happened would drift. Emitting from the one function that writes the
  audit row makes them the same event by construction.

Non-pipeline records must stay human-readable: a demo where uvicorn's access log
is unreadable JSON is worse than one with two formats.
"""

from __future__ import annotations

import json
import logging

import pytest

from app import audit, logging_setup
from app.logging_setup import STAGE_LOGGER_NAME, StageJsonFormatter, configure_logging
from app.schemas import Stage


@pytest.fixture
def stage_records(caplog):
    caplog.set_level(logging.INFO, logger=STAGE_LOGGER_NAME)
    return caplog


def formatted(record: logging.LogRecord) -> str:
    return StageJsonFormatter("%(levelname)s %(name)s: %(message)s").format(record)


def test_a_stage_record_formats_as_one_json_line(stage_records) -> None:
    logging_setup.log_stage(
        event_id="evt-1",
        stage=str(Stage.DIAGNOSE),
        input_summary={"decline_code": "card_expired"},
        output_summary={"root_cause": "card_expired", "confidence": 0.95},
    )

    line = formatted(stage_records.records[-1])

    assert "\n" not in line
    payload = json.loads(line)
    assert payload["event_id"] == "evt-1"
    assert payload["stage"] == "diagnose"
    assert payload["output_summary"]["root_cause"] == "card_expired"
    assert payload["level"] == "INFO"
    assert "ts" in payload


def test_notes_are_included_only_when_present(stage_records) -> None:
    logging_setup.log_stage(
        event_id="evt-1",
        stage=str(Stage.EXECUTE),
        input_summary={},
        output_summary={},
        notes="Queued for human review.",
    )
    with_notes = json.loads(formatted(stage_records.records[-1]))

    logging_setup.log_stage(
        event_id="evt-2",
        stage=str(Stage.EXECUTE),
        input_summary={},
        output_summary={},
    )
    without_notes = json.loads(formatted(stage_records.records[-1]))

    assert with_notes["notes"] == "Queued for human review."
    assert "notes" not in without_notes


def test_unserialisable_values_do_not_break_the_line(stage_records) -> None:
    """A datetime in a summary must not take the log line down mid-batch."""
    from datetime import UTC, datetime

    logging_setup.log_stage(
        event_id="evt-1",
        stage=str(Stage.DECIDE),
        input_summary={"now": datetime(2026, 6, 15, tzinfo=UTC)},
        output_summary={},
    )

    payload = json.loads(formatted(stage_records.records[-1]))

    assert "2026-06-15" in payload["input_summary"]["now"]


def test_a_normal_log_record_stays_human_readable() -> None:
    record = logging.LogRecord(
        name="app.detect",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="rejected webhook: %s",
        args=("bad signature",),
        exc_info=None,
    )

    line = formatted(record)

    assert line == "WARNING app.detect: rejected webhook: bad signature"
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)


def test_configure_logging_does_not_stack_handlers() -> None:
    """A reload must not make every line appear twice."""
    root = logging.getLogger()
    configure_logging()
    before = len(root.handlers)

    configure_logging()
    configure_logging()

    assert len(root.handlers) == before
    assert all(
        isinstance(handler.formatter, StageJsonFormatter) for handler in root.handlers
    )


def test_audit_record_emits_a_stage_line(stage_records) -> None:
    """The coupling that keeps the table and the stream describing one event."""

    class FakeSession:
        def __init__(self) -> None:
            self.added: list[object] = []

        def add(self, obj: object) -> None:
            self.added.append(obj)

    session = FakeSession()
    audit.record(
        session,  # type: ignore[arg-type]
        event_id="evt-audit",
        stage=Stage.DETECT,
        input_summary={"provider_event_name": "payment.failed"},
        output_summary={"event_type": "payment_failed"},
    )

    assert len(session.added) == 1
    stage_lines = [
        json.loads(formatted(r))
        for r in stage_records.records
        if getattr(r, "is_stage_record", False)
    ]
    assert stage_lines[-1]["event_id"] == "evt-audit"
    assert stage_lines[-1]["stage"] == "detect"


def test_audit_record_still_refuses_card_data_before_logging(stage_records) -> None:
    """The card-data gate must run before anything is emitted anywhere."""

    class FakeSession:
        def add(self, obj: object) -> None:  # pragma: no cover - must not be reached
            raise AssertionError("should not persist a rejected payload")

    with pytest.raises(audit.SensitiveCardDataError):
        audit.record(
            FakeSession(),  # type: ignore[arg-type]
            event_id="evt-bad",
            stage=Stage.DETECT,
            input_summary={"card": {"cvv": "123"}},
            output_summary={},
        )

    assert not [
        r for r in stage_records.records if getattr(r, "is_stage_record", False)
    ]
