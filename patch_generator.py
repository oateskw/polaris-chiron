import os
import re
from typing import Optional, Tuple
from anthropic import Anthropic


class ChironPatchGenerator:
    def __init__(self, sandbox_path: str):
        self.sandbox_path = sandbox_path
        self.api_key = os.getenv("ANTHROPIC_API_KEY")
        self.model = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
        self.client = Anthropic(api_key=self.api_key) if self.api_key else None

    def parse_traceback(
        self, traceback_str: str
    ) -> Tuple[Optional[str], Optional[int]]:
        """
        Parses a standard Python traceback to extract the relative file path
        and the last line number involved in the crash.
        """
        # Look for python traceback lines: File "/tmp/chiron_sandbox/.../file.py", line 123, in function
        # We target the last file frame in the stack trace as it is typically the origin of the crash
        matches = re.findall(r'File "([^"]+)", line (\d+)', traceback_str)
        if not matches:
            return None, None

        full_file_path, line_no = matches[-1]

        # We want the relative path inside our cloned repository
        # This strips out absolute system paths (e.g. /tmp/chiron_sandbox/polaris-beacon/)
        repo_name = os.path.basename(self.sandbox_path.rstrip("/"))
        relative_path_match = re.search(rf"{repo_name}/(.+)$", full_file_path)

        if relative_path_match:
            return relative_path_match.group(1), int(line_no)

        # Fallback to the raw filename if extraction fails
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

    def generate_fix(
        self, file_path: str, original_code: str, traceback_str: str, error_message: str
    ) -> Optional[str]:
        """Sends the context to Claude to generate a clean code replacement."""
        if not self.client:
            print("❌ [Chiron Patch] Anthropic API client not initialized.")
            return None

        system_prompt = (
            "You are Chiron, the advanced autonomous code-healing agent for the Polaris ecosystem.\n"
            "Your sole objective is to analyze a Python traceback, identify the root cause of the crash, "
            "and modify the target source file to resolve the exception with surgical precision.\n\n"
            "CRITICAL OPERATIONAL RULES:\n"
            "1. SURGICAL PRECISION: Do NOT rewrite, refactor, or clean up functional code that is unrelated to the crash. "
            "Preserve all existing logic, variables, and comments.\n"
            "2. DEFENSIVE PROGRAMMING: Implement robust error handling (e.g., try-except blocks, null checks, or default fallback values) "
            "around the failing line to prevent the agent from completely stalling in the future.\n"
            "3. IMPORT INTEGRITY: Ensure you do not accidentally remove or break any existing import statements required by the rest of the file.\n"
            "4. NO EXPLANATIONS: Do not explain the fix, do not write introductory prose, and do not provide post-fix commentary.\n"
            "5. RAW PYTHON ONLY: Return ONLY valid, executable Python code. "
            "Do NOT wrap your response in markdown code blocks (such as ```python ... ```). Output the code raw."
        )

        user_content = (
            f"Target File: {file_path}\n\n"
            f"Error Message:\n{error_message}\n\n"
            f"Traceback Details:\n{traceback_str}\n\n"
            f"Original Source Code:\n---\n{original_code}\n---\n\n"
            f"Please generate the fully functional, updated Python code that resolves the crash."
        )

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4000,
                temperature=0.0,  # Strict temperature for deterministic code generation
                system=system_prompt,
                messages=[{"role": "user", "content": user_content}],
            )

            # Combine block contents if spread out
            patched_code = "".join([block.text for block in response.content]).strip()

            # Defensive clean up in case Claude outputs markdown wrappers anyway
            if patched_code.startswith("```"):
                patched_code = re.sub(r"^```python\s*", "", patched_code)
                patched_code = re.sub(r"^```\s*", "", patched_code)
                patched_code = re.sub(r"\s*```$", "", patched_code)

            return patched_code.strip()

        except Exception as e:
            print(f"❌ [Chiron Patch] LLM API Call Failed: {e}")
            return None
