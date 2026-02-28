"""
メモリツール - ベクトル検索とSQLite
OpenClaw のメモリシステムを参考
"""

import sqlite3
import json
import time
from typing import List, Dict, Any, Optional
from pathlib import Path
import numpy as np


class MemoryStore:
    """シンプルなメモリストア (SQLite + ベクトル検索)"""

    def __init__(self, db_path: str = "data/memory.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """データベースを初期化"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # メモリテーブル
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                embedding TEXT,
                metadata TEXT,
                tags TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)

        # タグインデックス
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tags ON memories(tags)
        """)

        # 作成日インデックス
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_created_at ON memories(created_at DESC)
        """)

        conn.commit()
        conn.close()

    def store(
        self,
        content: str,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        embedding: Optional[List[float]] = None,
    ) -> int:
        """メモリを保存"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        now = time.time()
        tags_str = json.dumps(tags or [])
        metadata_str = json.dumps(metadata or {})
        embedding_str = json.dumps(embedding) if embedding else None

        cursor.execute("""
            INSERT INTO memories (content, embedding, metadata, tags, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (content, embedding_str, metadata_str, tags_str, now, now))

        memory_id = cursor.lastrowid
        conn.commit()
        conn.close()

        return memory_id

    def search(
        self,
        query: Optional[str] = None,
        tags: Optional[List[str]] = None,
        limit: int = 10,
        embedding: Optional[List[float]] = None,
    ) -> List[Dict[str, Any]]:
        """メモリを検索"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # タグフィルタ
        if tags:
            # タグの部分一致検索
            tag_conditions = " OR ".join([f"tags LIKE ?" for _ in tags])
            query_sql = f"""
                SELECT id, content, metadata, tags, created_at
                FROM memories
                WHERE {tag_conditions}
                ORDER BY created_at DESC
                LIMIT ?
            """
            params = [f'%"{tag}"%' for tag in tags] + [limit]
        elif query:
            # テキスト検索
            query_sql = """
                SELECT id, content, metadata, tags, created_at
                FROM memories
                WHERE content LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
            """
            params = [f"%{query}%", limit]
        else:
            # 全件取得 (最新順)
            query_sql = """
                SELECT id, content, metadata, tags, created_at
                FROM memories
                ORDER BY created_at DESC
                LIMIT ?
            """
            params = [limit]

        cursor.execute(query_sql, params)
        rows = cursor.fetchall()
        conn.close()

        results = []
        for row in rows:
            results.append({
                "id": row[0],
                "content": row[1],
                "metadata": json.loads(row[2]),
                "tags": json.loads(row[3]),
                "created_at": row[4],
            })

        # ベクトル検索 (簡易版 - 全件取得してコサイン類似度計算)
        if embedding and results:
            query_vec = np.array(embedding)
            scored_results = []

            for result in results:
                # 仮のスコア (実装時はembeddingを使って計算)
                result["score"] = 1.0
                scored_results.append(result)

            # スコア順にソート
            scored_results.sort(key=lambda x: x["score"], reverse=True)
            return scored_results[:limit]

        return results

    def delete(self, memory_id: int) -> bool:
        """メモリを削除"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        deleted = cursor.rowcount > 0

        conn.commit()
        conn.close()

        return deleted

    def get_stats(self) -> Dict[str, Any]:
        """メモリ統計を取得"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM memories")
        total = cursor.fetchone()[0]

        cursor.execute("SELECT created_at FROM memories ORDER BY created_at ASC LIMIT 1")
        oldest = cursor.fetchone()

        cursor.execute("SELECT created_at FROM memories ORDER BY created_at DESC LIMIT 1")
        newest = cursor.fetchone()

        conn.close()

        return {
            "total_memories": total,
            "oldest": oldest[0] if oldest else None,
            "newest": newest[0] if newest else None,
        }


# グローバルインスタンス
_memory_store: Optional[MemoryStore] = None


def get_memory_store() -> MemoryStore:
    """メモリストアインスタンスを取得"""
    global _memory_store
    if _memory_store is None:
        _memory_store = MemoryStore()
    return _memory_store


# ADK ツール関数


async def memory_store(
    content: str,
    tags: Optional[str] = None,
    metadata: Optional[str] = None,
) -> Dict[str, Any]:
    """
    情報をメモリに保存する

    Args:
        content: 保存する内容
        tags: タグ (カンマ区切り)
        metadata: メタデータ (JSON文字列)

    Returns:
        保存結果
    """
    try:
        store = get_memory_store()

        tags_list = [t.strip() for t in tags.split(",")] if tags else None
        metadata_dict = json.loads(metadata) if metadata else None

        memory_id = store.store(
            content=content,
            tags=tags_list,
            metadata=metadata_dict,
        )

        return {
            "memory_id": memory_id,
            "content": content,
            "tags": tags_list,
            "success": True,
        }

    except Exception as e:
        return {
            "error": str(e),
            "success": False,
        }


async def memory_search(
    query: Optional[str] = None,
    tags: Optional[str] = None,
    limit: int = 10,
) -> Dict[str, Any]:
    """
    メモリから情報を検索する

    Args:
        query: 検索クエリ
        tags: タグフィルタ (カンマ区切り)
        limit: 最大取得件数

    Returns:
        検索結果
    """
    try:
        store = get_memory_store()

        tags_list = [t.strip() for t in tags.split(",")] if tags else None

        results = store.search(
            query=query,
            tags=tags_list,
            limit=limit,
        )

        return {
            "results": results,
            "count": len(results),
            "query": query,
            "tags": tags_list,
            "success": True,
        }

    except Exception as e:
        return {
            "error": str(e),
            "success": False,
        }


async def memory_stats() -> Dict[str, Any]:
    """
    メモリ統計を取得する

    Returns:
        メモリ統計
    """
    try:
        store = get_memory_store()
        stats = store.get_stats()

        return {
            "stats": stats,
            "success": True,
        }

    except Exception as e:
        return {
            "error": str(e),
            "success": False,
        }
