"""
Typed WebSocket event definitions.

Server → Client:
  connected       : 接続確立
  chat.token      : ストリーミングトークン（将来拡張用）
  chat.done       : エージェント応答完了 / abort 確認
  system.event    : subagent / cron 通知
  health.tick     : ハートビート (30s)
  cron.update     : cron ジョブ状態変化

Client → Server:
  chat.send       : メッセージ送信
  chat.abort      : 実行中エージェントキャンセル
  presence.ping   : キープアライブ
"""

import time
from typing import Any, Optional


def ev_connected(session_id: str, user_id: str) -> dict:
    return {"event": "connected", "session_id": session_id, "user_id": user_id}


def ev_chat_token(text: str, request_id: Optional[str] = None) -> dict:
    d: dict[str, Any] = {"event": "chat.token", "text": text}
    if request_id:
        d["request_id"] = request_id
    return d


def ev_chat_done(
    text: str,
    request_id: Optional[str] = None,
    aborted: bool = False,
) -> dict:
    d: dict[str, Any] = {"event": "chat.done", "text": text, "aborted": aborted}
    if request_id:
        d["request_id"] = request_id
    return d


def ev_system_event(
    source: str,
    status: str,
    message: str,
    run_id: Optional[str] = None,
    agent_name: Optional[str] = None,
) -> dict:
    d: dict[str, Any] = {
        "event": "system.event",
        "source": source,
        "status": status,
        "message": message,
        "ts": time.time(),
    }
    if run_id:
        d["run_id"] = run_id
    if agent_name:
        d["agent_name"] = agent_name
    return d


def ev_health_tick(active_sessions: int) -> dict:
    return {"event": "health.tick", "ts": time.time(), "active_sessions": active_sessions}


def ev_cron_update(job_id: str, status: str, message: str = "") -> dict:
    return {
        "event": "cron.update",
        "job_id": job_id,
        "status": status,
        "message": message,
        "ts": time.time(),
    }
