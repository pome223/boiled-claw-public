class StateKeys:
    """ADK session.state のキー定数。

    Persistent keys: 次の turn にも残す制御データ。
    Temporary keys (temp:): 現在の invocation 中のみ有効。
    """

    # ── Task ──────────────────────────────────────────────────────────────
    TASK_GOAL = "task:goal"
    TASK_CONSTRAINTS = "task:constraints"
    TASK_SUCCESS_CRITERIA = "task:success_criteria"

    # ── Plan ──────────────────────────────────────────────────────────────
    PLAN_CURRENT = "plan:current"
    PLAN_APPROVED = "plan:approved"
    PLAN_RISK_LEVEL = "plan:risk_level"

    # ── Approval / Verification / Repair ──────────────────────────────────
    APPROVAL_STATUS = "approval:status"
    APPROVAL_REQUEST = "approval:request"
    VERIFY_LAST_REPORT = "verify:last_report"
    REPAIR_COUNT = "repair:count"

    # ── Memory ────────────────────────────────────────────────────────────
    MEMORY_LAST_CANDIDATE_IDS = "memory:last_candidate_ids"
    MEMORY_LAST_PROMOTED_IDS = "memory:last_promoted_ids"

    # ── Temporary (invocation-scoped) ─────────────────────────────────────
    TEMP_RETRIEVAL_BUNDLE = "temp:retrieval_bundle"
    TEMP_PLANNER_DRAFT = "temp:planner_draft"
    TEMP_EXECUTOR_OUTPUTS = "temp:executor_outputs"
    TEMP_ARTIFACT_REFS = "temp:artifact_refs"
    TEMP_VERIFICATION_INPUTS = "temp:verification_inputs"
    TEMP_REPAIR_PATCH = "temp:repair_patch"
