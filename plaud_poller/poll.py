from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from .api import PlaudApiError, PlaudAuthError, PlaudClient, maybe_gunzip
from .config import load_settings
from .render import (
    extract_summary_markdown,
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


def note_belongs_to_plaud_id(path: Path, rid: str) -> bool:
    if not path.exists():
        return False
    try:
        head = path.read_text(encoding="utf-8")[:2000]
    except OSError:
        return False
    return (
        f'plaud_id: "{rid}"' in head
        or f"plaud_id: {rid}" in head
        or f'"plaud_id": "{rid}"' in head
    )


def resolve_note_path(obsidian_dir: Path, title: str, rid: str) -> Path:
    """Return a human-readable note path without exposing PLAUD IDs in the filename.

    If two recordings have the same title, append a small numeric suffix rather
    than the PLAUD ID. The ID stays in frontmatter for idempotency.
    """
    base = slug_filename(title)
    candidate = obsidian_dir / f"{base}.md"
    if not candidate.exists() or note_belongs_to_plaud_id(candidate, rid):
        return candidate
    for idx in range(2, 100):
        candidate = obsidian_dir / f"{base} ({idx}).md"
        if not candidate.exists() or note_belongs_to_plaud_id(candidate, rid):
            return candidate
    return obsidian_dir / f"{base} (duplicate).md"


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


def _payload_from_data_link(client: PlaudClient, url: str) -> Any:
    raw = maybe_gunzip(client.fetch_presigned_bytes(url)).decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def fetch_content_list_artifacts(
    client: PlaudClient,
    content_list: Any,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Fetch transcript/summary blobs from PLAUD detail.content_list.

    PLAUD's `/ai/transsumm/{id}` response can lag behind speaker-name edits,
    while the downloadable `transaction` blob reflects the updated transcript.
    Prefer these blobs when present.
    """
    if not isinstance(content_list, list):
        return None, None
    transaction_url: str | None = None
    auto_summary_url: str | None = None
    fallback_summary_url: str | None = None
    for item in content_list:
        if not isinstance(item, dict):
            continue
        data_type = item.get("data_type")
        data_link = item.get("data_link")
        if not isinstance(data_link, str) or not data_link:
            continue
        if data_type == "transaction" and not transaction_url:
            transaction_url = data_link
        # PLAUD's auto_sum_note is the displayed summary. transaction_polish is
        # usually a polished transcript blob, not a summary, so don't prefer it.
        if data_type == "auto_sum_note" and not auto_summary_url:
            auto_summary_url = data_link
        elif isinstance(data_type, str) and "sum" in data_type and not fallback_summary_url:
            fallback_summary_url = data_link
        elif data_type == "transaction_polish" and not fallback_summary_url:
            fallback_summary_url = data_link

    segments: list[dict[str, Any]] | None = None
    summary_md: str | None = None
    if transaction_url:
        parsed = _payload_from_data_link(client, transaction_url)
        raw_segments = parsed if isinstance(parsed, list) else list(parsed.values()) if isinstance(parsed, dict) else []
        segments = [seg for seg in raw_segments if isinstance(seg, dict)]
    summary_url = auto_summary_url or fallback_summary_url
    if summary_url:
        parsed = _payload_from_data_link(client, summary_url)
        extracted = extract_summary_markdown(parsed)
        # Avoid treating transcript arrays as summaries.
        if extracted and not isinstance(parsed, list):
            summary_md = extracted
    return segments, summary_md


def process_recording(
    *,
    client: PlaudClient,
    state: State,
    row: dict[str, Any],
    data_dir: Path,
    obsidian_dir: Path,
    download_audio: bool,
    include_transcript: bool,
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
            content_segments, content_summary_md = fetch_content_list_artifacts(client, detail.get("content_list"))
            if content_segments is not None:
                transcript_segments = content_segments
                transcript_md = flatten_transcript(transcript_segments)
            if content_summary_md:
                summary_md = content_summary_md
        except Exception as exc:
            print(f"WARN {rid}: content-list artifact fetch failed: {exc}", file=sys.stderr)

        if transcript_segments is None or summary_md is None:
            try:
                transsumm = client.transcript_and_summary(rid)
                if transcript_segments is None:
                    raw_segments = transsumm.get("data_result")
                    if isinstance(raw_segments, list):
                        transcript_segments = [x for x in raw_segments if isinstance(x, dict)]
                        transcript_md = flatten_transcript(transcript_segments)
                if summary_md is None:
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
        include_transcript=include_transcript,
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

    note_path = resolve_note_path(obsidian_dir, title, rid)
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
        rows = client.list_all(page_size=settings.page_size, include_trash=settings.include_trash)
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
                include_transcript=settings.note_include_transcript,
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
