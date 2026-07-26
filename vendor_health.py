import os

import httpx


async def is_vendor_circuit_healthy(vendor_name: str = "Anthropic") -> bool:
    helix_url = os.getenv("HELIX_ORACLE_URL") or os.getenv("HELIX_API_URL")
    token = os.getenv("HELIX_AUTH_TOKEN")
    if not helix_url or not token or not vendor_name.strip():
        return False

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{helix_url.rstrip('/')}/api/v1/oracle/vendor-health/{vendor_name}",
                headers={"X-Inbound-Key": token},
            )
        if response.status_code != 200:
            return False
        payload = response.json()
        return (
            str(payload.get("vendor_name", "")).lower() == vendor_name.lower()
            and payload.get("healthy") is True
        )
    except (httpx.HTTPError, TypeError, ValueError):
        return False
