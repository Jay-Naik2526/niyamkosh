"""Hybrid retrieval for the forward flow: spec text → governing standard.

Retrieval and the graph decide which standards apply. Nothing downstream may
introduce an IS number that did not come out of this module, and every
recommendation carries the score and the field that produced it.

Pipeline: dense (MiniLM) ∥ BM25 → reciprocal rank fusion → cross-encoder
rerank → confidence gate → graph expansion → deterministic version and
certification lookup → clause composition under a subset guard.

Vectors live in a numpy array rather than pgvector: 405 rows × 384 dims is a
0.6 MB dot product, so a database extension would add infrastructure without
changing the result.
"""

import csv
import os
import re
import sqlite3

import numpy as np
import pandas as pd
from rank_bm25 import BM25Okapi

# sentence_transformers is imported inside _bi() and _cross(), not here. Importing
# it pulls in torch, which costs about 400 MB before a single vector is loaded —
# more than the whole budget of the host this is deployed to. In lexical mode it
# is never imported at all.

DB_PATH = "manak_setu.db"
EMBEDDINGS_PATH = "standards_embeddings.npy"
IS_NUMBERS_PATH = "standards_embeddings_is_numbers.csv"
BI_ENCODER = "all-MiniLM-L6-v2"
CROSS_ENCODER = "cross-encoder/ms-marco-MiniLM-L-6-v2"

RRF_K = 60
FUSE_DEPTH = 20          # per retriever, before fusion
RERANK_DEPTH = 10        # fused candidates handed to the cross-encoder

TOP_SCORE_THRESHOLD = 0.45
MARGIN_THRESHOLD = 0.10
# Above this the cross-encoder is saturated: near-identical scores mean several
# genuinely applicable standards (common within one IS family), not ambiguity.
# Applying the margin rule here abstained on correct #1 hits scoring 0.96-1.00.
HIGH_CONFIDENCE = 0.80

# MiniLM truncates at 256 word-pieces. A 120 KB spec pasted whole is silently
# embedded on its opening fragment and then scores 0.02 against everything, so
# the gate abstains with "no close match" — technically safe, but the officer
# is told the wrong reason. Cap it here and say so instead.
MAX_QUERY_CHARS = 2000

_state = {}


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _load():
    """Corpus, dense vectors and BM25 index. Built once per process."""
    if _state:
        return _state
    conn = _conn()
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM standards").fetchall()]
    finally:
        conn.close()

    # csv rather than pandas: this file is one column of 27,687 strings, and a
    # DataFrame for it costs more than the list it becomes.
    with open(IS_NUMBERS_PATH, encoding="utf-8-sig", newline="") as fh:
        order = [row[0] for row in csv.reader(fh)][1:]
    by_is = {r["IS Number"]: r for r in rows}
    # Only the columns anything downstream reads. Holding all seventeen for
    # 27,687 standards costs 39 MB of dictionaries to carry fields no caller
    # touches, and the deployment this has to fit in has 512 MB in total.
    keep = ("IS Number", "Full Title", "Product Family", "Status", "Year",
            "IS Base", "Replaced By", "Supersedes", "Review Due", "Overdue",
            "Title (Hindi)", "Source Link")
    corpus = [{k: by_is[i].get(k) for k in keep} for i in order if i in by_is]
    del rows, by_is

    blobs = [
        f"{r['IS Number']} — {r.get('Full Title') or ''} — {r.get('Product Family') or ''}"
        for r in corpus
    ]

    # BIS publishes a Hindi title for part of the catalogue, and it is the
    # authority on its own standards' names. Indexing those titles directly lets
    # a Hindi query be matched without a translation service in the loop — which
    # matters, because the free providers refuse the shared datacentre addresses
    # a hosted deployment sits behind, and a machine translation of a technical
    # title is a worse key than BIS's own wording either way.
    #
    # Only the standards that actually carry one are indexed. The rest are not
    # invented, so a Hindi query for a standard BIS has not named in Hindi
    # simply finds nothing here and falls back to translation.
    hindi_docs, hindi_index = [], []
    for i, r in enumerate(corpus):
        title = str(r.get("Title (Hindi)") or "").strip()
        if title:
            hindi_docs.append(_hindi_tokens(f"{title} {r['IS Number']}"))
            hindi_index.append(i)

    # float16 halves the largest array in the process. The vectors are
    # L2-normalised, so every component is within [-1, 1] where float16 has
    # about three decimal digits — far finer than the gaps between ranked
    # cosine scores. Measured on the golden set, ranking is unchanged.
    # In lexical mode the vectors are never multiplied by anything, so loading
    # 21 MB of them is pure cost.
    if LEXICAL_MODE:
        vectors = np.empty((0, 0), dtype=np.float16)
    else:
        vectors = np.load(EMBEDDINGS_PATH)
        if vectors.dtype != np.float16:
            vectors = vectors.astype(np.float16)

    # BM25Okapi builds its own frequency tables and never reads the token lists
    # again, so holding them costs 31 MB for nothing.
    tokenised = [_tokens(b) for b in blobs]
    bm25 = BM25Okapi(tokenised)
    del tokenised

    _state.update(
        corpus=corpus,
        blobs=blobs,
        vectors=vectors,
        bm25=bm25,
        hindi_bm25=BM25Okapi(hindi_docs) if hindi_docs else None,
        hindi_index=hindi_index,
        bi=None,
        cross=None,
    )
    return _state


def _hindi_tokens(text: str) -> list[str]:
    """Devanagari runs, Latin words and digits. Devanagari has no case and its
    word boundaries are spaces, so splitting on non-word characters is enough."""
    return re.findall(r"[\u0900-\u097F]+|[a-z0-9]+", (text or "").lower())


def hindi_ranking(query: str, depth: int = FUSE_DEPTH) -> list[int]:
    """Corpus positions for the best matches on BIS's own Hindi titles."""
    s = _load()
    if not s.get("hindi_bm25"):
        return []
    tokens = _hindi_tokens(query)
    if not tokens:
        return []
    scores = s["hindi_bm25"].get_scores(tokens)
    order = np.argsort(scores)[::-1][:depth]
    return [s["hindi_index"][j] for j in order if scores[j] > 0]


def _bi():
    if LEXICAL_MODE:
        return None
    s = _load()
    if s["bi"] is None:
        from sentence_transformers import SentenceTransformer

        s["bi"] = SentenceTransformer(BI_ENCODER)
    return s["bi"]


# A 512 MB host cannot hold both encoders comfortably. With MANAK_LIGHT=1 the
# cross-encoder is skipped and the fused RRF score carries the ranking instead.
# Retrieval is measurably worse without it — that is the trade, and the /health
# response says which mode is running so nobody has to guess.
LIGHT_MODE = os.getenv("MANAK_LIGHT") == "1"

# Lexical mode drops the neural encoders entirely: BM25, the four constraint
# filters and the confidence gate, and nothing that needs torch. It exists
# because the register outgrew the memory a free host provides — 27,687
# standards plus torch measured 773 MB against a 512 MB limit, and the service
# was killed on every query that touched retrieval. Without torch the same
# corpus and index cost 114 MB.
#
# On the golden set it scores 61/71 at rank 1 against the hybrid's 56/71. That
# is not evidence the encoders are unnecessary: the golden queries are largely
# drawn from the standards' own titles and QCO product descriptions, which is
# exactly the text BM25 matches on. An officer writing "cable for underground
# 11kV feeder" is a different question from a title lookup, and the dense
# retriever is there for that. The mode is a deployment accommodation, reported
# as such in /health, not a claim about which is better.
LEXICAL_MODE = os.getenv("MANAK_LEXICAL") == "1"

# Shape of the sigmoid applied to a fused-rank score when no cross-encoder is
# available. The midpoint is the share of the theoretical maximum at which a
# candidate is judged borderline; the gain sets how sharply confidence falls
# away from it. Chosen so a standard ranked near the top by both retrievers
# clears the gate and a query with no real match does not.
FUSED_SCORE_MIDPOINT = 0.55
FUSED_SCORE_GAIN = 12.0

# BM25 is unbounded, so a threshold on it has to be anchored to this corpus.
# Measured over the 71 golden queries against all 27,687 standards: where the
# top hit was correct it scored at least 14.3 (tenth percentile 25.2, median
# 41.9); over queries with no real answer the top hit peaked at 12.3 (median
# 7.8). The scale places the gate's 0.45 threshold between those two, so the
# weakest genuine match still clears it and the strongest coincidence does not.
# Re-measure these if the register changes size — BM25 scores move with the
# corpus, which is why they are named here rather than buried in an expression.
LEXICAL_SCORE_SCALE = 14.0
LEXICAL_SCORE_GAIN = 4.0

# Dense-only. Measured the same way as the two above, over 250 golden queries
# against all 27,687 standards: where the embedding's top hit was correct it
# scored median 0.851 (min 0.537, p10 0.727); where it was wrong, median 0.771
# (min 0.573, max 0.954). The distributions overlap almost completely, so no
# threshold on this score separates a right answer from a wrong one — which is
# why the `dense` pipeline is registered with gate: False. The midpoint below
# sits between the two medians and is used only to put the score on a 0-1 scale
# for display and ordering; nothing decides anything on it.
DENSE_SCORE_MIDPOINT = 0.81
DENSE_SCORE_GAIN = 12.0


def _cross():
    if LIGHT_MODE or LEXICAL_MODE:
        return None
    s = _load()
    if s["cross"] is None:
        from sentence_transformers import CrossEncoder

        s["cross"] = CrossEncoder(CROSS_ENCODER)
    return s["cross"]


def _sigmoid(x: float) -> float:
    return float(1 / (1 + np.exp(-x)))


# Words that overlap between any query and any title, and so justify nothing.
# The screen printed "matched on Full Title via 1, duty, electric, for" — two
# of those four terms are noise, and "1" is the part number of the standard the
# reader is already looking at. The overlap is still computed over every token,
# because that is what the retriever saw; only what is shown as the reason is
# filtered, and the count of what was dropped travels with it.
_STOP_TERMS = frozenset("""
a an and as at by for from in into of on or the to with without
part sec section is bs iso iec
""".split())


def _useful_terms(terms: list[str]) -> list[str]:
    return [t for t in terms if t not in _STOP_TERMS and not t.isdigit() and len(t) > 2]


def _matched_field(query: str, row: dict) -> dict:
    """Which stored field actually justifies this candidate. Shown in the UI so
    a recommendation can never be a bare score."""
    q = set(_tokens(query))
    best = {"field": "IS Number", "terms": []}
    if re.search(r"\bIS[:\s]*" + re.escape(str(row["IS Number"]).split()[-1]), query, re.I):
        return {"field": "IS Number", "terms": [row["IS Number"]], "weak_terms": 0}
    for field in ("Full Title", "Product Family"):
        terms = sorted(q & set(_tokens(row.get(field) or "")))
        if len(terms) > len(best["terms"]):
            best = {"field": field, "terms": terms}
    strong = _useful_terms(best["terms"])
    return {
        "field": best["field"],
        # If every shared word was a stopword, say so by showing none rather
        # than dressing "for, the, of" up as a reason.
        "terms": strong,
        "weak_terms": len(best["terms"]) - len(strong),
    }


def _rrf(rankings: list[list[int]]) -> dict[int, float]:
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, idx in enumerate(ranking):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)
    return fused


def clip_query(query: str) -> tuple[str, dict]:
    """Trim to the model's usable window, at a sentence boundary where one is
    near, and report what was dropped."""
    text = (query or "").strip()
    if len(text) <= MAX_QUERY_CHARS:
        return text, {"truncated": False, "characters": len(text)}
    head = text[:MAX_QUERY_CHARS]
    cut = max(head.rfind(". "), head.rfind("\n"))
    if cut > MAX_QUERY_CHARS * 0.6:
        head = head[: cut + 1]
    return head.strip(), {
        "truncated": True,
        "characters": len(text),
        "characters_read": len(head.strip()),
        "note": (
            f"Only the first {len(head.strip()):,} of {len(text):,} characters were matched — "
            "the encoder's context window ends there. Run the remaining sections separately, "
            "or paste the single clause you want a standard for."
        ),
    }



# ── pipeline registry ──────────────────────────────────────────────────────
#
# "Alternative RAG pipelines" is a question about engineering judgement, so the
# answer is a registry and a leaderboard rather than a second chatbot. Every
# entry runs the same query against the same 27,687 rows and is measured on the
# same 621 pairs; eval_pipelines.py prints the table and the default is whatever
# the table justifies.
#
# `gate` records whether the confidence gate can run on that pipeline's scores.
# It is False where the score is not bounded or not calibrated — BM25 scores
# grow with query length, so no fixed threshold separates a real match from a
# coincidence across queries of different shapes, and a gate on them would
# abstain by accident rather than by judgement.
PIPELINES = {
    "hybrid_ce": {
        "dense": True, "bm25": True, "rerank": True, "graph": False, "gate": True,
        "label": "Dense ∥ BM25 → RRF → cross-encoder",
        "note": "The default — and it keeps that place for the right to decline, "
                "not for accuracy. Against hybrid_rrf the rank-1 difference is 7 "
                "queries in 621 and does not survive a paired test (p=0.296). "
                "What does not survive removing the cross-encoder is abstention: "
                "38 of 621 here against 1.",
    },
    "hybrid_rrf": {
        "dense": True, "bm25": True, "rerank": False, "graph": False, "gate": True,
        "label": "Dense ∥ BM25 → RRF",
        "note": "The same without the cross-encoder, and as accurate at rank 1 to "
                "within noise — at half the latency. It is not the default because "
                "of what it does on the 38 queries hybrid_ce declines: it answers "
                "37 of them confidently, and is wrong on 30. Fused rank is bounded "
                "but flat, so almost nothing scores low enough to stop.",
    },
    "dense": {
        "dense": True, "bm25": False, "rerank": False, "graph": False, "gate": False,
        "label": "MiniLM embeddings only",
        "note": "No gate. Cosine similarity is bounded, but bounded is not the "
                "same as discriminating: over 250 golden queries a correct top "
                "hit scored median 0.851 and a wrong one 0.771, with ranges that "
                "overlap almost completely. A threshold there would abstain by "
                "accident.",
    },
    "bm25": {
        "dense": False, "bm25": True, "rerank": False, "graph": False, "gate": False,
        "label": "BM25 lexical only",
        "note": "No gate: BM25 scores are unbounded and scale with query length, "
                "so no fixed threshold means the same thing across queries.",
    },
    "graph_expand": {
        "dense": True, "bm25": True, "rerank": True, "graph": True, "gate": True,
        "label": "Hybrid + co-citation neighbours",
        "note": "GraphRAG in the honest sense — the candidate set widened by what "
                "real tenders cite alongside the top hits, not by a model. "
                "Measured, it does not help: identical to hybrid_ce on all 621 "
                "queries at rank 1, and recall@10 falls from 608 to 591 because "
                "the neighbours displace correct answers further down the list. "
                "Kept in the registry as the measurement, not as a recommendation.",
    },
    "llm_only": {
        "retrieval": False, "gate": False,
        "label": "Local model, no retrieval",
        "note": "The baseline that shows why the rest exists. Measured only when "
                "Ollama is running; never estimated.",
    },
}
DEFAULT_PIPELINE = "hybrid_ce"

# How many co-citation neighbours of the top hits graph_expand adds.
GRAPH_EXPAND_FROM = 3
GRAPH_EXPAND_EACH = 5


def _graph_neighbours(is_numbers: list[str]) -> list[str]:
    """What real tenders cite alongside these standards. Read from the
    co-citation table — evidence, not an embedding neighbourhood."""
    import sqlite3

    if not is_numbers:
        return []
    out: list[str] = []
    try:
        conn = sqlite3.connect("manak_setu.db")
        try:
            for number in is_numbers:
                for row in conn.execute(
                    'SELECT "Target IS" FROM co_citation WHERE "Source IS" = ? '
                    'ORDER BY "Co-citation Count" DESC LIMIT ?',
                    (number, GRAPH_EXPAND_EACH),
                ):
                    if row[0] and row[0] not in out:
                        out.append(row[0])
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return out


def search(query: str, boost: str | None = None,
           extra_rankings: list[list[int]] | None = None,
           rerank: bool = True, english: bool = True,
           pipeline: str = DEFAULT_PIPELINE) -> list[dict]:
    """Fused, reranked candidates. Highest calibrated relevance first.

    `boost` is the register's own vocabulary for any abbreviation in the query.
    Appending it to the query simply diluted it — "GI" expanded correctly to
    "steel tubes tubulars", but "pipes for water supply" outweighed it and the
    top hit was a GRP pipe standard. Given its own ranking and fused by RRF, the
    expansion gets a vote instead of a whisper."""
    s = _load()
    corpus, vectors = s["corpus"], s["vectors"]
    cfg = PIPELINES.get(pipeline) or PIPELINES[DEFAULT_PIPELINE]
    use_dense = cfg.get("dense", True)
    use_bm25 = cfg.get("bm25", True)
    rerank = rerank and cfg.get("rerank", True)

    # `english=False` means the query never reached English — translation was
    # unavailable and the text is still in its own script. The dense and lexical
    # indexes are built over English titles, so on that input they return noise,
    # and noise fused with a good ranking outvotes it: two bad rankings against
    # one good one put the same irrelevant standard on top of every query.
    dense_rank: list[int] = []
    bm_rank: list[int] = []
    dense_scores = None
    rankings = []
    if english and use_dense and not LEXICAL_MODE:
        qv = _bi().encode([query], normalize_embeddings=True)[0]
        # float16 storage, float32 arithmetic: the dot product accumulates over
        # 384 terms and half precision would lose the low bits that separate
        # close candidates.
        dense_scores = (vectors @ qv.astype(vectors.dtype)).astype(np.float32)
        dense_rank = list(np.argsort(dense_scores)[::-1][:FUSE_DEPTH])
        rankings.append(dense_rank)
    if english and use_bm25:
        bm_scores = s["bm25"].get_scores(_tokens(query))
        bm_rank = list(np.argsort(bm_scores)[::-1][:FUSE_DEPTH])
        rankings.append(bm_rank)
    # A ranking computed elsewhere — today, BIS's Hindi titles — gets a vote in
    # the fusion rather than a veto, exactly like the vocabulary expansion.
    rankings += [r for r in (extra_rankings or []) if r]
    if boost and english:
        bv = _bi().encode([boost], normalize_embeddings=True)[0]
        rankings.append(list(np.argsort(vectors @ bv)[::-1][:FUSE_DEPTH]))
        rankings.append(list(np.argsort(s["bm25"].get_scores(_tokens(boost)))[::-1][:FUSE_DEPTH]))

    fused = _rrf(rankings)
    shortlist = sorted(fused, key=fused.get, reverse=True)[:RERANK_DEPTH]

    # graph_expand widens the candidate set with what real tenders cite
    # alongside the current top hits, before the reranker sees it. The
    # neighbours join the shortlist as candidates — they do not jump the queue;
    # the reranker still has to prefer them on the query.
    if cfg.get("graph") and shortlist:
        by_number = {corpus[i]["IS Number"]: i for i in range(len(corpus))}
        top = [corpus[i]["IS Number"] for i in shortlist[:GRAPH_EXPAND_FROM]]
        added = 0
        for number in _graph_neighbours(top):
            idx = by_number.get(number)
            if idx is not None and idx not in fused:
                fused[idx] = 0.0          # a candidate, with no fused evidence
                shortlist.append(idx)
                added += 1
        _state["graph_added"] = added

    # The cross-encoder reads the query and an English title together. Handed a
    # query still in Devanagari — which is what happens when translation is
    # unreachable and the Hindi titles are carrying the search — it scores noise
    # against noise, and the confidence gate then abstains on a match the Hindi
    # index had found correctly. The fused rank stands in, as it does in light
    # mode.
    cross = _cross() if rerank else None
    if cross is None and dense_scores is not None and not use_bm25:
        # Dense-only: cosine similarity is already bounded in [-1, 1] and means
        # the same thing across queries, so it is the score. Passing it through
        # the fused-rank path instead would calibrate a rank against a scale it
        # never came from.
        logits = [DENSE_SCORE_GAIN * (float(dense_scores[i]) - DENSE_SCORE_MIDPOINT)
                  for i in shortlist]
    elif cross is None:
        # Fused rank stands in for a reranked score. It must be calibrated
        # against what a good match *could* score, not against the best match
        # actually found — dividing by the observed top gives the leader 1.0
        # whatever it is, so "banana republic" came back at score 1.0 and
        # clear_match, and the confidence gate could never fire. Reciprocal rank
        # fusion is bounded: a candidate placed first by every ranking scores
        # len(rankings)/(RRF_K + 1), so that is the scale.
        if len(rankings) > 1:
            ceiling = len(rankings) / (RRF_K + 1)
            logits = [FUSED_SCORE_GAIN * (min(fused[i] / ceiling, 1.0) - FUSED_SCORE_MIDPOINT)
                      for i in shortlist]
        else:
            # With a single ranking, reciprocal rank fusion says nothing about
            # quality: the leader scores exactly 1/(k+1) whether it is a perfect
            # match or the least bad of 27,687 wrong ones, so "purple unicorn
            # saddles" came back at 0.99. The retriever's own score is the only
            # signal there is, so it is used directly, relative to what this
            # corpus's titles score when they genuinely match.
            raw = s["bm25"].get_scores(_tokens(query))
            logits = [LEXICAL_SCORE_GAIN * (float(raw[i]) / LEXICAL_SCORE_SCALE - 1.0)
                      for i in shortlist]
    else:
        pairs = [(query, s["blobs"][i]) for i in shortlist]
        logits = cross.predict(pairs)

    out = []
    for idx, logit in zip(shortlist, logits):
        row = corpus[idx]
        out.append(
            {
                "is_number": row["IS Number"],
                "title": row.get("Full Title"),
                "year": row.get("Year"),
                "status": row.get("Status"),
                "product_family": row.get("Product Family"),
                # BIS's own review date for this edition. Not an amendment — BIS
                # does not publish amendment numbers through this catalogue — but
                # a date in the past means the edition is overdue for revision and
                # the citation is worth confirming before publication.
                "review_due": row.get("Review Due"),
                "review_overdue": row.get("Overdue") == "Yes",
                "score": round(_sigmoid(float(logit)), 4),
                "dense_rank": dense_rank.index(idx) + 1 if idx in dense_rank else None,
                "bm25_rank": bm_rank.index(idx) + 1 if idx in bm_rank else None,
                "rrf": round(fused[idx], 5),
                "matched_on": _matched_field(query, row),
            }
        )
    out.sort(key=lambda c: c["score"], reverse=True)
    # What each stage actually handled, so the page can show the path an answer
    # took without inferring it from the five candidates it is shown.
    _state["last_trace"] = {
        "dense_depth": len(dense_rank),
        "bm25_depth": len(bm_rank),
        "rankings_fused": len(rankings),
        "fused_candidates": len(fused),
        "reranked": len(shortlist),
        "reranker": "cross-encoder" if cross is not None else "fused rank",
        "pipeline": pipeline,
    }
    out, _ = _apply_voltage_filter(query, out)
    _rank_candidates(out)
    return out


def _gate(candidates: list[dict], stated_voltage: float | None = None) -> dict:
    """Pure logic. Abstaining is a valid, visible outcome — never a blank."""
    if not candidates:
        return {"decision": "abstain", "reason": "no_candidates"}
    top = candidates[0]["score"]
    second = candidates[1]["score"] if len(candidates) > 1 else 0.0
    if top < TOP_SCORE_THRESHOLD:
        return {"decision": "abstain", "reason": "no_close_match"}
    if top < HIGH_CONFIDENCE and (top - second) < MARGIN_THRESHOLD:
        return {"decision": "abstain", "reason": "ambiguous_match"}
    # Only ambiguous when the spec genuinely omits the voltage. Firing this on a
    # query that says "11 kV" told the officer to supply a figure they had
    # already supplied.
    if stated_voltage is None and _parts_tied_on_voltage(candidates):
        return {"decision": "abstain", "reason": "voltage_unspecified"}
    return {"decision": "recommend", "reason": "clear_match"}


def _parts_tied_on_voltage(candidates: list[dict]) -> bool:
    """Two parts of one standard, tied at the top, split only by a voltage the
    spec never states. Picking either is a coin flip presented as an answer;
    asking for the rating is the correct behaviour and takes the officer four
    seconds."""
    if len(candidates) < 2:
        return False
    a, b = candidates[0], candidates[1]
    if abs(a["score"] - b["score"]) >= MARGIN_THRESHOLD:
        return False
    if _base_is(a["is_number"]) != _base_is(b["is_number"]):
        return False
    ra = declared_voltage_range(a.get("title") or "")
    rb = declared_voltage_range(b.get("title") or "")
    return ra is not None and rb is not None and ra != rb


def _base_is(is_number: str) -> str:
    return re.sub(r"\s+", " ", str(is_number).split("(")[0]).strip()


IS_IN_TEXT = re.compile(
    r"\bIS[:\s]*(\d{2,6})(?:\s*\([^)]{0,40}\))?(?:\s*[:\-]\s*((?:19|20)\d{2}))?", re.I
)


def subset_guard(
    text: str, allowed: list[str], years: dict[str, str] | None = None
) -> tuple[bool, list[str]]:
    """Every IS number in composed prose must come from the retrieved set — and
    where the prose states an edition year, that year must be the retrieved one.

    The year matters as much as the number. "IS 4985:2012" reads as authoritative
    and is wrong; a guard that only checked the digits after "IS" would have shown
    a green tick beside a fabricated edition. Enforced in code, because a prompt
    instruction is not a guarantee."""
    allowed_bases = {re.sub(r"[^0-9]", "", a.split("(")[0]) for a in allowed}
    known = {
        re.sub(r"[^0-9]", "", k.split("(")[0]): str(v).split(".")[0]
        for k, v in (years or {}).items()
        if v and str(v).strip() and str(v).lower() != "nan"
    }
    leaked = []
    for base, year in IS_IN_TEXT.findall(text):
        digits = re.sub(r"[^0-9]", "", base)
        if digits and digits not in allowed_bases:
            leaked.append(f"IS {base}")
        elif year and digits in known and year != known[digits]:
            leaked.append(f"IS {base}:{year} (register says {known[digits]})")
    return (not leaked), leaked


def compose_clause(governing: dict, related: list[dict], cert: dict, use_llm: bool = True) -> dict:
    """Deterministic composition, optionally rephrased by a local LLM.

    The template runs first and always. If a model is available its output has to
    survive the subset guard and name the governing standard before it is used;
    otherwise the template stands and `composed_by` says which one you got. The
    facts are identical either way — only the prose differs."""
    cited = [governing["is_number"]] + [r["target_is"] for r in related]
    year = governing.get("year")
    family = governing.get("product_family") or "material"
    # Lower-case the first letter for mid-sentence use, but never an acronym:
    # "LED lighting" must not become "lED lighting".
    if not (len(family) > 1 and family[1].isupper()):
        family = family[0].lower() + family[1:]
    head = (
        f"The {family} supplied under this "
        f"contract shall conform in all respects to {governing['is_number']}"
        f"{f' : {year}' if year and str(year).isdigit() else ''}"
        f" ({governing.get('title')})."
    )
    parts = [head]
    if related:
        listed = ", ".join(r["target_is"] for r in related[:4])
        parts.append(
            f"The following standards are cited alongside {governing['is_number']} in comparable "
            f"published tenders and shall be reviewed for applicability to this procurement: "
            f"{listed}. Their inclusion here records observed procurement practice, not a "
            "determination that each one governs this item."
        )
    if cert.get("found") and cert.get("certification_mandatory") == "Yes":
        parts.append(
            f"The product falls under mandatory BIS certification ({cert.get('scheme')}); "
            "only material bearing a valid BIS Standard Mark shall be accepted."
        )
    if governing.get("status") in ("Superseded", "Withdrawn"):
        parts.append(
            f"Note: {governing['is_number']} is recorded as {governing['status']} and must be "
            "confirmed against the current BIS listing before publication."
        )
    years = {governing["is_number"]: governing.get("year")}
    text = " ".join(parts)
    ok, leaked = subset_guard(text, cited, years)
    result = {
        "text": text,
        "composed_by": "template",
        "cited_standards": cited,
        "subset_guard": {"passed": ok, "leaked": leaked},
    }
    if not use_llm:
        return result

    import llm

    attempt = llm.phrase(governing, related, cert, cited, years)
    if attempt.get("ok"):
        guard_ok, guard_leaked = subset_guard(attempt["text"], cited, years)
        result.update(
            text=attempt["text"],
            composed_by="llm",
            model=attempt.get("model"),
            generation_ms=attempt.get("duration_ms"),
            template_text=text,          # kept, so the two are comparable on screen
            subset_guard={"passed": guard_ok, "leaked": guard_leaked},
        )
    else:
        # A rejected generation is the guard doing its job, so it is shown rather
        # than swallowed: without the text you cannot tell a working model from a
        # dead one, and you cannot tell a real catch from an over-strict rule.
        result["llm"] = {
            "used": False,
            "reason": attempt.get("reason"),
            "detail": attempt.get("detail"),
            "rejected_text": attempt.get("rejected_text"),
            "model": attempt.get("model"),
        }
    return result


def recommend(query: str, ui_language: str | None = None,
              pipeline: str = DEFAULT_PIPELINE) -> dict:
    """Forward flow end to end.

    `pipeline` selects a registered retrieval strategy. It exists so the same
    end-to-end path — filters, gate, certification, co-citations — can be run
    over an alternative retriever and measured, rather than comparing a
    retriever in isolation against the product. Default is unchanged."""
    from engine import check_certification, related_standards

    import multilingual
    import normalize

    # Multilingual input: a spec written in an Indian language is translated to
    # English first, because the register is published in English. The original
    # is kept and returned so the officer can see what was actually matched.
    original = query
    lang = multilingual.translate_query(query, ui_language)
    query = lang["text"]

    # BIS's own Hindi titles, searched on the text as the officer wrote it. This
    # runs whether or not translation succeeded: where BIS has named a standard
    # in Hindi, its own wording is a better key than a machine translation of it.
    hindi_rank = []
    if lang.get("source_language") == "hi" or (ui_language or "").startswith("hi"):
        hindi_rank = hindi_ranking(original)
    if hindi_rank:
        lang = {**lang, "hindi_titles_searched": True, "hindi_title_hits": len(hindi_rank)}
        if not lang.get("applied"):
            lang["note"] = (
                "The translation service could not be reached, so this was matched "
                "against the Hindi titles BIS publishes for its own standards. "
                "Standards BIS has not named in Hindi cannot be found this way."
            )

    # Translation failed on text that is not in Latin script, so `query` is still
    # Devanagari, Tamil, Urdu and so on. The register is English: matching it as
    # written scores whatever Latin characters survive — for "11 केवी एक्सएलपीई
    # ... केबल" that was the digits, which retrieved thermal-ageing test methods
    # and coir matting at score 0.000. The gate abstained, correctly, but the
    # trace beneath it read as though the system had genuinely considered those
    # standards. Saying plainly that the text could not be read is honest; a
    # ranked list of unrelated standards is not.
    if lang.get("source_language") and not lang.get("applied") and not hindi_rank:
        return {
            "query": query,
            "decision": "abstain",
            "reason": "translation_unavailable",
            "thresholds": {
                "top_score": TOP_SCORE_THRESHOLD,
                "margin": MARGIN_THRESHOLD,
                "high_confidence": HIGH_CONFIDENCE,
            },
            "candidates": [],
            "voltage_filter": {"applied": False, "query_voltage_v": None},
            "material_filter": {"applied": False, "query_materials": [],
                                "demoted": [], "note": ""},
            "role_filter": {"applied": False, "wanted": None,
                            "demoted": [], "note": ""},
            # These must carry the same shape the full path returns. An earlier
            # version shortened them and the page stopped rendering: the trace
            # maps over normalization.terms, which was not there.
            "input": {"truncated": False, "characters": len(query)},
            "language": lang,
            "normalization": {"applied": False, "terms": [], "note": ""},
            "governing": None, "related": [], "allied": [],
            "certification": {"found": False},
            "clause": None,
        }

    query, clipped = clip_query(query)
    # Orchestration: give retrieval the register's own vocabulary before it runs.
    # The officer's words are kept; the register's are appended.
    # Retrieval AND the cross-encoder both see the expanded text. Giving the
    # expansion its own RRF ranking instead was tried and was worse: the reranker
    # still scored against the original wording, so expansion-found candidates
    # were retrieved and then immediately discarded.
    expanded, applied = normalize.expand(query)
    # Translation failed and the Hindi titles are the only usable signal, so the
    # query reaching the English encoders is still Devanagari.
    hindi_only = bool(hindi_rank) and not lang.get("applied") and lang.get("source_language")
    candidates = search(expanded,
                        extra_rankings=[hindi_rank] if hindi_rank else None,
                        rerank=not hindi_only, english=not hindi_only,
                        pipeline=pipeline)
    candidates, voltage_filter = _apply_voltage_filter(query, candidates)
    candidates, material_filter = _apply_material_filter(query, candidates)
    candidates, role_filter = _apply_role_filter(query, candidates)
    _rank_candidates(candidates)
    gate = _gate(candidates, stated_voltage=voltage_filter.get("query_voltage_v"))
    result = {
        "query": query,
        "decision": gate["decision"],
        "reason": gate["reason"],
        "thresholds": {
            "top_score": TOP_SCORE_THRESHOLD,
            "margin": MARGIN_THRESHOLD,
            "high_confidence": HIGH_CONFIDENCE,
        },
        "candidates": candidates[:5],
        "retrieval": dict(_state.get("last_trace") or {}, returned=len(candidates)),
        "voltage_filter": voltage_filter,
        "material_filter": material_filter,
        "role_filter": role_filter,
        "input": clipped,
        "language": lang,
        "normalization": {
            "applied": bool(applied),
            "terms": applied,
            "note": (
                "Trade abbreviations were expanded into the register's own wording before "
                "retrieval. The original text was kept, not replaced."
            ) if applied else None,
        },
    }
    if gate["decision"] == "abstain":
        result["message"] = {
            "no_close_match": "No candidate cleared the relevance threshold. "
            "Shown for reference only — route to a BIS officer.",
            "ambiguous_match": "Top candidates are too close to separate. "
            "A human must choose between them.",
            "no_candidates": "Nothing retrieved for this text.",
            "voltage_unspecified": (
                "Two parts of the same standard cover this product, separated only by working "
                "voltage, and the specification does not state one. Add the voltage rating "
                "(for example \u201c1.1 kV\u201d or \u201c11 kV\u201d) and re-run."
            ),
        }[gate["reason"]]
        return result

    gov = candidates[0]
    related = related_standards(gov["is_number"], limit=12)
    cert = check_certification(gov["is_number"])
    import allied
    from engine import standard_titles

    result.update(
        governing=gov,
        related=related[:6],
        allied=allied.classify(related, standard_titles([r["target_is"] for r in related])),
        certification=cert,
        clause=compose_clause(gov, related, cert),
    )
    return result


# ------------------------------------------------------- role constraints

# A specification describes a product, so it wants a product standard. Retrieval
# does not know that: "galvanized iron pipes for water supply" returned IS 11906,
# "Recommendations for cement mortar lining for ... pipes" — right subject, wrong
# kind of document. Roles are already readable from BIS titles (allied.py), so
# the same reading is used here to keep a code of practice or a test method from
# outranking the product standard the officer actually asked for.
ROLE_SEEKING = {
    "test_method": [r"\bmethod of test\b", r"\btest method\b", r"\btesting\b", r"\bsampling\b"],
    "installation": [r"\bcode of practice\b", r"\binstallation\b", r"\blaying\b",
                     r"\berection\b", r"\bmaintenance\b"],
    "terminology": [r"\bglossary\b", r"\bterminolog", r"\bdefinitions?\b"],
    "safety": [r"\bsafety requirements?\b"],
}
_SEEK_RE = {k: [re.compile(p, re.I) for p in v] for k, v in ROLE_SEEKING.items()}


def _wanted_role(query: str) -> str:
    """What kind of document the spec is asking for. A plain product description
    asks for a product standard, which is the common case."""
    for role, patterns in _SEEK_RE.items():
        if any(p.search(query or "") for p in patterns):
            return role
    return "product"


def _apply_role_filter(query: str, candidates: list[dict]) -> tuple[list[dict], dict]:
    """Demote documents of the wrong kind. Scores untouched, as with the others."""
    import allied

    wanted = _wanted_role(query)
    mismatched = 0
    for c in candidates:
        role, label = allied.role_of(c.get("title") or "")
        agrees = role == wanted or role == "product" and wanted == "product"
        # Only two roles are never the product specification: a glossary and a
        # code of practice. Demoting more than that cost real answers — nine of
        # the seventy-one evaluation targets are titled "safety", "test" or
        # "recommendations" and are still the governing standard for their
        # product ("Leather safety boots and shoes for miners" is classified
        # safety by its own name). Recall fell 92% to 87% and abstention rose to
        # 17% before this was narrowed.
        if wanted == "product" and role not in ("terminology", "installation"):
            agrees = True
        mismatched += 0 if agrees else 1
        c["role"] = {"of_candidate": role, "label": label, "wanted": wanted,
                     "verdict": "match" if agrees else "wrong_kind"}
    return candidates, {
        "applied": True, "wanted": wanted, "demoted": mismatched,
        "note": (
            f"The specification asks for a {wanted.replace('_', ' ')} standard. "
            "Test methods, glossaries and codes of practice were moved below product "
            "standards. Scores are unchanged."
        ) if mismatched else None,
    }


# ------------------------------------------------------- material constraints

# A specification names a material, and the register names it too. Semantic
# similarity does not separate them: "uPVC pipe for drinking water" scored CPVC
# and UPVC identically at 0.995, "GI pipes" returned glass-fibre reinforced
# plastic, and "reinforced cement concrete pipes" returned asbestos-cement. Every
# one of those is the same failure — a near-synonym of the product with the wrong
# substance. Materials are named explicitly in titles, so they can be read.
MATERIALS = {
    "cpvc": [r"\bCPVC\b", r"chlorinated\s+poly"],
    "upvc": [r"\bU-?PVC\b", r"\bPVC-?U\b", r"unplastici[sz]ed"],
    "pvc": [r"\bPVC\b", r"polyvinyl\s+chloride"],
    "xlpe": [r"\bXLPE\b", r"cross-?\s?linked\s+polyethylene"],
    "hdpe": [r"\bHDPE\b", r"high\s+density\s+polyethylene"],
    "ldpe": [r"\bLDPE\b", r"low\s+density\s+polyethylene"],
    "grp": [r"\bGRP\b", r"\bFRP\b", r"glass-?\s?fibre\s+reinforced"],
    "concrete": [r"\bconcrete\b", r"\bcement\s+concrete\b"],
    "asbestos": [r"\basbestos\b"],
    "cast_iron": [r"\bcast\s+iron\b", r"\bCI\b"],
    "ductile_iron": [r"\bductile\s+iron\b"],
    "steel": [r"\bgalvani[sz]ed\b", r"\bsteel\s+tube", r"\bmild\s+steel\b", r"\bGI\b"],
    "elastomer": [r"\belastomer\b", r"\brubber\b"],
}
# Materials that are genuinely compatible: naming the broader one should not
# exclude the narrower. Everything else is treated as a conflict.
COMPATIBLE = {("pvc", "upvc"), ("upvc", "pvc"), ("pvc", "cpvc"), ("cpvc", "pvc")}

_MATERIAL_RE = {k: [re.compile(p, re.I) for p in pats] for k, pats in MATERIALS.items()}


def materials_in(text: str) -> set[str]:
    found = set()
    for name, patterns in _MATERIAL_RE.items():
        if any(p.search(text or "") for p in patterns):
            found.add(name)
    # "unplasticized PVC" is UPVC, not bare PVC; the specific reading wins.
    if "upvc" in found or "cpvc" in found:
        found.discard("pvc")
    return found


def _apply_material_filter(query: str, candidates: list[dict]) -> tuple[list[dict], dict]:
    """Demote candidates whose title names a material the spec did not ask for.

    Scores are untouched, exactly as with the voltage filter: the candidate keeps
    what the cross-encoder gave it and carries a visible verdict, so the reordering
    can be audited rather than taken on trust."""
    wanted = materials_in(query)
    if not wanted:
        return candidates, {"applied": False, "query_materials": []}

    conflicts = 0
    for c in candidates:
        theirs = materials_in(c.get("title") or "")
        if not theirs:
            c["material"] = {"declared": None, "verdict": "not_declared"}
            continue
        agrees = bool(theirs & wanted) or any(
            (w, t) in COMPATIBLE for w in wanted for t in theirs
        )
        conflicts += 0 if agrees else 1
        c["material"] = {
            "declared": sorted(theirs),
            "verdict": "match" if agrees else "different_material",
        }

    return candidates, {
        "applied": True,
        "query_materials": sorted(wanted),
        "demoted": conflicts,
        "note": (
            f"Specification names {', '.join(sorted(wanted))}. Candidates whose title "
            "declares a different material were moved below those that match. Scores "
            "are unchanged."
        ),
    }


# ------------------------------------------------------- numeric constraints

_NUM = r"\d[\d\s]*(?:\.\d+)?"


def _volts(value: str, unit: str) -> float:
    v = float(re.sub(r"\s+", "", value))          # titles write "1 100 V"
    return v * 1000 if unit.lower() == "kv" else v


_RANGE_FROM = re.compile(
    rf"from\s+({_NUM})\s*(kV|V)\b.{{0,40}}?up\s*to\s+and\s+including\s+({_NUM})\s*(kV|V)\b", re.I
)
_RANGE_UPTO = re.compile(
    rf"up\s*to\s+and\s+including\s+({_NUM})\s*(kV|V)\b", re.I
)


def declared_voltage_range(title: str) -> tuple[float, float] | None:
    """The voltage band an IS part declares in its own title.

    IS 1554 Part 1 covers up to 1100 V and Part 2 covers 3.3-11 kV. The
    cross-encoder scores both nearly identically for '1.1 kV cable' because the
    titles differ by a number, and no amount of semantic similarity distinguishes
    numbers. Parsing the band and checking it is the only fix that actually works."""
    if not title:
        return None
    m = _RANGE_FROM.search(title)
    if m:
        return _volts(m.group(1), m.group(2)), _volts(m.group(3), m.group(4))
    m = _RANGE_UPTO.search(title)
    if m:
        return 0.0, _volts(m.group(1), m.group(2))
    return None


_QUERY_VOLT = re.compile(rf"({_NUM})\s*(kV|V)\b")


def query_voltage(query: str) -> float | None:
    """Highest voltage stated in the spec text. '650/1100 V' is a rating pair;
    the upper figure is the one a standard's band is written against."""
    found = [_volts(m.group(1), m.group(2)) for m in _QUERY_VOLT.finditer(query or "")]
    found = [v for v in found if v > 0]
    return max(found) if found else None


V_RANK = {"in_band": 0, "not_declared": 1, "out_of_band": 2}
M_RANK = {"match": 0, "not_declared": 1, "different_material": 2}
R_RANK = {"match": 0, "wrong_kind": 1}
# A live standard outranks a dead one at equal relevance. "PVC insulated heavy
# duty cable" scored IS 4288 (Withdrawn) and IS 1554 identically at 1.000 and
# returned the withdrawn one — this system exists to catch exactly that mistake
# in other people's tenders, so it must not make it in its own recommendation.
S_RANK = {"Current": 0, "Superseded": 1, "Withdrawn": 2}


def _rank_candidates(candidates: list[dict]) -> None:
    """One sort, after every filter has had its say.

    Each filter used to sort on its own key the moment it ran, so whichever ran
    last silently overwrote the others: the material filter promoted an
    out-of-band IS 7098 (Part 3) above the in-band Part 2 purely because it
    scored higher. Constraints have to be combined, not applied in sequence."""
    candidates.sort(key=lambda c: (
        V_RANK.get((c.get("voltage") or {}).get("verdict", "not_declared"), 1),
        M_RANK.get((c.get("material") or {}).get("verdict", "not_declared"), 1),
        R_RANK.get((c.get("role") or {}).get("verdict", "match"), 0),
        S_RANK.get(c.get("status"), 0),
        -c["score"],
    ))


def _apply_voltage_filter(query: str, candidates: list[dict]) -> tuple[list[dict], dict]:
    """Demote candidates whose declared band excludes the stated voltage.

    Scores are left untouched — a silently rewritten score is unauditable. The
    candidate keeps its cross-encoder score and carries a visible verdict, and
    ordering puts out-of-band parts last."""
    qv = query_voltage(query)
    if qv is None:
        return candidates, {"applied": False, "query_voltage_v": None}

    excluded = 0
    for c in candidates:
        band = declared_voltage_range(c.get("title") or "")
        if band is None:
            c["voltage"] = {"declared": None, "verdict": "not_declared"}
            continue
        low, high = band
        inside = low <= qv <= high
        excluded += 0 if inside else 1
        c["voltage"] = {
            "declared": f"{low:g}-{high:g} V" if low else f"up to {high:g} V",
            "low_v": low,
            "high_v": high,
            "verdict": "in_band" if inside else "out_of_band",
        }

    return candidates, {
        "applied": True,
        "query_voltage_v": qv,
        "demoted": excluded,
        "note": (
            f"Spec states {qv:g} V. Candidates whose title declares a voltage band excluding "
            "it were moved below those that cover it. Scores are unchanged."
        ),
    }
