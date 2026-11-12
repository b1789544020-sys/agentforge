"""Record isolated per-scene Edge clips for the verified AgentForge quality demo."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "tests"))
from quality_browser_e2e import api, gate, require, stop, wait_url  # noqa: E402

WORK = ROOT / ".scratch" / "completion-2026-11-12" / "browser-segments" / f"run-{uuid.uuid4().hex}"
WORK.mkdir(parents=True, exist_ok=False)
DB = WORK / "quality.sqlite3"
BASE = "http://127.0.0.1:5173"
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")


def main():
    for port in (8000, 5173):
        with socket.socket() as sock:
            require(sock.connect_ex(("127.0.0.1", port)) != 0, f"Port {port} is in use")
    metadata = {"mode": "segmented deterministic Mock browser evidence", "assertions": [],
                "screenshots": [], "segments": {}, "errors": []}
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(ROOT / "backend"), "PYTHONIOENCODING": "utf-8",
        "AGENTFORGE_DATABASE_URL": f"sqlite+aiosqlite:///{DB.as_posix()}",
        "AGENTFORGE_AUTH_ENABLED": "false", "AGENTFORGE_API_TOKEN": "",
        "AGENTFORGE_LLM_PROVIDER": "mock", "AGENTFORGE_TASK_BACKEND": "memory",
        "AGENTFORGE_CONTROL_BACKEND": "memory", "AGENTFORGE_STORAGE_DIR": str(WORK / "storage"),
        "AGENTFORGE_DELIVERY_RATE_LIMIT_REQUESTS": "0",
    })
    backend = frontend = None
    with (WORK / "backend.log").open("w", encoding="utf-8") as backend_log, \
            (WORK / "frontend.log").open("w", encoding="utf-8") as frontend_log:
        try:
            backend = subprocess.Popen([
                sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000",
            ], cwd=ROOT / "backend", env=env, stdin=subprocess.DEVNULL,
               stdout=backend_log, stderr=subprocess.STDOUT)
            frontend = subprocess.Popen([
                shutil.which("node") or "node", "node_modules/vite/bin/vite.js",
                "--host", "127.0.0.1", "--port", "5173", "--strictPort",
            ], cwd=ROOT / "frontend", env=env, stdin=subprocess.DEVNULL,
               stdout=frontend_log, stderr=subprocess.STDOUT)
            wait_url("http://127.0.0.1:8000/api/health", backend, "backend")
            wait_url(BASE, frontend, "frontend")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(EDGE), headless=True)
                setup_context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-CN")
                page = setup_context.new_page()
                page.set_default_timeout(30000)
                page.on("pageerror", lambda exc: metadata["errors"].append(f"pageerror: {exc}"))
                page.on("console", lambda msg: metadata["errors"].append(f"console: {msg.text}") if msg.type == "error" else None)
                page.on("response", lambda response: metadata["errors"].append(
                    f"http {response.status}: {response.request.method} {response.url}"
                ) if response.status >= 400 else None)
                page.goto(BASE, wait_until="networkidle")
                agent = api(page, "POST", "/agents", {
                    "name": "四层质量闭环 · 分段录制", "provider": "mock", "model": "mock-quality-v1",
                    "system_prompt": "准确回答固定问题。", "temperature": 0, "tools": [],
                    "config": {"mock_replies": {"good": "right", "bad": "right"}},
                }, (201,))
                agent_id = agent["id"]
                evaluation = api(page, "POST", f"/agents/{agent_id}/evals", {
                    "dataset_name": "公开固定回归", "scorer": "exact_match", "pass_threshold": 1.0,
                    "cases": [{"input": "good", "expected": "right"}, {"input": "bad", "expected": "right"}],
                }, (201,))
                for _ in range(100):
                    evaluation = api(page, "GET", f"/agents/{agent_id}/evals/{evaluation['id']}")
                    if evaluation["status"] == "completed":
                        break
                    page.wait_for_timeout(100)
                case_ids = [row["id"] for row in evaluation["cases"]]
                suite = api(page, "POST", "/suites", {
                    "name": "四层证据 · 固定套件", "agent_id": agent_id, "case_ids": case_ids,
                    "pass_threshold": 1.0, "scorer": "exact_match", "auto_on_save": False,
                }, (201,))
                version1 = api(page, "GET", f"/agents/{agent_id}/versions")[0]
                first = gate(page, agent_id, version1["id"], suite["id"])
                api(page, "PATCH", f"/agents/{agent_id}", {
                    "config": {"mock_replies": {"good": "right", "bad": "wrong"}},
                })
                version2 = api(page, "GET", f"/agents/{agent_id}/versions")[0]
                second = gate(page, agent_id, version2["id"], suite["id"])
                api(page, "PATCH", f"/agents/{agent_id}", {
                    "config": {"mock_replies": {"good": "right", "bad": "right"}},
                })
                version3 = api(page, "GET", f"/agents/{agent_id}/versions")[0]
                third = gate(page, agent_id, version3["id"], suite["id"])
                require(first["status"] == "passed" and second["status"] == "failed" and third["status"] == "passed",
                        {"first": first, "second": second, "third": third})
                failed_detail = next(row for row in second["details"] if not row["passed"])
                failed_item = next(item for item in second["evidence"] if item["case_id"] == failed_detail["case_id"])
                feedback = api(page, "POST", "/operations/feedback", {
                    "run_id": failed_item["run_id"], "rating": "negative", "expected_answer": "right",
                }, (201,))
                deployment = api(page, "POST", "/deployments", {
                    "agent_version_id": version3["id"], "name": "四层质量闭环 · 固定版本交付",
                }, (201,))
                metadata.update({"agent_id": agent_id, "suite_id": suite["id"], "case_ids": case_ids,
                                 "versions": [version1["id"], version2["id"], version3["id"]],
                                 "gates": [first["id"], second["id"], third["id"]],
                                 "feedback_id": feedback["id"], "deployment_id": deployment["id"]})
                setup_context.close()

                def record_scene(name, action, screenshot, start_url=BASE):
                    context = browser.new_context(
                        viewport={"width": 1440, "height": 1000}, locale="zh-CN",
                        record_video_dir=str(WORK / "raw" / name),
                        record_video_size={"width": 1440, "height": 1000},
                    )
                    scene = context.new_page()
                    scene.set_default_timeout(30000)
                    scene.on("pageerror", lambda exc: metadata["errors"].append(f"{name} pageerror: {exc}"))
                    scene.on("console", lambda msg: metadata["errors"].append(
                        f"{name} console: {msg.text}") if msg.type == "error" else None)
                    scene.on("response", lambda response: metadata["errors"].append(
                        f"{name} http {response.status}: {response.request.method} {response.url}"
                    ) if response.status >= 400 else None)
                    scene.goto(start_url, wait_until="networkidle")
                    action(scene)
                    scene.wait_for_timeout(1500)
                    # Full-page screenshots resize the capture surface and corrupt video framing.
                    scene.screenshot(path=str(WORK / screenshot), full_page=False)
                    metadata["screenshots"].append(screenshot)
                    raw = Path(scene.video.path())
                    context.close()
                    target = WORK / "segments" / f"{name}.webm"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(raw, target)
                    metadata["segments"][name] = f"segments/{name}.webm"

                def open_fixed(scene):
                    scene.get_by_role("tab", name="质量门禁", exact=True).click()
                    scene.locator("select").nth(0).select_option(agent_id)
                    evidence = scene.get_by_test_id("gate-quality-evidence")
                    expect(evidence).to_be_visible()
                    expect(scene.locator(".diff-fixed").first).to_contain_text("fixed")
                    expect(scene.locator(".diff-fixed").first).to_contain_text("comparable")
                    evidence.locator("details.quality-case").first.locator("summary").click()
                    expect(evidence).to_contain_text("output.case_score")
                    return evidence

                def fixed_action(scene):
                    open_fixed(scene)

                def run_action(scene):
                    evidence = open_fixed(scene)
                    evidence.get_by_role("button", name="打开确切 Run 与版本", exact=True).first.click()
                    expect(scene.get_by_test_id("run-quality-evidence")).to_be_visible()
                    expect(scene.get_by_test_id("run-quality-evidence")).to_contain_text("记录完整")
                    expect(scene.locator(".grid-2").first).to_contain_text("right")

                def regression_action(scene):
                    scene.get_by_role("tab", name="质量门禁", exact=True).click()
                    scene.locator("select").nth(0).select_option(agent_id)
                    scene.locator("tr.clickable", has_text="failed").first.click()
                    evidence = scene.get_by_test_id("gate-quality-evidence")
                    expect(evidence).to_contain_text("output.case_score")
                    expect(evidence).to_contain_text("failed")
                    expect(evidence).to_contain_text("system.terminal")
                    scene.wait_for_timeout(500)
                    failed_case = evidence.locator("details.quality-case", has_text="bad")
                    failed_case.scroll_into_view_if_needed()
                    expect(failed_case).to_contain_text("输出 · failed")
                    expect(failed_case).to_contain_text("系统 · passed")

                def feedback_action(scene):
                    scene.get_by_role("tab", name="运营与成本", exact=True).click()
                    card = scene.locator(".gate-case", has_text="wrong")
                    expect(card).to_be_visible()
                    card.get_by_placeholder("加入 Suite ID（可选）").fill(suite["id"])
                    with scene.expect_response(
                        lambda response: response.url.endswith(f"/operations/feedback/{feedback['id']}/convert")
                        and response.request.method == "POST", timeout=30000,
                    ) as pending:
                        card.get_by_role("button", name="转成 EvalCase 并回归", exact=True).click()
                    require(pending.value.status == 201, pending.value.status)
                    expect(card).to_contain_text(failed_detail["case_id"], timeout=30000)
                    resolutions = api(scene, "GET", f"/operations/feedback/{feedback['id']}/resolutions")
                    require(resolutions == [], resolutions)

                def resolution_action(scene):
                    scene.get_by_role("tab", name="运营与成本", exact=True).click()
                    card = scene.locator(".gate-case", has_text="wrong")
                    expect(card).to_be_visible()
                    card.get_by_label(f"复核 GateRun {feedback['id']}").fill(third["id"])
                    card.get_by_label(f"复核理由 {feedback['id']}").fill("人工核对失败 Run 与修复 Run，答案和版本关系一致。")
                    with scene.expect_response(
                        lambda response: response.url.endswith(f"/operations/feedback/{feedback['id']}/resolutions")
                        and response.request.method == "POST", timeout=30000,
                    ) as pending:
                        card.get_by_role("button", name="人工确认已修复", exact=True).click()
                    require(pending.value.status == 201, pending.value.status)
                    expect(card).to_contain_text("confirmed_fixed", timeout=30000)
                    history = api(scene, "GET", f"/operations/feedback/{feedback['id']}/resolutions")
                    require(len(history) == 1 and history[0]["verdict"] == "confirmed_fixed", history)

                def delivery_action(scene):
                    scene.get_by_label("输入消息").fill("good")
                    scene.get_by_role("button", name="发送", exact=True).click()
                    expect(scene.locator(".chat-message.assistant")).to_contain_text("right")
                    delivered = api(scene, "POST", f"/deliver/{deployment['token']}/chat", {"input": "good"})
                    require(delivered["agent_version_id"] == version3["id"], delivered)
                    require(delivered["output"] == "right", delivered)

                record_scene("gate-fixed", fixed_action, "01-gate-fixed.png")
                record_scene("exact-run", run_action, "02-exact-run.png")
                record_scene("gate-regression", regression_action, "03-gate-regression.png")
                record_scene("human-feedback", feedback_action, "04-human-feedback.png")
                record_scene("human-resolution", resolution_action, "04-human-resolution.png")
                record_scene("version-delivery", delivery_action, "05-version-delivery.png",
                             start_url=f"{BASE}/deliver/{deployment['token']}")
                metadata["assertions"] = [
                    "v1 passed, v2 new_failure, v3 fixed with exact Run evidence",
                    "Gate deep link opened the exact Run and four raw observations",
                    "Negative Gate Run feedback reused its original fixed EvalCase",
                    "Human confirmed_fixed persisted against the exact baseline Run",
                    "Public delivery returned the fixed version and exact output",
                ]
                check_context = browser.new_context(viewport={"width": 390, "height": 844}, locale="zh-CN")
                check_page = check_context.new_page()
                check_page.goto(f"{BASE}/deliver/{deployment['token']}", wait_until="networkidle")
                require(check_page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"),
                        "Mobile page has horizontal document overflow")
                check_context.close()
                require(not metadata["errors"], f"Browser errors: {metadata['errors']}")
                metadata["passed"] = True
                browser.close()
        except Exception as exc:
            metadata["passed"] = False
            metadata["failure"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            stop(frontend)
            stop(backend)
            metadata["database_existed_during_run"] = DB.exists()
            (WORK / "result.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
