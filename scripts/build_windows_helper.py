"""Build a self-contained helper using .NET 10; no machine-wide MIDI installer.

Usage: python scripts/build_windows_helper.py [path-to-dotnet.exe]
"""
import hashlib
from pathlib import Path
import subprocess
import sys
import urllib.request
from xml.sax.saxutils import escape

root = Path(__file__).resolve().parents[1]
feed = root / "build" / "midi-api-feed"
feed.mkdir(parents=True, exist_ok=True)
name = "Windows.Devices.Midi2.0.99.92-preview.11.nupkg"
package = feed / name
expected = "13afc6e6da2adac309ab8c174352f46f1c0ebe2d79e65475bd6274defb73df74"
if not package.is_file():
    urllib.request.urlretrieve(
        "https://github.com/microsoft/MIDI/releases/download/inbox-preview-11/" + name, package)
if hashlib.sha256(package.read_bytes()).hexdigest() != expected:
    raise SystemExit("Microsoft MIDI package checksum does not match Preview 11")
config = feed / "NuGet.Config"
config.write_text('<configuration><packageSources><clear/>'
                  f'<add key="local-midi" value="{escape(str(feed))}"/>'
                  '<add key="nuget" value="https://api.nuget.org/v3/index.json"/>'
                  '</packageSources></configuration>', encoding="utf-8")
subprocess.run([
    sys.argv[1] if len(sys.argv) > 1 else "dotnet", "publish",
    str(root / "native" / "windows-midi-helper" / "PartyKeys.Midi.csproj"),
    "-c", "Release", "-r", "win-x64", "--self-contained", "true", "-p:Platform=x64",
    "-p:CsWinRTWindowsMetadata=C:\\Windows\\System32\\WinMetadata",
    "-o", str(root / "native-midi-helper"), "--configfile", str(config),
], check=True)
