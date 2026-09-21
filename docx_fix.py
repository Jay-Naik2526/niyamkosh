"""Repair a .docx tender in place, keeping the document it arrived as.

A tender that comes back as plain text is not ready to submit. The department's
letterhead, the logo, the clause numbering, the schedule tables, the signature
block and the footer are the document as much as the words are — an officer who
has to rebuild those has not been given a corrected tender, they have been given
a draft to retype.

A .docx is a zip of XML, and python-docx walks it. So the original file is
opened, the citations are replaced inside the runs that already hold them, and
everything else is left untouched: styles, numbering, headers, footers, images,
tables, section breaks. The bytes that carry the logo are never read, let alone
rewritten.

The one hard part is that Word splits text across runs for reasons of its own.
"IS 1753" is often three runs — "IS 17", "5", "3" — because of a spell-check
mark or an edit made years ago. Replacing run by run therefore misses most real
citations. Each paragraph is instead flattened, matched, and written back with
the replacement text placed in the first run of the match and the remainder of
the matched runs cleared, which keeps the first run's formatting for the new
text. That is the standard technique, and it is why bold stays bold.
"""
from __future__ import annotations

import copy
import io
import re

MARK_STYLE_NOTE = "MANAK-SETU correction"


def _runs_text(para) -> str:
    return "".join(r.text for r in para.runs)


def _replace_in_paragraph(para, pattern: re.Pattern, replacement: str) -> int:
    """Replace across run boundaries, preserving the first run's formatting."""
    text = _runs_text(para)
    if not pattern.search(text):
        return 0
    # Character offset of each run, so a match can be mapped back to runs.
    spans, at = [], 0
    for r in para.runs:
        spans.append((at, at + len(r.text), r))
        at += len(r.text)

    count = 0
    for m in reversed(list(pattern.finditer(text))):
        start, end = m.span()
        touched = [(s, e, r) for (s, e, r) in spans if s < end and e > start]
        if not touched:
            continue
        first_s, _, first_r = touched[0]
        head = first_r.text[: start - first_s]
        last_s, last_e, last_r = touched[-1]
        tail = last_r.text[end - last_s:]
        first_r.text = head + replacement + (tail if last_r is first_r else "")
        for _, _, r in touched[1:]:
            r.text = ""
        if last_r is not first_r:
            last_r.text = tail
        count += 1
    return count


def _iter_paragraphs(doc):
    """Every paragraph in the document, including inside tables, headers and
    footers — a citation in a schedule table is still a citation."""
    yield from doc.paragraphs
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from cell.paragraphs
    for section in doc.sections:
        for part in (section.header, section.footer,
                     section.first_page_header, section.first_page_footer,
                     section.even_page_header, section.even_page_footer):
            if part is None:
                continue
            yield from part.paragraphs
            for table in part.tables:
                for row in table.rows:
                    for cell in row.cells:
                        yield from cell.paragraphs


def _citation_pattern(is_number: str) -> re.Pattern:
    """The many ways a tender writes one designation.

    IS 1753, IS:1753, IS-1753, IS 1753:1997, IS 1753 - 1997. The year is
    consumed when present, because leaving "IS 8130-1997" behind would assert an
    edition of the successor that nobody checked.
    """
    digits = re.sub(r"^IS[:\s/-]*", "", is_number.strip(), flags=re.I)
    core = re.escape(digits)
    return re.compile(
        r"IS[\s:/-]*" + core + r"(?![\d])(?:\s*[:\-\u2013]\s*(?:19|20)\d{2})?",
        re.I,
    )


def _is_signature(para) -> bool:
    """A trailing right-aligned paragraph is the signature block.

    Only right alignment counts. Plenty of tenders left-align the officer's
    designation, and a rule loose enough to catch those would also catch a
    genuine last clause — which would then have the certification requirement
    inserted above it instead of after it.
    """
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    return para.alignment == WD_ALIGN_PARAGRAPH.RIGHT


def _body_anchor(doc):
    """Where an added clause belongs: after the last clause of the tender, and
    before whatever signs it.

    This used to be simply the last non-empty paragraph, which in any document
    ending the ordinary way is the officer's designation. The compulsory
    certification clause then printed underneath the signature, right-aligned,
    reading as a postscript — in the file the officer is meant to submit.
    """
    body = [p for p in doc.paragraphs if p.text.strip()]
    if not body:
        return None
    i = len(body) - 1
    while i > 0 and _is_signature(body[i]):
        i -= 1
    return body[i]


def _append_like(doc, template_para, text: str):
    """A new paragraph that inherits the body style of the document it joins."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    new = copy.deepcopy(template_para._p) if template_para is not None else None
    if new is None:
        return doc.add_paragraph(text)
    template_para._p.addnext(new)
    from docx.text.paragraph import Paragraph

    para = Paragraph(new, template_para._parent)
    for r in list(para.runs)[1:]:
        r._r.getparent().remove(r._r)
    if para.runs:
        para.runs[0].text = text
    else:
        para.add_run(text)
    # A clause is body text wherever the paragraph it was styled from sat.
    if para.alignment in (WD_ALIGN_PARAGRAPH.RIGHT, WD_ALIGN_PARAGRAPH.CENTER):
        para.alignment = WD_ALIGN_PARAGRAPH.LEFT
    return para


def fix_docx(original: bytes, changes: list[dict]) -> tuple[bytes, dict]:
    """The same document with its citations corrected. Returns bytes and a log."""
    import docx

    doc = docx.Document(io.BytesIO(original))
    paragraphs = list(_iter_paragraphs(doc))

    applied, missed = [], []
    for ch in changes:
        if ch["action"] == "replace":
            pattern = _citation_pattern(ch["from"])
            n = sum(_replace_in_paragraph(p, pattern, ch["to"]) for p in paragraphs)
            (applied if n else missed).append({**ch, "occurrences": n})
        elif ch["action"] == "flag":
            pattern = _citation_pattern(ch["from"])
            note = (f'{ch["from"]} [withdrawn - no successor on record, '
                    f'confirm with BIS before publication]')
            n = sum(_replace_in_paragraph(p, pattern, note) for p in paragraphs)
            (applied if n else missed).append({**ch, "occurrences": n})

    # Additions and the certification clause go after the last clause of the
    # tender and before the signature block, styled like the body they join
    # rather than like a foreign note.
    anchor = _body_anchor(doc)
    # A standard a replacement has already put into the document does not also
    # belong in the "additional standards" line. The document is re-read after
    # the replacements, not before, so this reflects what is actually in it.
    present = "\n".join(p.text for p in _iter_paragraphs(doc))
    additions = [ch["to"] for ch in changes
                 if ch["action"] == "add" and ch.get("to")
                 and not _citation_pattern(ch["to"]).search(present)]
    tail = []
    if additions:
        tail.append("Additional standards applicable to this procurement: "
                    + ", ".join(additions) + ".")
    for ch in changes:
        if ch["action"] == "clause":
            tail.append(ch["text"])
    for line in tail:
        anchor = _append_like(doc, anchor, line)
        applied.append({"action": "append", "text": line[:120]})

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue(), {
        "format": "docx",
        "template_preserved": True,
        "applied": applied,
        "not_found_in_document": missed,
        "note": ("Edited inside the original file. Styles, numbering, tables, headers, "
                 "footers and images are the ones the document arrived with."),
    }


def docx_text(original: bytes) -> str:
    """Everything readable in the file, for the audit that plans the repair."""
    import docx

    doc = docx.Document(io.BytesIO(original))
    return "\n".join(p.text for p in _iter_paragraphs(doc))
