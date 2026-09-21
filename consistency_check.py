"""Assert the invariants this project's honesty rests on, and fail loudly.

Every serious bug in NiyamKosh has had the same shape: two places held the
same fact, one of them changed, and nothing noticed. The citation matcher had
two copies. The extraction pattern had two. The `Any Outdated` flag held a
pre-harvest answer while the register held a current one, and the two screens
that read them disagreed by 147 documents.

None of those were caught by a test, because each copy was individually
correct. What was wrong was the relationship between them. This script checks
relationships: the backlog against the coverage figure, the graph against the
register, the stored citations against what the current pattern reads out of
the documents they came from.

It imports its matcher from `engine`. A checker with its own copy of
`_is_base` would be the very bug it exists to find.

    python consistency_check.py             # all checks
    python consistency_check.py --sample 80 # re-extract more attachments
    python consistency_check.py --quick     # skip the re-extraction (no PDFs)

Exits non-zero if any check fails, so CI can gate a push on it.
"""

import argparse
import csv
import glob
import os
import random
import sqlite3
import sys

import engine

DB = "manak_setu.db"
PDF_DIR = os.path.join("data", "tenders", "gem")
GOLDEN = os.path.join("data", "golden_queries.csv")
SEED = 11

results: list[tuple[str, bool, list[str]]] = []


def check(name):
    """Register a check. The function returns (ok, detail lines)."""
    def wrap(fn):
        def run(*a, **k):
            try:
                ok, lines = fn(*a, **k)
            except Exception as exc:                          # noqa: BLE001
                ok, lines = False, [f"raised {type(exc).__name__}: {exc}"]
            results.append((name, ok, lines))
        run.__name__ = fn.__name__
        return run
    return wrap


def _cited_rows(conn):
    return conn.execute(
        'SELECT "Tender ID", "IS Numbers Cited" FROM tenders WHERE "Usability" = ?',
        ("Usable",),
    ).fetchall()


def _split(cited):
    return [c.strip() for c in str(cited or "").split(";") if c.strip()]


# ── 1. the backlog is exactly the set coverage says is missing ──────────────
@check("backlog equals the unmatched set coverage reports")
def check_backlog(conn):
    cited = set()
    for _, raw in _cited_rows(conn):
        cited.update(_split(raw))
    matched = {c for c in cited if engine._resolve_standard(conn, c)[0] is not None}
    # The backlog stores the citation as the document wrote it, part reference
    # and all — "IS 1802 (Part-I)" — so compare like with like rather than
    # reducing one side to its base.
    unmatched = cited - matched

    backlog = {str(r[0]).strip() for r in
               conn.execute('SELECT "IS Number" FROM coverage_gap_backlog')}
    only_backlog = backlog - unmatched
    only_coverage = unmatched - backlog
    lines = [
        f"distinct citations {len(cited)} · resolved {len(matched)} · unmatched bases {len(unmatched)}",
        f"backlog rows {len(backlog)}",
    ]
    if only_backlog:
        lines.append(f"in the backlog but now resolvable: {sorted(only_backlog)[:8]}")
    if only_coverage:
        lines.append(f"unmatched but missing from the backlog: {sorted(only_coverage)[:8]}")
    return not (only_backlog or only_coverage), lines


# ── 2. the graph is drawn entirely from rows the register holds ─────────────
@check("every graph endpoint is held in the register or declared as a gap")
def check_graph(conn):
    """A node that resolves to nothing is not automatically a bug. The graph is
    built from what real tenders cite, and real tenders cite numbers BIS never
    issued — so an unresolvable node is honest *provided* it is also declared in
    the coverage backlog. What would be dishonest is a node that resolves to
    nothing and is claimed nowhere."""
    edges = conn.execute('SELECT "Source IS", "Target IS" FROM co_citation').fetchall()
    nodes = {n for e in edges for n in e}
    backlog = {str(r[0]).strip() for r in
               conn.execute('SELECT "IS Number" FROM coverage_gap_backlog')}
    unresolved = {n for n in nodes if engine._resolve_standard(conn, n)[0] is None}
    undeclared = sorted(n for n in unresolved
                        if n not in backlog and engine._is_base(n) not in backlog)
    lines = [f"{len(edges)} edges over {len(nodes)} distinct standards",
             f"{len(unresolved)} unresolved, of which {len(unresolved) - len(undeclared)} are declared gaps"]
    if undeclared:
        lines.append(f"undeclared: {undeclared[:8]}")
    return not undeclared, lines


# ── 2b. a run that was not applied changed nothing ─────────────────────────
@check("no unapplied pipeline run reports a change")
def check_dry_runs_wrote_nothing(conn):
    """A pipeline run that was not applied must not have changed anything.

    Every stage takes `apply_changes`, and every stage but one read it. The
    link stage rebuilt the co-citation graph whenever it was called, so a dry
    run rewrote the shipped edge list, its layout and its recorded thresholds -
    and because rebuild_graph.py's built-in defaults are stricter than this
    corpus was built at, "rebuilding" meant replacing 65,872 edges with 14,257.
    It happened four times before anything noticed, and what finally noticed
    was check_graph_meta, one screen away from the damage.

    The run log records apply and changed for every stage, so it can be asked
    directly. This reads the record rather than the code, which means it stays
    true if the stage is ever rewritten.

    Four runs in the log did exactly this, and the log is a record of what
    happened - it is not edited to make a check pass. So the four are reported
    and the check fails only on a run recorded after the fix landed. That
    cut-off is a real event with a commit behind it, not a number chosen to
    make the suite green: move it later and you are hiding a regression, and
    the comment is here so that is obvious to whoever tries.
    """
    import json
    import os

    FIXED_AT = "2026-09-21T15:18:00"

    path = os.path.join("data", "pipeline_runs.jsonl")
    if not os.path.exists(path):
        return True, ["no pipeline run recorded"]

    offenders = []
    historic = []
    runs = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                run = json.loads(line)
            except ValueError:
                continue
            runs += 1
            if run.get("apply"):
                continue
            for stage in run.get("results", []):
                if not stage.get("changed"):
                    continue
                started = str(run.get("started") or "")
                note = (f"{started} · {stage.get('stage')} changed on a dry run"
                        + (f" ({stage.get('edges_before'):,} to {stage.get('edges_after'):,} edges)"
                           if stage.get("edges_before") is not None else ""))
                (offenders if started > FIXED_AT else historic).append(note)

    lines = [f"{runs} recorded runs"]
    if historic:
        lines.append(f"{len(historic)} before the fix, kept on the record:")
        lines += historic[-3:]
    if offenders:
        lines += offenders[-3:]
        return False, lines
    lines.append(f"no unapplied run since {FIXED_AT} reports a change")
    return True, lines


# ── 2c. the graph's stated thresholds are the ones it was built at ─────────
@check("graph_meta.json matches the graph actually in the database")
def check_graph_meta(conn):
    """The Graph screen prints the thresholds from data/graph_meta.json as the
    filter that produced what is on screen. Nothing made that file agree with
    the table beside it.

    It drifted exactly as you would expect. rebuild_graph.py's built-in defaults
    are stricter than the corpus was built at, so a bare run rewrote the meta to
    "2+ co-citations / 20%+ confidence" while the 65,872 permissive edges stayed
    in the database. The screen then spent a session describing a filter that
    had never been applied to the data it was drawing.

    The table knows the answer: the loosest edge in it *is* the threshold.
    """
    import json

    try:
        with open("data/graph_meta.json", encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError) as e:
        return False, [f"graph_meta.json unreadable: {e}"]

    row = conn.execute(
        'SELECT MIN("Co-citation Count"), MIN(Confidence), COUNT(*) FROM co_citation'
    ).fetchone()
    if row is None or row[2] == 0:
        return False, ["co_citation is empty"]
    min_count, min_conf, total = row[0], row[1], row[2]

    lines = [f"{total:,} edges in the table",
             f'meta says min_co_citations={meta.get("min_co_citations")}, '
             f'min_confidence={meta.get("min_confidence")}',
             f"loosest edge present is count={min_count}, confidence={min_conf}"]
    ok = True
    if meta.get("min_co_citations") is not None and min_count < meta["min_co_citations"]:
        ok = False
        lines.append(f'  the table holds an edge cited {min_count} time(s), below the '
                     f'{meta["min_co_citations"]} the screen claims')
    if meta.get("min_confidence") is not None and min_conf + 1e-9 < meta["min_confidence"]:
        ok = False
        lines.append(f'  the table holds an edge at confidence {min_conf}, below the '
                     f'{meta["min_confidence"]} the screen claims')
    return ok, lines


# ── 2d. the citation that makes a duty binding is a whole citation ─────────
@check("every certification notification reads as a complete citation")
def check_notifications_complete(conn):
    """The Certification screen names the gazette order that makes a product's
    BIS certification compulsory, and the repair writes the same string into a
    tender. So it has to be a citation an officer can act on.

    It was not. primary_notification() trims the amendment history off the
    stored reference, but its date matcher only knew numeric dates, so every
    rule that spelled its month out fell through to a blunt 180-character cut
    and stopped wherever the 180th character landed: 154 of 737 rules ended
    mid-word, on strings like "S.O. 165(E) dated 5 Fe".

    A reference may still be shortened - some run to 2,771 characters - but a
    shortened one ends on a word and says so with an ellipsis. What this
    forbids is the silent cut, which reads as a citation and is not one.
    """
    from audit import primary_notification

    rows = conn.execute(
        'SELECT "IS Number", "Notification Reference" FROM certification_rules'
    ).fetchall()
    cut, shortened, blank = [], 0, 0
    for row in rows:
        shown = primary_notification(row["Notification Reference"])
        if not shown:
            blank += 1
            continue
        if shown.endswith("\u2026"):
            shortened += 1
            continue
        # The test is not what the citation ends with - plenty end on a year,
        # and a first predicate that demanded a closing bracket called six
        # complete references truncated. It is whether the stop landed inside a
        # word: take what is shown, find it in the source it was cut from, and
        # look at the character that comes next. A letter or digit there means
        # the last token was still running when the cut fired.
        raw = " ".join(str(row["Notification Reference"] or "").split())
        at = raw.find(shown)
        if at >= 0:
            nxt = raw[at + len(shown): at + len(shown) + 1]
            if nxt and (nxt.isalnum() or nxt in "-/"):
                cut.append(f'{row["IS Number"]}: ...{shown[-30:]}|{raw[at+len(shown):at+len(shown)+10]}')

    lines = [f"{len(rows)} rules · {blank} carry no reference",
             f"{shortened} shortened and marked with an ellipsis"]
    if cut:
        lines.append(f"{len(cut)} stop mid-citation:")
        lines += cut[:4]
        return False, lines
    lines.append("none stop mid-citation")
    return True, lines


# ── 3. every citation is either held or declared as a gap ──────────────────
@check("every citation in a usable tender is held or declared missing")
def check_citations_accounted(conn):
    backlog = {str(r[0]).strip() for r in
               conn.execute('SELECT "IS Number" FROM coverage_gap_backlog')}
    stray = {}
    for tender_id, raw in _cited_rows(conn):
        for c in _split(raw):
            if engine._resolve_standard(conn, c)[0] is not None:
                continue
            if c in backlog or engine._is_base(c) in backlog:
                continue
            stray.setdefault(c, tender_id)
    lines = [f"{len(stray)} citations neither resolved nor declared"]
    if stray:
        lines += [f"  {c} — first seen in {t}" for c, t in list(stray.items())[:8]]
    return not stray, lines


# ── 4. no stored citation is malformed ─────────────────────────────────────
@check("every stored citation is a well-formed designation")
def check_wellformed(conn):
    bad = {}
    for tender_id, raw in _cited_rows(conn):
        for c in _split(raw):
            if not engine._is_digits(c):
                bad.setdefault(c, tender_id)
    lines = [f"{len(bad)} citations the designation pattern refuses"]
    if bad:
        lines += [f"  {c!r} — {t}" for c, t in list(bad.items())[:10]]
    return not bad, lines


# ── 5. the stored citations are what the current pattern reads ─────────────
@check("re-reading the saved attachments reproduces the stored citations")
def check_reextraction(conn, sample):
    if sample <= 0:
        return True, ["skipped (--quick)"]
    try:
        import pdfplumber
    except ImportError:
        return True, ["skipped — pdfplumber is not installed"]

    # The attachments are saved under the GeM bid id, which is a different
    # number from the Tender ID — deriving one from the other's digits turned
    # "GEM/2025/B/6232081" into "20256232081" and found no files, so this check
    # silently verified one document instead of fifty. Silence from a check is
    # not the same as a pass, so it now reports how many it actually read and
    # fails if that is nothing.
    columns = {r[1] for r in conn.execute("PRAGMA table_info(tenders)")}
    key = "GeM Bid Id" if "GeM Bid Id" in columns else None
    if not key:
        return True, ["no GeM Bid Id column — attachments cannot be located"]
    rows = [r for r in conn.execute(
        f'SELECT "{key}", "IS Numbers Cited" FROM tenders WHERE "Usability" = ?',
        ("Usable",)) if str(r[0]).strip() and str(r[0]).lower() != "nan"]
    random.Random(SEED).shuffle(rows)

    checked, diffs = 0, []
    for bid, raw in rows:
        try:
            digits = str(int(float(bid)))
        except (TypeError, ValueError):
            continue
        paths = [p for p in sorted(glob.glob(os.path.join(PDF_DIR, f"{digits}-*.pdf")))
                 if not p.endswith("-bid.pdf")]
        if not paths:
            continue
        found: list[str] = []
        for path in paths:
            try:
                with pdfplumber.open(path) as pdf:
                    text = "\n".join((pg.extract_text() or "")
                                     for pg in pdf.pages[:engine.MAX_PAGES])
            except Exception:                                 # noqa: BLE001
                continue
            for c in engine.extract_citations(text):
                if c not in found:
                    found.append(c)
        stored = _split(raw)
        checked += 1
        if set(found) != set(stored):
            diffs.append((digits,
                          sorted(set(found) - set(stored)),
                          sorted(set(stored) - set(found))))
        if checked >= sample:
            break

    lines = [f"re-read {checked} documents of the {sample} asked for, from {PDF_DIR}"]
    if not checked:
        lines.append("no saved attachments found — nothing was verified")
        return False, lines
    for tid, extra, gone in diffs[:6]:
        lines.append(f"  {tid}: pattern now reads {extra or '—'}, stored has {gone or '—'}")
    if diffs:
        lines.append(f"  {len(diffs)} of {checked} documents differ")
    return not diffs, lines


# ── 6. the evaluation set is answerable against this register ──────────────
@check("every expected answer in the golden set is in the register")
def check_golden(conn):
    if not os.path.exists(GOLDEN):
        return False, [f"{GOLDEN} is missing"]
    gap, unreadable, total = [], [], 0
    with open(GOLDEN, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            expected = (row.get("expected_is") or "").strip()
            if not expected:
                continue
            total += 1
            if engine._resolve_standard(conn, expected)[0] is not None:
                continue
            # Two different faults wear the same shape here. A label the matcher
            # can read that the register does not hold is a real coverage gap
            # and must fail. A label the matcher refuses is a malformed label —
            # BIS's own notification writing "IS 2141:20005" — which no register
            # could hold, and which is a data-quality note, not a broken
            # invariant. Failing on those would leave CI permanently red and
            # teach everyone to ignore it.
            (gap if engine._is_digits(expected) else unreadable).append(expected)
    lines = [f"{total} query/standard pairs",
             f"{len(gap)} name a readable standard the register does not hold",
             f"{len(unreadable)} are labels the matcher refuses to read"]
    if gap:
        lines.append(f"  gaps: {sorted(set(gap))[:8]}")
    if unreadable:
        lines.append(f"  unreadable: {sorted(set(unreadable))[:8]}")
    return not gap, lines


# ── 7. what /health reports is what the tables contain ─────────────────────
@check("the stats endpoint's row counts match live COUNT(*)")
def check_health(conn):
    reported = engine.corpus_stats()["row_counts"]
    lines, ok = [], True
    for table, said in reported.items():
        actual = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        lines.append(f"{table:<22} reported {said:>7,} · actual {actual:>7,}")
        if said != actual:
            ok = False
            lines[-1] += "   MISMATCH"
    return ok, lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=50,
                    help="how many saved attachments to re-extract (default 50)")
    ap.add_argument("--quick", action="store_true",
                    help="skip the re-extraction check")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        check_backlog(conn)
        check_graph(conn)
        check_graph_meta(conn)
        check_notifications_complete(conn)
        check_dry_runs_wrote_nothing(conn)
        check_citations_accounted(conn)
        check_wellformed(conn)
        check_reextraction(conn, 0 if args.quick else args.sample)
        check_golden(conn)
        check_health(conn)
    finally:
        conn.close()

    print("Consistency check\n" + "=" * 17)
    for name, ok, lines in results:
        print(f"\n{'PASS' if ok else 'FAIL'}  {name}")
        for line in lines:
            print(f"        {line}")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)} passed · {len(failed)} failed")
    if failed:
        print("failing: " + "; ".join(failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
