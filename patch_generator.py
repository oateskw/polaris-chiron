import os
import re
import subprocess
from typing import Optional, Tuple
from anthropic import AsyncAnthropic
from config import get_remote_prompt, get_agent_manifest
import vendor_health


class ChironPatchGenerator:
    def __init__(self, sandbox_path: str):
        self.sandbox_path = sandbox_path
        self.api_key = os.getenv("ANTHROPIC_API_KEY")
        self.model = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
        self.client = AsyncAnthropic(api_key=self.api_key) if self.api_key else None

    def parse_traceback(
        self, traceback_str: str
    ) -> Tuple[Optional[str], Optional[int]]:
        """
        Parses a standard Python traceback to extract the relative file path
        and the last line number involved in the crash.
        """
        matches = re.findall(r'File "([^"]+)", line (\d+)', traceback_str)
        if not matches:
            return None, None

        full_file_path, line_no = matches[-1]

        # Extract relative path inside our cloned repository
        repo_name = os.path.basename(self.sandbox_path.rstrip("/"))
        relative_path_match = re.search(rf"{repo_name}/(.+)$", full_file_path)

        if relative_path_match:
            return relative_path_match.group(1), int(line_no)

        return os.path.basename(full_file_path), int(line_no)

    def read_target_file(self, file_path: str) -> Optional[str]:
        """Reads the full contents of the target file to be patched."""
        absolute_path = os.path.join(self.sandbox_path, file_path)
        if not os.path.exists(absolute_path):
            print(f"❌ [Chiron Patch] Target file not found: {absolute_path}")
            return None
        with open(absolute_path, "r", encoding="utf-8") as f:
            return f.read()

    def write_patched_file(self, file_path: str, new_content: str) -> None:
        """Writes the updated code block back to the target file."""
        absolute_path = os.path.join(self.sandbox_path, file_path)
        with open(absolute_path, "w", encoding="utf-8") as f:
            f.write(new_content)

    def run_sandbox_tests(self) -> Tuple[bool, str]:
        """🚀 STEP 2: Runs local validation tests inside the sandbox directory."""
        print("🧪 [Chiron Validator] Running local test suite to verify code sanity...")
        try:
            # We assume pytest is used; falls back to standard unittest if desired
            result = subprocess.run(
                ["pytest"],
                cwd=self.sandbox_path,
                capture_output=True,
                text=True,
                timeout=30.0,
                shell=True,
            )
            if result.returncode == 0:
                print("✅ [Chiron Validator] All sandbox tests passed successfully!")
                return True, ""
            else:
                print(
                    "❌ [Chiron Validator] Tests failed. Capturing regression errors."
                )
                return False, result.stdout + "\n" + result.stderr
        except Exception as e:
            return False, f"Test framework execution crashed: {str(e)}"

    async def generate_fix(
        self,
        file_path: str,
        original_code: str,
        traceback_str: str,
        error_message: str,
        agent_name: str,
    ) -> Optional[str]:
        if not self.client:
            return None

        _, line_no = self.parse_traceback(traceback_str)
        line_no = line_no or 1

        lines = original_code.splitlines()
        start_line = max(0, line_no - 15)
        end_line = min(len(lines), line_no + 15)
        context_window = "\n".join(lines[start_line:end_line])

        system_prompt = await get_remote_prompt("chiron", "reactive_healer")
        if not system_prompt:
            return None

        # 🚀 STEP 1: Inject Agent Manifest Context
        manifest = await get_agent_manifest(agent_name)
        manifest_context = ""
        if manifest:
            manifest_context = (
                f"--- TARGET AGENT ARCHITECTURE MANIFEST ---\n"
                f"Agent Identity/Purpose: {manifest['purpose']}\n"
                f"Known Architecture Dependencies: {manifest['dependencies']}\n"
                f"Critical System Paths: {manifest['critical_paths']}\n"
                f"Keep this agent's ultimate design purpose in mind when patching its bugs.\n"
                f"-----------------------------------------\n\n"
            )

        user_content = (
            f"{manifest_context}"
            f"Target File: {file_path}\n"
            f"Crashing Line Number: {line_no}\n\n"
            f"Error Message:\n{error_message}\n\n"
            f"Traceback Details:\n{traceback_str}\n\n"
            f"30-Line Slice Around Error:\n---\n{context_window}\n---\n\n"
            f"Provide the tiny SEARCH/REPLACE block to resolve the bug."
        )

        attempts = 0
        working_code = original_code
        test_failures = ""

        # Loop up to 2 times to self-correct if it generates an update that breaks tests
        while attempts < 2:
            if test_failures:
                user_content += f"\n\n⚠️ YOUR PREVIOUS PATCH FAILED TESTS WITH THIS ERROR:\n{test_failures}\nAdjust your fix to satisfy the test requirements."

            try:
                if not await vendor_health.is_vendor_circuit_healthy():
                    print("🛑 [Chiron Patch] Anthropic circuit is unavailable.")
                    return None

                response = await self.client.messages.create(
                    model=self.model,
                    max_tokens=600,
                    temperature=0.0,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_content}],
                )
                response_text = "".join(
                    [block.text for block in response.content]
                ).strip()
                patched_code = self._apply_search_replace(working_code, response_text)

                if not patched_code:
                    return None

                # Temp-write to sandbox to run tests against it
                self.write_patched_file(file_path, patched_code)

                # Run the newly built step 2 validation test
                tests_passed, test_log = self.run_sandbox_tests()
                if tests_passed:
                    return patched_code

                # If tests failed, store error and loop to self-correct
                test_failures = test_log
                attempts += 1

            except Exception as e:
                print(f"❌ [Chiron Patch] LLM Self-Correction Cycle Failed: {e}")
                return None

        print(
            "❌ [Chiron Validator] Self-correction loop exhausted. Code changes failed tests."
        )
        return None

    def _apply_search_replace(
        self, original_code: str, patch_instruction: str
    ) -> Optional[str]:
        """Parses the Git-style conflict blocks and swaps them into the original code."""
        # Extract content between markers
        pattern = r"<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE"
        match = re.search(pattern, patch_instruction, re.DOTALL)

        if not match:
            print(
                "⚠️ [Chiron Patch] Could not parse SEARCH/REPLACE block from response. Output was:"
            )
            print(patch_instruction)
            return None

        search_block = match.group(1)
        replace_block = match.group(2)

        # Surgical swap
        if search_block in original_code:
            print(
                "🎯 [Chiron Patch] Found exact match in target file. Executing surgical replacement..."
            )
            return original_code.replace(search_block, replace_block)

        # Fallback: Strip whitespaces from ends to find matches with minor line spacing differences
        if search_block.strip() in original_code:
            print(
                "🎯 [Chiron Patch] Found whitespace-flexible match in target file. Executing replacement..."
            )
            return original_code.replace(search_block.strip(), replace_block)

        print(
            "❌ [Chiron Patch] SEARCH block does not match any code in the original file."
        )
        return None
