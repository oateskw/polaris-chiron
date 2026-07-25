import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import chiron_healer


class ResponseStub:
    status_code = 200
    text = "ok"


class AsyncClientStub:
    def __init__(self, captured, *args, **kwargs):
        self.captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False

    async def post(self, endpoint, **kwargs):
        self.captured.update({"endpoint": endpoint, **kwargs})
        return ResponseStub()


def install_http_client(monkeypatch, captured):
    monkeypatch.setattr(
        chiron_healer.httpx,
        "AsyncClient",
        lambda *args, **kwargs: AsyncClientStub(captured, *args, **kwargs),
    )


def test_chiron_downgrades_untraced_error_and_authenticates(monkeypatch):
    captured = {}
    monkeypatch.setenv("HELIX_API_URL", "https://helix.test")
    monkeypatch.setenv("HELIX_AUTH_TOKEN", "test-token")
    install_http_client(monkeypatch, captured)

    asyncio.run(
        chiron_healer.send_telemetry_log("ERROR", "Git clone failed", {"session_id": 7})
    )

    assert captured["headers"]["X-Helix-Auth-Token"] == "test-token"
    assert captured["json"]["log_level"] == "WARNING"
    assert captured["json"]["metadata"]["extra_context"] == {"session_id": 7}


def test_chiron_preserves_error_with_python_traceback(monkeypatch):
    captured = {}
    python_traceback = (
        "Traceback (most recent call last):\n"
        '  File "/app/chiron_healer.py", line 42, in run\n'
        "ValueError: failed"
    )
    monkeypatch.setenv("HELIX_API_URL", "https://helix.test")
    monkeypatch.setenv("HELIX_AUTH_TOKEN", "test-token")
    install_http_client(monkeypatch, captured)

    asyncio.run(
        chiron_healer.send_telemetry_log(
            "ERROR", "ValueError: failed", {"traceback": python_traceback}
        )
    )

    assert captured["json"]["log_level"] == "ERROR"
    assert captured["json"]["metadata"]["traceback"] == python_traceback


def test_optimization_worker_tracks_running_and_completed(monkeypatch):
    payload = SimpleNamespace(target_agent="Pulsar")
    queue = AsyncMock()
    queue.get.side_effect = [payload, asyncio.CancelledError()]
    queue.task_done = MagicMock()
    status_update = AsyncMock()
    pipeline = AsyncMock()
    monkeypatch.setattr(chiron_healer, "optimization_queue", queue)
    monkeypatch.setattr(
        chiron_healer, "update_manifest_optimization_status", status_update
    )
    monkeypatch.setattr(chiron_healer, "run_proactive_optimization_pipeline", pipeline)

    async def run_worker_once():
        try:
            await chiron_healer.chiron_optimization_worker()
        except asyncio.CancelledError:
            pass

    asyncio.run(run_worker_once())

    assert status_update.await_args_list[0].args == ("pulsar", "RUNNING")
    assert status_update.await_args_list[1].args == ("pulsar", "COMPLETED")
    queue.task_done.assert_called_once()
