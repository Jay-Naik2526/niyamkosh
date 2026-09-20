"""The other format, when the officer asks for one we cannot edit in place.

A .docx can be edited inside itself and a .pdf can be patched inside itself, so
each format round-trips with its template intact. Crossing between them cannot
work that way: converting a Word file to PDF needs a layout engine, and turning
a PDF back into Word means guessing at a structure the PDF does not record.

Rather than guess, these build a clean, plainly-formatted document from the
corrected text and say so. The officer gets the file in the format they asked
for; the screen tells them which of the two downloads carries their letterhead
and which does not, so nobody submits the wrong one by accident.
"""
from __future__ import annotations

import io
import re


def build_docx(text: str, title: str, changes: list[dict]) -> bytes:
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    d = docx.Document()
    style = d.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    h = d.add_paragraph()
    run = h.add_run(title)
    run.bold = True
    run.font.size = Pt(14)

    note = d.add_paragraph()
    n = note.add_run("Re-typeset from the corrected specification. This file does not carry "
                     "the original document's letterhead or layout.")
    n.italic = True
    n.font.size = Pt(8.5)
    n.font.color.rgb = RGBColor(0x54, 0x62, 0x72)

    for block in re.split(r"\n\s*\n", text.strip()):
        para = d.add_paragraph()
        para.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        para.add_run(" ".join(line.strip() for line in block.splitlines() if line.strip()))

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


def build_pdf(text: str, title: str, changes: list[dict]) -> bytes:
    from reportlab.lib.colors import Color
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    W, H = A4
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    margin = 56
    y = H - 64

    c.setFillColor(Color(0.03, 0.19, 0.25))
    c.rect(margin, y + 8, W - 2 * margin, 3, stroke=0, fill=1)
    y -= 12
    c.setFont("Helvetica-Bold", 13)
    c.setFillColor(Color(0, 0, 0))
    y = _draw_wrapped(c, title, margin, y, W - 2 * margin, 13, "Helvetica-Bold")
    y -= 6
    c.setFont("Helvetica-Oblique", 8)
    c.setFillColor(Color(0.33, 0.38, 0.44))
    y = _draw_wrapped(c, "Re-typeset from the corrected specification. This file does not carry "
                         "the original document's letterhead or layout.",
                      margin, y, W - 2 * margin, 8, "Helvetica-Oblique")
    y -= 14
    c.setFillColor(Color(0, 0, 0))

    for block in re.split(r"\n\s*\n", text.strip()):
        joined = " ".join(line.strip() for line in block.splitlines() if line.strip())
        y = _draw_wrapped(c, joined, margin, y, W - 2 * margin, 10.5, "Helvetica")
        y -= 9
        if y < 80:
            c.showPage()
            y = H - 70

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


def _draw_wrapped(c, text, x, y, room, size, font) -> float:
    c.setFont(font, size)
    line = ""
    for word in text.split():
        trial = (line + " " + word).strip()
        if c.stringWidth(trial, font, size) > room and line:
            c.drawString(x, y, line)
            y -= size * 1.42
            line = word
        else:
            line = trial
    if line:
        c.drawString(x, y, line)
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
