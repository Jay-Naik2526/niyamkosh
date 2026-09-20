"""The one place this project talks to a hosted model.

Used for exactly one job: rewriting the prose of a tender specification the
officer uploaded, so the corrected document reads like a document rather than
like a find-and-replace. It is never in the recommendation path. Which standard
applies, whether it is withdrawn, what replaces it and what certification it
carries are all decided by the register before this module is called, and
checked again after it returns.

The key is read from the environment or from a local .env, never from source.
`.env` is gitignored; the repository is public, and a key committed to a public
repository is a key that has been given away.

    GEMINI_API_KEY=...   in .env, or exported in the shell

If no key is present, `available()` is False and every caller falls back to the
deterministic path. The feature degrades to a worse document, never to no
document and never to a wrong one.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

MODEL = "gemini-flash-latest"
ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
TIMEOUT_SECONDS = 45


def _dotenv(path: str = ".env") -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def api_key() -> str:
    return (os.environ.get("GEMINI_API_KEY")
            or _dotenv().get("GEMINI_API_KEY")
            or "").strip()


def available() -> bool:
    return bool(api_key())


# The free tier returns 503 "high demand" often enough that a single attempt is
# a coin toss — three tries in a row during testing gave fail, fail, succeed.
# A demo cannot rest on that, so a retryable status is retried with a short
# backoff before the caller is told the model is unavailable.
RETRY_STATUS = {429, 500, 502, 503, 504}
ATTEMPTS = 3
BACKOFF_SECONDS = 1.5


def generate(prompt: str, temperature: float = 0.2, max_tokens: int = 4096) -> dict:
    """One completion, or a reason there is none. Never raises."""
    import time

    last = {"ok": False, "reason": "not_attempted"}
    for attempt in range(ATTEMPTS):
        last = _generate_once(prompt, temperature, max_tokens)
        if last.get("ok"):
            last["attempts"] = attempt + 1
            return last
        code = str(last.get("reason", ""))
        retryable = code.startswith("http_") and int(code[5:] or 0) in RETRY_STATUS
        if not retryable or attempt == ATTEMPTS - 1:
            break
        time.sleep(BACKOFF_SECONDS * (attempt + 1))
    last["attempts"] = ATTEMPTS
    return last


def _generate_once(prompt: str, temperature: float, max_tokens: int) -> dict:
    key = api_key()
    if not key:
        return {"ok": False, "reason": "no_key",
                "detail": "No GEMINI_API_KEY in the environment or .env."}
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
            # The reply is a document, not a conversation.
            "responseMimeType": "text/plain",
        },
    }).encode()
    req = urllib.request.Request(
        ENDPOINT, data=body, method="POST",
        headers={"Content-Type": "application/json", "X-goog-api-key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as r:
            payload = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:                      # noqa: PERF203
        detail = ""
        try:
            detail = json.loads(e.read().decode()).get("error", {}).get("message", "")
        except Exception:                                    # noqa: BLE001
            pass
        return {"ok": False, "reason": f"http_{e.code}", "detail": detail[:300]}
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "reason": "call_failed", "detail": str(e)[:300]}

    try:
        parts = payload["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError):
        blocked = (payload.get("promptFeedback") or {}).get("blockReason")
        return {"ok": False, "reason": "no_candidate", "detail": blocked or "empty response"}
    if not text.strip():
        return {"ok": False, "reason": "empty", "detail": "model returned no text"}
    return {"ok": True, "text": text, "model": MODEL}
