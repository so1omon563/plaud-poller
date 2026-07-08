from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any
from urllib.parse import unquote

from .api import PlaudApiError, PlaudAuthError, PlaudClient, maybe_gunzip
from .config import load_settings
from .render import (
    extract_summary_markdown,
    flatten_outline,
    flatten_transcript,
    obsidian_tag,
    render_obsidian_note,
    slug_filename,
    summary_from_transsumm,
)
from .state import State


@dataclass
class SyncResult:
    status: str
    message: str | None = None


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


MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
OBSIDIAN_WIKI_IMAGE_RE = re.compile(r"!\[\[([^\]|]+)(?:\|([^\]]*))?\]\]")
TASK_LINE_RE = re.compile(r"^(?P<prefix>\s*[-*+]\s+\[)(?P<status>[^\]])(?P<suffix>\]\s+)(?P<body>.*)$")
TASK_METADATA_RE = re.compile(r"\s+(?:✅|📅|➕|⏳|🛫|🔁|⏫|🔼|🔽|⏬|🔺).*?$")


def localize_markdown_images(
    markdown: str | None,
    *,
    client: PlaudClient,
    download_path_mapping: Any,
    obsidian_dir: Path,
    recording_id: str,
) -> tuple[str | None, list[Path]]:
    """Download PLAUD markdown images and rewrite them to vault-local paths.

    PLAUD summary markdown can reference internal storage paths such as
    `permanent/.../mark/example.png`. Obsidian treats those as vault-relative
    paths and cannot render them unless the poller downloads the mapped asset.
    """
    if not markdown or not isinstance(download_path_mapping, dict):
        return markdown, []

    downloaded: list[Path] = []

    def replacement(match: re.Match[str]) -> str:
        alt = match.group(1)
        raw_path = match.group(2).strip("<>")
        if raw_path.startswith(("http://", "https://", "data:")):
            return match.group(0)
        url = download_path_mapping.get(raw_path) or download_path_mapping.get(unquote(raw_path))
        if not isinstance(url, str) or not url:
            return match.group(0)
        filename = Path(unquote(raw_path)).name or "plaud-image"
        rel_path = Path("_attachments") / "plaud" / recording_id / filename
        dest = obsidian_dir / rel_path
        try:
            payload = client.fetch_presigned_bytes(url)
            write_bytes_if_missing(dest, payload)
            downloaded.append(dest)
        except Exception as exc:
            print(f"WARN {recording_id}: image fetch failed for {raw_path}: {exc}", file=sys.stderr)
            return match.group(0)
        return f"![{alt}]({rel_path.as_posix()})"

    return MARKDOWN_IMAGE_RE.sub(replacement, markdown), downloaded


def preserve_existing_image_link_styles(generated_markdown: str, existing_markdown: str | None) -> str:
    """Preserve Obsidian's rewritten wiki-image syntax for equivalent local images.

    Obsidian may rewrite `![alt](_attachments/.../image.png)` to
    `![[image.png|alt]]`. Both render the same file, so the poller should not
    flip the syntax back on every run and create noisy visible updates.
    """
    if not generated_markdown or not existing_markdown:
        return generated_markdown

    existing_by_key: dict[tuple[str, str], str] = {}
    existing_by_filename: dict[str, str] = {}
    for match in OBSIDIAN_WIKI_IMAGE_RE.finditer(existing_markdown):
        target = match.group(1).strip()
        alt = (match.group(2) or "").strip()
        filename = Path(unquote(target)).name
        if not filename:
            continue
        existing_by_key.setdefault((filename, alt), match.group(0))
        existing_by_filename.setdefault(filename, match.group(0))

    def replacement(match: re.Match[str]) -> str:
        alt = match.group(1).strip()
        raw_path = match.group(2).strip("<>")
        filename = Path(unquote(raw_path)).name
        if not filename:
            return match.group(0)
        return existing_by_key.get((filename, alt)) or existing_by_filename.get(filename) or match.group(0)

    return MARKDOWN_IMAGE_RE.sub(replacement, generated_markdown)


def split_task_body_metadata(body: str) -> tuple[str, str]:
    match = TASK_METADATA_RE.search(body)
    if not match:
        return body.rstrip(), ""
    return body[: match.start()].rstrip(), body[match.start() :].rstrip()


def normalize_task_body(body: str) -> str:
    text, _metadata = split_task_body_metadata(body)
    text = re.sub(r"\s+", " ", text).strip()
    return text.casefold()


def preserve_existing_task_states(generated_markdown: str | None, existing_markdown: str | None) -> str | None:
    """Preserve local Obsidian task completion state across PLAUD summary rewrites.

    Matches conservatively by normalized task text and occurrence order. This
    keeps Tasks-plugin checkbox changes made in dashboards from being reset when
    the poller re-renders a note from PLAUD's canonical summary.
    """
    if not generated_markdown or not existing_markdown:
        return generated_markdown

    existing_by_text: dict[str, list[tuple[str, str]]] = {}
    for line in existing_markdown.splitlines():
        match = TASK_LINE_RE.match(line)
        if not match:
            continue
        body = match.group("body")
        key = normalize_task_body(body)
        if not key:
            continue
        _base, metadata = split_task_body_metadata(body)
        existing_by_text.setdefault(key, []).append((match.group("status"), metadata))

    out: list[str] = []
    for line in generated_markdown.splitlines(keepends=True):
        newline = "\n" if line.endswith("\n") else ""
        bare = line[:-1] if newline else line
        match = TASK_LINE_RE.match(bare)
        if not match:
            out.append(line)
            continue
        key = normalize_task_body(match.group("body"))
        preserved = existing_by_text.get(key, [])
        if not preserved:
            out.append(line)
            continue
        status, metadata = preserved.pop(0)
        generated_body, generated_metadata = split_task_body_metadata(match.group("body"))
        body = generated_body + (metadata or generated_metadata)
        out.append(f"{match.group('prefix')}{status}{match.group('suffix')}{body}{newline}")
    return "".join(out)


def backup_note_before_overwrite(path: Path, backup_dir: Path) -> Path | None:
    if not path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = unique_destination(backup_dir / f"{path.stem}__{stamp}{path.suffix}")
    shutil.copy2(path, dest)
    return dest


def note_belongs_to_plaud_id(path: Path, rid: str) -> bool:
    if not path.exists():
        return False
    try:
        head = path.read_text(encoding="utf-8")[:2000]
    except OSError:
        return False
    return f'plaud_id: "{rid}"' in head or f"plaud_id: {rid}" in head or f'"plaud_id": "{rid}"' in head


def find_note_by_plaud_id(obsidian_dir: Path, rid: str) -> Path | None:
    if not obsidian_dir.exists():
        return None
    for path in sorted(obsidian_dir.glob("*.md")):
        if note_belongs_to_plaud_id(path, rid):
            return path
    return None


def unique_destination(path: Path) -> Path:
    if not path.exists():
        return path
    for idx in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({idx}){path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem} (duplicate){path.suffix}")


def move_note(src: Path, dst: Path) -> Path:
    dst = unique_destination(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return dst


def delete_note(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def reconcile_removed_recordings(
    *,
    state: State,
    obsidian_dir: Path,
    archive_dir: Path,
    visible_ids: set[str],
    trashed_ids: set[str],
    policy: str,
    dry_run: bool,
) -> list[SyncResult]:
    results: list[SyncResult] = []
    for rec in state.all_recordings():
        rid = str(rec.get("plaud_id") or "")
        if not rid or rid in visible_ids:
            continue
        note_path = find_note_by_plaud_id(obsidian_dir, rid)
        if note_path is None:
            continue
        reason = "trashed" if rid in trashed_ids else "missing"
        if policy == "keep":
            results.append(SyncResult("kept", f"kept {reason} Plaud note: {rid[:8]}"))
            continue
        if dry_run:
            results.append(SyncResult(policy, f"would {policy} {reason} Plaud note: {rid[:8]}"))
            continue
        if policy == "archive":
            dest = move_note(note_path, archive_dir / note_path.name)
            state.touch_changed(rid)
            results.append(SyncResult("archived", f"archived {reason} Plaud note: {rid[:8]}"))
        elif policy == "delete":
            delete_note(note_path)
            state.remove(rid)
            results.append(SyncResult("deleted", f"deleted {reason} Plaud note: {rid[:8]}"))
    return results


def result_counts(results: list[SyncResult]) -> dict[str, int]:
    keys = ["new", "updated", "renamed", "archived", "deleted", "unchanged", "kept", "skipped", "warn"]
    counts = {key: 0 for key in keys}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts


def format_counts(counts: dict[str, int]) -> str:
    order = ["new", "updated", "renamed", "archived", "deleted", "unchanged", "kept", "skipped", "warn"]
    return " ".join(f"{key}={counts.get(key, 0)}" for key in order)


def should_print_result(result: SyncResult, report_mode: str) -> bool:
    if not result.message:
        return False
    if report_mode == "quiet":
        return False
    if report_mode == "verbose":
        return True
    return result.status != "unchanged"


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


def unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


def folder_names_from_filetags(
    row: dict[str, Any],
    detail: dict[str, Any],
    filetags: dict[str, dict[str, Any]],
) -> list[str]:
    ids: list[str] = []
    for source in (detail, row):
        raw_ids = source.get("filetag_id_list")
        if isinstance(raw_ids, list):
            ids.extend(str(tag_id) for tag_id in raw_ids if tag_id)
    names: list[str] = []
    for tag_id in unique_strings(ids):
        tag = filetags.get(tag_id) or {}
        name = tag.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return unique_strings(names)


def tags_from_folders(folder_names: list[str]) -> list[str]:
    return unique_strings([tag for folder in folder_names if (tag := obsidian_tag(folder))])


def speaker_names_from_segments(segments: list[dict[str, Any]] | None) -> list[str]:
    if not segments:
        return []
    names: list[str] = []
    for seg in segments:
        for key in ("speaker", "original_speaker"):
            value = seg.get(key)
            if isinstance(value, str) and value.strip():
                names.append(value.strip())
                break
    return unique_strings(names)


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
) -> tuple[list[dict[str, Any]] | None, str | None, list[dict[str, Any]] | None]:
    """Fetch transcript/summary blobs from PLAUD detail.content_list.

    PLAUD's `/ai/transsumm/{id}` response can lag behind speaker-name edits,
    while the downloadable `transaction` blob reflects the updated transcript.
    Prefer these blobs when present.
    """
    if not isinstance(content_list, list):
        return None, None, None
    transaction_url: str | None = None
    outline_url: str | None = None
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
        elif data_type == "outline" and not outline_url:
            outline_url = data_link
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
    outline_items: list[dict[str, Any]] | None = None
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
    if outline_url:
        parsed = _payload_from_data_link(client, outline_url)
        raw_outline = parsed if isinstance(parsed, list) else list(parsed.values()) if isinstance(parsed, dict) else []
        outline_items = [item for item in raw_outline if isinstance(item, dict)]
    return segments, summary_md, outline_items


def process_recording(
    *,
    client: PlaudClient,
    state: State,
    row: dict[str, Any],
    data_dir: Path,
    obsidian_dir: Path,
    download_audio: bool,
    include_transcript: bool,
    include_outline: bool,
    filetags: dict[str, dict[str, Any]],
    backup_on_change: bool,
    backup_dir: Path,
    preserve_task_state: bool,
    dry_run: bool,
) -> SyncResult:
    rid = recording_id(row)
    if not rid:
        return SyncResult("skipped", "skipped row without recording id")

    detail = client.file_detail(rid)
    title = recording_title(row, detail)
    transsumm: dict[str, Any] = {}
    transcript_segments: list[dict[str, Any]] | None = None
    outline_items: list[dict[str, Any]] | None = None
    transcript_md = ""
    outline_md = ""
    summary_md: str | None = None

    is_trans = bool(row.get("is_trans") or detail.get("is_trans"))
    is_summary = bool(row.get("is_summary") or detail.get("is_summary"))
    if is_trans or is_summary:
        try:
            content_segments, content_summary_md, content_outline_items = fetch_content_list_artifacts(client, detail.get("content_list"))
            if content_segments is not None:
                transcript_segments = content_segments
                transcript_md = flatten_transcript(transcript_segments)
            if content_outline_items is not None:
                outline_items = content_outline_items
                outline_md = flatten_outline(outline_items)
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

    if summary_md and not dry_run:
        summary_md, _image_paths = localize_markdown_images(
            summary_md,
            client=client,
            download_path_mapping=detail.get("download_path_mapping"),
            obsidian_dir=obsidian_dir,
            recording_id=rid,
        )

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
    folder_names = folder_names_from_filetags(row, detail, filetags)
    folder_tags = tags_from_folders(folder_names)
    speakers = speaker_names_from_segments(transcript_segments)

    note_path = resolve_note_path(obsidian_dir, title, rid)
    existing_note_path = find_note_by_plaud_id(obsidian_dir, rid)
    existing_note_text: str | None = None
    if existing_note_path and existing_note_path.exists():
        try:
            existing_note_text = existing_note_path.read_text(encoding="utf-8")
        except OSError:
            existing_note_text = None
    elif note_path.exists():
        try:
            existing_note_text = note_path.read_text(encoding="utf-8")
        except OSError:
            existing_note_text = None
    if summary_md and preserve_task_state:
        summary_md = preserve_existing_task_states(summary_md, existing_note_text)
        summary_hash = sha256_text(summary_md)

    rec_dir = data_dir / "recordings" / rid
    note = render_obsidian_note(
        plaud_id=rid,
        title=title,
        metadata=detail or row,
        transcript_md=transcript_md,
        summary_md=summary_md,
        outline_md=outline_md,
        folder_names=folder_names,
        speakers=speakers,
        tags=folder_tags,
        include_transcript=include_transcript,
        include_outline=include_outline,
    )
    note = preserve_existing_image_link_styles(note, existing_note_text)
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
        if old is None:
            return SyncResult("new", f"would create Plaud note: {rid[:8]}")
        if changed:
            return SyncResult("updated", f"would update Plaud note: {rid[:8]}")
        return SyncResult("unchanged", f"unchanged Plaud note: {rid[:8]}")

    # Full metadata includes volatile presigned PLAUD URLs, so do not treat that
    # artifact as user-visible work for notification purposes.
    write_text_if_changed(rec_dir / "metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    artifact_written = False
    if transcript_segments is not None:
        artifact_written = write_text_if_changed(rec_dir / "transcript.json", json.dumps(transcript_segments, ensure_ascii=False, indent=2) + "\n") or artifact_written
        artifact_written = write_text_if_changed(rec_dir / "transcript.md", transcript_md + ("\n" if transcript_md else "")) or artifact_written
    if summary_md:
        artifact_written = write_text_if_changed(rec_dir / "summary.md", summary_md.strip() + "\n") or artifact_written
    if outline_items is not None:
        artifact_written = write_text_if_changed(rec_dir / "outline.json", json.dumps(outline_items, ensure_ascii=False, indent=2) + "\n") or artifact_written
        artifact_written = write_text_if_changed(rec_dir / "outline.md", outline_md + ("\n" if outline_md else "")) or artifact_written

    if download_audio and not audio_downloaded:
        try:
            url = client.temp_audio_url(rid)
            payload = client.fetch_presigned_bytes(url)
            if write_bytes_if_missing(rec_dir / "audio", payload):
                audio_downloaded = True
                artifact_written = True
        except PlaudApiError as exc:
            print(f"WARN {rid}: audio fetch failed: {exc}", file=sys.stderr)

    renamed_note = False
    if not dry_run and existing_note_path and existing_note_path != note_path:
        if backup_on_change:
            backup_note_before_overwrite(existing_note_path, backup_dir)
        moved_to = move_note(existing_note_path, note_path)
        note_path = moved_to
        renamed_note = True
    elif backup_on_change and note_path.exists() and note_path.read_text(encoding="utf-8") != note:
        backup_note_before_overwrite(note_path, backup_dir)
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
        changed=changed or note_written or renamed_note or artifact_written,
    )
    if old is None:
        return SyncResult("new", f"new Plaud note: {rid[:8]}")
    if renamed_note:
        return SyncResult("renamed", f"renamed Plaud note: {rid[:8]}")
    if note_written:
        return SyncResult("updated", f"updated Plaud note: {rid[:8]}")
    return SyncResult("unchanged", f"unchanged Plaud note: {rid[:8]}")


def refresh_after_auth_error(repo_root: Path) -> tuple[bool, str]:
    """Force a browser-session refresh after PLAUD rejects a still-unexpired token."""
    from .auth import refresh_env_token
    from .config import load_dotenv, truthy

    if not truthy(os.environ.get("PLAUD_AUTO_REFRESH_TOKEN")):
        return False, "PLAUD_AUTO_REFRESH_TOKEN is disabled"
    env_path = repo_root / ".env"
    changed, message = refresh_env_token(env_path, force=True)
    if changed:
        load_dotenv(env_path, override=True)
    return changed, message


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Poll PLAUD and sync local artifacts/Obsidian notes")
    parser.add_argument("--dry-run", action="store_true", help="List recordings/status without writing artifacts")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N recordings")
    parser.add_argument("--report", choices=["quiet", "changes", "summary", "verbose"], default=None, help="Override PLAUD_REPORT_MODE for this run")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    settings = load_settings(repo_root)
    client = PlaudClient(settings)
    state = State(settings.state_db)
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.recordings_dir.mkdir(parents=True, exist_ok=True)
        settings.obsidian_dir.mkdir(parents=True, exist_ok=True)
        try:
            rows = client.list_all(page_size=settings.page_size, include_trash=settings.include_trash)
            filetags = client.list_filetags()
        except PlaudAuthError as exc:
            refreshed, refresh_message = refresh_after_auth_error(repo_root)
            if not refreshed:
                raise PlaudAuthError(f"{exc}; auto-refresh failed: {refresh_message}") from exc
            settings = load_settings(repo_root)
            client = PlaudClient(settings)
            rows = client.list_all(page_size=settings.page_size, include_trash=settings.include_trash)
            filetags = client.list_filetags()
        if args.limit:
            rows = rows[: args.limit]
        report_mode = args.report or settings.report_mode
        visible_ids = {rid for row in rows if (rid := recording_id(row))}
        results: list[SyncResult] = []
        for row in rows:
            result = process_recording(
                client=client,
                state=state,
                row=row,
                data_dir=settings.data_dir,
                obsidian_dir=settings.obsidian_dir,
                download_audio=settings.download_audio,
                include_transcript=settings.note_include_transcript,
                include_outline=settings.note_include_outline,
                filetags=filetags,
                backup_on_change=settings.note_backup_on_change,
                backup_dir=settings.note_backup_dir,
                preserve_task_state=settings.preserve_task_state,
                dry_run=args.dry_run,
            )
            results.append(result)
        if not args.limit and not settings.include_trash:
            if not visible_ids and state.all_recordings():
                results.append(
                    SyncResult(
                        "warn",
                        "WARN: active PLAUD list is empty; skipping removed-recording reconciliation to avoid archiving all notes",
                    )
                )
            else:
                trash_rows = client.list_all(page_size=settings.page_size, trash_mode=1)
                trashed_ids = {rid for row in trash_rows if (rid := recording_id(row))}
                results.extend(
                    reconcile_removed_recordings(
                        state=state,
                        obsidian_dir=settings.obsidian_dir,
                        archive_dir=settings.trash_archive_dir,
                        visible_ids=visible_ids,
                        trashed_ids=trashed_ids,
                        policy=settings.trash_policy,
                        dry_run=args.dry_run,
                    )
                )
        counts = result_counts(results)
        if args.dry_run and report_mode != "quiet":
            print(f"Plaud recordings visible: {len(rows)}")
        for result in results:
            if should_print_result(result, report_mode):
                print(result.message)
        if report_mode == "summary" or (report_mode == "changes" and any(counts.get(key, 0) for key in ("new", "updated", "renamed", "archived", "deleted", "kept", "skipped", "warn"))):
            print(format_counts(counts))
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
