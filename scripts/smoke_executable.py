"""Exercise the frozen CLI without a keyboard, including redirected stdin."""
from pathlib import Path
import subprocess
import sys

exe = str(Path(sys.argv[1]).resolve())
root = Path(__file__).resolve().parents[1]

def run(*args, input=None, expected=None):
    result = subprocess.run([exe, *args], input=input, capture_output=True,
                            text=True, timeout=45, cwd=root)
    print(result.stdout)
    if result.returncode:
        raise AssertionError(f'{args}: exit {result.returncode}\n{result.stderr}')
    if expected and expected not in result.stdout:
        raise AssertionError(f'{args}: missing {expected!r}\n{result.stdout}')

run('--help', expected='bridge')
run('ports', expected='MIDI outputs:')  # Loads the bundled backend and native RtMidi binary.
run('inspect', 'examples/demo.mid', expected='notes;')
run('test', '--dry-run', '--protocol', 'palette71', '--palette-interval-ms', '10',
    '--seconds', '0.01', expected='71 01 3C 09')
run('preview', 'examples/demo.mid', '--dry-run', '--protocol', 'palette71',
    '--palette-interval-ms', '10', '--paused', input='play\npause\nseek 0.1\nspeed 2\nquit\n',
    expected='Independent playback:')
print('PASS: frozen CLI, native MIDI backend, palette output, and piped preview controls')
