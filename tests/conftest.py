from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolated_task_store(tmp_path, monkeypatch):
    from src.runtime import task_store as task_store_module

    monkeypatch.setattr(
        task_store_module,
        "get_settings",
        lambda: type("Settings", (), {"task_store_db_path": tmp_path / "tasks.db"})(),
    )
    task_store_module.reset_task_store()
    yield
    task_store_module.reset_task_store()
