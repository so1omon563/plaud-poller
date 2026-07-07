from __future__ import annotations

import argparse
from pathlib import Path
import re

from .api import PlaudClient
from .config import load_settings
from .poll import fetch_content_list_artifacts, recording_id, recording_title, resolve_note_path

FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n?", re.DOTALL)
HTML_META_RE = re.compile(r"\A<!--\s*plaud-poller:.*?-->\n?", re.DOTALL)


def visible_body(markdown: str) -> str:
    body = FRONTMATTER_RE.sub("", markdown, count=1)
    body = HTML_META_RE.sub("", body, count=1)
    for marker in ("\n---\n\n## Outline", "\n---\n\n## Transcript"):
        if marker in body:
            body = body.split(marker, 1)[0]
    return body.strip()


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify Obsidian visible bodies match PLAUD canonical summaries")
    parser.add_argument("--repo-root", default=None, help="Repository/config directory containing .env")
    parser.add_argument("--limit", type=int, default=0, help="Verify at most N recordings")
    parser.add_argument("--verbose", action="store_true", help="Print per-note filenames/results")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).expanduser().resolve() if args.repo_root else Path.cwd()
    settings = load_settings(repo_root)
    client = PlaudClient(settings)
    rows = client.list_all(page_size=settings.page_size, include_trash=False)
    if args.limit:
        rows = rows[: args.limit]

    failures = 0
    checked = 0
    for row in rows:
        rid = recording_id(row)
        if not rid:
            continue
        detail = client.file_detail(rid)
        title = recording_title(row, detail)
        _, summary, _ = fetch_content_list_artifacts(client, detail.get("content_list"))
        note_path = resolve_note_path(settings.obsidian_dir, title, rid)
        checked += 1
        if not summary:
            print(f"WARN {rid[:8]} no PLAUD auto summary available")
            continue
        if not note_path.exists():
            failures += 1
            print(f"FAIL {rid[:8]} note missing")
            if args.verbose:
                print(f"missing_path={note_path}")
            continue
        local = visible_body(note_path.read_text(encoding="utf-8"))
        ok = local == summary.strip()
        if args.verbose or not ok:
            print(f"{'ok' if ok else 'FAIL'} {rid[:8]}" + (f" {note_path.name}" if args.verbose else ""))
        if not ok:
            failures += 1
    print(f"checked={checked} failures={failures}")
    return 1 if failures else 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
