"""Installs dist/agentflight-vscode-<version>.vsix into an isolated VS Code (own user data and extensions folders) with
the VS Code CLI and checks that VS Code accepts and lists it. No window is opened. The .vsix is built with
scripts/build_vsix.py when it is missing. Skipped when VS Code is not installed.

Usage: python tests/vsix_install_test.py [Code.exe]
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_JSON = ROOT / "editors" / "vscode" / "package.json"
CODE_EXE_OVERRIDE: Path | None = None


def find_code_exe() -> Path | None:
    if CODE_EXE_OVERRIDE is not None:
        return CODE_EXE_OVERRIDE
    candidates = (Path(os.environ.get("TEMP", "")) / "agentflight-vscode-portable" / "vscode" / "Code.exe",
                  Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Microsoft VS Code" / "Code.exe")
    return next((p for p in candidates if p.is_file()), None)


class VsixInstallTest(unittest.TestCase):
    def test_vsix_installs(self):
        exe = find_code_exe()
        if exe is None:
            raise unittest.SkipTest("VS Code is not installed")
        # newer VS Code builds keep resources/ in a folder named after the commit
        cli = next(exe.parent.glob("*/resources/app/out/cli.js"), None) or exe.parent / "resources/app/out/cli.js"
        version = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["version"]
        vsix = ROOT / "dist" / f"agentflight-vscode-{version}.vsix"
        if not vsix.is_file():
            build = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_vsix.py")], capture_output=True,
                                   text=True, timeout=120)
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
        temp = Path(tempfile.mkdtemp(prefix="agentflight-vsix-"))
        env = {**os.environ, "ELECTRON_RUN_AS_NODE": "1"}
        base = [str(exe), str(cli), "--user-data-dir", str(temp / "user-data"),
                "--extensions-dir", str(temp / "extensions")]
        try:
            install = subprocess.run(base + ["--install-extension", str(vsix)], env=env, capture_output=True, text=True,
                                     timeout=180)
            print(install.stdout.strip(), install.stderr.strip()[-500:])
            listed = subprocess.run(base + ["--list-extensions", "--show-versions"], env=env, capture_output=True,
                                    text=True, timeout=120)
            print("installed:", listed.stdout.strip())
        finally:
            shutil.rmtree(temp, ignore_errors=True)
        self.assertIn(f"agentflight.agentflight@{version}", listed.stdout, install.stdout + install.stderr[-500:])


if __name__ == "__main__":
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        CODE_EXE_OVERRIDE = Path(sys.argv.pop(1))
    unittest.main()
