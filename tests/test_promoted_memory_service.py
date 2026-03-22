from datetime import datetime, timezone

import pytest

from src.memory_lifecycle.adk_memory_service import PromotedMemoryService
from src.memory_lifecycle.memory_schema import (
    MemoryType,
    OriginatorType,
    PromotedMemory,
    Provenance,
    ReviewStatus,
    SensitivityLevel,
)
from src.memory_lifecycle.promoted_store import PromotedMemoryStore


def _make_memory(memory_id: str, user_id: str, content: str) -> PromotedMemory:
    return PromotedMemory(
        memory_id=memory_id,
        user_id=user_id,
        memory_type=MemoryType.SEMANTIC,
        content=content,
        subject="runtime",
        provenance=Provenance(
            originator_type=OriginatorType.SYSTEM,
            capture_method="test",
            captured_at=datetime.now(timezone.utc),
        ),
        confidence=0.9,
        trust_score=0.9,
        sensitivity=SensitivityLevel.INTERNAL,
        review_status=ReviewStatus.PROMOTED,
    )


@pytest.mark.asyncio
async def test_promoted_memory_service_scopes_by_app_and_user(tmp_path):
    service = PromotedMemoryService(
        PromotedMemoryStore(str(tmp_path / "promoted.db"))
    )

    await service.store_promoted_memories(
        app_name="app-a",
        memories=[
            _make_memory("mem-1", "alice", "ADK runtime note"),
            _make_memory("mem-2", "bob", "ADK runtime note"),
        ],
    )
    await service.store_promoted_memories(
        app_name="app-b",
        memories=[_make_memory("mem-3", "alice", "ADK runtime note")],
    )

    result = await service.search_memory(
        app_name="app-a",
        user_id="alice",
        query="runtime",
    )

    assert len(result.memories) == 1
    assert result.memories[0].content.parts[0].text == "ADK runtime note"
    assert result.memories[0].author == "promoted:semantic"
