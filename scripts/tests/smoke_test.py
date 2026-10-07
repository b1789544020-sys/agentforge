"""Smoke test: create agent, run, eval, memory. Reports any 4xx/5xx."""
import json
import urllib.request
import urllib.error
import uuid
import sys

BASE = "http://localhost:8000"


def call(method, path, body=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# 1. create
status, agent = call("POST", "/api/v1/agents", {
    "name": "smoke-" + uuid.uuid4().hex[:6],
    "provider": "mock",
    "model": "mock-model",
    "system_prompt": "Use calculator and final_answer.",
    "tools": ["calculator", "final_answer", "echo"],
})
print(f"[1] CREATE agent  -> HTTP {status}  id={agent.get('id') if isinstance(agent, dict) else agent}")
aid = agent["id"]

# 2. run
status, run = call("POST", f"/api/v1/agents/{aid}/runs",
                   {"input": "echo hi then final_answer with 'done'"})
print(f"[2] RUN            -> HTTP {status}  status={run.get('status')}  steps={run.get('step_count')}")
if run.get("trace"):
    for s in run["trace"]:
        print(f"      step[{s['index']}] {s['kind']:14s} {s.get('name') or '-':14s} status={s['status']} err={s.get('error')}")

# 3. eval (with correct field name 'expected')
status, ev = call("POST", f"/api/v1/agents/{aid}/evals",
                  {"dataset_name": "smoke",
                   "cases": [{"input": "hi", "expected": "hi"}],
                   "scorer_name": "composite"})
print(f"[3] EVAL           -> HTTP {status}")
if isinstance(ev, dict):
    if "detail" in ev:
        print("     detail:", ev["detail"])
    else:
        print("     summary:", {k: ev.get(k) for k in ("id", "status", "total", "passed", "pass_rate", "score")})
eid = ev.get("id") if isinstance(ev, dict) and "id" in ev else None

# 4. eval detail
if eid:
    status, ev2 = call("GET", f"/api/v1/agents/{aid}/evals/{eid}")
    print(f"[4] EVAL detail    -> HTTP {status}  keys={list(ev2.keys())[:8] if isinstance(ev2, dict) else ev2}")

# 5. memory
status, mem = call("GET", f"/api/v1/agents/{aid}/memory")
print(f"[5] MEMORY list    -> HTTP {status}  count={len(mem) if isinstance(mem, list) else mem}")

# 6. tools
status, tools = call("GET", "/api/v1/tools")
print(f"[6] TOOLS list     -> HTTP {status}  names={[t['name'] for t in tools] if isinstance(tools, list) else tools}")
