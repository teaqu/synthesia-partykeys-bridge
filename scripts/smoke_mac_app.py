"""Verify the Finder app, optionally after unpacking its actual download ZIP."""
from pathlib import Path
import json
import plistlib
import subprocess
import sys
import tempfile

source = Path(sys.argv[1]).resolve()
with tempfile.TemporaryDirectory(prefix="partykeys-app-smoke-") as folder:
    if source.suffix == ".zip":
        subprocess.run(["ditto", "-x", "-k", str(source), folder], check=True)
        assert not (Path(folder) / "Practice lights.command").exists(), "Old GUI shell launcher remains"
        app = Path(folder) / "PartyKeys Lights.app"
    else:
        app = source
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == "org.teaqu.partykeys-lights"
    exe = app / "Contents" / "MacOS" / info["CFBundleExecutable"]
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    # No GUI argument: verify the app's Finder entry defaults to GUI. The hidden
    # smoke flag isolates settings and never starts the real MIDI worker.
    report = Path(folder) / "result.json"
    result = subprocess.run([str(exe), "--smoke-test", "--smoke-result", str(report)], capture_output=True, text=True, timeout=45)
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    outcome = json.loads(report.read_text()) if report.is_file() else {}
    assert result.returncode == 0 and outcome.get("passed") is True, f"App GUI smoke failed: {outcome}"
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
print("PASS: Mac app entry, GUI controls, unchanged bundle signature and ZIP extraction")
