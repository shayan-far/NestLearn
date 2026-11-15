"""Thin LLM client. Gemini over plain REST (no SDK needed); swap providers by adding a function here.

Set GEMINI_API_KEY in .env (copy .env.example). With no key, `available()` is False and callers use
their fallback text, so the app works offline.
"""
import json
import urllib.error
import urllib.request

from backend import config

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class LLMError(RuntimeError):
    pass


def available() -> bool:
    return config.LLM_PROVIDER == "gemini" and bool(config.GEMINI_API_KEY)


def status() -> dict:
    return {"provider": config.LLM_PROVIDER, "model": config.GEMINI_MODEL if available() else None,
            "ready": available()}


def generate(prompt: str, system: str = "", max_tokens: int = 400, timeout: float = None) -> str:
    """Blocking call; run it with asyncio.to_thread from async code."""
    if not available():
        raise LLMError("no LLM configured (set GEMINI_API_KEY in .env)")
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if config.GEMINI_MODEL.startswith("gemini-3"):
        # Gemini 3+: keep thinking light, these are short latency-sensitive replies
        body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": "LOW"}
    req = urllib.request.Request(
        GEMINI_URL.format(model=config.GEMINI_MODEL),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": config.GEMINI_API_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or config.LLM_TIMEOUT_S) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise LLMError(f"HTTP {e.code}: {e.read()[:300].decode(errors='ignore')}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise LLMError(f"network: {e}") from e
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError) as e:
        raise LLMError(f"unexpected response: {str(data)[:300]}") from e
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not text:
        raise LLMError("empty response")
    return text
