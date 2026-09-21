"""Fetch catalogue metadata for standards cited by tenders but absent from the register.

Source: the BIS Standards Portal's own public search endpoint, the one its
website calls to power "Know Your Standards".

  POST https://standardsadmin.bis.gov.in/review-service/searchKnowStandards
  {"searchText": "IS 2026"}

This retrieves *catalogue metadata only* — number, title, publication date,
withdrawal status. It does not touch the standard's text, which BIS sells and
which we neither download nor redistribute (specification section 3).
standards.bis.gov.in/robots.txt permits this path: `Allow: /`, with only auth,
admin and survey routes disallowed.

Requests are issued one at a time with a delay. Rows that come back empty are
left out rather than filled with a placeholder.

Writes data/standards_collected.csv. Does not modify standards_master.csv.
"""

import argparse
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request

import pandas as pd

API = "https://standardsadmin.bis.gov.in/review-service/searchKnowStandards"
DEPT_API = "https://standardsadmin.bis.gov.in/master-service/fetchDepartmentList"
OUT = "data/standards_collected.csv"
DELAY_SECONDS = 0.7

# isStatus / withdrawStatus as returned by the portal.
STATUS_MAP = {1: "Current", 2: "Current", 3: "Superseded", 4: "Withdrawn"}


def _post(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Origin": "https://standards.bis.gov.in",
            "Referer": "https://standards.bis.gov.in/",
            "User-Agent": "Mozilla/5.0 (NiyamKosh SIH26108 research prototype)",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.loads(resp.read())


def departments() -> dict[int, str]:
    """BIS's own technical-department taxonomy, so families are official rather
    than something we invented."""
    data = _post(DEPT_API, {}).get("data") or []
    return {
        d["departmentId"]: f"{d.get('deptAliasName','').strip()} — {d.get('deptName','').strip()}"
        for d in data
    }


def fetch(search_text: str) -> list[dict]:
    payload = json.dumps({"searchText": search_text}).encode()
    req = urllib.request.Request(
        API,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Origin": "https://standards.bis.gov.in",
            "Referer": "https://standards.bis.gov.in/",
            "User-Agent": "Mozilla/5.0 (NiyamKosh SIH26108 research prototype)",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=40) as resp:
        body = json.loads(resp.read())
    return body.get("data") or []


ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6",
         "vii": "7", "viii": "8", "ix": "9", "x": "10"}


def variants(is_number: str) -> list[str]:
    """The same standard, spelled the ways the portal will actually answer to.

    Two whole classes of "not in the register" turned out to be spelling, not
    absence. Tenders write Roman part numbers — "IS 10553 (Part II)" — where BIS
    indexes Arabic. And tenders write "IS 60947" for standards BIS designates
    "IS/IEC 60947": adopted IEC texts carry the dual prefix, so a plain IS query
    returns nothing while IS/IEC returns 26 records."""
    out = [is_number]

    roman = re.sub(
        r"\b(part|sec(?:tion)?)[\s.-]*([ivx]+)\b",
        lambda m: f"{m.group(1)} {ROMAN.get(m.group(2).lower(), m.group(2))}",
        is_number, flags=re.I,
    )
    if roman != is_number:
        out.append(roman)

    # IEC-adopted numbers live in the 60000+ range and are published as IS/IEC.
    digits = re.sub(r"[^0-9]", "", base_of(is_number))
    if digits.isdigit() and int(digits) >= 60000:
        for form in list(out):
            out.append(re.sub(r"^IS\b", "IS/IEC", form, flags=re.I))

    seen, uniq = set(), []
    for v in out:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def base_of(is_number: str) -> str:
    return re.sub(r"\s+", " ", str(is_number).split("(")[0].split(":")[0]).strip()


def _digits(is_number: str) -> str:
    return re.sub(r"[^0-9]", "", base_of(is_number))


def pick(records: list[dict], wanted: str) -> dict | None:
    """Prefer an exact base match; the endpoint also returns fuzzy neighbours.
    Compares on digits so "IS 60947" matches BIS's "IS/IEC 60947"."""
    target = _digits(wanted)
    if not target:
        return None
    for r in records:
        if _digits(r.get("standardNumber", "")) == target:
            return r
    return None


def missing_from_graph(limit: int | None) -> list[str]:
    conn = sqlite3.connect("manak_setu.db")
    try:
        edges = conn.execute('SELECT "Source IS", "Target IS" FROM co_citation').fetchall()
        held = {r[0] for r in conn.execute('SELECT "IS Number" FROM standards')}
        held |= {r[0] for r in conn.execute('SELECT "IS Base" FROM standards')}
    finally:
        conn.close()

    degree: dict[str, int] = {}
    for a, b in edges:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
    absent = [s for s in degree if s not in held and base_of(s) not in held]
    absent.sort(key=lambda s: -degree[s])
    return absent[:limit] if limit else absent


def all_missing() -> list[str]:
    """Every IS number any real source references and the register does not hold:
    the coverage backlog, the certification rules, the co-citation graph, and the
    raw citations of all 220 tenders. Collecting against the union rather than the
    graph alone is the difference between filling the picture and filling one
    corner of it."""
    conn = sqlite3.connect("manak_setu.db")
    try:
        held = {r[0] for r in conn.execute('SELECT "IS Number" FROM standards')}
        held |= {r[0] for r in conn.execute('SELECT "IS Base" FROM standards')}

        wanted: set[str] = set()
        wanted |= {r[0] for r in conn.execute('SELECT "IS Number" FROM coverage_gap_backlog')}
        wanted |= {r[0] for r in conn.execute('SELECT "IS Number" FROM certification_rules')}
        wanted |= {r[0] for r in conn.execute('SELECT "IS Base" FROM certification_rules') if r[0]}
        for a, b in conn.execute('SELECT "Source IS", "Target IS" FROM co_citation'):
            wanted.add(a)
            wanted.add(b)
        for (v,) in conn.execute('SELECT "IS Numbers Cited" FROM tenders'):
            wanted |= {x.strip() for x in str(v or "").split(";") if x.strip()}
    finally:
        conn.close()

    return sorted(w for w in wanted if w not in held and base_of(w) not in held)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="most-connected N only")
    ap.add_argument("--all", action="store_true",
                    help="collect every gap across backlog, certification rules, graph and "
                         "raw tender citations, not just graph nodes")
    ap.add_argument("--out", default=OUT, help="where to write the collected rows")
    ap.add_argument("--resume", action="store_true",
                    help="skip IS numbers already present in --out")
    args = ap.parse_args()

    dept_names = departments()
    print(f"{len(dept_names)} BIS technical departments loaded")
    targets = all_missing() if args.all else missing_from_graph(args.limit)
    if args.limit and args.all:
        targets = targets[: args.limit]

    done: set[str] = set()
    if args.resume and os.path.exists(args.out):
        done = set(pd.read_csv(args.out)["Cited As"].astype(str))
        targets = [t for t in targets if t not in done]
        print(f"resuming — {len(done)} already collected")
    print(f"{len(targets)} standards cited by tenders but absent from the register\n")

    rows, misses = [], []
    for i, is_number in enumerate(targets, 1):
        try:
            hit = None
            for form in variants(is_number):
                hit = pick(fetch(form), is_number)
                if hit:
                    break
                time.sleep(DELAY_SECONDS)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"  [{i}/{len(targets)}] {is_number:<20} request failed: {type(exc).__name__}")
            misses.append(is_number)
            time.sleep(DELAY_SECONDS)
            continue

        if not hit:
            print(f"  [{i}/{len(targets)}] {is_number:<20} no catalogue match")
            misses.append(is_number)
        else:
            published = (hit.get("publishedOn") or "")[:4]
            status = "Withdrawn" if hit.get("withdrawStatus") else STATUS_MAP.get(
                hit.get("isStatus"), "Current"
            )
            rows.append(
                {
                    "IS Number": hit.get("standardNumber"),
                    "Full Title": hit.get("standardName"),
                    "Year": published,
                    "Status": status,
                    "Replaced By": "UNKNOWN",
                    "Supersedes": "UNKNOWN",
                    "Product Family": dept_names.get(
                        hit.get("departmentId"), "Unassigned department"
                    ),
                    "Priority": "Collected",
                    "Source Link": "https://standards.bis.gov.in/website/know-your-standards",
                    "IS Base": base_of(hit.get("standardNumber", "")),
                    "Cited As": is_number,
                    "Collected From": "BIS searchKnowStandards",
                    "Department Id": hit.get("departmentId"),
                    "Committee Id": hit.get("committeeId"),
                }
            )
            print(f"  [{i}/{len(targets)}] {is_number:<20} → {hit.get('standardName','')[:58]}")
        time.sleep(DELAY_SECONDS)

    if rows:
        fresh = pd.DataFrame(rows)
        if args.resume and os.path.exists(args.out):
            fresh = pd.concat([pd.read_csv(args.out), fresh], ignore_index=True)
            fresh = fresh.drop_duplicates(subset=["IS Number"], keep="first")
        fresh.to_csv(args.out, index=False)
    print(f"\ncollected {len(rows)} · no match {len(misses)}")
    print(f"wrote {args.out}" if rows else "nothing written")
    if misses:
        print("no catalogue record for:", ", ".join(misses[:15]) + ("…" if len(misses) > 15 else ""))


if __name__ == "__main__":
    main()
