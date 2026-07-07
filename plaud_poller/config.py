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


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
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
    load_dotenv(root / ".env")

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
    )
