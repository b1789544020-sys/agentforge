"""End-to-end: real eval run with the correct scorer name."""
import json, urllib.request, urllib.error, uuid, time
BASE = "http://localhost:8000"
def call(method, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE+path, data=data,
        headers={"Content-Type":"application/json"}, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout); return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
s, a = call("POST", "/api/v1/agents", {
    "name":"e2e-"+uuid.uuid4().hex[:6], "provider":"mock", "model":"mock-model",
    "system_prompt":"You are a calculator. Use calculator and final_answer.",
    "tools":["calculator", "final_answer"]})
print("AGENT", s, a.get("id") if isinstance(a, dict) else a)
aid = a["id"]
s, ev = call("POST", f"/api/v1/agents/{aid}/evals", {
    "dataset_name":"smoke",
    "scorer_name":"contains",
    "cases":[{"input":"hi","expected":"hi"},{"input":"world","expected":"world"}]})
print("EVAL POST", s, json.dumps(ev, indent=2)[:600] if isinstance(ev, dict) else ev)
if isinstance(ev, dict) and "id" in ev:
    eid = ev["id"]
    for _ in range(40):
        s, p = call("GET", f"/api/v1/agents/{aid}/evals/{eid}/progress")
        st = p.get("status")
        print(f"  progress status={st} done={p.get('done')}/{p.get('total')} passed={p.get('passed')} failed={p.get('failed')}")
        if st in ("done","completed","failed","error","success") or (p.get("total",0)>0 and p.get("done",0)>=p.get("total",0)):
            break
        time.sleep(0.5)
    s, detail = call("GET", f"/api/v1/agents/{aid}/evals/{eid}")
    print("FINAL summary:", {k: detail.get(k) for k in ("status","total","passed","failed","average_score","score")})
    print("persisted cases:", len(detail.get("cases",[])))
    for c in detail.get("cases", []):
        print(f"  case[{c['index']}] input={c['input']!r} expected={c['expected']!r} output={c['output']!r} passed={c['passed']} score={c['score']}")

