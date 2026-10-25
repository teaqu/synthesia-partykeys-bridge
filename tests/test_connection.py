import unittest
from unittest.mock import patch
from copy import deepcopy
import queue

import mido
import rtmidi

from partykeys_bridge.bridge import Bridge
from partykeys_bridge.cli import parser
from partykeys_bridge.connection import BridgeConnection
from types import SimpleNamespace


class Device:
    def __init__(self, name, virtual=False):
        self.name, self.pending, self.sent = name, [], []
        self.virtual = virtual
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
    def test_live_bridge_brightness_changes_an_existing_cue_without_config_io(self):
        from partykeys_bridge.practice import LiveLighting
        live = self.session.args.live_lighting = LiveLighting()
        self.session.bridge.receive(mido.Message("note_on", channel=1, note=60))
        live.publish(self.session.bridge.practice, self.session.bridge.trail, bridge_brightness=.25)
        self.step()
        self.assertEqual(self.session.bridge.brightness, .25)
        self.assertEqual(self.session.bridge.lights()[60], (64, 64, 0))
        live.publish(self.session.bridge.practice, self.session.bridge.trail,
                     bridge_brightness=.25, bridge_enabled=False)
        self.session.tick()
        self.assertFalse(self.session.bridge.lights())
        live.publish(self.session.bridge.practice, self.session.bridge.trail, bridge_brightness=.25)
        self.session.tick()
        self.assertEqual(self.session.bridge.lights()[60], (64, 64, 0))

    def test_gui_receives_physical_keys_without_paint_capture_and_clears_on_disconnect(self):
        events = self.session.args.physical_key_events = queue.SimpleQueue()
        self.step()
        practice = deepcopy(self.session.bridge.practice)
        pressed, cue = self.session.ports["pressed"], self.session.ports["cue"]
        on = mido.Message("note_on", channel=2, note=60, velocity=80)
        off = mido.Message("note_on", channel=2, note=60, velocity=0)
        pressed.pending.extend([on, off])
        cue.pending.append(mido.Message("note_on", note=72))
        self.session.tick()
        self.assertEqual(events.get_nowait(), on)
        self.assertEqual(events.get_nowait(), off)
        self.assertTrue(events.empty())  # Synthesia cue notes are not physical presses.
        self.assertEqual(self.session.bridge.practice, practice)
        self.session.drop("output")
        self.assertTrue(events.empty())  # LED reconnects do not release physical keys.
        self.session.drop("pressed")
        self.assertIsNone(events.get_nowait())

    def test_keyboard_paint_is_applied_in_the_receiving_tick_without_ui_or_disk(self):
        import queue
        import threading
        from partykeys_bridge.practice import LiveLighting
        live = LiveLighting()
        trail = dict(enabled=False, duration_seconds=1.5)
        live.publish(dict(enabled=True, notes=[], color="FF0000", brightness=.2, note_colors={}), trail)
        self.session.args.live_lighting = live
        self.session.args.practice_capture = threading.Event()
        self.session.args.practice_capture.set()
        self.session.args.practice_key_events = queue.SimpleQueue()
        self.step()
        self.session.ports["pressed"].pending.append(mido.Message("note_on", note=60, velocity=80))
        self.session.tick()
        self.assertEqual(self.session.bridge.lights()[60], (51, 0, 0))
        self.assertEqual(self.session.args.practice_key_events.get_nowait(), 60)
        live.publish(self.session.bridge.practice, trail, erase=True)
        self.session.ports["pressed"].pending.append(mido.Message("note_on", note=60, velocity=80))
        self.session.tick()
        self.assertNotIn(60, self.session.bridge.lights())

    def setUp(self):
        discovery = patch("partykeys_bridge.routing.discover_boards", return_value=([(1, 0x3f, 48)], {1: 0}))
        discovery.start()
        self.addCleanup(discovery.stop)
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

    def test_joined_board_remove_and_add_keeps_cue_stream_running(self):
        from partykeys_bridge.routing import BoardWatcher
        self.session.args.protocol='palette71'
        self.session.args.board=[(1,0x3f,48),(1,0x40,84)]
        self.session.args.palette_interval_ms=10
        self.step()
        self.session.topology=BoardWatcher(self.session.args.board,now=lambda:self.now[0])
        def report(address,command,value=0):
            return mido.Message('sysex',data=(5,0x30,0x7e,0x7e,1,address,command,value,9))
        source=self.session.ports['pressed']
        self.session.ports['cue'].pending.append(mido.Message('note_on',channel=1,note=60))
        source.pending.extend([report(0x40,5),mido.Message('note_on',note=60)])
        self.session.tick()
        self.assertTrue(self.session.topology.suspended)
        self.assertIn(60,self.session.bridge.lights())
        self.now[0]+=.71;self.session.tick()
        source.pending.append(report(0x3f,2,1))
        self.now[0]+=3.01;self.session.tick()
        self.assertEqual(self.session.args.board,[(1,0x3f,48)])
        self.assertNotIn(96,self.session.driver.routes)
        self.assertIn(60,self.session.bridge.lights())
        source.pending.append(report(0x40,4));self.session.tick()
        self.now[0]+=.71;self.session.tick()
        source.pending.extend([report(0x3f,2,1),report(0x40,2)])
        self.now[0]+=3.01;self.session.tick()
        self.assertIn(96,self.session.driver.routes)
        self.step()
        self.assertEqual(self.session.driver.state[60-self.session.driver.base_note],self.session.bridge.left)

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
        self.assertEqual(self.session.driver.state[12], self.session.bridge.right)
        self.inputs.remove("PartyKeys")
        self.outputs.clear()
        self.step()
        self.assertIsNone(self.session.driver)
        cue.pending.extend([mido.Message("note_off", note=60), mido.Message("note_on", note=64)])
        self.step()
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

    def test_hardware_octave_report_updates_routes_and_survives_output_reconnect(self):
        self.session.args.protocol = "palette71"
        self.session.args.palette_interval_ms = 10
        self.session.args.board = [(1, 0x3f, 48), (1, 0x40, 84)]
        self.step()
        cue, pressed = self.session.ports["cue"], self.session.ports["pressed"]
        cue.pending.append(mido.Message("note_on", note=80))
        pressed.pending.append(mido.Message("note_on", note=80))
        self.step()
        message = mido.Message("sysex", data=[5,48,32,0,1,63,24,127,0])
        cue.pending.append(message)  # A song's SysEx must not change hardware routing.
        self.step()
        self.assertEqual(self.session.driver.octaves, {1: 0})
        pressed.pending.append(message)
        self.step()
        self.assertEqual(self.session.driver.octaves, {1: -1})
        self.assertIn(80, self.session.bridge.lights())
        self.assertEqual(self.session.driver.routes[80], (1, 0x40))
        self.session.drop("output")
        self.step()
        self.assertEqual(self.session.driver.octaves, {1: -1})
        self.assertEqual(tuple(self.session.driver.note_range), tuple(range(36,108)))

    def virtual_session(self, name="Synthesia Lights"):
        args = parser().parse_args(["bridge", "--virtual-input", "--input", name,
                                    "--output", "PartyKeys", "--palette-interval-ms", "10"])
        with patch("partykeys_bridge.connection.sys.platform", "darwin"):
            session = BridgeConnection(args, Bridge(), now=lambda: self.now[0])
        self.addCleanup(session.close)
        return session

    def test_virtual_destination_receives_cues_and_survives_refresh(self):
        session = self.virtual_session()
        session.tick()
        cue = session.ports["cue"]
        self.assertTrue(cue.virtual)
        self.assertNotIn(cue.name, self.inputs)
        self.outputs.append(cue.name)
        self.now[0] += 2
        session.tick()
        self.assertIs(session.ports["cue"], cue)
        cue.pending.append(mido.Message("note_on", channel=1, note=60))
        self.now[0] += .1
        session.tick()
        self.assertIn(60, session.bridge.lights())
        for _ in range(15):
            self.now[0] += .02
            session.tick()
        self.assertEqual(session.driver.state[12], session.bridge.left)
        self.outputs.remove("PartyKeys")
        self.now[0] += 2
        session.tick()
        self.assertIs(session.ports["cue"], cue)
        self.assertIsNone(session.ports["output"])
        session.close()
        self.assertTrue(cue.closed)

    def test_existing_destination_rejected_before_hardware_open(self):
        self.outputs.append("Synthesia Lights")
        session = self.virtual_session()
        with self.assertRaisesRegex(ValueError, "already exists"):
            session.refresh()
        self.assertTrue(all(port is None for port in session.ports.values()))

    def test_virtual_destination_never_auto_selected_as_led_output(self):
        session = self.virtual_session("PartyKeys Cues")
        self.inputs = ["IAC Lights"]
        self.outputs = []
        session.refresh()
        self.outputs = ["PartyKeys Cues"]
        session.refresh()
        self.assertIsNotNone(session.ports["cue"])
        self.assertIsNone(session.ports["output"])

    def test_virtual_option_rejected_on_unsupported_platform_before_enumeration(self):
        args = parser().parse_args(["bridge", "--virtual-input"])
        with patch("partykeys_bridge.connection.sys.platform", "linux"), \
                patch("mido.get_input_names") as inputs:
            with self.assertRaisesRegex(ValueError, "macOS"):
                BridgeConnection(args, Bridge())
            inputs.assert_not_called()

    def test_windows_native_device_reads_private_connection_without_rtmidi(self):
        args = parser().parse_args(["bridge", "--virtual-input",
                                    "--output", "PartyKeys", "--palette-interval-ms", "10"])
        from partykeys_bridge.windows_midi import CueRoute
        self.outputs += ["Synthesia Lights 4"]
        with patch("partykeys_bridge.connection.sys.platform", "win32"), \
                patch("partykeys_bridge.connection.existing_route", return_value=None), \
                patch("partykeys_bridge.connection.WindowsVirtualInput") as native:
            native.return_value.start.return_value = CueRoute("Synthesia Lights", "Synthesia Lights 4")
            native.return_value.iter_pending.return_value = []
            session = BridgeConnection(args, Bridge(), now=lambda: self.now[0])
            try:
                session.tick()
                self.assertIs(session.ports["cue"], native.return_value)
                native.return_value.iter_pending.return_value = [mido.Message("note_on", channel=1, note=60)]
                session.tick()
                self.assertIn(60, session.bridge.lights())
                self.assertEqual(session.ports["output"].name, "PartyKeys")
                session.refresh()
                native.return_value.start.assert_called_once()
            finally:
                session.close()
            self.assertTrue(native.return_value.close.called)

    def test_windows_reuses_existing_loopmidi_without_starting_helper(self):
        from partykeys_bridge.windows_midi import CueRoute
        args = parser().parse_args(["bridge", "--virtual-input"])
        self.inputs += ["Synthesia Lights 2"]
        with patch("partykeys_bridge.connection.sys.platform", "win32"), \
                patch("partykeys_bridge.connection.existing_route", return_value=CueRoute("Synthesia Lights 2", "Synthesia Lights 3")), \
                patch("partykeys_bridge.connection.WindowsVirtualInput") as native:
            session = BridgeConnection(args, Bridge(), now=lambda: self.now[0])
            session.tick()
            session.close()
            native.assert_not_called()
