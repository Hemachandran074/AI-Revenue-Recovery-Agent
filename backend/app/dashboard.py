"""Phase 6 dashboard: one page showing the batch and every event's audit trail.

``ui-context.md`` scopes this tightly: a thin reporting layer, one page, internal
ops tool rather than product, "don't spend build time on visual polish here;
spend it on the pipeline". So this is server-rendered HTML with no build step, no
JavaScript framework, and no template-engine dependency.

**Deviation worth knowing about.** ``architecture.md`` and ``code-standards.md``
both list React for the frontend, but under *Suggested* stack, while
``ui-context.md`` says plainly that "a simple HTML page or a Streamlit/Gradio app
is enough". The more specific doc wins, and a Node toolchain for one static table
would be the kind of over-investment ui-context.md explicitly warns against.
Logged in progress-tracker.md.

## Design choices that are not arbitrary

**No JavaScript at all.** The event detail views are ``<details>`` elements
rendered server-side with the data already inside them. A demo that needs a fetch
to succeed before the audit trail appears has one more thing that can fail while
somebody is watching, and ``<details>`` is keyboard-accessible for free.

**Colour is never the only signal.** ``ui-context.md`` asks for green/red on
outcomes and a clear flag on violations. Every coloured element also carries text,
so the page still reads correctly in greyscale or to a screen reader.

**Everything is escaped.** Most of what renders here is our own data, but
DIAGNOSE's ``reasoning`` is model-generated text and provider error descriptions
are third-party strings. Both are treated as untrusted.
"""

from __future__ import annotations

from collections import defaultdict
from html import escape
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import metrics as metrics_module
from app.db import get_db
from app.models import AuditLogEntry
from app.schemas import Stage

router = APIRouter(tags=["dashboard"])

DEFAULT_LIMIT = 100

# Outcome and disposition rendering. The label is what a reader sees; the class
# only tints it, so removing the stylesheet loses nothing but colour.
DISPOSITION_LABELS: dict[str, tuple[str, str]] = {
    "contacted": ("Contacted", "good"),
    "retry_scheduled": ("Retry scheduled", "good"),
    "deferred_to_allowed_window": ("Deferred to allowed window", "neutral"),
    "withheld_by_guardrail": ("Withheld by guardrail", "neutral"),
    "escalated_to_human": ("Escalated to human", "neutral"),
    "classifier_unavailable": ("Classifier unavailable", "warn"),
    "dispatch_failed": ("Dispatch failed", "bad"),
    "not_processed": ("Not processed", "warn"),
    "skipped_other": ("Skipped", "neutral"),
}


@router.get("/api/metrics", summary="Batch metrics as JSON")
def api_metrics(
    session: Annotated[Session, Depends(get_db)], limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    """The same numbers the dashboard renders, for scripting or the write-up."""
    batch, _ = metrics_module.compute_batch_metrics(session, limit=_cap(limit))
    return batch.to_dict()


@router.get("/api/events", summary="Per-event pipeline results as JSON")
def api_events(
    session: Annotated[Session, Depends(get_db)], limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    """One entry per event with every stage's result joined on."""
    rows = metrics_module.load_rows(session, limit=_cap(limit))
    return {"count": len(rows), "events": [row.to_dict() for row in rows]}


@router.get(
    "/dashboard",
    response_class=HTMLResponse,
    summary="Single-page batch dashboard",
)
def dashboard(
    session: Annotated[Session, Depends(get_db)], limit: int = DEFAULT_LIMIT
) -> HTMLResponse:
    """Headline metrics, the batch table, and every event's audit trail."""
    capped = _cap(limit)
    batch, rows = metrics_module.compute_batch_metrics(session, limit=capped)
    trails = _trails_for(session, [row.event_id for row in rows])
    return HTMLResponse(render_page(batch, rows, trails))


def _cap(limit: int) -> int:
    """Bound the page size. A demo box should not try to render 10,000 rows."""
    return max(1, min(limit, 500))


def _trails_for(
    session: Session, event_ids: list[str]
) -> dict[str, list[AuditLogEntry]]:
    """Every audit entry for the shown events, in one query rather than N."""
    if not event_ids:
        return {}
    grouped: dict[str, list[AuditLogEntry]] = defaultdict(list)
    for entry in session.scalars(
        select(AuditLogEntry)
        .where(AuditLogEntry.event_id.in_(event_ids))
        .order_by(AuditLogEntry.id)
    ):
        grouped[entry.event_id].append(entry)
    return grouped


# --------------------------------------------------------------------- render


def render_page(
    batch: metrics_module.BatchMetrics,
    rows: list[metrics_module.EventRow],
    trails: dict[str, list[AuditLogEntry]],
) -> str:
    """The whole page. Plain string building keeps the dependency count at zero."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Revenue Recovery Agent - batch results</title>
<style>{_CSS}</style>
</head>
<body>
<header>
  <h1>AI Revenue Recovery Agent</h1>
  <p class="sub">Batch results and per-event audit trail. Test-mode demo data.</p>
</header>
<main>
{_render_headline(batch)}
{_render_violations(batch)}
{_render_breakdowns(batch)}
{_render_table(rows)}
{_render_details(rows, trails)}
</main>
<footer>
  <p>Amounts in {escape(batch.money.currency)}, stored as minor units and shown
  as major. <strong>Recovered stays 0 until a provider webhook confirms a
  payment</strong> &mdash; a delivered message is not a payment, and this page
  will not pretend otherwise.</p>
</footer>
</body>
</html>"""


def _render_headline(batch: metrics_module.BatchMetrics) -> str:
    money = batch.money
    violations = batch.violation_count
    # ui-context.md: make a nonzero violation count visually obvious. The word
    # carries the meaning; the colour only reinforces it.
    violation_class = "metric bad" if violations else "metric good"
    violation_note = (
        "STOPPING RULES BREACHED" if violations else "none, re-derived independently"
    )
    coverage = batch.audit
    coverage_class = (
        "metric good" if coverage.rate == 1.0 and coverage.events else "metric warn"
    )
    return f"""<section aria-labelledby="headline">
  <h2 id="headline">Headline</h2>
  <div class="metrics">
    <div class="metric">
      <span class="label">Recovered / at risk</span>
      <span class="value">{_money(money.recovered_minor)} / {_money(money.at_risk_minor)}</span>
      <span class="note">{_pct(money.recovery_rate)} of everything at risk</span>
    </div>
    <div class="metric">
      <span class="label">Of what was actioned</span>
      <span class="value">{_pct(money.actioned_recovery_rate)}</span>
      <span class="note">{_money(money.actioned_at_risk_minor)} had an action taken</span>
    </div>
    <div class="metric">
      <span class="label">Events processed</span>
      <span class="value">{batch.events_total}</span>
      <span class="note">{batch.classifier_unavailable} without a classifier</span>
    </div>
    <div class="metric">
      <span class="label">Decision latency</span>
      <span class="value">{_ms(batch.decision_latency.mean_ms)}</span>
      <span class="note">max {_ms(batch.decision_latency.max_ms)},
        {batch.decision_latency.over_budget} over the 60s target</span>
    </div>
    <div class="metric">
      <span class="label">Send latency</span>
      <span class="value">{_ms(batch.send_latency.mean_ms)}</span>
      <span class="note">max {_ms(batch.send_latency.max_ms)}; long is correct
        when a send was deferred</span>
    </div>
    <div class="{violation_class}">
      <span class="label">Guardrail violations</span>
      <span class="value">{violations}</span>
      <span class="note">{violation_note}</span>
    </div>
    <div class="{coverage_class}">
      <span class="label">Audit coverage</span>
      <span class="value">{_pct(coverage.rate)}</span>
      <span class="note">{coverage.fully_covered} of {coverage.events} events have
        all four stages</span>
    </div>
  </div>
  <p class="aside">Two latency figures rather than one: a quiet-hours deferral
  correctly delays a send by hours, so a single blended number would score
  compliance as failure. Decision latency is the figure held against the
  60-second target.</p>
</section>"""


def _render_violations(batch: metrics_module.BatchMetrics) -> str:
    if not batch.violations:
        return """<section aria-labelledby="violations">
  <h2 id="violations">Stopping-rule violations</h2>
  <p class="ok-banner"><strong>None.</strong> Each rule was reconstructed from raw
  event data and tested against what actually happened, rather than reading back
  the flags the guardrails wrote &mdash; otherwise the enforcing code would be
  grading itself.</p>
</section>"""
    items = "\n".join(
        f"    <li><code>{escape(v.rule)}</code> &mdash; "
        f'<a href="#event-{escape(v.event_id)}">{escape(v.event_id[:8])}</a>: '
        f"{escape(v.detail)}</li>"
        for v in batch.violations
    )
    return f"""<section aria-labelledby="violations">
  <h2 id="violations">Stopping-rule violations</h2>
  <p class="bad-banner"><strong>{batch.violation_count} violation(s) found.</strong>
  This must read zero. Each entry names the rule and what breached it.</p>
  <ul class="violations">
{items}
  </ul>
</section>"""


def _render_breakdowns(batch: metrics_module.BatchMetrics) -> str:
    return f"""<section aria-labelledby="breakdown">
  <h2 id="breakdown">Breakdown</h2>
  <p class="aside">Dispositions are mutually exclusive. Most of a batch is
  usually <em>correctly</em> not contacted, so these buckets keep restraint from
  being counted as failure.</p>
  <div class="cols">
    {_count_table("Disposition", batch.disposition)}
    {_count_table("Root cause", batch.by_root_cause)}
    {_count_table("Action", batch.by_action)}
    {_count_table("Delivery", batch.by_delivery_status)}
    {_count_table("Event type", batch.by_event_type)}
  </div>
</section>"""


def _count_table(title: str, counts: dict[str, int]) -> str:
    if not counts:
        return f"<div><h3>{escape(title)}</h3><p class='muted'>No data.</p></div>"
    body = "\n".join(
        f"<tr><th scope='row'>{escape(name)}</th><td>{count}</td></tr>"
        for name, count in counts.items()
    )
    return f"""<div>
      <h3>{escape(title)}</h3>
      <table class="counts">
        <caption class="sr-only">{escape(title)} counts</caption>
        <tbody>
{body}
        </tbody>
      </table>
    </div>"""


def _render_table(rows: list[metrics_module.EventRow]) -> str:
    if not rows:
        return """<section aria-labelledby="batch">
  <h2 id="batch">Batch</h2>
  <p class="muted">No events yet. Replay a batch with
  <code>python -m app.replay</code>.</p>
</section>"""
    body = "\n".join(_render_row(row) for row in rows)
    return f"""<section aria-labelledby="batch">
  <h2 id="batch">Batch <span class="muted">({len(rows)} events)</span></h2>
  <table class="batch">
    <caption class="sr-only">One row per event with its pipeline result</caption>
    <thead>
      <tr>
        <th scope="col">Event</th>
        <th scope="col">Type</th>
        <th scope="col">Root cause</th>
        <th scope="col">Action</th>
        <th scope="col">Channel</th>
        <th scope="col">Outcome</th>
        <th scope="col" class="num">Amount</th>
        <th scope="col" class="num">Send latency</th>
        <th scope="col">Trail</th>
      </tr>
    </thead>
    <tbody>
{body}
    </tbody>
  </table>
</section>"""


def _render_row(row: metrics_module.EventRow) -> str:
    label, tone = DISPOSITION_LABELS.get(
        row.disposition, (row.disposition.replace("_", " ").title(), "neutral")
    )
    stage_count = len(row.stages)
    trail_ok = "4/4" if stage_count >= 4 else f"{stage_count}/4"
    trail_class = "" if stage_count >= 4 else "warn-text"
    return f"""      <tr>
        <th scope="row"><code>{escape(row.event_id[:8])}</code></th>
        <td>{escape(row.event_type)}</td>
        <td>{escape(row.root_cause or "-")}</td>
        <td>{escape(row.action or "-")}</td>
        <td>{escape(row.channel or "-")}</td>
        <td><span class="pill {tone}">{escape(label)}</span></td>
        <td class="num">{_money(row.amount_minor)}</td>
        <td class="num">{_ms(row.send_latency_ms)}</td>
        <td class="{trail_class}"><a href="#event-{escape(row.event_id)}">{trail_ok}</a></td>
      </tr>"""


def _render_details(
    rows: list[metrics_module.EventRow], trails: dict[str, list[AuditLogEntry]]
) -> str:
    if not rows:
        return ""
    blocks = "\n".join(
        _render_detail(row, trails.get(row.event_id, [])) for row in rows
    )
    return f"""<section aria-labelledby="trails">
  <h2 id="trails">Audit trails</h2>
  <p class="aside">The bar from <code>project-overview.md</code>: a stranger reads
  one of these and understands what happened and why in under 30 seconds.</p>
{blocks}
</section>"""


def _render_detail(row: metrics_module.EventRow, trail: list[AuditLogEntry]) -> str:
    label, tone = DISPOSITION_LABELS.get(row.disposition, (row.disposition, "neutral"))
    stages = "\n".join(_render_stage(entry) for entry in trail) or (
        "<p class='warn-text'>No audit entries for this event.</p>"
    )
    return f"""  <details id="event-{escape(row.event_id)}" class="trail">
    <summary>
      <code>{escape(row.event_id[:8])}</code>
      <span class="pill {tone}">{escape(label)}</span>
      <span class="muted">{escape(row.event_type)}
        &middot; {escape(row.root_cause or "no diagnosis")}
        &middot; {escape(row.action or "no decision")}
        &middot; {_money(row.amount_minor)}</span>
    </summary>
    <dl class="facts">
      <dt>Event id</dt><dd><code>{escape(row.event_id)}</code></dd>
      <dt>Customer</dt><dd><code>{escape(row.customer_id)}</code></dd>
      <dt>Decline code</dt><dd>{escape(row.decline_code or "-")}</dd>
      <dt>Prior attempts</dt><dd>{row.prior_attempts}</dd>
      <dt>Root cause</dt><dd>{_diagnosis(row)}</dd>
      <dt>Why</dt><dd>{escape(row.reasoning or "-")}</dd>
      <dt>Decision latency</dt><dd>{_ms(row.decision_latency_ms)}</dd>
      <dt>Send latency</dt><dd>{_ms(row.send_latency_ms)}</dd>
      <dt>Amount recovered</dt><dd>{_recovered(row)}</dd>
    </dl>
{stages}
{_render_guardrails(row)}
  </details>"""


def _render_stage(entry: AuditLogEntry) -> str:
    notes = (
        f"<p class='notes'><strong>Note:</strong> {escape(entry.notes)}</p>"
        if entry.notes
        else ""
    )
    when = entry.timestamp.isoformat() if entry.timestamp else "-"
    return f"""    <div class="stage">
      <h4>{escape(entry.stage.upper())} <span class="muted">{escape(when)}</span></h4>
      {notes}
      <div class="cols">
        <div><h5>Input</h5>{_kv(entry.input_summary)}</div>
        <div><h5>Output</h5>{_kv(entry.output_summary)}</div>
      </div>
    </div>"""


def _render_guardrails(row: metrics_module.EventRow) -> str:
    """All four checks, pass and fail alike. Constraint #5 made visible."""
    if not row.guardrail_checks:
        return ""
    check_rows: list[str] = []
    for check in row.guardrail_checks:
        passed = bool(check.get("passed"))
        tone = "good" if passed else "bad"
        verdict = "PASS" if passed else "FAIL"
        name = escape(str(check.get("name", "?")))
        detail = escape(str(check.get("detail", "")))
        check_rows.append(
            f"<tr><th scope='row'>{name}</th>"
            f"<td><span class='pill {tone}'>{verdict}</span></td>"
            f"<td>{detail}</td></tr>"
        )
    body = "\n".join(check_rows)
    blocked = (
        f"<p class='notes'><strong>Blocked:</strong> "
        f"{escape(row.blocked_reason)}</p>"
        if row.blocked_reason
        else "<p class='muted'>No guardrail cancelled this action.</p>"
    )
    return f"""    <div class="stage">
      <h4>GUARDRAIL CHECKS</h4>
      {blocked}
      <table class="checks">
        <caption class="sr-only">Every guardrail result for this event</caption>
        <thead><tr><th scope="col">Rule</th><th scope="col">Result</th>
          <th scope="col">Detail</th></tr></thead>
        <tbody>
{body}
        </tbody>
      </table>
    </div>"""


def _kv(summary: dict[str, Any] | None) -> str:
    if not summary:
        return "<p class='muted'>empty</p>"
    rows = "\n".join(
        f"<tr><th scope='row'>{escape(str(key))}</th>"
        f"<td>{escape(_short(value))}</td></tr>"
        for key, value in summary.items()
    )
    return f"<table class='kv'><tbody>\n{rows}\n</tbody></table>"


def _short(value: Any, limit: int = 400) -> str:
    text = "-" if value is None else str(value)
    return text if len(text) <= limit else f"{text[:limit]}..."


def _money(minor: int | None) -> str:
    return "-" if minor is None else f"{minor / 100:,.2f}"


def _diagnosis(row: metrics_module.EventRow) -> str:
    """Root cause with its confidence, and a flag when no classifier ran.

    ui-context.md asks the detail view to show root cause, confidence and the
    one-line reasoning. The outage flag matters as much as the label: an
    `unknown` nobody looked at is a different thing from one that was judged.
    """
    if row.root_cause is None:
        return "not diagnosed"
    label = escape(row.root_cause)
    if row.confidence is not None:
        label += f" <span class='muted'>(confidence {row.confidence:.2f})</span>"
    if row.classifier_unavailable:
        label += " <span class='pill warn'>classifier unavailable</span>"
    return label


def _recovered(row: metrics_module.EventRow) -> str:
    if row.amount_recovered_minor is None:
        return "not confirmed (awaiting a provider webhook)"
    return _money(row.amount_recovered_minor)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.0f} ms"


# Deliberately plain. ui-context.md: internal ops tool, not a consumer product.
# Contrast ratios on the tinted pills are kept above 4.5:1 against their
# backgrounds so the text stays readable, and every pill states its meaning in
# words as well as colour.
_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; font: 15px/1.5 -apple-system, Segoe UI, Roboto, sans-serif;
  color: #16191d; background: #f6f7f9; }
header { padding: 20px 24px; background: #16191d; color: #fff; }
header h1 { margin: 0 0 4px; font-size: 20px; }
header .sub { margin: 0; color: #b9c0c8; font-size: 13px; }
main { padding: 24px; max-width: 1500px; margin: 0 auto; }
section { margin-bottom: 32px; }
h2 { font-size: 17px; border-bottom: 2px solid #d7dbe0; padding-bottom: 6px; }
h3 { font-size: 13px; text-transform: uppercase; letter-spacing: .04em;
  color: #55606c; margin: 0 0 6px; }
h4 { font-size: 13px; margin: 0 0 8px; letter-spacing: .04em; }
h5 { font-size: 12px; text-transform: uppercase; color: #55606c; margin: 0 0 4px; }
.metrics { display: grid; gap: 12px;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); }
.metric { background: #fff; border: 1px solid #d7dbe0; border-left: 4px solid #7a8592;
  border-radius: 6px; padding: 12px 14px; }
.metric.good { border-left-color: #1d7a45; }
.metric.bad { border-left-color: #b3261e; background: #fdf2f1; }
.metric.warn { border-left-color: #8a6100; background: #fdf8ef; }
.metric .label { display: block; font-size: 12px; text-transform: uppercase;
  letter-spacing: .04em; color: #55606c; }
.metric .value { display: block; font-size: 22px; font-weight: 650; margin: 4px 0; }
.metric .note { display: block; font-size: 12px; color: #55606c; }
.aside { font-size: 13px; color: #48525d; background: #eef1f4;
  border-left: 3px solid #a7b0ba; padding: 8px 12px; border-radius: 0 4px 4px 0; }
.ok-banner { background: #edf7f0; border-left: 4px solid #1d7a45;
  padding: 10px 14px; border-radius: 0 4px 4px 0; font-size: 14px; }
.bad-banner { background: #fdf2f1; border-left: 4px solid #b3261e;
  padding: 10px 14px; border-radius: 0 4px 4px 0; font-size: 14px; }
table { border-collapse: collapse; width: 100%; background: #fff; }
caption { text-align: left; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #e4e7eb;
  vertical-align: top; font-size: 13px; }
thead th { background: #eef1f4; font-size: 12px; text-transform: uppercase;
  letter-spacing: .03em; position: sticky; top: 0; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.batch tbody tr:hover { background: #f0f4f8; }
.counts, .kv, .checks { border: 1px solid #e4e7eb; border-radius: 4px; }
.kv th { width: 42%; color: #48525d; font-weight: 500; }
.cols { display: grid; gap: 16px;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
.pill { display: inline-block; padding: 1px 8px; border-radius: 10px;
  font-size: 12px; font-weight: 600; border: 1px solid; }
.pill.good { background: #e6f4ea; color: #14532d; border-color: #1d7a45; }
.pill.bad { background: #fdecea; color: #7f1d1d; border-color: #b3261e; }
.pill.warn { background: #fdf3e3; color: #6b4500; border-color: #8a6100; }
.pill.neutral { background: #eef1f4; color: #35404b; border-color: #97a1ab; }
.trail { background: #fff; border: 1px solid #d7dbe0; border-radius: 6px;
  margin-bottom: 8px; padding: 10px 14px; }
.trail summary { cursor: pointer; display: flex; gap: 10px; flex-wrap: wrap;
  align-items: center; }
.trail[open] summary { margin-bottom: 12px; border-bottom: 1px solid #e4e7eb;
  padding-bottom: 8px; }
.stage { border: 1px solid #e4e7eb; border-radius: 4px; padding: 10px 12px;
  margin-bottom: 10px; background: #fbfcfd; }
.facts { display: grid; grid-template-columns: max-content 1fr; gap: 2px 14px;
  margin: 0 0 12px; font-size: 13px; }
.facts dt { color: #55606c; }
.facts dd { margin: 0; }
.notes { font-size: 13px; background: #fdf8ef; border-left: 3px solid #8a6100;
  padding: 6px 10px; margin: 0 0 8px; }
.violations { font-size: 13px; }
.muted { color: #55606c; font-size: 12px; font-weight: 400; }
.warn-text { color: #6b4500; font-weight: 600; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
footer { padding: 16px 24px; color: #48525d; font-size: 13px;
  border-top: 1px solid #d7dbe0; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
@media (prefers-color-scheme: dark) {
  body { background: #14171a; color: #e6e9ec; }
  .metric, table, .trail { background: #1d2126; border-color: #333a42; }
  .stage { background: #191d21; border-color: #333a42; }
  thead th { background: #24292f; }
  .aside { background: #22272c; color: #c3cad1; }
  h2 { border-color: #333a42; }
  .metric .label, .metric .note, .muted, .facts dt, h3, h5 { color: #a8b1ba; }
  .batch tbody tr:hover { background: #24292f; }
  .metric.bad, .bad-banner { background: #2a1d1d; }
  .metric.warn, .notes { background: #2a2419; }
  .ok-banner { background: #172619; }
}
"""


__all__ = ["router", "render_page", "Stage"]
