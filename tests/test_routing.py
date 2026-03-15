from src.gateway.routing import decision_from_payload, heuristic_decision


def test_heuristic_decision_routes_desktop_view_to_specialist():
    decision = heuristic_decision("前面アプリとウィンドウを確認してスクリーンショットを撮って")

    assert decision.target == "specialist"
    assert decision.specialist == "desktop_operator"
    assert decision.handoff_mode == "preflight_then_root"


def test_heuristic_decision_routes_desktop_control_to_specialist():
    decision = heuristic_decision("このボタンをクリックして文字を入力して")

    assert decision.target == "specialist"
    assert decision.specialist == "desktop_operator"
    assert decision.handoff_mode == "direct"


def test_heuristic_decision_routes_desktop_runtime_stop_to_specialist():
    decision = heuristic_decision("デスクトップ操作を停止して emergency stop にして")

    assert decision.target == "specialist"
    assert decision.specialist == "desktop_operator"
    assert decision.handoff_mode == "direct"


def test_heuristic_decision_routes_multistep_desktop_task_to_control_loop():
    decision = heuristic_decision(
        "Safari を開いて、その後検索欄をクリックして文字を入力して、結果を確認しながら順番に進めて"
    )

    assert decision.target == "control_loop"
    assert decision.specialist is None


def test_decision_from_payload_keeps_browser_only_flow_on_specialist():
    decision = decision_from_payload(
        {
            "target": "control_loop",
            "specialist": None,
            "handoff_mode": "direct",
            "reason": "multi-step browser task",
            "confidence": 0.9,
            "dynamic_agent": {},
        },
        fallback_message="ブラウザを開いて http://localhost:18789/chat を開いて、textarea に Hello World と入力して Enter を押して送信して",
    )

    assert decision.target == "specialist"
    assert decision.specialist == "control_ui_chat_operator"
    assert decision.handoff_mode == "direct"


def test_decision_from_payload_keeps_control_ui_chat_flow_on_specialist():
    decision = decision_from_payload(
        {
            "target": "control_loop",
            "specialist": None,
            "handoff_mode": "direct",
            "reason": "multi-step browser task",
            "confidence": 0.9,
            "dynamic_agent": {},
        },
        fallback_message="http://localhost:18789/chat にアクセスして東京の今日の天気を聞いてみて",
    )

    assert decision.target == "specialist"
    assert decision.specialist == "control_ui_chat_operator"
    assert decision.handoff_mode == "direct"
