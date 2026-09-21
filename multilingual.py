"""Accept a specification written in an Indian language.

The problem statement asks for multilingual *input*, not merely a translated
interface: an officer should be able to type

    11 केवी एक्सएलपीई इंसुलेटेड आर्मर्ड पावर केबल

and get IS 7098 (Part 2). Translating the interface does nothing for that.

The register is entirely in English, so the query is translated into English and
then run through the ordinary pipeline. Three properties make that honest:

  * The original text is kept and returned. The officer sees what the system
    read, in both languages, and can tell whether the translation was fair.
  * Translation failure degrades to the original query rather than to an error.
    Devanagari that never reaches an English form still gets retrieval — some
    IS numbers and Latin technical terms survive inside Indic text.
  * IS numbers, voltages and units are protected from the translator. "IS 1554"
    must not come back as "है 1554", and "11 kV" must not lose its unit, because
    the voltage filter downstream reads it.

Only the query is sent, never a tender document: at most a couple of hundred
characters of product description. A full tender never leaves the machine.

Provider is MyMemory, which needs no registration. For a real deployment this
should be Bhashini (bhashini.gov.in) — the Government of India's own translation
service, so government text stays in government infrastructure. Set
MANAK_TRANSLATE_PROVIDER=bhashini with ULCA credentials to switch.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

MYMEMORY = "https://api.mymemory.translated.net/get"
TIMEOUT = 12
MAX_QUERY_CHARS = 600

# Unicode blocks for the scripts used by the scheduled languages of India.
SCRIPTS = {
    "hi": r"ऀ-ॿ",      # Devanagari — Hindi, Marathi, Konkani, Nepali…
    "bn": r"ঀ-৿",      # Bengali, Assamese
    "pa": r"਀-੿",      # Gurmukhi
    "gu": r"઀-૿",      # Gujarati
    "or": r"଀-୿",      # Odia
    "ta": r"஀-௿",      # Tamil
    "te": r"ఀ-౿",      # Telugu
    "kn": r"ಀ-೿",      # Kannada
    "ml": r"ഀ-ൿ",      # Malayalam
    "ur": r"؀-ۿ",      # Arabic script — Urdu, Kashmiri, Sindhi
}
SCRIPT_RE = {code: re.compile(f"[{rng}]") for code, rng in SCRIPTS.items()}

INDIC_ANY = re.compile("[" + "".join(SCRIPTS.values()) + "]")

# Fragments a translator must not touch. Replaced with placeholders before the
# call and restored after: "IS 1554" came back as "है 1554" without this, and a
# mangled IS number is exactly the failure this project exists to prevent.
PROTECT = re.compile(
    r"\bIS[:\s/]*\d{2,6}(?:\s*\([^)]{0,40}\))?"     # IS 1554 (Part 1)
    r"|\bIS/IEC\s*\d{2,6}"
    r"|\d+(?:\.\d+)?\s*(?:kV|V|kW|W|mm|mm2|sqmm|A|Hz|K)\b"   # 11 kV, 90W, 6000K
    r"|\bIP\s?\d{2}\b",                                       # IP66
    re.I,
)


def detect_script(text: str) -> str | None:
    """Which Indic script dominates, if any. Returns None for Latin text."""
    counts = {code: len(rx.findall(text or "")) for code, rx in SCRIPT_RE.items()}
    best = max(counts, key=counts.get) if counts else None
    return best if best and counts[best] >= 2 else None


def _protect(text: str) -> tuple[str, list[str]]:
    kept: list[str] = []

    def stash(m):
        kept.append(m.group(0))
        return f" Z{len(kept) - 1}Z "     # survives translation as an opaque token

    return PROTECT.sub(stash, text), kept


def _restore(text: str, kept: list[str]) -> str:
    for i, original in enumerate(kept):
        text = re.sub(rf"\s*Z\s*{i}\s*Z\s*", f" {original} ", text, flags=re.I)
    return re.sub(r"\s{2,}", " ", text).strip()


# MyMemory's anonymous quota is counted per calling IP. That is generous for one
# officer on an office connection and useless on a shared host: the deployed
# instance sits behind a datacentre address other tenants have already spent, so
# every translation came back refused and Hindi text was matched as written —
# BM25 scored the bare digits and the gate abstained on nonsense. Setting
# MYMEMORY_EMAIL moves the quota to that address; a second provider covers the
# case where it is spent anyway.
GOOGLE_GTX = "https://translate.googleapis.com/translate_a/single"

# Requests without a User-Agent are refused outright by both providers.
_UA = {"User-Agent": "Mozilla/5.0 (compatible; NiyamKosh/0.4; SIH PS 26108)"}


def _get_json(url: str, timeout: int = TIMEOUT):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _via_mymemory(text: str, source: str) -> str | None:
    params = {"q": text, "langpair": f"{source}|en"}
    email = os.getenv("MYMEMORY_EMAIL")
    if email:
        params["de"] = email
    try:
        body = _get_json(f"{MYMEMORY}?{urllib.parse.urlencode(params)}")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    out = (body.get("responseData") or {}).get("translatedText") or ""
    # The daily-quota notice arrives as a 200 with the warning in the payload.
    if body.get("responseStatus") != 200 or "MYMEMORY WARNING" in out.upper():
        return None
    return out or None


def _via_google(text: str, source: str) -> str | None:
    """Fallback only. Returns segments as [[["translated","original",...]],...]."""
    params = {"client": "gtx", "sl": source, "tl": "en", "dt": "t", "q": text}
    try:
        body = _get_json(f"{GOOGLE_GTX}?{urllib.parse.urlencode(params)}")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    try:
        out = "".join(seg[0] for seg in body[0] if seg and seg[0])
    except (IndexError, TypeError):
        return None
    return out.strip() or None


def _mymemory(text: str, source: str) -> str | None:
    """Whichever provider answers first. The name is kept so callers are unchanged."""
    for provider in (_via_mymemory, _via_google):
        out = provider(text, source)
        if out:
            return out
    return None


# ---------------------------------------------------------------- UI batch

CACHE_PATH = "data/translation_cache.json"
_cache: dict | None = None


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(CACHE_PATH, encoding="utf-8") as fh:
                _cache = json.load(fh)
        except (OSError, json.JSONDecodeError):
            _cache = {}
    return _cache


def _save_cache() -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(_cache, fh, ensure_ascii=False, indent=0)
    except OSError:
        pass


def _to_target(text: str, target: str) -> str | None:
    """English -> target. Identifiers are masked exactly as for queries, so a
    label containing "IS 1554" or "11 kV" comes back with those intact."""
    masked, kept = _protect(text)
    params = {"q": masked, "langpair": f"en|{target}"}
    email = os.getenv("MYMEMORY_EMAIL")
    if email:
        params["de"] = email
    try:
        with urllib.request.urlopen(
            f"{MYMEMORY}?{urllib.parse.urlencode(params)}", timeout=TIMEOUT
        ) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    out = (body.get("responseData") or {}).get("translatedText") or ""
    if body.get("responseStatus") != 200 or "MYMEMORY WARNING" in out.upper():
        return None
    return _restore(out, kept) if out else None


# One request per string, serially, made a first page load take half a minute.
# The requests are independent, so they run concurrently — modestly, because a
# free endpoint should not be hammered.
TRANSLATE_WORKERS = 8


def translate_ui_batch(texts: list[str], target: str) -> dict:
    """Translate a page's worth of labels, using the cache wherever possible."""
    from concurrent.futures import ThreadPoolExecutor

    cache = _load_cache()
    out: dict[str, str] = {}
    cached = fetched = failed = 0

    wanted, misses = [], []
    for text in texts:
        clean = (text or "").strip()
        if not clean or len(clean) > 400 or clean in out:
            continue
        key = f"{target}\u0000{clean}"
        if key in cache:
            out[clean] = cache[key]
            cached += 1
        else:
            misses.append(clean)
        wanted.append(clean)

    if misses:
        with ThreadPoolExecutor(max_workers=TRANSLATE_WORKERS) as pool:
            for clean, got in zip(misses, pool.map(lambda s: _to_target(s, target), misses)):
                if got:
                    cache[f"{target}\u0000{clean}"] = got
                    out[clean] = got
                    fetched += 1
                else:
                    failed += 1      # left in English rather than blanked
        _save_cache()
    return {
        "translations": out, "cached": cached, "fetched": fetched, "failed": failed,
        "note": (
            "Interface text only. Standard titles, IS numbers and notification "
            "references are excluded by the caller and never sent."
        ),
    }


def translate_query(text: str, ui_language: str | None = None) -> dict:
    """Normalise a spec query to English. Always returns something usable.

    `ui_language` is the language the officer selected in the interface. Script
    detection can only see that text is Devanagari, which Hindi, Marathi, Konkani
    and Nepali all share — so Marathi was being translated as Hindi. When the
    officer has told us their language and it uses the script we detected, their
    answer is better than our guess."""
    text = (text or "").strip()
    if not text:
        return {"applied": False, "text": text, "original": text, "source_language": None}

    script = detect_script(text)
    if not script:
        return {"applied": False, "text": text, "original": text, "source_language": None}

    # Languages sharing the detected script, so a UI choice can refine it.
    SHARED = {"hi": {"hi", "mr", "kok", "ne", "sa", "mai", "doi", "brx"},
              "bn": {"bn", "as"}, "ur": {"ur", "ks", "sd"}}
    # A script is not a language. Devanagari carries Hindi, Marathi, Konkani,
    # Nepali and more, and nothing in the characters distinguishes them — so
    # "Read as HI" was told to an officer writing Marathi, which is a claim we
    # cannot support. The translator still needs one code and "hi" is the
    # workable default for the script, but that is a routing decision, not a
    # finding about the officer's language. `certain` says which it was.
    certain = bool(ui_language and ui_language in SHARED.get(script, set()))
    script_family = sorted(SHARED.get(script, {script}))
    if certain:
        script = ui_language

    if len(text) > MAX_QUERY_CHARS:
        text_for_api = text[:MAX_QUERY_CHARS]
    else:
        text_for_api = text

    masked, kept = _protect(text_for_api)
    translated = _mymemory(masked, script)

    if not translated:
        return {
            "applied": False,
            "text": text,
            "original": text,
            "source_language": script,
            "language_certain": certain,
            "script_family": script_family,
            "note": (
                "Could not reach the translation service, so the text was matched as "
                "written. IS numbers and units inside it are still read correctly."
            ),
        }

    english = _restore(translated, kept)
    return {
        "applied": True,
        "text": english,
        "original": text,
        "source_language": script,
        "language_certain": certain,
        "script_family": script_family,
        "protected": kept,
        "provider": "mymemory",
        "note": (
            f"Query read as {script} and translated to English before retrieval. "
            "IS numbers, voltages and units were held back from the translator so "
            "they could not be altered. The register is published in English."
        ),
    }
