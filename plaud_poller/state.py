from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
create table if not exists recordings (
  plaud_id text primary key,
  title text,
  start_time integer,
  duration integer,
  metadata_hash text,
  transcript_hash text,
  summary_hash text,
  note_hash text,
  audio_downloaded integer not null default 0,
  first_seen_at text not null,
  last_seen_at text not null,
  last_changed_at text
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class State:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def get(self, plaud_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("select * from recordings where plaud_id = ?", (plaud_id,)).fetchone()
        return dict(row) if row else None

    def upsert_seen(
        self,
        plaud_id: str,
        *,
        title: str,
        start_time: int | None,
        duration: int | None,
        metadata_hash: str,
        transcript_hash: str | None,
        summary_hash: str | None,
        note_hash: str | None,
        audio_downloaded: bool,
        changed: bool,
    ) -> None:
        now = utc_now()
        existing = self.get(plaud_id)
        if existing is None:
            self.conn.execute(
                """
                insert into recordings (
                  plaud_id, title, start_time, duration, metadata_hash, transcript_hash,
                  summary_hash, note_hash, audio_downloaded, first_seen_at, last_seen_at, last_changed_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plaud_id,
                    title,
                    start_time,
                    duration,
                    metadata_hash,
                    transcript_hash,
                    summary_hash,
                    note_hash,
                    int(audio_downloaded),
                    now,
                    now,
                    now if changed else None,
                ),
            )
        else:
            self.conn.execute(
                """
                update recordings
                   set title = ?, start_time = ?, duration = ?, metadata_hash = ?,
                       transcript_hash = ?, summary_hash = ?, note_hash = ?,
                       audio_downloaded = ?, last_seen_at = ?,
                       last_changed_at = case when ? then ? else last_changed_at end
                 where plaud_id = ?
                """,
                (
                    title,
                    start_time,
                    duration,
                    metadata_hash,
                    transcript_hash,
                    summary_hash,
                    note_hash,
                    int(audio_downloaded),
                    now,
                    int(changed),
                    now,
                    plaud_id,
                ),
            )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
