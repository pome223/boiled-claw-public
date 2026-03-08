"""
Cron job scheduler with SQLite persistence.

croniter で cron 式を解析し、asyncio ループで30秒ごとに期限切れジョブを実行する。
実行は SubagentManager.spawn() に委譲するため、isolated run として動作する。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

try:
    from croniter import croniter as _croniter
    _CRONITER_OK = True
except ImportError:
    _CRONITER_OK = False


_DB_PATH = Path("data/cron.db")
_CHECK_INTERVAL = 30  # seconds


@dataclass
class CronJob:
    id: str
    name: str
    cron_expr: str
    task: str
    agent_id: str
    enabled: bool
    created_at: float
    last_run: Optional[float]
    next_run: Optional[float]
    run_count: int
    last_result: Optional[str]
    last_error: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "cron_expr": self.cron_expr,
            "task": self.task,
            "agent_id": self.agent_id,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_run": self.last_run,
            "next_run": self.next_run,
            "run_count": self.run_count,
            "last_result": self.last_result,
            "last_error": self.last_error,
        }


class CronScheduler:
    """SQLite バックエンドの cron ジョブスケジューラ。"""

    def __init__(self, db_path: Path = _DB_PATH):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._loop_task: Optional[asyncio.Task] = None
        self._notifier: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None
        self._spawn_fn: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None
        self._init_db()

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS cron_jobs (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                cron_expr   TEXT NOT NULL,
                task        TEXT NOT NULL,
                agent_id    TEXT NOT NULL DEFAULT 'web_researcher',
                enabled     INTEGER NOT NULL DEFAULT 1,
                created_at  REAL NOT NULL,
                last_run    REAL,
                next_run    REAL,
                run_count   INTEGER NOT NULL DEFAULT 0,
                last_result TEXT,
                last_error  TEXT
            )
        """)
        self._conn.commit()

    def set_notifier(self, fn: Optional[Callable[[Dict[str, Any]], Awaitable[None]]]) -> None:
        self._notifier = fn

    def set_spawn_fn(self, fn: Callable[..., Awaitable[Dict[str, Any]]]) -> None:
        """SubagentManager.spawn を渡す。"""
        self._spawn_fn = fn

    # ------------------------------------------------------------------
    # job CRUD
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_cron(expr: str) -> None:
        if not _CRONITER_OK:
            raise ValueError("croniter is not installed. Run: pip install croniter")
        try:
            _croniter(expr)
        except Exception as e:
            raise ValueError(f"Invalid cron expression '{expr}': {e}") from e

    @staticmethod
    def _next_ts(expr: str, base: Optional[float] = None) -> Optional[float]:
        if not _CRONITER_OK:
            return None
        try:
            return _croniter(expr, base or time.time()).get_next(float)
        except Exception:
            return None

    def add_job(
        self,
        name: str,
        cron_expr: str,
        task: str,
        agent_id: str = "web_researcher",
    ) -> CronJob:
        if not name.strip():
            raise ValueError("name is required")
        if not task.strip():
            raise ValueError("task is required")
        self._validate_cron(cron_expr)

        job_id = str(uuid.uuid4())
        now = time.time()
        next_run = self._next_ts(cron_expr, now)

        self._conn.execute(
            """INSERT INTO cron_jobs (id, name, cron_expr, task, agent_id, created_at, next_run)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (job_id, name.strip(), cron_expr, task.strip(), agent_id, now, next_run),
        )
        self._conn.commit()
        return self._fetch(job_id)  # type: ignore[return-value]

    def list_jobs(self) -> List[CronJob]:
        rows = self._conn.execute(
            "SELECT * FROM cron_jobs ORDER BY created_at DESC"
        ).fetchall()
        return [self._row(r) for r in rows]

    def get_job(self, job_id: str) -> Optional[CronJob]:
        return self._fetch(job_id)

    def delete_job(self, job_id: str) -> bool:
        cur = self._conn.execute("DELETE FROM cron_jobs WHERE id = ?", (job_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def toggle_job(self, job_id: str, enabled: bool) -> Optional[CronJob]:
        self._conn.execute(
            "UPDATE cron_jobs SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, job_id),
        )
        self._conn.commit()
        return self._fetch(job_id)

    def _fetch(self, job_id: str) -> Optional[CronJob]:
        row = self._conn.execute(
            "SELECT * FROM cron_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return self._row(row) if row else None

    @staticmethod
    def _row(r: tuple) -> CronJob:
        return CronJob(
            id=r[0], name=r[1], cron_expr=r[2], task=r[3],
            agent_id=r[4], enabled=bool(r[5]), created_at=r[6],
            last_run=r[7], next_run=r[8], run_count=r[9],
            last_result=r[10], last_error=r[11],
        )

    # ------------------------------------------------------------------
    # scheduler loop
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._loop_task = asyncio.create_task(self._loop(), name="cron-scheduler")

    async def shutdown(self) -> None:
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(_CHECK_INTERVAL)

    async def _tick(self) -> None:
        now = time.time()
        rows = self._conn.execute(
            "SELECT * FROM cron_jobs WHERE enabled = 1 AND next_run IS NOT NULL AND next_run <= ?",
            (now,),
        ).fetchall()
        for row in rows:
            asyncio.create_task(self._run_job(self._row(row)), name=f"cron:{row[0]}")

    async def _run_job(self, job: CronJob) -> None:
        # 次回実行時刻を先に更新してから実行（重複起動防止）
        next_run = self._next_ts(job.cron_expr)
        self._conn.execute(
            """UPDATE cron_jobs
               SET last_run = ?, next_run = ?, run_count = run_count + 1
               WHERE id = ?""",
            (time.time(), next_run, job.id),
        )
        self._conn.commit()

        await self._notify(job.id, "running", f"[cron:{job.name}] started")

        if self._spawn_fn is None:
            return

        try:
            result = await self._spawn_fn(
                task=job.task,
                agent_name=job.agent_id,
                requester_session_id=f"cron_{job.id}",
                user_id="cron",
                app_name="boiled-claw",
                mode="run",
            )
            snippet = json.dumps(result, ensure_ascii=False)[:200]
            self._conn.execute(
                "UPDATE cron_jobs SET last_result = ?, last_error = NULL WHERE id = ?",
                (snippet, job.id),
            )
            self._conn.commit()
            run_id = result.get("run_id", "?")
            await self._notify(job.id, "accepted", f"[cron:{job.name}] spawned run_id={run_id}")
        except Exception as exc:
            self._conn.execute(
                "UPDATE cron_jobs SET last_error = ? WHERE id = ?",
                (str(exc), job.id),
            )
            self._conn.commit()
            await self._notify(job.id, "failed", f"[cron:{job.name}] error: {exc}")

    async def _notify(self, job_id: str, status: str, message: str) -> None:
        if self._notifier is None:
            return
        try:
            await self._notifier({"job_id": job_id, "status": status, "message": message})
        except Exception:
            pass


# グローバルシングルトン
_scheduler: Optional[CronScheduler] = None


def get_scheduler() -> CronScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = CronScheduler()
    return _scheduler
