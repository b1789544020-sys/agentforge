"""Scan tracked source for likely plaintext secrets; never print matched values."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RULES = [
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("github-token", re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[A-Z0-9]{16}\b")),
    ("literal-credential", re.compile(r"(?i)\b(?:api[_-]?key|password|secret|token)\s*[=:]\s*[\"']([^\"']{12,})[\"']")),
]
ALLOWED = {
    "change-me-in-production-please", "replace-with-a-long-random-secret",
    "replace-with-a-long-random-local-secret", "stage4-test-secret", "direction2-test-secret",
}


def main() -> None:
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8").split("\0")
    findings: list[str] = []
    for name in filter(None, names):
        path = ROOT / name
        if not path.is_file() or path.stat().st_size > 3_000_000:
            continue
        raw = path.read_bytes()
        if b"\0" in raw:
            continue
        text = raw.decode("utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), 1):
            for label, rule in RULES:
                match = rule.search(line)
                if not match:
                    continue
                if label == "literal-credential" and (
                    match.group(1) in ALLOWED or match.group(1).startswith(("<", "%", "$"))
                    or "test" in name or "example" in name
                ):
                    continue
                findings.append(f"{name}:{line_number}: {label}")
    if findings:
        print("Potential plaintext secrets detected (values suppressed):")
        print("\n".join(findings))
        raise SystemExit(1)
    print(f"OK: no likely plaintext secrets in {len(names)} tracked paths")


if __name__ == "__main__":
    sys.exit(main())
