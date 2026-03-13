from src.gateway.routing import heuristic_decision


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
