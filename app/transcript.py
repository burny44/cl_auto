"""Convert WebVTT subtitle files into clean, readable plain text."""

from __future__ import annotations

import html
import re

_TAG_RE = re.compile(r"<[^>]+>")  # <c>, <00:00:01.000>, </c>, etc.
_TIMESTAMP_LINE_RE = re.compile(r"-->")
_CUE_SETTING_RE = re.compile(r"^\d+$")  # numeric cue identifiers


def vtt_to_text(vtt: str) -> str:
    """Strip timestamps/markup from a VTT string and de-duplicate lines.

    Auto-generated captions repeat lines as words roll in, so we drop
    consecutive duplicates to produce readable prose.
    """

    lines: list[str] = []
    for raw_line in vtt.splitlines():
        line = raw_line.strip()

        if not line:
            continue
        if line.upper().startswith("WEBVTT"):
            continue
        if line.startswith(("Kind:", "Language:", "NOTE", "STYLE", "::cue")):
            continue
        if _TIMESTAMP_LINE_RE.search(line):
            continue
        if _CUE_SETTING_RE.match(line):
            continue

        cleaned = _TAG_RE.sub("", line)
        cleaned = html.unescape(cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            continue

        # Skip if identical to the last kept line (common in auto captions).
        if lines and lines[-1] == cleaned:
            continue
        # Skip if this line is just the previous line plus more text rolling in.
        if lines and cleaned.startswith(lines[-1]):
            lines[-1] = cleaned
            continue

        lines.append(cleaned)

    return "\n".join(lines).strip()
