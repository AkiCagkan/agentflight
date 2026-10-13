"""Runs the VS Code extension's logic tests (editors/vscode/test/unit.js) against a real AgentFlight panel.

unit.js runs under Node (``node`` on PATH) or, without Node, under VS Code's own runtime (``Code.exe`` with
ELECTRON_RUN_AS_NODE=1). No VS Code window is opened. The test is skipped when neither is available.

Usage: python tests/vscode_unit_test.py [node-or-Code.exe]
       python -m unittest tests.vscode_unit_test -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNIT_JS = ROOT / "editors" / "vscode" / "test" / "unit.js"
# A runtime given on the command line (node or Code.exe) wins over the automatic search.
RUNTIME_OVERRIDE: Path | None = None
# Variables that would keep the example program from connecting to the panel.
PANEL_BLOCKERS = ("AGENTFLIGHT_QUIET", "AGENTFLIGHT_PANEL", "AGENTFLIGHT_PANEL_SOCKET", "AGENTFLIGHT_PANEL_TOKEN")


def find_code_exe() -> Path | None:
    candidates = (Path(os.environ.get("TEMP", "")) / "agentflight-vscode-portable" / "vscode" / "Code.exe",
                  Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Microsoft VS Code" / "Code.exe")
    return next((p for p in candidates if p.is_file()), None)


def js_runtime() -> str | None:
    """node from PATH, else VS Code's Code.exe (run as Node through ELECTRON_RUN_AS_NODE), else None."""
    if RUNTIME_OVERRIDE is not None:
        return str(RUNTIME_OVERRIDE)
    node = shutil.which("node")
    if node:
        return node
    exe = find_code_exe()
    return str(exe) if exe else None


class VSCodeExtensionTest(unittest.TestCase):
    def test_extension_against_real_panel(self):
        runtime = js_runtime()
        if runtime is None:
            raise unittest.SkipTest("neither node nor VS Code is installed")
        # resolve(): the events carry resolved paths (no 8.3 short names on Windows, no /var symlink on macOS)
        temp = Path(tempfile.mkdtemp(prefix="agentflight-vscode-unit-")).resolve()
        project = temp / "examples"
        shutil.copytree(ROOT / "examples", project, ignore=shutil.ignore_patterns(".agentflight", "__pycache__"))
        env = {**os.environ, "ELECTRON_RUN_AS_NODE": "1", "AGENTFLIGHT_PYTHON": sys.executable,
               "AGENTFLIGHT_PROJECT": str(project),
               "AGENTFLIGHT_HOME": str(temp / "home"), "PYTHONIOENCODING": "utf-8",
               "AGENTFLIGHT_CLAUDE": str(ROOT / "tests" / "fake_claude.py"),
               "PYTHONPATH": os.pathsep.join(p for p in (str(ROOT), os.environ.get("PYTHONPATH")) if p)}
        for name in PANEL_BLOCKERS:
            env.pop(name, None)
        try:
            result = subprocess.run([runtime, str(UNIT_JS)], env=env, capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=300)
        finally:
            try:
                subprocess.run([sys.executable, "-m", "agentflight", "watch", "--stop"], env=env, capture_output=True,
                               timeout=60)
            except subprocess.TimeoutExpired:
                pass
            shutil.rmtree(temp, ignore_errors=True)
        output = result.stdout + result.stderr
        print(output)
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("ALL OK", result.stdout, output)


if __name__ == "__main__":
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        RUNTIME_OVERRIDE = Path(sys.argv.pop(1))
    unittest.main()
