"""The other format, when the officer asks for one we cannot edit in place.

A .docx can be edited inside itself and a .pdf can be patched inside itself, so
each format round-trips with its template intact. Crossing between them cannot
work that way: converting a Word file to PDF needs a layout engine, and turning
a PDF back into Word means guessing at a structure the PDF does not record.

Rather than guess, these build a clean, plainly-formatted document from the
corrected text. What they no longer do is drop the letterhead: `letterhead.py`
reads the office lines and the crest out of the source file and they are
rebuilt here as real content - a picture in the header and text at its own
size and alignment - so the officer can edit the tender number or replace the
crest in either format. The body layout is still this builder's, not the
original's, and the file says so.

A document that had no letterhead gets none invented for it.
"""
from __future__ import annotations

import io
import re


ALIGN = {"center": 1, "right": 2, "left": 0}
_ALIGN_DOCX = ALIGN


def _docx_letterhead(d, head: dict) -> None:
    """Rebuild the letterhead in the Word header, where it repeats on every
    page and stays editable: the crest is a picture the officer can select and
    replace, the office lines are text they can retype."""
    import docx
    from docx.shared import Pt, RGBColor

    header = d.sections[0].header
    header.is_linked_to_previous = False
    for stale in list(header.paragraphs):
        stale._element.getparent().remove(stale._element)

    for img in head.get("images", []):
        para = header.add_paragraph()
        para.alignment = ALIGN.get(head.get("image_align", "center"), 1)
        try:
            kw = {}
            if img.get("width_pt"):
                kw["width"] = Pt(min(img["width_pt"], 220))
            para.add_run().add_picture(io.BytesIO(img["blob"]), **kw)
        except Exception:
            # An image Word will not take is left out rather than replaced by a
            # placeholder that would read as somebody's crest.
            para._element.getparent().remove(para._element)

    for ln in head.get("lines", []):
        para = header.add_paragraph()
        para.alignment = ALIGN.get(ln.get("align", "center"), 1)
        run = para.add_run(ln["text"])
        run.bold = bool(ln.get("bold"))
        run.font.size = Pt(max(7.0, min(float(ln.get("size") or 10.5), 20.0)))

    rule = header.add_paragraph()
    pr = rule._element.get_or_add_pPr()
    borders = docx.oxml.parse_xml(
        '<w:pBdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:bottom w:val="single" w:sz="8" w:space="1" w:color="08303F"/></w:pBdr>')
    pr.append(borders)


def build_docx(blocks: list[dict], title: str, changes: list[dict],
               head: dict | None = None) -> bytes:
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    d = docx.Document()
    style = d.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    carried = bool(head and head.get("found"))
    if carried:
        _docx_letterhead(d, head)

    h = d.add_paragraph()
    run = h.add_run(title)
    run.bold = True
    run.font.size = Pt(14)

    note = d.add_paragraph()
    n = note.add_run(
        "Re-typeset from the corrected specification. The letterhead was carried over "
        "from the file you uploaded; the body layout is this document's own."
        if carried else
        "Re-typeset from the corrected specification. This file does not carry "
        "the original document's letterhead or layout.")
    n.italic = True
    n.font.size = Pt(8.5)
    n.font.color.rgb = RGBColor(0x54, 0x62, 0x72)

    for b in blocks:
        if b["kind"] == "table":
            _docx_table(d, b["rows"])
            continue
        para = d.add_paragraph()
        if b["kind"] == "heading":
            para.alignment = _ALIGN_DOCX.get(b.get("align", "left"), WD_ALIGN_PARAGRAPH.LEFT)
            run = para.add_run(b["text"])
            run.bold = True
            run.font.size = Pt(12)
        elif b["kind"] == "row":
            # A schedule row keeps its own line. It is not re-wrapped and not
            # re-columned, because the source recorded neither.
            para.paragraph_format.space_after = Pt(2)
            para.add_run(b["text"])
        else:
            para.alignment = (_ALIGN_DOCX.get(b["align"])
                              if b.get("align") in ("center", "right")
                              else WD_ALIGN_PARAGRAPH.JUSTIFY)
            para.add_run(b["text"])

    if changes:
        d.add_page_break()
        s = d.add_paragraph().add_run("SCHEDULE OF CORRECTIONS")
        s.bold = True
        s.font.size = Pt(12)
        table = d.add_table(rows=1, cols=3)
        table.style = "Table Grid"
        for cell, head in zip(table.rows[0].cells, ("Change", "Standard", "Basis")):
            cell.text = ""
            r = cell.paragraphs[0].add_run(head)
            r.bold = True
        for ch in changes:
            row = table.add_row().cells
            row[0].text = _change_word(ch)
            row[1].text = _change_subject(ch)
            row[2].text = _basis_word(ch)
    out = io.BytesIO()
    d.save(out)
    return out.getvalue()


def _pdf_letterhead(c, head: dict, W: float, H: float, margin: float) -> float:
    """Draw the letterhead at the top of a page and return the new cursor.

    The crest is placed as an image and the office lines as real text at their
    own size and alignment, so both stay selectable and editable in any PDF
    editor rather than being baked into one flat picture.
    """
    from reportlab.lib.colors import Color
    from reportlab.lib.utils import ImageReader

    y = H - 44
    room = W - 2 * margin

    for img in head.get("images", []):
        try:
            reader = ImageReader(io.BytesIO(img["blob"]))
            iw, ih = reader.getSize()
            w = min(float(img.get("width_pt") or 0) or iw, 120.0)
            h = w * ih / iw if iw else 0
            if h <= 0:
                continue
            c.drawImage(reader, margin, y - h, width=w, height=h,
                        mask="auto", preserveAspectRatio=True, anchor="nw")
            y -= h + 6
        except Exception:
            continue

    for ln in head.get("lines", []):
        size = max(7.0, min(float(ln.get("size") or 10.5), 20.0))
        font = "Helvetica-Bold" if ln.get("bold") else "Helvetica"
        c.setFont(font, size)
        c.setFillColor(Color(0, 0, 0))
        align = ln.get("align", "center")
        y -= size * 1.18
        if align == "center":
            c.drawCentredString(W / 2, y, ln["text"])
        elif align == "right":
            c.drawRightString(W - margin, y, ln["text"])
        else:
            c.drawString(margin, y, ln["text"])

    y -= 8
    c.setFillColor(Color(0.03, 0.19, 0.25))
    c.rect(margin, y, room, 1.2, stroke=0, fill=1)
    c.setFillColor(Color(0, 0, 0))
    return y - 18


def build_pdf(blocks: list[dict], title: str, changes: list[dict],
              head: dict | None = None) -> bytes:
    from reportlab.lib.colors import Color
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    W, H = A4
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    margin = 56
    y = H - 64

    carried = bool(head and head.get("found"))
    if carried:
        y = _pdf_letterhead(c, head, W, H, margin)
    else:
        c.setFillColor(Color(0.03, 0.19, 0.25))
        c.rect(margin, y + 8, W - 2 * margin, 3, stroke=0, fill=1)
        y -= 12
    c.setFont("Helvetica-Bold", 13)
    c.setFillColor(Color(0, 0, 0))
    y = _draw_wrapped(c, title, margin, y, W - 2 * margin, 13, "Helvetica-Bold")
    y -= 6
    c.setFont("Helvetica-Oblique", 8)
    c.setFillColor(Color(0.33, 0.38, 0.44))
    y = _draw_wrapped(c,
                      "Re-typeset from the corrected specification. The letterhead was carried "
                      "over from the file you uploaded; the body layout is this document's own."
                      if carried else
                      "Re-typeset from the corrected specification. This file does not carry "
                      "the original document's letterhead or layout.",
                      margin, y, W - 2 * margin, 8, "Helvetica-Oblique")
    y -= 14
    c.setFillColor(Color(0, 0, 0))

    room = W - 2 * margin
    for b in blocks:
        if y < 92:
            c.showPage()
            y = H - 70
        if b["kind"] == "table":
            y = _pdf_table(c, b["rows"], margin, y, room, H)
            continue
        if b["kind"] == "heading":
            y -= 4
            y = _draw_wrapped(c, b["text"], margin, y, room, 11.5, "Helvetica-Bold",
                              align=b.get("align", "left"), W=W, margin=margin)
            y -= 3
            continue
        if b["kind"] == "row":
            y = _draw_wrapped(c, b["text"], margin, y, room, 10, "Helvetica", wrap=False)
            y -= 2
            continue
        y = _draw_wrapped(c, b["text"], margin, y, room, 10.5, "Helvetica",
                          align=b.get("align", "left"), W=W, margin=margin)
        y -= 9

    if changes:
        c.showPage()
        y = H - 70
        c.setFont("Helvetica-Bold", 12)
        c.drawString(margin, y, "SCHEDULE OF CORRECTIONS")
        y -= 22
        for ch in changes:
            line = f"{_change_word(ch)}  —  {_change_subject(ch)}  ({_basis_word(ch)})"
            y = _draw_wrapped(c, line, margin, y, W - 2 * margin, 9.5, "Helvetica")
            y -= 5
            if y < 70:
                c.showPage()
                y = H - 70
    c.save()
    return buf.getvalue()


def _docx_table(d, rows) -> None:
    """The table as the source file recorded it — same cells, same order."""
    width = max(len(r) for r in rows)
    table = d.add_table(rows=0, cols=width)
    table.style = "Table Grid"
    for i, r in enumerate(rows):
        cells = table.add_row().cells
        for j in range(width):
            cells[j].text = ""
            run = cells[j].paragraphs[0].add_run(r[j] if j < len(r) else "")
            run.bold = (i == 0)


def _pdf_table(c, rows, x, y, room, H) -> float:
    """Ruled like the schedule it is, with column widths in the proportion the
    source used. A cell too long for its column wraps inside that column and
    the row grows to fit. It used to be clipped at the column edge instead,
    which quietly dropped the end of the longest cell in the table — and the
    longest cell is reliably the one carrying a withdrawal note, which is the
    single thing the correction exists to say.
    """
    from reportlab.lib.colors import Color

    width = max(len(r) for r in rows)
    shares = [max((len(r[j]) if j < len(r) else 0) for r in rows) or 1 for j in range(width)]
    # A column never narrower than about eight characters, so a short header
    # over long values does not squeeze its own column into a stripe.
    shares = [max(sh, 8) for sh in shares]
    total = sum(shares)
    widths = [room * sh / total for sh in shares]
    pad, lead = 3.0, 11.5

    def wrap(text, w, font, size):
        out, line = [], ""
        for word in str(text).split():
            trial = (line + " " + word).strip()
            if c.stringWidth(trial, font, size) > w - 2 * pad and line:
                out.append(line)
                line = word
            else:
                line = trial
        if line:
            out.append(line)
        return out or [""]

    for i, r in enumerate(rows):
        font = "Helvetica-Bold" if i == 0 else "Helvetica"
        cells = [wrap(r[j] if j < len(r) else "", widths[j], font, 9.5)
                 for j in range(width)]
        height = max(len(cl) for cl in cells) * lead + 5
        if y - height < 64:
            c.showPage()
            y = H - 70
        c.setFont(font, 9.5)
        cx = x
        for j, lines in enumerate(cells):
            for k, ln in enumerate(lines):
                c.drawString(cx + pad, y - 10 - k * lead, ln)
            cx += widths[j]
        y -= height
        c.setStrokeColor(Color(0.72, 0.78, 0.82))
        c.setLineWidth(0.6)
        c.line(x, y + 2, x + room, y + 2)
    return y - 18


def _draw_wrapped(c, text, x, y, room, size, font, align="left", W=0, margin=0,
                  wrap=True) -> float:
    c.setFont(font, size)

    def put(s, yy):
        if align == "center" and W:
            c.drawCentredString(W / 2, yy, s)
        elif align == "right" and W:
            c.drawRightString(W - margin, yy, s)
        else:
            c.drawString(x, yy, s)

    if not wrap:
        put(text, y)
        return y - size * 1.42
    line = ""
    for word in text.split():
        trial = (line + " " + word).strip()
        if c.stringWidth(trial, font, size) > room and line:
            put(line, y)
            y -= size * 1.42
            line = word
        else:
            line = trial
    if line:
        put(line, y)
        y -= size * 1.42
    return y


def _change_word(ch: dict) -> str:
    return {"replace": "Replaced", "add": "Added", "flag": "Flagged",
            "clause": "Clause added"}.get(ch["action"], ch["action"])


def _change_subject(ch: dict) -> str:
    if ch["action"] == "replace":
        return f'{ch["from"]} → {ch["to"]}'
    if ch["action"] == "flag":
        return f'{ch["from"]} (no successor on record)'
    if ch["action"] == "add":
        return str(ch.get("to") or "")
    return "BIS Standard Mark requirement"


def _basis_word(ch: dict) -> str:
    return {
        "bis_record": "BIS records the successor",
        "bis_record_inverted": "successor's own record",
        "closest_current": "closest current standard - confirm",
        "co_citation": "cited by comparable tenders",
        "quality_control_order": "compulsory certification",
        "no_successor_recorded": "no successor on record",
    }.get(ch.get("basis", ""), ch.get("basis", ""))
