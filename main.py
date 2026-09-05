#!/usr/bin/env python3
"""cl_auto — unattended transcript → Gemini → email pipeline.

On each run: latest council video → transcript → staged prompts → email.
Already-processed videos are skipped (state.json). Config is in config.env.

    python3 main.py            # same as the systemd timer
    python3 main.py --force    # reprocess even if already done
    python3 main.py --no-email # skip sending the email
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import traceback
from datetime import datetime

from app import ai, config, email_sender, pipeline
from app.youtube import (
    DEFAULT_TITLE_FILTER,
    TRANSCRIPTS_DIR,
    YtDlpError,
    fetch_transcript,
    get_latest_video,
    save_transcript,
    transcript_filename,
)

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(PROJECT_ROOT, "state.json")

DEFAULT_CHANNEL = "https://www.youtube.com/@slanicprahova"
DEFAULT_LANGUAGE = "ro"
DEFAULT_SOURCE = "auto"


def log(message: str) -> None:
    """Print a timestamped line to stdout (flushed, for live server logs)."""

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


# Desktop notifications (Linux Mint / any libnotify desktop). Disabled via
# --no-notify; silently skipped if notify-send isn't installed or there's no
# graphical session (e.g. a run that fires before login).
_NOTIFICATIONS = True


def notify(title: str, body: str = "", urgency: str = "normal") -> None:
    if not _NOTIFICATIONS:
        return
    exe = shutil.which("notify-send")
    if not exe:
        return
    try:
        subprocess.run(
            [exe, "--app-name=cl_auto", f"--urgency={urgency}", title, body],
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _load_state() -> dict:
    if os.path.isfile(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, ensure_ascii=False)
    except OSError as exc:
        log(f"WARNING: could not write state file: {exc}")


def run_once(args: argparse.Namespace) -> int:
    """Execute one full pipeline pass. Returns a process exit code."""

    notify("cl_auto a pornit", "Caut ultima ședință de consiliu…")

    title_filter = args.title_filter or None
    extra = f", title~/{title_filter}/" if title_filter else ""
    log(f"Looking up the latest video on {args.channel} (source={args.source}{extra}).")
    try:
        video = get_latest_video(
            args.channel, source=args.source, title_contains=title_filter
        )
    except (YtDlpError, ValueError) as exc:
        log(f"ERROR finding latest video: {exc}")
        _notify_error(args, "nu am putut găsi ultimul video", exc)
        return 1

    log(f"Latest video: {video.title!r} ({video.url})")

    state = _load_state()
    if not args.force and state.get("last_video_id") == video.video_id:
        log("This video was already processed in a previous run; nothing to do. "
            "Use --force to reprocess.")
        notify("cl_auto: nicio ședință nouă", "Nimic de trimis de data asta.")
        return 0

    # 1) Transcript
    try:
        transcript = fetch_transcript(video, language=args.language)
    except (YtDlpError, ValueError) as exc:
        log(f"ERROR fetching transcript: {exc}")
        _notify_error(args, "nu am putut descărca transcriptul", exc)
        return 1

    transcript_path = save_transcript(transcript, directory=TRANSCRIPTS_DIR)
    kind = "auto-generated" if transcript.auto_generated else "manual"
    log(f"Saved {kind} transcript -> {transcript_path}")

    # 2) Gemini processing
    chain = ai.default_models()
    log(f"Processing with Gemini (models tried in order: {', '.join(chain)}).")
    meeting_date = ai.date_from_transcript_name(transcript_path)
    try:
        result, model_used = pipeline.process_transcript(
            transcript.text, meeting_date, log=log
        )
    except ai.GeminiError as exc:
        log(f"ERROR processing with Gemini: {exc}")
        _notify_error(args, "procesarea cu Gemini a eșuat", exc)
        return 1

    final_path = ai.save_final(result, transcript_path)
    if model_used != chain[0]:
        log(f"NOTE: primary model unavailable; used fallback '{model_used}'.")
    log(f"Saved Gemini output ({model_used}) -> {final_path}")

    # 3) Email
    if args.no_email:
        log("Skipping email (--no-email).")
    else:
        _send_result_email(args, video, result, model_used, transcript_path, final_path)

    # 4) Remember what we processed so we don't repeat it next run.
    state["last_video_id"] = video.video_id
    state["last_video_title"] = video.title
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    state["last_final_file"] = os.path.basename(final_path)
    _save_state(state)

    log("Done.")
    sent = "fără email (--no-email)" if args.no_email else "email trimis"
    notify(
        "cl_auto a terminat",
        f"{video.title}\n{sent} · model: {model_used}",
    )
    return 0


def _send_result_email(
    args: argparse.Namespace,
    video,
    result: str,
    model_used: str,
    transcript_path: str,
    final_path: str,
) -> None:
    date_label = transcript_filename(video).removeprefix("trs_").removesuffix(".txt")
    subject = f"Rezumat ședință CL — {date_label} [{model_used}]"
    body = (
        f"{video.title}\n{video.url}\n"
        f"(Procesat cu Gemini: {model_used})\n"
        f"{'-' * 60}\n\n"
        f"{result}\n"
    )
    attachments = []
    if not args.no_attachments:
        attachments = [transcript_path, final_path]
    try:
        email_sender.send_email(subject, body, attachments=attachments)
    except email_sender.EmailError as exc:
        log(f"ERROR sending email: {exc}")
        return
    log(f"Email sent to: {config.get('EMAIL_TO')}")


def _notify_error(args: argparse.Namespace, what: str, exc: Exception) -> None:
    """Best-effort error desktop notification + email so failures get noticed."""

    notify("cl_auto — eroare", str(what), urgency="critical")

    if args.no_email or args.no_error_email:
        return
    if not config.get("SMTP_HOST"):
        return
    subject = "cl_auto — eroare la rularea automată"
    body = (
        f"Rularea automată cl_auto a eșuat: {what}.\n\n"
        f"Detalii:\n{exc}\n\n"
        f"{traceback.format_exc()}"
    )
    try:
        email_sender.send_email(subject, body)
        log("Error notification email sent.")
    except email_sender.EmailError as send_exc:
        log(f"Could not send error email: {send_exc}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Headless: latest YouTube transcript -> Gemini -> email."
    )
    parser.add_argument("--channel", default=DEFAULT_CHANNEL,
                        help=f"Channel handle or URL (default: {DEFAULT_CHANNEL}).")
    parser.add_argument("--language", default=DEFAULT_LANGUAGE,
                        help=f"Transcript language (default: {DEFAULT_LANGUAGE}).")
    parser.add_argument("--source", default=DEFAULT_SOURCE,
                        choices=("auto", "videos", "streams"),
                        help="Which tab(s) to pick the latest video from.")
    parser.add_argument("--title-filter", default=DEFAULT_TITLE_FILTER,
                        help="Only consider videos whose title matches this "
                             "(case-/accent-insensitive) regex. Default keeps "
                             "council sessions only. Pass '' to disable.")
    parser.add_argument("--primary-model", default=None,
                        help="Override the primary Gemini model "
                             "(default: GEMINI_MODEL from config.env).")
    parser.add_argument("--fallback-model", default=None,
                        help="Override the fallback model(s), comma-separated "
                             "(default: GEMINI_FALLBACK_MODEL from config.env).")
    parser.add_argument("--force", action="store_true",
                        help="Process even if the latest video was already done.")
    parser.add_argument("--no-email", action="store_true",
                        help="Do everything except sending the email.")
    parser.add_argument("--no-error-email", action="store_true",
                        help="Don't send a notification email when a run fails.")
    parser.add_argument("--no-attachments", action="store_true",
                        help="Don't attach the .txt files to the result email.")
    parser.add_argument("--no-notify", action="store_true",
                        help="Don't show desktop notifications (notify-send).")
    return parser


def main(argv: list[str] | None = None) -> int:
    config.load_config()
    args = build_parser().parse_args(argv)

    if args.primary_model:
        os.environ["GEMINI_MODEL"] = args.primary_model
    if args.fallback_model:
        os.environ["GEMINI_FALLBACK_MODEL"] = args.fallback_model

    global _NOTIFICATIONS
    _NOTIFICATIONS = not args.no_notify

    return run_once(args)


if __name__ == "__main__":
    sys.exit(main())
