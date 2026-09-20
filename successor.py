"""What to put in a tender in place of a standard BIS has withdrawn.

This is the question an officer asks the moment the console tells them a
citation is dead, and until now the honest answer was usually "we do not know".
The register carries a `Replaced By` field on 10 of 27,687 rows and a
`Supersedes` field on 13. BIS does not publish supersession through the
catalogue endpoint this project reads, so the gap is in the source, not in the
parsing.

Three ways to answer, in descending order of authority, and every answer says
which one produced it. That label is the whole point: a reader must be able to
tell "BIS says IS 8130 replaced IS 1753" from "this register's retriever thinks
IS 9537 is the closest current standard to IS 4985".

  bis_record        BIS names the successor on the withdrawn standard's row.
  bis_record_inverted
                    A current standard's own row says it supersedes this one.
                    The same fact, recorded from the other end.
  closest_current   Nobody recorded a successor. The withdrawn standard's own
                    title is run through the same retriever the rest of this
                    console uses, over current standards only, and the best
                    match is offered as a candidate with its score. This is a
                    suggestion to check, never a supersession, and it is
                    labelled that way everywhere it is shown.

Nothing here invents a designation. Every IS number returned is one the
register already holds.
"""
from __future__ import annotations

import sqlite3

DB = "manak_setu.db"

# Below this, the closest current standard is not close enough to name. Tuned
# against the gate the recommender already uses: an officer should not be handed
# a replacement the console would itself decline to recommend.
MIN_SUGGEST_SCORE = 0.45


def _conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def _row(conn, is_number: str):
    from engine import _is_base

    base = _is_base(is_number)
    return conn.execute(
        'SELECT * FROM standards WHERE "IS Base" = ? '
        'ORDER BY CASE "Status" WHEN "Current" THEN 0 ELSE 1 END LIMIT 1',
        (base,),
    ).fetchone()


def _live(conn, is_number: str):
    """The row for a designation, only if the register calls it Current."""
    from engine import _is_base

    return conn.execute(
        'SELECT * FROM standards WHERE "IS Base" = ? AND "Status" = "Current" LIMIT 1',
        (_is_base(is_number),),
    ).fetchone()


def successor_for(is_number: str, conn=None) -> dict:
    """The standard to cite instead, and where that answer came from."""
    from engine import _is_base

    close = conn is None
    conn = conn or _conn()
    try:
        row = _row(conn, is_number)
        if row is None:
            return {"found": False, "basis": "not_in_register", "for": is_number}
        status = row["Status"]
        if status == "Current":
            return {"found": False, "basis": "still_current", "for": is_number,
                    "note": "This standard is current. Nothing needs replacing."}

        # 1. BIS named it on this row.
        named = (row["Replaced By"] or "").strip()
        if named and named.upper() != "UNKNOWN":
            hit = _live(conn, named)
            return {
                "found": True, "basis": "bis_record", "for": is_number,
                "is_number": hit["IS Number"] if hit is not None else named,
                "title": hit["Full Title"] if hit is not None else None,
                "in_register": hit is not None,
                "why": "BIS records this standard as the successor.",
            }

        # 2. Some current standard's own row says it supersedes this one.
        base = _is_base(is_number)
        for cand in conn.execute(
            'SELECT * FROM standards WHERE "Status" = "Current" '
            'AND "Supersedes" IS NOT NULL AND "Supersedes" <> ""'
        ):
            parts = [p.strip() for p in str(cand["Supersedes"]).split(";") if p.strip()]
            if any(_is_base(p) == base for p in parts):
                return {
                    "found": True, "basis": "bis_record_inverted", "for": is_number,
                    "is_number": cand["IS Number"], "title": cand["Full Title"],
                    "in_register": True,
                    "why": f'{cand["IS Number"]} records that it supersedes this standard.',
                }

        # 3. Nobody recorded one. Offer the closest current standard, as a
        #    candidate, with the score that makes it one.
        title = (row["Full Title"] or "").strip()
        if not title:
            return {"found": False, "basis": "no_successor_recorded", "for": is_number,
                    "note": "BIS records no successor, and this standard has no title to match on."}
        import retrieval

        # Same product family first. Without it, IS 325 (three-phase induction
        # motors) returned a motor standard written for nuclear power plants:
        # a high score on a title that shares most of its words and almost none
        # of its scope. The family is the register's own grouping, so preferring
        # it is not a heuristic about the text.
        family = row["Product Family"]
        ranked = [c for c in retrieval.search(title)[:12]
                  if c.get("status") == "Current" and _is_base(c["is_number"]) != base]
        same = [c for c in ranked if family and c.get("product_family") == family]
        for cand in (same or ranked):
            score = float(cand.get("score") or 0)
            if score < MIN_SUGGEST_SCORE:
                break
            return {
                "found": True, "basis": "closest_current", "for": is_number,
                "is_number": cand["is_number"], "title": cand.get("title"),
                "in_register": True, "score": round(score, 4),
                "why": ("BIS records no successor. This is the closest current standard in the "
                        "register to the withdrawn one's own title — a candidate to check, "
                        "not a supersession."),
            }
        return {"found": False, "basis": "no_successor_recorded", "for": is_number,
                "note": ("BIS records no successor, and no current standard in the register is "
                         "close enough to this one's title to suggest.")}
    finally:
        if close:
            conn.close()


def successors_for(numbers: list[str]) -> dict[str, dict]:
    """One connection, many lookups."""
    conn = _conn()
    try:
        return {n: successor_for(n, conn) for n in numbers}
    finally:
        conn.close()
