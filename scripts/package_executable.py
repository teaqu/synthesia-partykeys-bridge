"""Package a native executable, launcher, docs, and licenses for a release."""
from pathlib import Path
import argparse
import hashlib
import shutil
import subprocess
import sys
import zipfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("platform", choices=("windows-x64", "macos-arm64", "macos-x64"))
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
name = "partykeys-" + args.platform
bundle = root / "dist" / name
bundle.mkdir(parents=True, exist_ok=True)
windows = args.platform.startswith("windows-")
exe = "partykeys.exe" if windows else "partykeys"
launcher = "Start Synthesia bridge.cmd" if windows else "Start Synthesia bridge.command"
for source in (root / "dist" / exe, root / "packaging" / launcher,
               root / "README.md", root / "LICENSE"):
    shutil.copy2(source, bundle / source.name)
if not windows:
    (bundle / exe).chmod(0o755)
    (bundle / launcher).chmod(0o755)
shutil.copytree(root / "examples", bundle / "examples", dirs_exist_ok=True)
shutil.copytree(root / "docs" / "images", bundle / "docs" / "images", dirs_exist_ok=True)
subprocess.run([sys.executable, str(root / "scripts" / "bundle_notices.py"),
                str(bundle)], check=True)
archive = root / "dist" / (name + ".zip")
with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
    for source in sorted(bundle.rglob("*")):
        if source.is_file():
            z.write(source, source.relative_to(bundle))
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
archive.with_suffix(".sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
print(f"Packaged {archive.name}: {digest}")
