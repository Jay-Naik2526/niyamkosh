"""Tender audit: read a published tender, report what is wrong with its citations.

The forward flow (retrieval.py) answers "which standard applies to this spec".
This module answers the opposite question — "this tender already cites standards;
what did its author get wrong?" — and that is the harder, more useful one, because
the mistakes are invisible to the officer who made them.

Three findings, in descending order of how much money they cost:

  dispute_risk        a cited standard is Superseded or Withdrawn. Supplier ships
                      to the old text, inspection rejects against the new one, the
                      contract goes to arbitration.
  statutory_omission  a cited product carries mandatory BIS certification under a
                      QCO, but the document never demands the Standard Mark. Legally
                      the tender cannot be awarded as written.
  missing_connected   a standard that real tenders for this product almost always
                      cite alongside one of these is absent. Usually the test method
                      or the conductor spec — the omission surfaces at inspection.

Every finding traces to a row: a standards record, a certification rule, or a
co-citation edge with its count and denominator. Nothing here infers a standard
that is not already in the register, and a citation we cannot resolve is reported
as unresolved rather than guessed at.
"""

import re
import sqlite3

DB_PATH = "manak_setu.db"

# A neighbour is worth flagging only when its absence is genuinely unusual.
# Below these the edge describes a common pairing, not an expectation.
SUGGEST_MIN_CONFIDENCE = 0.55
SUGGEST_MIN_CO_CITATIONS = 8
SUGGEST_MAX_PER_SOURCE = 3
SUGGEST_MAX_TOTAL = 8

# Phrases a tender uses when it does demand certified material. Absence of all of
# them is what makes a mandatory-certification citation an omission.
# This is the single definition. llm.py had a second one under the name
# MARK_CLAIM, used to check that a generated clause actually demands the mark;
# the two overlapped but were not the same, so a phrase could satisfy the guard
# and not the finding, or the reverse. Every copy of one fact in this codebase
# has eventually disagreed with itself, so llm.py now imports this.
MARK_PATTERNS = [
    r"standard\s*mark",
    r"\bISI\s*mark",
    r"certification\s+mark",
    r"BIS\s*(certifi|registrat|licen|mark)",
    r"\bCM\s*/\s*L\b",
    r"licen[cs]e\s+(no|number)",
    r"certified\s+under\s+.{0,30}BIS",
    r"shall\s+be\s+certified",
    r"\bQCO\b",
    r"quality\s+control\s+order",
]
MARK_RE = re.compile("|".join(MARK_PATTERNS), re.I)

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _base(is_number: str) -> str:
    """'IS 1554 (Part 1)' -> 'IS 1554'. Tenders cite parts inconsistently, so
    matching on the base number is what makes a lookup land."""
    return re.sub(r"\s+", " ", str(is_number).split("(")[0].split(":")[0]).strip()


def _digits(is_number: str) -> str:
    return re.sub(r"[^0-9]", "", _base(is_number))


def _resolve(conn, is_number: str):
    row = conn.execute('SELECT * FROM standards WHERE "IS Number" = ?', (is_number,)).fetchone()
    if row is not None:
        return row, "exact"
    row = conn.execute(
        'SELECT * FROM standards WHERE "IS Base" = ?', (_base(is_number),)
    ).fetchone()
    if row is not None:
        return row, "is_base_fallback"
    # "IS 60947" in a tender is BIS's "IS/IEC 60947": same standard, other prefix.
    digits = _digits(is_number)
    if digits:
        row = conn.execute(
            'SELECT * FROM standards WHERE "IS Digits" = ? ORDER BY "IS Number" LIMIT 1',
            (digits,),
        ).fetchone()
        if row is not None:
            return row, "number_fallback"
    return None, None


def _cert_rule(conn, is_number: str):
    row = conn.execute(
        'SELECT * FROM certification_rules WHERE "IS Number" = ?', (is_number,)
    ).fetchone()
    if row is not None:
        return row
    return conn.execute(
        'SELECT * FROM certification_rules WHERE "IS Base" = ?', (_base(is_number),)
    ).fetchone()


# ---------------------------------------------------------------- findings


def _dispute_risk(conn, cited: list[str]) -> list[dict]:
    """A citation the supplier can build to and the inspector can reject."""
    findings = []
    for citation in cited:
        row, matched_by = _resolve(conn, citation)
        if row is None or row["Status"] not in ("Superseded", "Withdrawn"):
            continue
        replaced = row["Replaced By"]
        replaced = replaced if replaced and str(replaced) != "UNKNOWN" else None
        findings.append(
            {
                "kind": "dispute_risk",
                "severity": "high",
                "is_number": citation,
                "resolved_as": row["IS Number"],
                "matched_by": matched_by,
                "title": row["Full Title"],
                "status": row["Status"],
                "replaced_by": replaced,
                "headline": f"{citation} is recorded as {row['Status']}",
                "detail": (
                    f"The tender cites {citation} as a live requirement. The register records it "
                    f"as {row['Status']}"
                    + (f", replaced by {replaced}. " if replaced else " with no successor listed. ")
                    + "A supplier manufacturing to the cited edition can be rejected at "
                    "inspection against the current one — this is the classic dispute."
                ),
                "action": (
                    f"Replace {citation} with {replaced}." if replaced
                    else f"Confirm the current status of {citation} with BIS before publication."
                ),
                "evidence": {
                    "source": "standards register",
                    "row": row["IS Number"],
                    "link": row["Source Link"],
                },
            }
        )
    return findings


# The gazette reference BIS publishes for a product is a running history: the
# order that made certification mandatory, then every supersession and every
# extension of the enforcement date, in one cell. 470 of 737 rules carry more
# than 200 characters this way and the longest is 2,771 — which the draft clause
# was pasting verbatim, so what an officer saw to copy into a tender was two
# thousand characters of amendment history.
#
# The operative instrument is the first one named. The rest is provenance: kept
# on the finding as `notification_history`, so nothing is lost and the full
# chain is still one click away, but the clause quotes the order itself.
_ORDER_TAIL = re.compile(
    r"\s*(?:Superseded by|Extension in |Order of extension|Extension Order|"
    r"Order of enforcement|Extension in the date|"
    # An amendment chain is provenance, not the operative instrument, and BIS
    # writes the join a dozen ways. Without these the split never fired on a
    # fifth of the register and the citation ran into the blunt length cut.
    r"Subsequent Amendment|Amendment in|Amendments\s*:|Amended by|"
    r"as amended by|read with)",
    re.I,
)

# "dated 17-02-2003" and "dated 17 Feb 2003" are the same date written two ways.
# Only the first was recognised, so every rule that spelled its month out fell
# through to the character limit.
_MONTH = (r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
          r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t)?(?:ember)?|Oct(?:ober)?|"
          r"Nov(?:ember)?|Dec(?:ember)?")
_DATED = re.compile(
    r"\bdated\s+\d{1,2}\s*(?:"
    r"[-/.]\s*\d{1,2}\s*[-/.]\s*\d{2,4}"          # 17-02-2003
    r"|(?:st|nd|rd|th)?\s*(?:" + _MONTH + r")\.?,?\s*\d{2,4}"   # 17 Feb 2003
    r")",
    re.I,
)

NOTIFICATION_MAX = 180


def primary_notification(reference: str | None) -> str:
    """The order that made certification mandatory, without its amendment history."""
    text = re.sub(r"\s+", " ", str(reference or "")).strip()
    if not text or text.upper() == "N/A":
        return ""
    text = re.sub(r"^\d+\.\s*", "", text)          # the list number from the BIS page
    head = _ORDER_TAIL.split(text)[0].strip(" ,;")
    # Some rows name the order once and then run straight into dates; cutting at
    # the first order's own date keeps the citation complete but bounded.
    m = _DATED.search(head)
    if m:
        head = head[: m.end()]
    if len(head) > NOTIFICATION_MAX:
        # The length cut is the last resort, and it used to land wherever the
        # 180th character fell: 154 of 737 rules ended mid-word, so a legal
        # citation read "S.O. 165(E) dated 5 Fe". Cut at a word boundary, and
        # say that something was cut - a citation that stops early is usable
        # only if the reader can tell it stopped early.
        head = head[:NOTIFICATION_MAX]
        space = head.rfind(" ")
        if space > NOTIFICATION_MAX // 2:
            head = head[:space]
        head = head.strip(" ,;") + " \u2026"
    head = head.strip(" ,;")
    # Cutting mid-reference can leave a bracket open — "(S.O. 294 (E) dated
    # 30-01-2020" — which reads as a truncation error rather than a citation.
    missing = head.count("(") - head.count(")")
    return head + ")" * max(0, missing)


def _statutory_omission(conn, cited: list[str], text: str) -> list[dict]:
    """Mandatory certification demanded by law, not asked for by the document.

    Whether the tender is *missing* the clause can only be established by reading
    the tender. When the officer supplies IS numbers with no document text there
    is nothing to search, and saying "no clause in this document requires the
    Standard Mark" would be a claim about a document we never read. The duty is
    still real and still reported — as a duty to state, not an omission found —
    and `text_supplied` is what the screen branches on.
    """
    supplied = bool((text or "").strip())
    if supplied and MARK_RE.search(text):
        return []  # the tender already demands the mark; nothing is missing

    findings = []
    for citation in cited:
        rule = _cert_rule(conn, citation)
        if rule is None or rule["Certification Mandatory"] != "Yes":
            continue
        history = rule["Notification Reference"]
        notification = primary_notification(history)
        findings.append(
            {
                "kind": "statutory_omission",
                "severity": "high" if supplied else "medium",
                "text_supplied": supplied,
                "is_number": citation,
                "resolved_as": rule["IS Number"],
                "product": rule["Product Description"],
                "scheme": rule["Scheme"],
                "notification_reference": notification,
                "notification_history": str(history or ""),
                "headline": (
                    f"{citation} needs mandatory BIS certification — the tender never asks for it"
                    if supplied else
                    f"{citation} carries a mandatory BIS certification duty"
                ),
                "detail": (
                    f"{rule['Product Description']} falls under mandatory BIS certification "
                    f"({rule['Scheme']}"
                    + (f", {notification}" if notification else "")
                    + ")."
                    + (" No clause in this document requires the BIS Standard Mark, a licence "
                       "number, or certified material. As written, uncertified goods meet the "
                       "specification."
                       if supplied else
                       " No document text was supplied, so whether this tender already demands "
                       "the Standard Mark could not be checked. The clause below is what the "
                       "document has to contain.")
                ),
                "action": (
                    "Add a clause requiring the BIS Standard Mark and a valid licence number "
                    "for this item."
                    if supplied else
                    "Confirm the tender carries a Standard Mark clause for this item; the "
                    "wording below is the one the notification requires."
                ),
                "evidence": {
                    "source": "certification rules",
                    "row": rule["IS Number"],
                    "link": rule["Source Link"],
                },
            }
        )
    return findings


def _missing_connected(conn, cited: list[str]) -> list[dict]:
    """What comparable tenders cite that this one does not.

    This is the graph earning its place. The edge is a count over real published
    tenders, so the finding reads as 'X of Y comparable documents did this' — a
    statement an officer can check, not a model's opinion."""
    cited_digits = {_digits(c) for c in cited if _digits(c)}
    best: dict[str, dict] = {}

    for citation in cited:
        rows = conn.execute(
            '''SELECT "Target IS", "Confidence", "Co-citation Count", "Tenders Citing Source",
                      "Lift", "Evidence Statement"
               FROM co_citation
               WHERE ("Source IS" = ? OR "Source IS" = ?)
                 AND "Confidence" >= ? AND "Co-citation Count" >= ?
               ORDER BY "Confidence" DESC, "Co-citation Count" DESC
               LIMIT ?''',
            (citation, _base(citation), SUGGEST_MIN_CONFIDENCE, SUGGEST_MIN_CO_CITATIONS,
             SUGGEST_MAX_PER_SOURCE * 4),
        ).fetchall()

        kept = 0
        for r in rows:
            target = r["Target IS"]
            td = _digits(target)
            if not td or td in cited_digits:
                continue  # already in the tender, under whatever part notation
            prior = best.get(td)
            if prior and prior["confidence"] >= r["Confidence"]:
                continue
            std, _ = _resolve(conn, target)
            # Never recommend adding a standard BIS has withdrawn or superseded.
            # The co-citation graph is built from tenders published over several
            # years, so a standard that was routine in 2021 can be dead now — and
            # this screen was telling an officer to add IS 325 (Withdrawn) in the
            # same breath as it flagged their own withdrawn citations as
            # blocking. That the peers cite it is still true and still useful,
            # but it belongs in the health index as evidence of a practice going
            # stale, not here as advice.
            if std is not None and std["Status"] in ("Withdrawn", "Superseded"):
                continue
            best[td] = {
                "kind": "missing_connected",
                "severity": "medium",
                "is_number": target,
                "title": std["Full Title"] if std is not None else None,
                "status": std["Status"] if std is not None else "Not in register",
                "in_register": std is not None,
                "because_of": citation,
                "confidence": r["Confidence"],
                "co_citation_count": r["Co-citation Count"],
                "tenders_citing_source": r["Tenders Citing Source"],
                "lift": r["Lift"],
                "headline": f"{target} is cited alongside {citation} in most comparable tenders",
                "detail": (
                    f"{r['Co-citation Count']} of {r['Tenders Citing Source']} published tenders "
                    f"that cite {citation} also cite {target}"
                    + (f" ({std['Full Title']})" if std is not None else "")
                    + f". This one does not. Confidence {r['Confidence']:.0%}, "
                    f"lift {r['Lift']:.2f}."
                ),
                "action": f"Confirm whether {target} should be cited, or record why it is excluded.",
                "evidence": {
                    "source": "co-citation graph",
                    "statement": r["Evidence Statement"],
                    "co_citation_count": r["Co-citation Count"],
                    "denominator": r["Tenders Citing Source"],
                },
            }
            kept += 1
            if kept >= SUGGEST_MAX_PER_SOURCE:
                break

    ranked = sorted(
        best.values(),
        key=lambda f: (-f["confidence"], -f["co_citation_count"]),
    )
    return ranked[:SUGGEST_MAX_TOTAL]


def _one_edit_apart(a: str, b: str) -> bool:
    """Damerau-Levenshtein distance of exactly one, on digit strings.

    One substitution, insertion, deletion, or transposition of adjacent digits —
    the four ways a number gets mistyped or misread off a scanned page."""
    if a == b:
        return False
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diff = [i for i in range(la) if a[i] != b[i]]
        if len(diff) == 1:
            return True                                   # substitution
        if len(diff) == 2 and diff[1] == diff[0] + 1:
            i, j = diff
            return a[i] == b[j] and a[j] == b[i]          # transposition
        return False
    # One insertion or deletion: the shorter must be the longer minus one digit.
    short, long = (a, b) if la < lb else (b, a)
    i = j = 0
    skipped = False
    while i < len(short) and j < len(long):
        if short[i] == long[j]:
            i += 1
            j += 1
        elif skipped:
            return False
        else:
            skipped = True
            j += 1
    return True


def _did_you_mean(conn, citation: str, resolved_bases: set[str]) -> dict | None:
    """A standard that exists, is one digit away, and the rest of this document
    already cites its neighbours.

    An IS number absent from the catalogue is usually not a standard we failed to
    collect — it is a typo, or a digit lost to a bad scan. Distance alone is far
    too weak a signal to act on: "IS 456" is one edit from IS 156, IS 458, IS 4566
    and a dozen others. What separates a slip from a coincidence is the rest of
    the document. A cement tender citing IS 4699 alongside IS 383 and IS 269 is
    almost certainly reaching for IS 4699's neighbour, and the co-citation graph
    knows which standards keep that company.

    So a candidate is only offered when the graph links it to something else this
    same tender cites. Returned as a question with its evidence, never as a
    correction: the officer is told what was found and decides."""
    digits = _digits(citation)
    if not digits or len(digits) < 3:
        return None

    candidates = []
    for (number, base, title, status) in conn.execute(
        'SELECT "IS Number", "IS Base", "Full Title", "Status" FROM standards'
    ):
        cand_digits = _digits(str(base or number))
        if not cand_digits or not _one_edit_apart(digits, cand_digits):
            continue
        candidates.append({"is_number": number, "base": str(base or number),
                           "title": title, "status": status})
    if not candidates:
        return None

    # Rank by how strongly the graph ties the candidate to this document's other
    # citations. No tie, no suggestion.
    others = {b for b in resolved_bases if b != _base(citation)}
    best = None
    for cand in candidates:
        links = []
        for other in others:
            row = conn.execute(
                'SELECT "Co-citation Count" FROM co_citation '
                'WHERE ("Source IS" = ? AND "Target IS" = ?) OR ("Source IS" = ? AND "Target IS" = ?) '
                'ORDER BY "Co-citation Count" DESC LIMIT 1',
                (cand["base"], other, other, cand["base"]),
            ).fetchone()
            if row and row[0]:
                links.append((other, int(row[0])))
        if not links:
            continue
        strength = sum(n for _, n in links)
        if best is None or strength > best["strength"]:
            links.sort(key=lambda kv: -kv[1])
            best = {**cand, "links": links, "strength": strength}
    if best is None:
        return None

    named = ", ".join(n for n, _ in best["links"][:3])
    return {
        "is_number": best["is_number"],
        "title": best["title"],
        "status": best["status"],
        "shares_citations_with": [n for n, _ in best["links"]],
        "evidence": (
            f"{best['is_number']} is co-cited with {named} in other tenders, and this "
            f"document cites {'them' if len(best['links']) > 1 else 'it'} too."
        ),
    }


def _unresolved(conn, cited: list[str]) -> list[dict]:
    """Cited but absent from the register. Reported, never invented."""
    resolved_bases = set()
    for citation in cited:
        row, _ = _resolve(conn, citation)
        if row is not None:
            resolved_bases.add(_base(citation))

    out = []
    for citation in cited:
        row, _ = _resolve(conn, citation)
        if row is not None:
            continue
        guess = _did_you_mean(conn, citation, resolved_bases)
        detail = (
            f"{citation} appears in this tender but has no record in the standards "
            "register, so its status and supersession cannot be checked. It is logged "
            "to the coverage backlog rather than assumed valid."
        )
        if guess:
            detail += (
                f" It is one digit from {guess['is_number']}, which does exist. "
                + guess["evidence"]
                + " Confirm against the source document before changing anything."
            )
        out.append(
            {
                "kind": "not_in_register",
                "severity": "low",
                "is_number": citation,
                "headline": (
                    f"{citation} is not in the register — possibly a slip for {guess['is_number']}"
                    if guess else f"{citation} is not in the register"
                ),
                "detail": detail,
                "action": (
                    f"Check whether {citation} was meant to be {guess['is_number']}."
                    if guess else f"Collect the BIS catalogue record for {citation}."
                ),
                "did_you_mean": guess,
                "evidence": {"source": "coverage gap", "row": None},
            }
        )
    return out


# ---------------------------------------------------------------- entry point


def audit_tender(text: str, filename: str | None = None, cited: list[str] | None = None) -> dict:
    """Full audit of one tender document.

    `cited` may be supplied when citations were extracted upstream (e.g. from a
    PDF); otherwise they are extracted literally from `text`."""
    from engine import extract_citations

    text = text or ""
    if cited is None:
        cited = extract_citations(text)

    conn = _conn()
    try:
        findings = (
            _dispute_risk(conn, cited)
            + _statutory_omission(conn, cited, text)
            + _missing_connected(conn, cited)
            + _unresolved(conn, cited)
        )
        resolved = []
        for citation in cited:
            row, matched_by = _resolve(conn, citation)
            resolved.append(
                {
                    "cited_as": citation,
                    "found": row is not None,
                    "is_number": row["IS Number"] if row is not None else None,
                    "title": row["Full Title"] if row is not None else None,
                    "year": row["Year"] if row is not None else None,
                    "status": row["Status"] if row is not None else None,
                    "product_family": row["Product Family"] if row is not None else None,
                    "matched_by": matched_by,
                }
            )
    finally:
        conn.close()

    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["kind"]))
    by_kind: dict[str, int] = {}
    for f in findings:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1

    high = sum(1 for f in findings if f["severity"] == "high")
    return {
        "suggestions": _suggestions(findings),
        "filename": filename,
        "characters": len(text),
        "cited_count": len(cited),
        "cited": cited,
        "resolved": resolved,
        "findings": findings,
        "counts": {
            "total": len(findings),
            "high": high,
            "medium": sum(1 for f in findings if f["severity"] == "medium"),
            "low": sum(1 for f in findings if f["severity"] == "low"),
            "by_kind": by_kind,
        },
        "verdict": (
            "clean" if not findings
            else "blocking" if high
            else "review"
        ),
        "summary": _summary(cited, findings, high),
        "thresholds": {
            "missing_connected_min_confidence": SUGGEST_MIN_CONFIDENCE,
            "missing_connected_min_co_citations": SUGGEST_MIN_CO_CITATIONS,
        },
    }


def _suggestions(findings: list[dict]) -> dict:
    """The findings restated as things to do to the document.

    An officer reading an audit does not want a taxonomy of problems, they want
    a list of edits: replace this citation, add that standard, add a clause. The
    finding types map onto three edits and nothing else, so that is what is
    shown. There is no accept/override step — the report suggests, the officer
    edits their tender, and the tender is the record.
    """
    replace, add, clauses, unresolved = [], [], [], []

    for f in findings:
        if f["kind"] == "dispute_risk":
            replace.append({
                "cite": f["is_number"],
                "status": f["status"],
                "with": f.get("replaced_by"),
                "why": (f"Recorded as {f['status']}"
                        + (f"; BIS lists {f['replaced_by']} in its place." if f.get("replaced_by")
                           else " with no successor recorded — confirm with BIS.")),
                "evidence": f.get("evidence"),
            })
        elif f["kind"] == "missing_connected":
            add.append({
                "cite": f["is_number"],
                "title": f.get("title"),
                # Which of this document's own citations pulled the suggestion
                # in. It was only ever inside the sentence, so the screen could
                # not group by it — and a flat list put "PVC-U pipes for soil
                # and waste discharge" under a tender for power cable, because
                # documents citing cement also cite drainage. True, and
                # unreadable without the branch it came from.
                "because_of": f.get("because_of"),
                "why": (f"{f['co_citation_count']} of {f['tenders_citing_source']} comparable "
                        f"tenders that cite {f['because_of']} also cite this."),
                "confidence": f.get("confidence"),
                "in_register": f.get("in_register", True),
            })
        elif f["kind"] == "statutory_omission":
            clauses.append({
                "for": f["is_number"],
                "product": f.get("product"),
                "scheme": f.get("scheme"),
                "notification": f.get("notification_reference"),
                "text_supplied": f.get("text_supplied", True),
                "clause": (
                    f"The {f.get('product') or 'material'} supplied shall bear the BIS Standard "
                    f"Mark under {f.get('scheme')}"
                    + (f" ({f['notification_reference']})"
                       if f.get("notification_reference")
                       and str(f["notification_reference"]) not in ("N/A", "nan", "None") else "")
                    + ". Only material bearing a valid Standard Mark and licence number "
                    "shall be accepted."
                ),
                "evidence": f.get("evidence"),
            })
        elif f["kind"] == "not_in_register":
            unresolved.append({"cite": f["is_number"],
                               "did_you_mean": f.get("did_you_mean")})

    return {
        "replace": replace,
        "add": add,
        "add_clause": clauses,
        "unresolved": unresolved,
        "counts": {"replace": len(replace), "add": len(add),
                   "add_clause": len(clauses), "unresolved": len(unresolved)},
        "note": (
            "Suggestions, not decisions. Every line names the row it came from so it "
            "can be checked before the tender is edited."
        ),
    }


def _summary(cited: list[str], findings: list[dict], high: int) -> str:
    if not cited:
        return (
            "No IS-number citations found in this document. Nothing to audit — check that the "
            "file is text rather than a scan."
        )
    if not findings:
        return (
            f"{len(cited)} citation{'s' if len(cited) != 1 else ''} checked against the "
            "register, the certification rules and "
            "the co-citation graph. Nothing flagged."
        )
    parts = []
    kinds = {f["kind"] for f in findings}
    if "dispute_risk" in kinds:
        n = sum(1 for f in findings if f["kind"] == "dispute_risk")
        parts.append(f"{n} citation{'s' if n > 1 else ''} no longer current")
    if "statutory_omission" in kinds:
        n = sum(1 for f in findings if f["kind"] == "statutory_omission")
        read = any(f.get("text_supplied") for f in findings if f["kind"] == "statutory_omission")
        parts.append(
            f"{n} mandatory-certification item{'s' if n > 1 else ''} with no Standard Mark clause"
            if read else
            f"{n} mandatory-certification item{'s' if n > 1 else ''} whose Standard Mark clause "
            "could not be checked without the document text"
        )
    if "missing_connected" in kinds:
        n = sum(1 for f in findings if f["kind"] == "missing_connected")
        # "omits" reads as a fault. These are standards that usually travel with
        # the ones already cited — worth a look, not a finding against the
        # document, and the heading they appear under says the same thing.
        parts.append(f"{n} standard{'s' if n > 1 else ''} that usually travel with these")
    if "not_in_register" in kinds:
        n = sum(1 for f in findings if f["kind"] == "not_in_register")
        parts.append(f"{n} citation{'s' if n > 1 else ''} not in the register")
    lead = "Blocking issues found. " if high else "Review recommended. "
    return (lead + f"{len(cited)} citation{'s' if len(cited) != 1 else ''} checked: "
            + "; ".join(parts) + ".")


# ---------------------------------------------------------------- decision log

DECISIONS = ("accepted", "rejected", "overridden", "escalated")

CREATE_LOG = """
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    officer      TEXT NOT NULL,
    document     TEXT,
    finding_kind TEXT,
    is_number    TEXT,
    decision     TEXT NOT NULL,
    rationale    TEXT,
    system_said  TEXT
)
"""


def _ensure_log(conn):
    """Officer decisions are runtime state, not register data — they live in
    their own table and never touch data/*.csv."""
    conn.execute(CREATE_LOG)
    conn.commit()


def record_decision(
    officer: str,
    decision: str,
    finding_kind: str | None = None,
    is_number: str | None = None,
    document: str | None = None,
    rationale: str | None = None,
    system_said: str | None = None,
) -> dict:
    """An override with no rationale is the thing an auditor asks about a year
    later, so the rationale is required whenever the officer disagrees."""
    import datetime

    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
    if decision in ("rejected", "overridden") and not (rationale or "").strip():
        raise ValueError("a rationale is required when overriding or rejecting a finding")

    created = datetime.datetime.now().isoformat(timespec="seconds")
    conn = _conn()
    try:
        _ensure_log(conn)
        cur = conn.execute(
            "INSERT INTO audit_log (created_at, officer, document, finding_kind, is_number, "
            "decision, rationale, system_said) VALUES (?,?,?,?,?,?,?,?)",
            (created, officer.strip() or "unattributed", document, finding_kind, is_number,
             decision, (rationale or "").strip() or None, system_said),
        )
        conn.commit()
        return {"id": cur.lastrowid, "created_at": created, "decision": decision}
    finally:
        conn.close()


def list_decisions(limit: int = 200) -> dict:
    conn = _conn()
    try:
        _ensure_log(conn)
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        entries = [dict(r) for r in rows]
        counts: dict[str, int] = {}
        for r in conn.execute("SELECT decision, COUNT(*) n FROM audit_log GROUP BY decision"):
            counts[r["decision"]] = r["n"]
        agreed = counts.get("accepted", 0)
        total = sum(counts.values())
        return {
            "entries": entries,
            "counts": counts,
            "total": total,
            "agreement_rate": round(100 * agreed / total, 1) if total else None,
            "note": (
                "Agreement rate is the share of findings an officer accepted. It is a measure of "
                "this deployment's usefulness, not of model accuracy — a low rate means the "
                "thresholds need tuning, and that is the point of logging it."
            ),
        }
    finally:
        conn.close()


def neighbourhood(is_number: str, depth: int = 1, limit: int = 40) -> dict:
    """The graph around one standard, for the evidence panel. Depth 2 is the
    practical ceiling — beyond that everything connects to everything and the
    picture stops meaning anything."""
    depth = max(1, min(int(depth), 2))
    conn = _conn()
    try:
        centre, matched_by = _resolve(conn, is_number)
        root = centre["IS Number"] if centre is not None else _base(is_number)

        seen = {root}
        frontier = [root]
        edges: list[dict] = []
        for _ in range(depth):
            nxt = []
            for node in frontier:
                rows = conn.execute(
                    '''SELECT "Source IS", "Target IS", "Confidence", "Lift",
                              "Co-citation Count", "Tenders Citing Source", "Evidence Statement"
                       FROM co_citation
                       WHERE "Source IS" = ? OR "Source IS" = ?
                       ORDER BY "Confidence" DESC LIMIT ?''',
                    (node, _base(node), limit),
                ).fetchall()
                for r in rows:
                    edges.append(
                        {
                            "source": r["Source IS"],
                            "target": r["Target IS"],
                            "confidence": r["Confidence"],
                            "lift": r["Lift"],
                            "co_citation_count": r["Co-citation Count"],
                            "tenders_citing_source": r["Tenders Citing Source"],
                            "evidence_statement": r["Evidence Statement"],
                        }
                    )
                    if r["Target IS"] not in seen:
                        seen.add(r["Target IS"])
                        nxt.append(r["Target IS"])
            frontier = nxt

        nodes = []
        for node_id in sorted(seen):
            std, _ = _resolve(conn, node_id)
            nodes.append(
                {
                    "id": node_id,
                    "title": std["Full Title"] if std is not None else None,
                    "status": std["Status"] if std is not None else "Not in register",
                    "product_family": std["Product Family"] if std is not None else None,
                    "in_standards_master": std is not None,
                    "is_centre": node_id == root,
                }
            )
        return {
            "centre": root,
            "found": centre is not None,
            "matched_by": matched_by,
            "depth": depth,
            "nodes": nodes,
            "edges": edges,
        }
    finally:
        conn.close()


def corpus_evidence(is_number: str, limit: int = 25) -> dict:
    """Which real tenders cite this standard. This is the 'where did your data
    come from' answer — every row links back to a published document."""
    conn = _conn()
    try:
        digits = _digits(is_number)
        rows = conn.execute("SELECT * FROM tenders").fetchall()
        # The "Dead refs" column here read the flag stored at collection time,
        # which is a historical record the benchmark depends on and is stale by
        # construction. One matcher, one answer: the same sets the health index
        # and the Tenders table decide against.
        from engine import _dead_sets, dead_citations_in

        dead_set, current_set = _dead_sets(conn)
        citing = []
        for r in rows:
            cited = [c.strip() for c in str(r["IS Numbers Cited"] or "").split(";") if c.strip()]
            if not any(_digits(c) == digits for c in cited):
                continue
            from tender_titles import display_title

            name = display_title(r["Tender ID"], r["Product Family"])
            citing.append(
                {
                    "tender_id": r["Tender ID"],
                    "title": name["title"],
                    "title_derived": name["derived"],
                    "product_family": r["Product Family"],
                    "document_type": r["Document Type"],
                    "usability": r["Usability"],
                    "citation_count": r["Count"],
                    "any_outdated": r["Any Outdated"],
                    "dead_now": (
                        "Yes" if dead_citations_in(r["IS Numbers Cited"], dead_set, current_set)
                        else ("No" if cited else "No citations")
                    ),
                    "source_link": r["Source Link"],
                    "buyer": (r["Ministry"] if "Ministry" in r.keys() else None) or None,
                }
            )
        std, matched_by = _resolve(conn, is_number)
        total_tenders = len(rows)
        # Who bought against it. The screen used to show "share of corpus",
        # which is the tender count divided by the corpus size — the same fact
        # as the tile beside it, one step of arithmetic later. This is a fact
        # the screen cannot work out for itself.
        buyers = sorted({c["buyer"] for c in citing if c["buyer"]})
        return {
            "is_number": is_number,
            "buyers": buyers,
            "resolved_as": std["IS Number"] if std is not None else None,
            "matched_by": matched_by,
            "in_register": std is not None,
            "standard": dict(std) if std is not None else None,
            "certification": (lambda c: dict(c) if c is not None else None)(
                _cert_rule(conn, is_number)
            ),
            "tenders_citing": len(citing),
            "corpus_size": total_tenders,
            "share": round(100 * len(citing) / total_tenders, 1) if total_tenders else 0.0,
            "tenders": citing[:limit],
            "truncated": len(citing) > limit,
        }
    finally:
        conn.close()
