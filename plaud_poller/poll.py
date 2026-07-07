from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from .api import PlaudApiError, PlaudAuthError, PlaudClient
from .config import load_settings
from .render import (
    date_from_start_time,
    flatten_transcript,
    render_obsidian_note,
    slug_filename,
    summary_from_transsumm,
)
from .state import State


def sha256_text(value: str | bytes | None) -> str | None:
    if value is None:
        return None
    data = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_text_if_changed(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def write_bytes_if_missing(path: Path, content: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        return False
    path.write_bytes(content)
    return True


def recording_id(row: dict[str, Any]) -> str | None:
    for key in ("file_id", "id", "data_id"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def recording_title(row: dict[str, Any], detail: dict[str, Any] | None = None) -> str:
    for source in (detail or {}, row):
        for key in ("file_name", "filename", "title", "data_title"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return "Untitled PLAUD Recording"


def stable_metadata_for_hash(row: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    """Return only stable metadata fields for change detection.

    PLAUD detail responses can include volatile pre-signed URLs and request/task
    metadata. Hashing those makes every poll look like a change even when the
    recording title/transcript/summary did not change.
    """
    stable_keys = {
        "data_id",
        "duration",
        "edit_time",
        "file_id",
        "file_name",
        "file_version",
        "filetag_id_list",
        "filetype",
        "has_thought_partner",
        "is_summary",
        "is_trans",
        "is_trash",
        "scene",
        "serial_number",
        "session_id",
        "start_time",
    }
    stable: dict[str, Any] = {}
    for prefix, source in (("list", row), ("detail", detail)):
        stable[prefix] = {key: source.get(key) for key in sorted(stable_keys) if key in source}
    content_list = detail.get("content_list")
    if isinstance(content_list, list):
        stable["content_list"] = [
            {
                key: item.get(key)
                for key in ("data_id", "data_type", "task_status", "err_code", "err_msg", "data_title", "data_tab_name")
                if isinstance(item, dict) and key in item
            }
            for item in content_list
            if isinstance(item, dict)
        ]
    return stable


def process_recording(
    *,
    client: PlaudClient,
    state: State,
    row: dict[str, Any],
    data_dir: Path,
    obsidian_dir: Path,
    download_audio: bool,
    dry_run: bool,
) -> str | None:
    rid = recording_id(row)
    if not rid:
        return "skipped row without recording id"

    detail = client.file_detail(rid)
    title = recording_title(row, detail)
    transsumm: dict[str, Any] = {}
    transcript_segments: list[dict[str, Any]] | None = None
    transcript_md = ""
    summary_md: str | None = None

    is_trans = bool(row.get("is_trans") or detail.get("is_trans"))
    is_summary = bool(row.get("is_summary") or detail.get("is_summary"))
    if is_trans or is_summary:
        try:
            transsumm = client.transcript_and_summary(rid)
            raw_segments = transsumm.get("data_result")
            if isinstance(raw_segments, list):
                transcript_segments = [x for x in raw_segments if isinstance(x, dict)]
                transcript_md = flatten_transcript(transcript_segments)
            summary_md = summary_from_transsumm(transsumm)
        except PlaudApiError as exc:
            # Keep metadata sync alive even if transcript endpoint is temporarily unavailable.
            print(f"WARN {rid}: transcript/summary fetch failed: {exc}", file=sys.stderr)

    metadata = {
        "list_row": row,
        "detail": detail,
        "transsumm_meta": {
            k: v
            for k, v in transsumm.items()
            if k not in {"data_result", "data_result_summ", "data_result_summ_mul"}
        },
    }
    metadata_hash = sha256_text(stable_json(stable_metadata_for_hash(row, detail))) or ""
    transcript_hash = sha256_text(stable_json(transcript_segments)) if transcript_segments is not None else None
    summary_hash = sha256_text(summary_md) if summary_md else None

    rec_dir = data_dir / "recordings" / rid
    note = render_obsidian_note(
        plaud_id=rid,
        title=title,
        metadata=detail or row,
        transcript_md=transcript_md,
        summary_md=summary_md,
    )
    note_hash = sha256_text(note)
    old = state.get(rid)
    changed = old is None or any(
        [
            old.get("title") != title,
            old.get("metadata_hash") != metadata_hash,
            old.get("transcript_hash") != transcript_hash,
            old.get("summary_hash") != summary_hash,
            old.get("note_hash") != note_hash,
        ]
    )

    audio_downloaded = bool(old and old.get("audio_downloaded"))
    if dry_run:
        status = "NEW" if old is None else "CHANGED" if changed else "unchanged"
        return f"{status} {rid} {title}"

    write_text_if_changed(rec_dir / "metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    if transcript_segments is not None:
        write_text_if_changed(rec_dir / "transcript.json", json.dumps(transcript_segments, ensure_ascii=False, indent=2) + "\n")
        write_text_if_changed(rec_dir / "transcript.md", transcript_md + ("\n" if transcript_md else ""))
    if summary_md:
        write_text_if_changed(rec_dir / "summary.md", summary_md.strip() + "\n")

    if download_audio and not audio_downloaded:
        try:
            url = client.temp_audio_url(rid)
            payload = client.fetch_presigned_bytes(url)
            if write_bytes_if_missing(rec_dir / "audio", payload):
                audio_downloaded = True
        except PlaudApiError as exc:
            print(f"WARN {rid}: audio fetch failed: {exc}", file=sys.stderr)

    note_name = f"{date_from_start_time((detail or row).get('start_time'))} - {slug_filename(title)} - {rid}.md"
    note_path = obsidian_dir / note_name
    note_written = write_text_if_changed(note_path, note)

    state.upsert_seen(
        rid,
        title=title,
        start_time=(detail or row).get("start_time"),
        duration=(detail or row).get("duration"),
        metadata_hash=metadata_hash,
        transcript_hash=transcript_hash,
        summary_hash=summary_hash,
        note_hash=note_hash,
        audio_downloaded=audio_downloaded,
        changed=changed or note_written,
    )
    if old is None:
        return f"new Plaud note: {note_path}"
    if changed or note_written:
        return f"updated Plaud note: {note_path}"
    return None


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Poll PLAUD and sync local artifacts/Obsidian notes")
    parser.add_argument("--dry-run", action="store_true", help="List recordings/status without writing artifacts")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N recordings")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    settings = load_settings(repo_root)
    client = PlaudClient(settings)
    state = State(settings.state_db)
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.recordings_dir.mkdir(parents=True, exist_ok=True)
        settings.obsidian_dir.mkdir(parents=True, exist_ok=True)
        rows = client.list_all(page_size=settings.page_size)
        if args.limit:
            rows = rows[: args.limit]
        messages: list[str] = []
        for row in rows:
            msg = process_recording(
                client=client,
                state=state,
                row=row,
                data_dir=settings.data_dir,
                obsidian_dir=settings.obsidian_dir,
                download_audio=settings.download_audio,
                dry_run=args.dry_run,
            )
            if msg:
                messages.append(msg)
        if args.dry_run:
            print(f"Plaud recordings visible: {len(rows)}")
        for msg in messages:
            print(msg)
        return 0
    except PlaudAuthError as exc:
        print(f"AUTH ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        state.close()


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
