# AI Revenue Recovery Agent

Detects revenue at risk in real time, diagnoses the root cause, and executes a
bounded, compliant recovery workflow — then proves how much money it recovered.

**Test-mode / demo build only.** No production payment credentials, ever.

The `context/` directory is the source of truth for scope, architecture, and
working rules. Read `context/ai-workflow-rules.md` first. This README is a
navigation aid, not a substitute for those docs.

## Status

`Phase 0 — Setup`. Scaffold only: no pipeline stage is implemented yet. The four
stage modules raise `NotImplementedError` on purpose so nothing returns a
plausible-looking fake result. See `context/progress-tracker.md`.

## Layout

```
context/                 Source-of-truth docs (scope, architecture, standards, progress)
backend/
  app/
    main.py              FastAPI entrypoint: /health, /readiness
    config.py            Env loading, test-mode key enforcement, readiness report
    schemas.py           Pydantic models + fixed taxonomy, mirroring architecture.md
    detect.py            Stage 1 — DETECT    (Phase 2, stub)
    diagnose.py          Stage 2 — DIAGNOSE  (Phase 3, stub)
    decide.py            Stage 3 — DECIDE    (Phase 4, stub)
    execute.py           Stage 4 — EXECUTE   (Phase 5, stub)
    guardrails.py        Stopping rules      (Phase 4, stub)
  prompts/diagnose.md    Versioned DIAGNOSE system prompt (placeholder)
  tests/                 pytest suite
.env.example             Every required key, placeholder values only
```

One module per pipeline stage, per `context/code-standards.md`. No stage's logic
lives in another stage's file.

## Setup

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item ..\.env.example ..\.env    # then fill in real test-mode keys
```

Start the database (Docker Desktop must be running):

```powershell
docker compose up -d --wait
```

`--wait` blocks until the healthcheck passes, so a clean return means the
database actually accepts connections.

If the bind fails with "socket in a way forbidden by its access permissions",
a native Postgres already owns the host port. Change `POSTGRES_PORT` in `.env`;
the app derives its connection URL from that same value.

Run the tests and linter:

```powershell
.\.venv\Scripts\python.exe -m pytest                  # skips DB tests if no container
.\.venv\Scripts\python.exe -m pytest -m integration   # DB tests only
.\.venv\Scripts\python.exe -m ruff check app tests
```

Run the API, then replay a batch through the signed webhook endpoint:

```powershell
.\.venv\Scripts\python.exe -c "from app.db import init_db; init_db()"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
# in another shell:
.\.venv\Scripts\python.exe -m app.replay
```

Use `--reload`, or edits to the pipeline will not be picked up and a replay will
silently exercise stale code.

The replay signs every payload with `RAZORPAY_WEBHOOK_SECRET` and posts to the
same endpoint a live Razorpay delivery hits, so the batch exercises signature
verification rather than bypassing it.

## Exposing the webhook publicly (live Razorpay test-mode failures)

Razorpay will not accept a `localhost` webhook URL, so a tunnel is required.

Expose `app.tunnel`, **not** `app.main`. `app.main` also serves `/health`,
`/readiness` and the audit endpoint, none of which have authentication;
`app.tunnel` contains only the signature-verified webhook route plus a
contentless liveness probe, so there is nothing on it to leak.

```powershell
# once per machine
ngrok config add-authtoken <token-from-ngrok-dashboard>

# shell 1 — public webhook intake
.\.venv\Scripts\python.exe -m uvicorn app.tunnel:app --port 8001 --reload

# shell 2 — the tunnel
ngrok http 8001

# shell 3 — ops endpoints, loopback only, never tunnelled
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000 --reload
```

Register the forwarding host plus the path in the Razorpay dashboard under
Account & Settings -> Webhooks, subscribed to `payment.failed`. This project's
assigned static dev domain:

```
https://prospectless-carlotta-unboding.ngrok-free.dev/webhooks/razorpay
```

Note the suffix is `.ngrok-free.dev`. Don't assume `.app`; copy whatever the agent
prints.

The webhook secret entered there must be the same string as
`RAZORPAY_WEBHOOK_SECRET` in `.env`. Razorpay does not generate it; you choose it.

The free plan assigns a static dev domain, so the URL survives agent restarts and
only needs registering once. Pin it explicitly with:

```powershell
ngrok http 8001 --url https://prospectless-carlotta-unboding.ngrok-free.dev
```

Verify the tunnel is wired correctly before relying on it. Only the first of these
should succeed:

```powershell
curl https://prospectless-carlotta-unboding.ngrok-free.dev/tunnel-health   # 200
curl https://prospectless-carlotta-unboding.ngrok-free.dev/readiness       # 404
```

A 200 from `/readiness` means the tunnel is pointed at port 8000 (`app.main`) and
is publishing your unauthenticated ops endpoints. Repoint it at 8001.

### Triggering a genuine provider failure

Create the Payment Link through the API, not the dashboard:

```powershell
.\.venv\Scripts\python.exe -m app.trigger_failure --amount 499
.\.venv\Scripts\python.exe -m app.trigger_failure --list
```

Creating it in the dashboard depends on the Test/Live toggle being right, and a
link created in one mode cannot be paid against the other. That mismatch shows up
as a misleading Razorpay error:

```
"The id provided does not exist"   step: payment_initiation
```

Nothing is wrong with the code when that appears — the link simply does not exist
in the account the keys authenticate against. Building it from the same keys the
project uses removes the possibility.

Open the printed URL and force a failure. Confirmed to work by live capture:

- **Netbanking or Wallet** — pick any provider, then choose Failure, or simply
  cancel on the mock page. Both emit `payment.failed` with
  `error_reason=payment_cancelled`.
- **UPI** — use `failure@razorpay`. Do **not** cancel a UPI payment; for UPI
  specifically Razorpay records a cancellation as a success.

If you complete the payment successfully, no `payment.failed` fires and nothing
reaches the pipeline. Create a fresh link and fail that one; a paid link cannot be
reused.

Replay through the public URL to confirm the whole path end to end:

```powershell
.\.venv\Scripts\python.exe -m app.replay --endpoint `
  https://prospectless-carlotta-unboding.ngrok-free.dev/webhooks/razorpay
```

Generate a synthetic batch:

```powershell
.\.venv\Scripts\python.exe -m app.simulation                    # 75 events -> fixtures/
.\.venv\Scripts\python.exe -m app.simulation --summary-only     # inspect, write nothing
.\.venv\Scripts\python.exe -m app.simulation --count 100
```

Regenerate before a demo run. Event timestamps are relative to generation time,
and a batch older than 7 days trips the hard-stop guardrail on every event, which
would report zero recovery from a perfectly working pipeline. Use `--now` to pin
the clock when a byte-stable fixture is wanted.

Run the API:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

`GET /readiness` lists which credentials are still missing, by key name. It
never returns a secret value.

## Two things the code enforces, not just documents

**Test-mode keys only.** `config.py` raises at startup on a `sk_live_` or
`rzp_live_` key. Production payment credentials cannot be loaded.

**No drift between code and docs.** `tests/test_schema_contract.py` parses
`context/architecture.md` and fails if the enums or the 8-row action table stop
matching the code. To add a root cause or action, edit `architecture.md` first.

## Security note

This service has no authentication layer. That is a deliberate choice for a
local demo tool (`context/ui-context.md`), which is why it binds to `127.0.0.1`
by default. Do not expose it to a network or deploy it without adding auth.
Provider webhook endpoints arrive in Phase 2 and require signature verification
before any payload enters DETECT.
