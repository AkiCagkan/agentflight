"""The watch panel (`agentflight watch`) end to end in a real browser (Playwright + Chrome or bundled Chromium).

A program started from a plain terminal (`python 02_research.py`) must show up in the panel live, its approval must
be answerable from the panel, and a recorded run must be replayable from a step (earlier steps come from the record).

Usage: python tests/panel_browser_test.py [screenshot_dir]     (default: docs/screenshots-test/)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import expect, sync_playwright  # noqa: E402

from tests.helpers import ROOT, TESTS, cleanup, wait_until  # noqa: E402

SCREENSHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "screenshots-test"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def environment(home: str) -> dict:
    """Environment of the panel and the program: isolated settings, slow fake model, fake Claude Code."""
    env = {**os.environ, "AGENTFLIGHT_HOME": home, "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1",
           "AGENTFLIGHT_FAKE_DELAY": "0.05"}
    for name in ("AGENTFLIGHT_QUIET", "AGENTFLIGHT_PANEL", "AGENTFLIGHT_PANEL_SOCKET", "AGENTFLIGHT_PANEL_TOKEN",
                 "AGENTFLIGHT_MODEL"):
        env.pop(name, None)
    fake_claude = TESTS / "fake_claude.py"
    if fake_claude.is_file():
        env["AGENTFLIGHT_CLAUDE"] = str(fake_claude)
    return env


def launch_browser(p):
    try:
        return p.chromium.launch(channel="chrome", headless=True)
    except Exception:  # noqa: BLE001 - Chrome is not installed (CI): the bundled Chromium
        return p.chromium.launch(headless=True)


def browser_scenario(url: str, project: Path, env: dict, programs: list, errors: list[str]) -> None:
    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_page(viewport={"width": 1500, "height": 950}, device_scale_factor=1)
        page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"page error: {e}"))
        try:
            page.goto(url)
            expect(page.locator(".agent-tree")).to_contain_text("No agents have run yet")
            expect(page.locator("#statusPill")).to_have_text("ready")
            page.screenshot(path=str(SCREENSHOTS / "panel-01-empty.png"))

            # a plain `python` run from "another terminal"
            out_path, err_path = project / "program-stdout.txt", project / "program-stderr.txt"
            with open(out_path, "wb") as out, open(err_path, "wb") as err:
                program = subprocess.Popen([sys.executable, "02_research.py"], cwd=project,
                                           env={**env, "AGENTFLIGHT_MODEL": "fake"}, stdout=out, stderr=err)
            programs.append(program)
            page.wait_for_selector(".node", timeout=30000)
            expect(page.locator(".pill.running").first).to_have_text("running")
            page.wait_for_timeout(600)
            page.screenshot(path=str(SCREENSHOTS / "panel-02-live.png"))

            # approval from the panel
            page.wait_for_selector("#approvalOverlay:not(.hidden)", timeout=60000)
            expect(page.locator("#approvalTitle")).to_have_text("Publish this newsletter?")
            expect(page.locator("#approveBtn")).to_have_text("✓ Approve")
            page.screenshot(path=str(SCREENSHOTS / "panel-03-approval.png"))
            page.click("#approveBtn")
            expect(page.locator("#approvalOverlay")).to_be_hidden()
            program.wait(timeout=60)
            stdout = out_path.read_text(encoding="utf-8", errors="replace")
            stderr = err_path.read_text(encoding="utf-8", errors="replace")
            assert program.returncode == 0, f"exit code {program.returncode}\n{stdout}\n{stderr}"
            assert "TR:" in stdout, stdout
            expect(page.locator("#statusPill")).to_have_text("done", timeout=30000)
            assert page.locator(".step-card").count() == 5, "five agent calls expected"

            # the program's output is mirrored into the Output tab
            page.click("#detailTabs button[data-tab=output]")
            expect(page.locator("#console")).to_contain_text("TR:")
            page.click("#detailTabs button[data-tab=steps]")

            # replay from step 4 (steps 1-3 come from the record)
            page.locator(".step-card").nth(3).click()
            before = page.locator("#runSelect option").count()
            page.click('[data-action=replay][data-step="4"]')
            page.wait_for_function(f"document.querySelectorAll('#runSelect option').length > {before}")
            try:
                page.wait_for_selector("#approvalOverlay:not(.hidden)", timeout=60000)
            except Exception:
                page.click("#detailTabs button[data-tab=output]")
                print("OUTPUT:", page.locator("#console").inner_text()[-1500:])
                raise
            page.click("#approveBtn")
            expect(page.locator("#statusPill")).to_have_text("done", timeout=60000)
            from_record = page.locator(".step-card .step-status.from-record").count()
            assert from_record == 3, f"steps 1-3 should come from the record, got {from_record}"
            assert page.locator(".step-card").count() == 5, "five steps expected in the replay"
            page.locator(".step-card").first.click()
            page.wait_for_timeout(300)
            page.screenshot(path=str(SCREENSHOTS / "panel-04-replay.png"))

            # history of the known projects (both runs)
            page.click("#detailTabs button[data-tab=history]")
            page.wait_for_selector("#historyPage .list-item")
            expect(page.locator("#historyPage .list-item")).to_have_count(2)
            expect(page.locator("#historyPage .list-item").first).to_contain_text("examples/")
            page.screenshot(path=str(SCREENSHOTS / "panel-05-history.png"))

            # accounts settings
            page.click("#settingsBtn")
            page.wait_for_selector(".account-card", timeout=15000)
            expect(page.locator("#chatgptSignIn")).to_have_text("Continue with ChatGPT")
            expect(page.locator(".account-card").nth(1)).to_contain_text("connected · test@example.com")
            page.screenshot(path=str(SCREENSHOTS / "panel-06-accounts.png"))

            # signed out of Claude Code: the card offers `agentflight login claude` (works in every shell)
            def signed_out(route):
                response = route.fetch()
                data = response.json()
                data["claude"]["signed_in"] = False
                route.fulfill(response=response, json=data)

            refreshed = re.compile(r"/api/accounts\?refresh=1$")
            page.route(refreshed, signed_out)
            page.click("#claudeRefresh")
            claude_command = page.locator(".account-card").nth(1).locator("code.command")
            expect(claude_command).to_have_text("agentflight login claude")
            page.unroute(refreshed)

            page.click("#settingsTabs [data-settings=general]")
            expect(page.locator("#editorSelect")).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.locator("#settingsOverlay")).to_be_hidden()

            # narrow (editor side panel) layout
            page.set_viewport_size({"width": 560, "height": 900})
            page.click("#detailTabs button[data-tab=steps]")
            page.wait_for_timeout(300)
            page.screenshot(path=str(SCREENSHOTS / "panel-07-narrow.png"))
        except BaseException:
            try:
                page.screenshot(path=str(SCREENSHOTS / "panel-failure.png"))
            except Exception:  # noqa: BLE001 - keep the original error
                pass
            raise
        finally:
            browser.close()


def main() -> int:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    home = tempfile.mkdtemp(prefix="agentflight-panel-home-")
    work = Path(tempfile.mkdtemp(prefix="agentflight-panel-test-"))
    project = work / "examples"
    shutil.copytree(ROOT / "examples", project, ignore=shutil.ignore_patterns(".agentflight", "__pycache__"))
    env = environment(home)
    panel_log = work / "panel.log"
    with open(panel_log, "wb") as log:
        command = [sys.executable, "-m", "agentflight", "watch", "--no-browser", "--port", str(free_port())]
        panel = subprocess.Popen(command,
                                 cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT)
    programs: list[subprocess.Popen] = []
    errors: list[str] = []
    failed = False
    try:
        discovery = Path(home) / "panel.json"
        if not wait_until(lambda: discovery.exists() or panel.poll() is not None, timeout=20, interval=0.1):
            raise RuntimeError("the panel did not start")
        if panel.poll() is not None:
            raise RuntimeError(f"the panel exited with code {panel.returncode}")
        info = json.loads(discovery.read_text(encoding="utf-8"))
        browser_scenario(f"http://127.0.0.1:{info['port']}/", project, env, programs, errors)
    except Exception:  # noqa: BLE001 - reported below
        failed = True
        traceback.print_exc()
    finally:
        for program in programs:
            if program.poll() is None:
                program.kill()
        panel.terminate()
        try:
            panel.wait(10)
        except subprocess.TimeoutExpired:
            panel.kill()
    if failed:
        print("PANEL LOG:\n" + panel_log.read_text(encoding="utf-8", errors="replace")[-3000:])
        for name in ("program-stdout.txt", "program-stderr.txt"):
            if (project / name).exists():
                print(f"{name}:\n" + (project / name).read_text(encoding="utf-8", errors="replace")[-3000:])
    if errors:
        print("BROWSER ERRORS:")
        for error in errors:
            print("  ", error)
    cleanup(work)
    cleanup(Path(home))
    if failed or errors:
        print("PANEL BROWSER TEST FAILED")
        return 1
    print("screenshots:", SCREENSHOTS)
    print("PANEL BROWSER TEST OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
