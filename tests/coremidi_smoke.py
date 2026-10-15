"""Temporary CoreMIDI routing check; no physical device ports are opened."""
import time
import faulthandler
faulthandler.dump_traceback_later(30, exit=True)
import mido
from partykeys_bridge.bridge import Bridge
from partykeys_bridge.driver import PartyKeys, MidiInput, rgb_messages

mido.set_backend('mido.backends.rtmidi')

def receive_until(port, predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for message in port.iter_pending():
            print('Received:', port.name, message, flush=True)
            if predicate(message):
                return message
        time.sleep(0.005)
    print('TIMEOUT receiving from', port.name, flush=True)
    raise AssertionError('Timed out waiting for virtual MIDI message')

with MidiInput('Codex PartyKeys Test LED Sink', virtual=True) as sink:
    with mido.open_output('Codex PartyKeys Test Synthesia Source', virtual=True) as source:
        with MidiInput('Codex PartyKeys Test Synthesia Source') as incoming:
            with mido.open_output('Codex PartyKeys Test LED Sink') as outgoing:
                driver = PartyKeys(outgoing)
                driver.initialize()
                receive_until(sink, lambda m: m.type == 'sysex' and m.data[6] == 0x0F)
                receive_until(sink, lambda m: m.type == 'sysex' and m.data[6] == 0x15)
                bridge = Bridge()
                source.send(mido.Message('note_on', note=60, channel=1, velocity=80))
                bridge.receive(receive_until(incoming, lambda m: m.type == 'note_on'))
                driver.show(bridge.lights(), force=True)
                expected = list(rgb_messages({12: bridge.left}))[0]
                actual = receive_until(sink, lambda m: m == expected)
                assert actual == expected, (actual, expected)
                source.send(mido.Message('note_off', note=60, channel=1))
                bridge.receive(receive_until(incoming, lambda m: m.type == 'note_off'))
                driver.show(bridge.lights(), force=True)
                off = receive_until(sink, lambda m: m.type == 'sysex')
                assert off == list(rgb_messages({12: (0, 0, 0)}))[0]
                driver.clear()
                print('PASS message routing; now closing ports', flush=True)
print('PASS: real CoreMIDI virtual-port note-on -> RGB and note-off -> black; temporary ports closed.')

faulthandler.cancel_dump_traceback_later()
