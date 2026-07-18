# config.py
import os
import psycopg
from typing import Optional


async def get_remote_prompt(agent_name: str, prompt_key: str) -> Optional[str]:
    """Dynamically fetches a system prompt from NeonDB."""
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        print("⚠️ [Chiron Config] DATABASE_URL not set. Cannot fetch prompt.")
        return None

    try:
        async with await psycopg.AsyncConnection.connect(db_url) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT system_prompt 
                    FROM helix_agent_prompts 
                    WHERE agent_name = %s AND prompt_key = %s 
                    ORDER BY updated_at DESC LIMIT 1;
                    """,
                    (agent_name, prompt_key),
                )
                row = await cur.fetchone()
                if row:
                    return row[0]
                else:
                    print(
                        f"⚠️ [Chiron Config] Prompt not found for {agent_name}::{prompt_key}"
                    )
                    return None
    except Exception as e:
        print(f"❌ [Chiron DB] Error fetching prompt from NeonDB: {e}")
        return None


async def get_agent_manifest(agent_name: str) -> Optional[dict]:
    """Dynamically fetches the operational manifest/purpose of a target agent from NeonDB."""
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        print("⚠️ [Chiron Config] DATABASE_URL not set. Cannot fetch manifest.")
        return None

    try:
        async with await psycopg.AsyncConnection.connect(db_url) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT purpose, dependencies, critical_paths 
                    FROM helix_agent_manifests 
                    WHERE agent_name = %s;
                    """,
                    (agent_name.lower(),),
                )
                row = await cur.fetchone()
                if row:
                    return {
                        "purpose": row[0],
                        "dependencies": row[1],
                        "critical_paths": row[2],
                    }
                else:
                    print(
                        f"⚠️ [Chiron Config] No manifest documentation found for agent: {agent_name}"
                    )
                    return None
    except Exception as e:
        print(f"❌ [Chiron DB] Error fetching agent manifest: {e}")
        return None
