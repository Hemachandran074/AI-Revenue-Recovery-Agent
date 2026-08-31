"""Measure real DIAGNOSE accuracy against the Phase 1 ground-truth labels.

    python -m app.diagnose_eval --limit 40
    python -m app.diagnose_eval --file fixtures/batch_seed42_n75.json
    python -m app.diagnose_eval --limit 20 --threshold 0.9

This is a MEASUREMENT, not a test. It calls the real Gemini API, costs quota and
takes a couple of seconds per event, so it is deliberately not part of `pytest`.
The test suite uses a fake client to pin behaviour; this reports how well the
prompt actually classifies.

Why it can exist at all: the generator labels every event with the root cause a
correct diagnosis should reach (`ground_truth.expected_root_cause`). Those labels
are never fed to the model — only the fields DETECT would produce.

Read the confusion pairs, not just the headline number. Which mistakes happen
matters more than how many, because the costs are not symmetric: calling a
bank_risk_block `insufficient_funds` schedules a retry against an instrument the
issuer refused, whereas escalating an ambiguous case to a human is merely slower.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.diagnose import (
    CLASSIFIER_UNAVAILABLE_PREFIX,
    PROMPT_VERSION,
    DiagnoseNotConfiguredError,
    ProviderContext,
    build_client,
    diagnose_root_cause,
)
from app.schemas import CustomerHistory, EventRecord, EventType, RootCause
from app.simulation.fixtures import DEFAULT_FIXTURE_DIR, load_fixture

# Mistakes that could lead to an unsafe action rather than merely a slow one.
# Retrying an instrument the issuer blocked is the case architecture.md's
# constraint 3 exists to prevent.
UNSAFE_CONFUSIONS = {
    (RootCause.BANK_RISK_BLOCK, RootCause.INSUFFICIENT_FUNDS),
    (RootCause.BANK_RISK_BLOCK, RootCause.NETWORK_ERROR),
    (RootCause.CARD_EXPIRED, RootCause.INSUFFICIENT_FUNDS),
    (RootCause.CARD_EXPIRED, RootCause.NETWORK_ERROR),
}


def _newest_fixture() -> Path:
    candidates = sorted(
        (p for p in DEFAULT_FIXTURE_DIR.glob("batch_*.json")),
        key=lambda p: p.stat().st_mtime,
    )
    if not candidates:
        raise SystemExit(
            f"No batch fixture in {DEFAULT_FIXTURE_DIR}. Generate one:\n"
            "  python -m app.simulation"
        )
    return candidates[-1]


def _event_from_fixture(entry: dict[str, Any]) -> tuple[EventRecord, ProviderContext]:
    """Rebuild what DETECT would hand DIAGNOSE, using no ground-truth fields."""
    pipeline = entry["pipeline_context"]
    customer = entry["customer_context"]
    webhook = entry["webhook"]
    entity = webhook["payload"][webhook["contains"][0]]["entity"]

    event = EventRecord(
        event_id=entry["event_id"],
        customer_id=customer["customer_id"],
        event_type=EventType(pipeline["event_type"]),
        decline_code=entity.get("error_reason"),
        amount=pipeline["amount_paise"] / 100,
        currency=entity.get("currency", "INR"),
        prior_attempts=pipeline["prior_attempts"],
        customer_history=CustomerHistory(
            tenure_days=customer["tenure_days"],
            past_failures=customer["past_failures"],
        ),
        detected_at=pipeline["detected_at"],
    )
    context = ProviderContext(
        error_code=entity.get("error_code") or None,
        error_description=entity.get("error_description") or None,
        error_source=entity.get("error_source"),
        error_step=entity.get("error_step"),
        payment_method=entity.get("method"),
    )
    return event, context


def evaluate(
    entries: list[dict[str, Any]], requests_per_minute: float = 5.0
) -> dict[str, Any]:
    """Classify each entry and score it against its ground-truth label.

    Paced to stay inside the free-tier quota, and cached across the batch. Without
    both, a run collapses into spurious escalations: an unpaced 30-event attempt
    produced 24 rate-limit failures and an apparent accuracy of 43%, which
    measured the quota rather than the prompt.
    """
    client = build_client()
    cache: dict[str, Any] = {}
    correct = 0
    latencies: list[float] = []
    confusion: Counter[tuple[str, str]] = Counter()
    per_category: dict[str, dict[str, int]] = {}
    escalations = 0
    unavailable = 0
    api_calls = 0
    rows: list[dict[str, Any]] = []
    min_gap = 60.0 / requests_per_minute if requests_per_minute > 0 else 0.0
    last_call_finished = 0.0

    for i, entry in enumerate(entries, start=1):
        event, context = _event_from_fixture(entry)
        expected = RootCause(entry["ground_truth"]["expected_root_cause"])

        cache_size_before = len(cache)
        # Only pace when the request will actually reach the API.
        if min_gap and last_call_finished:
            waited = time.monotonic() - last_call_finished
            if waited < min_gap:
                time.sleep(min_gap - waited)

        started = time.perf_counter()
        diagnosis = diagnose_root_cause(event, context, client=client, cache=cache)
        elapsed = (time.perf_counter() - started) * 1000
        if len(cache) > cache_size_before:
            api_calls += 1
            last_call_finished = time.monotonic()
            latencies.append(elapsed)
        if diagnosis.reasoning.startswith(CLASSIFIER_UNAVAILABLE_PREFIX):
            unavailable += 1
            api_calls += 1
            last_call_finished = time.monotonic()

        actual = diagnosis.root_cause
        bucket = per_category.setdefault(
            str(expected), {"total": 0, "correct": 0}
        )
        bucket["total"] += 1
        if actual is expected:
            bucket["correct"] += 1
            correct += 1
        else:
            confusion[(str(expected), str(actual))] += 1
        if actual is RootCause.UNKNOWN:
            escalations += 1

        rows.append(
            {
                "event_id": event.event_id,
                "event_type": str(event.event_type),
                "decline_code": event.decline_code,
                "expected": str(expected),
                "actual": str(actual),
                "confidence": round(diagnosis.confidence, 3),
                "correct": actual is expected,
                "reasoning": diagnosis.reasoning,
            }
        )
        print(
            f"  [{i}/{len(entries)}] {'OK ' if actual is expected else 'MISS'} "
            f"expected={expected} actual={actual} conf={diagnosis.confidence:.2f}",
            file=sys.stderr,
        )

    total = len(entries)
    unsafe = {
        f"{exp} -> {act}": n
        for (exp, act), n in confusion.items()
        if (RootCause(exp), RootCause(act)) in UNSAFE_CONFUSIONS
    }
    # Accuracy over events the classifier actually reached. The headline figure
    # is meaningless if a chunk of the batch never got classified.
    scorable = total - unavailable

    ordered = sorted(latencies)
    return {
        "prompt_version": PROMPT_VERSION,
        "model": get_settings().gemini_model,
        "confidence_threshold": get_settings().diagnose_confidence_threshold,
        "events": total,
        "api_calls": api_calls,
        "cache_hits": total - api_calls,
        "classifier_unavailable": unavailable,
        "scorable_events": scorable,
        "correct": correct,
        "accuracy_over_scorable": round(correct / scorable, 4) if scorable else 0.0,
        "accuracy_over_all_events": round(correct / total, 4) if total else 0.0,
        "escalated_to_unknown": escalations,
        "escalation_rate": round(escalations / total, 4) if total else 0.0,
        "per_category": {
            k: {
                **v,
                "accuracy": round(v["correct"] / v["total"], 4) if v["total"] else 0.0,
            }
            for k, v in sorted(per_category.items())
        },
        "confusions": {
            f"{exp} -> {act}": n
            for (exp, act), n in confusion.most_common()
        },
        "unsafe_confusions": unsafe,
        "latency_ms": {
            "mean": round(statistics.fmean(ordered), 1),
            "median": round(statistics.median(ordered), 1),
            "p95": round(ordered[min(int(len(ordered) * 0.95), len(ordered) - 1)], 1),
            "max": round(ordered[-1], 1),
        }
        if ordered
        else {},
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.diagnose_eval",
        description="Measure DIAGNOSE accuracy against ground-truth labels.",
    )
    parser.add_argument("--file", type=Path, help="Fixture to evaluate.")
    parser.add_argument("--limit", type=int, default=30,
                        help="Evaluate only the first N events (API quota).")
    parser.add_argument("--threshold", type=float, default=None,
                        help="Override the confidence floor for this run.")
    parser.add_argument("--out", type=Path, default=None,
                        help="Write the full report, including per-event rows, here.")
    parser.add_argument(
        "--model",
        default=None,
        help="Override GEMINI_MODEL for this run. Quota is per-model.",
    )
    parser.add_argument(
        "--rpm",
        type=float,
        default=5.0,
        help=(
            "Requests per minute to stay under. Free-tier gemini-2.5-flash allows "
            "5; exceeding it turns the run into rate-limit failures that look like "
            "escalations."
        ),
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.threshold is not None:
        object.__setattr__(settings, "diagnose_confidence_threshold", args.threshold)
    if args.model:
        object.__setattr__(settings, "gemini_model", args.model)

    path = args.file or _newest_fixture()
    fixture = load_fixture(path)
    entries = fixture["events"][: args.limit]
    print(f"evaluating {len(entries)} events from {path.name}", file=sys.stderr)

    try:
        report = evaluate(entries, requests_per_minute=args.rpm)
    except DiagnoseNotConfiguredError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    rows = report.pop("rows")
    print(json.dumps(report, indent=2))

    if args.out:
        args.out.write_text(
            json.dumps({**report, "rows": rows}, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\nfull report -> {args.out}", file=sys.stderr)

    if report["unsafe_confusions"]:
        print(
            "\nWARNING: unsafe confusions present. These could drive a retry "
            "against an instrument that should not be re-presented:",
            file=sys.stderr,
        )
        for pair, n in report["unsafe_confusions"].items():
            print(f"  {pair}: {n}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
