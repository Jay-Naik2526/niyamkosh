"""Reading a corrected document back as a document.

`docx_text()` and `pdf_text()` flatten a file into one stream of lines. That is
right for what they are for — the audit only asks whether a citation is present,
and a citation inside a schedule table or a running footer is still a citation.
It is the wrong reading to rebuild from. Run through it, a tender comes out as
two paragraph-shaped walls: the numbered clause headings sit inline with their
own text, every table becomes prose, and the running footer lands at the end of
the body because that is where the flat walk reaches it.

So the cross-format builder reads structure instead, and it reads it from the
already-corrected file rather than from the corrected text. The corrections are
in that file — put there by the editor that works inside the original — so
nothing has to be re-applied, and nothing can drift between what was audited
and what is typeset.

What comes back is a short, format-independent block list: headings, paragraphs
and tables, in document order. No block is invented; a run of text that cannot
be classified stays a paragraph.
"""
from __future__ import annotations

import io
import re

# A clause heading in an Indian tender is nearly always one of these: a
# numbered clause in capitals, or a short line in capitals on its own.
NUMBERED = re.compile(r"^\s*(\d+(\.\d+)*)[.)]?\s+(.{2,90})$")
HEAD_MAX = 92


def _looks_like_heading(text: str, bold: bool = False) -> bool:
    t = text.strip()
    if not t or len(t) > HEAD_MAX or t.endswith("."):
        return False
    letters = [c for c in t if c.isalpha()]
    if not letters:
        return False
    caps = sum(c.isupper() for c in letters) / len(letters)
    m = NUMBERED.match(t)
    if m and caps > 0.6:
        return True
    return caps > 0.85 and len(t.split()) <= 12 or (bold and caps > 0.6)


# A tender's schedule of quantities is very often ruled with horizontal lines
# and no vertical ones — pdfplumber's default strategy wants both, and finds
# nothing, so the schedule comes out as prose. Falling back to columns inferred
# from word positions recovers it. That inference is loose enough to turn an
# ordinary paragraph into a one-column "table", so a fallback result is kept
# only when it is shaped like a table: several ruled rows, more than one
# column, and most cells filled.
MIN_RULES = 3           # a ruled schedule has at least a head rule and two rows
MIN_RULE_WIDTH = 0.40   # of the page, so an underline is not a table


def _rule_bands(page):
    """Runs of evenly spaced horizontal rules — the vertical extent of a table
    that is ruled between its rows and has no vertical lines at all. A tender's
    schedule of quantities is very often drawn this way."""
    rules = sorted({round(l["top"], 1) for l in page.lines
                    if abs(l["y0"] - l["y1"]) < 1.5
                    and (l["x1"] - l["x0"]) >= page.width * MIN_RULE_WIDTH})
    bands, run = [], []
    for t in rules:
        if run and t - run[-1] > 46:
            if len(run) >= MIN_RULES:
                bands.append((run[0], run[-1]))
            run = []
        run.append(t)
    if len(run) >= MIN_RULES:
        bands.append((run[0], run[-1]))
    # One row's height of slack at each end: the first rule is under the head
    # row and the last is under the final row, so both sit inside the run.
    step = 20.0
    return [(a - step - 4, b + step + 4) for a, b in bands]


def _find_tables(page):
    """Only tables the file actually draws — ruled on both axes, or boxed.

    A table ruled between its rows and nowhere else records no column edges at
    all. Recovering them means inferring boundaries from where words happen to
    fall, and on this corpus that reads "IS 1753" as two cells: one holding
    "IS", one holding "1753". A schedule of quantities silently re-columned is
    a worse outcome than one that was never claimed to be a table, so the
    inference is not made. Rows inside a rule band keep their row structure as
    lines instead, which is true to the page without asserting a grid.
    """
    return page.find_tables()


def _block(kind, text="", **kw):
    b = {"kind": kind, "text": text, "align": "left", "rows": []}
    b.update(kw)
    return b


# ── Word ──────────────────────────────────────────────────────────────────

def from_docx(raw: bytes) -> list[dict]:
    """Blocks in document order, body only — the header and footer are the
    letterhead's business, not the body's."""
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(io.BytesIO(raw))
    align_word = {WD_ALIGN_PARAGRAPH.CENTER: "center", WD_ALIGN_PARAGRAPH.RIGHT: "right"}
    out: list[dict] = []
    body = d.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = Paragraph(child, d)
            # A paragraph can carry its own line breaks; each is a line of the
            # same block, not a new one.
            for line in [s for s in para.text.split("\n")]:
                line = line.strip()
                if not line:
                    continue
                bold = any(r.bold for r in para.runs if r.text.strip())
                styled = str(para.style.name or "").lower().startswith("heading")
                out.append(_block(
                    "heading" if styled or _looks_like_heading(line, bold) else "para",
                    line,
                    align=align_word.get(para.alignment, "left"),
                    bold=bold,
                ))
        elif tag == "tbl":
            table = Table(child, d)
            rows = []
            for row in table.rows:
                rows.append([" ".join(c.text.split()) for c in row.cells])
            if rows:
                out.append(_block("table", rows=rows))
    return out


# ── PDF ───────────────────────────────────────────────────────────────────

def from_pdf(raw: bytes, skip_top_pt: float = 0.0) -> list[dict]:
    """Blocks in reading order. `skip_top_pt` drops the letterhead band on page
    one, which is rebuilt separately and would otherwise print twice."""
    import pdfplumber

    out: list[dict] = []
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        for i, page in enumerate(pdf.pages):
            top_cut = skip_top_pt if i == 0 else 0.0
            tables = _find_tables(page)
            boxes = [t.bbox for t in tables]

            def in_table(top, bottom):
                return any(b[1] - 2 <= top and bottom <= b[3] + 2 for b in boxes)

            words = [w for w in page.extract_words(extra_attrs=["size", "fontname"])
                     if w["top"] >= top_cut]
            sizes = sorted(w.get("size") or 0 for w in words)
            body_size = sizes[len(sizes) // 2] if sizes else 10.0

            rows: dict[float, list] = {}
            for w in words:
                rows.setdefault(round(w["top"], 1), []).append(w)

            bands = _rule_bands(page)
            in_band = lambda t: any(a <= t <= b for a, b in bands)

            items = []
            for top in sorted(rows):
                row = sorted(rows[top], key=lambda w: w["x0"])
                bottom = max(w["bottom"] for w in row)
                if in_table(top, bottom):
                    continue
                text = " ".join(w["text"] for w in row).strip()
                if not text:
                    continue
                if in_band(top):
                    items.append((top, _block("row", text)))
                    continue
                size = float(row[0].get("size") or body_size)
                bold = "bold" in str(row[0].get("fontname", "")).lower()
                mid = (row[0]["x0"] + row[-1]["x1"]) / 2
                align = "center" if abs(mid - page.width / 2) / page.width < 0.06 else "left"
                items.append((top, _block(
                    "heading" if (size > body_size * 1.12 and len(text) <= HEAD_MAX)
                    or _looks_like_heading(text, bold) else "para",
                    text, align=align, bold=bold)))

            for t in tables:
                rows_out = [[" ".join((c or "").split()) for c in r] for r in t.extract()]
                rows_out = [r for r in rows_out if any(c for c in r)]
                if rows_out:
                    items.append((t.bbox[1], _block("table", rows=rows_out)))

            items.sort(key=lambda p: p[0])
            out.extend(b for _, b in items)
    return out


def merge_wrapped(blocks: list[dict]) -> list[dict]:
    """A PDF has no paragraphs, only lines. Consecutive body lines that read as
    one wrapped sentence are joined, so the rebuilt document reflows in Word
    instead of carrying the old page width as hard breaks."""
    out: list[dict] = []
    for b in blocks:
        prev = out[-1] if out else None
        if (b["kind"] == "para" and prev and prev["kind"] == "para"
                and prev["align"] == b["align"]
                and not prev["text"].rstrip().endswith((".", ":", ";"))
                and not re.match(r"^\s*(\d+[.)]|[a-z][.)]|[-•])\s", b["text"])):
            prev["text"] = prev["text"].rstrip() + " " + b["text"].lstrip()
            continue
        out.append(dict(b))
    return out
