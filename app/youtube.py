"""Thin wrapper around the `yt-dlp` command-line tool.

This module knows how to find the most recent upload on a channel (looking at
both the *Videos* and *Live* tabs) and how to fetch a video's transcript
(subtitles). It shells out to `yt-dlp` rather than importing it as a library so
that an up-to-date system binary is used independently of this project's Python
environment.

Notes on YouTube quirks handled here:

* Old yt-dlp versions silently return zero results, so we prefer a newer binary
  in ``~/.local/bin`` when present.
* YouTube frequently blocks the default ("web") player client from some IPs,
  returning "This video is not available". The ``android`` client usually still
  works, so we use it (with a fallback to the default).

(Copied, with minor tweaks, from the cl_script project so cl_auto fetches
transcripts in exactly the same way.)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass

from .transcript import vtt_to_text

# Where downloaded transcripts are stored (cl_auto/transcripts).
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRANSCRIPTS_DIR = os.path.join(PROJECT_ROOT, "transcripts")

# Matches a date like 25.05.2026, 25-5-26 or 25/05/2026 inside a title.
_DATE_RE = re.compile(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})")

# Tabs we look at when finding the "latest" upload, newest-first within each.
SOURCE_TABS = {
    "auto": ("videos", "streams"),
    "videos": ("videos",),
    "streams": ("streams",),
}

# Player clients to try, in order. The default web client is often blocked on
# datacenter IPs; android tends to keep working.
PLAYER_CLIENTS = ("android", "default")

# Default title filter (case- and accent-insensitive regex): only consider
# council-session uploads, skipping unrelated clips that have no transcript.
# Matches "Sedinta"/"Ședința" and "Consiliul Local".
DEFAULT_TITLE_FILTER = r"sedin|consiliul local"

# How many recent items per tab to scan when a title filter is in effect.
TITLE_SEARCH_LIMIT = 40


def _strip_accents(text: str) -> str:
    """Lowercase and remove diacritics so 'Ședința' matches 'sedinta'."""

    normalized = unicodedata.normalize("NFKD", text or "")
    without = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return without.lower()


class YtDlpError(RuntimeError):
    """Raised when yt-dlp is missing or fails."""


@dataclass
class Video:
    """A single video on a channel."""

    video_id: str
    title: str
    tab: str = ""
    upload_date: str = ""  # YYYYMMDD, when known

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


@dataclass
class Transcript:
    """A fetched transcript."""

    video: Video
    language: str
    text: str
    auto_generated: bool = False


def _ytdlp_path() -> str:
    """Locate yt-dlp, preferring a user-installed (newer) copy."""

    local = os.path.expanduser("~/.local/bin/yt-dlp")
    if os.path.isfile(local) and os.access(local, os.X_OK):
        return local
    path = shutil.which("yt-dlp")
    if path:
        return path
    raise YtDlpError(
        "yt-dlp was not found. Install/upgrade it, e.g.:\n"
        "    curl -fsSL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp "
        "-o ~/.local/bin/yt-dlp && chmod +x ~/.local/bin/yt-dlp"
    )


def _run(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [_ytdlp_path(), "--no-warnings", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - network dependent
        raise YtDlpError(f"yt-dlp timed out after {timeout}s.") from exc


def _client_args(client: str) -> list[str]:
    if client == "default":
        return []
    return ["--extractor-args", f"youtube:player_client={client}"]


def normalize_channel_base(channel: str) -> str:
    """Turn a handle or URL into a bare channel base URL (no tab)."""

    channel = channel.strip()
    if not channel:
        raise ValueError("Channel cannot be empty.")

    if channel.startswith("@"):
        channel = f"https://www.youtube.com/{channel}"
    elif not channel.startswith("http"):
        channel = f"https://www.youtube.com/@{channel}"

    channel = channel.rstrip("/")
    for tab in ("/videos", "/shorts", "/streams", "/featured", "/about", "/live"):
        if channel.endswith(tab):
            channel = channel[: -len(tab)]
            break
    return channel


def _list_tab(base: str, tab: str, limit: int = 1) -> list[Video]:
    """Return up to `limit` items from a channel tab (newest first).

    Uses the `youtubetab:approximate_date` extractor arg so the fast flat
    listing also includes an (approximate) upload date for each item.
    """

    proc = _run(
        [
            "--flat-playlist",
            "--playlist-items",
            f"1:{limit}",
            "--skip-download",
            "--extractor-args",
            "youtubetab:approximate_date",
            "--print",
            "%(upload_date)s\t%(id)s\t%(title)s",
            f"{base}/{tab}",
        ]
    )
    videos: list[Video] = []
    for line in proc.stdout.strip().splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        upload_date, video_id, title = parts
        if not video_id.strip():
            continue
        ud = upload_date.strip()
        videos.append(
            Video(
                video_id=video_id.strip(),
                title=title.strip() or "(untitled)",
                tab=tab,
                upload_date="" if ud.upper() == "NA" else ud,
            )
        )
    return videos


def _latest_in_tab(base: str, tab: str) -> Video | None:
    """Return the newest item in a channel tab, or None if it's empty/missing."""

    videos = _list_tab(base, tab, limit=1)
    return videos[0] if videos else None


def _resolve_upload_date(video: Video) -> str:
    """Fetch a video's upload date (YYYYMMDD); empty string if unavailable."""

    for client in PLAYER_CLIENTS:
        proc = _run(
            [
                "--skip-download",
                *_client_args(client),
                "--print",
                "%(upload_date)s",
                video.url,
            ]
        )
        out = proc.stdout.strip()
        if out and out.upper() != "NA" and out.isdigit():
            return out
    return ""


def get_latest_video(
    channel: str,
    source: str = "auto",
    title_contains: str | None = None,
) -> Video:
    """Return the newest video for a channel.

    `source` is one of "auto" (newest across Videos + Live), "videos", or
    "streams".

    If `title_contains` is given, it is treated as a case-/accent-insensitive
    regular expression and only videos whose title matches are considered. This
    is used to skip unrelated clips (e.g. promos) that have no transcript and
    pick the latest *council session* instead.
    """

    base = normalize_channel_base(channel)
    tabs = SOURCE_TABS.get(source, SOURCE_TABS["auto"])

    if not title_contains:
        # Fast path: just the newest item in each tab.
        candidates = [v for tab in tabs if (v := _latest_in_tab(base, tab))]
        if not candidates:
            raise YtDlpError(
                "No videos found for this channel. Check the URL/handle, and "
                "make sure yt-dlp is up to date."
            )
        if len(candidates) == 1:
            return candidates[0]
        for v in candidates:
            v.upload_date = _resolve_upload_date(v)
        candidates.sort(key=lambda v: v.upload_date or "00000000", reverse=True)
        return candidates[0]

    # Filtered path: scan recent items and keep only matching titles.
    pattern = re.compile(title_contains, re.IGNORECASE)
    seen: set[str] = set()
    matched: list[Video] = []
    for tab in tabs:
        for v in _list_tab(base, tab, limit=TITLE_SEARCH_LIMIT):
            if v.video_id in seen:
                continue
            seen.add(v.video_id)
            if pattern.search(_strip_accents(v.title)):
                matched.append(v)

    if not matched:
        raise YtDlpError(
            f"No recent videos matching /{title_contains}/ were found on this "
            f"channel. Adjust --title-filter (or set it empty to take the very "
            f"latest video regardless of title)."
        )

    # Pick the most recently uploaded match. Resolve dates only when missing.
    for v in matched:
        if not v.upload_date:
            v.upload_date = _resolve_upload_date(v)
    matched.sort(key=lambda v: v.upload_date or "00000000", reverse=True)
    return matched[0]


def fetch_transcript(video: Video, language: str = "ro") -> Transcript:
    """Download a transcript for `video` in `language`.

    Tries human-written subtitles first, then auto-generated captions, across
    the configured player clients. Raises YtDlpError if nothing is available.
    """

    last_error = ""
    for client in PLAYER_CLIENTS:
        with tempfile.TemporaryDirectory(prefix="cl_auto_subs_") as tmp:
            out_template = os.path.join(tmp, "%(id)s.%(ext)s")
            proc = _run(
                [
                    "--skip-download",
                    "--write-subs",
                    "--write-auto-subs",
                    "--sub-langs",
                    f"{language}.*",
                    "--sub-format",
                    "vtt/best",
                    *_client_args(client),
                    "-o",
                    out_template,
                    video.url,
                ]
            )

            vtt_files = [f for f in os.listdir(tmp) if f.endswith(".vtt")]
            if vtt_files:
                chosen = _pick_vtt(vtt_files, language)
                with open(os.path.join(tmp, chosen), "r", encoding="utf-8") as fh:
                    raw = fh.read()
                auto = "auto" in chosen.lower() or "orig" in chosen.lower()
                return Transcript(
                    video=video,
                    language=language,
                    text=vtt_to_text(raw),
                    auto_generated=auto,
                )
            last_error = proc.stderr.strip() or "yt-dlp produced no subtitle files."

    raise YtDlpError(
        f"No '{language}' transcript is available for this video.\n\n{last_error}"
    )


def _pick_vtt(files: list[str], language: str) -> str:
    """Pick the best subtitle file, preferring manual over auto/original."""

    manual = [
        f
        for f in files
        if f"{language}." in f and "auto" not in f.lower() and "orig" not in f.lower()
    ]
    if manual:
        return sorted(manual)[0]
    return sorted(files)[0]


def latest_transcript(channel: str, language: str = "ro", source: str = "auto") -> Transcript:
    """Convenience: latest video on a channel + its transcript."""

    return fetch_transcript(get_latest_video(channel, source=source), language=language)


def date_from_title(title: str) -> str | None:
    """Extract the date that appears in a video title, e.g. '25.05.2026'.

    Returns the date as written in the title (separators normalised to '.'),
    or None if no date-like token is found.
    """

    match = _DATE_RE.search(title or "")
    if not match:
        return None
    day, month, year = match.groups()
    return f"{day}.{month}.{year}"


def transcript_filename(video: Video) -> str:
    """Build the 'trs_<date>.txt' filename for a video."""

    date = date_from_title(video.title) or video.upload_date or "undated"
    return f"trs_{date}.txt"


def save_transcript(transcript: Transcript, directory: str = TRANSCRIPTS_DIR) -> str:
    """Save a transcript to <directory>/trs_<date>.txt and return the path."""

    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, transcript_filename(transcript.video))
    header = f"{transcript.video.title}\n{transcript.video.url}\n\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header + transcript.text)
    return path
