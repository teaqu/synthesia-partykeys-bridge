"""Package a native executable, launcher, docs, and licenses for a release."""
from pathlib import Path
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("platform", choices=("windows-x64", "macos-arm64", "macos-x64"))
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
project = (root / "pyproject.toml").read_text(encoding="utf-8").split("[project]", 1)[1].split("\n[", 1)[0]
version = re.search(r'^version\s*=\s*"([^"]+)"', project, re.MULTILINE).group(1)
if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+-]*", version):
    raise SystemExit("Project version cannot be used in an archive filename")
name = f"partykeys-lights-v{version}-{args.platform}"
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
practice_launcher = "Practice lights.cmd" if windows else "Practice lights.command"
shutil.copy2(root / "packaging" / practice_launcher, bundle / practice_launcher)
if not windows:
    (bundle / exe).chmod(0o755)
    (bundle / launcher).chmod(0o755)
    (bundle / practice_launcher).chmod(0o755)
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
if os.environ.get("GITHUB_OUTPUT"):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"name={name}\narchive={archive.relative_to(root).as_posix()}\n"
                     f"checksum={archive.with_suffix('.sha256').relative_to(root).as_posix()}\n")
