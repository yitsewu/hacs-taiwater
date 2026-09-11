"""只封裝白名單程式檔；不包含設定、測試快取或私人資料。"""
import json
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

root = Path(__file__).resolve().parents[1]
component = root / "custom_components" / "taiwater"
version = json.loads((component / "manifest.json").read_text(encoding="utf-8"))["version"]
output = root / "dist"
output.mkdir(exist_ok=True)
files = sorted(p for p in component.rglob("*") if p.is_file() and p.suffix in {".py", ".json", ".yaml", ".svg", ".png"} and "__pycache__" not in p.parts)
with ZipFile(output / f"taiwater-{version}.zip", "w", ZIP_DEFLATED) as archive:
    for path in files:
        archive.write(path, path.relative_to(root))
print(json.dumps({"version": version, "files": len(files), "archive": str(output / f"taiwater-{version}.zip")}))
