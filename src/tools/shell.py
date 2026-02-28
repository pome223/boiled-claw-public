"""
シェルコマンド実行ツール
"""

import asyncio
import shlex


# 危険なコマンドのブラックリスト
DANGEROUS_COMMANDS = [
    "rm -rf",
    "sudo rm",
    "mkfs",
    "dd if=",
    "> /dev/",
    "chmod 777",
    ":(){ :|:& };:",  # fork bomb
]


async def run_shell(command: str, timeout: int = 30) -> dict:
    """
    シェルコマンドを安全に実行する

    Args:
        command: 実行するコマンド
        timeout: タイムアウト秒数（デフォルト30秒）

    Returns:
        stdout, stderr, return_code を含む辞書
    """
    # 危険なコマンドチェック
    for dangerous in DANGEROUS_COMMANDS:
        if dangerous in command:
            return {
                "error": f"Dangerous command detected: '{dangerous}'. Execution blocked.",
                "stdout": "",
                "stderr": "",
                "return_code": -1,
            }

    try:
        process = await asyncio.create_subprocess_shell(
            command,
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
    except Exception as e:
        return {
            "error": str(e),
            "stdout": "",
            "stderr": "",
            "return_code": -1,
        }
