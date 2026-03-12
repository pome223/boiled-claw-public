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
import shlex
import subprocess
from typing import Optional

from src.bridges.host_bridge_schema import (
    BridgePingResult,
    CapabilityDescriptor,
    CapabilityListResult,
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
        ]
    )


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
