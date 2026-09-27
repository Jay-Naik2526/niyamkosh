"""Rebuild the co-citation graph from tender_dataset.csv.

The existing graph was computed over the 134 `Usability = Usable` tenders only.
The project specification computes it over every tender that contains citations
(161 documents), which is why the spec quotes ~3,718 edges and the shipped file
holds 376. Same source data, wider base.

Nothing here is generated: every edge is a count of two IS numbers appearing in
the same real tender document. Writes a new file and leaves the original alone.
"""

import itertools
import json
from collections import Counter

import pandas as pd

SRC = "data/tender_dataset.csv"
OUT = "data/co_citation_graph_full.csv"
META = "data/graph_meta.json"

# Evidence thresholds. These decide how much repetition a pair needs before the
# graph will claim the two standards go together — they are the graph's standard
# of proof, not a display setting, so they are printed with every rebuild and
# shown on the screen that draws the result.
#
# Lower values admit weakly-evidenced pairs. That is a legitimate choice for an
# operator who wants to see the whole corpus rather than only its core, which is
# why they are settable here; but an edge backed by two documents is a much
# weaker claim than one backed by forty, and the renderer must keep that visible
# rather than drawing both the same.
MIN_CO_CITATIONS = 2
MIN_CONFIDENCE = 0.20
MIN_SOURCE_TENDERS = 2


def citation_sets(df: pd.DataFrame) -> list[set[str]]:
    """One document, one set of citations — over the same population every other
    figure in this project measures.

    The graph used to read every row that carried citations, including the 27
    Multi-scope and 27 Not-extractable ones, while coverage, the backlog and the
    health index all count Usability='Usable' only. Two populations meant the
    graph could hold a standard that the backlog had never heard of, which is
    exactly what consistency_check found: IS 23896, an endpoint in the graph,
    cited only by two multi-scope documents, declared as a gap nowhere."""
    if "Usability" in df.columns:
        # The same population the headline measures: text read from the layer,
        # or by OCR with every citation confirmed against the register.
        from engine import EVIDENCE
        df = df[df["Usability"].isin(EVIDENCE)]
    sets = []
    for value in df["IS Numbers Cited"].dropna():
        cited = {c.strip() for c in str(value).split(";") if c.strip()}
        if cited:
            sets.append(cited)
    return sets


def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--min-co", type=int, default=MIN_CO_CITATIONS)
    ap.add_argument("--min-confidence", type=float, default=MIN_CONFIDENCE)
    ap.add_argument("--min-source", type=int, default=MIN_SOURCE_TENDERS)
    args = ap.parse_args()
    globals()["MIN_CO_CITATIONS"] = args.min_co
    globals()["MIN_CONFIDENCE"] = args.min_confidence
    globals()["MIN_SOURCE_TENDERS"] = args.min_source

    tenders = pd.read_csv(SRC, encoding="utf-8-sig")
    sets = citation_sets(tenders)
    print(f"{len(tenders)} tender rows · {len(sets)} contain citations")

    source_count: Counter = Counter()
    pair_count: Counter = Counter()
    for cited in sets:
        for is_number in cited:
            source_count[is_number] += 1
        for a, b in itertools.permutations(sorted(cited), 2):
            pair_count[(a, b)] += 1

    total = len(sets)
    rows = []
    for (source, target), co in pair_count.items():
        src_n = source_count[source]
        if co < MIN_CO_CITATIONS or src_n < MIN_SOURCE_TENDERS:
            continue
        confidence = co / src_n
        if confidence < MIN_CONFIDENCE:
            continue
        tgt_n = source_count[target]
        # lift > 1 means the pair co-occurs more than independent citation implies
        lift = (co / total) / ((src_n / total) * (tgt_n / total)) if tgt_n else 0.0
        rows.append(
            {
                "Source IS": source,
                "Target IS": target,
                "Tenders Citing Source": src_n,
                "Tenders Citing Target": tgt_n,
                "Co-citation Count": co,
                "Confidence": round(confidence, 3),
                "Lift": round(lift, 3),
                "Evidence Statement": (
                    f"Cited alongside {source} in {co} of {src_n} comparable tenders"
                ),
            }
        )

    out = pd.DataFrame(rows).sort_values(
        ["Confidence", "Co-citation Count"], ascending=False
    )
    out.to_csv(OUT, index=False)

    # The thresholds are a property of this graph, so they travel with it. The
    # screen used to print them from a string typed into app.js; when they
    # changed here, that string kept announcing the old ones — the same
    # two-copies-of-one-fact failure this project keeps finding. Now there is
    # one place they are written and one place they are read.
    import json as _json
    with open(META, "w", encoding="utf-8") as fh:
        _json.dump({
            "min_co_citations": MIN_CO_CITATIONS,
            "min_confidence": MIN_CONFIDENCE,
            "min_source_tenders": MIN_SOURCE_TENDERS,
            "documents": len(sets),
        }, fh, indent=1)

    nodes = set(out["Source IS"]) | set(out["Target IS"])
    print(f"\nthresholds: {MIN_CO_CITATIONS}+ co-citations, "
          f"{MIN_CONFIDENCE:.0%}+ confidence, source cited in {MIN_SOURCE_TENDERS}+ tenders")
    print(f"wrote {OUT}")
    print(f"  edges {len(out)}   nodes {len(nodes)}")
    print(f"  (previous file: 376 edges, 74 nodes)")
    print("\nstrongest relationships:")
    for _, r in out.head(8).iterrows():
        print(f"  {r['Source IS']:<22} -> {r['Target IS']:<22} "
              f"{r['Co-citation Count']:>3} of {r['Tenders Citing Source']:<3} "
              f"({r['Confidence']:.3f})")

    # The layout is a property of these edges, so it is recomputed here rather
    # than left for someone to remember. A stale layout would park every new
    # standard in the centre of the picture.
    print()
    from graph_layout import compute as _layout_compute

    edges = [(r["Source IS"], r["Target IS"], float(r["Confidence"] or 0))
             for _, r in out.iterrows()]
    coords = _layout_compute(edges)
    with open("data/graph_layout.json", "w", encoding="utf-8") as fh:
        json.dump(coords, fh, separators=(",", ":"), sort_keys=True)
    print(f"wrote data/graph_layout.json — {len(coords)} node positions")


if __name__ == "__main__":
    main()
