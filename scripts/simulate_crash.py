import os
import requests
from dotenv import load_dotenv

load_dotenv()

# Configuration
HELIX_API_URL = os.getenv("HELIX_API_URL", "https://polaris-helix.onrender.com").rstrip(
    "/"
)
HELIX_AUTH_TOKEN = os.getenv("HELIX_AUTH_TOKEN", "")

# Constructing a realistic traceback pointing to a file in your beacon repo
mock_traceback = """
Traceback (most recent call last):
  File "/app/polaris-beacon/beacon_agent.py", line 42, in process_leads
    average_lead_score = total_score / active_leads
ZeroDivisionError: division by zero
"""

# Format the payload to match Helix's LogIngestSchema
payload = {
    "agent_name": "beacon",
    "log_level": "ERROR",
    "message": "division by zero during active lead scoring sequence",
    "metadata": {"traceback": mock_traceback, "active_leads": 0, "total_score": 0},
}

headers = {
    "X-Helix-Auth-Token": HELIX_AUTH_TOKEN,
    "Content-Type": "application/json",
}


def trigger_simulation():
    if not HELIX_AUTH_TOKEN:
        raise RuntimeError("HELIX_AUTH_TOKEN is required to run the crash simulation")

    print(f"📡 Sending mock crash log to Helix at: {HELIX_API_URL}/api/v1/logs")
    try:
        response = requests.post(
            f"{HELIX_API_URL}/api/v1/logs", json=payload, headers=headers, timeout=10
        )
        if response.status_code == 200:
            data = response.json()
            print("✅ Crash successfully ingested by Helix!")
            print(f"📋 Logged ID: {data.get('logged_id')}")
            print(
                "\n🔮 Now, watch your Telegram channel and Neon DB. The following should happen:"
            )
            print(
                "1. Helix parses the error, identifies the crash, and triggers Chiron."
            )
            print(
                "2. Chiron writes state updates ('triggered' -> 'analyzed') to your Neon DB."
            )
            print(
                "3. Chiron posts real-time telemetry logs back to Helix, forwarding to Telegram."
            )
            print(
                "4. Chiron clones polaris-beacon, has Claude-3.5-Sonnet patch 'beacon_agent.py', and opens a PR!"
            )
        else:
            print(
                f"❌ Helix rejected the trigger payload ({response.status_code}): {response.text}"
            )
    except Exception as e:
        print(f"💥 Failed to reach Helix: {e}")


if __name__ == "__main__":
    trigger_simulation()
