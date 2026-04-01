"""
監査ログシステム
OpenClaw のセキュリティ監査機能を参考
"""

from datetime import datetime
from enum import Enum
import json
from pathlib import Path
import time
from typing import Any, Dict, Optional


class AuditEventType(Enum):
    """監査イベントタイプ"""
    SHELL_COMMAND = "shell_command"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    WEB_SEARCH = "web_search"
    BROWSER_NAVIGATE = "browser_navigate"
    DESKTOP_VIEW = "desktop_view"
    DESKTOP_CONTROL = "desktop_control"
    PHYSICAL_AI = "physical_ai"
    MEMORY_STORE = "memory_store"
    MEMORY_SEARCH = "memory_search"
    AGENT_MESSAGE = "agent_message"
    CHANNEL_MESSAGE = "channel_message"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    TOOL_APPROVAL = "tool_approval"
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

    @staticmethod
    def _search_text(entry: Dict[str, Any]) -> str:
        metadata = entry.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        parts = [
            entry.get("event_type"),
            entry.get("user_id"),
            entry.get("session_id"),
            entry.get("action"),
            entry.get("resource"),
            entry.get("result"),
            metadata.get("tool_name"),
            metadata.get("tool_pattern"),
            metadata.get("source"),
            metadata.get("actor_user_id"),
        ]
        if metadata:
            parts.append(json.dumps(metadata, ensure_ascii=False, sort_keys=True))
        return " ".join(str(part).strip() for part in parts if part).lower()

    @staticmethod
    def _tool_text(entry: Dict[str, Any]) -> str:
        metadata = entry.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        parts = [
            metadata.get("tool_name"),
            metadata.get("tool_pattern"),
            entry.get("action"),
            entry.get("resource"),
        ]
        return " ".join(str(part).strip() for part in parts if part).lower()

    @staticmethod
    def _entry_matches(
        entry: Dict[str, Any],
        *,
        actor_user_id: str,
        session_id: str,
        tool: str,
        source: str,
        result: str,
        q: str,
    ) -> bool:
        metadata = entry.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}

        if actor_user_id:
            actor_candidates = {
                str(entry.get("user_id") or "").strip().lower(),
                str(metadata.get("actor_user_id") or "").strip().lower(),
            }
            if actor_user_id not in actor_candidates:
                return False

        if session_id:
            session_candidates = {
                str(entry.get("session_id") or "").strip().lower(),
                str(metadata.get("target_session_id") or "").strip().lower(),
            }
            if session_id not in session_candidates:
                return False

        if tool and tool not in AuditLogger._tool_text(entry):
            return False

        if source and source != str(metadata.get("source") or "").strip().lower():
            return False

        if result and result not in str(entry.get("result") or "").strip().lower():
            return False

        if q and q not in AuditLogger._search_text(entry):
            return False

        return True

    def query_logs(
        self,
        *,
        actor_user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        tool: Optional[str] = None,
        source: Optional[str] = None,
        result: Optional[str] = None,
        q: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """Query audit logs with lightweight filtering and pagination."""
        resolved_page = max(1, int(page or 1))
        resolved_page_size = max(1, min(int(page_size or 20), 100))
        normalized_actor = str(actor_user_id or "").strip().lower()
        normalized_session = str(session_id or "").strip().lower()
        normalized_tool = str(tool or "").strip().lower()
        normalized_source = str(source or "").strip().lower()
        normalized_result = str(result or "").strip().lower()
        normalized_query = str(q or "").strip().lower()

        if not self.log_path.exists():
            return {
                "entries": [],
                "pagination": {
                    "page": resolved_page,
                    "page_size": resolved_page_size,
                    "total": 0,
                    "has_more": False,
                },
                "filters": {
                    "actor_user_id": normalized_actor,
                    "session_id": normalized_session,
                    "tool": normalized_tool,
                    "source": normalized_source,
                    "result": normalized_result,
                    "q": normalized_query,
                },
            }

        with open(self.log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        start = (resolved_page - 1) * resolved_page_size
        collected: list[dict[str, Any]] = []
        total = 0
        total_lines = len(lines)
        for reverse_index, line in enumerate(reversed(lines), start=1):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if not self._entry_matches(
                entry,
                actor_user_id=normalized_actor,
                session_id=normalized_session,
                tool=normalized_tool,
                source=normalized_source,
                result=normalized_result,
                q=normalized_query,
            ):
                continue
            total += 1
            if total <= start:
                continue
            if len(collected) >= resolved_page_size:
                continue
            entry["entry_id"] = f"audit-{total_lines - reverse_index + 1}"
            collected.append(entry)

        return {
            "entries": collected,
            "pagination": {
                "page": resolved_page,
                "page_size": resolved_page_size,
                "total": total,
                "has_more": start + len(collected) < total,
            },
            "filters": {
                "actor_user_id": normalized_actor,
                "session_id": normalized_session,
                "tool": normalized_tool,
                "source": normalized_source,
                "result": normalized_result,
                "q": normalized_query,
            },
        }


# グローバルインスタンス
_audit_logger: Optional[AuditLogger] = None


def get_audit_logger() -> AuditLogger:
    """監査ロガーインスタンスを取得"""
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = AuditLogger()
    return _audit_logger
