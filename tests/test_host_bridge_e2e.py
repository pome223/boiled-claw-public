import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

import src.config.settings as settings_module
import src.security.policy as policy_module
from src.bridges.host_bridge_client import get_host_bridge_client
from src.tools.file_manager import read_file, write_file
from src.tools.shell import run_shell


ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def host_bridge_server(tmp_path):
    port = _free_port()
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.setdefault("GOOGLE_API_KEY", "test-key")
    env["FILE_WORKSPACE_PATHS"] = str(workspace)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "src.mcp_servers.host_bridge_server",
            "--sse",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )

    url = f"http://127.0.0.1:{port}/sse"
    deadline = time.time() + 10
    last_error = ""
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            last_error = proc.stderr.read() if proc.stderr else ""
            break
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    ready = True
                    break
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.2)
    if not ready:
        if proc.poll() is not None and proc.stderr:
            last_error = proc.stderr.read()
        elif not last_error and proc.stderr:
            last_error = proc.stderr.read()
        proc.kill()
        raise AssertionError(f"Host Bridge failed to start: {last_error}")

    try:
        yield {"url": url, "workspace": workspace}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture
def configured_host_bridge_env(monkeypatch, host_bridge_server):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv("HOST_BRIDGE_ENABLED", "true")
    monkeypatch.setenv("HOST_BRIDGE_URL", host_bridge_server["url"])
    monkeypatch.setenv("FILE_WORKSPACE_PATHS", str(host_bridge_server["workspace"]))
    settings_module.reset_settings()
    policy_module._security_policy = None
    try:
        yield host_bridge_server
    finally:
        settings_module.reset_settings()
        policy_module._security_policy = None


@pytest.mark.asyncio
async def test_host_bridge_env_runs_shell_and_lists_capabilities(configured_host_bridge_env):
    client = get_host_bridge_client()
    assert client is not None

    ping = await client.ping()
    assert ping.ok is True
    assert ping.service == "host-bridge"

    capabilities = await client.list_capabilities()
    names = {cap.name for cap in capabilities.capabilities}
    assert "host.shell.run" in names
    assert "host.file.read" in names
    assert "host.browser.navigate" in names

    result = await run_shell("echo host-bridge-e2e")
    assert result["return_code"] == 0
    assert "host-bridge-e2e" in result["stdout"]


@pytest.mark.asyncio
async def test_host_bridge_env_reads_and_writes_files(configured_host_bridge_env):
    workspace = configured_host_bridge_env["workspace"]
    target = workspace / "bridge.txt"

    write_result = await write_file(str(target), "bridge file e2e")
    assert write_result["success"] is True
    assert target.read_text(encoding="utf-8") == "bridge file e2e"

    read_result = await read_file(str(target))
    assert read_result["content"] == "bridge file e2e"
    assert Path(read_result["path"]) == target.resolve()
