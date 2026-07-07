from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


DEFAULT_DENYLIST = ".plaud-privacy-denylist"


HOOK_SCRIPT = """#!/usr/bin/env sh
set -eu
python3 -m plaud_poller.privacy --repo-root .
"""


def read_terms(paths: list[Path], inline_terms: list[str]) -> list[str]:
    terms: list[str] = []
    for term in inline_terms:
        term = term.strip()
        if term:
            terms.append(term)
    for path in paths:
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            terms.append(line)
    # Preserve order, remove duplicates.
    return list(dict.fromkeys(terms))


def tracked_files(repo_root: Path) -> list[Path]:
    out = subprocess.check_output(["git", "ls-files"], cwd=repo_root, text=True)
    return [repo_root / line for line in out.splitlines() if line]


def scan_files(repo_root: Path, terms: list[str]) -> list[tuple[Path, int, str]]:
    hits: list[tuple[Path, int, str]] = []
    for path in tracked_files(repo_root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            for term in terms:
                if term and term in line:
                    hits.append((path.relative_to(repo_root), line_no, term))
    return hits


def install_hook(repo_root: Path, *, force: bool = False) -> Path:
    git_dir = repo_root / ".git"
    if not git_dir.is_dir():
        raise SystemExit(f"Not a git repository: {repo_root}")
    hook_path = git_dir / "hooks" / "pre-commit"
    if hook_path.exists() and not force:
        existing = hook_path.read_text(encoding="utf-8", errors="replace")
        if "plaud_poller.privacy" not in existing:
            raise SystemExit(f"pre-commit hook already exists: {hook_path} (use --force to replace)")
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(HOOK_SCRIPT, encoding="utf-8")
    hook_path.chmod(0o755)
    return hook_path


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan tracked repo files for caller-provided private terms")
    parser.add_argument("--repo-root", default=".", help="Git repository root")
    parser.add_argument("--denylist", action="append", default=[], help="File containing one private term per line")
    parser.add_argument("--term", action="append", default=[], help="Private term to scan for; may be repeated")
    parser.add_argument("--redact", action="store_true", default=True, help="Print only path/line/term metadata, not matched content")
    parser.add_argument("--install-hook", action="store_true", help="Install a git pre-commit hook that runs this privacy check")
    parser.add_argument("--force", action="store_true", help="Replace an existing pre-commit hook when used with --install-hook")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).expanduser().resolve()
    if args.install_hook:
        hook_path = install_hook(repo_root, force=args.force)
        print(f"installed_pre_commit_hook={hook_path}")
        return 0
    denylist_paths = [repo_root / DEFAULT_DENYLIST, *[Path(p).expanduser().resolve() for p in args.denylist]]
    terms = read_terms(denylist_paths, args.term)
    if not terms:
        print(f"No privacy terms configured. Add {DEFAULT_DENYLIST}, pass --denylist, or pass --term.")
        return 0
    hits = scan_files(repo_root, terms)
    if not hits:
        print(f"privacy_check=ok scanned_terms={len(terms)}")
        return 0
    print(f"privacy_check=FAIL hits={len(hits)} scanned_terms={len(terms)}", file=sys.stderr)
    for path, line_no, term in hits:
        print(f"{path}:{line_no}: term={term!r} [MATCH REDACTED]", file=sys.stderr)
    return 1


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
