"""只封裝白名單程式檔；不包含設定、測試快取或私人資料。"""
import json
from pathlib import Path
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED
import hashlib


def add_source(archive, path, root):
    """Stable bytes and metadata make identical source releases reproducible."""
    info = ZipInfo(path.relative_to(root).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.compress_type = ZIP_DEFLATED
    content = path.read_bytes()
    if path.suffix in {".py", ".json", ".yaml", ".svg", ".md", ".txt"} or path.name == "Dockerfile":
        content = content.replace(b"\r\n", b"\n")
    archive.writestr(info, content)


root = Path(__file__).resolve().parents[1]
component = root / "custom_components" / "taiwater"
version = json.loads((component / "manifest.json").read_text(encoding="utf-8"))["version"]
output = root / "dist"
output.mkdir(exist_ok=True)
files = sorted((p for p in component.rglob("*") if p.is_file() and p.suffix in {".py", ".json", ".yaml", ".svg", ".png", ".npz", ".txt", ".md"} and "__pycache__" not in p.parts), key=lambda p: p.as_posix())
with ZipFile(output / f"taiwater-{version}.zip", "w", ZIP_DEFLATED) as archive:
    for path in files:
        add_source(archive, path, root)
print(json.dumps({"version": version, "files": len(files), "archive": str(output / f"taiwater-{version}.zip")}))
addon = root / "taiwater_ocr"
addon_output = output / f"taiwater-ocr-{version}.zip"
with ZipFile(addon_output, "w", ZIP_DEFLATED) as archive:
    for path in sorted(addon.rglob("*"), key=lambda p: p.as_posix()):
        if path.is_file() and "__pycache__" not in path.parts and (path.suffix in {".py", ".yaml", ".md", ".txt"} or path.name == "Dockerfile"):
            add_source(archive, path, root)
archives = [output / f"taiwater-{version}.zip", addon_output]
(output / "SHA256SUMS").write_text("".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in archives), encoding="utf-8")
