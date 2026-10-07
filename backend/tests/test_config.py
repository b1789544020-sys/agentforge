"""Tests for ``app.config.Settings`` validation.

Specifically covers the LLM provider Literal (5 values: mock/openai/anthropic/
deepseek/qwen) so that ``AGENTFORGE_LLM_PROVIDER=deepseek`` etc. doesn't get
rejected at pydantic-parse time.  ``AgentLoop._build_default_llm`` already
knows how to handle all 5; before this fix ``Settings`` would refuse to
instantiate with ``deepseek`` / ``qwen`` because the Literal only listed 3.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


ALL_PROVIDERS = ["mock", "openai", "anthropic", "deepseek", "qwen"]


class TestLLMProviderLiteral:
    """Each of the 5 documented providers must be accepted by Settings."""

    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    def test_llm_provider_accepts_documented_value(self, monkeypatch, provider):
        # clear any env override so the Literal check is what we're testing
        monkeypatch.delenv("AGENTFORGE_LLM_PROVIDER", raising=False)
        s = Settings(llm_provider=provider)
        assert s.llm_provider == provider

    def test_llm_provider_rejects_unknown_value(self):
        with pytest.raises(ValidationError) as exc:
            Settings(llm_provider="weaviate")  # not a real LLM provider
        # pydantic v2 surfaces the offending value in the error
        assert "weaviate" in str(exc.value)

    def test_env_var_override_accepts_deepseek(self, monkeypatch):
        """Regression: AGENTFORGE_LLM_PROVIDER=deepseek used to raise
        ValidationError because deepseek wasn't in the Literal.  This is
        the env name documented in the README and read by
        ``AgentLoop._build_default_llm``.
        """
        monkeypatch.setenv("AGENTFORGE_LLM_PROVIDER", "deepseek")
        s = Settings()
        assert s.llm_provider == "deepseek"

    def test_env_var_override_accepts_qwen(self, monkeypatch):
        monkeypatch.setenv("AGENTFORGE_LLM_PROVIDER", "qwen")
        s = Settings()
        assert s.llm_provider == "qwen"

    def test_default_remains_mock(self, monkeypatch):
        monkeypatch.delenv("AGENTFORGE_LLM_PROVIDER", raising=False)
        s = Settings()
        assert s.llm_provider == "mock"

