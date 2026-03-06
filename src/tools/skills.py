"""
Skills ツール
ロード済みスキルの一覧取得と実行
"""

from __future__ import annotations

import json
from typing import Any, Dict

from src.skills.base import get_skill_registry
from src.skills.runtime import ensure_skills_loaded


async def skill_list() -> Dict[str, Any]:
    """ロード済みスキル一覧を返す"""
    await ensure_skills_loaded()
    registry = get_skill_registry()
    items = []
    for meta in registry.list_skills():
        items.append(
            {
                "name": meta.name,
                "description": meta.description,
                "version": meta.version,
                "author": meta.author,
                "tags": meta.tags,
            }
        )
    return {"count": len(items), "skills": items}


async def skill_execute(name: str, params_json: str = "{}") -> Dict[str, Any]:
    """
    スキルを実行する

    Args:
        name: スキル名
        params_json: スキル引数(JSON文字列)
    """
    await ensure_skills_loaded()
    registry = get_skill_registry()
    skill = registry.get_skill(name)
    if not skill:
        return {"ok": False, "message": f"Skill not found: {name}"}

    try:
        params = json.loads(params_json) if params_json.strip() else {}
        if not isinstance(params, dict):
            return {"ok": False, "message": "params_json must decode to object"}
    except json.JSONDecodeError as exc:
        return {"ok": False, "message": f"Invalid params_json: {exc}"}

    is_valid, reason = await skill.validate_input(**params)
    if not is_valid:
        return {"ok": False, "message": reason or "Invalid input"}

    result = await skill.execute(**params)
    return {"ok": True, "skill": name, "result": result}

