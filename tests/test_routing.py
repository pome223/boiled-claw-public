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
