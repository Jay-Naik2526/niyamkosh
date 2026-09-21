"""Collect real tender specifications from GeM, the Government e-Marketplace.

Every bid published on GeM has a public bid document at

    https://bidplus.gem.gov.in/showbidDocument/<bid id>

The bid form itself rarely cites a standard — it is a bilingual cover sheet.
But it carries link annotations to its attachments: the buyer's specification
documents, technical-compliance sheets and BOQs, hosted on gem.gov.in. Those are
where the IS numbers are written. So the collector reads the bid PDF for its
links, fetches the attachments that are PDFs, and extracts the citations that
are literally present in them. Nothing is inferred.

Bid ids are sequential integers, so the corpus can be sampled honestly across
time rather than cherry-picked around products we already know. A bid with no
specification attachments (most service bids) is skipped and counted; a bid
whose attachments are scans is kept as "Not extractable" so it is never
mistaken for a tender that cites nothing.

Writes data/tender_collected_gem.csv in the same schema as tender_dataset.csv.
Merging into the corpus is a separate, reviewed step: merge_gem_tenders.py.
The PDFs are kept under data/tenders/gem/ for re-extraction and are not
committed — they are public documents, but the repo carries citations and
source links, not copies.

    python collect_gem_tenders.py --sample 80                 # yield probe
    python collect_gem_tenders.py --sample 1500 --seed 3      # a real sweep
    python collect_gem_tenders.py --ids 6713228 9569038       # specific bids
"""

import argparse
import collections
import concurrent.futures
import io
import json
import os
import random
import re
import sqlite3
import time
import urllib.error
import urllib.request

import pandas as pd
from pypdf import PdfReader

from engine import extract_document

BID_URL = "https://bidplus.gem.gov.in/showbidDocument/{}"
OUT = "data/tender_collected_gem.csv"
PDF_DIR = "data/tenders/gem"
PROGRESS = "data/tender_collected_gem.progress.jsonl"
DB = "manak_setu.db"

# Observed live range: ids below ~6.7M are 2023 and earlier; ids above ~9.75M
# do not exist yet. Sampling across this span spreads the corpus over 2024-2026.
ID_LOW, ID_HIGH = 6_700_000, 9_750_000

DELAY_SECONDS = 0.5
WORKERS = int(os.getenv("MANAK_GEM_WORKERS", "4"))   # concurrent bids; each still pauses between its own requests
TIMEOUT = 45
MAX_ATTACHMENTS = 8
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36 "
                    "(NiyamKosh SIH26108 research prototype)"}

# Attachment routes that carry documents. Catalogue pages, SLA forms, PVC
# certificates and the bid's own download stub are not specifications.
ATTACHMENT_RE = re.compile(
    r"https://(?:mkp|bidplus)\.gem\.gov\.in/"
    r"(?:catalog_data/catalog_support_document/|bidding/bid/documentdownload/|resources/upload_nas/)"
    r"\S+", re.I,
)
BID_NUMBER_RE = re.compile(r"\bGEM/20\d\d/B/\d{6,8}\b")
# A label line in the bid form's two-column table: "/Item Category", "Total
# Quantity/ 10000", "Item Category/ ". Values wrap around the label, so the
# category is the run of non-label lines on either side of it.
LABEL_LINE_RE = re.compile(
    r"^\s*/|/\s*$|^[A-Z][A-Za-z() ]{2,45}/\s*\S"
    # Labels whose slash fell on the next line still start with a known phrase.
    r"|^(?:MSE |Minimum |Turnover|Past |Bid |OEM |Consignee|Estimated |Evaluation|Startup |Years |Document|Total )"
)
# The bid form is bilingual and its Hindi font leaves glyph ids in the text
# layer: "Item Category/मद (cid:18)(cid:18) testing weather proof PVC…". Those,
# and the Devanagari itself, are stripped so only the English line remains.
CID_RE = re.compile(r"\(cid:\d+\)|[\u0900-\u097F]+")
# Service bids carry scopes of work, not product specifications; their
# attachments were read and never cited a standard. The category names the kind.
SERVICE_RE = re.compile(r"\b(service|services|hiring|manpower|consultanc|outsourc|AMC|annual maintenance|"
                        r"repair|housekeeping|security guard|catering|transport|custom bid for services|"
                        r"leasing|rental|printing|training|survey|audit)\b", re.I)
FOREIGN_RE = re.compile(r"\b(?:IEC|ISO|ASTM|BS\s?EN|BS|DIN|EN|IEEE|ANSI)\s*[:\-]?\s*\d{2,6}(?:[-\s]?\d+)?\b")


def fetch(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def attachment_links(pdf_bytes: bytes) -> list[str]:
    """URLs the bid document links to. These live in annotations, not text."""
    links: list[str] = []
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception:                                    # noqa: BLE001
        return links
    for page in reader.pages:
        for annot in page.get("/Annots") or []:
            try:
                action = annot.get_object().get("/A") or {}
                uri = str(action.get("/URI") or "")
            except Exception:                            # noqa: BLE001
                continue
            if ATTACHMENT_RE.match(uri) and uri not in links:
                links.append(uri)
    return links[:MAX_ATTACHMENTS]


def register() -> tuple[dict[str, dict], dict[str, str]]:
    """Status and family by IS base number, for Outdated / Unmatched columns."""
    conn = sqlite3.connect(DB)
    try:
        rows = conn.execute('SELECT "IS Base", "Status", "Product Family" FROM standards').fetchall()
    finally:
        conn.close()
    by_base = {}
    for base, status, family in rows:
        by_base.setdefault(str(base).strip().upper(), {"status": status, "family": family})
    return by_base, {}


def base_of(citation: str) -> str:
    return re.sub(r"\s+", " ", citation.split("(")[0].split(":")[0]).strip().upper()


def classify(citations: list[str], reg: dict[str, dict]) -> tuple[str, list[str], list[str]]:
    """Family by majority of resolved citations; outdated and unmatched lists.
    Derived from the register, never guessed from the document's wording."""
    families = collections.Counter()
    outdated, unmatched = [], []
    for c in citations:
        hit = reg.get(base_of(c))
        if not hit:
            unmatched.append(c)
            continue
        if hit["family"]:
            families[hit["family"]] += 1
        if hit["status"] in ("Withdrawn", "Superseded"):
            outdated.append(f"{c} ({hit['status']})")
    family = families.most_common(1)[0][0] if families else "Unclassified"
    return family, outdated, unmatched


# The bid form's buyer block is bilingual on one line: the English label, its
# Devanagari translation, then the value.
#
#     Ministry/State Name/<devanagari> Ministry Of Chemicals And Fertilizers
#
# CID_RE has already removed the Devanagari by the time this runs, leaving the
# label, some stray slashes, and the value. So the value is what follows the
# English label on its own line. That is a property of how GeM lays the form
# out, not a guess about the buyer, and it means the value is stored exactly as
# printed.
BUYER_LABELS = {
    "Ministry": "Ministry/State Name",
    "Department": "Department Name",
    "Organisation": "Organisation Name",
    "Office": "Office Name",
}
# GeM redacts some office names to a run of asterisks. That is not a value.
_REDACTED = re.compile(r"^[*\s]+$")


def buyer_field(bid_text: str, label: str) -> str:
    """The value GeM prints against one buyer label, or "" when it is absent.

    Nothing is normalised and nothing is inferred: "Pmo" stays "Pmo" rather than
    becoming "Prime Minister's Office", because a ministry name this system
    invented would be indistinguishable on screen from one a buyer wrote. An
    absent or redacted label yields an empty string, which every downstream
    figure counts as unknown rather than folding into a total.
    """
    lines = [re.sub(r"\s+", " ", ln).strip()
             for ln in CID_RE.sub(" ", bid_text).splitlines()]
    for i, line in enumerate(lines):
        if label not in line:
            continue
        value = line.split(label, 1)[1].strip(" :/-,")
        if not value:
            # The value wrapped onto the next line; take it unless it is
            # another label.
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if nxt and not LABEL_LINE_RE.search(nxt):
                value = nxt.strip(" :/-,")
        if not value or _REDACTED.match(value):
            return ""
        return value[:120]
    return ""


def extract_buyer(bid_text: str) -> dict[str, str]:
    """All four buyer fields from one saved bid form."""
    return {col: buyer_field(bid_text, label) for col, label in BUYER_LABELS.items()}


def extract_category(bid_text: str) -> str:
    """The bid's "Item Category" as GeM prints it.

    The text layer of the form is a two-column table read row by row, so a long
    value wraps around its own label:

        XLPE Cable, Working Voltage from 3.3 kV up to and
         /Item Category
        including 33 kV (V2) ISI Marked to IS 7098 (Part 2) (Q2)

    The value is therefore the non-label lines immediately before and after the
    label, bounded by the neighbouring labels. Glyph ids from the Hindi font and
    the Devanagari itself are stripped first."""
    lines = [ln.strip() for ln in CID_RE.sub(" ", bid_text).splitlines()]
    lines = [re.sub(r"\s+", " ", ln) for ln in lines]
    idx = next((i for i, ln in enumerate(lines) if "Item Category" in ln), None)
    if idx is None:
        return ""
    label = lines[idx]
    parts: list[str] = []
    # Text sharing the label's own line, on either side of it:
    # "Item Category/ SLIDE TRACK ..." or "JOINTS FOR 11kV XLPE CABLE /Item Category".
    lead, _, tail = label.partition("Item Category")
    same = " ".join(x for x in (lead.strip(" /"), tail.strip(" /")) if x)
    before: list[str] = []
    for ln in reversed(lines[max(0, idx - 2):idx]):
        if not ln or LABEL_LINE_RE.search(ln):
            break
        before.insert(0, ln)
    after: list[str] = []
    for ln in lines[idx + 1:idx + 4]:
        if not ln or LABEL_LINE_RE.search(ln):
            break
        after.append(ln)
    parts = before + ([same] if same else []) + after
    category = " ".join(parts)
    # GeM prefixes catalogue items it could not match with "Custom-"; the words
    # after it are the buyer's own name for the item, so only the tag is dropped.
    category = re.sub(r"\bCustom(?:-|\s+Bid\s+for\s+Goods)?\s*", "", category)
    category = re.split(r"\s*Total\s+\d", category, maxsplit=1)[0]
    category = re.sub(r"\s*,\s*", " , ", category).strip(" ,")
    # Stripping the Devanagari leaves its brackets behind: "(Q2) ((33 ))" → "(Q2)".
    category = re.sub(r"\s*\(\(.*$", "", category)
    return category.strip(" :/-,")[:140]


def collect_bid(bid: int, reg: dict[str, dict]) -> dict | None:
    """One bid → one row, or None with a reason in the progress log."""
    pdf = fetch(BID_URL.format(bid))
    if not pdf or not pdf.startswith(b"%PDF-"):
        return {"bid": bid, "outcome": "no bid document"}
    links = attachment_links(pdf)
    if not links:
        return {"bid": bid, "outcome": "no specification attachments"}

    bid_text = extract_document(pdf, f"{bid}.pdf").get("text", "")
    number = (BID_NUMBER_RE.search(bid_text) or [None])[0] if BID_NUMBER_RE.search(bid_text) else f"GEM-bid-{bid}"
    category = extract_category(bid_text)
    buyer = extract_buyer(bid_text)
    if category and SERVICE_RE.search(category):
        return {"bid": bid, "outcome": "service bid", "category": category}

    citations: list[str] = []
    foreign: list[str] = []
    scanned_docs, text_docs, source = 0, 0, ""
    os.makedirs(PDF_DIR, exist_ok=True)
    # The bid form is kept too, so a title can be re-derived offline if the
    # category heuristic improves — without another round trip to the portal.
    with open(os.path.join(PDF_DIR, f"{bid}-bid.pdf"), "wb") as fh:
        fh.write(pdf)
    for i, url in enumerate(links):
        time.sleep(DELAY_SECONDS)
        doc = fetch(url)
        if not doc or not doc.startswith(b"%PDF-"):
            continue
        try:
            r = extract_document(doc, os.path.basename(url))
        except Exception:                                # noqa: BLE001
            continue
        with open(os.path.join(PDF_DIR, f"{bid}-{i}.pdf"), "wb") as fh:
            fh.write(doc)
        if r.get("scanned"):
            scanned_docs += 1
            continue
        text_docs += 1
        found = r.get("citations") or []
        if found and not source:
            source = url
        for c in found:
            if c not in citations:
                citations.append(c)
        for f in FOREIGN_RE.findall(r.get("text", "")):
            f = re.sub(r"\s+", " ", f).strip()
            if f not in foreign:
                foreign.append(f)

    if not text_docs and not scanned_docs:
        return {"bid": bid, "outcome": "attachments not fetchable"}
    if not citations:
        if scanned_docs and not text_docs:
            usability, doc_type = "Not extractable", "Scanned PDF"
        else:
            return {"bid": bid, "outcome": "specification cites no IS", "category": category}
    else:
        usability, doc_type = "Usable", "Text PDF"

    family, outdated, unmatched = classify(citations, reg)
    return {
        "bid": bid,
        "outcome": usability,
        "row": {
            "Tender ID": number,
            "Product Family": family,
            "IS Numbers Cited": "; ".join(citations),
            "Foreign Standards Cited": "; ".join(foreign[:12]),
            "Count": len(citations),
            "Outdated Citations": "; ".join(outdated),
            "Any Outdated": ("Yes" if outdated else "No") if citations else "Not checked",
            "Document Type": doc_type,
            "Usability": usability,
            "Source Link": source or links[0],
            "Unmatched Citations": "; ".join(unmatched),
            "Item Category": category,
            "Ministry": buyer["Ministry"],
            "Department": buyer["Department"],
            "Organisation": buyer["Organisation"],
            "Office": buyer["Office"],
            "GeM Bid Id": bid,
            "Attachments Read": text_docs + scanned_docs,
        },
    }


def done_ids() -> set[int]:
    if not os.path.exists(PROGRESS):
        return set()
    with open(PROGRESS, encoding="utf-8") as fh:
        return {json.loads(line)["bid"] for line in fh if line.strip()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0, help="how many bid ids to sample")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--ids", type=int, nargs="*", help="specific bid ids")
    ap.add_argument("--rederive", action="store_true",
                    help="recompute Item Category for collected rows from the saved bid forms")
    ap.add_argument("--markscan", action="store_true",
                    help="record whether each collected document demands the BIS "
                         "Standard Mark anywhere in its attachments")
    ap.add_argument("--recite", action="store_true",
                    help="re-extract citations for collected rows from the saved attachments")
    args = ap.parse_args()

    if args.markscan:
        # Whether a tender demands certified material can only be answered by
        # reading the tender, so this reads the saved attachments once and
        # records the answer per row.
        #
        # The two answers are not equally strong, and the reporting has to
        # respect that. "No" is solid: none of the phrases that demand a
        # Standard Mark, a licence number or certified material appear anywhere
        # in the specification, so as written uncertified goods meet it. "Yes"
        # is weak: a 60-page tender mentioning BIS somewhere is not proof that
        # it demands the mark *for the item under compulsory certification*. So
        # every figure built on this leans on the absence, never the presence.
        import glob

        import pdfplumber

        from audit import MARK_RE
        from engine import MAX_PAGES

        df = pd.read_csv(OUT, encoding="utf-8-sig")
        if "Demands Standard Mark" not in df.columns:
            df["Demands Standard Mark"] = ""
        read = yes = no = 0
        for i, bid in enumerate(df["GeM Bid Id"]):
            paths = [q for q in sorted(glob.glob(os.path.join(PDF_DIR, f"{int(bid)}-*.pdf")))
                     if not q.endswith("-bid.pdf")]
            if not paths:
                df.at[i, "Demands Standard Mark"] = "No attachment read"
                continue
            found = None
            for path in paths:
                try:
                    with pdfplumber.open(path) as pdf:
                        text = "\n".join((pg.extract_text() or "")
                                         for pg in pdf.pages[:MAX_PAGES])
                except Exception:                            # noqa: BLE001
                    continue
                found = bool(found) or bool(MARK_RE.search(text))
            if found is None:
                df.at[i, "Demands Standard Mark"] = "No attachment read"
                continue
            read += 1
            df.at[i, "Demands Standard Mark"] = "Yes" if found else "No"
            yes += bool(found)
            no += not found
            if read % 200 == 0:
                df.to_csv(OUT, index=False)
                print(f"  {read} documents read · {yes} demand the mark · {no} do not",
                      flush=True)
        df.to_csv(OUT, index=False)
        print(f"read {read} documents of {len(df)} rows")
        print(f"  demand a Standard Mark somewhere : {yes}")
        print(f"  no such language anywhere        : {no}")
        return

    if args.recite:
        # The citation pattern changed, so the stored lists were produced by the
        # old one. The attachments were kept for exactly this: re-reading them
        # costs no round trip to the portal and no re-download.
        import glob

        import pdfplumber

        from engine import MAX_PAGES, extract_citations

        reg, _ = register()
        df = pd.read_csv(OUT, encoding="utf-8-sig")
        changed = dropped = 0
        for i, bid in enumerate(df["GeM Bid Id"]):
            paths = [q for q in sorted(glob.glob(os.path.join(PDF_DIR, f"{int(bid)}-*.pdf")))
                     if not q.endswith("-bid.pdf")]
            if not paths:
                continue
            citations: list[str] = []
            for path in paths:
                try:
                    with pdfplumber.open(path) as pdf:
                        text = "\n".join((pg.extract_text() or "") for pg in pdf.pages[:MAX_PAGES])
                except Exception:                            # noqa: BLE001
                    continue
                for c in extract_citations(text):
                    if c not in citations:
                        citations.append(c)
            before = str(df.at[i, "IS Numbers Cited"] or "")
            after = "; ".join(citations)
            if after == before:
                continue
            changed += 1
            dropped += max(0, len([x for x in before.split(";") if x.strip()]) - len(citations))
            family, outdated, unmatched = classify(citations, reg)
            df.at[i, "IS Numbers Cited"] = after
            df.at[i, "Count"] = len(citations)
            df.at[i, "Product Family"] = family
            df.at[i, "Outdated Citations"] = "; ".join(outdated)
            df.at[i, "Any Outdated"] = ("Yes" if outdated else "No") if citations else "Not checked"
            df.at[i, "Unmatched Citations"] = "; ".join(unmatched)
            if not citations and df.at[i, "Usability"] == "Usable":
                df.at[i, "Usability"] = "Not extractable"
                df.at[i, "Document Type"] = "Text PDF"
            if (i + 1) % 200 == 0:
                df.to_csv(OUT, index=False)
                print(f"  {i + 1}/{len(df)} · {changed} rows changed", flush=True)
        df.to_csv(OUT, index=False)
        usable = int((df["Usability"] == "Usable").sum())
        print(f"re-extracted {len(df)} rows · {changed} changed · "
              f"{dropped} citations dropped · {usable} usable")
        return

    if args.rederive:
        # The saved bid form is the only record of who was buying. Re-reading it
        # costs no round trip, and the four buyer fields are read the same way
        # the category is — from the label GeM prints, exactly as printed.
        df = pd.read_csv(OUT, encoding="utf-8-sig")
        for col in ("Ministry", "Department", "Organisation", "Office"):
            if col not in df.columns:
                df[col] = ""
        changed = buyers = read = 0
        for i, bid in enumerate(df["GeM Bid Id"]):
            path = os.path.join(PDF_DIR, f"{int(bid)}-bid.pdf")
            if not os.path.exists(path):
                continue
            read += 1
            with open(path, "rb") as fh:
                text = extract_document(fh.read(), path).get("text", "")
            cat = extract_category(text)
            if cat and cat != str(df.at[i, "Item Category"]):
                df.at[i, "Item Category"] = cat
                changed += 1
            found = extract_buyer(text)
            if any(found.values()):
                buyers += 1
            for col, value in found.items():
                df.at[i, col] = value
            if read % 400 == 0:
                df.to_csv(OUT, index=False)
                print(f"  {read} bid forms read · {buyers} carry a buyer", flush=True)
        df.to_csv(OUT, index=False)
        titled = int(df["Item Category"].fillna("").astype(str).str.strip().astype(bool).sum())
        named = int(df["Ministry"].fillna("").astype(str).str.strip().astype(bool).sum())
        print(f"read {read} saved bid forms of {len(df)} rows")
        print(f"re-derived {changed} titles; {titled}/{len(df)} rows carry one")
        print(f"buyer named on {named}/{len(df)} rows "
              f"({read - buyers} of the {read} forms read carried no buyer block)")
        return

    rng = random.Random(args.seed)
    ids = list(args.ids or [])
    if args.sample:
        ids += rng.sample(range(ID_LOW, ID_HIGH), args.sample)
    skip = done_ids()
    ids = [i for i in ids if i not in skip]
    if not ids:
        raise SystemExit("nothing to do — every requested id is already in the progress log")

    reg, _ = register()
    rows = []
    if os.path.exists(OUT):
        rows = pd.read_csv(OUT, encoding="utf-8-sig").to_dict("records")
    outcomes = collections.Counter()
    print(f"{len(ids)} bids to read · {len(rows)} rows already collected · {len(skip)} ids already tried")

    def work(bid):
        try:
            return collect_bid(bid, reg) or {"bid": bid, "outcome": "error"}
        except Exception as exc:                          # noqa: BLE001
            return {"bid": bid, "outcome": f"error: {type(exc).__name__}"}

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS)
    for n, result in enumerate(pool.map(work, ids), 1):
        outcomes[result["outcome"]] += 1
        with open(PROGRESS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({k: v for k, v in result.items() if k != "row"}) + "\n")
        if "row" in result:
            rows.append(result["row"])
            pd.DataFrame(rows).to_csv(OUT, index=False)
            r = result["row"]
            print(f"  [{n}/{len(ids)}] {result['bid']}  {r['Usability']:<16} {r['Count']:>2} IS  "
                  f"{r['Product Family'][:28]:<30} {r['Item Category'][:40]}", flush=True)
        else:
            print(f"  [{n}/{len(ids)}] {result['bid']}  · {result['outcome']}", flush=True)
    pool.shutdown()

    print("\noutcomes:")
    for k, v in outcomes.most_common():
        print(f"  {v:>5}  {k}")
    usable = sum(1 for r in rows if r.get("Usability") == "Usable")
    print(f"\n{len(rows)} rows in {OUT} · {usable} usable · "
          f"{sum(int(r.get('Count') or 0) for r in rows)} citations in total")


if __name__ == "__main__":
    main()
