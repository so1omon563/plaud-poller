from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import sys

REGION_API_BASES = {
    "aws:us-west-2": "https://api.plaud.ai",
    "aws:eu-central-1": "https://api-euc1.plaud.ai",
    "aws:ap-southeast-1": "https://api-apse1.plaud.ai",
}
DEFAULT_REGION = "aws:us-west-2"
APP_NAME = "plaud-poller"


def load_dotenv(path: Path, *, override: bool = False) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if override:
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    authorization: str
    region: str
    data_dir: Path
    recordings_dir: Path
    obsidian_dir: Path
    state_db: Path
    page_size: int
    download_audio: bool
    include_trash: bool
    trash_policy: str
    trash_archive_dir: Path
    report_mode: str
    note_backup_on_change: bool
    note_backup_dir: Path
    note_include_transcript: bool
    note_include_outline: bool
    preserve_task_state: bool

    @property
    def api_base(self) -> str:
        return REGION_API_BASES.get(self.region, REGION_API_BASES[DEFAULT_REGION])


def truthy(value: str | None) -> bool:
    return bool(value and value.strip().lower() in {"1", "true", "yes", "on"})


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser()


def default_data_dir() -> Path:
    """Return a platform-neutral default data directory.

    Users should normally set PLAUD_DATA_DIR explicitly. This fallback avoids
    embedding one maintainer's home directory in a public project.
    """
    if xdg := os.environ.get("XDG_DATA_HOME"):
        return expand_path(xdg) / APP_NAME
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return expand_path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def load_settings(repo_root: Path | None = None) -> Settings:
    root = repo_root or Path.cwd()
    env_path = root / ".env"
    load_dotenv(env_path)

    if truthy(os.environ.get("PLAUD_AUTO_REFRESH_TOKEN")):
        from .auth import refresh_env_token

        changed, _ = refresh_env_token(
            env_path,
            min_ttl_seconds=int(os.environ.get("PLAUD_REFRESH_MIN_TTL_SECONDS", "3600")),
        )
        if changed:
            # Refresh process writes .env; update this process too.
            load_dotenv(env_path, override=True)

    authorization = os.environ.get("PLAUD_AUTHORIZATION", "").strip()
    token = os.environ.get("PLAUD_TOKEN", "").strip()
    if not authorization and token:
        authorization = f"Bearer {token}"
    if not authorization:
        raise SystemExit("Missing PLAUD_AUTHORIZATION or PLAUD_TOKEN in environment/.env")

    data_dir = expand_path(os.environ["PLAUD_DATA_DIR"]) if os.environ.get("PLAUD_DATA_DIR") else default_data_dir()
    recordings_dir = (
        expand_path(os.environ["PLAUD_RECORDINGS_DIR"])
        if os.environ.get("PLAUD_RECORDINGS_DIR")
        else data_dir / "recordings"
    )
    obsidian_dir = (
        expand_path(os.environ["PLAUD_OBSIDIAN_DIR"])
        if os.environ.get("PLAUD_OBSIDIAN_DIR")
        else data_dir / "obsidian-notes"
    )
    trash_policy = os.environ.get("PLAUD_TRASH_POLICY", "archive").strip().lower() or "archive"
    if trash_policy not in {"keep", "archive", "delete"}:
        raise SystemExit("PLAUD_TRASH_POLICY must be one of: keep, archive, delete")
    trash_archive_dir = (
        expand_path(os.environ["PLAUD_TRASH_ARCHIVE_DIR"])
        if os.environ.get("PLAUD_TRASH_ARCHIVE_DIR")
        else obsidian_dir / "_Archive" / "plaud-trash"
    )
    report_mode = os.environ.get("PLAUD_REPORT_MODE", "changes").strip().lower() or "changes"
    if report_mode not in {"quiet", "changes", "summary", "verbose"}:
        raise SystemExit("PLAUD_REPORT_MODE must be one of: quiet, changes, summary, verbose")
    note_backup_dir = (
        expand_path(os.environ["PLAUD_NOTE_BACKUP_DIR"])
        if os.environ.get("PLAUD_NOTE_BACKUP_DIR")
        else obsidian_dir / "_Archive" / "plaud-note-versions"
    )
    state_db = (
        expand_path(os.environ["PLAUD_STATE_DB"])
        if os.environ.get("PLAUD_STATE_DB")
        else data_dir / "state.sqlite"
    )

    return Settings(
        authorization=authorization,
        region=os.environ.get("PLAUD_REGION", DEFAULT_REGION).strip() or DEFAULT_REGION,
        data_dir=data_dir,
        recordings_dir=recordings_dir,
        obsidian_dir=obsidian_dir,
        state_db=state_db,
        page_size=int(os.environ.get("PLAUD_PAGE_SIZE", "50")),
        download_audio=truthy(os.environ.get("PLAUD_DOWNLOAD_AUDIO")),
        include_trash=truthy(os.environ.get("PLAUD_INCLUDE_TRASH")),
        trash_policy=trash_policy,
        trash_archive_dir=trash_archive_dir,
        report_mode=report_mode,
        note_backup_on_change=truthy(os.environ.get("PLAUD_NOTE_BACKUP_ON_CHANGE")),
        note_backup_dir=note_backup_dir,
        note_include_transcript=truthy(os.environ.get("PLAUD_NOTE_INCLUDE_TRANSCRIPT", "true")),
        note_include_outline=truthy(os.environ.get("PLAUD_NOTE_INCLUDE_OUTLINE")),
        preserve_task_state=truthy(os.environ.get("PLAUD_PRESERVE_TASK_STATE", "true")),
    )
