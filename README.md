# cl_auto

Unattended pipeline for local-council YouTube sessions: find the latest meeting, download the transcript, write a Romanian summary with Gemini, and email it.

Designed to run on a home machine (systemd timer or cron), not on cloud CI. YouTube blocks datacenter IPs; a residential connection does not need cookies or extra services. The machine does not need to stay on 24/7 — the timer catches up after boot.

## What it does

1. Finds the latest session on a YouTube channel (default `@slanicprahova`), filtering titles so clips without a real meeting are skipped.
2. Downloads the transcript (manual captions preferred) to `transcripts/trs_<date>.txt`.
3. Runs a two-step model pipeline:
   - **Extract** — structured facts (topics, amounts, votes, names)
   - **Compose** — final post (`🔑 Pe scurt` + `📋 Secțiunea detaliată`)
4. Emails the result, with the `.txt` files attached.

Already-processed videos are skipped (`state.json`). Very long transcripts are chunked automatically (`prompts/consolidate.txt`). Edit the files in `prompts/` to change how the model writes.

## Requirements

- Python 3.10+ (stdlib only — no `pip install`)
- A recent [yt-dlp](https://github.com/yt-dlp/yt-dlp) (old versions silently return nothing)
- A [Gemini API key](https://aistudio.google.com/apikey)
- SMTP credentials (Gmail: an [App Password](https://support.google.com/accounts/answer/185833), stored **without spaces**)

```bash
mkdir -p ~/.local/bin
curl -fsSL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
  -o ~/.local/bin/yt-dlp && chmod +x ~/.local/bin/yt-dlp
```

The app prefers `~/.local/bin/yt-dlp` when present.

## Setup

```bash
git clone <this-repo> && cd cl_auto
```

Create a gitignored `config.env` in the project root:

```env
ANTHROPIC_API_KEY=sk-ant-api03-...   # used by models named claude-*
GEMINI_API_KEY=...                   # used by the gemini-* fallbacks

# First model tried, then each fallback in order (names are historical:
# any claude-* model is routed to Anthropic automatically).
GEMINI_MODEL=claude-opus-5-5
GEMINI_FALLBACK_MODEL=gemini-3.1-pro-preview,gemini-3.5-flash,gemini-3-flash-preview

SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USER=you@gmail.com
SMTP_PASSWORD=your16charapppassword
EMAIL_FROM=you@gmail.com
EMAIL_TO=you@gmail.com
```

Environment variables override `config.env`. On the Gemini free tier each Flash model is capped (~20 requests/day); extra fallbacks keep a run going when one is exhausted.

## Usage

```bash
python3 main.py                 # one run (no-op if already processed)
python3 main.py --force         # reprocess the latest session
python3 main.py --no-email      # skip sending mail
python3 main.py --help
```

| Flag | Purpose |
|------|---------|
| `--channel` | Channel handle or URL (default `@slanicprahova`) |
| `--language` | Transcript language (default `ro`) |
| `--source` | `auto` / `videos` / `streams` |
| `--title-filter` | Title regex; pass `''` to take the latest video regardless |
| `--primary-model` / `--fallback-model` | Override the model chain |
| `--force` | Process even if already done |
| `--no-email` / `--no-error-email` / `--no-attachments` | Email controls |
| `--no-notify` | Skip desktop notifications |

Failed runs send a short error email unless `--no-error-email` is set.

## Schedule (systemd user timer)

Units are in `deploy/`. Install as **user** units (no root). Edit `WorkingDirectory` / `ExecStart` in the service if the project is not at `~/Projects/cl_auto`.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/cl_auto.service deploy/cl_auto.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cl_auto.timer

# Optional: run even when you are not logged in
sudo loginctl enable-linger "$USER"
```

The timer fires ~10 minutes after boot and every 3 days while the machine is on (`Persistent=true` catches missed runs).

```bash
systemctl --user list-timers cl_auto.timer
journalctl --user -u cl_auto.service -n 50 --no-pager
```

Day-to-day commands: [CHEATSHEET.md](CHEATSHEET.md).

## Layout

```
main.py            entry point
config.env         secrets (gitignored)
prompts/           extract / consolidate / compose / entities / example_post
reference/         people / locations / organizations .csv (edit in Excel; verificat = da/nu)
deploy/            systemd user units
app/               youtube, pipeline, Gemini/Anthropic, SMTP
transcripts/       trs_<date>.txt
final/             scl_<date>.txt
state.json         last processed video
```
