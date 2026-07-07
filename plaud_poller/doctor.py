from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

from .api import PlaudClient
from .auth import jwt_claims, read_env_token, validate_token
from .config import load_settings


@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    @property
    def status(self) -> str:
        return "ok" if self.ok else "WARN"


def fmt_exp(exp: Any) -> str:
    if not isinstance(exp, int):
        return "unknown"
    dt = datetime.fromtimestamp(exp, timezone.utc)
    now = datetime.now(timezone.utc)
    delta = dt - now
    hours = delta.total_seconds() / 3600
    return f"{dt.isoformat()} ({hours:.1f}h remaining)"


def is_writable_dir(path: Path) -> bool:
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ".plaud-poller-write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def nearest_obsidian_vault(path: Path) -> Path | None:
    current = path.resolve()
    if current.is_file():
        current = current.parent
    for candidate in [current, *current.parents]:
        if (candidate / ".obsidian").is_dir():
            return candidate
    return None


def active_obsidian_vault_paths() -> list[Path]:
    # Obsidian's Electron config is not a public API, but this is useful as a
    # best-effort local diagnostic. Failure is non-fatal.
    config = Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json"
    if not config.exists():
        return []
    try:
        import json

        data = json.loads(config.read_text(encoding="utf-8"))
        vaults = data.get("vaults") if isinstance(data, dict) else None
        if not isinstance(vaults, dict):
            return []
        out: list[Path] = []
        for item in vaults.values():
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                out.append(Path(item["path"]).expanduser())
        return out
    except Exception:
        return []


def count_rows(client: PlaudClient, is_trash: int) -> int:
    data = client.request_json(
        f"/file/simple/web?skip=0&limit=1&is_trash={is_trash}&sort_by=start_time&is_desc=true"
    )
    # The endpoint doesn't expose an obviously reliable total in all observed
    # responses, so count via list pagination for active/default behavior and
    # use a bounded page for trash modes.
    rows = data.get("data_file_list") or []
    if len(rows) < 1:
        return 0
    # Use full pagination for precise counts.
    total = 0
    skip = 0
    while True:
        page = client.request_json(
            f"/file/simple/web?skip={skip}&limit=50&is_trash={is_trash}&sort_by=start_time&is_desc=true"
        ).get("data_file_list") or []
        total += len(page)
        if len(page) < 50:
            return total
        skip += 50


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose plaud-poller configuration")
    parser.add_argument("--repo-root", default=None, help="Repository/config directory containing .env")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).expanduser().resolve() if args.repo_root else Path.cwd()
    checks: list[Check] = []

    try:
        settings = load_settings(repo_root)
    except SystemExit as exc:
        print(f"ERROR config: {exc}")
        return 2

    env_path = repo_root / ".env"
    token = read_env_token(env_path)
    claims = jwt_claims(token) if token else {}
    token_exp = claims.get("exp")
    token_region = claims.get("region")

    checks.append(Check("env_file", env_path.exists(), str(env_path)))
    checks.append(Check("token_present", bool(token), "token=[REDACTED]" if token else "missing"))
    if token:
        ok, region, error = validate_token(token, token_region if isinstance(token_region, str) else settings.region)
        checks.append(Check("token_valid", ok, f"region={region or 'unknown'} exp={fmt_exp(token_exp)}" if ok else f"{error}"))
    checks.append(Check("api_region", settings.region in {"aws:us-west-2", "aws:eu-central-1", "aws:ap-southeast-1"}, settings.region))

    for name, path in (
        ("data_dir", settings.data_dir),
        ("recordings_dir", settings.recordings_dir),
        ("state_db_parent", settings.state_db.parent),
        ("markdown_dir", settings.obsidian_dir),
    ):
        checks.append(Check(name, is_writable_dir(path), str(path)))

    vault = nearest_obsidian_vault(settings.obsidian_dir)
    if vault:
        checks.append(Check("markdown_inside_obsidian_vault", True, str(vault)))
        active_vaults = active_obsidian_vault_paths()
        if active_vaults:
            checks.append(
                Check(
                    "markdown_vault_known_to_obsidian",
                    any(vault.resolve() == p.resolve() for p in active_vaults if p.exists()),
                    ", ".join(str(p) for p in active_vaults),
                )
            )
    else:
        checks.append(Check("markdown_inside_obsidian_vault", False, f"{settings.obsidian_dir} is not under a .obsidian vault"))

    try:
        client = PlaudClient(settings)
        active_count = count_rows(client, 0)
        trash_count = count_rows(client, 1)
        checks.append(Check("plaud_active_recordings", True, str(active_count)))
        checks.append(Check("plaud_trash_recordings", True, str(trash_count)))
        checks.append(Check("include_trash", True, str(settings.include_trash).lower()))
        checks.append(Check("trash_policy", True, settings.trash_policy))
        checks.append(Check("trash_archive_dir", True, str(settings.trash_archive_dir)))
        checks.append(Check("note_include_transcript", True, str(settings.note_include_transcript).lower()))
        checks.append(Check("note_include_outline", True, str(settings.note_include_outline).lower()))
    except Exception as exc:
        checks.append(Check("plaud_api", False, str(exc)))

    width = max(len(c.name) for c in checks)
    for check in checks:
        print(f"{check.status:4} {check.name:<{width}} {check.detail}")

    return 0 if all(c.ok for c in checks) else 1


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
