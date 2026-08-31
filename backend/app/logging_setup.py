"""Structured JSON logging.

``code-standards.md`` -> Logging & audit trail asks for a structured log entry,
JSON, one line per event per stage, never print statements. The ``audit_log``
table already satisfies the durable half of that; this is the stream half, which
is what you actually watch while a batch runs.

Two things are worth knowing about the design.

**The stage log line is emitted by ``audit.record``, not by the stages.** If each
stage logged for itself, a stage could log without auditing or audit without
logging, and the two records of what happened would drift. Emitting from the one
function that writes the audit row makes them structurally the same event.

**Everything else keeps logging normally.** Only the pipeline-stage records are
JSON; uvicorn's access log and library warnings stay human-readable, because a
demo where the console is unreadable is worse than one with mixed formats. The
formatter here renders JSON when a record carries stage fields and falls back to
plain text otherwise.

No card data can reach these lines: the summaries are the same objects
``audit.record`` has already passed through ``assert_no_sensitive_card_data``.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

STAGE_LOGGER_NAME = "pipeline.stage"

# Attribute names this module attaches to a LogRecord. Anything else on the
# record is ignored, so a stray kwarg cannot silently reshape the log schema.
_STAGE_FIELDS = ("event_id", "stage", "input_summary", "output_summary", "notes")


class StageJsonFormatter(logging.Formatter):
    """JSON for pipeline-stage records, plain text for everything else."""

    def format(self, record: logging.LogRecord) -> str:
        if not getattr(record, "is_stage_record", False):
            return super().format(record)

        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
        }
        for field in _STAGE_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        # separators keeps it to one compact line, which is the point: these are
        # meant to be greppable and pipeable into jq, not read as prose.
        return json.dumps(payload, separators=(",", ":"), default=str)


def log_stage(
    *,
    event_id: str,
    stage: str,
    input_summary: dict[str, Any],
    output_summary: dict[str, Any],
    notes: str | None = None,
) -> None:
    """Emit one structured line for one event at one stage."""
    logging.getLogger(STAGE_LOGGER_NAME).info(
        "%s %s",
        stage,
        event_id,
        extra={
            "is_stage_record": True,
            "event_id": event_id,
            "stage": stage,
            "input_summary": input_summary,
            "output_summary": output_summary,
            "notes": notes,
        },
    )


def configure_logging(level: int = logging.INFO) -> None:
    """Install the formatter on the root handler. Idempotent.

    Called from the app entrypoints. Safe to call more than once: re-running
    replaces the formatter rather than stacking another handler, so a reload does
    not produce every line twice.
    """
    root = logging.getLogger()
    root.setLevel(level)

    formatter = StageJsonFormatter("%(levelname)s %(name)s: %(message)s")
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        root.addHandler(handler)
        return
    for handler in root.handlers:
        handler.setFormatter(formatter)
