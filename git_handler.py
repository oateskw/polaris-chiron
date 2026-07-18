import os
import stat
import shutil
import subprocess
import tempfile
import httpx
from typing import Optional
from pathlib import Path


def remove_readonly(func, path, excinfo):
    """Forcefully clears read-only attributes on Windows so shutil can delete .git objects."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass  # Absorb failures silently if the handle is entirely locked


class ChironGitHandler:
    def __init__(self, target_agent: str, branch_name: str):
        self.target_agent = target_agent.lower()
        self.branch_name = branch_name
        self.github_token = os.getenv("GITHUB_TOKEN")
        self.github_org = os.getenv(
            "GITHUB_ORG_OR_USER"
        )  # Your GitHub organization/username

        # Target repo map (maps agent names to their exact repository names)
        self.repo_map = {
            "helix": "polaris-helix",
            "beacon": "polaris-beacon",
            "nova": "polaris-vanguard",  # Nova lives inside polaris-vanguard
            "vanguard": "polaris-vanguard",
        }

        self.repo_name = self.repo_map.get(self.target_agent)

        # 🎯 WINDOWS SAFE: Dynamically fetches OS-native temp directories
        base_temp = tempfile.gettempdir()

        # Sandbox folder inside Chiron's container to clone files into
        self.sandbox_path = str(Path(base_temp) / "chiron_sandbox" / self.repo_name)

    def clean_sandbox(self) -> None:
        """Removes the local sandbox directory if it already exists."""
        if os.path.exists(self.sandbox_path):
            # Using onexc for Python 3.12+ / 3.14 compatibility
            shutil.rmtree(self.sandbox_path, onexc=remove_readonly)

    def clone_and_branch(self) -> bool:
        """Clones the target repository and checks out an isolated fix branch."""
        if not self.repo_name or not self.github_token or not self.github_org:
            print(
                f"❌ [Chiron Git] Missing configuration parameters for '{self.target_agent}'."
            )
            return False

        self.clean_sandbox()
        os.makedirs(os.path.dirname(self.sandbox_path), exist_ok=True)

        # Authenticate git with the secure GitHub Token
        clone_url = f"https://x-access-token:{self.github_token}@github.com/{self.github_org}/{self.repo_name}.git"

        try:
            # Clone the main branch
            subprocess.run(
                ["git", "clone", clone_url, self.sandbox_path],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

            # Navigate to sandbox and checkout our target branch
            subprocess.run(
                ["git", "checkout", "-b", self.branch_name],
                cwd=self.sandbox_path,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            print(f"✅ [Chiron Git] Repository cloned and branched: {self.branch_name}")
            return True
        except subprocess.CalledProcessError as e:
            print(
                f"❌ [Chiron Git] Failed during Git clone or branch execution: {e.stderr.decode()}"
            )
            return False

    def stage_commit_and_push(self, commit_message: str) -> bool:
        """Stages all code modifications, commits, and pushes them to GitHub."""
        try:
            # Stage all file changes
            subprocess.run(["git", "add", "."], cwd=self.sandbox_path, check=True)

            # Configure a temporary git user profile for Chiron commits
            subprocess.run(
                ["git", "config", "user.name", "Chiron Auto-Healer"],
                cwd=self.sandbox_path,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "chiron@polarisinnovations.local"],
                cwd=self.sandbox_path,
                check=True,
            )

            # Commit the changes
            subprocess.run(
                ["git", "commit", "-m", commit_message],
                cwd=self.sandbox_path,
                check=True,
            )

            # Push changes upstream to our isolated branch
            subprocess.run(
                ["git", "push", "origin", self.branch_name, "--force"],
                cwd=self.sandbox_path,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            print(
                f"🚀 [Chiron Git] Successfully pushed code changes to origin/{self.branch_name}"
            )
            return True
        except subprocess.CalledProcessError as e:
            print(f"❌ [Chiron Git] Failed pushing code changes: {e.stderr.decode()}")
            return False

    async def open_pull_request(self, pr_title: str, pr_body: str) -> Optional[dict]:
        """Uses the GitHub REST API to open a descriptive Pull Request."""
        url = f"https://api.github.com/repos/{self.github_org}/{self.repo_name}/pulls"
        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": "application/vnd.github+json",
        }
        payload = {
            "title": pr_title,
            "body": pr_body,
            "head": self.branch_name,
            "base": "main",  # Always target your standard production base
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    url, headers=headers, json=payload, timeout=10.0
                )
                if response.status_code == 201:
                    pr_data = response.json()
                    print(
                        f"✅ [Chiron Git] Pull Request successfully opened! PR #{pr_data['number']}"
                    )
                    return {
                        "pr_number": pr_data["number"],
                        "pr_url": pr_data["html_url"],
                    }
                else:
                    print(
                        f"❌ [Chiron Git] GitHub API PR submission failed ({response.status_code}): {response.text}"
                    )
                    return None
            except Exception as e:
                print(f"❌ [Chiron Git] Exception hit opening PR: {e}")
                return None
