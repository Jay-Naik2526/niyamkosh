"""Collect BIS Product Certification (Scheme I / ISI Mark) from the BIS website.

The problem statement asks the system to suggest mandatory certification
requirements and names three schemes: BIS Product Certification, CRS and
Hallmarking. The register held 77 rows, all QCO, all from one earlier collection.

BIS publishes the full Scheme I list — every product under compulsory ISI-mark
certification, with its IS number and the Quality Control Order that made it
mandatory — as an HTML table at

  bis.gov.in/product-certification/products-under-compulsory-certification/
      scheme-i-mark-scheme/?lang=en

The ?lang=en matters: the page is served in Hindi by default, and the Hindi
version cannot be joined to an English register.

The table is grouped: a single full-width row names a QCO, and the product rows
beneath it belong to that order. That grouping is the notification reference,
which is the field an officer needs to defend the clause, so it is carried down
rather than discarded.

Nothing is invented. A product with no IS number is skipped rather than guessed
at. Writes data/certification_scheme1.csv and leaves the existing
certification_rules.csv untouched.
"""

import argparse
import html
import re
import urllib.request

import pandas as pd

BASE = "https://www.bis.gov.in/product-certification/products-under-compulsory-certification/"

# ?lang=en is not optional. The site serves Hindi by default, and the Hindi table
# cannot be joined to an English register — an earlier attempt read the Hindi page
# and concluded, wrongly, that CRS was not published as a table at all.
SCHEMES = {
    "BIS Product Certification (ISI Mark, Scheme I)": BASE + "scheme-i-mark-scheme/?lang=en",
    "CRS (Compulsory Registration Scheme, Scheme II)": BASE + "scheme-ii-registration-scheme/?lang=en",
}
URL = SCHEMES["BIS Product Certification (ISI Mark, Scheme I)"]
OUT = "data/certification_schemes.csv"
HEADERS = {"User-Agent": "Mozilla/5.0 (NiyamKosh SIH26108 research prototype)"}

IS_RE = re.compile(r"^IS[\s:/]*\d{2,6}", re.I)


def text_of(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def base_of(is_number: str) -> str:
    return re.sub(r"\s+", " ", str(is_number).split("(")[0].split(":")[0]).strip()


def fetch(url: str = URL) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read().decode("utf-8", "replace")


def parse(page: str, scheme: str, source: str) -> list[dict]:
    """Read every table on the page, not just the largest.

    Scheme I is one long grouped table; Scheme II is four smaller ones, and
    taking only the biggest silently dropped three quarters of the CRS list."""
    tables = re.findall(r"<table.*?</table>", page, re.S | re.I)
    if not tables:
        raise SystemExit(f"no table on {source} — the layout has changed")
    rows = [r for t in tables for r in re.findall(r"<tr.*?</tr>", t, re.S | re.I)]

    out: list[dict] = []
    group = ""            # the category heading currently in force
    notification = ""
    for row in rows:
        cells = [text_of(c) for c in re.findall(r"<t[dh].*?</t[dh]>", row, re.S | re.I)]
        cells = [c for c in cells if c]
        if not cells:
            continue
        # A single-cell row is a group heading: the product category under one QCO.
        if len(cells) == 1:
            group = cells[0]
            continue
        if cells[0].lower().startswith(("sr", "sl.", "sl ", "s.no")):
            continue

        is_number = next((c for c in cells if IS_RE.match(c)), "")
        if not is_number:
            continue
        rest = [c for c in cells if c is not is_number and not re.fullmatch(r"\d+\.?", c)]
        product = next((c for c in rest if c != is_number and not c.lower().startswith(
            ("1. ", "2. ", "3. "))), "")
        notif = next((c for c in rest if re.search(
            r"quality control|order|s\.?o\.?\s*\d|gazette", c, re.I)), "")
        if notif:
            notification = notif

        out.append({
            "IS Number": is_number.replace("IS ", "IS ").strip(),
            "Product Description": product,
            "BIS Product Category": group,
            "Product Family": group,
            "Certification Mandatory": "Yes",
            "Scheme": scheme,
            "Notification Reference": notification,
            "Source Link": source,
            "IS Base": base_of(is_number),
        })
    return out


HALLMARK_URL = "https://www.bis.gov.in/hallmarking-overview/?lang=en"


def parse_hallmarking(page: str) -> list[dict]:
    """Hallmarking is not published as a table, so it is read from prose.

    The overview page names the standards that govern hallmarking and says what
    each one is for. Those sentences are extracted verbatim rather than typed in
    from memory, so every row can be traced back to a line on a BIS page. This
    yields a handful of rows, not a catalogue — hallmarking covers precious-metal
    articles, which is a narrow slice of public procurement.
    """
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page)))
    out, seen = [], set()
    for m in re.finditer(r"[^.]{0,200}\bIS\s*(\d{3,6})\s*:?\s*(\d{4})?[^.]{0,200}\.", text):
        number = f"IS {m.group(1)}"
        if number in seen:
            continue
        sentence = m.group(0).strip()
        # Only keep sentences that actually describe the standard's role.
        if not re.search(r"hallmark|assay|fineness|purity|jewellery|precious", sentence, re.I):
            continue
        seen.add(number)
        # Trim page furniture ("5 MB) Read More »") off the front: start the
        # description at the first capital letter that begins real prose.
        clean = re.sub(r"^.*?(?=\b[A-Z][a-z]{3,})", "", sentence, count=1) or sentence
        clean = re.sub(r"\s*Read More\s*»?\s*", " ", clean).strip(" ·-–—")
        out.append({
            "IS Number": number,
            "Product Description": clean[:280],
            "BIS Product Category": "Hallmarking of precious metal articles",
            "Product Family": "Hallmarking",
            "Certification Mandatory": "Yes",
            "Scheme": "Hallmarking",
            "Notification Reference": "See BIS Hallmarking Regulations; quoted from the BIS hallmarking overview page",
            "Source Link": HALLMARK_URL,
            "IS Base": base_of(number),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows: list[dict] = []
    for scheme, url in SCHEMES.items():
        print(f"fetching {scheme}")
        got = parse(fetch(url), scheme, url)
        print(f"   {len(got)} products")
        rows += got

    print("fetching Hallmarking")
    hall = parse_hallmarking(fetch(HALLMARK_URL))
    print(f"   {len(hall)} standards named on the BIS hallmarking page")
    rows += hall
    df = pd.DataFrame(rows).drop_duplicates(subset=["IS Number", "Product Description"])

    print(f"\nproducts under compulsory certification: {len(df)}")
    for k, v in df["Scheme"].value_counts().items():
        print(f"   {v:>4}  {k}")
    print(f"distinct IS numbers                              : {df['IS Base'].nunique()}")
    print(f"with a notification reference                    : "
          f"{(df['Notification Reference'].str.len() > 0).sum()}")
    print(f"product categories                               : {df['BIS Product Category'].nunique()}")
    print("\nsample:")
    for _, r in df.head(6).iterrows():
        print(f"  {r['IS Number']:<22} {r['Product Description'][:46]:<48} {r['BIS Product Category'][:24]}")

    if args.dry_run:
        print("\ndry run — nothing written")
        return
    df.to_csv(OUT, index=False)
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
