# Polaris Chiron

Chiron is the mechanic of the Polaris agent mesh. It is a FastAPI service that changes the code of the other agents when [Helix](https://github.com/oateskw/polaris-helix) asks it to: it clones the target agent's repository, asks Claude for a code change, runs the repository's tests, and opens a GitHub pull request. **Chiron never merges and never deploys.** The pull request is the human approval gate.

> Part of the Polaris workspace. Siblings: [polaris-helix](https://github.com/oateskw/polaris-helix) (orchestrator, the "boss" that instructs Chiron), [polaris-beacon](https://github.com/oateskw/polaris-beacon), [polaris-vanguard](https://github.com/oateskw/polaris-vanguard), [polaris-vector](https://github.com/oateskw/polaris-vector), [polaris-aura](https://github.com/oateskw/polaris-aura), [polaris-meridian](https://github.com/oateskw/polaris-meridian), [polaris_website](https://github.com/oateskw/polaris_website).

## Contents

- [What Chiron does](#what-chiron-does)
- [How a request flows](#how-a-request-flows)
- [HTTP API](#http-api)
- [How Helix uses Chiron](#how-helix-uses-chiron)
- [Safety model](#safety-model)
- [Repository layout](#repository-layout)
- [Getting started](#getting-started)
- [Configuration](#configuration)
- [Database dependencies](#database-dependencies)
- [Running on Render](#running-on-render)
- [Testing](#testing)
- [Known issues](#known-issues)

## What Chiron does

Chiron has two jobs, each exposed as one endpoint:

| Job | Endpoint | Trigger | Output |
| --- | --- | --- | --- |
| **Heal** (reactive) | `POST /api/v1/chiron/heal` | An agent logged an `ERROR` with a Python traceback | A `hotfix/<agent>-<timestamp>` branch and a PR that patches the crashing file |
| **Optimize** (proactive) | `POST /api/v1/chiron/optimize` | Helix's performance audit found an agent below its KPI thresholds | An `optimization/<agent>-<timestamp>` branch and a PR that refactors that agent's modules |

In both cases Claude proposes the change as `SEARCH/REPLACE` blocks, Chiron applies them in a throwaway clone, runs `pytest` in that clone, and only pushes and opens a PR if the tests pass.

## How a request flows

### Heal

1. Helix posts the failing log's `log_id`, `target_agent`, `error_message` and `full_traceback`.
2. Chiron creates (or reuses) a row in `chiron_healing_sessions`, unique per `log_id`, and answers immediately with the session ID. The rest runs as a background task.
3. It clones the agent's repository and creates the `hotfix/...` branch.
4. It reads the last `File "...", line N` from the traceback to find the crashing file, and sends Claude a 30-line window around the line, plus the error and the agent's manifest.
5. Claude replies with one `SEARCH/REPLACE` block. Chiron applies it, runs `pytest` in the clone, and retries once with the test output if the tests fail.
6. If tests pass, it commits as `Chiron Auto-Healer`, force-pushes the branch, and opens a PR against `main`.
7. Session status moves `triggered` -> `analyzed` -> `patch_generated` -> `pr_opened`, or `failed` with a `failure_reason`.

### Optimize

1. Helix posts `target_agent` and, optionally, `optimization_specification` (free text), `focus_area`, and context fields.
2. Chiron queues the request (one at a time, one per agent) and answers with its queue position. A worker marks the agent's manifest `QUEUED` -> `RUNNING` -> `COMPLETED` or `FAILED`.
3. It clones the repository, lists every `.py` file (skipping `.git`, `venv`, `__pycache__`, `.pytest_cache`, `.github`), and narrows the list to files whose name contains the agent name, or to the manifest's `target_files` when set.
4. For each file, Claude gets the full source, the agent's manifest, the focus area, and the audit directive, and returns `SEARCH/REPLACE` blocks. Chiron applies them, runs `pytest`, and retries once with the failure output.
5. If any module changed, it commits, force-pushes the branch, opens a PR, starts the 30-minute watchdog, and sends a Telegram alert. If nothing changed, it only sends an "audit complete, no PR" alert.

### After a PR is opened

A watchdog polls Helix's `GET /api/v1/telemetry/errors-count?agent=<name>` every 60 seconds for 30 minutes. If the agent's error count rises by more than 5 over the starting count, Chiron logs a `CRITICAL` event to Helix and sends a Telegram alert. It does not roll anything back. Note that the window starts when the PR is opened, not when it is merged.

## HTTP API

Both routes require the header `X-Chiron-Key` equal to `HELIX_AUTH_TOKEN`, compared in constant time. A missing or wrong key returns 401, and the service refuses everything if `HELIX_AUTH_TOKEN` is unset.

### `POST /api/v1/chiron/heal`

```json
{
  "log_id": 1234,
  "target_agent": "beacon",
  "error_message": "division by zero during active lead scoring",
  "full_traceback": "Traceback (most recent call last):\n  File \"/app/polaris-beacon/beacon_agent.py\", line 42, in process_leads\n ...",
  "metadata": {}
}
```

Response: `{"status": "healing_session_initiated", "target_agent": "beacon", "session_id": 17}`

### `POST /api/v1/chiron/optimize`

Only `target_agent` is required.

```json
{
  "target_agent": "nova",
  "focus_area": "general",
  "optimization_specification": "Reduce wasted MoltSet calls: skip domains already harvested this week.",
  "target_file": null,
  "reason": null,
  "issues_detected": [],
  "metrics_context": {}
}
```

| Field | Used by Chiron? |
| --- | --- |
| `target_agent` | Yes: selects the repository, branch name and file filter. |
| `optimization_specification` | Yes: injected into the prompt as the "specific audit directive". This is the field for plain-language instructions. |
| `focus_area` | Yes: included in the prompt (default `general`). |
| `reason`, `issues_detected`, `metrics_context` | Yes: appended to the audit directive, after `optimization_specification`. |
| `target_file` | Yes: restricts the crawl to the matching file. If nothing matches, Chiron falls back to manifest discovery. |

Responses:

- `{"status": "optimization_analysis_queued", "target_agent": "nova", "queue_position": 1}`
- `{"status": "optimization_analysis_already_queued", ...}` if that agent is already waiting or running (HTTP 200 both times).

There are no health, status or approval routes. The queue and the dedupe set live in memory, so a restart empties them.

## How Helix uses Chiron

- **Reactive:** when an agent logs an `ERROR` with a traceback, Helix's log analyzer posts to `/heal` (`services/telemetry.py` in Helix). It does not wait for the result.
- **Proactive:** Helix audits its agents every 30 minutes against KPI thresholds (for example Vanguard open rate at least 35% and bounce rate at most 3%, Nova at least 10 records per run). A miss posts to `/optimize` with the findings in `reason`, `issues_detected`, `optimization_specification` and `metrics_context`. Helix waits at least 7 days between optimizations of the same agent (`CHIRON_OPTIMIZATION_COOLDOWN_HOURS` on the Helix side) and skips agents already `QUEUED` or `RUNNING`.
- **Guardrails on the Helix side:** nothing is sent unless the Anthropic vendor circuit is healthy.
- **Reporting back:** Chiron posts progress logs as agent `chiron` to Helix's `POST /api/v1/logs`, and sends Telegram alerts.

Planned direction: Helix, acting as the business's right-hand agent, will decide *what an agent should achieve* from business results (replies, meetings, revenue) and hand Chiron that goal as a written brief in `optimization_specification`. Chiron decides and implements the code change, and the owner approves it by reviewing the PR. The fixes under [Known issues](#known-issues) come first.

## Safety model

- **Pull request is the gate.** Chiron pushes only to its own new branches (`hotfix/...`, `optimization/...`) and opens PRs against `main`. It never merges.
- **Branch protection is not enforced by Chiron.** To make review mandatory, turn on branch protection for `main` in every agent repository (require a PR and at least one approval), and do not let Render auto-deploy anything that has not been merged through that review.
- **Tests gate the PR.** Changes are only pushed if `pytest` passes in the clone within 120 seconds. A repository with no tests also passes (pytest exit code 5), so a PR on an untested agent has had no automated check.
- **Anthropic circuit.** Before each Claude call Chiron asks Helix whether the Anthropic circuit is healthy (`GET /api/v1/oracle/vendor-health/Anthropic`) and stops if not, so it cannot burn an exhausted budget.
- **Scope.** It edits `.py` files only, and the token you give it decides which repositories it can reach.
- **Secrets.** The GitHub token is embedded in the clone URL inside a temporary directory that is deleted afterwards.

## Repository layout

```
chiron_healer.py        FastAPI app, /heal and /optimize routes, queue worker, heal pipeline,
                        Telegram alerts, telemetry to Helix, session and manifest status updates
proactive_optimizer.py  Optimize pipeline: crawl, per-file Claude refactor, multi-block patching, tests
patch_generator.py      Heal pipeline: traceback parsing, 30-line context, single-block patching, tests
git_handler.py          Clone, branch, commit, force-push to the fix branch, open PR via GitHub API
config.py               Reads prompts (helix_agent_prompts) and agent manifests from Postgres
vendor_health.py        Asks Helix whether the Anthropic circuit is healthy
watchdog.py             30-minute post-PR error monitor
scripts/simulate_crash.py  Posts a fake crash log to Helix (see warning below)
tests/                  Unit tests for telemetry, status tracking and the vendor circuit
```

## Getting started

Requirements: Python 3.10+, `git` on the PATH, a Postgres database shared with Helix, an Anthropic API key, and a GitHub token.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn chiron_healer:app --host 0.0.0.0 --port 8001
```

Create a GitHub fine-grained token limited to your agent repositories, with **Contents: read and write** and **Pull requests: read and write**.

## Configuration

Set these as environment variables (or in a git-ignored `.env`).

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `HELIX_AUTH_TOKEN` | yes | none | Shared secret. Must match the value on Helix; incoming requests send it as `X-Chiron-Key`, and Chiron sends it back to Helix. |
| `DATABASE_URL` | yes | none | Same Postgres database as Helix. |
| `ANTHROPIC_API_KEY` | yes | none | Without it no patch can be generated. |
| `ANTHROPIC_MODEL` | no | `claude-3-5-sonnet-latest` | Model for patches. Set a current model ID explicitly. |
| `GITHUB_TOKEN` | yes | none | Clone, push, and open PRs. |
| `GITHUB_ORG_OR_USER` | yes | none | Owner of the repositories (for example `oateskw`). |
| `HELIX_API_URL` | no | `https://polaris-helix.onrender.com` | Where logs, the error-count query and the circuit check go. |
| `HELIX_ORACLE_URL` | no | `HELIX_API_URL` | Override for the circuit check only. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | no | none | Alert destination. |
| `TELEGRAM_CRITICAL_BOT_TOKEN`, `TELEGRAM_CRITICAL_CHAT_ID` | no | fall back to the two above | Separate critical channel. |
| `TELEGRAM_DIRECT_SEND_ENABLED` | no | `false` | When `false`, only critical-looking alerts are sent. |
| `TELEGRAM_DIRECT_CRITICAL_ONLY` | no | `true` | Works with the setting above: sends alerts that mention critical, error, failed, exception or traceback. |

## Database dependencies

Chiron reads and writes tables that Helix owns. It does not create them.

| Table | What Chiron needs |
| --- | --- |
| `helix_agent_prompts` | Rows `('chiron', 'reactive_healer')` and `('chiron', 'proactive_optimizer')`: the system prompts for the two jobs. **If a row is missing, Chiron silently skips generating a patch.** |
| `helix_agent_manifests` | Per agent: `purpose`, `dependencies`, `critical_paths`, and optionally `target_repository` and `target_files`. Chiron updates `optimization_status`, `last_optimized_at` and `last_optimization_pr_url`. |
| `chiron_healing_sessions` | One row per heal: `status`, `git_branch_name`, `target_file_path`, `pull_request_number`, `pull_request_url`, `failure_reason`, `updated_at`. |

Helix's `init_db.py` creates a minimal version of the manifest and sessions tables. The prompt rows and the manifest documentation columns come from the live database, so keep those when rebuilding.

## Running on Render

- **Build command:** `pip install -r requirements.txt`
- **Start command:** `uvicorn chiron_healer:app --host 0.0.0.0 --port $PORT`
- **Environment:** the variables above. Set `PYTHON_VERSION` to a full version such as `3.12.8`.
- The service needs `git` (present on Render's Python runtime) and clones into the OS temp directory.
- On a free instance the service sleeps when idle. The optimize queue and the 30-minute watchdog are in memory, so a sleep or restart drops queued work and any running watchdog. Helix wakes Chiron when it calls it, but a request received just before a sleep is not retried.
- Each agent's tests run with the dependencies installed in *Chiron's* environment. If an agent's tests import a package Chiron does not have, its tests fail and no PR is opened.

## Testing

```bash
pip install -r requirements.txt
pytest
```

Current tests cover telemetry and queue status tracking (`tests/test_chiron_telemetry.py`) and the vendor circuit check (`tests/test_vendor_circuit.py`). There are no tests for the git handler, patch application or the two pipelines.

`scripts/simulate_crash.py` posts a fake `ZeroDivisionError` for the `beacon` agent to Helix. **Against production this triggers a real heal run and a real PR on `polaris-beacon`.** Point `HELIX_API_URL` at a test instance, or close the PR afterwards.

## Known issues

Fixed in the latest commit: the Linux test gate (`shell=True` removed, timeout raised to 120 seconds), the optimize path now sends `reason`, `issues_detected` and `metrics_context` to the model and honours `target_file`, the manifest lookup now runs (it opens its own connection from `DATABASE_URL`), the fallback repository map, optimize PRs are recorded in `helix_agent_manifests.last_optimization_pr_url`, and a non-ASCII `X-Chiron-Key` returns 401.

Still open:

1. **In-memory state.** The queue, the dedupe set and the watchdog are lost on restart or sleep.
2. **Force-push.** Branch names include a timestamp, so collisions are unlikely, but pushes use `--force`.
3. **Pinned, older dependencies.** `anthropic==0.28.0` and `fastapi==0.111.0` are pinned in `requirements.txt`, and `@app.on_event("startup")` is deprecated in current FastAPI.
4. **Missing prompts.** If the `helix_agent_prompts` rows for Chiron are missing, Chiron skips patching. Helix's `python init_db.py` loads them from `seed_data/`.
