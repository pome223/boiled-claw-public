"""
シェルコマンド実行ツール
"""

import asyncio
import shlex
import uuid
from typing import Any, Optional

from google.adk.agents.context import Context as ToolContext

from src.bridges.host_bridge_client import HostBridgeError, get_host_bridge_client
from src.bridges.host_bridge_schema import HostShellRunRequest, HostShellRunResult
from src.config.settings import get_settings
from src.runtime.tool_events import emit_tool_result, emit_tool_start
from src.security.audit import get_audit_logger
from src.security.policy import get_security_policy
from src.security.tool_policy import get_tool_policy_engine
from src.tools.context import resolve_tool_context


async def _check_tool_policy(
    tool_name: str,
    args: dict[str, Any],
    tool_context: Optional[ToolContext],
) -> tuple[Optional[str], Optional[str]]:
    if tool_context is None:
        return None, None

    ctx = resolve_tool_context(tool_context)
    engine = get_tool_policy_engine()
    action, reason = engine.evaluate(ctx["agent_name"], tool_name)
    if action == "allow":
        return None, None
    if action == "deny":
        return f"Tool blocked by policy: {reason}", None

    approved, response_reason, approval_token = await engine.request_approval_with_id(
        tool_name=tool_name,
        agent_name=ctx["agent_name"],
        args=args,
        session_id=ctx["session_id"],
        reason=reason,
    )
    if approved:
        return None, approval_token
    detail = response_reason or reason or "user rejected"
    return f"Tool approval denied: {detail}", approval_token


async def run_shell(
    command: str,
    timeout: int = 30,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """
    シェルコマンドを安全に実行する。
    パイプ・リダイレクトは非対応（シェルインジェクション防止のため subprocess_exec を使用）。

    Args:
        command: 実行するコマンド（単一コマンド + 引数）
        timeout: タイムアウト秒数（デフォルト30秒）

    Returns:
        stdout, stderr, return_code を含む辞書
    """
    # ホワイトスペースを正規化してからポリシーチェック（空白2つ等の回避を防ぐ）
    normalized = " ".join(command.split())
    audit_logger = get_audit_logger()
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    audit_metadata: dict[str, Any] = {}

    def _audit(result: str, return_code: int | None = None) -> None:
        audit_logger.log_shell_command(
            normalized,
            user_id=ctx.get("user_id") or None,
            session_id=ctx.get("session_id") or None,
            result=result,
            return_code=return_code,
            metadata=audit_metadata,
        )

    policy = get_security_policy()
    allowed, reason = policy.is_command_allowed(normalized)
    if not allowed:
        _audit(f"blocked:{reason}", -1)
        return {
            "error": f"Command blocked by security policy: {reason}",
            "stdout": "",
            "stderr": "",
            "return_code": -1,
        }

    approval_error, approval_token = await _check_tool_policy(
        "run_shell",
        {"command": normalized, "timeout": timeout},
        tool_context,
    )
    if approval_error:
        _audit(approval_error, -1)
        return {
            "error": approval_error,
            "stdout": "",
            "stderr": "",
            "return_code": -1,
        }

    settings = get_settings()
    if settings.host_bridge_enabled:
        request = HostShellRunRequest(
            request_id=f"host-shell-{uuid.uuid4().hex[:12]}",
            session_id=ctx.get("session_id") or "standalone-session",
            user_id=ctx.get("user_id") or "standalone-user",
            agent_name=ctx.get("agent_name") or "unknown_agent",
            approval_token=approval_token,
            command=normalized,
            timeout_seconds=timeout,
            cwd=None,
        )
        audit_metadata = {
            "executor": "host_bridge",
            "request_id": request.request_id,
            "approval_token": approval_token,
        }
        try:
            client = get_host_bridge_client()
            if client is None:
                raise HostBridgeError("Host Bridge is not enabled")
            await emit_tool_start(
                session_id=request.session_id,
                tool_name="host.shell.run",
                agent_name=request.agent_name,
                args={
                    "command": request.command,
                    "timeout_seconds": request.timeout_seconds,
                },
                request_id=request.request_id,
                metadata={"executor": "host_bridge"},
            )
            result = await client.run_shell(request)
            await emit_tool_result(
                session_id=request.session_id,
                tool_name="host.shell.run",
                agent_name=request.agent_name,
                ok=result.ok,
                result=result.model_dump(),
                request_id=request.request_id,
                metadata={"executor": "host_bridge"},
            )
            _audit(
                "bridge_success" if result.return_code == 0 else "bridge_failed",
                result.return_code,
            )
            return result.model_dump()
        except HostBridgeError as e:
            await emit_tool_result(
                session_id=request.session_id,
                tool_name="host.shell.run",
                agent_name=request.agent_name,
                ok=False,
                result={"error": str(e), "return_code": -1},
                request_id=request.request_id,
                metadata={"executor": "host_bridge"},
            )
            _audit(f"bridge_error:{e}", -1)
            return {
                "error": str(e),
                "stdout": "",
                "stderr": "",
                "return_code": -1,
            }

    # コマンドをトークンに分解
    try:
        tokens = shlex.split(normalized)
    except ValueError as e:
        _audit(f"invalid:{e}", -1)
        return {
            "error": f"Invalid command syntax: {e}",
            "stdout": "",
            "stderr": "",
            "return_code": -1,
        }

    if not tokens:
        _audit("empty", -1)
        return {"error": "Empty command", "stdout": "", "stderr": "", "return_code": -1}

    # 先頭トークン（実行ファイル名）による追加チェック
    executable = tokens[0].lstrip("./").split("/")[-1]
    BLOCKED_EXECUTABLES = {
        "rm", "shred", "mkfs", "fdisk", "dd", "wipefs",
        "truncate", "srm", "secure-delete",
    }
    if executable in BLOCKED_EXECUTABLES:
        _audit(f"blocked_executable:{executable}", -1)
        return {
            "error": f"Executable '{executable}' is blocked for safety.",
            "stdout": "",
            "stderr": "",
            "return_code": -1,
        }

    try:
        # shell=False 相当: シェルメタキャラクタ（; | && $() 等）をインジェクションに使えない
        process = await asyncio.create_subprocess_exec(
            *tokens,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout
        )

        _audit(
            "success" if process.returncode == 0 else "failed",
            process.returncode,
        )
        return {
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "return_code": process.returncode,
        }

    except asyncio.TimeoutError:
        _audit("timeout", -1)
        return {
            "error": f"Command timed out after {timeout} seconds",
            "stdout": "",
            "stderr": "",
            "return_code": -1,
        }
    except FileNotFoundError:
        _audit("not_found", -1)
        return {
            "error": f"Command not found: {tokens[0]}",
            "stdout": "",
            "stderr": "",
            "return_code": -1,
        }
    except Exception as e:
        _audit(f"error:{e}", -1)
        return {
            "error": str(e),
            "stdout": "",
            "stderr": "",
            "return_code": -1,
        }
