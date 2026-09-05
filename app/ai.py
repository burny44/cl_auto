"""Gemini / Anthropic REST client with a multi-model fallback chain.

Instructions come from the caller (the staged pipeline in pipeline.py).
Processed output is saved to final/scl_<date>.txt.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FINAL_DIR = os.path.join(PROJECT_ROOT, "final")

# Fallback if GEMINI_API_KEY is not in the environment / config.env.
API_KEY_FILES = ("gemini_api_key.txt", ".gemini_api_key")

# Default model names, used when the environment doesn't override them.
DEFAULT_PRIMARY_MODEL = "gemini-3.1-pro-preview"
DEFAULT_FALLBACK_MODELS = "gemini-3.5-flash"


def primary_model() -> str:
    """The first model to try (GEMINI_MODEL, read fresh from the environment)."""

    return os.environ.get("GEMINI_MODEL", DEFAULT_PRIMARY_MODEL).strip()


def fallback_models() -> list[str]:
    """Fallback models, in order (GEMINI_FALLBACK_MODEL, comma-separated)."""

    raw = os.environ.get("GEMINI_FALLBACK_MODEL", DEFAULT_FALLBACK_MODELS)
    return [m.strip() for m in raw.split(",") if m.strip()]


def default_models() -> list[str]:
    """The model chain to try, in order: primary first, then each fallback.

    Read dynamically from the environment so that config loaded *after* this
    module is imported (the normal case) is still honored.
    """

    models = [primary_model()]
    for model in fallback_models():
        if model and model not in models:
            models.append(model)
    return models

# HTTP status codes that mean "this model is unavailable right now, try another":
# 429 = RESOURCE_EXHAUSTED (quota/rate limit), 503 = model overloaded.
_FALLBACK_STATUS = {429, 503}

_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Anthropic (Claude) is used as an additional fallback provider. Any model name
# starting with "claude" is routed here instead of to Gemini.
_ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_MAX_TOKENS = 16384


def is_anthropic_model(model: str) -> bool:
    return model.strip().lower().startswith("claude")


class GeminiError(RuntimeError):
    """Raised when the Gemini request can't be made or fails."""


class GeminiQuotaError(GeminiError):
    """Raised when a model is rate-limited/overloaded (so a fallback is worth trying)."""


def read_api_key() -> str:
    for env in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        if os.environ.get(env):
            return os.environ[env].strip()
    for name in API_KEY_FILES:
        path = os.path.join(PROJECT_ROOT, name)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                key = fh.read().strip()
            if key:
                return key
    raise GeminiError(
        "No Gemini API key found.\n\n"
        "Get one at https://aistudio.google.com/apikey then either:\n"
        "  • set the GEMINI_API_KEY environment variable, or\n"
        "  • save it in a file named 'gemini_api_key.txt' in the project folder."
    )


def read_anthropic_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    raise GeminiError(
        "No Anthropic API key found. Set ANTHROPIC_API_KEY in config.env to use "
        "a Claude model."
    )


def process_text(
    transcript: str,
    instructions: str,
    model: str | None = None,
    api_key: str | None = None,
    timeout: int = 300,
) -> str:
    """Send `transcript` to a single model and return the result.

    Raises GeminiQuotaError if the model is rate-limited/overloaded, or a plain
    GeminiError for any other failure.
    """

    model = model or primary_model()

    if is_anthropic_model(model):
        return _anthropic_generate(transcript, instructions, model, api_key, timeout)
    return _gemini_generate(transcript, instructions, model, api_key, timeout)


def _gemini_generate(
    transcript: str, instructions: str, model: str, api_key: str | None, timeout: int
) -> str:
    api_key = api_key or read_api_key()

    body = {
        "system_instruction": {"parts": [{"text": instructions}]},
        "contents": [{"role": "user", "parts": [{"text": transcript}]}],
    }
    request = urllib.request.Request(
        _API_URL.format(model=model),
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        if exc.code in _FALLBACK_STATUS:
            raise GeminiQuotaError(
                f"Model '{model}' is unavailable (HTTP {exc.code}): {detail}"
            ) from exc
        raise GeminiError(f"Gemini API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise GeminiError(f"Could not reach Gemini: {exc.reason}") from exc

    return _extract_text(payload)


def _anthropic_generate(
    transcript: str, instructions: str, model: str, api_key: str | None, timeout: int
) -> str:
    api_key = api_key or read_anthropic_key()

    body = {
        "model": model,
        "max_tokens": _ANTHROPIC_MAX_TOKENS,
        "system": instructions,
        "messages": [{"role": "user", "content": transcript}],
    }
    request = urllib.request.Request(
        _ANTHROPIC_API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        if exc.code in _FALLBACK_STATUS:
            raise GeminiQuotaError(
                f"Model '{model}' is unavailable (HTTP {exc.code}): {detail}"
            ) from exc
        raise GeminiError(f"Anthropic API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise GeminiError(f"Could not reach Anthropic: {exc.reason}") from exc

    return _extract_anthropic_text(payload)


def _extract_anthropic_text(payload: dict) -> str:
    blocks = payload.get("content") or []
    text = "".join(
        block.get("text", "") for block in blocks if block.get("type") == "text"
    ).strip()
    if not text:
        stop = payload.get("stop_reason")
        raise GeminiError(
            "Anthropic returned an empty response."
            + (f" stop_reason: {stop}" if stop else "")
        )
    return text


def process_with_fallback(
    transcript: str,
    instructions: str,
    models: list[str] | None = None,
    api_key: str | None = None,
    timeout: int = 300,
) -> tuple[str, str]:
    """Try each model in order, falling back on rate-limit/overload errors.

    Returns a tuple of (processed_text, model_actually_used). Raises GeminiError
    if every model failed.
    """

    if models is None:
        models = default_models()

    # Don't pre-read a key here: each model may use a different provider
    # (Gemini vs Anthropic). process_text resolves the right key per model.

    last_error: GeminiError | None = None
    for index, model in enumerate(models):
        is_last = index == len(models) - 1
        try:
            text = process_text(
                transcript,
                instructions=instructions,
                model=model,
                api_key=api_key,
                timeout=timeout,
            )
            return text, model
        except GeminiQuotaError as exc:
            # Rate-limited / overloaded: always worth trying the next model.
            last_error = exc
            continue
        except GeminiError as exc:
            # Other failures (e.g. a flaky preview-model alias returning 404).
            # Fall back to the next model too, but surface the error if this was
            # already the last model to try.
            last_error = exc
            if is_last:
                raise
            continue

    raise GeminiError(
        "All Gemini models failed.\n\n" + (str(last_error) if last_error else "")
    )


def _extract_text(payload: dict) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        feedback = payload.get("promptFeedback") or {}
        raise GeminiError(
            "Gemini returned no content."
            + (f" Reason: {feedback.get('blockReason')}" if feedback else "")
        )
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        raise GeminiError("Gemini returned an empty response.")
    return text


def date_from_transcript_name(path: str) -> str:
    """Pull the date portion out of a 'trs_<date>.txt' filename."""

    stem = os.path.splitext(os.path.basename(path))[0]
    if stem.startswith("trs_"):
        return stem[len("trs_"):]
    return stem


def final_filename(transcript_path: str) -> str:
    """Build the 'scl_<date>.txt' output name for a transcript file."""

    return f"scl_{date_from_transcript_name(transcript_path)}.txt"


def save_final(text: str, transcript_path: str, directory: str = FINAL_DIR) -> str:
    """Save processed text to <final>/scl_<date>.txt and return the path."""

    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, final_filename(transcript_path))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path
