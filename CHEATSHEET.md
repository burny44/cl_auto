# cl_auto — cheat sheet

```bash
cd ~/Projects/cl_auto
```

## Run

```bash
python3 main.py                 # skip if already processed
python3 main.py --force         # reprocess latest session
python3 main.py --no-email      # no mail
python3 main.py --no-notify     # no desktop notifications
python3 main.py --help
```

## Timer

```bash
systemctl --user list-timers --all cl_auto.timer
systemctl --user status cl_auto.service --no-pager
systemctl --user start cl_auto.service          # one run now
systemctl --user enable --now cl_auto.timer     # auto on
systemctl --user disable --now cl_auto.timer    # auto off
```

After editing `deploy/`:

```bash
cp deploy/cl_auto.service deploy/cl_auto.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user restart cl_auto.timer
```

Run when not logged in (once): `sudo loginctl enable-linger "$USER"`

## Logs

```bash
journalctl --user -u cl_auto.service -n 50 --no-pager
journalctl --user -u cl_auto.service --since today --no-pager
grep last_run state.json
```

## Config

Secrets live in `config.env` (gitignored). Env vars win over the file.

Gmail: `SMTP_PASSWORD` is a 16-character [App Password](https://myaccount.google.com/apppasswords), **no spaces**.

## Output

```
transcripts/trs_<date>.txt   downloaded transcript
final/scl_<date>.txt         model write-up
state.json                   last processed video
```

## Fix

```bash
# stale yt-dlp → empty results
curl -fsSL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
  -o ~/.local/bin/yt-dlp && chmod +x ~/.local/bin/yt-dlp

python3 main.py --force --no-email    # test download + model, no mail
```

Failures also show a desktop notification and send an error email (`--no-error-email` to skip).
