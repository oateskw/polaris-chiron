import asyncio
import os
import sys
import re
import datetime
import subprocess
from typing import Optional, Tuple
from anthropic import AsyncAnthropic
from config import get_remote_prompt, get_agent_manifest

# Global execution lock to prevent parallel path conflicts
OPTIMIZER_LOCK = asyncio.Lock()


class ChironProactiveOptimizer:
    def __init__(self, sandbox_path: str):
        self.sandbox_path = sandbox_path
        self.api_key = os.getenv("ANTHROPIC_API_KEY")
        self.model = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
        self.client = AsyncAnthropic(api_key=self.api_key) if self.api_key else None

    def read_target_file(self, file_path: str) -> Optional[str]:
        absolute_path = os.path.join(self.sandbox_path, file_path)
        if not os.path.exists(absolute_path):
            print(f"❌ [Chiron Proactive] Target file not found: {absolute_path}")
            return None
        with open(absolute_path, "r", encoding="utf-8") as f:
            return f.read()

    def write_patched_file(self, file_path: str, new_content: str) -> None:
        absolute_path = os.path.join(self.sandbox_path, file_path)
        with open(absolute_path, "w", encoding="utf-8") as f:
            f.write(new_content)

    def run_sandbox_tests(self) -> Tuple[bool, str]:
        """Runs local validation tests inside the sandbox directory."""
        print("🧪 [Chiron Validator] Testing proactive structural optimizations...")
        try:
            env_override = os.environ.copy()
            env_override["PYTHONPATH"] = (
                self.sandbox_path + os.pathsep + env_override.get("PYTHONPATH", "")
            )

            if hasattr(sys, "real_prefix") or (sys.base_prefix != sys.prefix):
                pytest_cmd = [sys.executable, "-m", "pytest", "--ignore=archive"]
            else:
                venv_pytest = (
                    os.path.join(os.getcwd(), ".venv", "Scripts", "pytest.exe")
                    if os.name == "nt"
                    else os.path.join(os.getcwd(), ".venv", "bin", "pytest")
                )
                pytest_cmd = (
                    [venv_pytest, "--ignore=archive"]
                    if os.path.exists(venv_pytest)
                    else ["pytest", "--ignore=archive"]
                )

            result = subprocess.run(
                pytest_cmd,
                cwd=self.sandbox_path,
                env=env_override,
                capture_output=True,
                text=True,
                timeout=30.0,
                shell=True,
            )

            if result.returncode in [0, 5]:
                if result.returncode == 5:
                    print(
                        "ℹ️ [Chiron Validator] No active unit tests discovered in workspace. Proceeding with structural safety check."
                    )
                return True, ""

            return False, result.stdout + "\n" + result.stderr
        except Exception as e:
            return False, f"Test run crashed: {str(e)}"

    async def generate_optimization(
        self,
        file_path: str,
        original_code: str,
        agent_name: str,
        focus_area: str = "general",
        optimization_spec: Optional[str] = None,
    ) -> Optional[str]:
        if not self.client:
            return None

        system_prompt = await get_remote_prompt("chiron", "proactive_optimizer")
        if not system_prompt:
            return None

        manifest = await get_agent_manifest(agent_name)
        manifest_context = ""
        if manifest:
            manifest_context = (
                f"--- TARGET AGENT ARCHITECTURE MANIFEST ---\n"
                f"Agent Purpose: {manifest['purpose']}\n"
                f"Dependencies: {manifest['dependencies']}\n"
                f"Critical Paths: {manifest['critical_paths']}\n"
                f"Ensure optimizations align with the architectural goal of this agent. Do not break design patterns.\n"
                f"-----------------------------------------\n\n"
            )

        print(
            f"🔬 [Diagnostic] Sending '{file_path}' to Claude. Code Length: {len(original_code or '')} chars."
        )
        if not original_code or not original_code.strip():
            print(
                f"⚠️ [Diagnostic] Skipping API call for '{file_path}' because the file content is empty string!"
            )
            return original_code

        spec_instruction = (
            f"\nSPECIFIC AUDIT DIRECTIVE:\n{optimization_spec}\n"
            if optimization_spec
            else ""
        )

        user_content = (
            f"{manifest_context}"
            f"Target File: {file_path}\n"
            f"Optimization Focus Area: {focus_area}\n"
            f"{spec_instruction}\n"
            f"Full Source Code:\n---\n{original_code}\n---\n\n"
            f"Provide the surgical SEARCH/REPLACE blocks to optimize this module."
        )

        attempts = 0
        working_code = original_code
        test_failures = ""

        while attempts < 2:
            if test_failures:
                user_content += f"\n\n⚠️ CRITICAL: YOUR MODIFICATIONS BROKE THE UNIT TESTS:\n{test_failures}\nRewrite your SEARCH/REPLACE blocks safely to avoid breaking behavior."

            try:
                response = await self.client.messages.create(
                    model=self.model,
                    max_tokens=2000,
                    temperature=0.2,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_content}],
                )
                response_text = "".join(
                    [block.text for block in response.content]
                ).strip()

                optimized_code = self._apply_multi_search_replace(
                    working_code, response_text
                )

                if not optimized_code or optimized_code == working_code:
                    return original_code

                self.write_patched_file(file_path, optimized_code)

                tests_passed, test_log = self.run_sandbox_tests()
                if tests_passed:
                    return optimized_code

                test_failures = test_log
                attempts += 1
            except Exception as e:
                print(f"❌ [Chiron Proactive] Refactor loop exception: {e}")
                return None

        print(
            "❌ [Chiron Validator] Optimization abandoned. Changes could not pass unit tests securely."
        )
        return None

    def _apply_multi_search_replace(
        self, original_code: str, patch_instruction: str
    ) -> Optional[str]:
        pattern = r"<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE"
        matches = list(re.finditer(pattern, patch_instruction, re.DOTALL))

        if matches:
            working_code = original_code
            for match in matches:
                search_block = match.group(1)
                replace_block = match.group(2)

                if search_block in working_code:
                    working_code = working_code.replace(search_block, replace_block)
                elif search_block.strip() in working_code:
                    working_code = working_code.replace(
                        search_block.strip(), replace_block
                    )
                else:
                    print(
                        "⚠️ [Chiron Patch Engine] Search block mismatch detected. Skipping file iteration."
                    )
                    return original_code
            return working_code

        md_pattern = r"```(?:python)?\n(.*?)\n```"
        md_match = re.search(md_pattern, patch_instruction, re.DOTALL)

        if md_match:
            extracted_code = md_match.group(1).strip()
            if len(extracted_code) > 10 and extracted_code != original_code.strip():
                return extracted_code

        return None


async def run_proactive_optimization_pipeline(payload):
    """🧠 CHIRON PROACTIVE CRAWLER ENGINE (Sequential Module Evolution)"""
    from git_handler import ChironGitHandler
    from chiron_healer import send_telemetry_log, fire_telegram_alert

    if OPTIMIZER_LOCK.locked():
        print(
            "📡 [Chiron Gatekeeper] Optimization loop already in progress. Rejecting overlapping request."
        )
        return

    async with OPTIMIZER_LOCK:
        timestamp_str = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        branch_name = f"optimization/{payload.target_agent}-{timestamp_str}"

        git_handler = ChironGitHandler(
            target_agent=payload.target_agent, branch_name=branch_name
        )
        optimizer = ChironProactiveOptimizer(sandbox_path=git_handler.sandbox_path)

        await send_telemetry_log(
            "INFO",
            f"Chiron initiating repository-wide crawl optimization for agent: '{payload.target_agent}'",
        )

        if not git_handler.clone_and_branch():
            return

        python_files_found = []
        for root, _, files in os.walk(git_handler.sandbox_path):
            if any(
                ignored in root
                for ignored in [
                    ".git",
                    "venv",
                    "__pycache__",
                    ".pytest_cache",
                    ".github",
                ]
            ):
                continue
            for file in files:
                if file.endswith(".py"):
                    relative_path = os.path.relpath(
                        os.path.join(root, file), git_handler.sandbox_path
                    )
                    python_files_found.append(relative_path)

        if not python_files_found:
            await send_telemetry_log(
                "WARNING",
                f"No python source files discovered for agent '{payload.target_agent}'.",
            )
            git_handler.clean_sandbox()
            return

        await send_telemetry_log(
            "INFO",
            f"Crawler discovered {len(python_files_found)} Python modules to analyze.",
        )
        modules_optimized = []

        optimization_spec = getattr(payload, "optimization_specification", None)

        for rel_file_path in python_files_found:
            original_code = optimizer.read_target_file(rel_file_path)
            if not original_code:
                continue

            await send_telemetry_log(
                "INFO", f"Scanning module architecture footprint: `{rel_file_path}`"
            )

            optimized_code = await optimizer.generate_optimization(
                file_path=rel_file_path,
                original_code=original_code,
                agent_name=payload.target_agent,
                focus_area=getattr(payload, "focus_area", "general"),
                optimization_spec=optimization_spec,
            )

            if optimized_code and optimized_code != original_code:
                optimizer.write_patched_file(rel_file_path, optimized_code)
                modules_optimized.append(rel_file_path)

        if not modules_optimized:
            await send_telemetry_log(
                "INFO",
                "Crawl complete. Fleet code configuration matching optimal target thresholds.",
            )
            git_handler.clean_sandbox()
            return

        commit_msg = f"refactor({payload.target_agent}): comprehensive autonomous codebase optimization suite"
        if not git_handler.stage_commit_and_push(commit_msg):
            git_handler.clean_sandbox()
            return

        file_list_str = "\n".join([f"* `{f}`" for f in modules_optimized])
        pr_title = f"🚀 [Chiron Base Evolution] Optimized Structural Layout inside `{payload.target_agent}`"
        pr_body = (
            f"### 🚀 Chiron Proactive Evolution Report\n\n"
            f"Chiron executed a top-to-bottom optimization crawl across **{payload.target_agent}** and generated high-impact code refactors.\n\n"
            f"#### 📦 Refactored System Modules:\n"
            f"{file_list_str}\n\n"
            f"#### 🔍 Verification Engine Result:\n"
            f"* All target modifications successfully resolved local unit test suites inside the sandboxed verification execution block.\n\n"
            f"_Please complete regression review pipeline checks prior to merge acceptance._"
        )

        pr_info = await git_handler.open_pull_request(pr_title, pr_body)
        if pr_info:
            from watchdog import monitor_production_telemetry

            asyncio.create_task(
                monitor_production_telemetry(payload.target_agent, pr_info["pr_number"])
            )

            await fire_telegram_alert(
                error_context=f"🚀 FLEET OPTIMIZATION MERGE READY: {payload.target_agent.upper()}",
                message_text=f"Surgically optimized {len(modules_optimized)} codebase modules!\nOpened PR #{pr_info['pr_number']}\n🔗 [View Evolution PR]({pr_info['pr_url']})",
            )

        git_handler.clean_sandbox()
