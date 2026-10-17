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
if windows:
    helper = root / "native-midi-helper"
    if not (helper / "PartyKeys.Midi.exe").is_file():
        raise SystemExit("Build native/windows-midi-helper before packaging Windows")
    shutil.copytree(helper, bundle / "native-midi-helper", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("*.pdb"))
    shutil.copy2(root / "native" / "windows-midi-helper" / "PREVIEW.txt", bundle / "WINDOWS-MIDI-PREVIEW.txt")
shutil.copy2(root / "config.json", bundle / "config.json")
if not windows:
    (bundle / exe).chmod(0o755)
    (bundle / launcher).chmod(0o755)
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
