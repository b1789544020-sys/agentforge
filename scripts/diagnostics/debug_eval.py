"""Debug: capture the 400 detail from /evals."""
import json, urllib.request, urllib.error, uuid
BASE = "http://localhost:8000"
def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE+path, data=data,
        headers={"Content-Type":"application/json"}, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=20); return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
# create agent
s, a = call("POST", "/api/v1/agents", {
    "name":"dbg-"+uuid.uuid4().hex[:6], "provider":"mock", "model":"mock-model",
    "tools":["echo"], "system_prompt":"x"})
print("AGENT", s, a[:200])
aid = json.loads(a)["id"]
# eval with various shapes
for body in [
    {"cases":[{"input":"hi"}]},
    {"cases":[{"input":"hi","expected":"hi"}]},
    {"scorer_name":"exact","cases":[{"input":"hi","expected":"hi"}]},
    {"dataset_name":"smoke","scorer_name":"exact","cases":[{"input":"hi","expected":"hi"}]},
]:
    s, r = call("POST", f"/api/v1/agents/{aid}/evals", body)
    print("EVAL body", body, "->", s, r[:400])
