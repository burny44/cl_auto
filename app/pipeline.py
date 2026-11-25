"""Staged transcript processing: extract -> consolidate -> compose.

Instead of asking the model to do everything at once over a long, noisy
transcript, we break the work into focused steps, each grounded in the previous
step's output:

  1. EXTRACT    split the transcript into chunks; pull structured facts (agenda
                items, decisions, vote counts, names) out of each chunk as JSON.
  2. CONSOLIDATE merge the per-chunk facts into one clean, de-duplicated list and
                 correct names against the council reference list.
  3. COMPOSE     turn the verified facts into the final Romanian Facebook post,
                 applying the required formatting.

Each step is a short, single-purpose prompt, which the model follows far more
reliably than one giant instruction over the whole transcript.
"""

from __future__ import annotations

import json
import os
import re
import time

from . import ai, references

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(PROJECT_ROOT, "prompts")

# A whole transcript shorter than this is extracted in a single call (modern
# Gemini models have a huge context window, so chunking a normal council meeting
# only wastes quota). Above it, we fall back to chunked extract + consolidate.
SINGLE_PASS_LIMIT = 200_000
# When we do chunk, roughly how many characters per extract call.
CHUNK_CHARS = 50_000
# Retry a stage this many times if a model is rate-limited per-minute (RPM).
QUOTA_RETRIES = 3
QUOTA_BACKOFF_SECONDS = 20


def _read_prompt(name: str) -> str:
    path = os.path.join(PROMPTS_DIR, name)
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().strip()


def _models() -> list[str]:
    return ai.default_models()


def _call(instructions: str, user_text: str, models: list[str], log=print) -> tuple[str, str]:
    """One model call with the given instructions, retrying on rate limits."""

    last_exc: Exception | None = None
    for attempt in range(1, QUOTA_RETRIES + 1):
        try:
            return ai.process_with_fallback(
                user_text, instructions=instructions, models=models
            )
        except ai.GeminiError as exc:
            last_exc = exc
            message = str(exc).lower()
            # A per-day free-tier cap won't recover in seconds: fail fast.
            daily_cap = "perday" in message or "requestsperday" in message
            rate_limited = any(
                token in message
                for token in ("429", "exhausted", "unavailable", "overloaded", "rate")
            )
            if rate_limited and not daily_cap and attempt < QUOTA_RETRIES:
                log(f"  rate-limited, waiting {QUOTA_BACKOFF_SECONDS}s "
                    f"(attempt {attempt}/{QUOTA_RETRIES})…")
                time.sleep(QUOTA_BACKOFF_SECONDS)
                continue
            raise
    assert last_exc is not None
    raise last_exc


def _strip_code_fences(text: str) -> str:
    """Remove ```json ... ``` fences a model may wrap JSON in."""

    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def chunk_transcript(text: str, chunk_chars: int = CHUNK_CHARS) -> list[str]:
    """Split a transcript into line-aligned chunks of ~chunk_chars characters."""

    lines = text.splitlines()
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        current.append(line)
        size += len(line) + 1
        if size >= chunk_chars:
            chunks.append("\n".join(current))
            current = []
            size = 0
    if current:
        chunks.append("\n".join(current))
    return chunks or [text]


def process_transcript(
    transcript: str,
    meeting_date: str,
    log=print,
) -> tuple[str, str, dict[str, list[str]]]:
    """Run extract -> consolidate -> compose -> learn.

    Returns (post_text, compose_model, learned), where `learned` maps each
    reference table ("people", "locations", "organizations") to the names that
    were added to reference/*.csv for manual review.
    """

    council = references.reference_text()
    extract_instr = _read_prompt("extract.txt") + "\n\n" + council
    consolidate_instr = _read_prompt("consolidate.txt") + "\n\n" + council
    compose_instr = (
        _read_prompt("compose.txt")
        + "\n\n" + council
        + "\n\n=== STRUCTURE EXAMPLE (FORMAT ONLY — a different meeting; do NOT "
        "reuse any of its facts, numbers, names or sentences) ===\n"
        + _read_prompt("example_post.txt")
        + "\n=== END OF STRUCTURE EXAMPLE ==="
    )

    models = _models()

    if len(transcript) <= SINGLE_PASS_LIMIT:
        # Common case: one extract call over the whole transcript, no consolidate.
        log("Pipeline: extracting facts…")
        facts_json, ex_model = _call(extract_instr, transcript, models, log=log)
        consolidated = _strip_code_fences(facts_json)
        log(f"  extracted ({ex_model}).")
    else:
        # Very long transcript: chunk -> extract per chunk -> consolidate.
        chunks = chunk_transcript(transcript)
        log(f"Pipeline: extracting facts from {len(chunks)} chunk(s)…")
        extractions: list[str] = []
        for i, chunk in enumerate(chunks, 1):
            text, model_used = _call(extract_instr, chunk, models, log=log)
            extractions.append(_strip_code_fences(text))
            log(f"  chunk {i}/{len(chunks)} done ({model_used}).")

        log("Pipeline: consolidating facts…")
        combined = "\n".join(extractions)
        consolidated, cons_model = _call(consolidate_instr, combined, models, log=log)
        consolidated = _strip_code_fences(consolidated)
        log(f"  consolidated ({cons_model}).")

    # Final) COMPOSE
    log("Pipeline: composing the final post…")
    facts = _safe_json(consolidated)
    compose_input = json.dumps(
        {"meeting_date": meeting_date, "facts": facts if facts is not None else consolidated},
        ensure_ascii=False,
    )
    post, compose_model = _call(compose_instr, compose_input, models, log=log)
    log(f"  composed ({compose_model}).")

    learned = learn_references(compose_input, meeting_date, models, log=log)

    return post.strip(), compose_model, learned


def learn_references(
    facts_json: str, meeting_date: str, models: list[str], log=print
) -> dict[str, list[str]]:
    """Ask the model for people/locations/organizations not yet in
    reference/*.csv and append them with verificat = "nu". Never fails the run:
    errors are only logged."""

    log("Pipeline: looking for new people / locations / organizations…")
    try:
        user_text = json.dumps(
            {"known": json.loads(references.known_names_json()),
             "facts": json.loads(facts_json)},
            ensure_ascii=False,
        )
        raw, model_used = _call(_read_prompt("entities.txt"), user_text, models, log=log)
        candidates = _safe_json(_strip_code_fences(raw))
        if not isinstance(candidates, dict):
            log("  could not parse the model's answer; skipped.")
            return {}
        added = references.add_candidates(candidates, meeting_date)
    except Exception as exc:  # learning is a bonus; never break the post
        log(f"  skipped ({exc}).")
        return {}
    summary = ", ".join(f"{len(v)} {k}" for k, v in added.items())
    log(f"  new entries added for review: {summary} ({model_used}).")
    return added
