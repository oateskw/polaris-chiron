from types import SimpleNamespace
from unittest.mock import MagicMock

import git_handler
import proactive_optimizer


def test_brief_includes_every_request_field():
    payload = SimpleNamespace(
        optimization_specification="Raise reply rate",
        reason="Replies fell",
        issues_detected=["subject too long"],
        metrics_context={"reply_rate": 0.01},
    )
    brief = proactive_optimizer.build_optimization_brief(payload)
    for text in ("Raise reply rate", "Replies fell", "subject too long", "reply_rate"):
        assert text in brief


def test_brief_is_none_when_request_is_empty():
    assert proactive_optimizer.build_optimization_brief(SimpleNamespace()) is None


def test_sandbox_tests_do_not_use_a_shell(monkeypatch, tmp_path):
    run = MagicMock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(proactive_optimizer.subprocess, "run", run)
    optimizer = proactive_optimizer.ChironProactiveOptimizer(str(tmp_path))
    assert optimizer.run_sandbox_tests() == (True, "")
    assert not run.call_args.kwargs.get("shell")


def test_fallback_repo_map_returns_repo_names(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_ORG_OR_USER", "o")
    expected = {
        "nexus": "polaris-beacon",
        "radar": "polaris-beacon",
        "pulsar": "polaris-beacon",
        "nova": "polaris-vanguard",
        "vanguard": "polaris-vanguard",
    }
    for agent, repo in expected.items():
        assert git_handler.ChironGitHandler(agent, "b").repo_name == repo


def test_manifest_repository_overrides_fallback():
    handler = git_handler.ChironGitHandler("nexus", "b", target_repository="custom")
    assert handler.repo_name == "custom"
