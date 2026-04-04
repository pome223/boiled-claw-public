"""Reuse and prompt helpers for self-improvement flows."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from google.adk.agents.context import Context as ToolContext

from src.tools.self_improvement_runtime.common import compact_text, read_state


MemorySearchFn = Callable[..., Awaitable[dict[str, Any]]]
MemoryStoreGetter = Callable[[], Any]


def trajectory_failure_reason(trajectory: dict[str, Any]) -> str:
    verification = trajectory.get("verification")
    if isinstance(verification, dict) and not verification.get("success"):
        return f"verification {verification.get('status', 'failed')}"
    for attempt in trajectory.get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        result = attempt.get("result")
        if isinstance(result, dict):
            error = str(result.get("error") or "").strip()
            if error:
                return error
        verification = attempt.get("verification")
        if isinstance(verification, dict) and not verification.get("success"):
            return f"verification {verification.get('status', 'failed')}"
    return "unknown failure"


def trajectory_demo_goal(trajectory: dict[str, Any]) -> str:
    request = trajectory.get("request") or {}
    action = str(trajectory.get("action") or "action")
    target = (
        request.get("selector")
        or request.get("title")
        or request.get("identifier")
        or request.get("value_contains")
        or trajectory.get("final_surface")
        or "unknown-target"
    )
    return f"Investigate failed {action} trajectory {trajectory.get('id')} for {target}"


def trajectory_search_goal(trajectory: dict[str, Any]) -> str:
    request = trajectory.get("request") or {}
    action = str(trajectory.get("action") or "action")
    target = (
        request.get("selector")
        or request.get("title")
        or request.get("identifier")
        or request.get("value_contains")
        or trajectory.get("final_surface")
        or "unknown-target"
    )
    return f"Search repair candidates for failed {action} trajectory {trajectory.get('id')} for {target}"


def trajectory_improvement_summary(trajectory: dict[str, Any]) -> str:
    request = trajectory.get("request") or {}
    action = str(trajectory.get("action") or "action")
    target = (
        request.get("selector")
        or request.get("title")
        or request.get("identifier")
        or request.get("value_contains")
        or "unknown-target"
    )
    return (
        f"Demo candidate for failed computer trajectory {trajectory.get('id')}: "
        f"improve {action} handling around {target} after {trajectory_failure_reason(trajectory)}."
    )


def trajectory_reuse_query(trajectory: dict[str, Any]) -> str:
    hints = trajectory_reuse_hints(trajectory)
    parts = [
        hints.get("action"),
        hints.get("selector"),
        hints.get("title"),
        hints.get("identifier"),
        hints.get("surface"),
        hints.get("failure_reason"),
    ]
    return " ".join(str(part).strip() for part in parts if part)


def normalize_reuse_value(value: Any) -> str:
    return str(value or "").strip().lower()


def trajectory_reuse_hints(trajectory: dict[str, Any]) -> dict[str, str]:
    request = trajectory.get("request") or {}
    action = normalize_reuse_value(trajectory.get("action"))
    selector = normalize_reuse_value(request.get("selector"))
    title = normalize_reuse_value(request.get("title"))
    identifier = normalize_reuse_value(request.get("identifier"))
    value_contains = normalize_reuse_value(request.get("value_contains"))
    surface = normalize_reuse_value(trajectory.get("final_surface"))
    failure_reason = normalize_reuse_value(trajectory_failure_reason(trajectory))
    target = selector or title or identifier or value_contains or surface or "unknown-target"
    trajectory_key = "::".join(part for part in [action, surface, target] if part)
    return {
        "trajectory_key": trajectory_key,
        "action": action,
        "selector": selector,
        "title": title,
        "identifier": identifier,
        "value_contains": value_contains,
        "surface": surface,
        "failure_reason": failure_reason,
        "target": target,
    }


def state_reuse_hints(canary) -> dict[str, str]:
    state = read_state(canary)
    for key in ("search", "demo"):
        candidate = state.get(key)
        if not isinstance(candidate, dict):
            continue
        reuse_hints = candidate.get("reuse_hints")
        if isinstance(reuse_hints, dict):
            return {str(name): normalize_reuse_value(value) for name, value in reuse_hints.items()}
    return {}


def cheap_reuse_match_score(
    hints: dict[str, str],
    metadata: dict[str, Any],
) -> int:
    score = 0
    metadata_key = normalize_reuse_value(metadata.get("trajectory_key"))
    if metadata_key and metadata_key == hints.get("trajectory_key"):
        score += 10
    for field, weight in {
        "selector": 5,
        "identifier": 4,
        "title": 3,
        "value_contains": 2,
        "action": 3,
        "surface": 2,
    }.items():
        hint_value = hints.get(field)
        metadata_value = normalize_reuse_value(metadata.get(field))
        if hint_value and metadata_value and hint_value == metadata_value:
            score += weight
    if hints.get("failure_reason") and normalize_reuse_value(metadata.get("failure_reason")) == hints.get("failure_reason"):
        score += 1
    return score


def prefilter_reuse_suggestions(
    trajectory: dict[str, Any],
    *,
    limit: int,
    get_memory_store_fn: MemoryStoreGetter,
) -> list[dict[str, Any]]:
    hints = trajectory_reuse_hints(trajectory)
    try:
        candidates = get_memory_store_fn().search(
            query=None,
            kinds=["approved_improvement"],
            limit=max(limit * 10, 50),
        )
    except Exception:
        return []

    matches: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        score = cheap_reuse_match_score(hints, metadata)
        if score <= 0:
            continue
        matches.append(
            {
                "memory_id": item.get("id"),
                "content": item.get("content"),
                "score": float(score),
                "created_at": item.get("created_at"),
                "tags": item.get("tags") or [],
                "metadata": metadata,
                "match_type": "prefilter",
            }
        )

    matches.sort(
        key=lambda item: (float(item.get("score") or 0.0), float(item.get("created_at") or 0.0)),
        reverse=True,
    )
    return matches[:limit]


async def find_reuse_suggestions(
    trajectory: dict[str, Any],
    *,
    get_memory_store_fn: MemoryStoreGetter,
    memory_search_fn: MemorySearchFn,
    limit: int = 3,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    query = trajectory_reuse_query(trajectory)
    if not query:
        return {"query": "", "results": []}

    bounded_limit = max(1, min(limit, 10))
    prefiltered = prefilter_reuse_suggestions(
        trajectory,
        limit=bounded_limit,
        get_memory_store_fn=get_memory_store_fn,
    )
    if len(prefiltered) >= bounded_limit:
        return {"query": query, "results": prefiltered}

    search = await memory_search_fn(
        query=query,
        kind="approved_improvement",
        limit=bounded_limit,
        tool_context=tool_context,
    )
    if not search.get("success"):
        return {
            "query": query,
            "results": [],
            "error": search.get("error") or "failed to search approved improvements",
        }

    results_by_id: dict[Any, dict[str, Any]] = {
        item.get("memory_id"): item for item in prefiltered if item.get("memory_id") is not None
    }
    for item in search.get("results") or []:
        if not isinstance(item, dict):
            continue
        payload = {
            "memory_id": item.get("id"),
            "content": item.get("content"),
            "score": item.get("score"),
            "created_at": item.get("created_at"),
            "tags": item.get("tags") or [],
            "metadata": item.get("metadata") or {},
            "match_type": "semantic",
        }
        memory_id = payload.get("memory_id")
        if memory_id in results_by_id:
            continue
        results_by_id[memory_id] = payload
    results = list(results_by_id.values())
    results.sort(
        key=lambda item: (float(item.get("score") or 0.0), float(item.get("created_at") or 0.0)),
        reverse=True,
    )
    return {"query": query, "results": results[:bounded_limit]}


def reuse_guidance_lines(reuse: dict[str, Any], *, limit: int = 3) -> list[str]:
    results = reuse.get("results") if isinstance(reuse, dict) else None
    if not isinstance(results, list):
        return []

    guidance: list[str] = []
    for item in results[: max(1, min(limit, 5))]:
        if not isinstance(item, dict):
            continue
        content = compact_text(str(item.get("content") or ""), limit=160)
        if not content:
            continue
        metadata = item.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        qualifiers = []
        if metadata.get("trajectory_key"):
            qualifiers.append(str(metadata["trajectory_key"]))
        elif metadata.get("selector"):
            qualifiers.append(f"selector={metadata['selector']}")
        if metadata.get("surface"):
            qualifiers.append(f"surface={metadata['surface']}")
        if item.get("memory_id") is not None:
            qualifiers.append(f"memory={item['memory_id']}")
        qualifier_text = f" ({'; '.join(qualifiers)})" if qualifiers else ""
        guidance.append(f"- {content}{qualifier_text}")
    return guidance


def reuse_guidance_text(reuse: dict[str, Any], *, limit: int = 3) -> str:
    lines = reuse_guidance_lines(reuse, limit=limit)
    if not lines:
        return ""
    return "\n".join(
        [
            "Approved improvement reuse hints:",
            *lines,
            "Prefer adapting these approved improvements before inventing a new fix.",
        ]
    )


def improvement_summary_with_reuse(base_summary: str, reuse: dict[str, Any]) -> str:
    guidance = reuse_guidance_text(reuse)
    if not guidance:
        return base_summary
    return f"{base_summary}\n\n{guidance}"


def build_repair_prompt(
    *,
    goal: str,
    improvement_summary: str,
    trajectory: dict[str, Any],
    reuse: dict[str, Any],
) -> str:
    request = trajectory.get("request") or {}
    target = (
        request.get("selector")
        or request.get("title")
        or request.get("identifier")
        or request.get("value_contains")
        or trajectory.get("final_surface")
        or "unknown-target"
    )
    surface = str(trajectory.get("final_surface") or "unknown")
    lines = [
        goal,
        "",
        f"Failure reason: {trajectory_failure_reason(trajectory)}",
        f"Target: {target}",
        f"Surface: {surface}",
        "",
        f"Improvement summary: {improvement_summary}",
    ]
    guidance = reuse_guidance_text(reuse)
    if guidance:
        lines.extend(["", guidance])
    return "\n".join(lines)


__all__ = [
    "build_repair_prompt",
    "find_reuse_suggestions",
    "improvement_summary_with_reuse",
    "prefilter_reuse_suggestions",
    "state_reuse_hints",
    "trajectory_demo_goal",
    "trajectory_failure_reason",
    "trajectory_improvement_summary",
    "trajectory_reuse_hints",
    "trajectory_reuse_query",
    "trajectory_search_goal",
]
