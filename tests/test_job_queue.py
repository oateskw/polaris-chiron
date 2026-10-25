import asyncio
from unittest.mock import AsyncMock

import pytest

import job_queue

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def job(job_id=1, agent="vanguard"):
    return {"id": job_id, "target_agent": agent, "payload": {"target_agent": agent}}


@pytest.fixture
def recorded(monkeypatch):
    finish = AsyncMock()
    status = AsyncMock()
    monkeypatch.setattr(job_queue, "finish_job", finish)
    return finish, status


async def test_successful_job_is_done_with_pr_url(recorded):
    finish, status = recorded
    runner = AsyncMock(return_value={"outcome": "pr_opened", "pr_url": "https://x/pull/1"})

    await job_queue.process_job(job(), runner, status)

    finish.assert_awaited_once_with(1, "done", pr_url="https://x/pull/1")
    assert [c.args for c in status.await_args_list] == [
        ("vanguard", "RUNNING"),
        ("vanguard", "COMPLETED"),
    ]


async def test_no_changes_is_still_done(recorded):
    finish, status = recorded
    await job_queue.process_job(
        job(), AsyncMock(return_value={"outcome": "no_changes"}), status
    )
    finish.assert_awaited_once_with(1, "done", pr_url=None)


async def test_failed_outcome_marks_job_and_manifest_failed(recorded):
    finish, status = recorded
    runner = AsyncMock(return_value={"outcome": "failed", "error": "clone failed"})

    await job_queue.process_job(job(), runner, status)

    finish.assert_awaited_once_with(1, "failed", error="clone failed")
    assert status.await_args_list[-1].args == ("vanguard", "FAILED")


async def test_exception_marks_job_failed_and_does_not_raise(recorded):
    finish, status = recorded
    runner = AsyncMock(side_effect=RuntimeError("boom"))

    await job_queue.process_job(job(), runner, status)

    assert finish.await_args.args[:2] == (1, "failed")
    assert status.await_args_list[-1].args == ("vanguard", "FAILED")


async def test_run_cap_fails_a_stuck_job(recorded, monkeypatch):
    finish, status = recorded
    monkeypatch.setattr(job_queue, "RUN_CAP_MINUTES", 0.001)

    async def stuck(_payload):
        await asyncio.sleep(10)

    await job_queue.process_job(job(), stuck, status)

    assert finish.await_args.args[:2] == (1, "failed")
    assert "run cap" in finish.await_args.kwargs["error"]


async def test_shutdown_leaves_job_running_for_recovery(recorded):
    finish, status = recorded

    async def cancelled(_payload):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await job_queue.process_job(job(), cancelled, status)

    finish.assert_not_called()


async def test_keep_awake_runs_only_while_a_job_runs(recorded, monkeypatch):
    finish, status = recorded
    pings = []
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://chiron.test")
    monkeypatch.setattr(job_queue, "KEEP_AWAKE_INTERVAL_SECONDS", 0.01)

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def head(self, url):
            pings.append(url)

    monkeypatch.setattr(job_queue.httpx, "AsyncClient", Client)

    async def slow(_payload):
        await asyncio.sleep(0.1)
        return {"outcome": "no_changes"}

    await job_queue.process_job(job(), slow, status)
    during = len(pings)
    await asyncio.sleep(0.1)

    assert during >= 2
    assert len(pings) == during  # stopped when the job ended
    assert pings[0] == "https://chiron.test/"


async def test_keep_awake_is_a_noop_without_a_public_url(monkeypatch):
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    await asyncio.wait_for(job_queue.keep_awake(), timeout=1)


async def test_worker_drains_queue_then_waits(monkeypatch):
    claims = [job(1), job(2), None]
    monkeypatch.setattr(job_queue, "recover_jobs", AsyncMock(return_value=2))
    monkeypatch.setattr(job_queue, "claim_next_job", AsyncMock(side_effect=claims))
    processed = []

    async def fake_process(j, runner, status):
        processed.append(j["id"])

    monkeypatch.setattr(job_queue, "process_job", fake_process)

    task = asyncio.create_task(job_queue.worker_loop(AsyncMock(), AsyncMock()))
    await asyncio.sleep(0.1)
    task.cancel()

    assert processed == [1, 2]
    assert job_queue.claim_next_job.await_count == 3  # no polling after the queue drained


async def test_worker_survives_failed_recovery(monkeypatch):
    monkeypatch.setattr(
        job_queue, "recover_jobs", AsyncMock(side_effect=RuntimeError("db down"))
    )
    claim = AsyncMock(return_value=None)
    monkeypatch.setattr(job_queue, "claim_next_job", claim)

    task = asyncio.create_task(job_queue.worker_loop(AsyncMock(), AsyncMock()))
    await asyncio.sleep(0.05)
    job_queue.wake()
    await asyncio.sleep(0.05)
    task.cancel()

    claim.assert_awaited()


async def post_optimize(monkeypatch, enqueue):
    import chiron_healer
    from httpx import ASGITransport, AsyncClient

    monkeypatch.setenv("HELIX_AUTH_TOKEN", "tok")
    monkeypatch.setattr(job_queue, "enqueue_job", enqueue)
    monkeypatch.setattr(job_queue, "queue_position", AsyncMock(return_value=1))
    monkeypatch.setattr(
        chiron_healer, "update_manifest_optimization_status", AsyncMock()
    )
    async with AsyncClient(
        transport=ASGITransport(app=chiron_healer.app), base_url="http://t"
    ) as client:
        return await client.post(
            "/api/v1/chiron/optimize",
            headers={"X-Chiron-Key": "tok"},
            json={"target_agent": "Vanguard", "optimization_specification": "x"},
        )


async def test_optimize_endpoint_stores_job_and_wakes_worker(monkeypatch):
    woke = []
    monkeypatch.setattr(job_queue, "wake", lambda: woke.append(1))

    res = await post_optimize(monkeypatch, AsyncMock(return_value=7))

    assert res.json()["status"] == "optimization_analysis_queued"
    assert res.json()["job_id"] == 7
    assert woke == [1]


async def test_optimize_endpoint_reports_duplicate(monkeypatch):
    res = await post_optimize(monkeypatch, AsyncMock(return_value=None))
    assert res.json()["status"] == "optimization_analysis_already_queued"


async def test_optimize_endpoint_503_when_database_is_down(monkeypatch):
    res = await post_optimize(monkeypatch, AsyncMock(side_effect=RuntimeError("down")))
    assert res.status_code == 503
