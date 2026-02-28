"""
スキルローダー
スキルの動的読み込み
"""

import importlib
import importlib.util
from pathlib import Path
from typing import List, Optional

from src.skills.base import BaseSkill, get_skill_registry


class SkillLoader:
    """スキルローダー"""

    def __init__(self, skills_dir: str = "skills"):
        self.skills_dir = Path(skills_dir)
        self.registry = get_skill_registry()

    async def load_skill_from_file(self, file_path: Path) -> Optional[BaseSkill]:
        """ファイルからスキルを読み込む"""
        try:
            # モジュールを動的インポート
            spec = importlib.util.spec_from_file_location("skill_module", file_path)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                # BaseSkillを継承したクラスを探す
                for name in dir(module):
                    obj = getattr(module, name)
                    if (
                        isinstance(obj, type)
                        and issubclass(obj, BaseSkill)
                        and obj is not BaseSkill
                    ):
                        # スキルインスタンス化
                        skill = obj()
                        await skill.on_load()
                        self.registry.register(skill)
                        return skill

        except Exception as e:
            print(f"Failed to load skill from {file_path}: {e}")

        return None

    async def load_skills_from_directory(self, directory: Optional[Path] = None) -> List[BaseSkill]:
        """ディレクトリから全スキルを読み込む"""
        directory = directory or self.skills_dir

        if not directory.exists():
            return []

        loaded_skills = []

        for file_path in directory.glob("*.py"):
            if file_path.name.startswith("_"):
                continue

            skill = await self.load_skill_from_file(file_path)
            if skill:
                loaded_skills.append(skill)

        return loaded_skills

    async def unload_skill(self, skill_name: str) -> bool:
        """スキルをアンロード"""
        skill = self.registry.get_skill(skill_name)

        if skill:
            await skill.on_unload()
            del self.registry.skills[skill_name]
            return True

        return False

    async def reload_skill(self, skill_name: str, file_path: Path) -> Optional[BaseSkill]:
        """スキルをリロード"""
        await self.unload_skill(skill_name)
        return await self.load_skill_from_file(file_path)
