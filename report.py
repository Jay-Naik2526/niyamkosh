"""A compliance report an officer can attach to a file noting.

The web console answers questions. A file noting needs a document: something
dated, printable, and specific about where each statement came from. This
renders one from the same `audit_tender()` result the screen draws, so the two
can never disagree — the report is a second view of one audit, not a second
audit.

What it deliberately does not do is certify anything. It lists what the register
holds about the standards this tender cites, on the date the register was last
checked against BIS, and says so in its own footer. A report that read as an
approval would be the most damaging thing this project could produce.

No dependencies and no template engine: one function, inline CSS, print-ready.
"""

import datetime
import html

PALETTE = {"petrol": "#08303F", "amber": "#C25A0D"}

SEVERITY_LABEL = {
    "high": "Blocking",
    "medium": "Review",
    "low": "Note",
}


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def _rows(cells: list[str]) -> str:
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def _status_cell(finding: dict) -> str:
    """What the register says about this citation.

    Only a not_in_register finding means the register does not hold it. Every
    other kind may simply carry no status field — a certification duty says
    nothing about supersession — and reading an absent field as "not in the
    register" reported IS 694, a current standard with a live QCO, as missing.
    A report that mislabels a held standard as absent is worse than one that
    says nothing, so silence is the default."""
    if finding.get("kind") == "not_in_register":
        return '<span class="mute">not in the register</span>'
    status = finding.get("status") or ""
    if status in ("Withdrawn", "Superseded"):
        return f'<span class="bad">{_e(status)}</span>'
    if status:
        return f'<span class="ok">{_e(status)}</span>'
    return '<span class="ok">Current</span>'


def render(audit: dict, bis_check: dict | None = None) -> str:
    """One audit result, as a printable document."""
    today = datetime.date.today().strftime("%d %B %Y")
    bis = bis_check or {}
    suggestions = audit.get("suggestions") or {}
    findings = audit.get("findings") or []
    counts = audit.get("counts") or {}

    # Citations, worst first, each with what the register says and where that
    # came from. The order matches the screen's.
    by_number: dict[str, dict] = {}
    for f in findings:
        number = f.get("is_number")
        if number and number not in by_number:
            by_number[number] = f

    citation_rows = []
    for number in audit.get("cited") or []:
        f = by_number.get(number)
        if f is None:
            citation_rows.append(_rows([
                f'<span class="mono">{_e(number)}</span>',
                '<span class="ok">Current</span>',
                '<span class="mute">—</span>',
                '<span class="mute">no finding</span>',
            ]))
            continue
        replaced = f.get("replaced_by")
        replaced = ("" if replaced in (None, "", "UNKNOWN") else replaced)
        # A successor is only meaningful for a standard that has been
        # superseded or withdrawn. Printing "no successor recorded" against a
        # current standard reads as a defect in the standard rather than as the
        # column not applying.
        dead = (f.get("status") in ("Withdrawn", "Superseded"))
        if replaced:
            successor = f'<span class="mono">{_e(replaced)}</span>'
        elif dead:
            successor = '<span class="mute">no successor recorded</span>'
        else:
            successor = '<span class="mute">—</span>'
        citation_rows.append(_rows([
            f'<span class="mono">{_e(number)}</span>',
            _status_cell(f),
            successor,
            _e(f.get("headline") or ""),
        ]))

    def block(title: str, items: list[str]) -> str:
        if not items:
            return ""
        return (f'<h2>{_e(title)}</h2><ol class="edits">'
                + "".join(f"<li>{i}</li>" for i in items) + "</ol>")

    replace_items = [
        f'<span class="mono">{_e(r.get("cite"))}</span> — {_e(r.get("status"))}. '
        + (f'Replace with <span class="mono">{_e(r.get("with"))}</span>. '
           if r.get("with") else "No successor is recorded; confirm with BIS. ")
        + _e(r.get("why") or "")
        # The BIS deep link is a 400-character encrypted id. Printed inline it
        # swamps the sentence it belongs to, so it stays a link with a short
        # label — clickable in the PDF, out of the way on paper.
        + (f' <a class="src" href="{_e((r.get("evidence") or {}).get("link"))}">BIS record</a>'
           if (r.get("evidence") or {}).get("link") else "")
        for r in suggestions.get("replace") or []
    ]

    clause_items = [
        f'<span class="mono">{_e(r.get("for"))}</span> — mandatory certification under '
        f'{_e(r.get("scheme") or "a BIS scheme")}'
        + (f' ({_e(r.get("notification"))})' if r.get("notification")
           and str(r.get("notification")) not in ("N/A", "None", "nan") else "")
        + f'.<blockquote>{_e(r.get("clause"))}</blockquote>'
        for r in suggestions.get("add_clause") or []
    ]

    add_items = [
        f'<span class="mono">{_e(r.get("cite"))}</span>'
        + (f' — {_e(r.get("title"))}' if r.get("title") else "")
        + f'<div class="why">{_e(r.get("why") or "")}</div>'
        for r in suggestions.get("add") or []
    ]

    unresolved_items = []
    for u in suggestions.get("unresolved") or []:
        line = f'<span class="mono">{_e(u.get("cite"))}</span> — not held in the register.'
        hint = u.get("did_you_mean") or {}
        if hint:
            line += (f' Possibly a slip for <span class="mono">{_e(hint.get("is_number"))}</span>: '
                     f'{_e(hint.get("evidence"))} Confirm against the source document before '
                     "changing anything.")
        unresolved_items.append(line)

    checked = bis.get("checked")
    snapshot = (f"Register last checked against the BIS catalogue on {_e(checked)}"
                if checked else "Register status is as collected")

    return f"""<!doctype html>
<meta charset="utf-8">
<title>Standards compliance report — {_e(audit.get('filename') or 'tender')}</title>
<style>
  @page {{ size: A4; margin: 16mm 14mm; }}
  * {{ box-sizing: border-box; }}
  body {{ font: 11pt/1.5 Georgia, 'Times New Roman', serif; color: #14222A;
         background: #fff; margin: 0; }}
  header {{ border-bottom: 2px solid {PALETTE['petrol']}; padding-bottom: 10px;
            margin-bottom: 18px; }}
  .mark {{ font: 700 9pt/1 ui-monospace, 'SF Mono', Menlo, monospace;
           letter-spacing: .18em; text-transform: uppercase;
           color: {PALETTE['amber']}; }}
  h1 {{ font-size: 19pt; margin: 6px 0 4px; color: {PALETTE['petrol']}; }}
  h2 {{ font-size: 12pt; margin: 20px 0 7px; color: {PALETTE['petrol']};
        border-bottom: 1px solid #C7D6DD; padding-bottom: 3px; }}
  .meta {{ font-size: 9pt; color: #4A5D6B; }}
  .meta b {{ color: #14222A; }}
  .verdict {{ padding: 9px 12px; border-left: 4px solid {PALETTE['amber']};
              background: #FBF4EE; margin: 14px 0; font-size: 10.5pt; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 6px;
           font-size: 9.5pt; }}
  th {{ text-align: left; font: 700 8pt/1.4 ui-monospace, Menlo, monospace;
        letter-spacing: .07em; text-transform: uppercase; color: #4A5D6B;
        border-bottom: 1px solid #C7D6DD; padding: 5px 6px; }}
  td {{ padding: 5px 6px; border-bottom: 1px solid #E7EDF0; vertical-align: top; }}
  .mono {{ font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size: 9.5pt; }}
  .bad {{ color: #A3301F; font-weight: 700; }}
  .ok {{ color: #1B6E30; }}
  .mute {{ color: #7C8B97; }}
  .src {{ font-size: 8pt; color: #7C8B97; word-break: break-all; }}
  .why {{ font-size: 9pt; color: #4A5D6B; margin-top: 2px; }}
  ol.edits {{ margin: 0; padding-left: 18px; }}
  ol.edits li {{ margin-bottom: 9px; }}
  blockquote {{ margin: 6px 0 0; padding: 7px 10px; background: #F4F7F9;
                border-left: 3px solid #C7D6DD; font-size: 9.5pt; }}
  footer {{ margin-top: 24px; padding-top: 10px; border-top: 1px solid #C7D6DD;
            font-size: 8.5pt; color: #4A5D6B; }}
  @media print {{ .noprint {{ display: none; }} }}
</style>
<header>
  <div class="mark"><img src="data:image/png;base64,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" alt="" style="width:18px;height:18px;vertical-align:-4px;margin-right:6px">NiyamKosh · standards compliance report</div>
  <h1>{_e(audit.get('filename') or 'Tender document')}</h1>
  <div class="meta">
    Generated <b>{today}</b> ·
    <b>{_e(audit.get('cited_count', 0))}</b> citations read ·
    <b>{_e(counts.get('high', 0))}</b> blocking ·
    <b>{_e(counts.get('medium', 0))}</b> for review
  </div>
</header>

<div class="verdict"><b>{_e(audit.get('verdict', '').replace('_', ' ').title())}.</b>
  {_e(audit.get('summary') or '')}</div>

<h2>Citations and their status</h2>
<table>
  <thead><tr><th>IS number</th><th>Register status</th><th>Replaced by</th>
    <th>Finding</th></tr></thead>
  <tbody>{''.join(citation_rows) or _rows(['<span class="mute">No citations read.</span>','','',''])}</tbody>
</table>

{block('Citations to replace', replace_items)}
{block('Certification clauses this tender must carry', clause_items)}
{block('Standards comparable tenders cite that this one omits', add_items)}
{block('Cited but not held in the register', unresolved_items)}

<footer>
  {snapshot}. This report lists what the NiyamKosh register holds about the
  standards cited in this document. It is not a certification of the tender and
  confers no approval: every entry traces to a BIS catalogue record or to a
  published tender document, and any standard the register does not hold is
  reported as unresolved rather than assumed valid. Figures are counts over this
  document only.
</footer>
"""
