"""
Host Bridge MCP server.

Host OS capability surface for boiled-claw.

v1 tools:
  - ping
  - capabilities.list
  - host.shell.run

起動方法:
  python -m src.mcp_servers.host_bridge_server
  python -m src.mcp_servers.host_bridge_server --sse --host 127.0.0.1 --port 8766
"""

import argparse
from pathlib import Path
import shlex
import subprocess
from typing import Optional

from src.bridges.host_bridge_schema import (
    BridgePingResult,
    CapabilityDescriptor,
    CapabilityListResult,
    HostFileListRequest,
    HostFileListResult,
    HostFileReadRequest,
    HostFileReadResult,
    HostFileWriteRequest,
    HostFileWriteResult,
    HostShellRunRequest,
    HostShellRunResult,
)
from src.security.policy import get_security_policy


_BLOCKED_EXECUTABLES = {
    "rm", "shred", "mkfs", "fdisk", "dd", "wipefs",
    "truncate", "srm", "secure-delete",
}


def _run_host_shell(request: HostShellRunRequest) -> HostShellRunResult:
    normalized = " ".join(request.command.split())

    policy = get_security_policy()
    allowed, reason = policy.is_command_allowed(normalized)
    if not allowed:
        return HostShellRunResult(
            ok=False,
            error=f"Command blocked by security policy: {reason}",
            return_code=-1,
        )

    try:
        tokens = shlex.split(normalized)
    except ValueError as exc:
        return HostShellRunResult(
            ok=False,
            error=f"Invalid command syntax: {exc}",
            return_code=-1,
        )

    if not tokens:
        return HostShellRunResult(
            ok=False,
            error="Empty command",
            return_code=-1,
        )

    executable = tokens[0].lstrip("./").split("/")[-1]
    if executable in _BLOCKED_EXECUTABLES:
        return HostShellRunResult(
            ok=False,
            error=f"Executable '{executable}' is blocked for safety.",
            return_code=-1,
        )

    try:
        completed = subprocess.run(
            tokens,
            cwd=request.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=request.timeout_seconds,
            check=False,
        )
        return HostShellRunResult(
            ok=completed.returncode == 0,
            stdout=completed.stdout.decode("utf-8", errors="replace"),
            stderr=completed.stderr.decode("utf-8", errors="replace"),
            return_code=completed.returncode,
        )
    except subprocess.TimeoutExpired:
        return HostShellRunResult(
            ok=False,
            error=f"Command timed out after {request.timeout_seconds} seconds",
            return_code=-1,
            timed_out=True,
        )
    except FileNotFoundError:
        return HostShellRunResult(
            ok=False,
            error=f"Command not found: {tokens[0]}",
            return_code=-1,
        )
    except Exception as exc:
        return HostShellRunResult(
            ok=False,
            error=str(exc),
            return_code=-1,
        )


def _capabilities() -> CapabilityListResult:
    return CapabilityListResult(
        capabilities=[
            CapabilityDescriptor(
                name="host.shell.run",
                risk="medium",
                requires_approval=True,
                description="Run a guarded shell command on the host OS.",
            ),
            CapabilityDescriptor(
                name="host.file.read",
                risk="low",
                requires_approval=False,
                description="Read a guarded file from the host OS.",
            ),
            CapabilityDescriptor(
                name="host.file.write",
                risk="medium",
                requires_approval=True,
                description="Write a guarded file on the host OS.",
            ),
            CapabilityDescriptor(
                name="host.file.list",
                risk="low",
                requires_approval=False,
                description="List a guarded directory on the host OS.",
            ),
        ]
    )


def _read_host_file(request: HostFileReadRequest) -> HostFileReadResult:
    policy = get_security_policy()
    allowed, reason = policy.is_path_allowed(request.path, "read")
    if not allowed:
        return HostFileReadResult(ok=False, error=f"Access denied: {reason}")

    try:
        file_path = Path(request.path).expanduser().resolve()
        content = file_path.read_text(encoding="utf-8")
        return HostFileReadResult(
            ok=True,
            path=str(file_path),
            content=content,
            size=len(content),
        )
    except FileNotFoundError:
        return HostFileReadResult(ok=False, error=f"File not found: {request.path}")
    except PermissionError:
        return HostFileReadResult(ok=False, error=f"Permission denied: {request.path}")
    except Exception as exc:
        return HostFileReadResult(ok=False, error=str(exc))


def _write_host_file(request: HostFileWriteRequest) -> HostFileWriteResult:
    policy = get_security_policy()
    allowed, reason = policy.is_path_allowed(request.path, "write")
    if not allowed:
        return HostFileWriteResult(ok=False, error=f"Access denied: {reason}")

    content_allowed, content_reason = policy.validate_file_content(request.content, request.path)
    if not content_allowed:
        return HostFileWriteResult(
            ok=False,
            error=f"Content blocked by security policy: {content_reason}",
        )

    try:
        file_path = Path(request.path).expanduser().resolve()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(request.content, encoding="utf-8")
        return HostFileWriteResult(
            ok=True,
            path=str(file_path),
            size=len(request.content),
            success=True,
        )
    except PermissionError:
        return HostFileWriteResult(ok=False, error=f"Permission denied: {request.path}")
    except Exception as exc:
        return HostFileWriteResult(ok=False, error=str(exc))


def _list_host_files(request: HostFileListRequest) -> HostFileListResult:
    policy = get_security_policy()
    allowed, reason = policy.is_path_allowed(request.path, "read")
    if not allowed:
        return HostFileListResult(ok=False, error=f"Access denied: {reason}")

    try:
        dir_path = Path(request.path).expanduser().resolve()
        entries = []
        for entry in sorted(dir_path.iterdir(), key=lambda item: item.name.lower()):
            try:
                stat = entry.stat()
                size = stat.st_size if entry.is_file() else 0
            except OSError:
                size = 0
            entries.append({
                "name": entry.name,
                "path": str(entry),
                "is_dir": entry.is_dir(),
                "size": size,
            })

        return HostFileListResult(ok=True, path=str(dir_path), entries=entries)
    except FileNotFoundError:
        return HostFileListResult(ok=False, error=f"File not found: {request.path}")
    except NotADirectoryError:
        return HostFileListResult(ok=False, error=f"Not a directory: {request.path}")
    except PermissionError:
        return HostFileListResult(ok=False, error=f"Permission denied: {request.path}")
    except Exception as exc:
        return HostFileListResult(ok=False, error=str(exc))


def create_server(host: str = "127.0.0.1", port: int = 8766):
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.server import TransportSecuritySettings

    mcp = FastMCP("host-bridge")
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )

    transport_hint = "sse" if host != "stdio" else "stdio"

    @mcp.tool(name="ping", description="Host Bridge health probe.")
    def ping() -> dict:
        return BridgePingResult(transport=transport_hint).model_dump()

    @mcp.tool(name="capabilities.list", description="List implemented host capabilities.")
    def list_capabilities() -> dict:
        return _capabilities().model_dump()

    @mcp.tool(name="host.shell.run", description="Run a guarded shell command on the host.")
    def host_shell_run(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        command: str,
        timeout_seconds: int = 30,
        cwd: Optional[str] = None,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = HostShellRunRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            command=command,
            timeout_seconds=timeout_seconds,
            cwd=cwd,
        )
        return _run_host_shell(request).model_dump()

    @mcp.tool(name="host.file.read", description="Read a guarded file on the host.")
    def host_file_read(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        path: str,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = HostFileReadRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            path=path,
        )
        return _read_host_file(request).model_dump()

    @mcp.tool(name="host.file.write", description="Write a guarded file on the host.")
    def host_file_write(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        path: str,
        content: str,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = HostFileWriteRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            path=path,
            content=content,
        )
        return _write_host_file(request).model_dump()

    @mcp.tool(name="host.file.list", description="List a guarded directory on the host.")
    def host_file_list(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        path: str,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = HostFileListRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            path=path,
        )
        return _list_host_files(request).model_dump()

    return mcp


def main():
    parser = argparse.ArgumentParser(description="Host Bridge MCP Server")
    parser.add_argument("--sse", action="store_true", help="Run in SSE mode")
    parser.add_argument("--port", type=int, default=8766, help="SSE port")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="SSE host")
    args = parser.parse_args()

    if args.sse:
        mcp = create_server(host=args.host, port=args.port)
        print(f"SSE mode: http://{args.host}:{args.port}/sse")
        mcp.run(transport="sse")
    else:
        mcp = create_server(host="stdio")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
