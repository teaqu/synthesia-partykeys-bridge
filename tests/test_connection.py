import unittest
from unittest.mock import patch

import mido
import rtmidi

from partykeys_bridge.bridge import Bridge
from partykeys_bridge.cli import parser
from partykeys_bridge.connection import BridgeConnection
from types import SimpleNamespace


class Device:
    def __init__(self, name):
        self.name, self.pending, self.sent = name, [], []
        self.closed = self.fail_send = False

    def iter_pending(self):
        pending, self.pending = self.pending, []
        return iter(pending)

    def send(self, message):
        if self.fail_send:
            raise RuntimeError("device disappeared during send")
        self.sent.append(message)

    def close(self):
        self.closed = True


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.inputs = ["IAC Lights", "PartyKeys"]
        self.outputs = ["PartyKeys"]
        self.now = [0]
        def sleep(seconds):
            self.now[0] += seconds
        clock = patch("partykeys_bridge.driver.time", SimpleNamespace(monotonic=lambda: self.now[0], sleep=sleep))
        clock.start()
        self.addCleanup(clock.stop)
        for name, replacement in (
            ("mido.get_input_names", lambda: self.inputs[:]),
            ("mido.get_output_names", lambda: self.outputs[:]),
            ("mido.open_output", Device),
            ("partykeys_bridge.connection.MidiInput", Device),
        ):
            handle = patch(name, replacement)
            handle.start()
            self.addCleanup(handle.stop)
        args = parser().parse_args(["bridge", "--input", "IAC Lights", "--output", "PartyKeys", "--protocol", "rgb15"])
        self.session = BridgeConnection(args, Bridge(), now=lambda: self.now[0])
        self.addCleanup(self.session.close)

    def step(self):
        # Let the nonblocking startup and current deltas finish under fake time.
        for _ in range(42):
            self.now[0] += 0.25
            self.session.tick()

    def test_transient_enumeration_race_keeps_live_handles(self):
        self.step()
        original = self.session.ports.copy()
        for error in (RuntimeError("scan changed"),
                      rtmidi.InvalidPortError("MidiInCore::getPortName: portNumber invalid")):
            with patch("mido.get_input_names", side_effect=error):
                self.step()
        self.assertEqual(self.session.ports, original)
        self.assertFalse(any(port.closed for port in original.values()))
        self.step()
        self.assertEqual(self.session.ports, original)

    def test_backend_invalid_port_during_open_is_retried(self):
        with patch("mido.open_output", side_effect=rtmidi.InvalidPortError("portNumber invalid")):
            self.step()
        self.assertIsNone(self.session.driver)
        self.step()
        self.assertIsNotNone(self.session.driver)

    def test_reconnect_keeps_fresh_cues_and_discards_old_presses(self):
        self.step()
        cue = self.session.ports["cue"]
        cue.pending.append(mido.Message("note_on", note=60))
        self.session.ports["pressed"].pending.append(mido.Message("note_on", note=60))
        self.step()
        self.assertEqual(self.session.driver.state[12], (255, 0, 0))
        self.inputs.remove("PartyKeys")
        self.outputs.clear()
        self.step()
        self.assertIsNone(self.session.driver)
        cue.pending.extend([mido.Message("note_off", note=60), mido.Message("note_on", note=64)])
        self.step()
        self.assertEqual(self.session.bridge.pressed, set())
        self.inputs.append("PartyKeys")
        self.outputs.append("PartyKeys")
        self.step()
        self.assertIs(self.session.ports["cue"], cue)
        self.assertEqual(self.session.driver.state[12], (0, 0, 0))
        self.assertEqual(self.session.driver.state[16], self.session.bridge.right)
        sent = self.session.ports["output"].sent
        self.assertEqual(sent[0].hex(), "F0 05 30 7F 7F 20 00 0F 01 F7")
        self.assertEqual(sent[1].data[8:14], (0, 0, 0, 0, 0, 0))

    def test_send_failure_reopens_only_output(self):
        self.step()
        cue, output = self.session.ports["cue"], self.session.ports["output"]
        output.fail_send = True
        cue.pending.append(mido.Message("note_on", note=60))
        self.now[0] += 0.25
        self.session.tick()
        self.assertIsNone(self.session.driver)
        self.assertTrue(output.closed)
        self.step()
        self.assertIs(self.session.ports["cue"], cue)
        self.assertIsNot(self.session.ports["output"], output)
        self.assertEqual(self.session.driver.state[12], self.session.bridge.right)

    def test_missing_device_at_start_waits_then_connects(self):
        self.inputs.remove("PartyKeys")
        self.outputs.clear()
        self.step()
        self.assertIsNotNone(self.session.ports["cue"])
        self.assertIsNone(self.session.driver)
        self.inputs.append("PartyKeys")
        self.outputs.append("PartyKeys")
        self.step()
        self.assertIsNotNone(self.session.driver)

    def test_missing_cue_source_clears_stale_lights(self):
        self.step()
        self.session.ports["cue"].pending.append(mido.Message("note_on", note=60))
        self.step()
        self.inputs.remove("IAC Lights")
        self.step()
        self.assertEqual(self.session.bridge.lights(), {})
        self.assertEqual(self.session.driver.state[12], (0, 0, 0))

    def test_reconnect_never_selects_different_keyboard(self):
        self.step()
        self.inputs[:] = ["IAC Lights", "PartyKeys Other"]
        self.outputs[:] = ["PartyKeys Other"]
        self.step()
        self.assertIsNone(self.session.driver)
        self.assertIsNone(self.session.ports["pressed"])
