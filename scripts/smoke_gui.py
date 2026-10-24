"""Start the packaged Tk GUI on native Windows/macOS CI and require clean exit."""
from pathlib import Path
import subprocess
import sys

exe = str(Path(sys.argv[1]).resolve())
result = subprocess.run([exe, "gui", "--smoke-test"], capture_output=True,
                        text=True, timeout=45)
print(result.stdout)
if result.stderr:
    print(result.stderr, file=sys.stderr)
if result.returncode != 0 or "PASS: GUI launched" not in result.stdout:
    raise SystemExit(f"Packaged GUI launch failed: exit {result.returncode}")
