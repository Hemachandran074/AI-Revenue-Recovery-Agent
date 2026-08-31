"""Serialising generated batches to disk, and loading them back.

The on-disk format keeps three things visibly separate, because conflating them
is how a pipeline ends up accidentally reading its own answer key:

  ``webhook``          exactly what Razorpay would POST. The only thing DETECT
                       is allowed to consume.
  ``customer_context`` our own records (tenure, timezone, contact). Not from
                       Razorpay. A real deployment would read this from its own
                       customer table.
  ``ground_truth``     the correct answer, for scoring DIAGNOSE and DECIDE.
                       Never an input to any stage.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.simulation.generator import BatchFixture, generate_batch

FIXTURE_VERSION = 1
DEFAULT_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures"


def batch_to_dict(batch: BatchFixture) -> dict[str, Any]:
    """Render a batch as the JSON-serialisable fixture structure."""
    return {
        "fixture_version": FIXTURE_VERSION,
        "provider": "razorpay",
        "razorpay_events": sorted({e.razorpay_event for e in batch.events}),
        "notice": (
            "Synthetic test data. Contact details are invented and use reserved "
            "example domains; never message them. 'ground_truth' is for scoring "
            "only and must never be fed into the pipeline."
        ),
        "summary": batch.summary(),
        "events": [
            {
                "event_id": e.event_id,
                "webhook": e.envelope,
                "customer_context": {
                    "customer_id": e.customer.customer_id,
                    "tenure_days": e.customer.tenure_days,
                    "past_failures": e.customer.past_failures,
                    "timezone": e.customer.timezone,
                    "preferred_method": str(e.customer.preferred_method),
                },
                "pipeline_context": {
                    "event_type": str(e.event_type),
                    "razorpay_event": e.razorpay_event,
                    "prior_attempts": e.prior_attempts,
                    "first_failure_at": e.first_failure_at.isoformat(),
                    "detected_at": e.detected_at.isoformat(),
                    "amount_paise": e.amount_paise,
                    # None for abandonment events: an expired link has no single
                    # payment method because the customer never chose one.
                    "method": str(e.method) if e.method is not None else None,
                },
                "ground_truth": {
                    "scenario_key": e.scenario_key,
                    "scenario_provenance": str(e.provenance),
                    "expected_root_cause": str(e.expected_root_cause),
                    "expected_guardrail_failures": [
                        str(g) for g in e.expected_guardrail_failures
                    ],
                    # Only set for abandonment events. This is the evidence the
                    # friction-vs-disinterest call rests on, and it is NOT present
                    # in a payment_link.expired payload — see app/simulation/
                    # abandonment.py. DETECT must supply it by enrichment.
                    "abandonment_signal": (
                        str(e.abandonment_signal)
                        if e.abandonment_signal is not None
                        else None
                    ),
                },
            }
            for e in batch.events
        ],
    }


def write_fixture(
    batch: BatchFixture, path: Path | None = None, fixture_dir: Path | None = None
) -> Path:
    """Write ``batch`` as pretty-printed JSON and return the path written."""
    if path is None:
        directory = fixture_dir or DEFAULT_FIXTURE_DIR
        directory.mkdir(parents=True, exist_ok=True)
        # Named generically because a batch now spans payment.failed,
        # payment_link.expired and invoice.expired.
        path = directory / f"batch_seed{batch.seed}_n{len(batch.events)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = batch_to_dict(batch)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


def load_fixture(path: Path) -> dict[str, Any]:
    """Load a fixture file, rejecting a version this code does not understand."""
    data = json.loads(path.read_text(encoding="utf-8"))
    version = data.get("fixture_version")
    if version != FIXTURE_VERSION:
        raise ValueError(
            f"{path.name} is fixture_version {version!r}, expected "
            f"{FIXTURE_VERSION}. Regenerate it rather than reading it loosely."
        )
    return data


def webhooks_only(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    """Just the Razorpay envelopes, in order.

    Use this when replaying into DETECT. It structurally prevents a caller from
    reaching ``ground_truth`` by accident.
    """
    return [event["webhook"] for event in fixture["events"]]


def regenerate_default(seed: int = 42, count: int = 75) -> Path:
    """Convenience for scripts and tests: generate and write in one call."""
    return write_fixture(generate_batch(seed=seed, count=count))
