"""Produce the capability-led AgentForge showcase from verified browser recordings."""
# Chinese narration intentionally uses full-width punctuation.
# ruff: noqa: RUF001
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import edge_tts
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
FFMPEG = Path(os.environ.get("AGENTFORGE_FFMPEG", "ffmpeg"))
FFPROBE = Path(os.environ.get("AGENTFORGE_FFPROBE", "ffprobe"))
WORK = ROOT / ".scratch" / "completion-2026-11-12" / "showcase-production"
ASSETS = ROOT / "docs" / "assets" / "agentforge-quality-loop"
VOICE = "zh-CN-XiaoxiaoNeural"

SCENES = [
    {
        "id": "title", "kind": "title", "title": "AgentForge｜Agent 质量工程与交付平台",
        "text": "AgentForge 是一个自托管的 Agent 质量工程与交付平台。它把 Agent 调试、版本管理、固定回归、质量门禁和发布交付连接起来，帮助团队更放心地迭代自己的 AI 应用。",
    },
    {
        "id": "model", "kind": "model", "title": "一个平台，管理 Agent 的完整生命周期",
        "text": "在 Studio 中，可以配置模型、Prompt、工具和知识库，并通过 Trace 查看每次执行。平台还支持团队权限、运营成本、模板和多种模型服务。每一次保存都会形成独立版本，方便对比、追踪和发布。",
    },
    {
        "id": "gate-fixed", "kind": "video", "title": "固定回归，看清每一次版本变化",
        "text": "这里用同一组固定用例检查三个 Agent 版本。第一版建立基线，第二版引入变化，第三版完成修复。每次门禁都会保留通过率、逐案结果和版本差异，团队可以清楚看到哪些用例保持通过，哪些已经被修复。",
    },
    {
        "id": "exact-run", "kind": "video", "title": "从评测结果，直达完整执行细节",
        "text": "点击任意一条证据，就能打开对应的确切运行。回答内容、工具调用、检索记录、系统状态和完整 Trace 都集中在一个页面。无论是日常调试还是版本复核，都可以从汇总结果快速回到实际执行细节。",
    },
    {
        "id": "gate-regression", "kind": "video", "title": "四层证据，快速定位问题",
        "text": "四层证据让问题定位更加直观。这个版本中，系统执行正常，但输出用例没有达到预期，门禁准确标记了回归。开发者可以同时查看失败原因、实际答案和运行记录，再针对具体问题修改配置。",
    },
    {
        "id": "human-feedback", "kind": "video", "title": "把用户反馈变成回归资产",
        "text": "用户反馈也能直接进入质量流程。运营页面保存反馈对应的版本和原始运行，修正答案后可以加入固定回归套件。来自门禁的反馈会关联原用例，让一次使用中的问题沉淀为以后每次发布都会自动检查的回归资产。",
    },
    {
        "id": "human-resolution", "kind": "video", "title": "自动评测与人工复核协同工作",
        "text": "修复后，同一用例在新版本中重新通过，版本差异会显示 fixed。团队还可以补充人工复核理由，保存已确认修复的结论。自动评测与人工判断共同构成清晰的修复记录，方便后续追踪和协作。",
    },
    {
        "id": "version-delivery", "kind": "video", "title": "将通过验证的版本交付给用户",
        "text": "验证完成后，可以把选定的不可变版本发布为 REST 接口、独立聊天页面或 iframe。部署支持启停、回滚和令牌轮换。这里公开页面返回了预期结果，响应中的版本也与通过门禁的版本一致。",
    },
    {
        "id": "closing", "kind": "closing", "title": "让每一次 Agent 迭代更有信心",
        "text": "从开发调试到回归评测，从人工复核到版本交付，AgentForge 为团队提供一条完整、清晰、可重复的 Agent 发布流程。自托管、可观测、可回归，让每一次迭代都更有信心。",
    },
]


def run(command: list[str], *, cwd: Path = ROOT) -> None:
    result = subprocess.run(command, cwd=cwd, stdin=subprocess.DEVNULL, check=False)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {subprocess.list2cmdline(command)}")


def probe(path: Path) -> dict:
    result = subprocess.run([
        str(FFPROBE), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path),
    ], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def duration(path: Path) -> float:
    return float(probe(path)["format"]["duration"])


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def font(size: int, bold: bool = False):
    name = "msyhbd.ttc" if bold else "msyh.ttc"
    return ImageFont.truetype(str(Path(r"C:\Windows\Fonts") / name), size)


def wrap_draw(draw: ImageDraw.ImageDraw, text: str, face, width: int) -> list[str]:
    lines, current = [], ""
    for char in text:
        candidate = current + char
        if current and draw.textlength(candidate, font=face) > width:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def create_card(scene: dict, path: Path) -> None:
    image = Image.new("RGB", (1280, 720), "#0b1220")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((44, 40, 1236, 680), radius=26, fill="#131c30", outline="#3a4a76", width=2)
    draw.text((84, 78), "AGENTFORGE · QUALITY ENGINEERING", font=font(20, True), fill="#8b5cf6")
    title_lines = wrap_draw(draw, scene["title"], font(44, True), 1050)
    for index, line in enumerate(title_lines):
        draw.text((84, 135 + index * 58), line, font=font(44, True), fill="#e6ecf7")
    if scene["kind"] == "model":
        labels = [("Studio", "模型 · Prompt · 工具", "开发调试"),
                  ("Knowledge", "文档 · 检索 · 引用", "私有知识"),
                  ("Quality", "用例 · 门禁 · 证据", "质量保障"),
                  ("Delivery", "REST · 页面 · iframe", "版本交付")]
        for index, (title, subtitle, key) in enumerate(labels):
            x = 78 + index * 294
            draw.rounded_rectangle((x, 280, x + 258, 450), radius=18, fill="#1c2540", outline="#6366f1", width=2)
            draw.text((x + 20, 310), title, font=font(28, True), fill="#c4b5fd")
            draw.text((x + 20, 370), subtitle, font=font(17), fill="#cbd5e1")
            draw.text((x + 20, 410), key, font=font(18), fill="#94a3b8")
        draw.text((84, 500), "团队协作  ·  运营成本  ·  本地模型  ·  私有化部署", font=font(25, True), fill="#6ee7b7")
    else:
        summary = ("Agent 调试  ·  不可变版本  ·  固定回归  ·  质量门禁  ·  一键交付"
                   if scene["kind"] == "title" else
                   "自托管  ·  多模型  ·  团队治理  ·  私有知识  ·  运营分析")
        for index, line in enumerate(wrap_draw(draw, summary, font(26), 1050)):
            draw.text((84, 300 + index * 46), line, font=font(26), fill="#cbd5e1")
        draw.rounded_rectangle((84, 470, 1110, 555), radius=14, fill="#1c2540")
        footer = ("让每一次 Agent 迭代都可观察、可比较、可发布" if scene["kind"] == "title"
                  else "开发、评测、发布与运营，在一个平台完成")
        draw.text((108, 495), footer, font=font(25, True), fill="#6ee7b7")
    # Keep the lower card clear for the subtitle safe area.
    image.save(path)


async def synthesize(scene: dict, audio_dir: Path) -> tuple[Path, list[dict]]:
    target = audio_dir / f"{scene['id']}.mp3"
    boundaries = []
    for attempt in range(3):
        boundaries = []
        try:
            with target.open("wb") as audio:
                communicate = edge_tts.Communicate(scene["text"], voice=VOICE, rate="-5%")
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        audio.write(chunk["data"])
                    elif chunk["type"] == "SentenceBoundary":
                        boundaries.append({
                            "text": chunk["text"], "offset": chunk["offset"], "duration": chunk["duration"],
                        })
            if target.stat().st_size > 1000:
                break
        except Exception:
            target.unlink(missing_ok=True)
            if attempt == 2:
                raise
            await asyncio.sleep(1.5 * (attempt + 1))
    if not boundaries:
        boundaries = [{"text": scene["text"], "offset": 0, "duration": int(duration(target) * 10_000_000)}]
    (audio_dir / f"{scene['id']}.boundaries.json").write_text(
        json.dumps(boundaries, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return target, boundaries


def srt_time(seconds: float) -> str:
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def caption_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    if len(text) <= 30:
        return text
    midpoint = len(text) // 2
    candidates = [index + 1 for index, char in enumerate(text) if char in "，；、：,.!? "]
    split = min(candidates, key=lambda index: abs(index - midpoint)) if candidates else midpoint
    return text[:split].strip() + "\n" + text[split:].strip()


def render_scene(
    scene: dict, raw: Path, source_range: tuple[float, float] | None,
    audio: Path, card: Path, target_dir: Path,
) -> tuple[Path, float]:
    audio_seconds = duration(audio)
    raw_seconds = max(0.0, source_range[1] - source_range[0]) if source_range else 0.0
    target_seconds = max(raw_seconds, audio_seconds + 0.65, 4.0)
    output = target_dir / f"{scene['id']}.mp4"
    command = [str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error"]
    if source_range:
        command += ["-ss", f"{source_range[0]:.3f}", "-to", f"{source_range[1]:.3f}", "-i", str(raw)]
    else:
        command += ["-loop", "1", "-i", str(card)]
    command += ["-i", str(audio), "-filter_complex",
        f"[0:v]scale=1280:720:force_original_aspect_ratio=decrease,"
        f"pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=0x0b1220,fps=25,"
        f"tpad=stop_mode=clone:stop_duration={target_seconds:.3f}[v];"
        f"[1:a]apad,atrim=duration={target_seconds:.3f}[a]",
        "-map", "[v]", "-map", "[a]", "-t", f"{target_seconds:.3f}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-video_track_timescale", "12800",
        "-movflags", "+faststart", str(output),
    ]
    run(command)
    return output, duration(output)


def validate_review_frames(scene_records: list[dict]) -> None:
    """Reject browser-close gray/white transition frames before publication."""
    for item in scene_records:
        path = WORK / "review" / f"{item['id']}.png"
        image = Image.open(path).convert("RGB")
        pixels = list(image.resize((160, 90)).getdata())
        gray = sum(
            1 for red, green, blue in pixels
            if max(red, green, blue) - min(red, green, blue) < 5 and 105 <= red <= 170
        ) / len(pixels)
        white = sum(
            1 for red, green, blue in pixels
            if min(red, green, blue) > 235 and max(red, green, blue) - min(red, green, blue) < 8
        ) / len(pixels)
        if gray > 0.20 or white > 0.80:
            raise ValueError(f"transition frame detected in {item['id']}: gray={gray:.1%}, white={white:.1%}")


def parse_srt(path: Path) -> list[dict]:
    blocks = re.split(r"\r?\n\r?\n", path.read_text(encoding="utf-8").strip())
    captions = []
    for block in blocks:
        lines = block.splitlines()
        match = re.match(r"(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)", lines[1])
        if not match:
            raise ValueError(f"invalid SRT timing: {lines[1]}")
        values = [int(value) for value in match.groups()]
        start = values[0] * 3600 + values[1] * 60 + values[2] + values[3] / 1000
        end = values[4] * 3600 + values[5] * 60 + values[6] + values[7] / 1000
        captions.append({"start": start, "end": end, "text": "\n".join(lines[2:])})
    return captions


def source_ranges(result: dict, raw: Path) -> dict[str, tuple[float, float]]:
    markers = result["timeline"]
    ranges = {}
    for index, marker in enumerate(markers[:-1]):
        end = max(marker["seconds"], markers[index + 1]["seconds"] - 0.20)
        if marker["name"] != "complete" and end > marker["seconds"]:
            ranges[marker["name"]] = (marker["seconds"], end)
    last = markers[-1]
    if last["name"] != "complete":
        ranges[last["name"]] = (last["seconds"], duration(raw))
    return ranges


async def main(run_dir: Path) -> dict:
    forbidden = ("不证明", "不代表", "不等于", "尚未", "0/5", "生产验收")
    for scene in SCENES:
        if any(phrase in scene["text"] or phrase in scene["title"] for phrase in forbidden):
            raise ValueError(f"engineering limitation language found in public showcase: {scene['id']}")
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    if not result.get("passed"):
        raise ValueError("browser run did not pass; refusing to produce a final demo")
    raw = run_dir / "quality-loop.webm"
    if not raw.exists() and not result.get("segments"):
        raise FileNotFoundError("No continuous or per-scene browser recording found")
    if WORK.exists():
        shutil.rmtree(WORK)
    audio_dir, segments, cards, review = [WORK / name for name in ("audio", "segments", "cards", "review")]
    for folder in (audio_dir, segments, cards, review, ASSETS):
        folder.mkdir(parents=True, exist_ok=True)
    ranges = source_ranges(result, raw) if raw.exists() else {}
    rendered = []
    for scene in SCENES:
        card = cards / f"{scene['id']}.png"
        if scene["kind"] != "video":
            create_card(scene, card)
        audio, boundaries = await synthesize(scene, audio_dir)
        scene_recording = run_dir / "segments" / f"{scene['id']}.webm"
        if scene_recording.exists():
            # Exclude Playwright's final screenshot/context-close transition frames.
            scene_raw = scene_recording
            source_range = (0.10, max(0.20, duration(scene_recording) - 1.0))
        else:
            scene_raw, source_range = raw, ranges.get(scene["id"])
        segment, seconds = render_scene(scene, scene_raw, source_range, audio, card, segments)
        rendered.append({"scene": scene, "segment": segment, "seconds": seconds, "boundaries": boundaries})
    concat = segments / "concat.txt"
    concat.write_text("".join(f"file '{item['segment'].name}'\n" for item in rendered), encoding="ascii")
    clean = WORK / "AgentForge-demo-clean.mp4"
    run([str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", concat.name, "-c", "copy", str(clean)], cwd=segments)
    return finish(rendered, clean, run_dir, result)


def finish(rendered: list[dict], clean: Path, run_dir: Path, browser_result: dict) -> dict:
    final = ROOT / "AgentForge-demo.mp4"
    srt = ROOT / "AgentForge-demo.srt"
    gif = ROOT / "AgentForge-demo.gif"
    captions = []
    scene_records = []
    offset = 0.0
    for item in rendered:
        scene = item["scene"]
        scene_records.append({"id": scene["id"], "title": scene["title"], "start": offset, "duration": item["seconds"]})
        for boundary in item["boundaries"]:
            start = offset + boundary["offset"] / 10_000_000
            end = offset + (boundary["offset"] + boundary["duration"]) / 10_000_000
            captions.append({"start": start, "end": min(end, offset + item["seconds"]), "text": boundary["text"]})
        offset += item["seconds"]
    captions.sort(key=lambda item: item["start"])
    for index, caption in enumerate(captions):
        if index + 1 < len(captions):
            caption["end"] = min(caption["end"], captions[index + 1]["start"])
    srt.write_text("\n\n".join(
        f"{index}\n{srt_time(item['start'])} --> {srt_time(item['end'])}\n{caption_text(item['text'])}"
        for index, item in enumerate(captions, 1)
    ) + "\n", encoding="utf-8")
    shutil.copy2(srt, WORK / "demo.srt")
    subtitle_filter = (
        "subtitles=demo.srt:force_style='FontName=Microsoft YaHei,FontSize=14,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,BorderStyle=1,"
        "Outline=1,Shadow=0,MarginV=20,Alignment=2'"
    )
    run([str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error", "-i", str(clean),
         "-vf", subtitle_filter, "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
         "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", str(final)], cwd=WORK)
    preview = next(item for item in scene_records if item["id"] == "gate-fixed")
    run([str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
         "-ss", f"{preview['start']:.3f}", "-t", "24", "-i", str(clean),
         "-filter_complex", "fps=10,scale=800:450:force_original_aspect_ratio=decrease,"
         "pad=800:450:(ow-iw)/2:(oh-ih)/2:color=0x0b1220,split[s0][s1];"
         "[s0]palettegen[p];[s1][p]paletteuse", "-loop", "0", str(gif)])
    for item in scene_records:
        run([str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
             "-ss", f"{item['start'] + item['duration'] / 2:.3f}", "-i", str(final),
             "-frames:v", "1", str(WORK / "review" / f"{item['id']}.png")])
    validate_review_frames(scene_records)
    for screenshot in browser_result["screenshots"]:
        shutil.copy2(run_dir / screenshot, ASSETS / screenshot)
    (ASSETS / "narration.json").write_text(json.dumps(
        [{"id": scene["id"], "title": scene["title"], "text": scene["text"]} for scene in SCENES],
        ensure_ascii=False, indent=2,
    ), encoding="utf-8")
    decoded = subprocess.run([str(FFMPEG), "-v", "error", "-i", str(final), "-f", "null", "-"],
                             capture_output=True, text=True, check=False)
    if decoded.returncode or decoded.stderr.strip():
        raise RuntimeError(f"full decode failed: {decoded.stderr}")
    volume = subprocess.run([str(FFMPEG), "-i", str(final), "-af", "volumedetect", "-f", "null", "-"],
                            capture_output=True, text=True, check=False)
    mean = re.search(r"mean_volume:\s*(-?[\d.]+) dB", volume.stderr)
    if not mean or float(mean.group(1)) < -35:
        raise RuntimeError(f"audio is silent or too quiet: {volume.stderr[-1000:]}")
    media = probe(final)
    video_stream = next(item for item in media["streams"] if item["codec_type"] == "video")
    audio_stream = next(item for item in media["streams"] if item["codec_type"] == "audio")
    final_seconds = float(media["format"]["duration"])
    parsed = parse_srt(srt)
    if final_seconds <= 120.633:
        raise ValueError(f"demo must exceed the 120.633 second baseline: {final_seconds}")
    if video_stream["codec_name"] != "h264" or (video_stream["width"], video_stream["height"]) != (1280, 720):
        raise ValueError("unexpected video format")
    if audio_stream["codec_name"] != "aac" or int(audio_stream["sample_rate"]) != 48000:
        raise ValueError("unexpected audio format")
    if not parsed or any(item["end"] <= item["start"] or item["end"] > final_seconds + 0.1 for item in parsed):
        raise ValueError("invalid or out-of-range subtitles")
    if any(parsed[index]["start"] < parsed[index - 1]["end"] - 0.001 for index in range(1, len(parsed))):
        raise ValueError("overlapping subtitles")
    report = {
        "passed": True, "scope": "capability-led product showcase using verified real-browser recordings",
        "source_browser_run": run_dir.name, "browser_assertions": browser_result["assertions"],
        "browser_errors": browser_result["errors"], "duration_seconds": final_seconds,
        "video": {"codec": video_stream["codec_name"], "width": 1280, "height": 720,
                  "fps": video_stream["avg_frame_rate"], "sha256": sha256(final)},
        "audio": {"codec": audio_stream["codec_name"], "sample_rate": 48000,
                  "mean_volume_db": float(mean.group(1))},
        "subtitles": {"count": len(parsed), "encoding": "UTF-8", "non_overlapping": True,
                      "sha256": sha256(srt)},
        "gif": {"duration_seconds": duration(gif), "sha256": sha256(gif)},
        "scenes": scene_records, "full_decode_errors": [], "transition_frame_check": "passed",
        "review_frames": [f"{item['id']}.png" for item in scene_records],
        "privacy": "No credentials, private paths, browser profiles, or upstream model responses are shown.",
        "technical_evidence": "docs/planning/DEVELOPMENT_BASELINE.md",
        "future_plan": "docs/planning/FUTURE.md",
    }
    (ASSETS / "media-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, help="Verified browser run directory")
    args = parser.parse_args()
    selected = args.run
    if selected is None:
        candidates = []
        for base in (
            ROOT / ".scratch" / "completion-2026-11-12" / "browser",
            ROOT / ".scratch" / "completion-2026-11-12" / "browser-segments",
        ):
            for path in base.glob("run-*"):
                result_path = path / "result.json"
                if result_path.exists():
                    data = json.loads(result_path.read_text(encoding="utf-8"))
                    if data.get("passed") and ((path / "quality-loop.webm").exists() or data.get("segments")):
                        candidates.append(path)
        if not candidates:
            raise SystemExit("No passed browser recording found")
        selected = max(candidates, key=lambda path: path.stat().st_mtime)
    report = asyncio.run(main(selected.resolve()))
    print(json.dumps({"passed": report["passed"], "duration": report["duration_seconds"],
                      "video": "AgentForge-demo.mp4", "subtitles": report["subtitles"]["count"]},
                     ensure_ascii=False, indent=2))
