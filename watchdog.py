import os
import asyncio
import httpx


async def monitor_production_telemetry(
    agent_name: str, pr_number: int, monitoring_window_minutes: int = 30
):
    """
    Monitors production trace profiles for a window following deployment actions.
    """
    from chiron_healer import send_telemetry_log, fire_telegram_alert

    await send_telemetry_log(
        "INFO",
        f"Watchdog initialized. Monitoring production health metrics for '{agent_name}' tracking PR #{pr_number}.",
    )

    logs_base_url = os.getenv("HELIX_API_URL", "").rstrip("/")
    if not logs_base_url:
        print("📡 [Chiron Watchdog] Abandoning loop: HELIX_API_URL missing.")
        return

    check_interval_seconds = 60
    total_checks = int((monitoring_window_minutes * 60) / check_interval_seconds)

    initial_error_count = await fetch_live_error_count(agent_name, logs_base_url)

    for check in range(total_checks):
        await asyncio.sleep(check_interval_seconds)
        current_error_count = await fetch_live_error_count(agent_name, logs_base_url)

        # Anomaly threshold: If errors jump by more than 5 since deployment
        if current_error_count > (initial_error_count + 5):
            await send_telemetry_log(
                "CRITICAL",
                f"Watchdog Alert: Post-deployment anomaly trend caught inside '{agent_name}' tracing merge actions.",
            )
            await fire_telegram_alert(
                error_context=f"🚨 TELEMETRY REGRESSION CAPTURED: {agent_name.upper()}",
                message_text=(
                    f"Anomaly spike recognized within post-deployment monitoring window for PR #{pr_number}!\n"
                    f"Immediate human operator analysis or automated fallback commit triage recommended."
                ),
            )
            return

    await send_telemetry_log(
        "INFO",
        f"Watchdog verification cleared. Production state metrics stable for '{agent_name}' post-PR merge check.",
    )


async def fetch_live_error_count(agent_name: str, logs_url: str) -> int:
    """Queries centralized Helix log analytics infrastructure for active error counts."""
    endpoint = f"{logs_url}/api/v1/telemetry/errors-count?agent={agent_name.lower()}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(endpoint)
            if resp.status_code == 200:
                return int(resp.json().get("error_count", 0))
    except Exception as e:
        print(f"⚠️ [Watchdog Log Pull Failed] Exception calling metrics engine: {e}")
    return 0
