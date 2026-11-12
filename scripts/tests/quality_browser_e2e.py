"""Real Edge regression for four-layer quality evidence and human feedback review."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / ".scratch" / "completion-2026-11-12" / "browser" / f"run-{uuid.uuid4().hex}"
WORK.mkdir(parents=True, exist_ok=False)
DB = WORK / "quality.sqlite3"
BASE = "http://127.0.0.1:5173"
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")


def require(value, message):
    if not value:
        raise AssertionError(message)


def api(page, method, path, payload=None, expected=(200,)):
    response = page.evaluate("""async ({method, path, payload}) => {
      const response = await fetch('/api' + path, {method,
        headers: {'Content-Type': 'application/json'},
        body: payload === null ? undefined : JSON.stringify(payload)});
      const text = await response.text();
      let body; try { body = JSON.parse(text); } catch { body = text; }
      return {status: response.status, body};
    }""", {"method": method, "path": path, "payload": payload})
    require(response["status"] in expected, f"{method} {path}: {response}")
    return response["body"]


def gate(page, agent_id, version_id, suite_id):
    created = api(page, "POST", f"/agents/{agent_id}/gates", {
        "agent_version_id": version_id, "suite_id": suite_id,
    }, (202,))
    for _ in range(100):
        current = api(page, "GET", f"/agents/{agent_id}/gates/{created['id']}")
        if current["status"] in {"passed", "failed"}:
            return current
        page.wait_for_timeout(100)
    raise TimeoutError(f"Gate did not finish: {created['id']}")


def wait_url(url, process, label):
    for _ in range(160):
        if process.poll() is not None:
            raise RuntimeError(f"{label} exited with {process.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.15)
    raise TimeoutError(f"{label} readiness timeout")


def stop(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main():
    for port in (8000, 5173):
        with socket.socket() as sock:
            require(sock.connect_ex(("127.0.0.1", port)) != 0, f"Port {port} is in use")
    metadata = {
        "mode": "deterministic Mock engineering evidence", "assertions": [],
        "screenshots": [], "errors": [], "timeline": [],
    }
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
                context = browser.new_context(
                    viewport={"width": 1440, "height": 1000}, locale="zh-CN",
                    record_video_dir=str(WORK / "video"),
                    record_video_size={"width": 1440, "height": 1000},
                )
                page = context.new_page()
                video_origin = time.monotonic()

                def mark(name):
                    # Keep each real result readable before moving to the next scene.
                    page.wait_for_timeout(1500)
                    metadata["timeline"].append({"name": name, "seconds": time.monotonic() - video_origin})

                page.set_default_timeout(30000)
                page.on("pageerror", lambda exc: metadata["errors"].append(f"pageerror: {exc}"))
                page.on("console", lambda msg: metadata["errors"].append(f"console: {msg.text}") if msg.type == "error" else None)
                page.on("response", lambda response: metadata["errors"].append(
                    f"http {response.status}: {response.request.method} {response.url}"
                ) if response.status >= 400 else None)
                page.goto(BASE, wait_until="networkidle")
                expect(page.locator("h1")).to_contain_text("AgentForge")
                agent = api(page, "POST", "/agents", {
                    "name": "四层质量闭环 · 隔离演示", "provider": "mock", "model": "mock-quality-v1",
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
                require(evaluation["status"] == "completed", evaluation)
                case_ids = [row["id"] for row in evaluation["cases"]]
                suite = api(page, "POST", "/suites", {
                    "name": "四层证据 · 固定套件", "agent_id": agent_id, "case_ids": case_ids,
                    "pass_threshold": 1.0, "scorer": "exact_match", "auto_on_save": False,
                }, (201,))
                version1 = api(page, "GET", f"/agents/{agent_id}/versions")[0]
                first = gate(page, agent_id, version1["id"], suite["id"])
                require(first["status"] == "passed" and len(first["evidence"]) == 2, first)
                api(page, "PATCH", f"/agents/{agent_id}", {
                    "config": {"mock_replies": {"good": "right", "bad": "wrong"}},
                })
                version2 = api(page, "GET", f"/agents/{agent_id}/versions")[0]
                second = gate(page, agent_id, version2["id"], suite["id"])
                require(second["status"] == "failed", second)
                require(any(row["change"] == "new_failure" for row in second["diff"]), second)
                api(page, "PATCH", f"/agents/{agent_id}", {
                    "config": {"mock_replies": {"good": "right", "bad": "right"}},
                })
                version3 = api(page, "GET", f"/agents/{agent_id}/versions")[0]
                third = gate(page, agent_id, version3["id"], suite["id"])
                require(third["status"] == "passed", third)
                require(any(row["change"] == "fixed" for row in third["diff"]), third)
                metadata.update({"agent_id": agent_id, "suite_id": suite["id"], "case_ids": case_ids,
                                 "versions": [version1["id"], version2["id"], version3["id"]],
                                 "gates": [first["id"], second["id"], third["id"]]})
                metadata["assertions"].append("v1 passed, v2 new_failure, v3 fixed with exact Run evidence")

                mark("gate-fixed")
                page.get_by_role("tab", name="质量门禁", exact=True).click()
                page.locator("select").nth(0).select_option(agent_id)
                evidence = page.get_by_test_id("gate-quality-evidence")
                expect(evidence).to_be_visible()
                expect(page.locator(".diff-fixed").first).to_contain_text("fixed")
                expect(page.locator(".diff-fixed").first).to_contain_text("comparable")
                page.screenshot(path=str(WORK / "01-gate-fixed.png"), full_page=True)
                metadata["screenshots"].append("01-gate-fixed.png")
                mark("exact-run")
                evidence.locator("details.quality-case").first.locator("summary").click()
                evidence.get_by_role("button", name="打开确切 Run 与版本", exact=True).first.click()
                run_evidence = page.get_by_test_id("run-quality-evidence")
                expect(run_evidence).to_be_visible()
                expect(run_evidence).to_contain_text("记录完整")
                expect(page.locator(".grid-2").first).to_contain_text("right")
                page.screenshot(path=str(WORK / "02-exact-run.png"), full_page=True)
                metadata["screenshots"].append("02-exact-run.png")
                metadata["assertions"].append("Gate deep link opened the exact Run and four raw observations")

                mark("gate-regression")
                page.get_by_role("tab", name="质量门禁", exact=True).click()
                page.locator("tr.clickable", has_text="failed").first.click()
                failed_evidence = page.get_by_test_id("gate-quality-evidence")
                expect(failed_evidence).to_contain_text("output.case_score")
                expect(failed_evidence).to_contain_text("failed")
                page.screenshot(path=str(WORK / "03-gate-regression.png"), full_page=True)
                metadata["screenshots"].append("03-gate-regression.png")

                failed_detail = next(row for row in second["details"] if not row["passed"])
                failed_item = next(item for item in second["evidence"] if item["case_id"] == failed_detail["case_id"])
                feedback = api(page, "POST", "/operations/feedback", {
                    "run_id": failed_item["run_id"], "rating": "negative", "expected_answer": "right",
                }, (201,))
                require(feedback["agent_version_id"] == version2["id"], feedback)
                mark("human-feedback")
                page.get_by_role("tab", name="运营与成本", exact=True).click()
                feedback_card = page.locator(".gate-case", has_text="wrong")
                expect(feedback_card).to_be_visible()
                feedback_card.locator("textarea").first.fill("right")
                feedback_card.get_by_placeholder("加入 Suite ID（可选）").fill(suite["id"])
                metadata["convert_form_before"] = {
                    "answer": feedback_card.locator("textarea").first.input_value(),
                    "suite": feedback_card.get_by_placeholder("加入 Suite ID（可选）").input_value(),
                }
                with page.expect_response(
                    lambda response: response.url.endswith(f"/operations/feedback/{feedback['id']}/convert")
                    and response.request.method == "POST", timeout=10000,
                ) as convert_pending:
                    feedback_card.get_by_role("button", name="转成 EvalCase 并回归", exact=True).click()
                    page.wait_for_timeout(750)
                    metadata["convert_form_after"] = {
                        "banners": page.locator(".banner.err").all_inner_texts(),
                        "card": feedback_card.inner_text(),
                    }
                require(convert_pending.value.status == 201, convert_pending.value.status)
                expect(feedback_card).to_contain_text(failed_detail["case_id"], timeout=30000)
                metadata["assertions"].append("Negative Gate Run feedback reused its original fixed EvalCase")
                mark("human-resolution")
                feedback_card.get_by_label(f"复核 GateRun {feedback['id']}").fill(third["id"])
                feedback_card.get_by_label(f"复核理由 {feedback['id']}").fill("人工核对失败 Run 与修复 Run，答案和版本关系一致。")
                with page.expect_response(
                    lambda response: response.url.endswith(f"/operations/feedback/{feedback['id']}/resolutions")
                    and response.request.method == "POST", timeout=30000,
                ) as resolution_pending:
                    feedback_card.get_by_role("button", name="人工确认已修复", exact=True).click()
                require(resolution_pending.value.status == 201, resolution_pending.value.status)
                expect(feedback_card).to_contain_text("confirmed_fixed", timeout=30000)
                history = api(page, "GET", f"/operations/feedback/{feedback['id']}/resolutions")
                require(len(history) == 1 and history[0]["verdict"] == "confirmed_fixed", history)
                page.screenshot(path=str(WORK / "04-human-resolution.png"), full_page=True)
                metadata["screenshots"].append("04-human-resolution.png")
                metadata["feedback_id"] = feedback["id"]
                metadata["assertions"].append("Human confirmed_fixed persisted against the exact baseline Run")

                mark("version-delivery")
                deployment = api(page, "POST", "/deployments", {
                    "agent_version_id": version3["id"], "name": "四层质量闭环 · 固定版本交付",
                }, (201,))
                page.goto(f"{BASE}/deliver/{deployment['token']}", wait_until="networkidle")
                page.get_by_label("输入消息").fill("good")
                page.get_by_role("button", name="发送", exact=True).click()
                expect(page.locator(".chat-message.assistant")).to_contain_text("right")
                delivery_run = api(page, "POST", f"/deliver/{deployment['token']}/chat", {"input": "good"})
                require(delivery_run["agent_version_id"] == version3["id"], delivery_run)
                require(delivery_run["output"] == "right", delivery_run)
                page.screenshot(path=str(WORK / "05-version-delivery.png"), full_page=True)
                metadata["screenshots"].append("05-version-delivery.png")
                metadata["deployment_id"] = deployment["id"]
                metadata["assertions"].append("Public delivery returned the fixed version and exact output")

                mark("complete")
                page.set_viewport_size({"width": 390, "height": 844})
                require(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"),
                        "Mobile page has horizontal document overflow")
                require(not metadata["errors"], f"Browser errors: {metadata['errors']}")
                metadata["passed"] = True
                raw_video = Path(page.video.path())
                context.close()
                shutil.copy2(raw_video, WORK / "quality-loop.webm")
                metadata["video"] = "quality-loop.webm"
                browser.close()
        except Exception as exc:
            metadata["passed"] = False
            metadata["failure"] = f"{type(exc).__name__}: {exc}"
            if "page" in locals():
                try:
                    page.screenshot(path=str(WORK / "failure.png"), full_page=True)
                    metadata["failure_body_text"] = page.locator("body").inner_text()[-12000:]
                except Exception as debug_exc:
                    metadata["debug_capture_error"] = str(debug_exc)
            raise
        finally:
            stop(frontend)
            stop(backend)
            metadata["database_existed_during_run"] = DB.exists()
            (WORK / "result.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
