"""The ingestion pipeline from the architecture, as one runnable thing.

    Harvest -> Extract -> Link References -> Track Versions -> Map Certification

Until now these existed as five separate scripts that a human ran in the right
order and remembered to follow with a database reload. That is not a pipeline,
it is a folder of scripts, and the architecture diagram claimed more than the
code delivered.

Every stage is idempotent: running twice changes nothing the second time. Every
run appends a record to data/pipeline_runs.jsonl saying what it found and what
it changed, so "when was this data last refreshed, and what moved?" is a
question with an answer.

Nothing here invents data. Stage 4 detects amendments by asking the BIS portal
about standards we already hold and comparing the answer to our stored record;
a difference is reported, and only written into the register when --apply is
passed. A pipeline that silently rewrote the register would be the same failure
mode this project exists to prevent.

    python pipeline.py                  # all stages, report only
    python pipeline.py --apply          # write detected amendments into the register
    python pipeline.py --only versions  # one stage
    python pipeline.py --watch 21600    # re-run every 6 hours
"""

import argparse
import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

DB_PATH = "manak_setu.db"
RUN_LOG = "data/pipeline_runs.jsonl"
AMENDMENTS = "data/amendments_detected.csv"
TENDER_DIR = "data/tenders"

STAGES = ("harvest", "extract", "link", "versions", "certification")


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _run_script(name: str, args: list[str] | None = None) -> tuple[bool, str]:
    """Stages that already exist as scripts are invoked, not reimplemented —
    one definition of each step, not two that can drift apart."""
    cmd = [sys.executable, name, *(args or [])]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


# ─────────────────────────────────────────────────────────── 1. harvest


def stage_harvest(apply_changes: bool) -> dict:
    """Catalogue metadata for standards that tenders cite and we do not hold."""
    conn = _conn()
    try:
        edges = conn.execute('SELECT "Source IS", "Target IS" FROM co_citation').fetchall()
        held = {r[0] for r in conn.execute('SELECT "IS Number" FROM standards')}
        held |= {r[0] for r in conn.execute('SELECT "IS Base" FROM standards')}
    finally:
        conn.close()

    base = lambda s: re.sub(r"\s+", " ", str(s).split("(")[0].split(":")[0]).strip()
    cited = {r[0] for r in edges} | {r[1] for r in edges}
    missing = sorted(c for c in cited if c not in held and base(c) not in held)

    if not missing:
        return {"stage": "harvest", "outcome": "nothing to collect",
                "graph_nodes_missing": 0, "changed": False}
    if not apply_changes:
        return {"stage": "harvest", "outcome": "would collect",
                "graph_nodes_missing": len(missing), "sample": missing[:8], "changed": False}

    ok, out = _run_script("collect_missing_standards.py")
    return {"stage": "harvest", "outcome": "collected" if ok else "failed",
            "graph_nodes_missing": len(missing), "changed": ok, "log": out[-400:]}


# ─────────────────────────────────────────────────────────── 2. extract


def stage_extract(apply_changes: bool) -> dict:
    """Re-read any tender documents present on disk.

    The corpus was extracted once and lives in tender_dataset.csv; the PDFs
    themselves are not redistributed with this repo. If an operator drops new
    documents into data/tenders/ this stage reads them."""
    if not os.path.isdir(TENDER_DIR):
        return {"stage": "extract", "outcome": "no document directory",
                "detail": f"create {TENDER_DIR}/ and add PDFs or .docx to ingest new tenders",
                "documents": 0, "changed": False}

    files = [f for f in os.listdir(TENDER_DIR) if f.lower().endswith((".pdf", ".docx"))]
    if not files:
        return {"stage": "extract", "outcome": "no new documents",
                "documents": 0, "changed": False}

    from engine import extract_document

    read, scanned, citations = 0, 0, set()
    for name in files:
        with open(os.path.join(TENDER_DIR, name), "rb") as fh:
            try:
                r = extract_document(fh.read(), name)
            except Exception:                                    # noqa: BLE001
                continue
        read += 1
        scanned += 1 if r.get("scanned") else 0
        citations.update(r.get("citations") or [])

    return {"stage": "extract", "outcome": "read", "documents": read,
            "scanned_unreadable": scanned, "distinct_citations": len(citations),
            "changed": False,
            "note": "Extraction is reported, not merged: adding rows to the tender "
                    "corpus is a reviewed step, not an automatic one."}


# ─────────────────────────────────────────────────────────── 3. link


def _edge_count() -> int:
    if not os.path.exists("data/co_citation_graph_full.csv"):
        return 0
    with open("data/co_citation_graph_full.csv") as fh:
        return sum(1 for _ in fh) - 1


def _graph_thresholds() -> list[str]:
    """The thresholds the graph on disk was actually built at.

    rebuild_graph.py's built-in defaults are stricter than this corpus was
    built with, so invoking it bare does not rebuild the graph - it replaces it
    with a much smaller one. The thresholds travel in graph_meta.json precisely
    so the chain can carry them, and the link stage carries them.
    """
    try:
        with open("data/graph_meta.json", encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return []
    flags = []
    for key, flag in (("min_co_citations", "--min-co"),
                      ("min_confidence", "--min-confidence"),
                      ("min_source_tenders", "--min-source")):
        if meta.get(key) is not None:
            flags += [flag, str(meta[key])]
    return flags


def stage_link(apply_changes: bool) -> dict:
    """Rebuild the co-citation graph from the tender corpus.

    This stage took `apply_changes` and never read it. Every other stage
    reports on a dry run and writes only when asked - extract says so in as
    many words - and this one rebuilt the graph every time it was called. Four
    recorded runs, all of them `apply: false`, cut the shipped graph from
    65,872 edges to 14,257 and left graph_meta.json describing thresholds the
    database had never been built at. A dry run that destroys 51,615 edges is
    not a dry run.

    So: on a dry run it reports what is there and what a rebuild would do, and
    touches nothing. When it does run, it passes the thresholds the graph was
    built at rather than the script's stricter defaults, which is the second
    half of the same bug - a rebuild that shrinks the graph is not a rebuild
    either.
    """
    before = _edge_count()
    flags = _graph_thresholds()
    if not apply_changes:
        return {"stage": "link", "outcome": "would rebuild",
                "edges_before": before, "edges_after": before,
                "thresholds": " ".join(flags) or "script defaults",
                "changed": False,
                "note": "The graph is rebuilt only when the run is applied. "
                        "Rebuilding rewrites the shipped edge list, its layout "
                        "and its recorded thresholds."}

    ok, out = _run_script("rebuild_graph.py", flags)
    after = _edge_count()
    return {"stage": "link", "outcome": "rebuilt" if ok else "failed",
            "edges_before": before, "edges_after": after,
            "thresholds": " ".join(flags) or "script defaults",
            "changed": ok and before != after,
            "log": out[-400:] if not ok else ""}


# ─────────────────────────────────────────────────────────── 4. versions


PART_RE = re.compile(r"part\s*([0-9ivx]+)|sec(?:tion)?\s*([0-9ivx]+)", re.I)


def _designation(is_number: str) -> tuple[str, tuple]:
    """Base digits plus every part/section token, so 'IS 10118 (Part 2)' and
    'IS 10118 (Part 1):2018' are recognised as different standards."""
    text = str(is_number or "")
    base = re.sub(r"[^0-9]", "", text.split("(")[0].split(":")[0])
    parts = tuple(
        (a or b).lower() for a, b in PART_RE.findall(text)
    )
    return base, parts


def _same_designation(held: str, portal: str) -> bool:
    """A portal record counts as *this* standard only if the part and section
    match exactly. Matching on the base number alone made Part 1's edition look
    like an amendment to Parts 2, 3 and 4 — a false amendment that --apply would
    have written straight into the register."""
    hb, hp = _designation(held)
    pb, pp = _designation(portal)
    return hb == pb and hp == pp


# On numbered amendments, for whoever looks next.
#
# BIS publishes Amendment No. 1, 2, 3 to a standard, and the problem statement
# asks for them. They are not available through any public endpoint we could
# reach, and the search was thorough enough to be worth recording:
#
#   standardsadmin.bis.gov.in/review-service/getAmendmentDetails
#       exists (its error carries "totalAmendments"), but rejects every payload
#       shape tried; the calling component is not in the public Angular bundle.
#
#   services.bis.gov.in/.../Indian_standards/getamendments
#       the old portal's endpoint. Contract recovered from its own page script:
#       POST {pk_is_id, con_date, numb_amendments}, where pk_is_id comes from
#       POST Elasticsearch/getsearchAjax {search, type: 1, wh: 0}. Both calls
#       work. getamendments returns [] for every standard tried, including
#       IS 456 and IS 1554, which certainly have amendments in print. The
#       sibling gazette and corrigenda endpoints return false.
#
# So the system tracks edition year, status and BIS's own review date, and says
# plainly in the interface that it does not track numbered amendments. Claiming
# otherwise would be the one thing this project must not do.


def stage_versions(apply_changes: bool, limit: int | None = None) -> dict:
    """Ask BIS about standards we hold, and compare with what we recorded.

    This is what makes the pipeline's "re-runs when BIS publishes an amendment"
    claim real rather than aspirational: a withdrawal or a new edition shows up
    here as a difference between the portal and our register."""
    import collect_missing_standards as bis

    conn = _conn()
    try:
        # Rows collected from the portal in this same run are already the portal's
        # answer; re-asking about them would spend minutes to confirm a copy of
        # itself. Only rows the register held independently are worth verifying.
        rows = conn.execute(
            'SELECT "IS Number", "IS Base", Year, Status, Provenance FROM standards '
            "WHERE Provenance IS NULL OR Provenance NOT LIKE 'BIS searchKnowStandards%' "
            'ORDER BY "IS Number"'
        ).fetchall()
    finally:
        conn.close()
    rows = rows[:limit] if limit else rows

    changes, checked, unreachable, mismatched = [], 0, 0, 0
    for r in rows:
        try:
            hit = bis.pick(bis.fetch(r["IS Base"] or r["IS Number"]), r["IS Number"])
        except Exception:                                        # noqa: BLE001
            unreachable += 1
            continue
        checked += 1
        if not hit:
            continue
        if not _same_designation(r["IS Number"], hit.get("standardNumber") or ""):
            mismatched += 1
            continue

        portal_status = "Withdrawn" if hit.get("withdrawStatus") else bis.STATUS_MAP.get(
            hit.get("isStatus"), "Current"
        )
        portal_number = hit.get("standardNumber") or ""
        # validUpto is the portal's own review date. It is not an amendment
        # number — BIS does not publish those here — but it is real version
        # information and worth recording rather than discarding.
        valid_upto = (hit.get("validUpto") or "")[:10]
        portal_year = ""
        m = re.search(r":\s*((?:19|20)\d{2})", portal_number)
        if m:
            portal_year = m.group(1)
        held_year = str(r["Year"] or "").split(".")[0]

        if portal_status != r["Status"]:
            changes.append({
                "is_number": r["IS Number"], "field": "Status",
                "held": r["Status"], "portal": portal_status,
                "withdrawn_on": hit.get("withdrawOn"),
                "valid_upto": valid_upto,
            })
        elif portal_year and held_year and portal_year != held_year:
            changes.append({
                "is_number": r["IS Number"], "field": "Year",
                "held": held_year, "portal": portal_year,
                "portal_number": portal_number,
                "valid_upto": valid_upto,
            })
        time.sleep(bis.DELAY_SECONDS)

    if changes:
        import pandas as pd
        pd.DataFrame(changes).to_csv(AMENDMENTS, index=False)

    applied = 0
    if changes and apply_changes:
        applied = _apply_amendments(changes)

    return {"stage": "versions", "outcome": "checked", "standards_checked": checked,
            "unreachable": unreachable,
            "skipped_part_mismatch": mismatched,
            "amendments_found": len(changes),
            "amendments_applied": applied, "report": AMENDMENTS if changes else None,
            "changed": applied > 0,
            "sample": changes[:5]}


def _apply_amendments(changes: list[dict]) -> int:
    """Write portal values into the register. Only reached with --apply, and only
    for fields BIS itself returned — never a value we inferred."""
    import pandas as pd

    path = "data/standards_master_extended.csv"
    df = pd.read_csv(path, encoding="utf-8-sig")
    applied = 0
    for ch in changes:
        mask = df["IS Number"] == ch["is_number"]
        if not mask.any():
            continue
        df.loc[mask, ch["field"]] = ch["portal"]
        df.loc[mask, "Provenance"] = (
            f"amended from BIS portal {datetime.date.today().isoformat()}"
        )
        applied += 1
    if applied:
        df.to_csv(path, index=False)
    return applied


# ─────────────────────────────────────────────────────────── 5. certification


def stage_certification(apply_changes: bool) -> dict:
    """Confirm every certification rule still points at a standard we hold."""
    conn = _conn()
    try:
        rules = conn.execute(
            'SELECT "IS Number", "IS Base", "Certification Mandatory" FROM certification_rules'
        ).fetchall()
        held = {r[0] for r in conn.execute('SELECT "IS Number" FROM standards')}
        held |= {r[0] for r in conn.execute('SELECT "IS Base" FROM standards')}
    finally:
        conn.close()

    orphans = [r["IS Number"] for r in rules
               if r["IS Number"] not in held and (r["IS Base"] or "") not in held]
    mandatory = sum(1 for r in rules if r["Certification Mandatory"] == "Yes")
    return {"stage": "certification", "outcome": "verified", "rules": len(rules),
            "mandatory": mandatory, "orphaned_rules": len(orphans),
            "sample_orphans": orphans[:6], "changed": False,
            "note": "An orphan is a certification duty whose standard is not in the "
                    "register — a collection gap, not a bad rule."}


# ─────────────────────────────────────────────────────────── refresh


def refresh_derived() -> dict:
    """Backlog, database and embeddings, in the only order that is correct."""
    steps = []
    for script, args in (("rebuild_backlog.py", None),
                         ("load_db.py", None),
                         ("build_embeddings.py", None)):
        ok, out = _run_script(script, args)
        steps.append({"script": script, "ok": ok, "tail": out.strip().splitlines()[-1:] })
        if not ok:
            break
    return {"stage": "refresh", "steps": steps,
            "changed": all(s["ok"] for s in steps)}


# ─────────────────────────────────────────────────────────── driver


def run(stages: tuple[str, ...], apply_changes: bool, refresh: bool,
        versions_limit: int | None) -> dict:
    started = _now()
    t0 = time.time()
    results = []

    fns = {
        "harvest": lambda: stage_harvest(apply_changes),
        "extract": lambda: stage_extract(apply_changes),
        "link": lambda: stage_link(apply_changes),
        "versions": lambda: stage_versions(apply_changes, versions_limit),
        "certification": lambda: stage_certification(apply_changes),
    }
    for name in stages:
        print(f"\n── {name}")
        try:
            r = fns[name]()
        except Exception as exc:                                 # noqa: BLE001
            r = {"stage": name, "outcome": "error", "error": f"{type(exc).__name__}: {exc}",
                 "changed": False}
        results.append(r)
        for k, v in r.items():
            if k != "stage":
                print(f"   {k}: {v}")

    changed = any(r.get("changed") for r in results)
    if refresh and changed:
        print("\n── refresh (derived artefacts)")
        r = refresh_derived()
        results.append(r)
        for s in r["steps"]:
            print(f"   {s['script']}: {'ok' if s['ok'] else 'FAILED'}")
    elif refresh:
        print("\n── refresh skipped: no stage changed anything")

    record = {
        "started": started, "finished": _now(), "seconds": round(time.time() - t0, 1),
        "stages": list(stages), "apply": apply_changes, "changed": changed,
        "results": results,
    }
    os.makedirs("data", exist_ok=True)
    with open(RUN_LOG, "a") as fh:
        fh.write(json.dumps(record) + "\n")
    print(f"\nrun logged to {RUN_LOG} · {record['seconds']}s · changed={changed}")
    return record


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=STAGES, help="run one stage")
    ap.add_argument("--apply", action="store_true",
                    help="write detected amendments into the register (default: report only)")
    ap.add_argument("--no-refresh", action="store_true",
                    help="skip backlog/database/embeddings rebuild")
    ap.add_argument("--versions-limit", type=int,
                    help="check only the first N standards against BIS (the full "
                         "register is 551 requests at 0.7s each)")
    ap.add_argument("--watch", type=int, metavar="SECONDS",
                    help="re-run on this interval until interrupted")
    args = ap.parse_args()

    stages = (args.only,) if args.only else STAGES
    while True:
        run(stages, args.apply, not args.no_refresh, args.versions_limit)
        if not args.watch:
            return 0
        print(f"\nsleeping {args.watch}s — next run at "
              f"{(datetime.datetime.now() + datetime.timedelta(seconds=args.watch)):%H:%M:%S}\n")
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            print("stopped")
            return 0


if __name__ == "__main__":
    sys.exit(main())
