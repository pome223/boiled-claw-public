"""Persistent validation-run storage for simulation-first physical AI flows."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from src.config.settings import get_settings


class PhysicalAIValidationStore:
    """Persist validation runs so dispatch decisions survive process restarts."""

    def __init__(self, db_path: str = "data/physical_ai_validation.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS physical_ai_validation_runs (
                    run_id TEXT PRIMARY KEY,
                    adapter TEXT NOT NULL,
                    status TEXT NOT NULL,
                    validated INTEGER NOT NULL,
                    workflow TEXT,
                    scenario TEXT,
                    robot TEXT,
                    task TEXT,
                    response_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_physical_ai_validation_runs_updated_at
                ON physical_ai_validation_runs(updated_at DESC)
                """
            )
            conn.commit()

    def upsert(self, run: dict[str, Any]) -> None:
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO physical_ai_validation_runs (
                    run_id,
                    adapter,
                    status,
                    validated,
                    workflow,
                    scenario,
                    robot,
                    task,
                    response_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    adapter = excluded.adapter,
                    status = excluded.status,
                    validated = excluded.validated,
                    workflow = excluded.workflow,
                    scenario = excluded.scenario,
                    robot = excluded.robot,
                    task = excluded.task,
                    response_json = excluded.response_json,
                    updated_at = excluded.updated_at
                """,
                (
                    run["run_id"],
                    run["adapter"],
                    run["status"],
                    1 if run.get("validated") else 0,
                    run.get("workflow"),
                    run.get("scenario"),
                    run.get("robot"),
                    run.get("task"),
                    json.dumps(run.get("response") or {}, ensure_ascii=True),
                    run.get("created_at", now),
                    now,
                ),
            )
            conn.commit()

    def get(self, run_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT run_id, adapter, status, validated, workflow, scenario, robot, task,
                       response_json, created_at, updated_at
                FROM physical_ai_validation_runs
                WHERE run_id = ?
                """,
                (run_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {
            "run_id": row[0],
            "adapter": row[1],
            "status": row[2],
            "validated": bool(row[3]),
            "workflow": row[4],
            "scenario": row[5],
            "robot": row[6],
            "task": row[7],
            "response": json.loads(row[8]) if row[8] else {},
            "created_at": row[9],
            "updated_at": row[10],
        }

    def clear(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM physical_ai_validation_runs")
            conn.commit()


_validation_store: PhysicalAIValidationStore | None = None


def get_physical_ai_validation_store() -> PhysicalAIValidationStore:
    global _validation_store
    if _validation_store is None:
        settings = get_settings()
        db_path = getattr(
            settings,
            "physical_ai_validation_db_path",
            Path("data/physical_ai_validation.db"),
        )
        if not db_path:
            db_path = Path("data/physical_ai_validation.db")
        _validation_store = PhysicalAIValidationStore(db_path=str(db_path))
    return _validation_store


def reset_physical_ai_validation_store() -> None:
    global _validation_store
    _validation_store = None
