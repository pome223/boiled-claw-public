import json

from click.testing import CliRunner

from src.main import cli
from src.quickstart_smoke import run_quickstart_smoke
from src.runtime.task_store import get_task_store


def test_run_quickstart_smoke_creates_completed_task_and_timeline():
    result = run_quickstart_smoke(gateway_url="http://127.0.0.1:18789")

    assert result["success"] is True
    assert result["requires"] == {
        "google_api_key": False,
        "chrome_extension": False,
        "host_bridge": False,
        "desktop_bridge": False,
    }

    task = get_task_store().get(result["task_id"])
    assert task is not None
    assert task["kind"] == "quickstart_smoke"
    assert task["status"] == "completed"
    assert task["owner_session_id"] == "quickstart-local"
    assert task["owner_user_id"] == "quickstart"
    assert task["artifacts"]["quickstart"]["requires_model"] is False
    assert task["artifacts"]["result"]["success"] is True
    assert task["metadata"]["source"] == "quickstart"

    timeline = get_task_store().query_timeline(result["task_id"], page=1, page_size=20)
    event_types = {event["event_type"] for event in timeline["events"]}
    assert {
        "created",
        "quickstart_smoke_started",
        "status_changed",
        "quickstart_smoke_completed",
    }.issubset(event_types)


def test_quickstart_smoke_cli_outputs_machine_readable_json():
    runner = CliRunner()
    cli_result = runner.invoke(
        cli,
        [
            "quickstart-smoke",
            "--gateway-url",
            "http://127.0.0.1:18789",
            "--user-id",
            "alice",
            "--session-id",
            "sess-quick",
            "--json",
        ],
    )

    assert cli_result.exit_code == 0
    payload = json.loads(cli_result.output)
    assert payload["success"] is True
    assert payload["control_ui_url"] == "http://127.0.0.1:18789/chat"

    task = get_task_store().get(payload["task_id"])
    assert task is not None
    assert task["owner_user_id"] == "alice"
    assert task["owner_session_id"] == "sess-quick"
    assert task["status"] == "completed"


def test_gateway_web_dry_run_does_not_require_google_api_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    runner = CliRunner()
    cli_result = runner.invoke(cli, ["web", "--dry-run"])

    assert cli_result.exit_code == 0
    assert "Config OK" in cli_result.output
