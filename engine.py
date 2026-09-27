import json
import re as _re_module

from audit import primary_notification as _primary_notification
import sqlite3

import numpy as np
import pandas as pd

DB_PATH = "manak_setu.db"
LAYOUT_PATH = "data/graph_layout.json"
EMBEDDINGS_PATH = "standards_embeddings.npy"
IS_NUMBERS_PATH = "standards_embeddings_is_numbers.csv"
MODEL_NAME = "all-MiniLM-L6-v2"

_model = None
_embeddings = None
_is_numbers = None


def _get_model():
    global _model
    if _model is None:
        # Imported here, not at module scope: the import pulls torch, and every
        # part of this module that does not embed anything — lookups, stats,
        # extraction, the health index, peer citations — would otherwise pay
        # 400 MB for a model they never call.
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(MODEL_NAME)
    return _model


def _get_embeddings():
    global _embeddings, _is_numbers
    if _embeddings is None:
        _embeddings = np.load(EMBEDDINGS_PATH)
        _is_numbers = pd.read_csv(IS_NUMBERS_PATH, encoding="utf-8-sig")["IS Number"].tolist()
    return _embeddings, _is_numbers


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _is_base(is_number: str) -> str:
    return is_number.split("(")[0].strip()


# A well-formed designation: the prefix, then the number, and nothing odd in
# between. "IS -1" is not one — stripping its punctuation would leave "1", and
# a register that holds IS 1 would then resolve malformed input to a real
# standard. The fallback is for spelling variants, not for repairing garbage.
_DESIGNATION = _re_module.compile(
    r"^\s*IS(?:/(?:IEC|ISO))?\s*[:\s]?\s*0*(\d{1,6})\s*$", _re_module.I
)


# Half the part references in real tenders are written without brackets — "IS
# 1239 Part II", "IS 432 : Part 1 : 1982", "IS 13585 Part 1". The bracketed form
# was already handled by splitting on "(", so only this spelling fell through,
# and it fell through silently: the designation pattern saw trailing words and
# declined, so a citation of a standard the register holds came back not found.
_TRAILING_PART = _re_module.compile(
    r"[\s,:;/-]*\b(?:Parts?|Pt\.?|Sections?|Sec\.?)\b.*$", _re_module.I
)

# BIS designates an adopted international text by naming both: "IS 2403 : 2014 /
# ISO 606 : 2004". The Indian designation is the one this register holds, so the
# foreign half is dropped. The slash after digits is what distinguishes this
# from the "IS/IEC 60947" prefix, where the slash follows "IS" directly.
_DUAL_DESIGNATION = _re_module.compile(
    r"(?<=\d)\s*/\s*(?:ISO|IEC|EN|BS|ASTM|DIN|JIS)\b.*$", _re_module.I
)
# A footnote marker carried over from the notification table — "IS 8828 *".
_TRAILING_JUNK = _re_module.compile(r"[\s*+#†‡.,;:/-]+$")


def _is_digits(is_number: str) -> str:
    """The bare number, prefix and edition stripped: 'IS/IEC 60947 (Part 1):2020'
    and 'IS 60947' are the same standard and must resolve to the same row.

    Returns "" when the text is not a designation at all, so the number fallback
    declines rather than guessing."""
    # Strip the edition year only where one is actually written: splitting on
    # every colon turns "IS:694", which is how half of Indian tenders write it,
    # into "IS".
    head = str(is_number).split("(")[0]
    head = _DUAL_DESIGNATION.sub("", head)
    head = _TRAILING_PART.sub("", head)
    head = _re_module.sub(r":\s*(?:19|20)\d{2}\s*$", "", head)
    head = _TRAILING_JUNK.sub("", head)
    m = _DESIGNATION.match(head)
    return m.group(1) if m else ""


def _resolve_standard(conn, is_number: str):
    """exact -> base -> bare number. Returns (row, how_it_matched)."""
    row = conn.execute('SELECT * FROM standards WHERE "IS Number" = ?', (is_number,)).fetchone()
    if row is not None:
        return row, "exact"
    row = conn.execute(
        'SELECT * FROM standards WHERE "IS Base" = ?', (_is_base(is_number),)
    ).fetchone()
    if row is not None:
        return row, "is_base_fallback"
    digits = _is_digits(is_number)
    if digits:
        row = conn.execute(
            'SELECT * FROM standards WHERE "IS Digits" = ? ORDER BY "IS Number" LIMIT 1',
            (digits,),
        ).fetchone()
        if row is not None:
            return row, "number_fallback"
    return None, None


def _display(value):
    return value if pd.notna(value) else "N/A"


def _confidence_label(score: float) -> str:
    if score >= 0.65:
        return "High"
    if score >= 0.40:
        return "Medium"
    return "Low - route to BIS office"


def match_spec(query_text: str, top_k: int = 5) -> dict:
    model = _get_model()
    embeddings, is_numbers = _get_embeddings()

    query_vec = model.encode([query_text], normalize_embeddings=True)[0]
    scores = embeddings @ query_vec

    top_idx = np.argsort(scores)[::-1][:top_k]
    # The title travels with the match. The page used to look it up in a copy of
    # the whole register held in the browser, which is part of why the whole
    # register was being downloaded.
    titles = standard_titles([is_numbers[i] for i in top_idx])
    matches = [{"is_number": is_numbers[i], "score": round(float(scores[i]), 4),
                "title": titles.get(is_numbers[i], "")} for i in top_idx]

    top_score = matches[0]["score"] if matches else 0.0
    confidence = _confidence_label(top_score)

    result = {"matches": matches, "confidence": confidence}
    if confidence == "Low - route to BIS office":
        result["message"] = (
            "No confident match found for this spec. Top candidates are shown for "
            "reference only — route to a BIS office for manual verification."
        )
    return result


def check_dead_citation(is_number: str) -> dict:
    conn = _get_conn()
    try:
        row, _ = _resolve_standard(conn, is_number)
        if row is None:
            return {"found": False}

        status = row["Status"]
        if status in ("Superseded", "Withdrawn"):
            replaced_by = row["Replaced By"]
            return {
                "found": True,
                "dead": True,
                "status": status,
                "replaced_by": replaced_by if pd.notna(replaced_by) else "UNKNOWN",
            }
        return {"found": True, "dead": False, "status": status}
    finally:
        conn.close()


def related_standards(is_number: str, limit: int = 5) -> list[dict]:
    """Co-cited standards for this one.

    The graph is keyed on the numbers tenders actually write — "IS 1554" — while
    retrieval returns the register's designation, "IS 1554 (Part 1)". Matching
    only on the exact string returned nothing for every part-suffixed standard,
    so the co-citation panel sat empty on exactly the standards that have parts.
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            '''SELECT "Target IS", "Confidence", "Lift", "Evidence Statement"
               FROM co_citation
               WHERE "Source IS" = ? OR "Source IS" = ?
               ORDER BY "Confidence" DESC
               LIMIT ?''',
            (is_number, _is_base(is_number), limit),
        ).fetchall()
        if not rows:
            digits = _is_digits(is_number)
            rows = conn.execute(
                '''SELECT "Target IS", "Confidence", "Lift", "Evidence Statement"
                   FROM co_citation
                   WHERE REPLACE(REPLACE(SUBSTR("Source IS", 1,
                         CASE WHEN INSTR("Source IS", '(') > 0
                              THEN INSTR("Source IS", '(') - 1 ELSE LENGTH("Source IS") END),
                         'IS ', ''), ' ', '') = ?
                   ORDER BY "Confidence" DESC LIMIT ?''',
                (digits, limit),
            ).fetchall()
        return [
            {
                "target_is": r["Target IS"],
                "confidence": r["Confidence"],
                "lift": r["Lift"],
                "evidence_statement": r["Evidence Statement"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def check_certification(is_number: str) -> dict:
    conn = _get_conn()
    try:
        row = conn.execute(
            'SELECT * FROM certification_rules WHERE "IS Number" = ?', (is_number,)
        ).fetchone()
        if row is None:
            row = conn.execute(
                'SELECT * FROM certification_rules WHERE "IS Base" = ?', (_is_base(is_number),)
            ).fetchone()
        if row is None:
            return {"found": False}

        return {
            "found": True,
            "certification_mandatory": row["Certification Mandatory"],
            "scheme": row["Scheme"],
            # The operative order, not its amendment history — see
            # audit.primary_notification. The full chain stays available.
            "notification_reference": _primary_notification(row["Notification Reference"]),
            "notification_history": _display(row["Notification Reference"]),
        }
    finally:
        conn.close()


_LAYOUT: dict | None = None


def _layout() -> dict:
    """Node coordinates computed once by graph_layout.py.

    The browser used to settle this itself — 110 frames of physics over 319
    mutually repelling nodes — which was a quarter of a million DOM writes for a
    picture that never changes, and which came out differently on every visit
    because it depended on how many frames finished. Reading coordinates is the
    whole of the client's layout work now."""
    global _LAYOUT
    if _LAYOUT is None:
        try:
            with open(LAYOUT_PATH, encoding="utf-8") as fh:
                _LAYOUT = json.load(fh)
        except (OSError, ValueError):
            _LAYOUT = {}
    return _LAYOUT


# How many relationships each standard contributes to the drawing. The stored
# graph keeps every co-citation — that is the evidence, and the drawer reads it
# in full — but drawing all of it is neither useful nor fast: at the lowest
# thresholds the table holds over a hundred thousand rows, which is 7.6 MB and
# two and a half seconds before a pixel appears. Keeping each standard's
# best-evidenced partners shows every node and the relationships that actually
# say something, and the number is stated on screen so nobody mistakes the
# picture for the whole table.
DRAW_EDGES_PER_NODE = 12


def full_graph(node_limit: int | None = None, edge_limit: int | None = None,
               min_count: int = 0, per_node: int | None = None) -> dict:
    """The co-citation graph, ready to draw.

    Only the fields the renderer reads: an edge is two endpoints, a confidence
    and a count; a node is its identity, what it is, and where it sits. The
    evidence sentence and lift stay on `/standard`, where the drawer shows one
    standard's relationships in full — sending them for every edge cost 600 KB
    to render nothing.
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            'SELECT "Source IS", "Target IS", "Confidence", "Co-citation Count" '
            'FROM co_citation WHERE "Co-citation Count" >= ?', (min_count,)
        ).fetchall()

        # A caller that only needs a representative sample (the overview hero)
        # should not download the whole corpus.
        if node_limit:
            degree: dict[str, int] = {}
            for r in rows:
                degree[r["Source IS"]] = degree.get(r["Source IS"], 0) + 1
                degree[r["Target IS"]] = degree.get(r["Target IS"], 0) + 1
            keep = {k for k, _ in sorted(degree.items(), key=lambda kv: -kv[1])[:node_limit]}
            rows = [r for r in rows if r["Source IS"] in keep and r["Target IS"] in keep]
        if edge_limit:
            # By count, not confidence: confidence is a ratio, so a pair cited
            # together twice out of twice scores 1.0 and outranks a pair cited
            # together forty times out of fifty. The strongest evidence should
            # survive the trim.
            rows = sorted(rows, key=lambda r: -(r["Co-citation Count"] or 0))[:edge_limit]

        # The table stores a row per direction, because confidence is
        # directional — "cited alongside A in 18 of 20" is not the same claim as
        # "cited alongside B in 18 of 51". Drawing is not directional, so both
        # rows drew the same line on top of itself: half the graph's render cost
        # was producing pixels that were already there. One line per pair now,
        # keeping the better-evidenced direction so the tooltip still reads true.
        best: dict[tuple, object] = {}
        for r in rows:
            key = (r["Source IS"], r["Target IS"])
            key = key if key[0] <= key[1] else (key[1], key[0])
            held_row = best.get(key)
            if held_row is None or (r["Confidence"] or 0) > (held_row["Confidence"] or 0):
                best[key] = r
        rows = list(best.values())
        # Pairs available at this threshold, counted after deduplication and
        # before the cap — so the screen can say "N of M drawn" with an M that
        # responds to the filter. It used to be total // 2, which ignored
        # min_count entirely and reported the same M for every request.
        pairs = len(rows)

        # Each standard keeps its strongest relationships. Applied after the
        # deduplication so the cap counts lines, not table rows.
        cap = DRAW_EDGES_PER_NODE if per_node is None else per_node
        if cap and cap > 0:
            kept: list = []
            per: dict[str, int] = {}
            for r in sorted(rows, key=lambda r: -(r["Co-citation Count"] or 0)):
                a, b = r["Source IS"], r["Target IS"]
                if per.get(a, 0) >= cap and per.get(b, 0) >= cap:
                    continue
                per[a] = per.get(a, 0) + 1
                per[b] = per.get(b, 0) + 1
                kept.append(r)
            rows = kept

        node_ids = sorted({r["Source IS"] for r in rows} | {r["Target IS"] for r in rows})
        degree = {}
        for r in rows:
            degree[r["Source IS"]] = degree.get(r["Source IS"], 0) + 1
            degree[r["Target IS"]] = degree.get(r["Target IS"], 0) + 1

        # One query for every node's record instead of one query per node.
        held = {}
        if node_ids:
            marks = ",".join("?" * len(node_ids))
            for std in conn.execute(
                f'SELECT "IS Number", "IS Base", "Full Title", "Status", "Product Family" '
                f'FROM standards WHERE "IS Number" IN ({marks}) OR "IS Base" IN ({marks})',
                node_ids + node_ids,
            ):
                held.setdefault(std["IS Number"], std)
                held.setdefault(std["IS Base"], std)

        layout = _layout()
        nodes = []
        for node_id in node_ids:
            std = held.get(node_id) or held.get(_is_base(node_id))
            xy = layout.get(node_id)
            nodes.append({
                "id": node_id,
                "title": _display(std["Full Title"]) if std is not None else "N/A",
                "status": std["Status"] if std is not None else "Unknown",
                "product_family": _display(std["Product Family"]) if std is not None else "N/A",
                "degree": degree.get(node_id, 0),
                "x": xy[0] if xy else None,
                "y": xy[1] if xy else None,
            })

        edges = [
            {
                "source": r["Source IS"],
                "target": r["Target IS"],
                "confidence": r["Confidence"],
                "count": r["Co-citation Count"],
            }
            for r in rows
        ]
        total = conn.execute("SELECT COUNT(*) FROM co_citation").fetchone()[0]
        try:
            with open("data/graph_meta.json", encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            meta = {}

        return {
            "nodes": nodes,
            "edges": edges,
            "thresholds": meta,
            "total_edges": total,
            "distinct_pairs": pairs,
            "edges_per_node": cap,
            "truncated": len(edges) < pairs,
            "layout": "precomputed" if layout else "missing",
        }
    finally:
        conn.close()


def standard_titles(is_numbers: list[str]) -> dict[str, str]:
    """Titles for a set of IS numbers, resolved the same three ways as everything
    else — exact, base, then bare number."""
    out: dict[str, str] = {}
    conn = _get_conn()
    try:
        for n in is_numbers:
            row, _ = _resolve_standard(conn, n)
            if row is not None:
                out[n] = row["Full Title"] or ""
    finally:
        conn.close()
    return out


# The register grew from 2,087 rows to 27,687 and these endpoints did not
# change: /standards served 10.2 MB and /tenders 3.4 MB, the browser parsed all
# of it, and each view then displayed the first 250 rows. Searching, filtering
# and paging belong in SQL, where an index can do them.
LIST_PAGE_DEFAULT = 100
LIST_PAGE_MAX = 500


def _facets(conn, table: str, columns: tuple[str, ...]) -> dict[str, list[str]]:
    """Distinct values for the filter dropdowns.

    They used to be derived in the browser from the whole table, which is one of
    the reasons the whole table had to be downloaded."""
    out = {}
    for col in columns:
        out[col] = [
            r[0] for r in conn.execute(
                f'SELECT DISTINCT "{col}" FROM {table} '
                f'WHERE TRIM(COALESCE("{col}", "")) NOT IN ("", "N/A", "nan") '
                f'ORDER BY "{col}"'
            )
        ]
    return out


def _page(conn, table: str, columns: tuple[str, ...], *,
          q: str = "", search_columns: tuple[str, ...] = (),
          filters: dict[str, str] | None = None,
          sort: str = "", descending: bool = False,
          limit: int = LIST_PAGE_DEFAULT, offset: int = 0,
          facet_columns: tuple[str, ...] = ()) -> dict:
    """One page of a table, with the total the page was drawn from."""
    limit = max(1, min(int(limit or LIST_PAGE_DEFAULT), LIST_PAGE_MAX))
    offset = max(0, int(offset or 0))

    where, params = [], []
    for col, value in (filters or {}).items():
        if value:
            where.append(f'"{col}" = ?')
            params.append(value)

    term = (q or "").strip()
    if term and search_columns:
        clauses = [f'"{c}" LIKE ? COLLATE NOCASE' for c in search_columns]
        params += [f"%{term}%"] * len(search_columns)
        # "IS:694", "IS 694" and "694" are the same citation, so a search for
        # any of them finds the row. Same normalisation the resolver uses.
        digits = _is_digits(term) or _re_module.sub(r"[^0-9]", "", term)
        if digits and "IS Digits" in _columns(conn, table):
            clauses.append('"IS Digits" = ?')
            params.append(digits)
        where.append("(" + " OR ".join(clauses) + ")")

    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM {table}{sql_where}", params).fetchone()[0]

    order = ""
    if sort and sort in columns:
        order = f' ORDER BY "{sort}" {"DESC" if descending else "ASC"}'
    picked = ", ".join(f'"{c}"' for c in columns)
    rows = conn.execute(
        f"SELECT {picked} FROM {table}{sql_where}{order} LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()

    out = {
        "total": total,
        "offset": offset,
        "limit": limit,
        "rows": [{k: _display(r[k]) for k in r.keys()} for r in rows],
    }
    if facet_columns and offset == 0:
        out["facets"] = _facets(conn, table, facet_columns)
    return out


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def list_standards(q: str = "", status: str = "", family: str = "",
                   sort: str = "", descending: bool = False,
                   limit: int = LIST_PAGE_DEFAULT, offset: int = 0) -> dict:
    conn = _get_conn()
    try:
        return _page(
            conn, "standards",
            ("IS Number", "Full Title", "Year", "Status", "Replaced By", "Product Family"),
            q=q, search_columns=("IS Number", "Full Title"),
            filters={"Status": status, "Product Family": family},
            sort=sort, descending=descending, limit=limit, offset=offset,
            facet_columns=("Status", "Product Family"),
        )
    finally:
        conn.close()


def list_certifications(q: str = "", scheme: str = "", family: str = "",
                        sort: str = "", descending: bool = False,
                        limit: int = LIST_PAGE_DEFAULT, offset: int = 0) -> dict:
    conn = _get_conn()
    try:
        page = _page(
            conn, "certification_rules",
            ("IS Number", "Product Description", "BIS Product Category",
             "Certification Mandatory", "Scheme", "Notification Reference"),
            q=q, search_columns=("IS Number", "Product Description", "Scheme"),
            filters={"Scheme": scheme, "Product Family": family},
            sort=sort, descending=descending, limit=limit, offset=offset,
            facet_columns=("Scheme", "Product Family"),
        )
        # The stored reference is the order plus its whole amendment history —
        # up to 2,771 characters. The table shows the order.
        for row in page["rows"]:
            full = row.get("Notification Reference") or ""
            row["Notification History"] = full
            row["Notification Reference"] = _primary_notification(full) or "N/A"
        return page
    finally:
        conn.close()


def _tender_title(row) -> tuple[str, bool]:
    """GeM prints the bid's own product line; that is the document's real name.
    A filename-derived title is the fallback, and `derived` says which."""
    from tender_titles import display_title

    keys = row.keys()
    category = (row["Item Category"] if "Item Category" in keys else "") or ""
    if str(category).strip() and str(category).strip().lower() != "nan":
        return str(category).strip(), True
    name = display_title(row["Tender ID"], row["Product Family"])
    return name["title"], name["derived"]


def list_tenders(q: str = "", usability: str = "", family: str = "",
                 sort: str = "", descending: bool = False,
                 limit: int = LIST_PAGE_DEFAULT, offset: int = 0) -> dict:
    conn = _get_conn()
    try:
        cols = _columns(conn, "tenders")
        picked = tuple(c for c in (
            "Tender ID", "Product Family", "Count", "Any Outdated", "Usability",
            "Item Category", "Source Link", "IS Numbers Cited",
        ) if c in cols)
        search = tuple(c for c in ("Tender ID", "Item Category", "Product Family") if c in cols)
        page = _page(
            conn, "tenders", picked,
            q=q, search_columns=search,
            filters={"Usability": usability, "Product Family": family},
            sort=sort, descending=descending, limit=limit, offset=offset,
            facet_columns=("Usability", "Product Family"),
        )
        raw = {r["Tender ID"]: r for r in conn.execute(
            f'SELECT * FROM tenders WHERE "Tender ID" IN '
            f'({",".join("?" * len(page["rows"]))})',
            [r["Tender ID"] for r in page["rows"]],
        )} if page["rows"] else {}
        # The table used to print "Any Outdated", the flag written when the
        # document was collected. That flag is a historical record the benchmark
        # depends on — it is deliberately not updated — so on this screen it read
        # "Not checked" for 3,790 of 4,917 rows and stated a stale Yes/No for the
        # rest. A corpus screen should say what the register says now.
        #
        # So the verdict is computed here, per row, against the live register.
        # Where no text was ever read there are no citations to check, and the
        # row says that rather than pretending the question is open.
        dead, current = _dead_sets(conn)
        for row in page["rows"]:
            source = raw.get(row["Tender ID"])
            title, derived = _tender_title(source) if source is not None else (row["Tender ID"], False)
            row["Title"] = title
            row["title_derived"] = derived
            # Read from the source row, not the page row: the page has been
            # through _display, which renders an empty field as the string
            # "N/A" — and "N/A" split on ";" is one non-empty citation.
            cited = str((source["IS Numbers Cited"] if source is not None
                         else row.get("IS Numbers Cited")) or "")
            if not [c for c in cited.split(";") if c.strip()]:
                row["Dead Now"] = "No citations"
                row["Dead Now Count"] = 0
            else:
                hits = dead_citations_in(cited, dead, current)
                row["Dead Now"] = "Yes" if hits else "No"
                row["Dead Now Count"] = len(hits)
        return page
    finally:
        conn.close()


def get_tender(tender_id: str) -> dict:
    """One tender in full — what the corpus drawer needs without the corpus."""
    conn = _get_conn()
    try:
        row = conn.execute(
            'SELECT * FROM tenders WHERE "Tender ID" = ?', (tender_id,)
        ).fetchone()
        if row is None:
            return {"found": False, "tender_id": tender_id}
        d = {k: _display(row[k]) for k in row.keys()}
        d["Title"], d["title_derived"] = _tender_title(row)
        d["found"] = True
        return d
    finally:
        conn.close()


def list_backlog() -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            'SELECT "Tenders Citing", "IS Number" FROM coverage_gap_backlog '
            'ORDER BY "Tenders Citing" DESC'
        ).fetchall()
        return [{"tenders_citing": r["Tenders Citing"], "is_number": r["IS Number"]} for r in rows]
    finally:
        conn.close()


def standard_detail(is_number: str) -> dict:
    conn = _get_conn()
    try:
        row, matched_by = _resolve_standard(conn, is_number)
        if row is None:
            return {"found": False, "is_number": is_number}
        detail = {k: _display(row[k]) for k in row.keys()}
    finally:
        conn.close()

    return {
        "found": True,
        "matched_by": matched_by,
        "standard": detail,
        "certification": check_certification(is_number),
        "related": related_standards(is_number, limit=12),
    }


def _count_by(conn, table: str, column: str) -> list[dict]:
    rows = conn.execute(
        f'SELECT "{column}" AS k, COUNT(*) AS n FROM {table} '
        f'GROUP BY "{column}" ORDER BY n DESC'
    ).fetchall()
    return [{"key": r["k"] if r["k"] is not None else "N/A", "count": r["n"]} for r in rows]


def _dead_sets(conn) -> tuple[set, set]:
    """The two sets every live dead-citation answer is decided against: bases
    BIS has withdrawn or superseded, and bases it still publishes as Current.

    One copy, because this is the fact the whole console turns on. It used to be
    inlined in the health index while the Tenders table read a flag written at
    collection time — which is the same defect in a different costume, and it is
    what had one screen saying 275 and another 422.
    """
    dead = {
        r["IS Base"] for r in conn.execute(
            'SELECT "IS Base" FROM standards WHERE "Status" IN ("Withdrawn", "Superseded")'
        ) if r["IS Base"]
    }
    current = {
        r["IS Base"] for r in conn.execute(
            'SELECT "IS Base" FROM standards WHERE "Status" = "Current"'
        ) if r["IS Base"]
    }
    return dead, current


def dead_citations_in(cited: str, dead: set, current: set) -> list[str]:
    """Which of a document's cited standards the register now calls dead.

    A base with a Current edition is not dead: one withdrawn part of IS 1554
    does not make every citation of IS 1554 outdated.
    """
    hits = []
    for citation in {c.strip() for c in str(cited or "").split(";") if c.strip()}:
        if _is_base(citation) in dead and _is_base(citation) not in current:
            hits.append(citation)
    return sorted(hits)


# The documents whose text was actually read — from the text layer, or by OCR
# on this machine with every citation confirmed against the register. The
# headline, the co-citation graph and the tender counts all measure this one
# population, so no two of them can describe a different corpus.
#
# Coverage and the benchmark stay on the text layer alone: an OCR citation is
# kept only if the register resolves it, so counting OCR rows in coverage would
# score the register against citations pre-filtered to match it.
EVIDENCE = ("Usable", "Read by OCR")
_EVIDENCE_SQL = '"Usability" IN (%s)' % ", ".join("'%s'" % u for u in EVIDENCE)


def _has_citations(cited) -> bool:
    return bool(str(cited or "").strip()) and str(cited).strip().lower() != "nan"


def evidence_breakdown(conn=None) -> dict:
    """Documents read and documents citing a dead standard, per source."""
    close = conn is None
    conn = conn or _get_conn()
    try:
        dead, current = _dead_sets(conn)
        out = {u: {"documents": 0, "with_dead": 0} for u in EVIDENCE}
        for usability, cited in conn.execute(
            f'SELECT "Usability", "IS Numbers Cited" FROM tenders WHERE {_EVIDENCE_SQL}'
        ):
            if not _has_citations(cited):
                continue
            out[usability]["documents"] += 1
            if dead_citations_in(cited, dead, current):
                out[usability]["with_dead"] += 1
        return out
    finally:
        if close:
            conn.close()


def dead_citation_documents(conn=None) -> int:
    """Machine-readable documents citing a standard BIS has withdrawn or
    superseded, counted against the register as it is now.

    The corpus carries an "Any Outdated" flag written when each document was
    collected, and it is stale by construction: the catalogue harvest took the
    register from 2,087 standards to 27,687, so citations that could not be
    resolved then resolve now — and 147 documents that the flag calls clean are
    not. The Tenders screen read the flag and the health index computed this,
    which is two numbers for one fact. Both now call here.
    """
    close = conn is None
    conn = conn or _get_conn()
    try:
        dead, current = _dead_sets(conn)
        count = 0
        for (cited,) in conn.execute(
            f'SELECT "IS Numbers Cited" FROM tenders WHERE {_EVIDENCE_SQL}'
        ):
            if _has_citations(cited) and dead_citations_in(cited, dead, current):
                count += 1
        return count
    finally:
        if close:
            conn.close()


def corpus_stats() -> dict:
    """Every figure here is recomputed from the current database on each call —
    never copied from a previous run or a written report."""
    conn = _get_conn()
    try:
        usable_rows = conn.execute(
            'SELECT "IS Numbers Cited" FROM tenders WHERE "Usability" = ?', ("Usable",)
        ).fetchall()
        cited = set()
        for r in usable_rows:
            if r["IS Numbers Cited"]:
                for part in str(r["IS Numbers Cited"]).split(";"):
                    part = part.strip()
                    if part:
                        cited.add(part)

        std_rows = conn.execute(
            'SELECT "IS Number", "IS Base", "IS Digits" FROM standards'
        ).fetchall()
        exact_set = {r["IS Number"] for r in std_rows}
        base_set = {r["IS Base"] for r in std_rows}
        # Same three-step resolution the lookups and the backlog use. Without the
        # digit fallback here, this figure said 97.1% while the backlog said 99.0%
        # — two numbers for one fact, which is the failure this project is about.
        digit_set = {r["IS Digits"] for r in std_rows if r["IS Digits"]}
        matched = {
            c for c in cited
            if c in exact_set or _is_base(c) in base_set or _is_digits(c) in digit_set
        }

        years = conn.execute(
            'SELECT "Year" AS y, COUNT(*) AS n FROM standards GROUP BY "Year"'
        ).fetchall()
        decades: dict[str, int] = {}
        for r in years:
            try:
                decade = f"{int(r['y']) // 10 * 10}s"
            except (TypeError, ValueError):
                decade = "Unknown"
            decades[decade] = decades.get(decade, 0) + r["n"]

        edges = conn.execute(
            'SELECT "Source IS" AS s, "Target IS" AS t FROM co_citation'
        ).fetchall()
        degree: dict[str, int] = {}
        for e in edges:
            degree[e["s"]] = degree.get(e["s"], 0) + 1
            degree[e["t"]] = degree.get(e["t"], 0) + 1
        top_degree = sorted(degree.items(), key=lambda kv: kv[1], reverse=True)[:10]

        backlog_top = conn.execute(
            'SELECT "Tenders Citing" AS n, "IS Number" AS is_number FROM coverage_gap_backlog '
            'ORDER BY n DESC LIMIT 12'
        ).fetchall()

        evidence = evidence_breakdown(conn)
        return {
            "row_counts": {
                "standards": conn.execute("SELECT COUNT(*) FROM standards").fetchone()[0],
                "tenders": conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0],
                "co_citation": conn.execute("SELECT COUNT(*) FROM co_citation").fetchone()[0],
                "certification_rules": conn.execute(
                    "SELECT COUNT(*) FROM certification_rules"
                ).fetchone()[0],
                "coverage_gap_backlog": conn.execute(
                    "SELECT COUNT(*) FROM coverage_gap_backlog"
                ).fetchone()[0],
            },
            "standards_by_status": _count_by(conn, "standards", "Status"),
            "standards_by_family": _count_by(conn, "standards", "Product Family"),
            "standards_by_priority": _count_by(conn, "standards", "Priority"),
            "standards_by_decade": sorted(
                [{"key": k, "count": v} for k, v in decades.items()], key=lambda d: d["key"]
            ),
            "tenders_by_usability": _count_by(conn, "tenders", "Usability"),
            "tenders_by_family": _count_by(conn, "tenders", "Product Family"),
            "certs_by_scheme": _count_by(conn, "certification_rules", "Scheme"),
            "certs_by_mandatory": _count_by(conn, "certification_rules", "Certification Mandatory"),
            "coverage": {
                "usable_tenders": len(usable_rows),
                # Computed against the register as it is now, not read from the
                # flag stored at collection time — see dead_citation_documents.
                "any_outdated": sum(b["with_dead"] for b in evidence.values()),
                "evidence_tenders": sum(b["documents"] for b in evidence.values()),
                "evidence_breakdown": evidence,
                "evidence_note": (
                    "Documents whose text was read and which cite at least one "
                    "standard: the text layer, plus scans read by OCR whose "
                    "citations the register confirms."
                ),
                "distinct_cited": len(cited),
                "matched": len(matched),
                "unmatched": len(cited) - len(matched),
                "pct": round(100 * len(matched) / len(cited), 1) if cited else 0.0,
                "denominator_note": (
                    "Distinct IS numbers cited across tenders with Usability='Usable' only. "
                    "Tenders marked Multi-scope / Not extractable / Extraction failed are excluded."
                ),
            },
            "graph": {
                "nodes": len(degree),
                # The table stores a row per direction because confidence is
                # directional. len(edges) is therefore rows, not relationships,
                # and reporting it as "graph edges" doubled the size of the
                # graph on the hero — 65,872 against the 35,806 pairs the Graph
                # screen names for the same data. One fact, one number.
                "edges": len({tuple(sorted((e["s"], e["t"]))) for e in edges}),
                "edge_rows": len(edges),
                "top_degree": [{"is_number": k, "degree": v} for k, v in top_degree],
            },
            "backlog_top": [
                {"is_number": r["is_number"], "tenders_citing": r["n"]} for r in backlog_top
            ],
        }
    finally:
        conn.close()


def run_benchmark(sample_size: int = 20, seed: int = 42) -> dict:
    """Re-runs the golden benchmark live. Reports true/false positives and
    negatives explicitly — never a bare accuracy percentage."""
    import random

    conn = _get_conn()
    try:
        rows = conn.execute(
            'SELECT "Tender ID", "IS Numbers Cited", "Any Outdated" FROM tenders '
            "WHERE \"Usability\" = 'Usable' AND \"Any Outdated\" IN ('Yes','No')"
        ).fetchall()
    finally:
        conn.close()

    yes_rows = [r for r in rows if r["Any Outdated"] == "Yes"]
    no_rows = [r for r in rows if r["Any Outdated"] == "No"]
    rng = random.Random(seed)
    sampled_no = rng.sample(no_rows, min(sample_size, len(no_rows)))
    evaluated = yes_rows + sampled_no

    results = []
    tp = tn = fp = fn = 0
    for r in evaluated:
        cited = [s.strip() for s in str(r["IS Numbers Cited"] or "").split(";") if s.strip()]
        predicted = "No"
        hits = []
        for is_number in cited:
            check = check_dead_citation(is_number)
            if check.get("found") and check.get("dead"):
                predicted = "Yes"
                hits.append({"is_number": is_number, **check})
        actual = r["Any Outdated"]
        match = predicted == actual
        if actual == "Yes" and predicted == "Yes":
            tp += 1
        elif actual == "No" and predicted == "No":
            tn += 1
        elif actual == "No" and predicted == "Yes":
            fp += 1
        else:
            fn += 1
        results.append(
            {
                "tender_id": r["Tender ID"],
                "actual": actual,
                "predicted": predicted,
                "match": match,
                "dead_hits": hits,
            }
        )

    return {
        "evaluated": len(evaluated),
        "positives_in_set": len(yes_rows),
        "negatives_sampled": len(sampled_no),
        # Kept under their old names so nothing that reads this breaks, but they
        # are agreement counts, not accuracy counts — see `what_this_measures`.
        "true_positives": tp,
        "true_negatives": tn,
        "false_positives": fp,
        "false_negatives": fn,
        "agree": tp + tn,
        "newly_dead": fp,
        "flag_says_dead_register_does_not": fn,
        "mismatches": [r for r in results if not r["match"]],
        "what_this_measures": (
            "Agreement between the Any Outdated flag stored when each document was "
            "collected and what the register says today — not the system's accuracy. "
            "The flag was written against a smaller register, so a disagreement "
            "usually means the register has since learned that a cited standard is "
            "withdrawn, not that the check is wrong."
        ),
        "honest_summary": (
            f"{tp + tn} of {len(evaluated)} documents agree with the flag stored when they "
            f"were collected. {fp} now cite a standard the register has since recorded as "
            f"withdrawn or superseded — the flag calls them clean and the register does not. "
            f"{fn} are flagged dead but resolve as current today. This measures how far the "
            "register has moved since collection; it is not an accuracy figure for the check."
        ),
        "results": results,
    }


# "as per relevant IS" is tender boilerplate meaning "whichever Indian Standard
# applies" — it names nothing. In a table it is followed by the next row's
# number, and a pattern that reads "conforming to relevant IS 10." as a citation
# of IS 10 invents one. In one real GeM specification 59 of 97 matches were this,
# numbered consecutively 10 through 21: row numbers, not standards. Short IS
# numbers cannot simply be dropped — IS 10 through IS 25 are real BIS standards —
# so the generic phrase itself is what is excluded.
GENERIC_IS_PHRASE = r"(?<!\brelevant )(?<!\bapplicable )(?<!\brespective )(?<!\bappropriate )(?<!\bany other )"
# The designation is always capitalised: BIS writes "IS 4985", never "is 4985".
# Matching case-insensitively made the pattern match the English verb, and tender
# documents are full of it — "to prove that he is 11 satisfying the eligibility
# criteria" became a citation of IS 11, and "the margin of purchase preference is
# 20%" became IS 20. Across the corpus that made IS 20 look like one of the most
# cited standards in Indian procurement.
#
# The word boundaries matter for the same reason: without them THIS 20, BASIS 12
# and AXIS 400 all contain a match. (?<![A-Za-z]) rather than \b because the
# character before must not be a letter specifically — a full stop or a bracket
# is fine.
# IS/IEC and IS/ISO are how BIS designates adopted international texts, and
# tenders cite them that way. Neither was ever matched before; the number is the
# same one the register holds, so the prefix is consumed and the digits kept.
# The digit run is captured whole, not capped at six. Capping it silently
# truncated: tender PDFs routinely lose the separator between the number and the
# edition year, so "IS:2016-1967" comes out of pdfplumber as "IS:20161967", and
# a six-digit cap turned that into a citation of "IS 201619" — a designation
# that exists in no document and no register. Twelve of the twenty-seven rows in
# the coverage backlog were this, not a missing standard.
IS_CITATION_PATTERN = (
    GENERIC_IS_PHRASE
    + r"(?<![A-Za-z])IS(?:/(?:IEC|ISO))?(?![A-Za-z])[:\s]*(\d{2,})(?:\s*\(([^)]{0,40})\))?"
)

# An IS number is at most six digits. Anything longer is a run whose separator
# the PDF dropped.
_MAX_IS_DIGITS = 6
_GLUED_YEAR = _re_module.compile(r"^(\d{2,6})((?:19|20)\d{2})$")


def _ungloss_digits(digits: str) -> str:
    """The IS number inside a digit run, or "" when it cannot be read.

    Splitting off a trailing edition year is parsing, not inference: "IS 2062 :
    2006" and "IS:20622006" are the same citation written with and without the
    separator, and the year is stripped everywhere else in this module anyway.
    Anything else over six digits is not a designation we can read, and the
    honest output is no citation at all — a wrong number is worse than a
    missing one, because a wrong one is quietly counted as a coverage gap.
    """
    if len(digits) <= _MAX_IS_DIGITS:
        return digits
    m = _GLUED_YEAR.match(digits)
    return m.group(1) if m else ""


def peer_citations(text: str, documents: int = 30, limit: int = 12) -> dict:
    """What other government buyers of a similar item actually cited.

    Every other answer this system gives is derived from the register: what BIS
    publishes, what supersedes what, which standards co-occur. This one is not
    derived at all — it is a tally of what real procurement officers wrote when
    buying the same kind of thing, taken from the item category GeM prints on
    each bid.

    It is useful precisely where the register is silent. A buyer purchasing an
    11 kV cable joint has no way to know that most comparable bids also cite the
    conductor and insulation standards; the corpus does. And when peers are
    citing something withdrawn, that shows too, because a common practice being
    wrong is worth seeing.

    Matching is BM25 over the category text, so it is a similarity over words the
    buyers themselves used. A query with no similar bids returns nothing rather
    than the most-cited standards overall, which would be a corpus average
    dressed up as a recommendation.
    """
    state = _peer_index()
    if not state["rows"]:
        return {"found": False, "reason": "no categorised documents in the corpus"}
    tokens = _peer_tokens(text)
    if not tokens:
        return {"found": False, "reason": "no usable words in the query"}

    scores = state["bm25"].get_scores(tokens)
    order = np.argsort(scores)[::-1][:documents]
    best = float(scores[order[0]]) if len(order) else 0.0
    # A single weak match is not a peer group. "photocopier paper" shares one
    # common word with a substation bid and would otherwise come back holding
    # earthquake and transformer standards, which is worse than saying nothing.
    matched = [state["rows"][i] for i in order
               if scores[i] > 0 and scores[i] >= best * PEER_SCORE_FLOOR]
    if len(matched) < PEER_MIN_DOCUMENTS:
        return {"found": False,
                "reason": f"fewer than {PEER_MIN_DOCUMENTS} comparable bids in the corpus"}

    # Tally by the standard each citation resolves to, not by how it was spelled.
    # "IS 10322", "IS 10322 (Part 1)" and "IS 10322 (Part-1)" are one standard,
    # and listing them separately splits its count three ways.
    conn = _get_conn()
    try:
        dead_set, current_set = _dead_sets(conn)
        tally: dict[str, dict] = {}
        # Which of the comparable bids are repeating a dead citation, and which
        # dead standard each one repeated. This is the whole point of the peer
        # group: an officer does not write a specification from a blank page,
        # they open the last tender for the same item and copy its clause. That
        # is how a withdrawn standard survives a decade — and it is the one
        # moment where naming the bad precedent actually prevents the mistake.
        repeats: list[dict] = []
        for row in matched:
            seen = set()
            for citation in {c.strip() for c in str(row["cited"] or "").split(";") if c.strip()}:
                hit, _ = _resolve_standard(conn, citation)
                key = hit["IS Number"] if hit is not None else citation
                if key in seen:
                    continue
                seen.add(key)
                entry = tally.setdefault(key, {
                    "is_number": key,
                    "title": hit["Full Title"] if hit is not None else None,
                    "status": hit["Status"] if hit is not None else None,
                    "in_register": hit is not None,
                    "documents": 0,
                    "of": len(matched),
                })
                entry["documents"] += 1
            bad = dead_citations_in(row["cited"], dead_set, current_set)
            if bad:
                repeats.append({
                    "tender_id": row["tender_id"],
                    "buyer": row["buyer"] or None,
                    "category": row["category"][:110],
                    "link": row["link"] if row["link"] and row["link"] != "N/A" else None,
                    "dead": sorted(bad)[:6],
                })
        # Dead first, then by how widely it is cited. Sorting purely by count
        # buries the finding under six correct standards nobody needs warning
        # about — the officer is looking at this list to find the trap in it.
        rank = {"Withdrawn": 0, "Superseded": 0}
        out = sorted(tally.values(),
                     key=lambda e: (rank.get(e["status"], 1), -e["documents"], e["is_number"]))[:limit]
    finally:
        conn.close()

    repeats.sort(key=lambda r: -len(r["dead"]))
    return {
        "found": True,
        "matched_documents": len(matched),
        "examples": [r["category"][:110] for r in matched[:4]],
        "citations": out,
        "repeating_a_dead_citation": len(repeats),
        "repeats": repeats[:8],
        "buyers": sorted({r["buyer"] for r in matched if r["buyer"]})[:6],
        "note": (
            f"Counted across {len(matched)} bid documents in this corpus whose item "
            "category is textually similar to the query. A tally of what buyers "
            "cited, not advice about what to cite."
        ),
    }


# A peer group needs more than one document, and the documents in it have to be
# comparably similar — not merely the least dissimilar thing in the corpus.
PEER_MIN_DOCUMENTS = 3
PEER_SCORE_FLOOR = 0.35


def _peer_tokens(text: str) -> list[str]:
    return _re_module.findall(r"[a-z0-9]+", str(text or "").lower())


_PEER: dict = {}


def _peer_index() -> dict:
    """BM25 over the item categories of usable, categorised documents."""
    if _PEER:
        return _PEER
    from rank_bm25 import BM25Okapi

    conn = _get_conn()
    try:
        rows = [
            # The document's own identity travels with it. The tally alone says
            # "five of thirty bids cite a superseded standard", which an officer
            # has no way to act on or to check. Naming the five, with the buyer
            # and the link to the published document, turns a claim into
            # evidence they can open in another tab.
            {"category": str(r["Item Category"]), "cited": r["IS Numbers Cited"],
             "tender_id": r["Tender ID"], "buyer": r["Ministry"],
             "link": r["Source Link"]}
            for r in conn.execute(
                'SELECT "Item Category", "IS Numbers Cited", "Tender ID", '
                '"Ministry", "Source Link" FROM tenders '
                'WHERE "Usability" = ? AND TRIM(COALESCE("Item Category", "")) <> ""',
                ("Usable",),
            )
        ]
    finally:
        conn.close()
    _PEER.update(
        rows=rows,
        bm25=BM25Okapi([_peer_tokens(r["category"]) for r in rows]) if rows else None,
    )
    return _PEER


def extract_citations(text: str) -> list[str]:
    """Literal substring extraction of IS-number citations from supplied text.
    Nothing is inferred — a citation only appears if it is written in the text.
    Whitespace is collapsed and spelling variants of the same part reference
    (e.g. "Part-2/ Sec.-1" vs "Part 2/Sec 1") collapse to one entry."""
    import re

    found = []
    seen = set()
    # Deliberately case-sensitive: see IS_CITATION_PATTERN.
    for match in re.finditer(IS_CITATION_PATTERN, text):
        number = _ungloss_digits(match.group(1))
        if not number:
            continue
        raw_part = re.sub(r"\s+", " ", match.group(2)).strip() if match.group(2) else ""
        citation = f"IS {number} ({raw_part})" if raw_part else f"IS {number}"
        key = f"{number}|{re.sub(r'[^a-z0-9]', '', raw_part.lower())}"
        if key not in seen:
            seen.add(key)
            found.append(citation)
    return found


SCANNED_CHARS_PER_PAGE = 200
"""Below this a page is almost certainly a scan: pdfplumber returns the page's
text layer, and a photographed page has none. Tenders from smaller state PSUs are
routinely scans, and silently returning zero citations for one would be the worst
possible failure — it reads as 'this tender is clean'."""

MAX_PAGES = 60


def extract_pdf(file_bytes: bytes) -> dict:
    import io

    import pdfplumber

    pages = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages[:MAX_PAGES]:
            pages.append(page.extract_text() or "")
    text = "\n".join(pages)
    per_page = len(text) / len(pages) if pages else 0.0
    scanned = bool(pages) and per_page < SCANNED_CHARS_PER_PAGE

    return {
        "format": "pdf",
        "page_count": page_count,
        "pages_read": len(pages),
        "characters": len(text),
        "chars_per_page": round(per_page, 1),
        "scanned": scanned,
        "scanned_note": (
            f"Only {per_page:.0f} characters per page were recoverable, below the "
            f"{SCANNED_CHARS_PER_PAGE}-character threshold. This document is very likely a scan "
            "with no text layer. Citations found below are whatever text existed and are almost "
            "certainly incomplete — do not read an empty result as a clean tender. Run OCR, or "
            "paste the specification text directly."
        ) if scanned else None,
        "text": text[:20000],
        "citations": extract_citations(text),
    }


def extract_docx(file_bytes: bytes) -> dict:
    """Tenders arrive as .docx as often as .pdf, and the tables are where the
    citations live — a paragraph-only read misses most of them."""
    import io

    import docx

    document = docx.Document(io.BytesIO(file_bytes))
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    table_count = 0
    for table in document.tables:
        table_count += 1
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    text = "\n".join(parts)

    return {
        "format": "docx",
        "page_count": None,
        "paragraphs": len(document.paragraphs),
        "tables": table_count,
        "characters": len(text),
        "scanned": False,
        "scanned_note": None,
        "text": text[:20000],
        "citations": extract_citations(text),
    }


PDF_MAGIC = b"%PDF"
ZIP_MAGIC = b"PK\x03\x04"  # .docx is a zip container


def extract_document(file_bytes: bytes, filename: str | None = None) -> dict:
    """Sniff the container rather than trusting the extension — a .pdf that is
    really a .docx is a real thing that happens on procurement portals."""
    if file_bytes.startswith(PDF_MAGIC):
        result = extract_pdf(file_bytes)
    elif file_bytes.startswith(ZIP_MAGIC):
        result = extract_docx(file_bytes)
    else:
        raise ValueError(
            "Unrecognised file. Upload a PDF or a .docx, or paste the specification text."
        )
    result["filename"] = filename
    return result
