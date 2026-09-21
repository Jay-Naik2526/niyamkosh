import datetime
import hashlib
import json
import os
import sqlite3

from fastapi import FastAPI, File, HTTPException, UploadFile, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from audit import (
    audit_tender,
    corpus_evidence,
    list_decisions,
    neighbourhood,
    record_decision,
)
from engine import (
    check_certification,
    check_dead_citation,
    corpus_stats,
    extract_citations,
    extract_document,
    full_graph,
    list_backlog,
    list_certifications,
    list_standards,
    list_tenders,
    match_spec,
    related_standards,
    run_benchmark,
    standard_detail,
)

DB_PATH = "manak_setu.db"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
# A pasted body larger than this is not a tender clause, it is an accident or a
# probe. Bounded here so the limit is a stated rule rather than whatever the
# process happens to survive.
MAX_TEXT_CHARS = 400_000
VERSION = "0.4"

def _bis_check() -> dict:
    """When the register was last checked against the BIS portal, and what that
    check covered.

    `dataset_date` below is a file mtime. It moves whenever anything writes a
    CSV — a merge, a re-extraction, a title fix — so it says when the files were
    last touched, not when BIS was last consulted. Presenting it as freshness
    would be the project's own failure mode: one number standing in for a
    different fact. This reads the pipeline's own run log, which records every
    time `--only versions` actually contacted standards.bis.gov.in.
    """
    path = os.path.join("data", "pipeline_runs.jsonl")
    if not os.path.exists(path):
        return {"checked": None, "note": "no pipeline run recorded"}
    latest = None
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    run = json.loads(line)
                except ValueError:
                    continue
                for stage in run.get("results") or []:
                    if stage.get("stage") == "versions" and stage.get("outcome") == "checked":
                        latest = (run.get("finished"), stage)
    except OSError:
        return {"checked": None, "note": "run log unreadable"}
    if not latest:
        return {"checked": None, "note": "no version check recorded"}
    finished, stage = latest
    day = str(finished).split("T")[0]
    checked = stage.get("standards_checked") or 0
    found = stage.get("amendments_found") or 0
    return {
        "checked": day,
        "standards_checked": checked,
        "amendments_found": found,
        "note": (f"{checked} standards re-checked against the BIS portal on {day}; "
                 f"{found} amendment{'' if found == 1 else 's'} found. "
                 "The rest of the register carries the status recorded when it "
                 "was collected."),
    }


def _dataset_date() -> str:
    """Newest mtime across the source CSVs — when the files were last written,
    which is not the same as when BIS was last consulted. See _bis_check."""
    newest = 0.0
    for name in os.listdir("data") if os.path.isdir("data") else []:
        if name.endswith(".csv"):
            newest = max(newest, os.path.getmtime(os.path.join("data", name)))
    if not newest:
        return "unknown"
    return datetime.date.fromtimestamp(newest).strftime("%d %b %Y")

app = FastAPI(title="MANAK-SETU Backend")

@app.middleware("http")
async def revalidate(request, call_next):
    """Always ask, but do not always send.

    Everything used to be `no-store`, because data changes when the CSVs are
    rebuilt and a browser holding yesterday's /graph shows stale counts with no
    visible error. That is still the right instinct — but `no-cache` gets it
    without the cost: the browser revalidates on every request, and when nothing
    has changed the answer is a 304 with no body instead of the payload again.

    The tag is a hash of the body, so it changes exactly when the data does.
    Streaming responses (the CSV exports) are passed through untouched — they
    must not be buffered to be hashed.
    """
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache, must-revalidate"

    if request.method != "GET" or response.status_code != 200:
        return response
    if not hasattr(response, "body_iterator"):
        return response
    if response.headers.get("content-type", "").startswith("text/csv"):
        return response

    chunks = [chunk async for chunk in response.body_iterator]
    body = b"".join(chunks)
    tag = '"' + hashlib.sha256(body).hexdigest()[:24] + '"'
    if request.headers.get("if-none-match") == tag:
        headers = {k: v for k, v in response.headers.items()
                   if k.lower() not in ("content-length", "content-type")}
        headers["etag"] = tag
        return Response(status_code=304, headers=headers)

    headers = dict(response.headers)
    headers["etag"] = tag
    headers["content-length"] = str(len(body))
    return Response(content=body, status_code=200, headers=headers,
                    media_type=response.media_type)


# The graph and standards payloads are highly repetitive JSON; over a tunnel or
# a phone connection the transfer, not the query, is what makes the page wait.
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Same-origin in every deployment we run: FastAPI serves the frontend itself, so
# the browser never makes a cross-origin call. The list stays configurable for a
# split deploy, but it defaults to closed rather than open — /decision writes to
# the audit log, and "*" would let anyone who finds the URL write to it.
_origins = [o.strip() for o in os.getenv("MANAK_ALLOWED_ORIGINS", "").split(",") if o.strip()]
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )


class AnalyzeRequest(BaseModel):
    spec_text: str | None = None
    cited_is_numbers: list[str] = []


@app.post("/analyze")
def analyze(req: AnalyzeRequest):
    response = {
        "matched_standards": None,
        "dead_citations": {},
        "certifications": {},
        "related": {},
    }

    if req.spec_text:
        response["matched_standards"] = match_spec(req.spec_text)

    for is_number in req.cited_is_numbers:
        response["dead_citations"][is_number] = check_dead_citation(is_number)
        response["certifications"][is_number] = check_certification(is_number)
        response["related"][is_number] = related_standards(is_number)

    return response


async def _read_upload(file: UploadFile) -> bytes:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 25 MB limit")
    return content


def _extract_or_422(content: bytes, filename: str | None) -> dict:
    try:
        return extract_document(content, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not parse document: {exc}") from exc


class TextRequest(BaseModel):
    text: str


class AuditTextRequest(BaseModel):
    text: str = ""
    cited: list[str] | None = None
    document: str | None = None


@app.post("/extract")
async def extract(file: UploadFile = File(...)):
    """Text and literal IS-number citations from an uploaded PDF or .docx."""
    return _extract_or_422(await _read_upload(file), file.filename)


@app.post("/audit-tender")
async def audit_tender_upload(file: UploadFile = File(...)):
    """Audit screen, file path: extract, then report dispute risk, statutory
    omissions and standards comparable tenders cite but this one omits."""
    extracted = _extract_or_422(await _read_upload(file), file.filename)
    result = audit_tender(
        extracted.get("text", ""),
        filename=file.filename,
        cited=extracted.get("citations", []),
    )
    result["extraction"] = {
        k: extracted.get(k)
        for k in ("format", "page_count", "pages_read", "characters", "chars_per_page",
                  "tables", "scanned", "scanned_note")
    }
    if extracted.get("scanned"):
        # A scan yields no text layer, so "no findings" would be a lie of omission.
        result["verdict"] = "unreadable"
        result["summary"] = extracted["scanned_note"]
    return result


@app.post("/audit-text")
def audit_tender_text(req: AuditTextRequest):
    """Same audit for pasted text, or for citations queued by hand. The demo
    path, and the fallback when a PDF turns out to be a scan."""
    if len(req.text or "") > MAX_TEXT_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"Text exceeds {MAX_TEXT_CHARS:,} characters. Upload the document instead.",
        )
    cited = [c.strip() for c in (req.cited or []) if c and c.strip()] or None
    if not (req.text or "").strip() and not cited:
        raise HTTPException(
            status_code=400, detail="Provide specification text or at least one IS number"
        )
    return audit_tender(req.text or "", filename=req.document or "pasted text", cited=cited)


from collections import OrderedDict  # noqa: E402  (used by the fixed-document cache)

# Repaired documents, held just long enough for the officer to download them.
# Nothing is written to disk: a corrected tender is the officer's document, not
# this service's, and a temp directory of other people's procurement is a
# liability nobody asked for. Entries expire; the cap stops a long session
# growing without bound.
_FIXED: OrderedDict = OrderedDict()
FIXED_TTL_SECONDS = 1800
FIXED_MAX = 24


def _stash(entry: dict) -> str:
    import secrets
    import time

    now = time.time()
    for key in [k for k, v in _FIXED.items() if now - v["at"] > FIXED_TTL_SECONDS]:
        _FIXED.pop(key, None)
    while len(_FIXED) >= FIXED_MAX:
        _FIXED.popitem(last=False)
    token = secrets.token_urlsafe(12)
    _FIXED[token] = {**entry, "at": now}
    return token


class FixRequest(BaseModel):
    text: str = ""
    document: str | None = None
    # A caller may switch the model off to see the deterministic repair on its
    # own. The deterministic path is always computed either way and always
    # returned, so the two can be compared on screen.
    use_model: bool = True


@app.post("/fix-tender")
def fix_tender(req: FixRequest):
    """Repair a tender specification, then audit the repair.

    The register decides every substantive change before the model is called,
    the model only re-renders the prose, and the result is audited again from
    scratch by the same code that audited the original. See tender_fix.py.
    """
    from tender_fix import fix_document

    text = req.text or ""
    if len(text) > MAX_TEXT_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"Text exceeds {MAX_TEXT_CHARS:,} characters. Upload the document instead.",
        )
    if not text.strip():
        raise HTTPException(status_code=400, detail="Provide the specification text to repair")
    return fix_document(text, filename=req.document or "pasted text", use_model=req.use_model)


@app.post("/fix-document")
async def fix_document_upload(file: UploadFile = File(...)):
    """Repair an uploaded tender and keep the file it arrived as.

    A .docx is edited inside itself and a .pdf is patched inside itself, so the
    letterhead, logo, tables and numbering are the ones the officer submitted.
    The corrected document is then audited again from scratch, and the token
    returned here downloads it in either format.
    """
    import docx_fix
    import letterhead
    import pdf_fix
    from tender_fix import fix_document, plan_fix, verify

    raw = await _read_upload(file)
    name = file.filename or "document"
    lower = name.lower()
    if lower.endswith(".docx"):
        kind, text = "docx", docx_fix.docx_text(raw)
    elif lower.endswith(".pdf"):
        kind, text = "pdf", pdf_fix.pdf_text(raw)
    else:
        raise HTTPException(
            status_code=415,
            detail="Upload a .pdf or a .docx. Other formats have no template to preserve.",
        )
    if not text.strip():
        raise HTTPException(
            status_code=422,
            detail=("No text could be read from this file. A scan has no text layer to "
                    "correct \u2014 audit it first, then paste the specification."),
        )

    # The plan and the prose repair, exactly as the text path does them.
    result = fix_document(text, filename=name)

    # The same changes, applied inside the original file.
    if kind == "docx":
        fixed, log = docx_fix.fix_docx(raw, result["changes"])
        produced_text = docx_fix.docx_text(fixed)
    else:
        fixed, log = pdf_fix.fix_pdf(raw, result["changes"])
        produced_text = pdf_fix.pdf_text(fixed)

    # Audit what was actually produced, not what we meant to produce. This is
    # the number the screen reports, whatever it turns out to be.
    plan = plan_fix(text, filename=name)
    check = verify(produced_text, plan)
    result["document"] = {
        **log,
        "source_format": kind,
        "after_in_file": {
            "verdict": check["audit"]["verdict"],
            "counts": check["audit"]["counts"],
            "summary": check["audit"]["summary"],
        },
        "intruders": check["intruders"],
        "verified": check["clean"],
    }
    result["verified"] = check["clean"]
    result["after"] = {
        "verdict": check["audit"]["verdict"],
        "counts": check["audit"]["counts"],
        "summary": check["audit"]["summary"],
        "cited_count": check["audit"]["cited_count"],
        "findings": check["audit"]["findings"],
    }
    # The letterhead is read from the file the officer uploaded, so the other
    # format can be rebuilt with their office on it instead of on a blank page.
    head = letterhead.extract(raw, kind)
    result["token"] = _stash({
        "name": name, "kind": kind, "bytes": fixed,
        "text": result["corrected"], "changes": result["changes"],
        "head": head,
    })
    other = "pdf" if kind == "docx" else "docx"
    result["downloads"] = {
        kind: {"template_preserved": True,
               "note": "Your original file, corrected inside itself."},
        other: {
            "template_preserved": False,
            "letterhead_carried": bool(head.get("found")),
            "note": (
                "Rebuilt from your corrected document, with your letterhead carried over "
                "from " + head.get("source", "the uploaded file") + ". The body layout is "
                "this builder's, not your original's."
                if head.get("found") else
                "Rebuilt from your corrected document. No letterhead was found in the "
                "uploaded file, so none was added."),
        },
    }
    return result


def _blocks_of(entry: dict) -> list[dict]:
    """The corrected document read back as a document, cached on the entry so
    a second download in the other format does not parse it again."""
    import doc_blocks

    if entry.get("blocks") is None:
        head = entry.get("head") or {}
        if entry["kind"] == "docx":
            entry["blocks"] = doc_blocks.from_docx(entry["bytes"])
        else:
            entry["blocks"] = doc_blocks.merge_wrapped(
                doc_blocks.from_pdf(entry["bytes"], head.get("band_pt", 0.0)))
    return entry["blocks"]


@app.get("/fix-download/{token}")
def fix_download(token: str, fmt: str = "same"):
    """The repaired document, in the format the officer picked."""
    import doc_build

    entry = _FIXED.get(token)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail="This download has expired. Run the correction again.",
        )
    stem = entry["name"].rsplit(".", 1)[0]
    want = entry["kind"] if fmt in ("same", entry["kind"]) else fmt
    if want == entry["kind"]:
        data, ext = entry["bytes"], entry["kind"]
    elif want in ("docx", "pdf"):
        # Rebuilt from the corrected file, not from the corrected text: the
        # file still records its headings, its tables and its order, and the
        # corrections are already inside it.
        blocks = _blocks_of(entry)
        head = entry.get("head")
        title = f"{stem} \u2014 corrected"
        data = (doc_build.build_docx(blocks, title, entry["changes"], head)
                if want == "docx" else
                doc_build.build_pdf(blocks, title, entry["changes"], head))
        ext = want
    else:
        raise HTTPException(status_code=400, detail="Ask for pdf or docx.")
    media = ("application/pdf" if ext == "pdf"
             else "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    return Response(
        content=data, media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{stem}-corrected.{ext}"'},
    )


@app.post("/fix-tender-upload")
async def fix_tender_upload(file: UploadFile = File(...)):
    """The same repair, from an uploaded PDF or .docx."""
    from tender_fix import fix_document

    extracted = _extract_or_422(await _read_upload(file), file.filename)
    text = extracted.get("text", "")
    if not text.strip():
        raise HTTPException(
            status_code=422,
            detail=("No text could be read from this document. A scan has no text layer to "
                    "repair \u2014 audit it first, then paste the specification."),
        )
    result = fix_document(text, filename=file.filename)
    result["extraction"] = {
        k: extracted.get(k)
        for k in ("format", "page_count", "pages_read", "characters", "chars_per_page",
                  "scanned", "note")
        if k in extracted
    }
    return result


class RecommendRequest(BaseModel):
    spec_text: str
    ui_language: str | None = None
    # Which registered retrieval pipeline to run. Absent means the default;
    # the UI never sends it. It is here so the leaderboard's alternatives can
    # be exercised end to end rather than only inside eval_pipelines.py.
    pipeline: str | None = None


class DecisionRequest(BaseModel):
    officer: str
    decision: str
    finding_kind: str | None = None
    is_number: str | None = None
    document: str | None = None
    rationale: str | None = None
    system_said: str | None = None


@app.post("/extract-text")
def extract_text(req: TextRequest):
    return {"citations": extract_citations(req.text), "characters": len(req.text)}


@app.post("/recommend")
def recommend(req: RecommendRequest):
    """Forward flow: spec text -> governing standard, related, certification,
    composed clause. Hybrid retrieval decides; composition only phrases."""
    from retrieval import recommend as _recommend

    if not req.spec_text.strip():
        raise HTTPException(status_code=400, detail="spec_text is empty")
    if len(req.spec_text) > MAX_TEXT_CHARS:
        raise HTTPException(
            status_code=413, detail=f"spec_text exceeds {MAX_TEXT_CHARS:,} characters"
        )
    import retrieval as retrieval_module

    pipeline = req.pipeline or retrieval_module.DEFAULT_PIPELINE
    if pipeline not in retrieval_module.PIPELINES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown pipeline {pipeline!r}. "
                   f"Registered: {', '.join(retrieval_module.PIPELINES)}",
        )
    if not retrieval_module.PIPELINES[pipeline].get("retrieval", True):
        raise HTTPException(
            status_code=400,
            detail=f"{pipeline!r} is a measurement baseline with no retrieval; "
                   "it is not served here.",
        )
    return _recommend(req.spec_text.strip(), req.ui_language, pipeline=pipeline)


class ReportRequest(BaseModel):
    text: str = ""
    cited: list[str] | None = None
    document: str | None = None


@app.post("/report")
def compliance_report(req: ReportRequest):
    """The audit as a document an officer can attach to a file noting.

    Rendered from the same audit_tender() result the screen draws, so the two
    cannot disagree — this is a second view of one audit, not a second audit.
    Returns standalone print-ready HTML; the browser prints it to PDF."""
    import report as report_module

    if len(req.text or "") > MAX_TEXT_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"Text exceeds {MAX_TEXT_CHARS:,} characters.",
        )
    cited = [c.strip() for c in (req.cited or []) if c and c.strip()] or None
    if not (req.text or "").strip() and not cited:
        raise HTTPException(
            status_code=400, detail="Provide specification text or at least one IS number"
        )
    result = audit_tender(req.text or "", filename=req.document or "Pasted specification",
                          cited=cited)
    return Response(content=report_module.render(result, _bis_check()),
                    media_type="text/html; charset=utf-8")


@app.get("/pipelines")
def pipelines():
    """The retrieval pipelines on offer, and the last measurement of each.

    The registry is the source of truth for what exists; the leaderboard file
    is the source of truth for how each one scored. Serving them together means
    a pipeline that has never been measured shows up as exactly that rather
    than being quietly omitted."""
    import retrieval as retrieval_module

    registry = [
        {"pipeline": name, "label": cfg["label"], "note": cfg["note"],
         "gate": bool(cfg.get("gate"))}
        for name, cfg in retrieval_module.PIPELINES.items()
    ]
    board = {}
    try:
        with open("data/pipeline_leaderboard.json", encoding="utf-8") as fh:
            board = json.load(fh)
    except (OSError, ValueError):
        board = {}
    measured = {r["pipeline"]: r for r in board.get("pipelines") or []}
    return {
        "default": retrieval_module.DEFAULT_PIPELINE,
        "generated": board.get("generated"),
        "queries": board.get("queries"),
        "recall_at": board.get("recall_at"),
        "note": board.get("note"),
        # These figures are not the ones on the Coverage screen, and the
        # difference is not an error. eval_pipelines measures the retriever:
        # raw candidates, before the voltage, material, role and status filters
        # and before the gate. eval_retrieval measures the product: what an
        # officer is actually shown after all of that. The retriever finds the
        # right standard more often than the product shows it, which is the
        # filters doing their job — and saying so here is cheaper than letting
        # two numbers for one fact sit on two screens.
        # The decision this table produced, recorded where the table is read.
        "decision": (
            "hybrid_ce stays the default, and not for accuracy. Its rank-1 lead "
            "over hybrid_rrf is 7 queries in 621 and does not survive a paired "
            "test (p=0.296); hybrid_rrf is half the latency and dense is a "
            "quarter. It keeps the place because it is the only configuration "
            "where the confidence gate can still decline: it abstains on 38 of "
            "621, hybrid_rrf on 1. On those same 38 queries hybrid_rrf answers "
            "37 confidently and is wrong on 30 — so dropping the cross-encoder "
            "would trade 30 honest abstentions for 30 confident wrong answers. "
            "In 34 of the 38 the correct standard was in the candidate list "
            "anyway, which is what the abstention is for: show the officer the "
            "shortlist, do not pick for them."
        ),
        "measures": ("the retriever alone — raw candidates, before the voltage, "
                     "material, role and status filters and before the confidence "
                     "gate. The end-to-end figures on Coverage measure what an "
                     "officer is shown after all of those, and are lower."),
        "pipelines": [{**row, **measured.get(row["pipeline"], {})} for row in registry],
    }


@app.get("/graph")
def graph(nodes: int | None = None, edges: int | None = None, min_count: int = 0,
          per_node: int | None = None):
    """The co-citation graph, trimmed to the fields the renderer draws.

    Every node is sent. Edges are deduplicated to one line per pair — the table
    stores a row per direction because confidence is directional, but drawing
    both put the same line on the canvas twice — and then each standard keeps
    its best-evidenced relationships, `per_node` of them.

    The cap exists because the evidence thresholds were lowered so an operator
    could see the whole corpus rather than only its core: nodes went from 146 to
    1,100 and the stored table from 4,904 rows to 106,379, which is 7.6 MB and
    2.3 seconds before anything appears. Every node still appears; what the cap
    drops is a standard's weakest relationships, and the response says how many
    each kept so the picture is never mistaken for the whole table.

    `nodes` and `edges` remain for callers that want a sample; the overview hero
    uses them. `per_node=0` disables the cap and sends every pair."""
    return full_graph(node_limit=nodes, edge_limit=edges, min_count=min_count,
                      per_node=per_node)


@app.get("/standards")
def standards(q: str = "", status: str = "", family: str = "", sort: str = "",
              descending: bool = False, limit: int = 100, offset: int = 0):
    """One page of the register, searched and filtered in SQL.

    This used to return all 27,687 rows — 10.2 MB — so the browser could show
    the first 250."""
    return list_standards(q=q, status=status, family=family, sort=sort,
                          descending=descending, limit=limit, offset=offset)


@app.get("/standard")
def standard(is_number: str):
    return standard_detail(is_number)


@app.get("/certifications")
def certifications(q: str = "", scheme: str = "", family: str = "", sort: str = "",
                   descending: bool = False, limit: int = 100, offset: int = 0):
    return list_certifications(q=q, scheme=scheme, family=family, sort=sort,
                               descending=descending, limit=limit, offset=offset)


@app.get("/tenders")
def tenders(q: str = "", usability: str = "", family: str = "", sort: str = "",
            descending: bool = False, limit: int = 100, offset: int = 0):
    return list_tenders(q=q, usability=usability, family=family, sort=sort,
                        descending=descending, limit=limit, offset=offset)


@app.get("/tender")
def tender(tender_id: str):
    """One document in full — the corpus drawer used to search the whole table
    in the browser to find it."""
    from engine import get_tender

    return get_tender(tender_id)


@app.get("/export/{table}.csv")
def export_csv(table: str):
    """The whole table, streamed.

    The views page now, but an export is still an export: the officer who clicks
    CSV wants every row, and streaming means the server never holds them either."""
    import csv
    import io
    import sqlite3

    from fastapi.responses import StreamingResponse

    allowed = {"standards", "tenders", "certification_rules", "co_citation",
               "coverage_gap_backlog"}
    if table not in allowed:
        raise HTTPException(status_code=404, detail=f"no exportable table named {table!r}")

    def rows():
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(f"SELECT * FROM {table}")
            buf = io.StringIO()
            writer = csv.writer(buf)
            first = cursor.fetchone()
            if first is None:
                return
            writer.writerow(first.keys())
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)
            for row in [first] + cursor.fetchall():
                writer.writerow([row[k] for k in row.keys()])
                yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        finally:
            conn.close()

    return StreamingResponse(
        rows(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="manak-setu-{table}.csv"'},
    )


@app.get("/backlog")
def backlog():
    return list_backlog()


@app.get("/stats")
def stats():
    return corpus_stats()


@app.get("/peers")
def peers(text: str):
    """What other government buyers of a similar item cited.

    A tally over the corpus, not a recommendation — the wording of the response
    says so, and withdrawn standards are returned with their status rather than
    filtered out, because peers citing something dead is worth seeing."""
    from engine import peer_citations

    if not (text or "").strip():
        raise HTTPException(status_code=400, detail="text is required")
    return peer_citations(text)


@app.get("/calibration")
def calibration():
    """How often a score of a given size was actually right.

    Written by `eval_retrieval.py` over the golden set. Absent until that has
    been run, and the page says nothing about calibration rather than implying
    a number."""
    import json as _json

    try:
        with open("data/calibration.json", encoding="utf-8") as fh:
            return _json.load(fh)
    except (OSError, ValueError):
        return {"measured": False}


@app.get("/health-index")
def health_index():
    """How healthy are the standards government buyers actually cite?

    Computed live from the tender corpus joined to BIS status, so it tracks the
    corpus as it grows rather than being a figure written down once."""
    from health_index import build

    return build()


@app.get("/benchmark")
def benchmark():
    return run_benchmark()


@app.get("/graph/{is_number:path}")
def graph_neighbourhood(is_number: str, depth: int = 1):
    """The graph around one standard — what the evidence panel draws."""
    return neighbourhood(is_number, depth=depth)


@app.get("/evidence")
def evidence(is_number: str):
    """Which real published tenders cite this standard, with their links."""
    return corpus_evidence(is_number)


@app.post("/decision")
def decision(req: DecisionRequest):
    """Log an officer's verdict on a finding. The system recommends; a person
    decides; the decision is what an auditor reads a year later."""
    try:
        return record_decision(
            officer=req.officer,
            decision=req.decision,
            finding_kind=req.finding_kind,
            is_number=req.is_number,
            document=req.document,
            rationale=req.rationale,
            system_said=req.system_said,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/decisions")
def decisions(limit: int = 200):
    return list_decisions(limit=limit)


class QueryRequest(BaseModel):
    text: str
    route: str | None = None      # "draft" | "audit" to override the router


@app.post("/query")
def query(req: QueryRequest):
    """Orchestration: one entry point, routed by what the text actually is.

    A specification names a product and wants a standard. A tender already cites
    standards and wants them checked. Callers that cannot know which they hold —
    the browser extension, a future integration — post here and let the router
    decide, with its reasoning returned so the choice is never invisible."""
    from engine import extract_citations

    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is empty")
    if len(text) > MAX_TEXT_CHARS:
        raise HTTPException(status_code=413, detail=f"text exceeds {MAX_TEXT_CHARS:,} characters")

    citations = extract_citations(text)
    # The router reads citations, which survive translation, so it can decide
    # before any language handling happens.
    if req.route in ("draft", "audit"):
        route, why = req.route, "caller specified the route"
    elif len(citations) >= 2:
        route = "audit"
        why = (f"{len(citations)} IS numbers are already cited, so this reads as an existing "
               "tender to be checked rather than a specification needing one")
    elif citations:
        route = "audit"
        why = ("one standard is already cited, so it is checked rather than replaced")
    else:
        route = "draft"
        why = "no IS number is cited, so this reads as a specification needing a standard"

    routing = {"route": route, "reason": why, "citations_found": len(citations)}
    if route == "audit":
        result = audit_tender(text, filename="routed query", cited=citations or None)
    else:
        from retrieval import recommend as _recommend

        result = _recommend(text)
    return {"routing": routing, "result": result}


class TranslateRequest(BaseModel):
    texts: list[str]
    target: str


@app.post("/translate")
def translate(req: TranslateRequest):
    """Translate interface text for the language switcher.

    Cached on disk and keyed by (target, text), because a government console
    shows the same few hundred labels to everyone: the first viewer in a
    language pays for the call, nobody after them does. Without the cache a
    free-tier quota is gone in a handful of page loads.

    This endpoint is for *interface* text. Standard titles and IS numbers are
    excluded by the caller and never sent.
    """
    import multilingual

    if not req.texts:
        return {"translations": {}, "cached": 0, "fetched": 0}
    if len(req.texts) > 400:
        raise HTTPException(status_code=413, detail="At most 400 strings per request")
    return multilingual.translate_ui_batch(req.texts, req.target)


@app.get("/llm")
def llm_status():
    """Whether a local model is actually available. The UI reads this so the
    clause panel can say how the text in front of you was produced."""
    import llm

    return llm.status()


@app.get("/health")
def health():
    conn = sqlite3.connect(DB_PATH)
    try:
        counts = {}
        for table in (
            "standards",
            "tenders",
            "co_citation",
            "certification_rules",
            "coverage_gap_backlog",
        ):
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        import llm
        from retrieval import LIGHT_MODE

        return {
            "status": "ok",
            "retrieval_mode": "light (no cross-encoder)" if LIGHT_MODE else "full",
            "version": VERSION,
            "dataset_date": _dataset_date(),
            "dataset_date_note": "when the data files were last written, not when BIS was last checked",
            "bis_check": _bis_check(),
            "row_counts": counts,
            "llm": llm.status(),
        }
    finally:
        conn.close()


class NoCacheStatic(StaticFiles):
    """Browsers were serving a stale app.js after edits, because a cache-buster
    on the page URL does not invalidate its subresources. Demo machines must
    never show yesterday's build."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        # `no-cache` rather than `no-store`: the browser still checks on every
        # request, so a demo machine can never run yesterday's build, but an
        # unchanged app.js comes back as a 304 instead of 125 KB. StaticFiles
        # already sets etag and last-modified, so the check is free.
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


# Mounted last so it never shadows the API routes above. Serving the UI from the
# same origin means there is one port to share when demoing over LAN or a tunnel.
if os.path.isdir("frontend"):
    app.mount("/", NoCacheStatic(directory="frontend", html=True), name="frontend")
