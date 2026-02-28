"""
ファイル操作ツール
"""

from pathlib import Path


async def read_file(path: str) -> dict:
    """
    ファイルを読み込む

    Args:
        path: 読み込むファイルのパス

    Returns:
        ファイルの内容
    """
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


async def write_file(path: str, content: str) -> dict:
    """
    ファイルに書き込む

    Args:
        path: 書き込むファイルのパス
        content: 書き込む内容

    Returns:
        書き込み結果
    """
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
