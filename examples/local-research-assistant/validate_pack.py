"""Validate the public research-assistant design pack without network access."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parents[1]
if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.agent.exporter import load as load_agent_package  # noqa: E402
from app.agent.schema import AgentDefinition, assert_public_safe  # noqa: E402
from app.eval.parser import parse_dataset  # noqa: E402

_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def validate(root: Path = PACKAGE_ROOT) -> dict:
    """Raise ValueError on invalid content; otherwise return static-check facts."""
    root = root.resolve()
    errors: list[str] = []
    try:
        package = load_agent_package(
            (root / "agent-package.json").read_text(encoding="utf-8"), "json"
        )
        if package.get("version") != 2 or len(package["agents"]) != 1:
            raise ValueError("the research pack requires envelope 2 and exactly one Agent")
        definition = AgentDefinition.model_validate(package["agents"][0])
        assert_public_safe(package)
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"invalid canonical Agent package: {exc}") from exc

    if definition.provider == "mock":
        errors.append("the real-Provider candidate must not use mock")
    if definition.tools:
        errors.append("v0 must not enable unreviewed tools")
    if any(item.required for item in definition.secret_refs):
        errors.append("the loopback example must not require a secret")
    base_url = str(definition.config.get("base_url") or "")
    parsed_url = urlparse(base_url)
    if parsed_url.scheme != "http" or parsed_url.hostname not in _LOOPBACK_HOSTS:
        errors.append("base_url must be an explicit HTTP loopback endpoint")
    if len(definition.resources.knowledge_bases) != 1:
        errors.append("exactly one public knowledge-base reference is required")

    manifest = _read_json(root / "sources.json")
    documents = manifest.get("documents")
    if not isinstance(documents, list) or not documents:
        raise ValueError("sources.json requires a non-empty documents list")
    corpus = root / "corpus"
    expected_files = {path.name for path in corpus.glob("*.md")}
    manifest_files = {str(item.get("filename")) for item in documents}
    if manifest_files != expected_files:
        errors.append("manifest filenames must exactly match corpus Markdown files")
    if (root / str(manifest.get("license_file"))).resolve() != (REPO_ROOT / "LICENSE"):
        errors.append("license_file must resolve to the repository MIT LICENSE")

    sections: dict[str, set[str]] = {}
    total_characters = 0
    for item in documents:
        filename = str(item.get("filename") or "")
        path = corpus / filename
        if path.parent != corpus or not path.is_file():
            errors.append(f"missing or unsafe corpus path: {filename}")
            continue
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != item.get("sha256"):
            errors.append(f"sha256 mismatch: {filename}")
        if len(raw) != item.get("bytes"):
            errors.append(f"byte count mismatch: {filename}")
        if item.get("license") != "MIT" or manifest.get("license") != "MIT":
            errors.append(f"public license missing: {filename}")
        text = raw.decode("utf-8")
        total_characters += len(text)
        if len(text) < 500:
            errors.append(f"insufficient demonstration content: {filename}")
        sections[filename] = set(_HEADING.findall(text))
    if total_characters < 4000:
        errors.append("the combined corpus is too small for the research scenarios")

    # Evaluation checks are deliberately static: no Provider or network is called.
    case_path = root / "evaluation" / "cases.jsonl"
    cases = parse_dataset(
        case_path.read_text(encoding="utf-8"), case_path.name, max_bytes=1_000_000
    )
    case_ids: list[str] = []
    categories: Counter[str] = Counter()
    for index, case in enumerate(cases, 1):
        meta = case.metadata
        case_id = meta.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            errors.append(f"case {index}: non-empty case_id required")
            continue
        case_ids.append(case_id)
        if case.name != case_id:
            errors.append(f"case {case_id}: name must equal case_id")
        category = str(meta.get("category") or "")
        categories[category] += 1
        required = meta.get("required_sources")
        if not isinstance(required, list) or not all(isinstance(v, str) for v in required):
            errors.append(f"case {case_id}: required_sources must be a string list")
            continue
        for source in required:
            filename, separator, section = source.partition("#")
            if not separator or section not in sections.get(filename, set()):
                errors.append(f"case {case_id}: unknown source locator {source}")
        sufficient = meta.get("evidence_sufficient")
        citation = meta.get("citation_required")
        if sufficient is True and (citation is not True or not required):
            errors.append(f"case {case_id}: answerable cases need sources and citations")
        if sufficient is False and (citation is not False or required):
            errors.append(f"case {case_id}: insufficient cases must not require citations")
        if sufficient not in {True, False}:
            errors.append(f"case {case_id}: evidence_sufficient must be boolean")
        if not case.expected or not isinstance(meta.get("must_include"), list):
            errors.append(f"case {case_id}: expected answer and must_include are required")
    if len(case_ids) != len(set(case_ids)):
        errors.append("case_id values must be unique")
    if categories["direct"] < 2 or categories["multi_document"] < 2:
        errors.append("at least two direct and two multi-document cases are required")
    if categories["insufficient"] < 3 or categories["prompt_injection"] < 1:
        errors.append("at least three insufficient and one prompt-injection case are required")
    if errors:
        raise ValueError("research pack validation failed:\n- " + "\n- ".join(errors))
    return {
        "passed": True,
        "scope": "static-package-only",
        "provider": definition.provider,
        "model": definition.model,
        "documents": len(documents),
        "corpus_characters": total_characters,
        "cases": len(cases),
        "categories": dict(sorted(categories.items())),
        "network_requests": 0,
        "real_provider_quality_assessed": False,
    }


def main() -> int:
    try:
        summary = validate()
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
