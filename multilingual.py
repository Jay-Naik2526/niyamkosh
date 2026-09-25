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
    r"|\b(?:\d{1,3}(?:[ ,]\d{3})+|\d+)(?:\.\d+)?\s*(?:kV|V|kW|W|mm|mm2|sqmm|A|Hz|K)\b"   # 11 kV, 6000K, 1 100 V
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


# ---------------------------------------------------------------- Bhashini

# Bhashini (MeitY's National Language Translation Mission) running AI4Bharat's
# IndicTrans2. It is the primary provider whenever a key is configured: it is the
# Government of India's own service, it covers every scheduled language the
# switcher offers, and unlike the free providers it does not run out of quota
# halfway through a page — which is what left screens half English.
#
# The key lives only in .env (BHASHINI_INFERENCE_KEY), never in source.
BHASHINI_URL = "https://dhruva-api.bhashini.gov.in/services/inference/pipeline"
BHASHINI_SERVICE = "ai4bharat/indictrans-v2-all-gpu--t4"
BHASHINI_BATCH = 50
BHASHINI_TIMEOUT = 60

# Interface text carries more fixed tokens than a query does: the product name,
# algorithm names and Gazette references must come back exactly as written.
PROTECT_UI = re.compile(
    PROTECT.pattern
    + r"|\bNiyamKosh\b|\bBM25\b|\bMiniLM\b|\bRRF\b|\bQCO\b|\bCRS\b|\bBIS\b"
    + r"|\bS\.O\.\s*\d+\s*\(E\)|https?://\S+"
    + r"|\b[\w.-]+\.(?:db|csv|jsonl?|pdf|docx|xlsx)\b",   # file names
    re.I,
)
_SLOT = re.compile(r"\{(\d+)\}")

LANGUAGE_EN = {
    "hi": "Hindi", "mr": "Marathi", "bn": "Bengali", "ta": "Tamil", "te": "Telugu",
    "gu": "Gujarati", "kn": "Kannada", "ml": "Malayalam", "pa": "Punjabi", "or": "Odia",
    "ur": "Urdu", "as": "Assamese", "ne": "Nepali", "kok": "Konkani", "sa": "Sanskrit",
}

# Post-edits for mistranslations a reviewer found on real screens. The model
# reads "insulated" in a cable title as "untouchable" (अछूता) — a word no
# officer should ever see on a standard. Technical Hindi keeps the loanword.
# Add to this as native speakers review more screens; it is data, not logic.
GLOSSARY = {
    "hi": [("अछूता", "इंसुलेटेड"), ("अछूती", "इंसुलेटेड"), ("अछूते", "इंसुलेटेड"),
           ("रन बनाए", "पुनः क्रमित")],
    "mr": [("अस्पृश्य", "इन्सुलेटेड")],
}


def _post_edit(text: str, target: str) -> str:
    for bad, good in GLOSSARY.get(target, []):
        text = text.replace(bad, good)
    return text


def _env(name: str) -> str:
    val = os.getenv(name)
    if val:
        return val.strip()
    try:
        from gemini import _dotenv
        return (_dotenv().get(name) or "").strip()
    except Exception:
        return ""


def bhashini_available() -> bool:
    return bool(_env("BHASHINI_INFERENCE_KEY"))


def _slot_mask(text: str, pattern=PROTECT_UI) -> tuple[str, list[str]]:
    """Replace protected spans with {0}, {1}… — the one placeholder form that
    IndicTrans2 was measured to return untouched in Hindi, Tamil, Bengali and
    Urdu. "Z0Z" came back as "जेड0जेड" and "#0#" lost its closing mark."""
    kept: list[str] = []

    def stash(m):
        kept.append(m.group(0))
        return "{%d}" % (len(kept) - 1)

    return pattern.sub(stash, text), kept


def _slot_restore(text: str, kept: list[str]) -> str | None:
    """Put the protected spans back. A translation that lost, duplicated or
    invented a slot is rejected rather than shown: an IS number that silently
    disappeared from a sentence is worse than an English sentence."""
    found = [int(i) for i in _SLOT.findall(text)]
    if sorted(found) != list(range(len(kept))):
        return None
    return _SLOT.sub(lambda m: kept[int(m.group(1))], text)


def _bhashini_call(texts: list[str], source: str, target: str) -> list[str] | None:
    key = _env("BHASHINI_INFERENCE_KEY")
    if not key or not texts:
        return None
    body = {
        "pipelineTasks": [{
            "taskType": "translation",
            "config": {"language": {"sourceLanguage": source, "targetLanguage": target},
                       "serviceId": BHASHINI_SERVICE},
        }],
        "inputData": {"input": [{"source": t} for t in texts]},
    }
    req = urllib.request.Request(
        BHASHINI_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": key, **_UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=BHASHINI_TIMEOUT) as resp:
            out = json.loads(resp.read())
        got = [o.get("target") for o in out["pipelineResponse"][0]["output"]]
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError,
            KeyError, IndexError, TypeError):
        return None
    return got if len(got) == len(texts) else None


_SENTENCE_END = re.compile(r"(?<=[.;?!])\s+(?=[A-Z{(\"'])")


def _sentences(masked: str, kept: list[str]) -> list[tuple[str, list[str]]]:
    """Split masked text into sentences, each renumbered from {0} with only its
    own protected spans. One slot per sentence is far easier for the model to
    carry than eight across a paragraph — a 645-character clause with eight IS
    numbers came back with one dropped every time it was sent whole."""
    out = []
    for sent in _SENTENCE_END.split(masked):
        local: list[str] = []

        def renum(m):
            local.append(kept[int(m.group(1))])
            return "{%d}" % (len(local) - 1)

        out.append((_SLOT.sub(renum, sent), local))
    return out


def _pieces(masked: str) -> list[str]:
    """The text between slots, for the last-resort path."""
    return [p for p in _SLOT.split(masked)]


def bhashini_translate(texts: list[str], source: str, target: str,
                       pattern=PROTECT_UI) -> list[str | None]:
    """Translate many strings. Each result is the translation with its protected
    spans restored, or None where that string failed — the caller decides what
    a failure falls back to.

    Three passes, each only for what the previous one could not do:
      1. every sentence of every string, batched, with {n} slots;
      2. a sentence whose slots came back wrong is translated in the pieces
         between its slots, and the protected spans are put back in order —
         word order may be English-shaped, but no IS number can be lost;
      3. anything still missing returns None."""
    from concurrent.futures import ThreadPoolExecutor

    def batched(strings: list[str]) -> list[str | None]:
        got: list[str | None] = [None] * len(strings)
        chunks = [list(range(i, min(i + BHASHINI_BATCH, len(strings))))
                  for i in range(0, len(strings), BHASHINI_BATCH)]

        def run(idx):
            return idx, _bhashini_call([strings[i] for i in idx], source, target)

        with ThreadPoolExecutor(max_workers=4) as pool:
            for idx, res in pool.map(run, chunks):
                for i, r in zip(idx, res or []):
                    got[i] = r
        return got

    plan = []                       # per text: list of (masked sentence, kept)
    flat: list[tuple[int, int]] = []
    for ti, t in enumerate(texts):
        masked, kept = _slot_mask(t, pattern)
        sents = _sentences(masked, kept)
        plan.append(sents)
        flat.extend((ti, si) for si in range(len(sents)))

    first = batched([plan[ti][si][0] for ti, si in flat])
    done: dict[tuple[int, int], str | None] = {}
    retry: list[tuple[int, int]] = []
    for (ti, si), out in zip(flat, first):
        sent, local = plan[ti][si]
        restored = _slot_restore(out, local) if out else None
        if restored is None and sent.strip():
            retry.append((ti, si))
        done[(ti, si)] = restored if sent.strip() else sent

    if retry:
        jobs, where = [], []
        for ti, si in retry:
            parts = _pieces(plan[ti][si][0])
            for pi, part in enumerate(parts):
                if re.search(r"[A-Za-z]{2}", part):
                    where.append((ti, si, pi))
                    jobs.append(part.strip())
            done[(ti, si)] = parts          # filled in below
        got = batched(jobs) if jobs else []
        for (ti, si, pi), g in zip(where, got):
            if g:
                lead = re.match(r"\s*", done[(ti, si)][pi]).group(0)
                tail = re.search(r"\s*$", done[(ti, si)][pi]).group(0)
                done[(ti, si)][pi] = f"{lead}{g}{tail}"
            else:
                done[(ti, si)] = None
                continue
        for ti, si in retry:
            parts = done[(ti, si)]
            if parts is None:
                continue
            local = plan[ti][si][1]
            rebuilt = parts[0]
            for k, kept_span in enumerate(local):
                rebuilt += kept_span + (parts[k + 1] if k + 1 < len(parts) else "")
            done[(ti, si)] = rebuilt

    results: list[str | None] = []
    for ti, sents in enumerate(plan):
        segs = [done.get((ti, si)) for si in range(len(sents))]
        results.append(None if any(x is None for x in segs)
                       else _post_edit(re.sub(r"\s{2,}", " ", " ".join(segs)).strip(), target))
    return results


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
MAX_UI_CHARS = 1500


def translate_ui_batch(texts: list[str], target: str) -> dict:
    """Translate a page's worth of text, using the cache wherever possible.

    Bhashini first when a key is configured; the free providers only for what
    Bhashini could not return. Cache keys carry the provider, so a MyMemory
    translation cached before the key existed is never served in place of a
    Bhashini one."""
    from concurrent.futures import ThreadPoolExecutor

    cache = _load_cache()
    out: dict[str, str] = {}
    cached = fetched = failed = 0
    use_bh = bhashini_available()
    prefix = "bh\u0000" if use_bh else ""

    misses: list[str] = []
    for text in texts:
        clean = (text or "").strip()
        if not clean or len(clean) > MAX_UI_CHARS or clean in out or clean in misses:
            continue
        key = f"{prefix}{target}\u0000{clean}"
        if key in cache:
            out[clean] = cache[key]
            cached += 1
        else:
            misses.append(clean)

    provider = "bhashini" if use_bh else "mymemory"
    if misses:
        got: list[str | None] = [None] * len(misses)
        if use_bh:
            got = bhashini_translate(misses, "en", target)
        rest = [i for i, g in enumerate(got) if not g]
        if rest:
            with ThreadPoolExecutor(max_workers=TRANSLATE_WORKERS) as pool:
                for i, g in zip(rest, pool.map(lambda i: _to_target(misses[i], target), rest)):
                    got[i] = g
        fallback = set(rest)
        for i, (clean, g) in enumerate(zip(misses, got)):
            if g:
                # A fallback translation is served but not cached under the
                # Bhashini key, so the next request retries Bhashini for it.
                if not (use_bh and i in fallback):
                    cache[f"{prefix}{target}\u0000{clean}"] = g
                out[clean] = g
                fetched += 1
            else:
                failed += 1      # left in English rather than blanked
        _save_cache()
    return {
        "translations": out, "cached": cached, "fetched": fetched, "failed": failed,
        "provider": provider,
        "note": "IS numbers, units, scheme codes and Gazette references are held back "
                "from the translator and restored exactly as written.",
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

    provider = "mymemory"
    translated = None
    if bhashini_available():
        # Queries keep the narrower query pattern: "BIS" or "QCO" typed inside a
        # Hindi description are words to translate around, not labels.
        translated = bhashini_translate([text_for_api], script, "en", pattern=PROTECT)[0]
        if translated:
            provider = "bhashini"
    if not translated:
        masked, kept = _protect(text_for_api)
        translated = _mymemory(masked, script)
        translated = _restore(translated, kept) if translated else None

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

    english = re.sub(r"\s{2,}", " ", translated).strip()
    return {
        "applied": True,
        "text": english,
        "original": text,
        "source_language": script,
        "language_certain": certain,
        "script_family": script_family,
        "protected": PROTECT.findall(text_for_api),
        "provider": provider,
        "note": (
            f"Query read as {LANGUAGE_EN.get(script, script)} and translated to English before retrieval. "
            "IS numbers, voltages and units were held back from the translator so "
            "they could not be altered. The register is published in English."
        ),
    }
