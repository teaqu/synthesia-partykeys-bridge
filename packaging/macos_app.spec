"""Build a single-process Finder app; keep settings outside its signed bundle."""
from pathlib import Path
import re
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).parent
version = re.search(r'^version\s*=\s*"([^"]+)"', (root / "pyproject.toml").read_text(), re.MULTILINE).group(1)
datas, binaries, hiddenimports = collect_all("rtmidi")
a = Analysis([str(root / "packaging" / "gui_entry.py")], pathex=[str(root)],
             binaries=binaries, datas=datas,
             hiddenimports=["mido.backends.rtmidi", *hiddenimports])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="PartyKeys Lights",
          console=False, icon="NONE")
coll = COLLECT(exe, a.binaries, a.datas, name="PartyKeys Lights")
app = BUNDLE(coll, name="PartyKeys Lights.app", icon=None,
             bundle_identifier="org.teaqu.partykeys-lights", version=version,
             info_plist={"CFBundleShortVersionString": version, "NSHighResolutionCapable": True})
