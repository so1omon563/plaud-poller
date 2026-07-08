# plaud-poller

`plaud-poller` is a local-first synchronization tool that mirrors PLAUD recordings, summaries, transcripts, outlines, and metadata to ordinary files and Markdown notes.

PLAUD remains the place to capture and clean up recordings. Your filesystem becomes the long-term home for the data. The poller keeps those two views aligned by writing local artifacts and Markdown notes suitable for Obsidian or any folder-based Markdown workflow.

It is designed to be deterministic and idempotent: repeated runs should be quiet when nothing changed, and PLAUD-side edits should be reflected locally without creating duplicate notes. It is for users who want a simple, inspectable sync process instead of a cloud automation chain. It does not depend on Applaud, Zapier, Google Sheets, n8n, or any other hosted workflow system.

```text
PLAUD
  │
  ▼
plaud-poller
  ├── artifacts
  └── Markdown notes
```

## Why this exists

PLAUD is the capture tool. Your filesystem is the long-term home. `plaud-poller` keeps the two synchronized without putting a cloud automation service in the middle.

That matters when recordings are edited after capture. Titles, summaries, speaker labels, folders, and trash state can change inside PLAUD; the local Markdown view should follow those changes without losing inspectability or creating duplicate notes.

The project favors plain files, local state, and scheduled runs over hosted workflow glue. The result is easy to inspect, easy to back up, and under your control.

## Features

### Core features

- Poll PLAUD directly on demand or on a schedule.
- Store raw recording artifacts locally.
- Render and update Markdown notes for Obsidian or any Markdown directory.
- Treat PLAUD as the source of truth for titles, summaries, transcripts, speaker labels, folders, and trash state.
- Re-fetch and hash PLAUD content so title, summary, transcript, speaker, folder, and metadata changes propagate locally.
- Use PLAUD titles for note filenames while keeping the PLAUD ID in frontmatter for idempotency.
- Save transcript and outline artifacts even when generated notes are summary-only.
- Optionally include transcript and outline sections in generated notes.

### Additional capabilities

- Convert PLAUD folders to Obsidian tags.
- Convert PLAUD speaker labels to Obsidian wikilinks in frontmatter.
- Reconcile renamed notes by PLAUD ID instead of leaving duplicates.
- Archive, keep, or delete local notes whose PLAUD recordings are no longer active.
- Optionally back up existing Obsidian notes before overwriting or renaming them.
- Optionally preserve local Markdown task checkbox state for Obsidian Tasks dashboards.
- Provide quiet, change-only, summary, and verbose reporting modes.
- Provide diagnostics, canonical summary verification, and repository privacy checks.
- Keep credentials and synced PLAUD content out of git.
- Avoid hardcoded machine-specific paths.

## Design principles

- **Local-first**: credentials, state, artifacts, and notes live on your machine.
- **Deterministic**: the poller is a normal Python CLI, not a long-running service with hidden state.
- **Idempotent**: repeated runs should not rewrite notes or print output when nothing changed.
- **Simple to deploy**: run it manually or schedule the same command with cron, launchd, systemd, Docker, or Hermes cron.
- **Markdown-friendly**: generated notes work well in Obsidian but do not require Obsidian.
- **No cloud automation dependency**: no Zapier, Google Sheets, hosted workflow glue, or Applaud fork is required.

## How it works

On each run, `plaud-poller`:

1. Loads configuration from the environment and `.env`.
2. Authenticates to PLAUD using a bearer token.
3. Lists active PLAUD recordings by default.
4. Fetches recording detail, summary, transcript, outline, and folder metadata where available.
5. Writes raw artifacts under the configured recordings directory.
6. Renders or updates the Markdown note for each recording.
7. Uses the PLAUD ID stored in frontmatter to detect existing notes, including notes whose PLAUD title changed.
8. Reconciles notes for recordings that disappeared from active PLAUD results according to the configured trash policy.
9. Updates a local SQLite state database so future runs can detect real changes.

The generated note body treats PLAUD's displayed summary as canonical. Sync metadata stays in YAML frontmatter. Transcript and outline data are saved as artifacts and can optionally be included in the Markdown note.

## Installation

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

## Configuration

All paths are configurable with environment variables. Leave path variables blank to use portable defaults.

| Variable | Purpose | Default |
|---|---|---|
| `PLAUD_DATA_DIR` | Base directory for state and artifacts | macOS: `~/Library/Application Support/plaud-poller`; Linux: `${XDG_DATA_HOME:-~/.local/share}/plaud-poller`; Windows: `%LOCALAPPDATA%\\plaud-poller` or `%APPDATA%\\plaud-poller` |
| `PLAUD_RECORDINGS_DIR` | Raw recording artifact directory | `$PLAUD_DATA_DIR/recordings` |
| `PLAUD_STATE_DB` | SQLite state database path | `$PLAUD_DATA_DIR/state.sqlite` |
| `PLAUD_OBSIDIAN_DIR` | Markdown output directory | `$PLAUD_DATA_DIR/obsidian-notes` |
| `PLAUD_INCLUDE_TRASH` | Also sync recordings in PLAUD trash | `false` |
| `PLAUD_TRASH_POLICY` | What to do with notes whose PLAUD recording is no longer active: `keep`, `archive`, or `delete` | `archive` |
| `PLAUD_TRASH_ARCHIVE_DIR` | Destination for archived removed/trashed notes | `$PLAUD_OBSIDIAN_DIR/_Archive/plaud-trash` |
| `PLAUD_REPORT_MODE` | Poll output mode: `quiet`, `changes`, `summary`, or `verbose` | `changes` |
| `PLAUD_NOTE_BACKUP_ON_CHANGE` | Archive the previous Obsidian note before overwriting/renaming it | `false` |
| `PLAUD_NOTE_BACKUP_DIR` | Destination for note version backups | `$PLAUD_OBSIDIAN_DIR/_Archive/plaud-note-versions` |
| `PLAUD_NOTE_INCLUDE_TRANSCRIPT` | Include full transcript text in generated Markdown notes | `true` |
| `PLAUD_NOTE_INCLUDE_OUTLINE` | Include PLAUD outline as an extra Markdown section in generated notes | `false` |
| `PLAUD_PRESERVE_TASK_STATE` | Preserve local Markdown task checkbox state across regenerated PLAUD summaries | `true` |

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

### Trash handling

By default, only active PLAUD recordings are synced. If a previously synced recording later disappears from active PLAUD results, `PLAUD_TRASH_POLICY=archive` moves its Markdown note to:

```text
$PLAUD_OBSIDIAN_DIR/_Archive/plaud-trash
```

Other policies are available:

- `keep` — leave the note in place.
- `archive` — move the note to the trash archive directory.
- `delete` — remove the Markdown note and local state row.

Set `PLAUD_INCLUDE_TRASH=true` only if you intentionally want local copies of deleted or trashed PLAUD recordings.

As a safety guard, if PLAUD unexpectedly returns an empty active-recording list while the local state database already knows about recordings, the poller skips removed-recording reconciliation for that run. This avoids archiving or deleting every local note during a transient API/session issue.

### Note content

Transcript and outline artifacts are always saved under `PLAUD_RECORDINGS_DIR` when available. Generated notes can include or omit those sections:

```bash
PLAUD_NOTE_INCLUDE_TRANSCRIPT=true
PLAUD_NOTE_INCLUDE_OUTLINE=false
```

Set `PLAUD_NOTE_INCLUDE_TRANSCRIPT=false` if you want generated notes to focus on PLAUD summaries while keeping transcripts available as local artifacts. Set `PLAUD_NOTE_INCLUDE_OUTLINE=true` if you want the PLAUD outline appended as a separate note section.

PLAUD summaries may contain Markdown task checkboxes, which work well with Obsidian Tasks and Dataview dashboards. By default, the poller preserves local checkbox state for matching task text when a PLAUD summary is regenerated:

```bash
PLAUD_PRESERVE_TASK_STATE=true
```

This means a task completed in Obsidian stays completed even if PLAUD still returns it as unchecked. The matcher is conservative: it compares normalized task text, preserves duplicate tasks by occurrence order, and keeps Obsidian Tasks completion metadata such as `✅ 2026-07-08`.

Set this to `false` if you want PLAUD to remain fully canonical and overwrite local task completion state on every regenerated note:

```bash
PLAUD_PRESERVE_TASK_STATE=false
```

Generated Markdown filenames use the PLAUD title only, for example:

```text
2026-01-15 Product Review Search Improvements.md
```

The PLAUD ID is stored in frontmatter for idempotency, but is not included in the filename or visible title. The generated note avoids adding its own visible title, date, or summary wrapper; PLAUD's summary body is treated as canonical.

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

The poller does not keep a local speaker or person registry. Rename speakers in PLAUD; the next sync uses PLAUD's labels.

### Report modes

`PLAUD_REPORT_MODE` controls normal poll output:

- `quiet` — no normal output, even when changes happen.
- `changes` — quiet when unchanged; prints changed short PLAUD IDs plus a summary when work happened.
- `summary` — always prints counts such as `new=0 updated=1 renamed=0 archived=0 unchanged=2`.
- `verbose` — prints per-record status, including unchanged records.

For one-off manual runs, override the configured mode:

```bash
python3 -m plaud_poller.poll --report summary
```

### Note backups

If `PLAUD_NOTE_BACKUP_ON_CHANGE=true`, existing Obsidian notes are copied before overwrite or rename to:

```text
$PLAUD_NOTE_BACKUP_DIR
```

By default, that directory is:

```text
$PLAUD_OBSIDIAN_DIR/_Archive/plaud-note-versions
```

Backups use timestamped filenames such as:

```text
Title__YYYYMMDDTHHMMSSZ.md
```

## Authentication helpers

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

If the browser session has been server-invalidated and PLAUD requires a full login re-exchange, open the login page and poll browser storage for the fresh PLAUD token:

```bash
python3 -m plaud_poller.auth browser-login --env .env --method google
```

`--method` is a status/output hint and accepts `google`, `apple`, `email`, or `generic`; the actual login still happens in the browser UI. This makes the recovery path work for Google, Apple, and regular email accounts without scraping provider credentials. For Google and Apple, the helper relies on the browser's normal identity-provider session to complete the re-exchange. For email accounts, complete the PLAUD email/password or magic-link flow in the browser, then the helper captures only the resulting PLAUD token metadata.

The helper prints token metadata only, such as browser/profile, region, and expiry. It never prints the token value.

Optional auto-refresh before every poll:

```bash
PLAUD_AUTO_REFRESH_TOKEN=true
PLAUD_REFRESH_MIN_TTL_SECONDS=3600
```

When enabled, the poller scans local Chromium-family browser storage before polling. If the `.env` token is missing, expired, or near expiry, and a valid browser token is found, `.env` is updated automatically. If PLAUD rejects a still-unexpired token during polling, the poller also attempts one forced browser-session refresh and retries the API request before failing. This is intended for personal machines where you stay logged into `https://web.plaud.ai/`. For servers and containers, prefer explicit token management.

## Usage

List recordings without writing artifacts:

```bash
python3 -m plaud_poller.poll --dry-run
```

Poll and write changed artifacts and notes:

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

- `.env` exists,
- token is present and valid,
- token expiry and PLAUD region,
- data, artifact, and Markdown directories are writable,
- Markdown output is inside an Obsidian vault when applicable,
- best-effort check that the vault is known to the local Obsidian app,
- active and trashed PLAUD recording counts,
- whether trash sync is enabled and which trash policy is active,
- report mode and note backup settings,
- whether transcript and outline sections are included in notes,
- whether local Markdown task checkbox state is preserved across regenerated summaries.

Installed CLI entrypoint:

```bash
plaud-poller-doctor
```

### Verification and privacy checks

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

Install a local git pre-commit hook that blocks commits containing denylisted terms:

```bash
python3 -m plaud_poller.privacy --install-hook
```

Use `--force` only if you intentionally want to replace an existing pre-commit hook.

Installed CLI entrypoints:

```bash
plaud-poller-verify
plaud-poller-privacy-check
```

## Scheduling

The poller is a normal CLI. Schedule the same command or wrapper script with the system you already use.

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

## Output

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
$PLAUD_OBSIDIAN_DIR/_Archive/plaud-note-versions/Title__YYYYMMDDTHHMMSSZ.md
```

## Security

Do not commit `.env`, SQLite state, recordings, transcripts, summaries, audio, or privacy denylist files.

The public repository intentionally contains only code, examples, and documentation. Local credentials and synced PLAUD content belong in untracked paths.

The authentication helper does not print token values. Diagnostic and verification commands are designed to avoid printing secrets, and default verification output avoids note filenames unless `--verbose` is requested.
