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

    def generate_fix(
        self, file_path: str, original_code: str, traceback_str: str, error_message: str
    ) -> Optional[str]:
        """Sends a small line window context to Claude to generate a surgical swap."""
        if not self.client:
            print("❌ [Chiron Patch] Anthropic API client not initialized.")
            return None

        # 1. Parse the crashing line number from the traceback
        _, line_no = self.parse_traceback(traceback_str)
        if not line_no:
            print(
                "⚠️ [Chiron Patch] Could not parse line number from traceback. Defaulting to full file."
            )
            line_no = 1

        # 2. Extract a 30-line window around the crash to prevent Claude from getting overwhelmed
        lines = original_code.splitlines()
        start_line = max(0, line_no - 15)
        end_line = min(len(lines), line_no + 15)

        context_window = "\n".join(lines[start_line:end_line])

        # We instruct Claude to use a hyper-targeted SEARCH/REPLACE block matching the window
        system_prompt = (
            "You are Chiron, an advanced autonomous code-healing agent.\n"
            "Your task is to review a Python traceback and a small slice of a buggy source file, "
            "then output a surgical SEARCH/REPLACE block to fix the crash.\n\n"
            "You must output EXACTLY one block matching this format:\n"
            "<<<<<<< SEARCH\n"
            "[exact code lines from the original file slice that contain the bug]\n"
            "=======\n"
            "[the updated, defensively written code lines to replace them]\n"
            ">>>>>>> REPLACE\n\n"
            "RULES:\n"
            "1. Precision: Keep the SEARCH block as small and concise as possible (only 1-5 lines of code surrounding the crash).\n"
            "2. Spacing: The SEARCH block must match the original file slice indentation and syntax EXACTLY.\n"
            "3. Defensive: Wrap the fix with safety checks (e.g., zero-division or null checks) to prevent future crashes.\n"
            "4. No prose: Do not explain the fix. Output ONLY the raw SEARCH/REPLACE block. Do not wrap in markdown ticks."
        )

        user_content = (
            f"Target File: {file_path}\n"
            f"Crashing Line Number: {line_no}\n\n"
            f"Error Message:\n{error_message}\n\n"
            f"Traceback Details:\n{traceback_str}\n\n"
            f"30-Line Slice Around Error (Lines {start_line + 1} to {end_line}):\n"
            f"---\n{context_window}\n---\n\n"
            f"Provide the tiny SEARCH/REPLACE block to resolve the bug."
        )

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=500,  # Keep token limits tight so Claude focuses on a tiny edit
                temperature=0.0,
                system=system_prompt,
                messages=[{"role": "user", "content": user_content}],
            )

            response_text = "".join([block.text for block in response.content]).strip()

            # Parse and apply the search-and-replace block safely
            patched_code = self._apply_search_replace(original_code, response_text)
            if not patched_code:
                print(
                    "❌ [Chiron Patch] Failed to match or apply Claude's SEARCH/REPLACE block."
                )
                return None

            return patched_code

        except Exception as e:
            print(f"❌ [Chiron Patch] LLM API Call Failed: {e}")
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
