"""
監査ログシステム
OpenClaw のセキュリティ監査機能を参考
"""

import json
import time
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime
from enum import Enum


class AuditEventType(Enum):
    """監査イベントタイプ"""
    SHELL_COMMAND = "shell_command"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    WEB_SEARCH = "web_search"
    BROWSER_NAVIGATE = "browser_navigate"
    MEMORY_STORE = "memory_store"
    MEMORY_SEARCH = "memory_search"
    AGENT_MESSAGE = "agent_message"
    CHANNEL_MESSAGE = "channel_message"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    ERROR = "error"


class AuditLogger:
    """監査ログ記録"""

    def __init__(self, log_path: str = "data/audit.log"):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        event_type: AuditEventType,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        action: Optional[str] = None,
        resource: Optional[str] = None,
        result: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """監査ログを記録"""
        timestamp = time.time()
        log_entry = {
            "timestamp": timestamp,
            "datetime": datetime.fromtimestamp(timestamp).isoformat(),
            "event_type": event_type.value,
            "user_id": user_id,
            "session_id": session_id,
            "action": action,
            "resource": resource,
            "result": result,
            "metadata": metadata or {},
        }

        # JSON Lines形式で追記
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    def log_shell_command(
        self,
        command: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        result: Optional[str] = None,
        return_code: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """シェルコマンド実行ログ"""
        self.log(
            event_type=AuditEventType.SHELL_COMMAND,
            user_id=user_id,
            session_id=session_id,
            action="execute",
            resource=command,
            result=result or "success" if return_code == 0 else "failed",
            metadata={"command": command, "return_code": return_code, **(metadata or {})},
        )

    def log_file_operation(
        self,
        operation: str,
        file_path: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        result: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """ファイル操作ログ"""
        event_type = (
            AuditEventType.FILE_READ if operation == "read" else AuditEventType.FILE_WRITE
        )
        self.log(
            event_type=event_type,
            user_id=user_id,
            session_id=session_id,
            action=operation,
            resource=file_path,
            result=result or "success",
            metadata={"file_path": file_path, "operation": operation, **(metadata or {})},
        )

    def log_agent_message(
        self,
        agent_name: str,
        message: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        role: str = "assistant",
    ):
        """エージェントメッセージログ"""
        self.log(
            event_type=AuditEventType.AGENT_MESSAGE,
            user_id=user_id,
            session_id=session_id,
            action="message",
            resource=agent_name,
            result="sent",
            metadata={"agent": agent_name, "role": role, "message_preview": message[:100]},
        )

    def log_error(
        self,
        error: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ):
        """エラーログ"""
        self.log(
            event_type=AuditEventType.ERROR,
            user_id=user_id,
            session_id=session_id,
            action="error",
            resource=None,
            result="error",
            metadata={"error": error, "context": context or {}},
        )

    def get_recent_logs(self, limit: int = 100) -> list:
        """最近のログを取得"""
        if not self.log_path.exists():
            return []

        logs = []
        with open(self.log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            for line in lines[-limit:]:
                try:
                    logs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        return logs


# グローバルインスタンス
_audit_logger: Optional[AuditLogger] = None


def get_audit_logger() -> AuditLogger:
    """監査ロガーインスタンスを取得"""
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = AuditLogger()
    return _audit_logger
