# plaud-poller

A small, deterministic PLAUD poller for syncing recordings, transcripts, and summaries to local disk and Markdown without Applaud, Zapier, or Google Sheets.

The project was originally built to run from Hermes cron, but the poller itself is a normal Python CLI and can be run from cron, systemd, launchd, Docker, GitHub Actions/self-hosted runners, or manually.

## Goals

- Poll PLAUD directly on a schedule.
- Store raw artifacts locally.
- Render/update Markdown notes for Obsidian or any folder of Markdown files.
- Re-fetch and hash transcript, summary, title, and metadata so PLAUD-side edits propagate.
- Keep credentials local and out of git.
- Avoid hardcoded machine-specific paths.

## Current status

Initial scaffold with:

- PLAUD API client using browser-like User-Agent.
- Region auto-correction support for PLAUD regional API responses.
- SQLite state DB.
- Artifact writer.
- Markdown renderer.
- Dry-run/list-only mode.

## Setup

```bash
git clone https://github.com/so1omon563/plaud-poller.git
cd plaud-poller
cp .env.example .env
chmod 600 .env
$EDITOR .env
```

Set either:

```bash
PLAUD_AUTHORIZATION='Bearer ...'
```

or:

```bash
PLAUD_TOKEN='...'
```

`PLAUD_AUTHORIZATION` wins when both are set.

## Paths and portability

All paths are configurable by environment variables. Leave them blank to use portable defaults.

| Variable | Purpose | Default |
|---|---|---|
| `PLAUD_DATA_DIR` | Base directory for state and artifacts | macOS: `~/Library/Application Support/plaud-poller`; Linux: `${XDG_DATA_HOME:-~/.local/share}/plaud-poller`; Windows: `%LOCALAPPDATA%\\plaud-poller` or `%APPDATA%\\plaud-poller` |
| `PLAUD_RECORDINGS_DIR` | Raw recording artifact directory | `$PLAUD_DATA_DIR/recordings` |
| `PLAUD_STATE_DB` | SQLite state database path | `$PLAUD_DATA_DIR/state.sqlite` |
| `PLAUD_OBSIDIAN_DIR` | Markdown output directory | `$PLAUD_DATA_DIR/obsidian-notes` |
| `PLAUD_INCLUDE_TRASH` | Also sync recordings in PLAUD trash | `false` |

By default, only active PLAUD recordings are synced. Set `PLAUD_INCLUDE_TRASH=true` only if you intentionally want local copies of deleted/trashed PLAUD recordings.

Example for Obsidian:

```bash
PLAUD_DATA_DIR=~/Documents/Plaud
PLAUD_OBSIDIAN_DIR=~/Documents/Obsidian/Plaud
```

Example for a server:

```bash
PLAUD_DATA_DIR=/var/lib/plaud-poller
PLAUD_OBSIDIAN_DIR=/srv/notes/Plaud
```

## Auth helpers

The poller needs the same bearer token used by the PLAUD web app. You can paste it manually into `.env`, or use the helper to scan local Chromium-family browser profiles for a `web.plaud.ai` session token.

Detect valid browser tokens without printing secrets:

```bash
python3 -m plaud_poller.auth detect
```

Write a detected valid token to `.env`:

```bash
python3 -m plaud_poller.auth refresh --env .env
```

Force replacement even if the current `.env` token still has time left:

```bash
python3 -m plaud_poller.auth refresh --env .env --force
```

The helper prints token metadata only, such as browser/profile, region, and expiry. It never prints the token value.

Optional auto-refresh before every poll:

```bash
PLAUD_AUTO_REFRESH_TOKEN=true
PLAUD_REFRESH_MIN_TTL_SECONDS=3600
```

When enabled, the poller scans local Chromium-family browser storage before polling. If the `.env` token is missing, expired, or near expiry, and a valid browser token is found, `.env` is updated automatically. This is intended for personal machines where you stay logged into `https://web.plaud.ai/`. For servers and containers, prefer explicit token management.

## Manual run

List recordings without writing artifacts:

```bash
python3 -m plaud_poller.poll --dry-run
```

Poll and write changed artifacts/notes:

```bash
python3 -m plaud_poller.poll
```

Process only the first N visible recordings while testing:

```bash
python3 -m plaud_poller.poll --limit 5
```

## Scheduling examples

### Hermes cron

Use a script-only job so no LLM runs every poll:

```text
schedule: every 10m
script: /absolute/path/to/plaud-poller/scripts/run-poller.zsh
no_agent: true
```

The script is quiet when nothing changed. Non-zero exits should alert via Hermes cron.

### macOS launchd

Create a LaunchAgent that runs:

```bash
/path/to/plaud-poller/scripts/run-poller.zsh
```

Set the interval with `StartInterval` and make sure the launchd environment can find `python3`, or set `PYTHON_BIN` in the plist.

### system cron

```cron
*/10 * * * * cd /path/to/plaud-poller && /usr/bin/python3 -m plaud_poller.poll
```

### systemd timer

Use `WorkingDirectory=/path/to/plaud-poller` and `ExecStart=/usr/bin/python3 -m plaud_poller.poll`.

## Output files

With defaults:

```text
$PLAUD_DATA_DIR/state.sqlite
$PLAUD_RECORDINGS_DIR/<plaud_id>/metadata.json
$PLAUD_RECORDINGS_DIR/<plaud_id>/transcript.json
$PLAUD_RECORDINGS_DIR/<plaud_id>/transcript.md
$PLAUD_RECORDINGS_DIR/<plaud_id>/summary.md
$PLAUD_OBSIDIAN_DIR/YYYY-MM-DD - Title - <plaud_id>.md
```

## Security

Do not commit `.env`, SQLite state, recordings, transcripts, summaries, or audio.

The public repository intentionally contains only code, examples, and documentation. Local credentials and synced PLAUD content belong in untracked paths.
