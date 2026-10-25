"""Durable optimization job queue for a host that sleeps (Render free tier).

Jobs live in Postgres (`chiron_jobs`, created by Helix's init_db.py), so a sleep or
restart cannot lose work. The worker waits on an event instead of polling, so an idle
Chiron makes no database calls and the service is free to sleep. While a job runs,
Chiron requests its own public URL so Render does not put it to sleep mid-run.
"""

import asyncio
import os
from typing import Any, Awaitable, Callable, Optional

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

RUN_CAP_MINUTES = int(os.getenv("CHIRON_RUN_CAP_MINUTES", "30"))
MAX_ATTEMPTS = 2
KEEP_AWAKE_INTERVAL_SECONDS = 5 * 60

_wake: Optional[asyncio.Event] = None  # created by worker_loop, on its own event loop


def wake() -> None:
    """Tell the worker there may be a queued job."""
    if _wake is not None:
        _wake.set()


def _db_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


async def _connect():
    return await psycopg.AsyncConnection.connect(_db_url())


async def enqueue_job(payload: dict[str, Any]) -> Optional[int]:
    """Insert a queued job. Returns None when the agent already has an open job."""
    async with await _connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO chiron_jobs (target_agent, payload)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id;
                """,
                (payload["target_agent"].lower().strip(), Jsonb(payload)),
            )
            row = await cur.fetchone()
        await conn.commit()
    return row[0] if row else None


async def queue_position(job_id: int) -> int:
    async with await _connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) FROM chiron_jobs WHERE status = 'queued' AND id <= %s;",
                (job_id,),
            )
            row = await cur.fetchone()
    return int(row[0]) if row else 1


async def recover_jobs() -> int:
    """Re-queue jobs a killed process left in 'running'. Jobs that already used all
    their attempts are failed instead, so a job that crashes the host cannot loop.
    Returns the number of jobs waiting to run."""
    async with await _connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE chiron_jobs
                SET status = CASE WHEN attempts >= %s THEN 'failed' ELSE 'queued' END,
                    error = CASE WHEN attempts >= %s
                                 THEN 'Interrupted too many times' ELSE error END,
                    finished_at = CASE WHEN attempts >= %s THEN NOW() ELSE finished_at END
                WHERE status = 'running'
                  AND started_at < NOW() - (%s || ' minutes')::INTERVAL;
                """,
                (MAX_ATTEMPTS, MAX_ATTEMPTS, MAX_ATTEMPTS, RUN_CAP_MINUTES + 5),
            )
            await cur.execute("SELECT COUNT(*) FROM chiron_jobs WHERE status = 'queued';")
            row = await cur.fetchone()
        await conn.commit()
    return int(row[0]) if row else 0


async def claim_next_job() -> Optional[dict[str, Any]]:
    async with await _connect() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE chiron_jobs
                SET status = 'running', started_at = NOW(), attempts = attempts + 1
                WHERE id = (
                    SELECT id FROM chiron_jobs
                    WHERE status = 'queued'
                    ORDER BY id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING id, target_agent, payload;
                """
            )
            row = await cur.fetchone()
        await conn.commit()
    return row


async def finish_job(
    job_id: int, status: str, pr_url: Optional[str] = None, error: Optional[str] = None
) -> None:
    async with await _connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE chiron_jobs
                SET status = %s, pr_url = %s, error = %s, finished_at = NOW()
                WHERE id = %s;
                """,
                (status, pr_url, error, job_id),
            )
        await conn.commit()


async def keep_awake() -> None:
    """Request our own public URL on an interval. Runs only while a job is running,
    because traffic is what stops Render from putting the service to sleep."""
    url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not url:
        return
    while True:
        await asyncio.sleep(KEEP_AWAKE_INTERVAL_SECONDS)
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.head(f"{url}/")
        except Exception as exc:
            print(f"⚠️ [Chiron Queue] Keep-awake request failed: {exc}")


async def process_job(
    job: dict[str, Any],
    runner: Callable[[dict[str, Any]], Awaitable[Optional[dict[str, Any]]]],
    set_manifest_status: Callable[[str, str], Awaitable[None]],
) -> None:
    """Run one claimed job under the time cap and record the result. Never raises."""
    agent = job["target_agent"]
    await set_manifest_status(agent, "RUNNING")
    awake = asyncio.create_task(keep_awake())
    try:
        result = await asyncio.wait_for(
            runner(job["payload"]), timeout=RUN_CAP_MINUTES * 60
        )
        result = result or {}
        if result.get("outcome") == "failed":
            await finish_job(job["id"], "failed", error=result.get("error") or "Run failed")
            await set_manifest_status(agent, "FAILED")
        else:
            await finish_job(job["id"], "done", pr_url=result.get("pr_url"))
            await set_manifest_status(agent, "COMPLETED")
    except asyncio.TimeoutError:
        await finish_job(
            job["id"], "failed", error=f"Exceeded the {RUN_CAP_MINUTES} minute run cap"
        )
        await set_manifest_status(agent, "FAILED")
    except asyncio.CancelledError:
        # The host is shutting down; leave the row 'running' so recover_jobs re-queues it.
        raise
    except Exception as exc:
        print(f"⚠️ [Chiron Queue] Job {job['id']} failed: {exc}")
        await finish_job(job["id"], "failed", error=str(exc)[:500])
        await set_manifest_status(agent, "FAILED")
    finally:
        awake.cancel()


async def _drain(runner, set_manifest_status) -> None:
    while True:
        try:
            job = await claim_next_job()
        except Exception as exc:
            print(f"⚠️ [Chiron Queue] Could not claim a job: {exc}")
            return
        if not job:
            return
        await process_job(job, runner, set_manifest_status)


async def worker_loop(
    runner: Callable[[dict[str, Any]], Awaitable[Optional[dict[str, Any]]]],
    set_manifest_status: Callable[[str, str], Awaitable[None]],
) -> None:
    """Re-queue interrupted jobs, drain the queue, then wait to be woken. There is no
    polling, so an idle Chiron makes no database calls."""
    global _wake
    _wake = asyncio.Event()
    try:
        await recover_jobs()
    except Exception as exc:
        print(f"⚠️ [Chiron Queue] Startup recovery skipped: {exc}")
    await _drain(runner, set_manifest_status)
    while True:
        await _wake.wait()
        _wake.clear()
        await _drain(runner, set_manifest_status)
