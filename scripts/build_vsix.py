"""Packages editors/vscode as a .vsix (VS Code, Cursor, VSCodium...) without Node or vsce.

Usage: python scripts/build_vsix.py          -> dist/agentflight-vscode-<version>.vsix
Install: code --install-extension dist/agentflight-vscode-<version>.vsix   (Cursor: cursor --install-extension ...)
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
EXTENSION = ROOT / "editors" / "vscode"
DIST = ROOT / "dist"
EXCLUDE = {"test"}

CONTENT_TYPES = {
    ".json": "application/json", ".js": "application/javascript", ".md": "text/markdown", ".png": "image/png",
    ".svg": "image/svg+xml", ".txt": "text/plain", ".vsixmanifest": "text/xml",
}


def manifest(pkg: dict) -> str:
    properties = {
        "Microsoft.VisualStudio.Code.Engine": pkg["engines"]["vscode"],
        "Microsoft.VisualStudio.Code.ExtensionDependencies": "",
        "Microsoft.VisualStudio.Code.ExtensionPack": "",
        "Microsoft.VisualStudio.Code.ExtensionKind": "workspace",
        "Microsoft.VisualStudio.Code.LocalizedLanguages": "",
        "Microsoft.VisualStudio.Services.GitHubFlavoredMarkdown": "true",
    }
    lines = "\n".join(f'      <Property Id="{k}" Value="{escape(v)}" />' for k, v in properties.items())
    return f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011"
    xmlns:d="http://schemas.microsoft.com/developer/vsx-schema-design/2011">
  <Metadata>
    <Identity Language="en-US" Id="{pkg['name']}" Version="{pkg['version']}" Publisher="{pkg['publisher']}" />
    <DisplayName>{escape(pkg['displayName'])}</DisplayName>
    <Description xml:space="preserve">{escape(pkg['description'])}</Description>
    <Tags>{escape(",".join(pkg.get('keywords', [])))}</Tags>
    <Categories>{escape(",".join(pkg.get('categories', [])))}</Categories>
    <GalleryFlags>Public</GalleryFlags>
    <Properties>
{lines}
    </Properties>
    <License>extension/LICENSE.txt</License>
    <Icon>extension/{pkg['icon']}</Icon>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code"/>
  </Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true" />
    <Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md" Addressable="true" />
    <Asset Type="Microsoft.VisualStudio.Services.Content.License" Path="extension/LICENSE.txt" Addressable="true" />
    <Asset Type="Microsoft.VisualStudio.Services.Icons.Default" Path="extension/{pkg['icon']}" Addressable="true" />
  </Assets>
</PackageManifest>
"""


def main() -> int:
    pkg = json.loads((EXTENSION / "package.json").read_text(encoding="utf-8"))
    files = [f for f in sorted(EXTENSION.rglob("*"))
             if f.is_file() and not (set(f.relative_to(EXTENSION).parts) & EXCLUDE)]
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8") if (ROOT / "LICENSE").is_file() else "MIT License\n"
    suffixes = sorted({f.suffix.lower() for f in files} | {".txt", ".vsixmanifest", ".json", ".md"})
    defaults = "".join(f'<Default Extension="{s}" ContentType="{CONTENT_TYPES.get(s, "application/octet-stream")}"/>'
                       for s in suffixes if s)
    content_types = ('<?xml version="1.0" encoding="utf-8"?>\n'
                     f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">{defaults}</Types>')
    DIST.mkdir(exist_ok=True)
    target = DIST / f"agentflight-vscode-{pkg['version']}.vsix"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("extension.vsixmanifest", manifest(pkg))
        z.writestr("[Content_Types].xml", content_types)
        for f in files:
            z.write(f, "extension/" + f.relative_to(EXTENSION).as_posix())
        z.writestr("extension/LICENSE.txt", license_text)
    print(f"{target} ({target.stat().st_size // 1024} KB, {len(files)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
