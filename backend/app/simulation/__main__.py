"""CLI for generating batch fixtures.

    python -m app.simulation                       # seed 42, 75 events
    python -m app.simulation --count 100           # bigger batch
    python -m app.simulation --seed 7 --count 50   # different draw
    python -m app.simulation --summary-only        # inspect without writing

Run from the ``backend`` directory so ``app`` is importable.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from app.simulation.fixtures import write_fixture
from app.simulation.generator import generate_batch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.simulation",
        description="Generate synthetic Razorpay payment.failed fixtures.",
    )
    parser.add_argument("--seed", type=int, default=42,
                        help="RNG seed. Same seed gives an identical batch.")
    parser.add_argument("--count", type=int, default=75,
                        help="Number of events (project scope is 50-100).")
    parser.add_argument("--window-days", type=int, default=14,
                        help="Spread events across this many days.")
    parser.add_argument("--blocked-share", type=float, default=0.18,
                        help="Fraction nudged toward failing a stopping rule.")
    parser.add_argument("--summary-only", action="store_true",
                        help="Print the summary without writing a file.")
    parser.add_argument(
        "--now",
        type=str,
        default=None,
        metavar="ISO8601",
        help=(
            "Pin the reference time (e.g. 2026-08-30T12:00:00+00:00) for a "
            "byte-stable fixture. Defaults to the current time, which is what a "
            "demo wants: a batch older than 7 days trips the hard-stop guardrail "
            "on every event and recovers nothing."
        ),
    )
    args = parser.parse_args(argv)

    pinned_now = None
    if args.now:
        pinned_now = datetime.fromisoformat(args.now)
        if pinned_now.tzinfo is None:
            parser.error("--now must include a UTC offset, e.g. ...T12:00:00+00:00")

    if not 50 <= args.count <= 100:
        print(
            f"note: count={args.count} is outside the 50-100 range "
            "project-overview.md scopes for the demo batch.",
            file=sys.stderr,
        )

    batch = generate_batch(
        seed=args.seed,
        count=args.count,
        window_days=args.window_days,
        blocked_share=args.blocked_share,
        now=pinned_now,
    )

    print(json.dumps(batch.summary(), indent=2))

    if args.summary_only:
        return 0

    path = write_fixture(batch)
    print(f"\nwrote {len(batch.events)} events -> {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
