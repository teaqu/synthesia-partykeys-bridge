"""Check both Windows apps from the actual extracted release ZIP, without MIDI."""
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

archive = Path(sys.argv[1]).resolve()
scripts = Path(__file__).resolve().parent
with tempfile.TemporaryDirectory(prefix="partykeys-windows-archive-") as folder:
    bundle = Path(folder)
    with zipfile.ZipFile(archive) as zipped:
        zipped.extractall(bundle)
    for relative in ("_internal/python312.dll", "_console/python312.dll",
                     "native-midi-helper/PartyKeys.Midi.exe", "config.json",
                     "Start Synthesia bridge.cmd", "THIRD_PARTY_LICENSES/Python-LICENSE.txt"):
        if not (bundle / relative).is_file():
            raise SystemExit(f"Windows ZIP is missing {relative}")
    for script, exe in (("smoke_executable.py", "partykeys.exe"),
                        ("smoke_gui.py", "PartyKeys Lights.exe")):
        subprocess.run([sys.executable, str(scripts / script), str(bundle / exe)], check=True)
print("PASS: Windows release ZIP includes working CLI, GUI, runtimes, helper and licenses")
