"""Verify SHA-256 hashes in an offline bundle manifest."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    failures = []
    for relative, expected in manifest.get("artifacts_sha256", {}).items():
        path = root / relative
        if not path.is_file():
            failures.append(f"missing: {relative}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            failures.append(f"checksum mismatch: {relative}")
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"OK: {len(manifest.get('artifacts_sha256', {}))} artifacts; commit={manifest.get('source_commit')}")


if __name__ == "__main__":
    main()
