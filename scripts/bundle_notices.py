"""Preserve licenses shipped by dependencies in the executable distribution."""
from importlib.metadata import distribution
from pathlib import Path
import shutil
import sys

root = Path(sys.argv[1]) / 'THIRD_PARTY_LICENSES'
root.mkdir(parents=True, exist_ok=True)
for name in ('mido', 'python-rtmidi', 'packaging', 'pyinstaller'):
    dist = distribution(name)
    for entry in dist.files or ():
        if any(word in entry.name.lower() for word in ('license', 'copying', 'copyright')):
            source = Path(dist.locate_file(entry))
            if source.is_file():
                target = root / name / str(entry).replace('..', '_')
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
python_license = Path(sys.base_prefix) / 'LICENSE.txt'
if python_license.is_file():
    shutil.copy2(python_license, root / 'Python-LICENSE.txt')
