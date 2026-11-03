"""Target-environment evidence: health, readiness, and one real Agent run."""
from __future__ import annotations

import argparse
import json
import os
import platform
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api")
    parser.add_argument("--provider", default="mock", help="mock, ollama, vllm, or openai-compatible")
    parser.add_argument("--model", default="mock-model")
    parser.add_argument("--endpoint", default=None, help="OpenAI-compatible /v1 root; never pass credentials here")
    parser.add_argument("--output", type=Path, default=None, help="optional JSON evidence path")
    args = parser.parse_args()
    headers = {"Authorization": f"Bearer {os.environ['AGENTFORGE_VERIFY_TOKEN']}"} if os.environ.get("AGENTFORGE_VERIFY_TOKEN") else {}
    evidence = {
        "timestamp": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(),
        "machine": platform.machine(), "provider": args.provider, "model": args.model,
        "endpoint": args.endpoint, "checks": {},
    }
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=180) as client:
        for name, path in (("health", "/health"), ("readiness", "/health/ready")):
            response = client.get(path)
            evidence["checks"][name] = {"status_code": response.status_code, "body": response.json()}
            response.raise_for_status()
        config = {"base_url": args.endpoint} if args.endpoint else {}
        response = client.post("/agents", json={
            "name": f"offline-verification-{uuid.uuid4()}", "provider": args.provider,
            "model": args.model, "system_prompt": "You are an offline deployment health check.",
            "max_steps": 2, "tools": [], "config": config,
        })
        evidence["checks"]["create_agent"] = {"status_code": response.status_code}
        response.raise_for_status()
        agent_id = response.json()["id"]
        evidence["agent_id"] = agent_id
        try:
            response = client.post(f"/agents/{agent_id}/runs", json={"input": "Return a short deployment confirmation."})
            evidence["checks"]["agent_run"] = {"status_code": response.status_code, "body": response.json()}
            response.raise_for_status()
            if response.json().get("status") not in {"success", "completed", "ok"}:
                raise RuntimeError(f"agent run did not succeed: {response.json()}")
        finally:
            client.delete(f"/agents/{agent_id}")
    evidence["result"] = "passed"
    text = json.dumps(evidence, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
