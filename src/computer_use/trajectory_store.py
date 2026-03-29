"""Trajectory capture for browser-first computer use."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from src.config.settings import get_settings


class ComputerTrajectoryStore:
    """Persist computer-use attempts for later review and repair analysis."""

    def __init__(self, db_path: str = "data/computer_trajectories.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS computer_trajectories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    final_surface TEXT,
                    attempts_json TEXT NOT NULL,
                    verification_json TEXT,
                    request_json TEXT,
                    observation_json TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_computer_trajectories_created_at
                ON computer_trajectories(created_at DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_computer_trajectories_status
                ON computer_trajectories(status)
                """
            )
            conn.commit()

    def record(
        self,
        *,
        action: str,
        status: str,
        final_surface: Optional[str],
        attempts: list[dict[str, Any]],
        verification: dict[str, Any] | None,
        request: dict[str, Any],
        observation: dict[str, Any],
    ) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO computer_trajectories (
                    action,
                    status,
                    final_surface,
                    attempts_json,
                    verification_json,
                    request_json,
                    observation_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action,
                    status,
                    final_surface,
                    json.dumps(attempts, ensure_ascii=True),
                    json.dumps(verification, ensure_ascii=True) if verification is not None else None,
                    json.dumps(request, ensure_ascii=True),
                    json.dumps(observation, ensure_ascii=True),
                    time.time(),
                ),
            )
            trajectory_id = cursor.lastrowid
            conn.commit()
        return int(trajectory_id)

    def recent(self, *, status: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            if status:
                cursor.execute(
                    """
                    SELECT id, action, status, final_surface, attempts_json, verification_json,
                           request_json, observation_json, created_at
                    FROM computer_trajectories
                    WHERE status = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (status, limit),
                )
            else:
                cursor.execute(
                    """
                    SELECT id, action, status, final_surface, attempts_json, verification_json,
                           request_json, observation_json, created_at
                    FROM computer_trajectories
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            rows = cursor.fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    "id": row[0],
                    "action": row[1],
                    "status": row[2],
                    "final_surface": row[3],
                    "attempts": json.loads(row[4]),
                    "verification": json.loads(row[5]) if row[5] else None,
                    "request": json.loads(row[6]) if row[6] else {},
                    "observation": json.loads(row[7]) if row[7] else {},
                    "created_at": row[8],
                }
            )
        return result


_trajectory_store: ComputerTrajectoryStore | None = None


def get_computer_trajectory_store() -> ComputerTrajectoryStore:
    global _trajectory_store
    if _trajectory_store is None:
        settings = get_settings()
        _trajectory_store = ComputerTrajectoryStore(
            db_path=str(settings.computer_trajectory_db_path),
        )
    return _trajectory_store


def reset_computer_trajectory_store() -> None:
    global _trajectory_store
    _trajectory_store = None
