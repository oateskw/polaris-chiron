import asyncio
import os
import hmac
import datetime
import psycopg
import httpx
from psycopg import sql
from typing import Any, Optional, Set
from fastapi import FastAPI, HTTPException, Header, BackgroundTasks
from pydantic import BaseModel
from dotenv import load_dotenv

from git_handler import ChironGitHandler
from patch_generator import ChironPatchGenerator
from proactive_optimizer import run_proactive_optimization_pipeline

load_dotenv()

app = FastAPI(title="Polaris Chiron GitOps Healing Agent")

# ------------------------------------------------------------------
# 🌟 Async Queue & Deduplication State
# ------------------------------------------------------------------
optimization_queue: asyncio.Queue = asyncio.Queue()
queued_agents: Set[str] = set()
worker_task: Optional[asyncio.Task] = None


async def update_manifest_optimization_status(agent_name: str, status: str) -> None:
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        print(
            f"⚠️ [Chiron Status] DATABASE_URL missing; could not mark {agent_name} as {status}."
        )
        return

    try:
        async with await psycopg.AsyncConnection.connect(db_url) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE helix_agent_manifests
                    SET optimization_status = %s
                    WHERE LOWER(agent_name) = %s;
                    """,
                    (status, agent_name.lower().strip()),
                )
            await conn.commit()
    except Exception as exc:
        print(f"⚠️ [Chiron Status] Could not mark {agent_name} as {status}: {exc}")


async def chiron_optimization_worker():
    """
    Background worker that sequentially pulls optimization requests
    from the queue and executes run_proactive_optimization_pipeline.
    """
    while True:
        payload = await optimization_queue.get()
        agent_name = payload.target_agent.lower().strip()

        try:
            await update_manifest_optimization_status(agent_name, "RUNNING")
            print(
                f"📡 [Chiron Queue Worker] Processing optimization for agent: '{payload.target_agent}'"
            )
            await run_proactive_optimization_pipeline(payload)
            await update_manifest_optimization_status(agent_name, "COMPLETED")
        except Exception as e:
            await update_manifest_optimization_status(agent_name, "FAILED")
            print(
                f"⚠️ [Chiron Queue Worker] Exception during '{payload.target_agent}' run: {e}"
            )
        finally:
            queued_agents.discard(agent_name)
            optimization_queue.task_done()


@app.on_event("startup")
async def startup_chiron_worker():
    """Launches the queue worker task on server startup."""
    global worker_task
    worker_task = asyncio.create_task(chiron_optimization_worker())


class HealingTriggerSchema(BaseModel):
    log_id: int
    target_agent: str
    error_message: str
    full_traceback: str
    metadata: Optional[dict[str, Any]] = None


class OptimizationTriggerSchema(BaseModel):
    target_agent: str
    target_file: Optional[str] = None
    focus_area: Optional[str] = "general"
    reason: Optional[str] = None
    issues_detected: Optional[list[str]] = None
    optimization_specification: Optional[str] = None
    metrics_context: Optional[dict[str, Any]] = None


async def fire_telegram_alert(error_context: str, message_text: str) -> None:
    """🚨 Sends a live notification alert to the configured Telegram channel."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        print("⚠️ [Chiron Telegram] Alert skipped: Credentials missing in environment.")
        return

    clean_context = error_context.replace("\r", "").strip()
    clean_msg = message_text.replace("\r", "").strip()

    formatted_text = (
        f"🩺 **CHIRON GITOPS UPDATE**\n\n"
        f"📅 **Timestamp:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"💡 **Context:** {clean_context}\n"
        f"📝 **Details:**\n{clean_msg}\n\n"
        f"⚙️ **Environment:** Production (Chiron Engine)"
    )

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": formatted_text, "parse_mode": "Markdown"}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                print(
                    f"⚠️ [Chiron Telegram] Rejected dispatch: {resp.status_code} - {resp.text}"
                )
            else:
                print("📨 [Chiron Telegram] Notification pushed successfully.")
    except Exception as e:
        print(f"❌ [Chiron Telegram] Connection failure to Telegram API: {e}")


async def send_telemetry_log(
    log_level: str, message: str, metadata: Optional[dict] = None
) -> None:
    """Sends structured telemetry logs back to Helix's centralized log ingestion endpoint."""
    # Read HELIX_API_URL or fall back to the live Helix production URL
    logs_base_url = (
        os.getenv("HELIX_API_URL") or "https://polaris-helix.onrender.com"
    ).rstrip("/")

    if not logs_base_url:
        print(f"📡 [Chiron Offline Log] {log_level}: {message}")
        return

    endpoint = f"{logs_base_url}/api/v1/logs"

    # Capitalize log_level to match LogIngestSchema Literal["INFO", "WARNING", "ERROR", "CRITICAL"]
    formatted_level = log_level.upper()
    if formatted_level not in ["INFO", "WARNING", "ERROR", "CRITICAL"]:
        formatted_level = "INFO"

    metadata_values = dict(metadata or {})
    traceback_text = metadata_values.pop("traceback", None)
    if formatted_level == "ERROR" and not traceback_text:
        formatted_level = "WARNING"

    payload = {
        "agent_name": "chiron",
        "log_level": formatted_level,
        "message": message,
        "metadata": (
            {"traceback": traceback_text, "extra_context": metadata_values}
            if traceback_text or metadata_values
            else None
        ),
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                endpoint,
                headers={"X-Helix-Auth-Token": os.getenv("HELIX_AUTH_TOKEN", "")},
                json=payload,
            )
            if response.status_code != 200:
                print(
                    f"⚠️ [Chiron Log] Helix rejected telemetry log: {response.status_code} - {response.text}"
                )
    except Exception as e:
        print(f"⚠️ [Chiron Log] Failed to reach Helix logging server: {e}")


async def update_session_db(
    session_id: int, status: str, extra_fields: Optional[dict] = None
) -> None:
    """Helper to update state directly in Neon Postgres."""
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        print("⚠️ DATABASE_URL not set. Skipping session DB update.")
        return

    assignments: list[sql.Composable] = [
        sql.SQL("status = %s"),
        sql.SQL("updated_at = NOW()"),
    ]
    params: list[Any] = [status]

    if extra_fields:
        for key, value in extra_fields.items():
            assignments.append(sql.SQL("{} = %s").format(sql.Identifier(key)))
            params.append(value)

    params.append(session_id)

    try:
        async with await psycopg.AsyncConnection.connect(db_url) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    sql.SQL(
                        "UPDATE chiron_healing_sessions SET {} WHERE id = %s;"
                    ).format(sql.SQL(", ").join(assignments)),
                    params,
                )
            await conn.commit()
    except Exception as e:
        print(f"❌ [Chiron DB] Failed to update healing session status: {e}")


async def run_healing_pipeline(payload: HealingTriggerSchema, session_id: int):
    """🧠 CHIRON CORE EXECUTION LOOP"""
    await send_telemetry_log(
        "INFO",
        f"Chiron initiated GitOps code diagnosis for agent: '{payload.target_agent}'",
        {"session_id": session_id, "triggering_log_id": payload.log_id},
    )

    timestamp_str = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    branch_name = f"hotfix/{payload.target_agent}-{timestamp_str}"

    git_handler = ChironGitHandler(
        target_agent=payload.target_agent, branch_name=branch_name
    )
    patch_generator = ChironPatchGenerator(sandbox_path=git_handler.sandbox_path)

    await update_session_db(session_id, "analyzed", {"git_branch_name": branch_name})

    # 1. Clone & Branch
    await send_telemetry_log(
        "INFO",
        f"Cloning target repository '{git_handler.repo_name}' to sandboxed environment...",
        {"session_id": session_id},
    )
    if not git_handler.clone_and_branch():
        error_reason = "Git cloning or branch creation failed."
        await update_session_db(session_id, "failed", {"failure_reason": error_reason})
        await send_telemetry_log(
            "ERROR", f"Abort: {error_reason}", {"session_id": session_id}
        )
        await fire_telegram_alert(
            error_context=f"❌ HEAL ABORTED: {payload.target_agent.upper()}",
            message_text=f"Git cloning or branch creation failed for session #{session_id}.",
        )
        return

    # 2. Parse Traceback
    relative_file_path, error_line = patch_generator.parse_traceback(
        payload.full_traceback
    )
    if not relative_file_path:
        error_reason = "Could not extract relative file path from traceback."
        await update_session_db(session_id, "failed", {"failure_reason": error_reason})
        await send_telemetry_log(
            "ERROR", f"Abort: {error_reason}", {"session_id": session_id}
        )
        await fire_telegram_alert(
            error_context=f"❌ HEAL ABORTED: {payload.target_agent.upper()}",
            message_text=f"Failed parsing relative file path from traceback:\n`{payload.error_message}`",
        )
        git_handler.clean_sandbox()
        return

    await update_session_db(
        session_id, "analyzed", {"target_file_path": relative_file_path}
    )

    # 3. Read raw file
    original_code = patch_generator.read_target_file(relative_file_path)
    if not original_code:
        error_reason = f"Target file '{relative_file_path}' unreadable in sandbox."
        await update_session_db(session_id, "failed", {"failure_reason": error_reason})
        await send_telemetry_log(
            "ERROR", f"Abort: {error_reason}", {"session_id": session_id}
        )
        await fire_telegram_alert(
            error_context=f"❌ HEAL ABORTED: {payload.target_agent.upper()}",
            message_text=f"Could not read target file `{relative_file_path}` in sandbox environment.",
        )
        git_handler.clean_sandbox()
        return

    # 4. Generate & Write Patch
    await send_telemetry_log(
        "INFO",
        f"Generating automated hotfix for '{relative_file_path}' at line {error_line} via Claude-3.5-Sonnet...",
        {"session_id": session_id},
    )

    patched_code = await patch_generator.generate_fix(
        file_path=relative_file_path,
        original_code=original_code,
        traceback_str=payload.full_traceback,
        error_message=payload.error_message,
        agent_name=payload.target_agent,
    )

    if not patched_code:
        error_reason = "AI Patch Generator failed to output a code fix or could not pass sandbox tests."
        await update_session_db(session_id, "failed", {"failure_reason": error_reason})
        await send_telemetry_log(
            "ERROR", f"Abort: {error_reason}", {"session_id": session_id}
        )
        await fire_telegram_alert(
            error_context=f"⚠️ PATCH FAILED: {payload.target_agent.upper()}",
            message_text=f"Claude was unable to isolate a patch, or the fix broke local sandbox unit tests for `{relative_file_path}`.",
        )
        git_handler.clean_sandbox()
        return

    patch_generator.write_patched_file(relative_file_path, patched_code)
    await update_session_db(session_id, "patch_generated")

    # 5. Commit, Push, and PR
    commit_msg = f"fix({payload.target_agent}): resolve runtime crash at line {error_line or 'unknown'}"
    await send_telemetry_log(
        "INFO",
        "Staging modifications and pushing fix branch to origin...",
        {"session_id": session_id, "commit_message": commit_msg},
    )

    if not git_handler.stage_commit_and_push(commit_msg):
        error_reason = "Git stage/commit/push command execution failed."
        await update_session_db(session_id, "failed", {"failure_reason": error_reason})
        await send_telemetry_log(
            "ERROR", f"Abort: {error_reason}", {"session_id": session_id}
        )
        await fire_telegram_alert(
            error_context=f"❌ GITOPS ERROR: {payload.target_agent.upper()}",
            message_text="Failed to stage, commit, or push changes to GitHub origin branch.",
        )
        git_handler.clean_sandbox()
        return

    pr_title = f"🩹 [Chiron Auto-Fix] Resolve Runtime Crash in `{payload.target_agent}`"
    pr_body = (
        f"### 🤖 Chiron Autonomous Healing Report\n\n"
        f"Chiron caught an active crash log coming from **{payload.target_agent}** and generated this self-correcting Pull Request.\n\n"
        f"#### 🔍 Diagnostics Details:\n"
        f"* **Triggering Log ID:** `{payload.log_id}`\n"
        f"* **Target File:** `{relative_file_path}`\n"
        f"* **Estimated Crash Line:** `{error_line}`\n"
        f"* **Error Context:** `{payload.error_message}`\n\n"
        f"#### 🚀 Action Taken:\n"
        f"Chiron cloned the target repo, checked out the branch `{branch_name}`, modified the file to bypass or patch the active exception, and pushed the code back.\n\n"
        f"_Please review the changes and run deployment tests._"
    )

    await send_telemetry_log(
        "INFO",
        "Opening GitHub Pull Request to main branch...",
        {"session_id": session_id},
    )
    pr_info = await git_handler.open_pull_request(pr_title, pr_body)

    if pr_info:
        await update_session_db(
            session_id,
            "pr_opened",
            {
                "pull_request_number": pr_info["pr_number"],
                "pull_request_url": pr_info["pr_url"],
            },
        )
        await send_telemetry_log(
            "INFO",
            f"Execution status: success. Created PR #{pr_info['pr_number']}",
            {"session_id": session_id, "pr_url": pr_info["pr_url"]},
        )

        await fire_telegram_alert(
            error_context=f"✅ HEALED SUCCESSFULLY: {payload.target_agent.upper()}",
            message_text=(
                f"Surgically patched `{relative_file_path}` on line {error_line}!\n\n"
                f"Opened PR #{pr_info['pr_number']}\n"
                f"🔗 [View GitHub Pull Request]({pr_info['pr_url']})"
            ),
        )
    else:
        error_reason = "Changes pushed but failed to initialize PR via GitHub API."
        await update_session_db(session_id, "failed", {"failure_reason": error_reason})
        await send_telemetry_log(
            "ERROR", f"Abort: {error_reason}", {"session_id": session_id}
        )
        await fire_telegram_alert(
            error_context=f"⚠️ PR OPEN FAULT: {payload.target_agent.upper()}",
            message_text="Hotfix branch was pushed, but Chiron failed to open GitHub PR automatically.",
        )

    git_handler.clean_sandbox()


@app.post("/api/v1/chiron/heal")
async def trigger_healing(
    payload: HealingTriggerSchema,
    background_tasks: BackgroundTasks,
    x_chiron_key: Optional[str] = Header(default=None),
):
    expected_key = os.getenv("HELIX_AUTH_TOKEN")
    if not expected_key or not x_chiron_key:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not hmac.compare_digest(x_chiron_key, expected_key):
        raise HTTPException(status_code=401, detail="Unauthorized handshake signature")

    db_url = os.getenv("DATABASE_URL", "")
    session_id = None
    try:
        async with await psycopg.AsyncConnection.connect(db_url) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM chiron_healing_sessions WHERE triggering_log_id = %s ORDER BY id DESC LIMIT 1;",
                    (payload.log_id,),
                )
                row = await cur.fetchone()
                if row:
                    session_id = row[0]
    except Exception as e:
        print(f"⚠️ Failed to look up session ID in DB: {e}")

    if not session_id:
        try:
            async with await psycopg.AsyncConnection.connect(db_url) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO chiron_healing_sessions (triggering_log_id, target_agent, error_message, full_traceback, status)
                        VALUES (%s, %s, %s, %s, 'triggered') RETURNING id;
                        """,
                        (
                            payload.log_id,
                            payload.target_agent,
                            payload.error_message,
                            payload.full_traceback,
                        ),
                    )
                    row = await cur.fetchone()
                    if row:
                        session_id = row[0]
                await conn.commit()
        except Exception as db_err:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to record session initiation state: {db_err}",
            )

    if session_id is None:
        raise HTTPException(
            status_code=500,
            detail="Failed to obtain the healing session ID.",
        )

    background_tasks.add_task(run_healing_pipeline, payload, session_id)

    return {
        "status": "healing_session_initiated",
        "target_agent": payload.target_agent,
        "session_id": session_id,
    }


@app.post("/api/v1/chiron/optimize")
async def trigger_optimization(
    payload: OptimizationTriggerSchema,
    x_chiron_key: Optional[str] = Header(default=None),
):
    expected_key = os.getenv("HELIX_AUTH_TOKEN")
    if not expected_key or not x_chiron_key:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not hmac.compare_digest(x_chiron_key, expected_key):
        raise HTTPException(status_code=401, detail="Unauthorized handshake signature")

    agent_key = payload.target_agent.lower().strip()

    # Prevent duplicate enqueuing if the agent is already waiting or running
    if agent_key in queued_agents:
        return {
            "status": "optimization_analysis_already_queued",
            "target_agent": payload.target_agent,
            "message": f"Optimization for agent '{payload.target_agent}' is already queued or in progress.",
        }

    await update_manifest_optimization_status(agent_key, "QUEUED")
    queued_agents.add(agent_key)
    await optimization_queue.put(payload)

    return {
        "status": "optimization_analysis_queued",
        "target_agent": payload.target_agent,
        "queue_position": optimization_queue.qsize(),
    }
