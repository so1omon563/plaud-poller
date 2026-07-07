# plaud-poller

A small, deterministic PLAUD poller for syncing recordings/transcripts/summaries to local disk and Obsidian without Applaud, Zapier, or Google Sheets.

## Goals

- Poll PLAUD directly on a schedule.
- Keep raw artifacts under `~/Documents/Plaud`.
- Render/update Markdown notes under `~/Documents/Obsidian Vault/Plaud`.
- Re-fetch and hash transcript/summary/title metadata so PLAUD-side edits propagate.
- Keep credentials local and out of git.

## Current status

Initial scaffold with:

- PLAUD API client using browser-like User-Agent.
- Region auto-correction support for PLAUD regional API responses.
- SQLite state DB.
- Artifact writer.
- Obsidian Markdown renderer.
- Dry-run/list-only mode.

## Setup

```bash
cd /Users/so1omon/homelab/plaud-poller
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

## Manual run

List recordings without writing artifacts:

```bash
python3 -m plaud_poller.poll --dry-run
```

Poll and write changed artifacts/notes:

```bash
python3 -m plaud_poller.poll
```

## Scheduling with Hermes cron

Use a script-only job so no LLM runs every poll:

```text
schedule: every 10m
script: /Users/so1omon/homelab/plaud-poller/scripts/run-poller.zsh
no_agent: true
```

The script is quiet when nothing changed. Non-zero exits should alert via Hermes cron.

## Files

Default local data paths:

```text
~/Documents/Plaud/state.sqlite
~/Documents/Plaud/recordings/<plaud_id>/metadata.json
~/Documents/Plaud/recordings/<plaud_id>/transcript.json
~/Documents/Plaud/recordings/<plaud_id>/transcript.md
~/Documents/Plaud/recordings/<plaud_id>/summary.md
~/Documents/Obsidian Vault/Plaud/YYYY-MM-DD - Title - <plaud_id>.md
```

## Security

Do not commit `.env`, SQLite state, recordings, transcripts, summaries, or audio.
