"""Builds the JetBrains plugin (PyCharm, IntelliJ IDEA, PhpStorm... 2024.1+) without Gradle.

Compiles editors/jetbrains against the jars of an installed JetBrains IDE (its bundled JBR has javac)
and writes dist/agentflight-jetbrains-<version>.zip, installable with Settings | Plugins | ⚙ | Install Plugin from Disk.

Usage: python scripts/build_jetbrains.py ["C:\\Program Files\\JetBrains\\<IDE>"]
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "editors" / "jetbrains" / "src" / "main"
DIST = ROOT / "dist"


def find_ide() -> Path:
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    for base in (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "JetBrains",
                 Path(os.environ.get("LOCALAPPDATA", "")) / "Programs"):
        if base.is_dir():
            for candidate in sorted(base.iterdir(), reverse=True):
                if (candidate / "lib").is_dir() and (candidate / "jbr" / "bin").is_dir():
                    return candidate
    raise SystemExit("No JetBrains IDE found; pass its install folder as an argument.")


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from agentflight import __version__

    ide = find_ide()
    javac = ide / "jbr" / "bin" / ("javac.exe" if os.name == "nt" else "javac")
    jars = sorted((ide / "lib").rglob("*.jar"))
    jcef = ide / "plugins" / "jcef-plugin" / "lib"  # 2025+: JCEF is a separate bundled plugin
    if jcef.is_dir():
        jars += sorted(jcef.rglob("*.jar"))
    temp = Path(tempfile.mkdtemp(prefix="agentflight-jetbrains-"))
    classes = temp / "classes"
    classes.mkdir()
    classpath = temp / "classpath.txt"
    # In a javac argument file the backslash is an escape character: paths are written with /
    classpath.write_text(os.pathsep.join(j.as_posix() for j in jars), encoding="utf-8")
    sources = sorted((SOURCE / "java").rglob("*.java"))
    args_file = temp / "args.txt"
    args_file.write_text("\n".join(
        ["-encoding", "UTF-8", "--release", "17", "-nowarn", "-d", f'"{classes.as_posix()}"',
         "-cp", f'"{classpath.read_text(encoding="utf-8")}"']
        + [f'"{s.as_posix()}"' for s in sources]), encoding="utf-8")
    result = subprocess.run([str(javac), f"@{args_file}"], capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout + result.stderr)
        return 1
    for file in (SOURCE / "resources").rglob("*"):
        if file.is_file():
            target = classes / file.relative_to(SOURCE / "resources")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)
    xml = classes / "META-INF" / "plugin.xml"
    xml.write_text(re.sub(r"<version>[^<]*</version>", f"<version>{__version__}</version>",
                          xml.read_text(encoding="utf-8"), count=1), encoding="utf-8")
    jar = temp / "agentflight.jar"
    with zipfile.ZipFile(jar, "w", zipfile.ZIP_DEFLATED) as z:
        for file in sorted(classes.rglob("*")):
            if file.is_file():
                z.write(file, file.relative_to(classes).as_posix())
    DIST.mkdir(exist_ok=True)
    package = DIST / f"agentflight-jetbrains-{__version__}.zip"
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(jar, "agentflight/lib/agentflight.jar")
    shutil.rmtree(temp, ignore_errors=True)
    print(f"built with {ide.name}: {package} ({package.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
