"""Unit tests for the LLM/agent config update validation (pure logic)."""

import pytest

from routes.llm_config import _apply_changes
from utils.config import config


def test_apply_changes_rejects_unknown_section():
    with pytest.raises(ValueError, match="read-only section"):
        _apply_changes({"flask_config": {"debug": True}})


def test_apply_changes_rejects_secret_and_unknown_fields():
    with pytest.raises(ValueError, match="api_key"):
        _apply_changes({"llm_config": {"api_key": "sk-123"}})
    with pytest.raises(ValueError, match="read-only field"):
        _apply_changes({"agent": {"not_a_field": 1}})


def test_apply_changes_rejects_invalid_values():
    with pytest.raises(ValueError, match="llm_config"):
        _apply_changes({"llm_config": {"timeout_seconds": 0}})
    with pytest.raises(ValueError, match="agent"):
        _apply_changes({"agent": {"max_tool_retries": 99}})


def test_apply_changes_merges_without_touching_live_config():
    original_guard = config.agent.guard_enabled
    original_temperature = config.llm_config.temperature

    new_config = _apply_changes({
        "llm_config": {"temperature": 0.9},
        "agent": {"guard_enabled": not original_guard},
    })

    assert new_config.llm_config.temperature == 0.9
    assert new_config.agent.guard_enabled == (not original_guard)
    assert new_config.llm_config.provider == config.llm_config.provider
    assert config.llm_config.temperature == original_temperature
    assert config.agent.guard_enabled == original_guard
    assert new_config is not config