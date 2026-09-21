"""The letterhead, carried across formats.

A .docx is corrected inside itself and a .pdf is patched inside itself, so each
format round-trips with its own template intact. Crossing between them is where
the letterhead used to be lost: the cross-format builder re-typeset the
corrected text on a blank page and said, honestly, that the officer's letterhead
was not on it. Honest, and not much use — a tender without its issuing office on
it is not a document anyone can submit.

So the letterhead is lifted out of the source file and rebuilt in the other
format as real content: the logo as a picture, the office lines as text at their
own sizes and alignment. Rebuilt rather than flattened, because an officer has
to be able to edit it — change the tender number, correct a date, drop the crest
— in Word and in the PDF alike.

The one rule this module answers to is the project's first one. Nothing here
invents a ministry, an office, a crest or a tender number. Every line and every
image is read out of the file the officer uploaded, and a document with no
letterhead in it produces no letterhead here: `found` comes back false and the
screen says so, rather than a plausible-looking header appearing over somebody's
tender.
"""
from __future__ import annotations

import io
import re

EMU_PER_PT = 12700          # OOXML measures drawings in English Metric Units
MAX_LINES = 8               # a letterhead is a handful of lines, not a page
MAX_BAND_FRACTION = 0.30    # nothing below the top third of page one qualifies
MIN_GAP_RATIO = 1.6         # the cut has to be clearly bigger than a line gap
BANNER_WIDTH_FRACTION = 0.8 # a full-width filled rect at the top is a banner


def _blank():
    return {"found": False, "lines": [], "images": [], "banner": None,
            "band_pt": 0.0, "strip": [], "source": "", "note": ""}


# ── Word ──────────────────────────────────────────────────────────────────

def _docx_image_size(drawing) -> tuple[float, float]:
    """The size Word draws the picture at, in points, from wp:extent."""
    for el in drawing.iter():
        if el.tag.endswith("}extent"):
            cx, cy = el.get("cx"), el.get("cy")
            if cx and cy:
                return int(cx) / EMU_PER_PT, int(cy) / EMU_PER_PT
    return 0.0, 0.0


def from_docx(raw: bytes) -> dict:
    """Read the letterhead out of the first section's header."""
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    out = _blank()
    try:
        d = docx.Document(io.BytesIO(raw))
        header = d.sections[0].header
    except Exception:
        return out

    align_word = {WD_ALIGN_PARAGRAPH.CENTER: "center",
                  WD_ALIGN_PARAGRAPH.RIGHT: "right"}
    for para in header.paragraphs[:MAX_LINES]:
        # The picture first: in a letterhead the crest sits above or beside the
        # office name, and reading it in paragraph order keeps that order.
        for run in para.runs:
            for drawing in run._element.iter():
                if not drawing.tag.endswith("}drawing"):
                    continue
                w, h = _docx_image_size(drawing)
                for blip in drawing.iter():
                    if not blip.tag.endswith("}blip"):
                        continue
                    rid = next((v for k, v in blip.attrib.items() if k.endswith("}embed")), None)
                    if not rid:
                        continue
                    try:
                        part = header.part.related_parts[rid]
                    except Exception:
                        continue
                    out["images"].append({"blob": part.blob, "width_pt": w, "height_pt": h,
                                          "ext": str(part.partname).rsplit(".", 1)[-1].lower()})
        text = para.text.strip()
        if not text:
            continue
        runs = [r for r in para.runs if r.text.strip()]
        size = next((r.font.size.pt for r in runs if r.font.size is not None), None)
        out["lines"].append({
            "text": re.sub(r"\s{2,}", "  ", text),
            "size": size or 10.5,
            "bold": bool(runs and runs[0].bold),
            "align": align_word.get(para.alignment, "left"),
        })

    out["found"] = bool(out["lines"] or out["images"])
    out["source"] = "the header of the .docx you uploaded"
    # docx_text() reads the body only, so nothing has to be stripped from it.
    out["strip"] = []
    return out


# ── PDF ───────────────────────────────────────────────────────────────────

def _band_cut(page) -> float:
    """Where the letterhead stops on page one, in points from the top.

    Two pieces of evidence, strongest first. A filled rectangle anchored at the
    top and running most of the page's width is a printed banner and its own
    bottom edge is the answer. Failing that, the largest vertical gap in the top
    third of the page is the break between the header and the document — but
    only if it is clearly larger than the gaps between ordinary lines, or every
    page with generous leading would report a letterhead it does not have.
    """
    limit = page.height * MAX_BAND_FRACTION
    for r in page.rects:
        if not r.get("fill"):
            continue
        if r["top"] <= 2 and (r["x1"] - r["x0"]) >= page.width * BANNER_WIDTH_FRACTION \
                and 8 < r["bottom"] <= limit:
            return float(r["bottom"])

    tops = sorted({round(w["top"], 1) for w in page.extract_words()})
    tops = [t for t in tops if t <= limit]
    if len(tops) < 2:
        return 0.0
    gaps = [(b - a, a, b) for a, b in zip(tops, tops[1:])]
    typical = sorted(g[0] for g in gaps)[len(gaps) // 2]
    big = max(gaps)
    if typical <= 0 or big[0] < typical * MIN_GAP_RATIO:
        return 0.0
    return float(big[1] + (big[0] / 2))


def from_pdf(raw: bytes) -> dict:
    """Read the letterhead off the top of page one."""
    import pdfplumber

    out = _blank()
    try:
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            if not pdf.pages:
                return out
            page = pdf.pages[0]
            cut = _band_cut(page)
            if cut <= 0:
                out["note"] = "No letterhead band could be identified on page one."
                return out
            out["band_pt"] = cut

            for img in page.images:
                if img.get("bottom", 0) > cut:
                    continue
                blob = _image_bytes(img)
                if blob:
                    out["images"].append({
                        "blob": blob,
                        "width_pt": float(img["x1"] - img["x0"]),
                        "height_pt": float(img["bottom"] - img["top"]),
                        "ext": "png",
                    })

            words = [w for w in page.extract_words(extra_attrs=["size", "fontname"])
                     if w["bottom"] <= cut]
            rows: dict[float, list] = {}
            for w in words:
                rows.setdefault(round(w["top"], 1), []).append(w)
            for top in sorted(rows)[:MAX_LINES]:
                row = sorted(rows[top], key=lambda w: w["x0"])
                text = " ".join(w["text"] for w in row).strip()
                if not text:
                    continue
                mid = (row[0]["x0"] + row[-1]["x1"]) / 2
                off = abs(mid - page.width / 2) / page.width
                out["lines"].append({
                    "text": text,
                    "size": float(row[0].get("size") or 10.5),
                    "bold": "bold" in str(row[0].get("fontname", "")).lower(),
                    "align": "center" if off < 0.06 else
                             ("right" if row[0]["x0"] > page.width * 0.55 else "left"),
                })
    except Exception:
        return _blank()

    out["found"] = bool(out["lines"] or out["images"])
    out["source"] = "the top of page one of the .pdf you uploaded"
    # pdf_text() reads the whole page, so these lines are also the first lines
    # of the body. Whoever rebuilds the document drops them from it, or the
    # office name is printed twice.
    out["strip"] = [ln["text"] for ln in out["lines"]]
    return out


def _image_bytes(img) -> bytes | None:
    """The picture's own bytes, when the PDF stores it in a form we can pass
    straight through. A form we cannot decode is skipped rather than guessed
    at — a wrong crest is worse than no crest."""
    stream = img.get("stream")
    if stream is None:
        return None
    try:
        filters = stream.get("Filter")
        names = [str(f) for f in (filters if isinstance(filters, list) else [filters])]
        data = stream.get_rawdata() if any("DCT" in n or "JPX" in n for n in names) else None
        if data:
            return bytes(data)
        from PIL import Image  # optional; absent in the default environment
        raw = stream.get_data()
        w, h = int(stream["Width"]), int(stream["Height"])
        mode = "RGB" if len(raw) >= w * h * 3 else "L"
        buf = io.BytesIO()
        Image.frombytes(mode, (w, h), raw[: w * h * (3 if mode == "RGB" else 1)]).save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return None


def extract(raw: bytes, kind: str) -> dict:
    return from_docx(raw) if kind == "docx" else from_pdf(raw)


def strip_from(text: str, head: dict) -> str:
    """Remove the letterhead lines from the front of the body text.

    Only from the front, and only while they keep matching in order: a line that
    also appears further down the tender is part of the tender.
    """
    if not head.get("strip"):
        return text
    lines = text.split("\n")
    want = [re.sub(r"\s+", " ", s).strip().lower() for s in head["strip"]]
    i = 0
    while i < len(lines) and want:
        got = re.sub(r"\s+", " ", lines[i]).strip().lower()
        if not got:
            i += 1
            continue
        if got == want[0]:
            want.pop(0)
            i += 1
            continue
        break
    return "\n".join(lines[i:]).lstrip("\n")
