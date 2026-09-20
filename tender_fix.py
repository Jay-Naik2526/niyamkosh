"""Turn a flawed tender specification into a corrected one, and then prove it.

The audit tells an officer what is wrong with their document. This takes the
next step and hands back the document with the faults repaired: the withdrawn
citations swapped for what replaces them, the standards their own citations
imply added, and the Standard Mark demanded where the law requires it.

Three rules hold the whole thing up.

**The register decides, the model writes.** Which standard is dead, what
replaces it, what must be added and what certification applies are all settled
before the model is called, by the same code that answers every other screen.
The model is given that decision as a list of instructions and one job:
re-render the officer's own prose around it. It is never asked what a standard
is, and it is never in a position to answer.

**Nothing outside the whitelist survives.** Every IS number permitted in the
output is enumerated in advance. The generated text is scanned with the same
extractor the rest of this project uses, and a designation that is not on that
list voids the generation — the document is rebuilt deterministically instead.
A model cannot introduce a standard here, because a generation that does is
discarded rather than shown.

**The output is audited again.** The corrected document goes back through
`audit_tender` from scratch, as if an officer had uploaded it. The before and
after are reported side by side. If the rewrite did not actually clear the
findings, that is what the screen says — the claim is never that it worked, it
is the second audit's result, whatever that turns out to be.

The deterministic path is not a fallback for emergencies; it is the floor. With
no API key, no network, or a refused generation, the document is still repaired
by literal substitution. The model makes it read better. It never makes it
more correct.
"""
from __future__ import annotations

import re

MAX_INPUT_CHARS = 18000


def _mark_clause(schemes: list[str]) -> str:
    scheme = schemes[0] if schemes else "BIS certification"
    return (
        "All material supplied against this specification shall bear the Standard Mark "
        f"of the Bureau of Indian Standards under {scheme}. The bidder shall furnish a "
        "valid BIS licence covering the offered item at the time of bid submission. "
        "Material without a valid Standard Mark shall not be accepted."
    )


def _demands_mark(text: str) -> bool:
    """Whether the document already asks for certified material, in any of the
    ways a tender writes it. Same test the audit uses, so the two cannot
    disagree about whether a clause is needed."""
    from audit import MARK_RE

    return bool(MARK_RE.search(text or ""))


def _mandatory_duties(cited: list[str]) -> list[dict]:
    """Certification rules that apply to the corrected document's citations."""
    import sqlite3

    from engine import _is_base

    conn = sqlite3.connect("manak_setu.db")
    conn.row_factory = sqlite3.Row
    try:
        out, seen = [], set()
        for c in cited:
            base = _is_base(c)
            if base in seen:
                continue
            seen.add(base)
            row = conn.execute(
                'SELECT * FROM certification_rules WHERE "IS Base" = ? '
                'AND "Certification Mandatory" = "Yes" LIMIT 1', (base,)).fetchone()
            if row is not None:
                out.append({"is_number": row["IS Number"], "scheme": row["Scheme"]})
        return out
    finally:
        conn.close()


def plan_fix(text: str, filename: str | None = None, cited: list[str] | None = None) -> dict:
    """What has to change, decided entirely from the register."""
    import successor
    from audit import audit_tender

    text = (text or "")[:MAX_INPUT_CHARS]
    before = audit_tender(text, filename, cited)
    sug = before["suggestions"]

    # Every designation the corrected document is allowed to contain: the
    # citations that are fine as they are, plus what the register says to put in.
    keep = [r["cited_as"] for r in before["resolved"]
            if r["found"] and r["status"] == "Current"]
    changes: list[dict] = []

    for item in sug.get("replace", []):
        s = successor.successor_for(item["cite"])
        if s.get("found"):
            changes.append({
                "action": "replace", "from": item["cite"], "to": s["is_number"],
                "to_title": s.get("title"), "basis": s["basis"], "why": s["why"],
                "status": item.get("status"),
                "confirmed": s["basis"] in ("bis_record", "bis_record_inverted"),
                "score": s.get("score"),
            })
        else:
            changes.append({
                "action": "flag", "from": item["cite"], "to": None,
                "basis": s.get("basis", "no_successor_recorded"),
                "status": item.get("status"),
                "confirmed": False,
                "why": ("BIS records no successor and no current standard is close enough to "
                        "name one. This citation is marked in the document for a human to "
                        "resolve — it is not silently deleted and not silently kept."),
            })

    for item in sug.get("add", []):
        if not item.get("in_register", True):
            continue
        changes.append({
            "action": "add", "from": None, "to": item["cite"], "to_title": item.get("title"),
            "basis": "co_citation", "confirmed": False,
            "confidence": item.get("confidence"), "why": item.get("why"),
        })

    # The mark clause has to be decided over the citations the corrected
    # document will carry, not the ones it arrived with. On the first real run
    # the replacements and additions pulled in three more standards under
    # compulsory certification, so the second audit came back with *more*
    # statutory omissions than the first — the fix had created the finding it
    # was meant to clear. The certification duty is re-derived here over the
    # final set.
    final_cites = list(keep)
    for ch in changes:
        if ch["action"] in ("replace", "add") and ch.get("to"):
            final_cites.append(ch["to"])
        elif ch["action"] == "flag" and ch.get("from"):
            final_cites.append(ch["from"])
    duties = _mandatory_duties(final_cites)
    if duties and not _demands_mark(text):
        changes.append({
            "action": "clause", "from": None, "to": None, "basis": "quality_control_order",
            "confirmed": True,
            "for": [d["is_number"] for d in duties],
            "why": ("The corrected document cites "
                    + ", ".join(d["is_number"] for d in duties[:4])
                    + (" and others" if len(duties) > 4 else "")
                    + ", which BIS certification is compulsory for, and the text demands the "
                      "Standard Mark nowhere."),
            "text": _mark_clause([d["scheme"] for d in duties if d.get("scheme")]),
        })

    allowed = set(keep)
    for ch in changes:
        if ch["action"] in ("replace", "add") and ch.get("to"):
            allowed.add(ch["to"])
        if ch["action"] == "flag" and ch.get("from"):
            allowed.add(ch["from"])         # kept, and marked
    return {
        "before": before,
        "changes": changes,
        "allowed": sorted(allowed),
        "unresolved": sug.get("unresolved", []),
        "text": text,
    }


def deterministic_fix(text: str, plan: dict) -> str:
    """The corrected document, by literal substitution. No model involved.

    This is the floor the feature never drops below, and the thing the model's
    output is checked against. Replacements are made on the exact string the
    document used, so a document that wrote "IS:303-1989" gets its own spelling
    replaced rather than a normalised one appearing beside it.
    """
    out = text
    added, flagged = [], []
    for ch in plan["changes"]:
        if ch["action"] == "replace":
            pattern = re.compile(
                r"IS[:\s]*" + re.escape(re.sub(r"^IS[:\s]*", "", ch["from"]).strip())
                + r"(?:\s*[:\-]\s*\d{4})?", re.I)
            out, n = pattern.subn(ch["to"], out)
            if not n:
                added.append(f'{ch["to"]} (replacing {ch["from"]})')
        elif ch["action"] == "flag":
            flagged.append(ch["from"])
        elif ch["action"] == "add":
            # A standard that a replacement already put into the text does not
            # also belong in the "additional standards" line.
            if not re.search(r"(?<![\w])" + re.escape(ch["to"]) + r"(?![\d])", out, re.I):
                added.append(ch["to"])

    tail = []
    if added:
        tail.append("Additional standards applicable to this procurement: "
                    + ", ".join(added) + ".")
    if flagged:
        tail.append("The following citations are recorded as withdrawn or superseded and BIS "
                    "records no successor. They require confirmation with BIS before this "
                    "document is published: " + ", ".join(flagged) + ".")
    for ch in plan["changes"]:
        if ch["action"] == "clause":
            tail.append(ch["text"])
    if tail:
        out = out.rstrip() + "\n\n" + "\n\n".join(tail) + "\n"
    return out


def _instructions(plan: dict) -> str:
    lines = []
    for ch in plan["changes"]:
        if ch["action"] == "replace":
            lines.append(f'- Replace every reference to {ch["from"]} with {ch["to"]}. '
                         f'Reason: {ch["from"]} is {ch.get("status", "not current")}.')
        elif ch["action"] == "flag":
            lines.append(f'- Keep {ch["from"]} where it is, and append exactly this after it: '
                         f'"[{ch["from"]} is recorded as {ch.get("status", "not current")}; '
                         f'no successor is on record - confirm with BIS before publication]".')
        elif ch["action"] == "add":
            lines.append(f'- Add {ch["to"]} as an applicable standard'
                         + (f' ({ch["to_title"]})' if ch.get("to_title") else '') + '.')
        elif ch["action"] == "clause":
            lines.append('- Add this clause verbatim as its own paragraph at the end: "'
                         + ch["text"] + '"')
    return "\n".join(lines) or "- No changes required."


def _prompt(plan: dict) -> str:
    return f"""You are editing an Indian government procurement specification.

Apply the edits listed below to the document. Do not do anything else.

RULES, all absolute:
1. Do not invent, infer or add any Indian Standard number. The only IS numbers
   permitted anywhere in your output are these: {", ".join(plan["allowed"]) or "none"}.
   Any other IS number makes your output invalid and it will be discarded.
2. Keep every other detail of the document exactly as written: quantities,
   sizes, materials, ratings, delivery terms, clause numbering, headings, and
   the order of the paragraphs.
3. Do not add commentary, notes, explanations, markdown formatting, or a
   preamble. Return only the corrected specification text.
4. Do not soften, summarise or shorten the document. It must remain a
   specification that could be published as-is.

EDITS TO APPLY:
{_instructions(plan)}

DOCUMENT:
{plan["text"]}
"""


def verify(text_out: str, plan: dict) -> dict:
    """Audit the generated document from scratch, exactly as if it had been
    uploaded, and check it introduced no designation it was not given."""
    from audit import audit_tender
    from engine import _is_base, extract_citations

    allowed_bases = {_is_base(a) for a in plan["allowed"]}
    found = extract_citations(text_out)
    intruders = sorted({c for c in found if _is_base(c) not in allowed_bases})
    after = audit_tender(text_out, filename="corrected")
    return {
        "audit": after,
        "intruders": intruders,
        "clean": not intruders and after["counts"]["high"] == 0,
        "citations": found,
    }


def fix_document(text: str, filename: str | None = None,
                 cited: list[str] | None = None, use_model: bool = True) -> dict:
    """Plan, rewrite, verify. The result reports what actually happened."""
    import gemini

    plan = plan_fix(text, filename, cited)
    baseline = deterministic_fix(plan["text"], plan)

    model = {"used": False, "reason": "not_requested"}
    corrected, source = baseline, "deterministic"

    if use_model and plan["changes"] and gemini.available():
        got = gemini.generate(_prompt(plan))
        if not got.get("ok"):
            model = {"used": False, "reason": got.get("reason"), "detail": got.get("detail")}
        else:
            candidate = got["text"].strip()
            check = verify(candidate, plan)
            if check["intruders"]:
                # The one failure mode that matters, and it is caught in code.
                model = {"used": False, "reason": "introduced_a_standard",
                         "detail": "The model wrote IS numbers it was not given: "
                                   + ", ".join(check["intruders"]),
                         "rejected_text": candidate[:4000], "model": got.get("model")}
            elif len(candidate) < len(plan["text"]) * 0.55:
                model = {"used": False, "reason": "lost_content",
                         "detail": (f"The rewrite came back {len(candidate)} characters against "
                                    f"{len(plan['text'])} in, so it dropped part of the document."),
                         "rejected_text": candidate[:4000], "model": got.get("model")}
            else:
                corrected, source = candidate, "model"
                model = {"used": True, "model": got.get("model"),
                         "attempts": got.get("attempts")}
    elif use_model and plan["changes"] and not gemini.available():
        model = {"used": False, "reason": "no_key"}

    final = verify(corrected, plan)
    return {
        "filename": filename,
        "source": source,
        "model": model,
        "changes": plan["changes"],
        "allowed": plan["allowed"],
        "unresolved": plan["unresolved"],
        "original": plan["text"],
        "corrected": corrected,
        "deterministic": baseline,
        "before": {
            "verdict": plan["before"]["verdict"],
            "counts": plan["before"]["counts"],
            "summary": plan["before"]["summary"],
            "cited_count": plan["before"]["cited_count"],
        },
        "after": {
            "verdict": final["audit"]["verdict"],
            "counts": final["audit"]["counts"],
            "summary": final["audit"]["summary"],
            "cited_count": final["audit"]["cited_count"],
            "findings": final["audit"]["findings"],
        },
        "verified": final["clean"],
        "intruders": final["intruders"],
        "note": (
            "The corrected document was audited again from scratch by the same code that "
            "audited the original. The 'after' figures are that second audit's result, not "
            "a claim about the rewrite."
        ),
    }
