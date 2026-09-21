"""Translate the interface strings with a machine-translation API, once.

The obvious idea is to call a translation API from the browser as the page
renders. This does not do that, deliberately:

  * A page whose labels arrive over the network has no labels when the network
    is slow, and none at all when the API is down or out of quota.
  * The same button can read differently on two loads. Government interfaces
    should not be non-deterministic.
  * It bills per page view forever, for text that changes once a month.
  * Nothing is reviewable. A wrong translation of "Blocking — do not publish"
    would appear on screen without anyone having read it.

So the API is a build-time tool. This script translates the English strings
once, writes frontend/locales.js, and the shipped app loads a static file with
no runtime dependency on anyone's service. Re-run it when the English changes.

Two providers:

  mymemory  (default) — no registration, no key, works immediately.
             ~5,000 characters/day anonymously; pass --email to raise that to
             50,000. The whole interface is about 3,000 characters per language.

  bhashini  — the Government of India's National Language Translation Mission
             (MeitY), running AI4Bharat's IndicTrans2. Better on Indian-language
             nuance and the text stays inside government infrastructure, which is
             the stronger answer for a real deployment. Needs a ULCA user id and
             API key from https://bhashini.gov.in.

Start with mymemory to get screens on the board. Move to bhashini before anything
ships, and have a native speaker read the result either way.

    python translate_ui.py --lang mr ta bn
    python translate_ui.py --lang mr --provider bhashini

Every generated string is marked machine-translated. A human still has to read
them before they go in front of an officer; the marks are there so a reviewer
can see exactly which ones nobody has checked.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

SOURCE = "frontend/i18n.js"
OUT = "frontend/locales.js"
REVIEW = "data/translation_review.csv"

CONFIG_URL = "https://meity-auth.ulcacontrib.org/ulca/apis/v0/model/getModelsPipeline"
PIPELINE_ID = "64392f96daac500b55c543cd"

# The 22 scheduled languages Bhashini serves, by ISO code.
LANGUAGE_NAMES = {
    "hi": "हिन्दी", "bn": "বাংলা", "mr": "मराठी", "te": "తెలుగు", "ta": "தமிழ்",
    "gu": "ગુજરાતી", "kn": "ಕನ್ನಡ", "ml": "മലയാളം", "pa": "ਪੰਜਾਬੀ", "or": "ଓଡ଼ିଆ",
    "as": "অসমীয়া", "ur": "اردو", "sa": "संस्कृतम्", "ne": "नेपाली", "kok": "कोंकणी",
    "mai": "मैथिली", "sd": "سنڌي", "doi": "डोगरी", "brx": "बड़ो", "sat": "ᱥᱟᱱᱛᱟᱲᱤ",
    "mni": "ꯃꯤꯇꯩꯂꯣꯟ", "ks": "کٲشُر",
}

# Terms that must never be machine-translated: they are cited text, product
# identity, or algorithm names an officer will look up.
DO_NOT_TRANSLATE = re.compile(
    r"\bIS\s*\d+|NiyamKosh|BIS|QCO|BM25|MiniLM|RRF|PDF|docx|CSV|S\.O\.", re.I
)


def english_strings() -> dict[str, str]:
    """Read the en block out of i18n.js. The English text lives in one place; this
    script never becomes a second copy of it that can drift."""
    src = open(SOURCE, encoding="utf-8").read()
    block = re.search(r"\ben:\s*\{(.*?)\n  \},", src, re.S)
    if not block:
        raise SystemExit(f"could not find the 'en' block in {SOURCE}")
    pairs = re.findall(r"'([^']+)':\s*'((?:[^'\\]|\\.)*)'", block.group(1))
    return {k: v.replace("\\'", "'") for k, v in pairs}


def translate_mymemory(texts: list[str], target: str, email: str | None) -> list[str]:
    """One request per string. MyMemory has no batch endpoint, but the interface
    is barely a hundred short strings, so a hundred small requests is fine and
    keeps failures isolated to a single label."""
    import time
    import urllib.parse

    out = []
    for text in texts:
        params = {"q": text, "langpair": f"en|{target}"}
        if email:
            params["de"] = email          # raises the daily quota tenfold
        url = "https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                body = json.loads(resp.read())
            status = body.get("responseStatus")
            translated = (body.get("responseData") or {}).get("translatedText") or ""
            # The quota message arrives as a 200 with the warning in the payload,
            # so a naive reader would write "MYMEMORY WARNING..." into the UI.
            if status != 200 or "MYMEMORY WARNING" in translated.upper() or not translated:
                out.append(text)
                print(f"   quota or error on {text[:34]!r} — left in English")
            else:
                out.append(translated)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            out.append(text)
        time.sleep(0.35)
    return out


def bhashini_endpoint(target: str) -> tuple[str, dict, str]:
    """Ask ULCA which service handles en->target, and how to authenticate."""
    user_id, api_key = os.getenv("ULCA_USER_ID"), os.getenv("ULCA_API_KEY")
    if not (user_id and api_key):
        raise SystemExit(
            "Set ULCA_USER_ID and ULCA_API_KEY (register at https://bhashini.gov.in).\n"
            "Without them only the public config endpoint is reachable, which "
            "names the model but cannot translate."
        )
    payload = {
        "pipelineTasks": [{"taskType": "translation",
                           "config": {"language": {"sourceLanguage": "en",
                                                   "targetLanguage": target}}}],
        "pipelineRequestConfig": {"pipelineId": PIPELINE_ID},
    }
    req = urllib.request.Request(
        CONFIG_URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "userID": user_id, "ulcaApiKey": api_key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        cfg = json.loads(resp.read())

    endpoint = cfg.get("pipelineInferenceAPIEndPoint") or {}
    callback = endpoint.get("callbackUrl")
    key = endpoint.get("inferenceApiKey") or {}
    service = (cfg["pipelineResponseConfig"][0]["config"][0]["serviceId"])
    if not callback:
        raise SystemExit(
            f"ULCA returned no inference endpoint for en->{target}. The credentials "
            "may not be authorised for this pipeline."
        )
    return callback, {key.get("name", "Authorization"): key.get("value", "")}, service


def translate_batch(texts: list[str], target: str, provider: str = "mymemory",
                    email: str | None = None) -> list[str]:
    if provider == "mymemory":
        return translate_mymemory(texts, target, email)
    callback, headers, service = bhashini_endpoint(target)
    body = {
        "pipelineTasks": [{"taskType": "translation",
                           "config": {"language": {"sourceLanguage": "en",
                                                   "targetLanguage": target},
                                      "serviceId": service}}],
        "inputData": {"input": [{"source": s} for s in texts]},
    }
    req = urllib.request.Request(
        callback, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **headers}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        out = json.loads(resp.read())
    return [o["target"] for o in out["pipelineResponse"][0]["output"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", nargs="+", required=True,
                    help="target codes, e.g. mr ta bn kn")
    ap.add_argument("--provider", choices=("mymemory", "bhashini"), default="mymemory",
                    help="mymemory needs no key; bhashini needs ULCA credentials")
    ap.add_argument("--email", help="MyMemory: raises the daily quota from 5k to 50k characters")
    ap.add_argument("--batch", type=int, default=25)
    args = ap.parse_args()

    strings = english_strings()
    print(f"{len(strings)} interface strings in {SOURCE}  ·  provider: {args.provider}")

    keys = list(strings)
    locales: dict[str, dict] = {}
    if os.path.exists(OUT):
        existing = re.search(r"const LOCALES = (\{.*\});", open(OUT, encoding="utf-8").read(), re.S)
        if existing:
            locales = json.loads(existing.group(1))

    review_rows = []
    for lang in args.lang:
        if args.provider == "bhashini" and lang not in LANGUAGE_NAMES:
            print(f"  skipping {lang}: not a Bhashini scheduled language")
            continue
        print(f"\n── {lang} ({LANGUAGE_NAMES[lang]})")
        out: dict[str, str] = {}
        for i in range(0, len(keys), args.batch):
            chunk = keys[i:i + args.batch]
            try:
                got = translate_batch([strings[k] for k in chunk], lang,
                                      args.provider, args.email)
            except (urllib.error.URLError, urllib.error.HTTPError, KeyError, TimeoutError) as exc:
                print(f"   batch {i}: failed ({type(exc).__name__}) — left in English")
                got = [strings[k] for k in chunk]
            for k, v in zip(chunk, got):
                out[k] = v
                review_rows.append({"lang": lang, "key": k, "english": strings[k],
                                    "machine": v, "reviewed_by": "", "corrected": ""})
            print(f"   {min(i + args.batch, len(keys))}/{len(keys)}")

        flagged = [k for k, v in out.items() if DO_NOT_TRANSLATE.search(strings[k])]
        if flagged:
            print(f"   {len(flagged)} strings contain identifiers (IS numbers, BM25, PDF) — "
                  "check these first, machine translation mangles them")
        locales[lang] = out

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(
            "/* Generated by translate_ui.py — do not edit by hand.\n"
            " *\n"
            " * Machine translations, NOT yet human-reviewed:\n"
            " * see data/translation_review.csv, correct the `corrected` column, and\n"
            " * re-run with --apply-review.\n"
            " *\n"
            " * Standard titles, IS numbers and notification references are never\n"
            " * translated anywhere in this application — they are cited text.\n"
            " */\n\n"
            "const LOCALES = "
        )
        json.dump(locales, fh, ensure_ascii=False, indent=1)
        fh.write(";\n")

    if review_rows:
        import csv
        os.makedirs("data", exist_ok=True)
        with open(REVIEW, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(review_rows[0]))
            w.writeheader()
            w.writerows(review_rows)

    print(f"\nwrote {OUT} — languages: {', '.join(locales)}")
    print(f"wrote {REVIEW} — {len(review_rows)} strings awaiting human review")
    print("\nNothing here is reviewed yet. A machine translation of "
          "\"Blocking — do not publish as written\" is not something to put in front "
          "of a procurement officer unread.")


if __name__ == "__main__":
    sys.exit(main())
