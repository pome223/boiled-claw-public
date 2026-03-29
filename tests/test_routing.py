from src.gateway.routing import decision_from_payload, heuristic_decision, targets_user_browser


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


def test_heuristic_decision_routes_browser_spreadsheet_task_to_control_loop():
    decision = heuristic_decision(
        "ブラウザを開いて GTCの予想を調べてスプレッドシートにまとめて"
    )

    assert decision.target == "control_loop"
    assert decision.specialist is None


def test_heuristic_decision_routes_computer_use_request_to_specialist():
    decision = heuristic_decision(
        "computer use としてこのブラウザの画面を見て押せるボタンを教えて"
    )

    assert decision.target == "specialist"
    assert decision.specialist == "computer_operator"
    assert decision.handoff_mode == "direct"


def test_heuristic_decision_routes_current_browser_click_to_computer_operator():
    decision = heuristic_decision("このブラウザのボタンをクリックして")

    assert decision.target == "specialist"
    assert decision.specialist == "computer_operator"
    assert decision.handoff_mode == "direct"


def test_targets_user_browser_detects_current_browser_language():
    assert targets_user_browser(
        "私が開いているブラウザのスプレッドシートにまとめて"
    ) is True
    assert targets_user_browser(
        "今開いているタブの Google スプレッドシートに入力して"
    ) is True
    assert targets_user_browser(
        "このブラウザを使って GTC の予想を調べて"
    ) is True
    assert targets_user_browser(
        "ブラウザを開いて GTCの予想を調べてスプレッドシートにまとめて"
    ) is False


def test_decision_from_payload_forces_control_loop_for_current_browser_spreadsheet_request():
    decision = decision_from_payload(
        {
            "target": "specialist",
            "specialist": "web_researcher",
            "handoff_mode": "preflight_then_root",
            "reason": "research request",
            "confidence": 0.91,
            "dynamic_agent": {},
        },
        fallback_message="このブラウザを使ってNvidiaのGTCで紹介される可能性のある技術を調べてスプレッドsーとにまとめて",
    )

    assert decision.target == "control_loop"
    assert decision.specialist is None


def test_decision_from_payload_routes_current_browser_research_to_current_tab_operator():
    decision = decision_from_payload(
        {
            "target": "control_loop",
            "specialist": None,
            "handoff_mode": "direct",
            "reason": "multi-step browser task",
            "confidence": 0.9,
            "dynamic_agent": {},
        },
        fallback_message="このブラウザをつかって午後の東京の花粉を調べて",
    )

    assert decision.target == "specialist"
    assert decision.specialist == "current_tab_operator"
    assert decision.handoff_mode == "direct"


def test_decision_from_payload_routes_screen_aware_current_browser_request_to_computer_operator():
    decision = decision_from_payload(
        {
            "target": "specialist",
            "specialist": "current_tab_operator",
            "handoff_mode": "direct",
            "reason": "browser task",
            "confidence": 0.9,
            "dynamic_agent": {},
        },
        fallback_message="このブラウザの画面を見ながら押せるボタンを教えて",
    )

    assert decision.target == "specialist"
    assert decision.specialist == "computer_operator"
    assert decision.handoff_mode == "direct"


def test_decision_from_payload_routes_current_browser_click_to_computer_operator():
    decision = decision_from_payload(
        {
            "target": "specialist",
            "specialist": "desktop_operator",
            "handoff_mode": "direct",
            "reason": "desktop control request",
            "confidence": 0.83,
            "dynamic_agent": {},
        },
        fallback_message="このブラウザのボタンをクリックして",
    )

    assert decision.target == "specialist"
    assert decision.specialist == "computer_operator"
    assert decision.handoff_mode == "direct"


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
