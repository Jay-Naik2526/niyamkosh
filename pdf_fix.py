"""Repair a .pdf tender without rebuilding it.

Rewriting a PDF is not like rewriting a document: there is no text flow to
edit, only glyphs placed at coordinates. Regenerating the file from extracted
text would produce a correct specification on a blank page, which is not what
an officer asked for — the letterhead, the seal, the signature block and the
schedule tables are the reason the file is submittable.

So the original pages are kept exactly as they are, and only the words that
change are patched: pdfplumber reports the bounding box of every word, a white
rectangle covers the old citation, and the new one is drawn at the same
baseline in a matching size. Every other mark on the page — including the logo,
which is never decoded — passes through untouched.

The honest limits, stated because they matter:

  * A scanned page has no extractable words, so there is nothing to locate and
    nothing is patched. Those pages come back unchanged and the caller is told.
  * The replacement is drawn in Helvetica at the height of the text it covers.
    On a page set in a different face the patched word will not match its
    neighbours exactly. It is legible, correctly placed, and visibly a
    correction — which for a document that is about to be re-issued is the
    honest outcome.
  * Additions and the certification clause cannot be woven into a fixed layout,
    so they are added on a clearly headed continuation page at the end, which
    is how an addendum is issued on paper anyway.
"""
from __future__ import annotations

import io
import re

WHITE = (1, 1, 1)
PAD_X = 1.2
FONT = "Helvetica"


def pdf_words(raw: bytes, max_pages: int = 60):
    """Every word with its box, page by page."""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        for i, page in enumerate(pdf.pages[:max_pages]):
            yield i, page.width, page.height, page.extract_words() or []


TRAILING = re.compile(r"^[.,;:)\]\u2013-]*$")


def _spans(words: list[dict], pattern: re.Pattern) -> list[tuple[list[dict], str]]:
    """Runs of consecutive words whose joined text starts with the citation.

    A citation is split across words as often in a PDF as in a Word file:
    "IS", "1753", or "IS:", "1753-1997". Joining up to four neighbours finds
    them without guessing at tokenisation.

    The join is matched from the start rather than whole, because a word carries
    the punctuation that followed it: "IS 1753." tokenises as "IS" and "1753."
    and a whole-string match misses every citation that ends a sentence, which
    is most of them. Whatever the pattern did not consume is kept and redrawn
    after the replacement, so the full stop survives the edit.
    """
    out, n, used = [], len(words), set()
    for i in range(n):
        if i in used:
            continue
        for span in range(1, 5):
            if i + span > n:
                break
            group = words[i:i + span]
            joined = "".join(w["text"] for w in group)
            m = pattern.match(joined)
            if not m:
                continue
            rest = joined[m.end():]
            if not TRAILING.match(rest):
                continue
            out.append((group, rest, i + span))
            used.update(range(i, i + span))
            break
    return out


def _already_replaced(words: list[dict], after: int, group: list[dict],
                      replacement: str) -> bool:
    """True when the text at this position already reads as the replacement.

    The second pass re-scans the document the first pass has already edited,
    and a replacement usually still contains the citation it replaced: marking
    IS 1570 withdrawn leaves "IS 1570 [withdrawn - confirm with BIS]" on the
    page, whose first two words match the pattern again. Without this check the
    overlay covers the corrected text and redraws the note on top of itself, at
    whatever size fits the two words it measured - which is how a tender ended
    up reading "IS 1570 [withdrawn[withdrawn - confirm with BIS]6 MT" across a
    column boundary.
    """
    want = re.sub(r"\s+", "", replacement)
    if not want:
        return False
    # Reading order is not reliable here. A replacement longer than the text it
    # replaced runs past its column, so the next column's words sort in among
    # its own and a straight walk forward through the word list reads
    # "IS 1570 [withdrawn - confirm 6 MT with BIS]". The line the citation sits
    # on, re-joined left to right, is the text that is actually on the page.
    top = min(w["top"] for w in group)
    left = min(w["x0"] for w in group)
    line = sorted((w for w in words
                   if abs(w["top"] - top) <= 2.5 and w["x0"] >= left - 0.5),
                  key=lambda w: w["x0"])
    got = re.sub(r"\s+", "", "".join(w["text"] for w in line))
    # Contained in order rather than a prefix: where the replacement is wider
    # than the space its column left it, the glyphs of the next column fall
    # between its own, and the line reads "…confirm with BI6S ]MT". Every
    # character of the replacement is still there, in order, which is what
    # says the first pass reached this citation.
    i = 0
    for ch in got:
        if i < len(want) and ch == want[i]:
            i += 1
    return i == len(want)


def _pattern(is_number: str) -> re.Pattern:
    digits = re.sub(r"^IS[:\s/-]*", "", is_number.strip(), flags=re.I)
    return re.compile(
        r"IS[\s:/-]*" + re.escape(digits) + r"(?![\d])(?:[:\-\u2013](?:19|20)\d{2})?",
        re.I,
    )


def _rewrite_text_layer(page, rules: list[tuple[re.Pattern, str]]) -> int:
    """Edit the citation inside the page's own content stream.

    Covering the old text with a white rectangle fixes the picture and not the
    document: the glyphs are still in the content stream, so the file still
    reads "IS 1753" to anything that extracts text — a copy-paste, a search, a
    procurement portal's parser, and this project's own re-audit, which would
    then report the corrected document as still citing a withdrawn standard.
    Being visually right and textually wrong is worse than either.

    So the text-showing operators are rewritten in place. This works on the
    common case, where the font maps bytes to the characters they look like.
    Where it does not, the operator is left alone and the caller finds out the
    way anything else would — by extracting the text of the file that was
    produced and auditing it.
    """
    from pypdf.generic import ContentStream, NameObject, TextStringObject

    try:
        content = ContentStream(page.get_contents(), page.pdf)
    except Exception:                                        # noqa: BLE001
        return 0

    changed = 0

    def swap(value: str) -> str | None:
        out = value
        for pattern, replacement in rules:
            out = pattern.sub(replacement, out)
        return out if out != value else None

    for operands, operator in content.operations:
        if operator in (b"Tj", b"'"):
            if operands and isinstance(operands[0], TextStringObject):
                got = swap(str(operands[0]))
                if got is not None:
                    operands[0] = TextStringObject(got)
                    changed += 1
        elif operator == b'"':
            if len(operands) >= 3 and isinstance(operands[2], TextStringObject):
                got = swap(str(operands[2]))
                if got is not None:
                    operands[2] = TextStringObject(got)
                    changed += 1
        elif operator == b"TJ":
            if not operands or not isinstance(operands[0], list):
                continue
            # A TJ array interleaves strings with kerning numbers, so a citation
            # can straddle several entries. The strings are joined, edited, and
            # written back into the first entry with the rest emptied — the
            # kerning numbers stay where they are, so spacing elsewhere on the
            # line is undisturbed.
            parts = [(i, str(x)) for i, x in enumerate(operands[0])
                     if isinstance(x, TextStringObject)]
            if not parts:
                continue
            joined = "".join(t for _, t in parts)
            got = swap(joined)
            if got is None:
                continue
            operands[0][parts[0][0]] = TextStringObject(got)
            for i, _ in parts[1:]:
                operands[0][i] = TextStringObject("")
            changed += 1

    if changed:
        page.replace_contents(content)
    return changed


def fix_pdf(raw: bytes, changes: list[dict]) -> tuple[bytes, dict]:
    """The original PDF with its dead citations corrected, plus an addendum page.

    Two passes, in this order and for this reason.

    The content stream is edited first. When that works the replacement is drawn
    by the page's own font at the page's own spacing, so the corrected citation
    is typographically indistinguishable from the ones around it, and the file
    reads correctly to anything that extracts text.

    Then the result is re-read. Any citation that should have gone and is still
    there — a font whose bytes do not map to the characters they look like, text
    inside a form field or an annotation — gets the visual patch instead: a white
    box and the replacement redrawn in Helvetica. That is second best, and it
    only runs where the first pass could not reach.

    Doing the overlay unconditionally, which is where this started, put the new
    text on the page twice: once in the corrected stream and once on top of it.
    The page looked right and extracted as "IISS 8 811303.0".
    """
    from pypdf import PdfReader, PdfWriter

    subs = [(_pattern(c["from"]), c["to"]) for c in changes if c["action"] == "replace"]
    flags = [(_pattern(c["from"]), f'{c["from"]} [withdrawn]')
             for c in changes if c["action"] == "flag"]
    rules = subs + flags

    # Pass 1 — the text layer.
    reader = PdfReader(io.BytesIO(raw))
    writer = PdfWriter()
    text_edits = 0
    for page in reader.pages:
        if rules:
            text_edits += _rewrite_text_layer(page, rules)
        writer.add_page(page)
    staged = io.BytesIO()
    writer.write(staged)
    staged_bytes = staged.getvalue()

    # Pass 2 — whatever the first pass could not reach.
    overlays, patched_per_page, scanned_pages = {}, {}, []
    remaining = []
    for pattern, replacement in rules:
        remaining.append((pattern, replacement))
    for index, width, height, words in pdf_words(staged_bytes):
        if not words:
            scanned_pages.append(index + 1)
            continue
        drawn = _overlay_for(width, height, words, remaining)
        if drawn:
            overlays[index], patched_per_page[index + 1] = drawn
    if overlays:
        reader2 = PdfReader(io.BytesIO(staged_bytes))
        writer = PdfWriter()
        for i, page in enumerate(reader2.pages):
            if i in overlays:
                page.merge_page(PdfReader(io.BytesIO(overlays[i])).pages[0])
            writer.add_page(page)
    else:
        writer = PdfWriter(clone_from=io.BytesIO(staged_bytes))

    tail = _addendum_lines(changes)
    if tail:
        first = reader.pages[0]
        size = (float(first.mediabox.width), float(first.mediabox.height))
        writer.add_page(PdfReader(io.BytesIO(_addendum_page(tail, size))).pages[0])

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), {
        "format": "pdf",
        "template_preserved": True,
        "text_layer_edits": text_edits,
        "overlay_patches": sum(patched_per_page.values()),
        "pages_patched": sorted(patched_per_page),
        "addendum_page": bool(tail),
        "pages_with_no_text": scanned_pages,
        "note": ("The original pages are the original pages. The citations were corrected in "
                 "the page's own content stream, so they keep the document's typeface; only "
                 "where that was not possible was a citation covered and redrawn."
                 + (f" {len(scanned_pages)} page(s) carry no extractable text and were left "
                    "untouched." if scanned_pages else "")),
    }


def _overlay_for(width, height, words, rules):
    """A one-page overlay for citations the stream edit did not reach, or None."""
    from reportlab.lib.colors import Color
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    hits = 0
    for pattern, base_replacement in rules:
        for group, trailing, after in _spans(words, pattern):
            replacement = base_replacement + trailing
            if _already_replaced(words, after, group, replacement):
                continue
            x0 = min(w["x0"] for w in group) - PAD_X
            x1 = max(w["x1"] for w in group) + PAD_X
            top = min(w["top"] for w in group)
            bottom = max(w["bottom"] for w in group)
            size = max(6.0, (bottom - top) * 0.82)
            y0 = height - bottom                     # pdfplumber measures from the top
            c.setFillColor(Color(*WHITE))
            c.rect(x0, y0 - 0.6, x1 - x0, (bottom - top) + 1.2, stroke=0, fill=1)
            c.setFillColor(Color(0, 0, 0))
            room = (x1 - x0) - 1.0
            while size > 5.0 and c.stringWidth(replacement, FONT, size) > room:
                size -= 0.25
            c.setFont(FONT, size)
            c.drawString(x0 + 0.5, y0 + (bottom - top) * 0.20, replacement)
            hits += 1
    if not hits:
        return None
    c.save()
    return buf.getvalue(), hits


def _addendum_lines(changes: list[dict]) -> list[str]:
    lines = []
    adds = [c["to"] for c in changes if c["action"] == "add" and c.get("to")]
    if adds:
        lines.append("Additional standards applicable to this procurement: "
                     + ", ".join(adds) + ".")
    flagged = [c["from"] for c in changes if c["action"] == "flag" and c.get("from")]
    if flagged:
        # A PDF cannot reflow. Marking a citation withdrawn in the page itself
        # has to fit the width the original word occupied, or it runs into
        # whatever sits to its right - in a schedule of quantities, the
        # quantity. So the page carries a short mark and the sentence it stands
        # for is stated here, where there is room for it.
        lines.append(
            "Withdrawn standards marked [withdrawn] in this document: "
            + ", ".join(flagged)
            + ". No successor is recorded for these. Confirm the current "
              "designation with BIS before publication.")
    for c in changes:
        if c["action"] == "clause":
            lines.append(c["text"])
    return lines


def _addendum_page(lines: list[str], size: tuple[float, float]) -> bytes:
    from reportlab.lib.colors import Color
    from reportlab.pdfgen import canvas

    width, height = size
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=size)
    margin, y = 56, height - 64
    c.setFillColor(Color(0.03, 0.19, 0.25))
    c.rect(margin, y, width - 2 * margin, 3, stroke=0, fill=1)
    y -= 26
    c.setFont("Helvetica-Bold", 13)
    c.drawString(margin, y, "ADDENDUM - APPLICABLE INDIAN STANDARDS")
    y -= 16
    c.setFont("Helvetica", 8.5)
    c.setFillColor(Color(0.33, 0.38, 0.44))
    c.drawString(margin, y, "Issued with, and forming part of, the specification in this document.")
    y -= 26
    c.setFillColor(Color(0, 0, 0))
    for line in lines:
        c.setFont("Helvetica", 10.5)
        for row in _wrap(line, width - 2 * margin, 10.5, c):
            c.drawString(margin, y, row)
            y -= 14.5
            if y < 70:
                c.showPage()
                y = height - 70
        y -= 8
    c.save()
    return buf.getvalue()


def _wrap(text: str, room: float, size: float, c) -> list[str]:
    words, rows, line = text.split(), [], ""
    for w in words:
        trial = (line + " " + w).strip()
        if c.stringWidth(trial, "Helvetica", size) > room and line:
            rows.append(line)
            line = w
        else:
            line = trial
    if line:
        rows.append(line)
    return rows


def pdf_text(raw: bytes, max_pages: int = 60) -> str:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages[:max_pages])
