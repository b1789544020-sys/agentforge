"""Regression checks for the public 16.9 research-assistant design pack."""
from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "examples" / "local-research-assistant"
SPEC = importlib.util.spec_from_file_location("research_pack_validator", PACK / "validate_pack.py")
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


def test_public_research_pack_is_canonical_complete_and_hash_stable() -> None:
    summary = validator.validate()

    assert summary["passed"] is True
    assert summary["scope"] == "static-package-only"
    assert summary["provider"] == "ollama"
    assert summary["documents"] == 4
    assert summary["cases"] == 10
    assert summary["categories"]["insufficient"] == 3
    assert summary["network_requests"] == 0
    assert summary["real_provider_quality_assessed"] is False


def test_public_research_pack_rejects_unrecorded_corpus_changes(tmp_path: Path) -> None:
    copied = tmp_path / "local-research-assistant"
    shutil.copytree(PACK, copied)
    document = copied / "corpus" / "01-provider-and-runtime.md"
    document.write_text(document.read_text(encoding="utf-8") + "\n未记录的资料变化。\n", encoding="utf-8")

    with pytest.raises(ValueError, match="sha256 mismatch"):
        validator.validate(copied)
