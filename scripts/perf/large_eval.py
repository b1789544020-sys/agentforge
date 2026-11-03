#!/usr/bin/env python3
"""Optional large evaluation dataset load test against a running AgentForge."""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request


def request(base: str, path: str, token: str, payload=None, method: str = "GET"):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.loads(response.read())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("PERF_BASE_URL", "http://localhost:8000/api"))
    parser.add_argument("--agent-id", default=os.environ.get("PERF_AGENT_ID"))
    parser.add_argument("--token", default=os.environ.get("PERF_TOKEN", ""))
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    if args.count < 1 or args.count > 10_000:
        parser.error("--count must be between 1 and 10000")
    agent_id = args.agent_id
    if not agent_id:
        agent = request(args.base_url, "/agents", args.token, {
            "name": f"perf-eval-{int(time.time())}", "provider": "mock", "model": "mock", "tools": [],
        }, "POST")
        agent_id = agent["id"]
    cases = [
        {"input": f"perf case {index}", "expected": "Mock response (no upstream call)."}
        for index in range(args.count)
    ]
    started = time.perf_counter()
    evaluation = request(args.base_url, f"/agents/{agent_id}/evals", args.token, {
        "dataset_name": "perf-large", "scorer_name": "answer", "cases": cases,
        "max_concurrency": args.concurrency,
    }, "POST")
    eval_id = evaluation["id"]
    while True:
        progress = request(args.base_url, f"/agents/{agent_id}/evals/{eval_id}/progress", args.token)
        print(json.dumps(progress), flush=True)
        if progress["status"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(1)
    elapsed = time.perf_counter() - started
    print(json.dumps({
        "eval_id": eval_id, "cases": args.count, "seconds": round(elapsed, 3),
        "cases_per_second": round(args.count / elapsed, 3),
        "passed": progress["passed"], "failed": progress["failed"], "status": progress["status"],
    }))


if __name__ == "__main__":
    main()
