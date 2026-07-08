from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import REGION_API_BASES

JWT_RE = re.compile(rb"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
API_BASE_TO_REGION = {urlparse(v).hostname: k for k, v in REGION_API_BASES.items()}


@dataclass(frozen=True)
class BrowserToken:
    token: str
    browser: str
    profile: str
    email: str | None
    iat: int | None
    exp: int | None
    region: str | None

    @property
    def expires_at(self) -> str:
        if not self.exp:
            return "unknown"
        return datetime.fromtimestamp(self.exp, timezone.utc).isoformat()


@dataclass(frozen=True)
class BrowserWorkspaceSession:
    browser: str
    profile: str
    workspace_id: str
    domain: str | None
    region: str | None
    workspace_token: str | None
    expires_at_ms: int | None
    refresh_token: str
    refresh_expires_at_ms: int | None

    @property
    def refresh_expires_at(self) -> str:
        if not self.refresh_expires_at_ms:
            return "unknown"
        return datetime.fromtimestamp(self.refresh_expires_at_ms / 1000, timezone.utc).isoformat()


def jwt_claims(token: str) -> dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode()).decode("utf-8"))
    except Exception:
        return {}


def browser_roots() -> list[tuple[str, Path]]:
    home = Path.home()
    if sys.platform == "darwin":
        app_support = home / "Library" / "Application Support"
        return [
            ("Chrome", app_support / "Google" / "Chrome"),
            ("Edge", app_support / "Microsoft Edge"),
            ("Brave", app_support / "BraveSoftware" / "Brave-Browser"),
            ("Arc", app_support / "Arc" / "User Data"),
            ("Vivaldi", app_support / "Vivaldi"),
        ]
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return [
            ("Chrome", local / "Google" / "Chrome" / "User Data"),
            ("Edge", local / "Microsoft" / "Edge" / "User Data"),
            ("Brave", local / "BraveSoftware" / "Brave-Browser" / "User Data"),
            ("Vivaldi", local / "Vivaldi" / "User Data"),
        ]
    cfg = home / ".config"
    return [
        ("Chrome", cfg / "google-chrome"),
        ("Edge", cfg / "microsoft-edge"),
        ("Brave", cfg / "BraveSoftware" / "Brave-Browser"),
        ("Vivaldi", cfg / "vivaldi"),
        ("Chromium", cfg / "chromium"),
    ]


def discover_leveldb_profiles() -> list[tuple[str, str, Path]]:
    found: list[tuple[str, str, Path]] = []
    for browser, root in browser_roots():
        if not root.exists():
            continue
        for child in root.iterdir():
            if child.name != "Default" and not child.name.startswith("Profile "):
                continue
            leveldb = child / "Local Storage" / "leveldb"
            if leveldb.exists():
                found.append((browser, child.name, leveldb))
    return sorted(found, key=lambda item: (item[0], item[1] != "Default", item[1]))


def _scan_file_for_tokens(path: Path) -> set[str]:
    try:
        data = path.read_bytes()
    except OSError:
        return set()
    if b"plaud.ai" not in data and b"PLADU_" not in data and b"tokenstr" not in data:
        return set()
    tokens: set[str] = set()
    for match in JWT_RE.finditer(data):
        token = match.group(0).decode("ascii", errors="ignore")
        claims = jwt_claims(token)
        if isinstance(claims.get("exp"), int) or isinstance(claims.get("iat"), int) or claims.get("region"):
            tokens.add(token)
    return tokens


def _candidate_workspace_json_blobs(text: str) -> list[str]:
    blobs: list[str] = []
    search_from = 0
    while True:
        marker = text.find("workspaceList", search_from)
        if marker < 0:
            break
        start = text.find("[", marker)
        if start < 0:
            search_from = marker + len("workspaceList")
            continue
        depth = 0
        in_string = False
        escaped = False
        for pos in range(start, min(len(text), start + 200_000)):
            ch = text[pos]
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    blobs.append(text[start : pos + 1])
                    search_from = pos + 1
                    break
        else:
            search_from = marker + len("workspaceList")
    return blobs


def _scan_file_for_workspace_sessions(path: Path, browser: str, profile: str) -> list[BrowserWorkspaceSession]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    if "workspaceList" not in text or "refreshToken" not in text:
        return []
    sessions: list[BrowserWorkspaceSession] = []
    for raw in _candidate_workspace_json_blobs(text):
        # Chromium LevelDB stores JSON strings with escaped quotes. Try both the raw
        # substring and an unescaped variant.
        for candidate in (raw, raw.encode("utf-8", errors="ignore").decode("unicode_escape", errors="ignore")):
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, list):
                continue
            for item in data:
                if not isinstance(item, dict):
                    continue
                workspace_id = item.get("workspaceId")
                refresh_token = item.get("refreshToken")
                if not isinstance(workspace_id, str) or not isinstance(refresh_token, str):
                    continue
                sessions.append(
                    BrowserWorkspaceSession(
                        browser=browser,
                        profile=profile,
                        workspace_id=workspace_id,
                        domain=item.get("domain") if isinstance(item.get("domain"), str) else None,
                        region=item.get("region") if isinstance(item.get("region"), str) else None,
                        workspace_token=item.get("workspaceToken") if isinstance(item.get("workspaceToken"), str) else None,
                        expires_at_ms=item.get("expiresAt") if isinstance(item.get("expiresAt"), int) else None,
                        refresh_token=refresh_token,
                        refresh_expires_at_ms=item.get("refreshExpiresAt") if isinstance(item.get("refreshExpiresAt"), int) else None,
                    )
                )
            break
    return sessions


def scan_leveldb_for_workspace_sessions(browser: str, profile: str, leveldb_path: Path) -> list[BrowserWorkspaceSession]:
    sessions: list[BrowserWorkspaceSession] = []
    with tempfile.TemporaryDirectory(prefix="plaud-poller-ldb-") as tmp:
        tmp_path = Path(tmp)
        for entry in leveldb_path.iterdir():
            if entry.name == "LOCK":
                continue
            dest = tmp_path / entry.name
            try:
                if entry.is_dir():
                    shutil.copytree(entry, dest)
                else:
                    shutil.copy2(entry, dest)
            except OSError:
                continue
        for file in tmp_path.iterdir():
            if file.suffix in {".ldb", ".log"}:
                sessions.extend(_scan_file_for_workspace_sessions(file, browser, profile))
    # Deduplicate by refresh token while preserving the newest-looking entry.
    dedup: dict[str, BrowserWorkspaceSession] = {}
    for session in sessions:
        existing = dedup.get(session.refresh_token)
        if not existing or (session.refresh_expires_at_ms or 0) > (existing.refresh_expires_at_ms or 0):
            dedup[session.refresh_token] = session
    return list(dedup.values())


def find_browser_workspace_sessions() -> list[BrowserWorkspaceSession]:
    sessions: list[BrowserWorkspaceSession] = []
    for browser, profile, leveldb in discover_leveldb_profiles():
        sessions.extend(scan_leveldb_for_workspace_sessions(browser, profile, leveldb))
    sessions.sort(key=lambda s: s.refresh_expires_at_ms or 0, reverse=True)
    return sessions


def scan_leveldb_for_tokens(browser: str, profile: str, leveldb_path: Path) -> list[BrowserToken]:
    tokens: set[str] = set()
    # Copying avoids problems when Chromium has a live LOCK file.
    with tempfile.TemporaryDirectory(prefix="plaud-poller-ldb-") as tmp:
        tmp_path = Path(tmp)
        for entry in leveldb_path.iterdir():
            if entry.name == "LOCK":
                continue
            dest = tmp_path / entry.name
            try:
                if entry.is_dir():
                    shutil.copytree(entry, dest)
                else:
                    shutil.copy2(entry, dest)
            except OSError:
                continue
        for file in tmp_path.iterdir():
            if file.suffix in {".ldb", ".log"}:
                tokens.update(_scan_file_for_tokens(file))
    out: list[BrowserToken] = []
    for token in tokens:
        claims = jwt_claims(token)
        out.append(
            BrowserToken(
                token=token,
                browser=browser,
                profile=profile,
                email=claims.get("email") if isinstance(claims.get("email"), str) else None,
                iat=claims.get("iat") if isinstance(claims.get("iat"), int) else None,
                exp=claims.get("exp") if isinstance(claims.get("exp"), int) else None,
                region=claims.get("region") if isinstance(claims.get("region"), str) else None,
            )
        )
    return out


def find_browser_tokens() -> list[BrowserToken]:
    tokens: list[BrowserToken] = []
    for browser, profile, leveldb in discover_leveldb_profiles():
        tokens.extend(scan_leveldb_for_tokens(browser, profile, leveldb))
    tokens.sort(key=lambda t: t.iat or 0, reverse=True)
    return tokens


def validate_token(token: str, region: str | None = None) -> tuple[bool, str | None, str | None]:
    order: list[str] = []
    if region in REGION_API_BASES:
        order.append(region)
    order.extend(r for r in REGION_API_BASES if r not in order)
    last_error: str | None = None
    for candidate_region in order:
        base = REGION_API_BASES[candidate_region]
        url = f"{base}/file/simple/web?skip=0&limit=1&is_trash=2&sort_by=start_time&is_desc=true"
        req = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
                "Authorization": f"Bearer {token}",
            },
            method="GET",
        )
        try:
            with urlopen(req, timeout=30) as resp:  # noqa: S310 - explicit Plaud API validation
                text = resp.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            last_error = exc.read().decode("utf-8", errors="replace")[:200]
            continue
        except URLError as exc:
            last_error = str(exc)[:200]
            continue
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            last_error = text[:200]
            continue
        if body.get("status") == 0:
            return True, candidate_region, None
        if body.get("status") == -302:
            api = (((body.get("data") or {}).get("domains") or {}).get("api"))
            host = urlparse(api).hostname if api else None
            corrected = API_BASE_TO_REGION.get(host)
            if corrected and corrected != candidate_region:
                return validate_token(token, corrected)
        last_error = str(body.get("msg") or body)[:200]
    return False, None, last_error


def refresh_workspace_session(session: BrowserWorkspaceSession) -> tuple[bool, str | None, str | None, str | None]:
    domain = session.domain or (REGION_API_BASES.get(session.region or "") or REGION_API_BASES["aws:us-west-2"])
    url = f"{domain.rstrip('/')}/user-app/auth/workspace/refresh/{session.workspace_id}"
    req = Request(
        url,
        data=b"{}",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {session.refresh_token}",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as resp:  # noqa: S310 - explicit Plaud API refresh
            text = resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        return False, None, session.region, exc.read().decode("utf-8", errors="replace")[:200]
    except URLError as exc:
        return False, None, session.region, str(exc)[:200]
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        return False, None, session.region, text[:200]
    if body.get("status") == -302:
        api = (((body.get("data") or {}).get("domains") or {}).get("api"))
        host = urlparse(api).hostname if api else None
        corrected_region = API_BASE_TO_REGION.get(host)
        if corrected_region and corrected_region != session.region:
            corrected = BrowserWorkspaceSession(
                browser=session.browser,
                profile=session.profile,
                workspace_id=session.workspace_id,
                domain=api,
                region=corrected_region,
                workspace_token=session.workspace_token,
                expires_at_ms=session.expires_at_ms,
                refresh_token=session.refresh_token,
                refresh_expires_at_ms=session.refresh_expires_at_ms,
            )
            return refresh_workspace_session(corrected)
    if body.get("status") != 0:
        return False, None, session.region, str(body.get("msg") or body)[:200]
    data = body.get("data") or {}
    token = data.get("workspace_token") or data.get("access_token")
    if not isinstance(token, str) or not token:
        return False, None, session.region, "refresh response did not include workspace token"
    ok, region, error = validate_token(token, session.region)
    if not ok:
        return False, None, region or session.region, error or "refreshed workspace token did not validate"
    return True, token, region or session.region, None


def read_env_token(env_path: Path) -> str | None:
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("PLAUD_AUTHORIZATION="):
            value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            return value.removeprefix("Bearer ").strip() or None
        if stripped.startswith("PLAUD_TOKEN="):
            value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            if value:
                return value
    return None


def write_env_values(env_path: Path, updates: dict[str, str]) -> None:
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    lines = existing.splitlines()
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
            else:
                out.append(line)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    env_path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass


def refresh_env_token(env_path: Path, *, min_ttl_seconds: int = 3600, force: bool = False) -> tuple[bool, str]:
    current = read_env_token(env_path)
    current_claims = jwt_claims(current) if current else {}
    current_exp = current_claims.get("exp") if isinstance(current_claims.get("exp"), int) else None
    now = int(time.time())
    if current and not force and current_exp and current_exp - now > min_ttl_seconds:
        return False, "existing token still has sufficient TTL"

    last_refresh_error: str | None = None
    for session in find_browser_workspace_sessions():
        ok, token, region, error = refresh_workspace_session(session)
        if not ok or not token:
            last_refresh_error = error
            continue
        write_env_values(
            env_path,
            {
                "PLAUD_AUTHORIZATION": f"Bearer {token}",
                "PLAUD_TOKEN": "",
                "PLAUD_REGION": region or session.region or "aws:us-west-2",
            },
        )
        return True, f"refreshed workspace token from {session.browser}/{session.profile}; refresh_exp={session.refresh_expires_at}"

    for candidate in find_browser_tokens():
        if not force and current and candidate.token == current and current_exp and current_exp - now > 0:
            continue
        ok, region, error = validate_token(candidate.token, candidate.region)
        if not ok:
            continue
        write_env_values(
            env_path,
            {
                "PLAUD_AUTHORIZATION": f"Bearer {candidate.token}",
                "PLAUD_TOKEN": "",
                "PLAUD_REGION": region or candidate.region or "aws:us-west-2",
            },
        )
        return True, f"refreshed from {candidate.browser}/{candidate.profile}; exp={candidate.expires_at}"
    detail = f"; latest workspace refresh error: {last_refresh_error}" if last_refresh_error else ""
    return False, f"no valid browser token found{detail}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PLAUD auth helper")
    parser.add_argument("command", choices=["detect", "refresh"], help="Detect browser tokens or refresh .env")
    parser.add_argument("--env", default=".env", help="Path to .env file to update")
    parser.add_argument("--force", action="store_true", help="Refresh even when current token appears usable")
    args = parser.parse_args(argv)

    if args.command == "detect":
        sessions = find_browser_workspace_sessions()
        for session in sessions[:10]:
            ok, _, region, error = refresh_workspace_session(session)
            status = "refreshable" if ok else f"not_refreshable error={error or 'unknown'}"
            print(
                f"workspace source={session.browser}/{session.profile} region={region or session.region or 'unknown'} "
                f"workspace_id={session.workspace_id} refresh_exp={session.refresh_expires_at} {status} token=[REDACTED]"
            )
        found = find_browser_tokens()
        valid_count = 0
        for token in found[:10]:
            ok, region, _ = validate_token(token.token, token.region)
            if ok:
                valid_count += 1
                print(
                    f"valid source={token.browser}/{token.profile} region={region or token.region or 'unknown'} "
                    f"email={token.email or 'unknown'} exp={token.expires_at} token=[REDACTED]"
                )
        if valid_count == 0:
            print(f"no valid PLAUD browser tokens found; scanned_candidates={len(found)}")
            return 1
        return 0

    changed, message = refresh_env_token(Path(args.env), force=args.force)
    print(message)
    return 0 if changed or "sufficient TTL" in message else 1


if __name__ == "__main__":
    raise SystemExit(main())
