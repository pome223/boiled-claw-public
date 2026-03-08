"""
ファイル操作ツール
"""

from pathlib import Path
from typing import Optional

from google.adk.agents.context import Context as ToolContext

from src.security.policy import get_security_policy
from src.security.tool_policy import get_tool_policy_engine
from src.tools.context import resolve_tool_context


async def _check_write_policy(
    path: str,
    content: str,
    tool_context: Optional[ToolContext],
) -> Optional[str]:
    if tool_context is None:
        return None

    ctx = resolve_tool_context(tool_context)
    engine = get_tool_policy_engine()
    action, reason = engine.evaluate(ctx["agent_name"], "write_file")
    if action == "allow":
        return None
    if action == "deny":
        return f"Tool blocked by policy: {reason}"

    approved, response_reason = await engine.request_approval(
        tool_name="write_file",
        agent_name=ctx["agent_name"],
        args={"path": str(Path(path).expanduser()), "size": len(content)},
        session_id=ctx["session_id"],
        reason=reason,
    )
    if approved:
        return None
    detail = response_reason or reason or "user rejected"
    return f"Tool approval denied: {detail}"


async def read_file(path: str) -> dict:
    """
    ファイルを読み込む

    Args:
        path: 読み込むファイルのパス

    Returns:
        ファイルの内容
    """
    policy = get_security_policy()
    allowed, reason = policy.is_path_allowed(path, "read")
    if not allowed:
        return {"error": f"Access denied: {reason}"}

    try:
        file_path = Path(path).expanduser().resolve()
        content = file_path.read_text(encoding="utf-8")
        return {
            "path": str(file_path),
            "content": content,
            "size": len(content),
        }
    except FileNotFoundError:
        return {"error": f"File not found: {path}"}
    except PermissionError:
        return {"error": f"Permission denied: {path}"}
    except Exception as e:
        return {"error": str(e)}


async def write_file(
    path: str,
    content: str,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """
    ファイルに書き込む

    Args:
        path: 書き込むファイルのパス
        content: 書き込む内容

    Returns:
        書き込み結果
    """
    policy = get_security_policy()
    allowed, reason = policy.is_path_allowed(path, "write")
    if not allowed:
        return {"error": f"Access denied: {reason}"}
    content_allowed, content_reason = policy.validate_file_content(content, path)
    if not content_allowed:
        return {"error": f"Content blocked by security policy: {content_reason}"}

    approval_error = await _check_write_policy(path, content, tool_context)
    if approval_error:
        return {"error": approval_error}

    try:
        file_path = Path(path).expanduser().resolve()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return {
            "path": str(file_path),
            "size": len(content),
            "success": True,
        }
    except PermissionError:
        return {"error": f"Permission denied: {path}"}
    except Exception as e:
        return {"error": str(e)}
