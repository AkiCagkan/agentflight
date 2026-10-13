"""Records docs/images/demo.gif: the panel while examples/02_research.py runs (parallel agents, tools, approval).

    python scripts/record_demo.py [MODEL]          # default: fake (offline); e.g. lmstudio/qwen/qwen3-4b-2507

Needs Playwright with Chrome (or its bundled Chromium) and Pillow.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images" / "demo.gif"
WIDTH, HEIGHT = 1280, 760
FRAME_MS = 300


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    model = sys.argv[1] if len(sys.argv) > 1 else "fake"
    home = tempfile.mkdtemp(prefix="agentflight-demo-home-")
    project = Path(tempfile.mkdtemp(prefix="agentflight-demo-")) / "examples"
    shutil.copytree(ROOT / "examples", project, ignore=shutil.ignore_patterns(".agentflight", "__pycache__"))
    env = {**os.environ, "AGENTFLIGHT_HOME": home, "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1",
           "AGENTFLIGHT_FAKE_DELAY": "0.04",
           "PYTHONPATH": str(ROOT)}
    for key in ("AGENTFLIGHT_QUIET", "AGENTFLIGHT_PANEL", "AGENTFLIGHT_PANEL_SOCKET", "AGENTFLIGHT_PANEL_TOKEN"):
        env.pop(key, None)
    panel = subprocess.Popen([sys.executable, "-m", "agentflight", "watch", "--no-browser", "--port", str(free_port())],
                             env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    frames: list[Image.Image] = []
    try:
        discovery = Path(home) / "panel.json"
        for _ in range(100):
            if discovery.exists():
                break
            time.sleep(0.1)
        url = f"http://127.0.0.1:{json.loads(discovery.read_text(encoding='utf-8'))['port']}/"
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome")
            except Exception:  # noqa: BLE001 - Chrome is not installed: use Playwright's Chromium
                browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=1)
            page.goto(url)
            page.wait_for_selector(".agent-tree")

            def snap(repeat: int = 1):
                image = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
                frames.extend([image] * repeat)

            snap(4)
            program = subprocess.Popen([sys.executable, "02_research.py"], cwd=project,
                                       env={**env, "AGENTFLIGHT_MODEL": model}, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
            approved = False
            deadline = time.time() + 600
            while time.time() < deadline:
                started = time.time()
                snap()
                if not approved and page.locator("#approvalOverlay:not(.hidden)").count():
                    snap(8)
                    page.click("#approveBtn")
                    approved = True
                if program.poll() is not None:
                    break
                time.sleep(max(0.0, FRAME_MS / 1000 - (time.time() - started)))
            time.sleep(0.5)
            snap(3)
            page.click("#detailTabs button[data-tab=steps]")
            page.locator(".step-card").nth(3).click()
            snap(10)
            browser.close()
    finally:
        panel.terminate()
    # Long runs (a slow local model) are thinned out so the GIF stays short.
    if len(frames) > 140:
        step = len(frames) / 140
        frames = [frames[int(i * step)] for i in range(140)]
    small = [f.resize((960, int(960 * HEIGHT / WIDTH)), Image.LANCZOS) for f in frames]
    palette = small[len(small) // 2].quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    gif = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in small]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    durations = [FRAME_MS] * (len(gif) - 1) + [3000]
    gif[0].save(OUT, save_all=True, append_images=gif[1:], duration=durations, loop=0, optimize=True)
    print(f"{OUT}  ({len(gif)} frames, {OUT.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
