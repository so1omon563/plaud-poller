from __future__ import annotations

from dataclasses import replace
import gzip
import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .config import REGION_API_BASES, Settings

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
API_BASE_TO_REGION = {urlparse(v).hostname: k for k, v in REGION_API_BASES.items()}


class PlaudAuthError(RuntimeError):
    pass


class PlaudApiError(RuntimeError):
    def __init__(self, message: str, status: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class PlaudClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        retry_region: bool = True,
    ) -> Any:
        text = self._request_text(path, method=method, body=body, headers=headers)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PlaudApiError(f"Plaud {path} returned non-JSON: {exc}", body=text[:500]) from exc

        if retry_region and isinstance(parsed, dict) and parsed.get("status") == -302:
            api_domain = ((parsed.get("data") or {}).get("domains") or {}).get("api")
            if not api_domain:
                raise PlaudApiError("Plaud region mismatch response did not include API domain", body=text[:500])
            hostname = urlparse(api_domain).hostname
            region = API_BASE_TO_REGION.get(hostname)
            if not region:
                raise PlaudApiError(f"Plaud requested unknown API domain {api_domain}", body=text[:500])
            self.settings = replace(self.settings, region=region)
            text = self._request_text(path, method=method, body=body, headers=headers)
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise PlaudApiError(
                    f"Plaud {path} returned non-JSON after region correction: {exc}",
                    body=text[:500],
                ) from exc
        return parsed

    def _request_text(
        self,
        path_or_url: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        url = path_or_url if path_or_url.startswith("http") else f"{self.settings.api_base}{path_or_url}"
        req_headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Authorization": self.settings.authorization,
            **(headers or {}),
        }
        if body is not None and "Content-Type" not in req_headers:
            req_headers["Content-Type"] = "application/json"
        data = body
        last: Exception | None = None
        for attempt in range(1, 4):
            try:
                req = Request(url, data=data, headers=req_headers, method=method)
                with urlopen(req, timeout=60) as res:  # noqa: S310 - explicit target API/presigned URLs
                    return res.read().decode("utf-8")
            except HTTPError as exc:
                text = exc.read().decode("utf-8", errors="replace")
                if exc.code == 401:
                    raise PlaudAuthError("Plaud returned 401 — token expired or revoked") from exc
                if exc.code >= 500 and attempt < 3:
                    time.sleep(attempt)
                    continue
                raise PlaudApiError(f"Plaud {method} {path_or_url} → HTTP {exc.code}", exc.code, text[:500]) from exc
            except URLError as exc:
                last = exc
                if attempt < 3:
                    time.sleep(attempt)
                    continue
        raise PlaudApiError(f"Network error after retries: {last}")

    def list_recordings(self, *, skip: int = 0, limit: int = 50, include_trash: bool = False) -> list[dict[str, Any]]:
        query = urlencode(
            {
                "skip": skip,
                "limit": limit,
                # PLAUD values observed:
                #   0 = active recordings only
                #   1 = trash only
                #   2 = active + trash
                "is_trash": 2 if include_trash else 0,
                "sort_by": "start_time",
                "is_desc": "true",
            }
        )
        data = self.request_json(f"/file/simple/web?{query}")
        return list(data.get("data_file_list") or [])

    def list_all(
        self,
        *,
        page_size: int = 50,
        max_pages: int = 200,
        include_trash: bool = False,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        skip = 0
        for _ in range(max_pages):
            page = self.list_recordings(skip=skip, limit=page_size, include_trash=include_trash)
            if not page:
                break
            out.extend(page)
            if len(page) < page_size:
                break
            skip += page_size
        return out

    def file_detail(self, recording_id: str) -> dict[str, Any]:
        data = self.request_json(f"/file/detail/{recording_id}")
        return dict(data.get("data") or {})

    def transcript_and_summary(self, recording_id: str) -> dict[str, Any]:
        return dict(self.request_json(f"/ai/transsumm/{recording_id}", method="POST", body=b"{}"))

    def temp_audio_url(self, recording_id: str) -> str:
        data = self.request_json(f"/file/temp-url/{recording_id}")
        url = data.get("temp_url")
        if not isinstance(url, str) or not url:
            raise PlaudApiError(f"No temp_url in response for {recording_id}", body=json.dumps(data)[:500])
        return url

    def fetch_presigned_bytes(self, url: str) -> bytes:
        req = Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
        with urlopen(req, timeout=120) as res:  # noqa: S310 - presigned URL from Plaud
            return res.read()


def maybe_gunzip(payload: bytes) -> bytes:
    try:
        return gzip.decompress(payload)
    except OSError:
        return payload
