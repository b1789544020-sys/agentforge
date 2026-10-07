"""Poll /progress until the eval completes."""
import json, urllib.request, time
BASE = "http://localhost:8000"
def call(method, path, timeout=30):
    req = urllib.request.Request(BASE+path, method=method)
    return urllib.request.urlopen(req, timeout=timeout)
# read the last eval id we created from the test agent
s, a = call("GET","/api/v1/agents/6999e908-3b50-47e5-a3b5-c702a89b56ae/evals", timeout=10).read(), None
import urllib.request
r = urllib.request.urlopen("http://localhost:8000/api/v1/agents/6999e908-3b50-47e5-a3b5-c702a89b56ae/evals",timeout=10)
evals = json.loads(r.read())
print(f"Have {len(evals)} evals")
eid = evals[0]["id"]
print("polling", eid)
for i in range(40):
    r = urllib.request.urlopen(f"http://localhost:8000/api/v1/agents/6999e908-3b50-47e5-a3b5-c702a89b56ae/evals/{eid}/progress",timeout=5)
    p = json.loads(r.read())
    print(f"  [{i:02d}] status={p['status']}  done={p['done']}/{p['total']}  passed={p['passed']}  failed={p['failed']}")
    if p["status"] in ("done","completed","failed","error") or (p["total"]>0 and p["done"]>=p["total"]):
        break
    time.sleep(0.5)
r = urllib.request.urlopen(f"http://localhost:8000/api/v1/agents/6999e908-3b50-47e5-a3b5-c702a89b56ae/evals/{eid}",timeout=10)
detail = json.loads(r.read())
print("FINAL:", json.dumps({k: detail.get(k) for k in ("status","total","passed","failed","average_score","score")}, indent=2))
print("cases count in detail:", len(detail.get("cases",[])))
