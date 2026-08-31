"""Replay a fixture batch through the live signed webhook endpoint.

    python -m app.replay                          # replay the newest fixture
    python -m app.replay --generate --count 75    # fresh batch, current clock
    python -m app.replay --file fixtures/x.json
    python -m app.replay --limit 5                # smoke test

Why this exists rather than calling ``detect_event`` in a loop: the batch has to
exercise the SAME path a live Razorpay delivery takes, signature verification
included. Calling the stage directly would leave the verification, routing and
raw-body handling untested by the batch run, and those are exactly the parts most
likely to break in production.

Requires the server to be running and ``RAZORPAY_WEBHOOK_SECRET`` to be set,
because it signs with the same secret the server verifies against:

    python -m uvicorn app.main:app
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

import httpx

from app.config import get_settings
from app.simulation.fixtures import DEFAULT_FIXTURE_DIR, load_fixture, webhooks_only
from app.simulation.generator import generate_batch
from app.simulation.signing import signed_delivery

DEFAULT_ENDPOINT = "http://127.0.0.1:8000/webhooks/razorpay"


def _newest_fixture() -> Path:
    candidates = sorted(
        DEFAULT_FIXTURE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime
    )
    if not candidates:
        raise SystemExit(
            f"No fixtures in {DEFAULT_FIXTURE_DIR}. Generate one first:\n"
            "  python -m app.simulation"
        )
    return candidates[-1]


def replay(
    envelopes: list[dict[str, Any]],
    endpoint: str,
    secret: str,
    # Generous on purpose. With the pipeline running inline, one request covers a
    # classifier round trip, and DIAGNOSE deliberately backs off and retries when
    # the free tier rate-limits it. At 10s the client gave up mid-backoff and
    # reported transport_error for requests the server went on to answer 200,
    # which reads as a broken endpoint rather than an exhausted quota.
    timeout: float = 45.0,
) -> dict[str, Any]:
    """POST each envelope, signed, and collect outcomes and latencies."""
    outcomes: Counter[str] = Counter()
    latencies_ms: list[float] = []
    failures: list[dict[str, Any]] = []

    with httpx.Client(timeout=timeout) as client:
        for envelope in envelopes:
            body, headers = signed_delivery(envelope, secret)
            started = time.perf_counter()
            try:
                response = client.post(endpoint, content=body, headers=headers)
            except httpx.HTTPError as exc:
                outcomes["transport_error"] += 1
                failures.append({"error": str(exc)})
                continue
            elapsed_ms = (time.perf_counter() - started) * 1000
            latencies_ms.append(elapsed_ms)

            try:
                payload = response.json()
            except ValueError:
                payload = {"status": f"non_json_{response.status_code}"}

            status = payload.get("status", f"http_{response.status_code}")
            outcomes[status] += 1
            if response.status_code >= 400:
                failures.append(
                    {
                        "http_status": response.status_code,
                        "body": payload,
                        "payment_id": envelope.get("payload", {})
                        .get("payment", {})
                        .get("entity", {})
                        .get("id"),
                    }
                )

    summary: dict[str, Any] = {
        "endpoint": endpoint,
        "events_sent": len(envelopes),
        "outcomes": dict(outcomes),
        "failures": failures[:10],
    }
    if latencies_ms:
        ordered = sorted(latencies_ms)
        summary["latency_ms"] = {
            "mean": round(statistics.fmean(ordered), 2),
            "median": round(statistics.median(ordered), 2),
            "p95": round(ordered[min(int(len(ordered) * 0.95), len(ordered) - 1)], 2),
            "max": round(ordered[-1], 2),
        }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.replay",
        description="Replay a batch through the signed webhook endpoint.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--file", type=Path, help="Fixture file to replay.")
    source.add_argument(
        "--generate",
        action="store_true",
        help="Generate a fresh batch instead of reading a fixture.",
    )
    parser.add_argument("--count", type=int, default=75, help="Events when generating.")
    parser.add_argument("--seed", type=int, default=42, help="Seed when generating.")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--limit", type=int, default=None, help="Send only the first N.")
    parser.add_argument(
        "--timeout",
        type=float,
        default=45.0,
        help="Per-request timeout in seconds. Must exceed the DIAGNOSE backoff.",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    secret = settings.razorpay_webhook_secret
    if not secret:
        print(
            "RAZORPAY_WEBHOOK_SECRET is not set. The server verifies against it, so "
            "replaying without it would only prove that rejection works.",
            file=sys.stderr,
        )
        return 2

    if args.generate:
        batch = generate_batch(seed=args.seed, count=args.count)
        envelopes = [e.envelope for e in batch.events]
        origin = f"generated (seed={args.seed}, n={args.count})"
    else:
        path = args.file or _newest_fixture()
        envelopes = webhooks_only(load_fixture(path))
        origin = str(path)

    if args.limit is not None:
        envelopes = envelopes[: args.limit]

    print(f"replaying {len(envelopes)} events from {origin}", file=sys.stderr)
    summary = replay(envelopes, args.endpoint, secret, timeout=args.timeout)
    print(json.dumps(summary, indent=2))

    rejected = sum(
        count
        for status, count in summary["outcomes"].items()
        if status in {"rejected", "transport_error"} or status.startswith("http_")
    )
    if rejected:
        print(f"\n{rejected} event(s) were rejected", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
