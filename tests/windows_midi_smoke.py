"""Real WinMM -> private Windows MIDI connection -> bridge test. Never opens physical MIDI ports.

Run manually on Windows 11 25H2+ after building native-midi-helper.
"""
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mido
from partykeys_bridge.bridge import Bridge
from partykeys_bridge.cli import parser
from partykeys_bridge.connection import BridgeConnection
import partykeys_bridge.windows_midi as windows_midi

# Optionally exercise the helper from an extracted release bundle.
if len(sys.argv) > 1:
    bundled_helper = Path(sys.argv[1]).resolve() / "native-midi-helper" / "PartyKeys.Midi.exe"
    windows_midi.helper_path = lambda: bundled_helper


class Capture:
    now = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)

    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)


def until(predicate, tick, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        tick()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for MIDI state")


if sys.platform != "win32":
    raise SystemExit("Windows-only integration test")
mido.set_backend("mido.backends.rtmidi")
name = "PK API Test " + uuid.uuid4().hex[:8]
args = parser().parse_args(["bridge", "--virtual-input", "--input", name,
                            "--dry-run", "--palette-interval-ms", "10"])
capture = Capture()
session = BridgeConnection(args, Bridge(args.left_color, args.right_color), capture)
try:
    session.tick()
    route = session.windows_route
    assert route, route
    try:
        windows_midi.existing_route(name)
    except ValueError as error:
        assert "already owns" in str(error), error
    else:
        raise AssertionError("Another bridge could take over the running native route")
    outputs = [p for p in mido.get_output_names() if name in p]
    inputs = [p for p in mido.get_input_names() if name in p]
    assert len(outputs) == 1 and len(inputs) <= 1, (outputs, inputs)
    assert not any("Bridge" in p for p in outputs + inputs), (outputs, inputs)
    with mido.open_output(route.synthesia_output) as sender:
        sender.send(mido.Message("note_on", channel=1, note=60, velocity=100))
        until(lambda: session.driver.state.get(60 - session.driver.base_note) == session.bridge.left,
              session.tick)
        sender.send(mido.Message("note_off", channel=1, note=60))
        until(lambda: session.driver.state.get(60 - session.driver.base_note) == (0, 0, 0), session.tick)
        sender.send(mido.Message("note_on", channel=6, note=72, velocity=100))
        until(lambda: session.driver.state.get(72 - session.driver.base_note) == session.bridge.right,
              session.tick)
        assert any(message.data[-4:] == (0x71, 1, 72, 7) for message in capture.sent), "Right-hand cue did not use green palette slot"
        assert any(message.data[-4:] == (0x71, 1, 60, 9) for message in capture.sent), "Left-hand cue did not use blue palette slot"
        sender.send(mido.Message("note_off", channel=6, note=72))
        until(lambda: session.driver.state.get(72 - session.driver.base_note) == (0, 0, 0), session.tick)
        sender.send(mido.Message("note_on", channel=1, note=64, velocity=90))
        until(lambda: 64 in session.bridge.lights(), session.tick)
        sender.send(mido.Message("control_change", channel=0, control=16, value=0))
        until(lambda: not session.bridge.lights(), session.tick)
        sender.send(mido.Message("note_on", channel=1, note=67, velocity=90))
        until(lambda: 67 in session.bridge.lights(), session.tick)
        sender.send(mido.Message("stop"))
        until(lambda: not session.bridge.lights(), session.tick)
        assert capture.sent, "No LED messages generated"
        print("PASS: exactly one visible output, hidden receiver, private note-on/off + CC + Stop delivery, bridge LED messages (dry-run)")
finally:
    session.close()
until(lambda: not any(name in port for port in mido.get_input_names() + mido.get_output_names()), lambda: None)
print("PASS: temporary ports removed; no physical MIDI outputs opened")
