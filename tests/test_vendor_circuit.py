from unittest.mock import AsyncMock

import pytest

import patch_generator
import proactive_optimizer

pytestmark = pytest.mark.anyio


async def test_patch_generator_blocks_anthropic_when_circuit_is_unhealthy(
    monkeypatch, tmp_path
):
    generator = patch_generator.ChironPatchGenerator(str(tmp_path))
    generator.client = AsyncMock()
    monkeypatch.setattr(
        patch_generator.vendor_health,
        "is_vendor_circuit_healthy",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        patch_generator, "get_remote_prompt", AsyncMock(return_value="prompt")
    )
    monkeypatch.setattr(
        patch_generator, "get_agent_manifest", AsyncMock(return_value=None)
    )

    result = await generator.generate_fix(
        "target.py", "raise ValueError()", "traceback", "failure", "agent"
    )

    assert result is None
    generator.client.messages.create.assert_not_awaited()


async def test_proactive_optimizer_blocks_anthropic_when_circuit_is_unhealthy(
    monkeypatch, tmp_path
):
    optimizer = proactive_optimizer.ChironProactiveOptimizer(str(tmp_path))
    optimizer.client = AsyncMock()
    monkeypatch.setattr(
        proactive_optimizer.vendor_health,
        "is_vendor_circuit_healthy",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        proactive_optimizer, "get_remote_prompt", AsyncMock(return_value="prompt")
    )
    monkeypatch.setattr(
        proactive_optimizer, "get_agent_manifest", AsyncMock(return_value=None)
    )

    result = await optimizer.generate_optimization("target.py", "value = 1", "agent")

    assert result is None
    optimizer.client.messages.create.assert_not_awaited()
