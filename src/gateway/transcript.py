"""
Gateway-owned transcript store.

SQLite-backed persistent transcript that serves as the single source of truth
for conversation history. Replaces client-side localStorage dependency.

Each entry records: role, content, aborted flag, metadata, timestamps.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


_DB_PATH = Path("data/transcript.db")


@dataclass
class TranscriptEntry:
    id: str
    session_id: str
    role: str  # "user" | "assistant" | "system" | "tool" | "inject"
    content: str
    request_id: Optional[str]
    aborted: bool
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "role": self.role,
            "content": self.content,
            "request_id": self.request_id,
            "aborted": self.aborted,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }


class TranscriptStore:
    """SQLite-backed transcript storage."""

    def __init__(self, db_path: Path = _DB_PATH):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._init_db()

    def _init_db(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS transcript (
                id          TEXT PRIMARY KEY,
                session_id  TEXT NOT NULL,
                role        TEXT NOT NULL,
                content     TEXT NOT NULL DEFAULT '',
                request_id  TEXT,
                aborted     INTEGER NOT NULL DEFAULT 0,
                metadata    TEXT NOT NULL DEFAULT '{}',
                created_at  REAL NOT NULL
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_transcript_session
            ON transcript (session_id, created_at)
        """)
        self._conn.commit()

    def append(
        self,
        session_id: str,
        role: str,
        content: str,
        request_id: Optional[str] = None,
        aborted: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TranscriptEntry:
        entry_id = uuid.uuid4().hex[:16]
        now = time.time()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)

        self._conn.execute(
            """INSERT INTO transcript
               (id, session_id, role, content, request_id, aborted, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (entry_id, session_id, role, content, request_id, 1 if aborted else 0,
             meta_json, now),
        )
        self._conn.commit()

        return TranscriptEntry(
            id=entry_id,
            session_id=session_id,
            role=role,
            content=content,
            request_id=request_id,
            aborted=aborted,
            metadata=metadata or {},
            created_at=now,
        )

    def get_history(
        self,
        session_id: str,
        limit: int = 100,
        before: Optional[float] = None,
    ) -> List[TranscriptEntry]:
        if before is not None:
            rows = self._conn.execute(
                """SELECT * FROM transcript
                   WHERE session_id = ? AND created_at < ?
                   ORDER BY created_at DESC LIMIT ?""",
                (session_id, before, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT * FROM transcript
                   WHERE session_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()

        entries = [self._row(r) for r in rows]
        entries.reverse()  # chronological order
        return entries

    def get_entry(self, entry_id: str) -> Optional[TranscriptEntry]:
        row = self._conn.execute(
            "SELECT * FROM transcript WHERE id = ?", (entry_id,)
        ).fetchone()
        return self._row(row) if row else None

    def session_count(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM transcript WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return row[0] if row else 0

    def list_sessions(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return distinct sessions with their entry counts and last activity."""
        rows = self._conn.execute(
            """SELECT session_id, COUNT(*) as cnt, MAX(created_at) as last_at
               FROM transcript
               GROUP BY session_id
               ORDER BY last_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [
            {"session_id": r[0], "entry_count": r[1], "last_activity": r[2]}
            for r in rows
        ]

    @staticmethod
    def _row(r: tuple) -> TranscriptEntry:
        try:
            meta = json.loads(r[6])
        except (json.JSONDecodeError, TypeError):
            meta = {}
        return TranscriptEntry(
            id=r[0],
            session_id=r[1],
            role=r[2],
            content=r[3],
            request_id=r[4],
            aborted=bool(r[5]),
            metadata=meta,
            created_at=r[7],
        )


# Global singleton
_store: Optional[TranscriptStore] = None


def get_transcript_store() -> TranscriptStore:
    global _store
    if _store is None:
        _store = TranscriptStore()
    return _store
