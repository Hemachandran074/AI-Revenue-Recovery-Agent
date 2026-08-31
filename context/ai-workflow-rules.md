# AI Workflow Rules — how to work on this project

These rules govern how an AI coding assistant (Claude Code, Cursor, etc.)
should behave when working on this repo. Read this file first, every session.

## Session start checklist
1. Read `project-overview.md` to confirm current scope (v1 only, unless
   `progress-tracker.md` says stretch goals are unlocked).
2. Read `progress-tracker.md` to see what's done, what's in progress, what's next.
3. Read `architecture.md` before touching DIAGNOSE or DECIDE — do not
   improvise the schema or the action set.
4. Read `code-standards.md` before writing or editing any file.
5. State which stage (DETECT/DIAGNOSE/DECIDE/EXECUTE) and which single task
   you're working on before writing code. One task at a time.

## Build order — do not skip ahead
1. Data simulation layer (synthetic events + Stripe test-mode triggers)
2. DETECT (webhook receiver, event normalization)
3. DIAGNOSE (LLM classification against fixed taxonomy)
4. DECIDE (deterministic rules engine + guardrails)
5. EXECUTE (action dispatch, stubbed channels first, real APIs after)
6. Audit log + metrics dashboard
7. Stretch goals — only after step 6 is fully working on a real batch

If asked to build something out of this order, flag it and ask for
confirmation before proceeding — don't silently comply.

## Scope discipline
- Do not add a new "direction" (checkout recovery, B2B chaser, etc.) without
  it being explicitly added to `project-overview.md` first.
- Do not add a new action to the DECIDE action set without updating
  `architecture.md`'s table first. Code and docs must never drift apart.
- If a request would violate a Non-Negotiable Constraint in `architecture.md`
  (e.g., "just auto-fill the card details"), say so plainly and propose the
  compliant alternative instead of quietly building the risky version.

## When implementing DIAGNOSE (the LLM stage)
- Root cause must be one of the fixed enum values — never a free-text category.
- Always log the model's one-sentence reasoning alongside the classification.
- If confidence is low, route to `unknown` → `escalate_to_human_review`,
  don't force a guess into a specific category.

## When implementing DECIDE
- This stage must be deterministic code (a lookup table / rules function),
  not an LLM call. If you're tempted to "just ask the LLM what to do here,"
  stop — re-read `architecture.md`'s action-set table instead.
- Every guardrail check (max retries, quiet hours, contact frequency) must
  run and log its result even when it passes.

## Testing expectations
- Every stage needs a unit test with at least one example per taxonomy
  category / action type before moving to the next stage.
- Before declaring v1 "done," run the full batch (50–100 events) through the
  pipeline end to end and confirm the four `Definition of done` checkboxes
  in `project-overview.md`.

## Progress tracking
- After completing any task, update `progress-tracker.md` — don't let it go
  stale. This is the source of truth for "what's actually built" vs. "what's
  planned."
- If a task turns out bigger than expected, split it and log the split,
  rather than silently expanding scope.

## Communication style for this project
- Be explicit about trade-offs (e.g., "SQLite is fine for the demo batch,
  but won't hold up for concurrent webhooks in production").
- Flag anything that looks like it would require real customer PCI-scope
  handling, real production payment credentials, or real customer contact —
  this project only runs against test-mode data.
- Prefer small, reviewable diffs over large rewrites.
