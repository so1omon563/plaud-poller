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
- Transcript/summary fetching from PLAUD downloadable content blobs when available, so speaker-name edits propagate.
- PLAUD folder names become Obsidian tags, and PLAUD speaker labels become Obsidian wikilinks in frontmatter.

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
| `PLAUD_TRASH_POLICY` | What to do with notes whose PLAUD recording is no longer active: `keep`, `archive`, or `delete` | `archive` |
| `PLAUD_TRASH_ARCHIVE_DIR` | Destination for archived removed/trashed notes | `$PLAUD_OBSIDIAN_DIR/_Archive/plaud-trash` |
| `PLAUD_NOTE_INCLUDE_TRANSCRIPT` | Include full transcript text in generated Markdown notes | `true` |
| `PLAUD_NOTE_INCLUDE_OUTLINE` | Include PLAUD outline as an extra Markdown section in generated notes | `false` |

By default, only active PLAUD recordings are synced. If a previously synced recording later disappears from active PLAUD results, `PLAUD_TRASH_POLICY=archive` moves its Markdown note to the archive folder. Set `keep` to leave it in place, or `delete` to remove the Markdown note and local state row. Set `PLAUD_INCLUDE_TRASH=true` only if you intentionally want local copies of deleted/trashed PLAUD recordings.

Transcript and outline artifacts are always saved under `PLAUD_RECORDINGS_DIR` when available. Set `PLAUD_NOTE_INCLUDE_TRANSCRIPT=false` if you want generated Markdown notes to focus on PLAUD summaries while keeping transcripts available as local artifacts. Set `PLAUD_NOTE_INCLUDE_OUTLINE=true` if you want the PLAUD outline appended as a separate note section.

Generated Markdown filenames use the PLAUD title only, for example:

```text
2026-01-15 Product Review Search Improvements.md
```

The PLAUD ID is stored in Obsidian/YAML frontmatter for idempotency, but is not included in the filename/title. The generated note avoids adding its own visible title/date/summary wrapper; PLAUD's summary body is treated as canonical.

Generated frontmatter includes stable indexing metadata. PLAUD folders become Obsidian tags, and speaker labels come only from PLAUD transcript data, rendered as Obsidian links:

```yaml
---
source: plaud
ingest: plaud-poller
plaud_id: "..."
title: "2026-01-15 Product Review Search Improvements"
duration_ms: 1234567
plaud_updated_at: "2026-01-15T17:30:00Z"
has_summary: true
has_transcript: true
has_outline: true
tags:
  - "work"
plaud_folders:
  - "Work"
speakers:
  - "[[Jane Example]]"
  - "[[Sam Example]]"
---
```

The poller does not keep a local speaker/person registry. Rename speakers in PLAUD; the next sync uses PLAUD's labels.

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

## Diagnostics

Run the doctor command to verify token, paths, vault placement, and PLAUD API visibility without printing secrets:

```bash
python3 -m plaud_poller.doctor
```

It checks:

- `.env` exists
- token is present and valid
- token expiry and PLAUD region
- data/artifact/Markdown directories are writable
- Markdown output is inside an Obsidian vault when applicable
- best-effort check that the vault is known to the local Obsidian app
- active and trashed PLAUD recording counts
- whether trash sync is enabled and which trash policy is active
- whether transcript/outline sections are included in notes

Installed CLI entrypoint:

```bash
plaud-poller-doctor
```

## Verification and privacy checks

Verify that the visible Obsidian body matches PLAUD's canonical `auto_sum_note` summary after removing frontmatter. Default output is summary-only to avoid printing private note titles; add `--verbose` when you want per-note filenames:

```bash
python3 -m plaud_poller.verify
python3 -m plaud_poller.verify --verbose
```

Scan tracked repository files for caller-provided private terms before committing:

```bash
# Optional local file, ignored by git:
$EDITOR .plaud-privacy-denylist
python3 -m plaud_poller.privacy
```

You can also pass terms directly for CI or one-off checks:

```bash
python3 -m plaud_poller.privacy --term "Private Customer Name" --term "Internal Meeting Title"
```

Installed CLI entrypoints:

```bash
plaud-poller-verify
plaud-poller-privacy-check
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
$PLAUD_RECORDINGS_DIR/<plaud_id>/outline.json
$PLAUD_RECORDINGS_DIR/<plaud_id>/outline.md
$PLAUD_OBSIDIAN_DIR/Title.md
$PLAUD_OBSIDIAN_DIR/_Archive/plaud-trash/Title.md
```

## Security

Do not commit `.env`, SQLite state, recordings, transcripts, summaries, or audio.

The public repository intentionally contains only code, examples, and documentation. Local credentials and synced PLAUD content belong in untracked paths.
