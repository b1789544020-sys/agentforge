"""Confirm the eval pipeline marks a passing case correctly."""
import json, urllib.request, urllib.error, uuid, time
BASE = "http://localhost:8000"
def call(method, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE+path, data=data,
        headers={"Content-Type":"application/json"}, method=method)
    r = urllib.request.urlopen(req, timeout=timeout)
    return r.status, json.loads(r.read() or b"null")
# 'contains' against a substring of the deterministic mock response
s, a = call("POST", "/api/v1/agents", {
    "name":"pass-"+uuid.uuid4().hex[:6], "provider":"mock", "model":"mock-model",
    "system_prompt":"x", "tools":["echo"]})
print("AGENT", s, a["id"]); aid = a["id"]
s, ev = call("POST", f"/api/v1/agents/{aid}/evals", {
    "dataset_name":"smoke","scorer_name":"contains",
    "cases":[{"input":"hi","expected":"Mock response"}]})
print("EVAL", s, ev["id"], "status@post=", ev["status"]); eid = ev["id"]
for _ in range(20):
    s, p = call("GET", f"/api/v1/agents/{aid}/evals/{eid}/progress")
    print(f"  progress {p['status']} {p['done']}/{p['total']} passed={p['passed']} failed={p['failed']}")
    if p["done"] >= p["total"]: break
    time.sleep(0.3)
s, d = call("GET", f"/api/v1/agents/{aid}/evals/{eid}")
print("FINAL:", {k: d.get(k) for k in ("status","total","passed","failed","average_score")})
for c in d.get("cases",[]):
    print(f"  case input={c['input']!r} expected={c['expected']!r} output={c['output']!r} passed={c['passed']} score={c['score']}")
