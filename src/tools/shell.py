"""
シェルコマンド実行ツール
"""

import asyncio
import shlex

from src.security.policy import get_security_policy


async def run_shell(command: str, timeout: int = 30) -> dict:
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

    policy = get_security_policy()
    allowed, reason = policy.is_command_allowed(normalized)
    if not allowed:
        return {
            "error": f"Command blocked by security policy: {reason}",
            "stdout": "",
            "stderr": "",
            "return_code": -1,
        }

    # コマンドをトークンに分解
    try:
        tokens = shlex.split(normalized)
    except ValueError as e:
        return {
            "error": f"Invalid command syntax: {e}",
            "stdout": "",
            "stderr": "",
            "return_code": -1,
        }

    if not tokens:
        return {"error": "Empty command", "stdout": "", "stderr": "", "return_code": -1}

    # 先頭トークン（実行ファイル名）による追加チェック
    executable = tokens[0].lstrip("./").split("/")[-1]
    BLOCKED_EXECUTABLES = {
        "rm", "shred", "mkfs", "fdisk", "dd", "wipefs",
        "truncate", "srm", "secure-delete",
    }
    if executable in BLOCKED_EXECUTABLES:
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

        return {
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "return_code": process.returncode,
        }

    except asyncio.TimeoutError:
        return {
            "error": f"Command timed out after {timeout} seconds",
            "stdout": "",
            "stderr": "",
            "return_code": -1,
        }
    except FileNotFoundError:
        return {
            "error": f"Command not found: {tokens[0]}",
            "stdout": "",
            "stderr": "",
            "return_code": -1,
        }
    except Exception as e:
        return {
            "error": str(e),
            "stdout": "",
            "stderr": "",
            "return_code": -1,
        }
