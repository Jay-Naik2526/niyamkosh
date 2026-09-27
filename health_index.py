"""Procurement Standards Health Index — how healthy are the standards government
buyers are actually citing?

Every other measurement in this project asks whether the system is right. This
one asks something the system is uniquely placed to answer and nobody else can:
across real published government bids, how often does a tender cite a standard
BIS has already withdrawn or superseded, and which dead standards are still in
circulation?

That is not a retrieval metric. It is a procurement-policy measurement, and it
falls out of two things we already hold: a corpus of real tender documents with
their literal citations, and BIS's own status for each standard. Joining them
gives the Department of Consumer Affairs a demand signal it does not otherwise
have — which standards the procurement system leans on, and which of those are
overdue for revision.

Every figure carries its denominator. The corpus is a sample of Indian public
procurement, not a census, and the output says so: these are counts over the
documents we collected, never an estimate of the national rate.

    python health_index.py            # print the report
    python health_index.py --write    # also write data/health_index.json
"""

import argparse
import collections
import datetime
import json
import re
import sqlite3

DB = "manak_setu.db"
OUT = "data/health_index.json"
TOP_N = 15
# Below this a share is a fact about a handful of tenders, not about a buyer.
MIN_BUYER_DOCUMENTS = 10

# GeM bid numbers carry the year they were floated: GEM/2025/B/6183459.
YEAR_RE = re.compile(r"\b(20\d{2})\b")


def base_of(citation: str) -> str:
    return re.sub(r"\s+", " ", str(citation).split("(")[0].split(":")[0]).strip().upper()


def _register(conn) -> dict[str, dict]:
    """Status, title and successor by IS base number."""
    out: dict[str, dict] = {}
    for base, number, title, status, replaced, review, overdue in conn.execute(
        'SELECT "IS Base", "IS Number", "Full Title", "Status", "Replaced By", '
        '"Review Due", "Overdue" FROM standards'
    ):
        key = str(base).strip().upper()
        # First writer wins, and Current beats a dead edition of the same base:
        # a tender citing "IS 1554" without a part should not be called dead
        # because one part of it was withdrawn.
        if key not in out or (status == "Current" and out[key]["status"] != "Current"):
            out[key] = {
                "is_number": number, "title": title, "status": status,
                "replaced_by": replaced, "review_due": review, "overdue": overdue,
            }
    return out


def _year_of(tender_id: str) -> str:
    m = YEAR_RE.search(str(tender_id))
    return m.group(1) if m else "unknown"


def build() -> dict:
    conn = sqlite3.connect(DB)
    try:
        reg = _register(conn)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(tenders)")}
        buyer_cols = [c for c in ("Ministry", "Department") if c in columns]
        mark_col = "Demands Standard Mark" if "Demands Standard Mark" in columns else None
        extra = buyer_cols + ([mark_col] if mark_col else [])
        select = ", ".join(f'"{c}"' for c in
                           ["Tender ID", "Product Family", "IS Numbers Cited"] + extra
                           + ["Usability"])
        from engine import EVIDENCE, _has_citations
        marks = ", ".join("?" * len(EVIDENCE))
        rows = [r for r in conn.execute(
            f'SELECT {select} FROM tenders WHERE "Usability" IN ({marks})', EVIDENCE
        ).fetchall() if _has_citations(r[2])]

        # Which cited standards carry a compulsory certification duty. Read from
        # the certification register, never inferred from the product's wording.
        mandatory = {
            str(r[0]).strip().upper()
            for r in conn.execute(
                'SELECT "IS Number" FROM certification_rules '
                'WHERE "Certification Mandatory" = ?', ("Yes",))
            if r[0]
        }
        total_documents = conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
    finally:
        conn.close()

    dead_by_doc = collections.Counter()      # is_number -> documents citing it
    demand = collections.Counter()           # is_number -> documents citing it
    by_year = collections.defaultdict(lambda: [0, 0])     # year -> [docs, docs with a dead citation]
    by_family = collections.defaultdict(lambda: [0, 0])
    unresolved_docs = 0
    documents_with_dead = 0
    citations_total = 0

    # Who was buying. Only GeM rows carry this — the original 220-document set
    # has no bid form — so every buyer figure states its own denominator rather
    # than borrowing the corpus total.
    by_buyer = {c: collections.defaultdict(lambda: [0, 0, collections.Counter()])
                for c in buyer_cols}
    named = 0

    # The QCO enforcement gap. A document that cites a product under compulsory
    # BIS certification and demands the Standard Mark nowhere in its own text
    # accepts uncertified goods as written — which is the Department of Consumer
    # Affairs' own mandate failing at the point of purchase.
    #
    # Only the absence is reported. "No mark language anywhere" is unambiguous:
    # none of the phrases that demand a mark, a licence number or certified
    # material appear. The opposite is weak — a sixty-page tender mentioning BIS
    # somewhere is no proof that it demands the mark for the certified item — so
    # no figure here is built on the presence.
    qco_cited = qco_unprotected = qco_unknown = 0
    qco_by_family = collections.Counter()

    by_source = collections.defaultdict(lambda: [0, 0])   # usability -> [docs, docs with a dead citation]
    for row in rows:
        tender_id, family, cited = row[0], row[1], row[2]
        by_source[row[-1]][0] += 1
        buyers = dict(zip(buyer_cols, row[3:3 + len(buyer_cols)]))
        demands = str(row[3 + len(buyer_cols)] or "") if mark_col else ""
        citations = [c.strip() for c in str(cited or "").split(";") if c.strip()]
        citations_total += len(citations)
        year = _year_of(tender_id)
        fam = str(family or "Unclassified")
        by_year[year][0] += 1
        by_family[fam][0] += 1

        has_dead, has_unresolved = False, False
        seen_bases = set()
        for c in citations:
            base = base_of(c)
            if base in seen_bases:
                continue
            seen_bases.add(base)
            hit = reg.get(base)
            if not hit:
                has_unresolved = True
                continue
            demand[hit["is_number"]] += 1
            if hit["status"] in ("Withdrawn", "Superseded"):
                has_dead = True
                dead_by_doc[hit["is_number"]] += 1
        if mark_col and any(base_of(c) in mandatory for c in citations):
            qco_cited += 1
            if demands == "No":
                qco_unprotected += 1
                qco_by_family[fam] += 1
            elif demands != "Yes":
                qco_unknown += 1

        if any(str(v or "").strip() for v in buyers.values()):
            named += 1
        for col, value in buyers.items():
            name = str(value or "").strip()
            if name:
                by_buyer[col][name][0] += 1

        if has_dead:
            documents_with_dead += 1  # checked against engine.dead_citation_documents
            by_source[row[-1]][1] += 1
            by_year[year][1] += 1
            by_family[fam][1] += 1
            for col, value in buyers.items():
                name = str(value or "").strip()
                if name:
                    by_buyer[col][name][1] += 1
                    for c in citations:
                        hit = reg.get(base_of(c))
                        if hit and hit["status"] in ("Withdrawn", "Superseded"):
                            by_buyer[col][name][2][hit["is_number"]] += 1
        if has_unresolved:
            unresolved_docs += 1

    usable = len(rows)

    def rank(counter, limit=TOP_N):
        out = []
        for number, docs in counter.most_common(limit):
            hit = reg.get(base_of(number), {})
            out.append({
                "is_number": number,
                "title": hit.get("title"),
                "status": hit.get("status"),
                "replaced_by": (hit.get("replaced_by") or "UNKNOWN"),
                "review_due": hit.get("review_due") or "",
                "overdue": hit.get("overdue") or "",
                "documents": docs,
                "of": usable,
            })
        return out

    return {
        "generated": datetime.date.today().isoformat(),
        "corpus": {
            "documents_collected": total_documents,
            "documents_measured": usable,
            "citations_read": citations_total,
            "note": (
                "Counts over the tender documents in this corpus whose text was read "
                "and which cite a standard — from the text layer, or by OCR with every "
                "citation confirmed against the register. A sample of Indian public procurement, "
                "not a census — these are not national rates."
            ),
        },
        "headline": {
            "documents_with_a_dead_citation": documents_with_dead,
            "of_documents": usable,
            "by_source": [{"source": k, "documents": n, "with_dead_citation": d}
                          for k, (n, d) in sorted(by_source.items())],
            "documents_citing_a_standard_not_in_the_register": unresolved_docs,
            "distinct_dead_standards_in_circulation": len(dead_by_doc),
        },
        "certification_gap": {
            "documents_citing_a_compulsory_item": qco_cited,
            "of_documents": usable,
            # The rate belongs over the documents whose text was actually read.
            # Dividing by all 161 that cite a compulsory item, when 57 of them
            # were never scanned, reports a lower rate than the evidence
            # supports and hides that the check did not run on a third of them.
            "scanned": qco_cited - qco_unknown,
            "no_standard_mark_clause": qco_unprotected,
            "not_scanned": qco_unknown,
            "top_families": [{"family": f, "documents": n}
                             for f, n in qco_by_family.most_common(8)],
            "note": (
                "Documents citing at least one standard the certification register "
                "marks as compulsory, whose own attachment text contains no demand "
                "for the BIS Standard Mark, a licence number or certified material. "
                "Only the absence is counted: it is unambiguous, whereas a mention "
                "of BIS somewhere in a long tender is no proof that the certified "
                "item is covered."
            ),
        },
        "buyers": {
            "documents_naming_a_buyer": named,
            "of_documents": usable,
            "note": (
                "Read from the saved GeM bid form, exactly as the form prints it. "
                "Documents collected before the GeM sweep have no bid form and name "
                "no buyer, so they are excluded from every buyer figure rather than "
                "counted as unknown."
            ),
            **{
                col.lower(): [
                    {
                        "name": name,
                        "documents": n,
                        "with_dead_citation": dead,
                        "top_dead_standard": (top.most_common(1)[0][0] if top else None),
                        "top_dead_standard_documents": (top.most_common(1)[0][1] if top else 0),
                    }
                    # Ten documents is the floor for showing a buyer at all: a
                    # share over three documents is not a finding about a
                    # ministry, it is a fact about three tenders.
                    for name, (n, dead, top) in sorted(
                        by_buyer[col].items(), key=lambda kv: (-kv[1][1], -kv[1][0])
                    )
                    if n >= MIN_BUYER_DOCUMENTS
                ][:TOP_N]
                for col in buyer_cols
            },
        },
        "by_year": [
            {"year": y, "documents": n, "with_dead_citation": d}
            for y, (n, d) in sorted(by_year.items())
        ],
        "by_family": [
            {"family": f, "documents": n, "with_dead_citation": d}
            for f, (n, d) in sorted(by_family.items(), key=lambda kv: -kv[1][1])
            if n
        ][:TOP_N],
        "dead_standards_still_cited": rank(dead_by_doc),
        "most_cited_standards": rank(demand),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    d = build()

    c, h = d["corpus"], d["headline"]
    print(f"Procurement Standards Health Index · {d['generated']}")
    print(f"\ncorpus: {c['documents_measured']} machine-readable documents "
          f"of {c['documents_collected']} collected · {c['citations_read']} citations read")
    print(f"\ndocuments citing a withdrawn or superseded standard: "
          f"{h['documents_with_a_dead_citation']} of {h['of_documents']}")
    print(f"distinct dead standards still in circulation        : "
          f"{h['distinct_dead_standards_in_circulation']}")
    print(f"documents citing a standard not in the register     : "
          f"{h['documents_citing_a_standard_not_in_the_register']} of {h['of_documents']}")

    if d["dead_standards_still_cited"]:
        print("\ndead standards still being cited:")
        for r in d["dead_standards_still_cited"][:8]:
            print(f"  {r['documents']:>3} of {r['of']:<4} {r['is_number']:<24} "
                  f"{r['status']:<11} {str(r['title'])[:44]}")

    if d["most_cited_standards"]:
        print("\nmost-cited standards (the demand signal for revision priority):")
        for r in d["most_cited_standards"][:8]:
            flag = " · REVIEW OVERDUE" if str(r["overdue"]).lower() == "yes" else ""
            print(f"  {r['documents']:>3} of {r['of']:<4} {r['is_number']:<24} "
                  f"{str(r['title'])[:40]}{flag}")

    g = d.get("certification_gap") or {}
    if g.get("scanned"):
        print("\ncompulsory certification, not demanded:")
        print(f"  {g['no_standard_mark_clause']} of the {g['scanned']} documents that cite a "
              f"product under compulsory BIS")
        print("  certification and whose text could be read never demand the Standard Mark.")
        print(f"  ({g['documents_citing_a_compulsory_item']} cite such a product in all; "
              f"{g['not_scanned']} had no readable attachment)")
        for r in g.get("top_families") or []:
            print(f"    {r['documents']:>4}  {r['family'][:52]}")

    b = d.get("buyers") or {}
    if b.get("ministry"):
        print(f"\nby ministry (buyer named on {b['documents_naming_a_buyer']} of "
              f"{b['of_documents']} measured documents; "
              f"{MIN_BUYER_DOCUMENTS}+ documents each):")
        print(f"  {'documents':>9} {'with a dead citation':>21}   buyer")
        for r in b["ministry"][:10]:
            share = f"{r['with_dead_citation']} of {r['documents']}"
            print(f"  {r['documents']:>9} {share:>21}   {str(r['name'])[:44]}")

    if args.write:
        with open(OUT, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=2, ensure_ascii=False)
        print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
