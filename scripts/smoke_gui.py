"""Start the packaged Tk GUI on native Windows/macOS CI and require clean exit."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile

exe = str(Path(sys.argv[1]).resolve())
with tempfile.TemporaryDirectory(prefix="partykeys-gui-result-") as folder:
    report = Path(folder) / "result.json"
    result = subprocess.run([exe, "gui", "--smoke-test", "--smoke-result", str(report)], capture_output=True,
                            text=True, timeout=45)
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    outcome = json.loads(report.read_text()) if report.is_file() else {}
    if result.returncode != 0 or outcome.get("passed") is not True:
        raise SystemExit(f"Packaged GUI launch failed: exit {result.returncode}; {outcome}")
    print("PASS: GUI controls verified, including windowed builds without stdout")
