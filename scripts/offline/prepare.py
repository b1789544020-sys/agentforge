"""Build an offline installation bundle on a connected preparation machine.

Run after committing the intended release: python scripts/offline/prepare.py dist/offline
The connected machine needs Python, npm, Docker and git. No command is run by tests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run(command: list[str], cwd: Path = ROOT) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="bundle directory, e.g. dist/offline")
    parser.add_argument("--skip-images", action="store_true", help="skip Docker images for a source-only bundle")
    args = parser.parse_args()
    output = args.output.resolve()
    if output == ROOT or ROOT not in output.parents:
        raise SystemExit("output must be a new directory inside the AgentForge repository")
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"refusing non-empty output directory: {output}")
    for name in ("wheels", "npm-cache", "images", "source", "models"):
        (output / name).mkdir(parents=True, exist_ok=True)

    run([os_python(), "-m", "pip", "download", "--dest", str(output / "wheels"),
         "-r", str(ROOT / "backend" / "requirements.txt")])
    run(["npm", "ci", "--cache", str(output / "npm-cache")], cwd=ROOT / "frontend")
    run(["git", "archive", "--format=tar", "--output", str(output / "source" / "agentforge-source.tar"), "HEAD"])

    images: list[str] = []
    if not args.skip_images:
        run(["docker", "compose", "pull", "postgres", "redis", "qdrant"])
        run(["docker", "compose", "build", "agentforge-api", "agentforge-frontend", "sandbox"])
        images = sorted(set(subprocess.check_output(
            ["docker", "compose", "config", "--images"], cwd=ROOT, text=True
        ).splitlines()))
        run(["docker", "image", "save", "--output", str(output / "images" / "agentforge-images.tar"), *images])

    artifacts = {}
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.suffix in {".tar", ".whl"}:
            artifacts[str(path.relative_to(output))] = digest(path)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "prepare_platform": platform.platform(),
        "python_requires": "3.11+ (use a matching target ABI for wheel downloads)",
        "docker_images": images,
        "artifacts_sha256": artifacts,
        "install": "bash scripts/offline/install.sh /path/to/offline-bundle",
        "warning": "Prepare wheels/images for the target OS, architecture and Python ABI; do not mix ARM and x86 artifacts.",
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    (output / "scripts").mkdir(exist_ok=True)
    for name in ("install.sh", "verify_bundle.py"):
        source = ROOT / "scripts" / "offline" / name
        (output / "scripts" / name).write_bytes(source.read_bytes())
    print(f"Offline bundle ready: {output}")


def os_python() -> str:
    import sys
    return sys.executable


if __name__ == "__main__":
    main()
