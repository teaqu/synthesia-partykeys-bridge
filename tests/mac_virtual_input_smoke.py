"""Test automatic macOS MIDI setup using only a temporary virtual destination.

Optionally pass a packaged executable path. No physical keyboard is opened.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

import mido

if sys.platform != "darwin":
    raise SystemExit("This integration check requires macOS")

name = "PartyKeys setup test " + uuid.uuid4().hex[:12]
command = [str(Path(sys.argv[1]).resolve())] if len(sys.argv) > 1 else [sys.executable, "-m", "partykeys_bridge"]
command += ["bridge", "--virtual-input", "--input", name, "--dry-run",
            "--no-press-feedback", "--palette-interval-ms", "10"]

def wait_for(predicate, label, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError("Timed out: " + label)

with tempfile.TemporaryDirectory() as directory:
    log = Path(directory) / "bridge.log"
    with log.open("w") as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                   env={**os.environ, "PYTHONUNBUFFERED": "1"})
        try:
            wait_for(lambda: name in mido.get_output_names(), "virtual destination creation")
            # Let the initial mode-entry delay finish; verify refresh keeps the port.
            time.sleep(1.2)
            assert name in mido.get_output_names()
            with mido.open_output(name) as sender:
                sender.send(mido.Message("note_on", note=60, channel=11, velocity=1))
                wait_for(lambda: "71 01 3C 09" in log.read_text(), "blue cue")
                sender.send(mido.Message("note_off", note=60, channel=0))
                wait_for(lambda: "71 01 3C 00" in log.read_text(), "cue release")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise AssertionError("Bridge did not exit gracefully")
        assert process.returncode == 0, log.read_text()
    wait_for(lambda: name not in mido.get_output_names(), "virtual destination removal")
print("PASS: automatic MIDI destination, cue routing, release, and cleanup; no hardware ports opened")
