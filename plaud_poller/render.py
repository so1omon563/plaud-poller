from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from typing import Any


def format_timestamp(ms: int | float | None) -> str:
    if not ms:
        return "00:00"
    total = int(ms // 1000)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def flatten_transcript(segments: list[dict[str, Any]] | None) -> str:
    if not segments:
        return ""
    lines: list[str] = []
    for seg in segments:
        speaker = seg.get("speaker") or seg.get("original_speaker") or "Speaker"
        content = str(seg.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"[{format_timestamp(seg.get('start_time'))}] {speaker}: {content}")
    return "\n\n".join(lines)


def flatten_outline(items: list[dict[str, Any]] | None) -> str:
    if not items:
        return ""
    lines: list[str] = []
    for item in items:
        topic = item.get("topic") or item.get("title") or item.get("content") or item.get("text")
        if not isinstance(topic, str) or not topic.strip():
            continue
        lines.append(f"- **{format_timestamp(item.get('start_time'))}** — {topic.strip()}")
    return "\n".join(lines)


def extract_summary_markdown(payload: Any) -> str | None:
    obj = payload
    if obj is None:
        return None
    if isinstance(obj, str):
        stripped = obj.strip()
        if not stripped:
            return None
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                return stripped
        else:
            return stripped
    if isinstance(obj, list):
        # Some summary variants are structured lists. Keep a readable JSON fallback.
        return json.dumps(obj, ensure_ascii=False, indent=2)
    if not isinstance(obj, dict):
        return None
    for key in ("ai_content", "markdown"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value
    content = obj.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, dict):
        markdown = content.get("markdown")
        if isinstance(markdown, str) and markdown.strip():
            return markdown
    return None


def summary_from_transsumm(resp: dict[str, Any]) -> str | None:
    for key in ("data_result_summ", "data_result_summ_mul", "data_note_result"):
        md = extract_summary_markdown(resp.get(key))
        if md:
            return md
    outline = resp.get("outline_result")
    if isinstance(outline, list) and outline:
        lines = ["## Topics", ""]
        for item in outline:
            if not isinstance(item, dict):
                continue
            ts = format_timestamp(item.get("start_time"))
            topic = item.get("topic") or "Untitled topic"
            lines.append(f"- **{ts}** — {topic}")
        return "\n".join(lines)
    return None


def slug_filename(value: str, *, max_len: int = 90) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\n\r\t]+", " ", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or "Untitled")[:max_len].rstrip(" .")


def date_from_start_time(start_time: int | float | None) -> str:
    if not start_time:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Plaud uses milliseconds in observed API responses.
    ts = float(start_time)
    if ts > 10_000_000_000:
        ts /= 1000
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def render_obsidian_note(
    *,
    plaud_id: str,
    title: str,
    metadata: dict[str, Any],
    transcript_md: str,
    summary_md: str | None,
    outline_md: str | None = None,
    include_transcript: bool = True,
    include_outline: bool = False,
) -> str:
    """Render an Obsidian note with PLAUD as the canonical visible body.

    The filename supplies the Obsidian title, and PLAUD's summary blob supplies
    the visible body. Keep poller sync metadata in Obsidian/YAML frontmatter
    instead of an HTML comment.
    """
    duration = metadata.get("duration")
    body = [
        "---",
        "source: plaud",
        "ingest: plaud-poller",
        f'plaud_id: "{plaud_id}"',
    ]
    if duration is not None:
        body.append(f"duration_ms: {duration}")
    body.extend(["---", ""])
    if summary_md:
        body.extend([summary_md.strip(), ""])
    if include_outline and outline_md:
        body.extend(["---", "", "## Outline", "", outline_md.strip(), ""])
    if include_transcript and transcript_md:
        body.extend(["---", "", "## Transcript", "", transcript_md.strip(), ""])
    return "\n".join(body).rstrip() + "\n"
