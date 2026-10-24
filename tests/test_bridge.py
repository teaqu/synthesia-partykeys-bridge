import tempfile
import queue
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import mido

from partykeys_bridge.bridge import Bridge
from partykeys_bridge.cli import Commands, check_connections, lights_session, parser
from partykeys_bridge.driver import MidiInput, PartyKeys, PalettePartyKeys, octave_report, rgb_messages, select_port, make_driver
from partykeys_bridge.preview import AudioPlayback, Preview, Transport
from partykeys_bridge.song import Note, Song


class Port:
    def __init__(self):
        self.sent = []
        self.panics = self.resets = 0

    def send(self, message):
        self.sent.append(message)

    def panic(self):
        self.panics += 1

    def reset(self):
        self.resets += 1


def note(pitch=60, start=2, end=3, channel=0, track=0):
    return Note(start, end, pitch, channel, track, 100)


def song(*notes):
    return Song(list(notes), [], max((n.end for n in notes), default=0), ["Piano"])


class DriverClock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class ClockedTests(unittest.TestCase):
    def setUp(self):
        self.clock = DriverClock()
        handle = patch("partykeys_bridge.driver.time", self.clock)
        handle.start()
        self.addCleanup(handle.stop)


class JoinedBoardTests(ClockedTests):
    def driver(self, port=None):
        return PalettePartyKeys(port or Port(), min_interval=.01,
                                boards=[(1, 0x3f, 48), (1, 0x40, 84)])

    def test_full_width_targets_and_cleanup(self):
        driver = self.driver()
        driver.initialize()
        driver.port.sent.clear()
        driver.show({n: (0, 80, 255) for n in range(128)}, force=True)
        self.assertEqual(len(driver.port.sent), 24)
        lit = []
        for message in driver.port.sent:
            data = message.data
            self.assertEqual(data[:3], (5, 0x30, 1))
            self.assertEqual(data[6:8], (0x71, 3))
            notes = data[8::2]
            self.assertTrue(all((48 <= n < 84 if data[3] == 0x3f else 84 <= n < 120) for n in notes))
            self.assertEqual(data[9::2], (9, 9, 9))
            lit.extend(notes)
        self.assertEqual(lit, list(range(48, 120)))
        driver.port.sent.clear()
        driver.clear()
        self.assertEqual(len(driver.port.sent), 24)
        self.assertTrue(all(set(m.data[9::2]) == {0} for m in driver.port.sent))
        self.assertTrue(all(m.data[3] in (0x3f, 0x40) for m in driver.port.sent))

    def test_cross_board_release_before_on_and_pressed_invalidation(self):
        driver = self.driver()
        driver.initialize()
        driver.show({84: (0, 80, 255)}, force=True)
        driver.port.sent.clear()
        driver.show({48: (0, 80, 255)}, force=True)
        self.assertEqual([(m.data[3], m.data[8:]) for m in driver.port.sent],
                         [(0x40, (84, 0)), (0x3f, (48, 9))])
        driver.port.sent.clear()
        driver.invalidate_note(48)
        driver.show({48: (0, 80, 255)}, force=True)
        self.assertEqual(driver.port.sent[0].data[3], 0x3f)

    def test_failed_second_board_does_not_commit_its_state(self):
        driver = self.driver()
        driver.initialize()
        driver.port.send = Mock(side_effect=[None, OSError("disconnected")])
        with self.assertRaises(OSError):
            driver.show({83: (0, 80, 255), 84: (0, 80, 255)}, force=True)
        self.assertEqual(driver.state[83 - 48], (0, 80, 255))
        self.assertEqual(driver.state[84 - 48], (0, 0, 0))

    def test_configuration_validation_before_hardware(self):
        for flags in (["--board", "1:0x3f:48", "--board", "1:0x40:80"],
                      ["--board", "1:0x3f:48", "--board", "1:0x3f:84"],
                      ["--board", "0:0x3f:48"], ["--board", "1:0x7f:48"],
                      ["--board", "1:0x40:93"],
                      ["--board", "1:0x3f:48", "--protocol", "rgb15"]):
            args = parser().parse_args(["bridge", *flags])
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                make_driver(None, args)
        args = parser().parse_args(["bridge", "--board", "1:0x3f:48", "--board", "1:64:84"])
        self.assertEqual(tuple(make_driver(None, args).note_range), tuple(range(48, 120)))

    def test_octave_report_accepts_real_button_messages_only(self):
        for value, expected in ((127, -1), (126, -2), (0, 0), (3, 3)):
            message = mido.Message("sysex", data=[5, 48, 32, 0, 1, 63, 24, value, 0])
            self.assertEqual(octave_report(message), (1, expected))
        for data in ([5, 48, 32, 0, 1, 63, 24], [5, 48, 32, 0, 1, 63, 24, 4],
                     [5, 48, 127, 127, 1, 63, 24, 127], [5, 48, 32, 0, 0, 63, 24, 127],
                     [5, 48, 32, 0, 1, 63, 2, 127]):
            self.assertIsNone(octave_report(mido.Message("sysex", data=data)))
        self.assertIsNone(octave_report(mido.Message("note_on", note=48)))

    def test_octave_remaps_boundary_and_repaints_inactive_keys(self):
        driver = self.driver()
        driver.initialize()
        driver.show({80: (0, 80, 255)}, force=True)  # Initially on left board.
        self.assertTrue(driver.set_octave(1, -1))
        self.assertEqual(tuple(driver.note_range), tuple(range(36, 108)))
        driver.port.sent.clear()
        driver.show({80: (0, 80, 255)}, force=True)
        self.assertEqual(driver.port.sent[-1].data[3], 0x40)  # Same cue now on right.
        self.assertEqual(driver.port.sent[-1].data[-2:], (80, 9))
        cleared = {(m.data[3], n) for m in driver.port.sent for n, slot in zip(m.data[8::2], m.data[9::2]) if slot == 0}
        self.assertIn((0x3f, 68), cleared)  # Old physical key is explicitly dark.
        self.assertFalse(driver.set_octave(1, -1))  # Duplicate doesn't erase cache.
        self.assertEqual(driver.state[80 - 48], (0, 80, 255))
        driver.set_octave(1, 0)
        self.assertEqual(tuple(driver.note_range), tuple(range(48, 120)))

    def test_initial_octave_and_midi_bounds(self):
        args = parser().parse_args(["bridge", "--board", "1:0x3f:48", "--board", "1:0x40:84", "--initial-octave", "-1"])
        driver = make_driver(Port(), args)
        self.assertEqual(tuple(driver.note_range), tuple(range(36, 108)))
        driver.set_octave(1, 3)
        self.assertEqual(tuple(driver.note_range), tuple(range(84, 128)))
        driver.initialize()
        driver.show({127: (0, 80, 255)}, force=True)
        self.assertEqual(driver.port.sent[-1].data[8:], (127, 9))


class WindowsCommandTests(unittest.TestCase):
    def test_nonblocking_partial_lines_crlf_and_eof(self):
        with patch('partykeys_bridge.cli.sys.platform', 'win32'), \
                patch('partykeys_bridge.cli.sys.stdin') as stdin, \
                patch('partykeys_bridge.cli.threading.Thread'):
            stdin.fileno.return_value = 0
            commands = Commands()
        self.assertIsNone(commands.poll())
        commands.chunks.put(b'pa')
        self.assertIsNone(commands.poll())
        commands.chunks.put(b'use\r\nspeed 2\r\nqu')
        self.assertEqual(commands.poll(), ['pause'])
        self.assertEqual(commands.poll(), ['speed', '2'])
        commands.chunks.put(b'it')
        self.assertIsNone(commands.poll())
        commands.chunks.put(b'')
        self.assertEqual(commands.poll(), ['quit'])
        self.assertFalse(commands.open)

    def test_reader_delivers_data_before_eof(self):
        chunks = queue.SimpleQueue()
        with patch('partykeys_bridge.cli.os.read', side_effect=[b'play\n', b'']):
            Commands._read_windows(0, chunks)
        self.assertEqual(chunks.get_nowait(), b'play\n')
        self.assertEqual(chunks.get_nowait(), b'')

    def test_closed_console_becomes_eof(self):
        chunks = queue.SimpleQueue()
        with patch('partykeys_bridge.cli.os.read', side_effect=OSError('console closed')):
            Commands._read_windows(0, chunks)
        self.assertEqual(chunks.get_nowait(), b'')


class ProtocolTests(ClockedTests):
    def test_rate_limit_latest_state_and_force_cannot_bypass_spacing(self):
        port = Port()
        times = []
        original_send = port.send
        port.send = lambda message: (times.append(self.clock.monotonic()), original_send(message))
        driver = PartyKeys(port, min_interval=0.04)
        driver.initialize()
        driver.show({60: (255, 0, 0)}, force=True)
        count = len(port.sent)
        driver.show({61: (0, 255, 0)})
        self.assertEqual(len(port.sent), count)
        self.clock.sleep(0.25)
        driver.show({62: (0, 0, 255)})  # Off first; obsolete green is never sent.
        self.assertEqual(driver.state[12], (0, 0, 0))
        self.assertEqual(driver.state[14], (0, 0, 0))
        self.clock.sleep(0.25)
        driver.show({62: (0, 0, 255)})
        self.assertEqual(driver.state[14], (0, 0, 255))
        driver.show({}, force=True)
        self.assertEqual(driver.state[14], (0, 0, 0))
        self.assertGreaterEqual(times[1] - times[0], 1.0)
        self.assertTrue(all(b-a >= 0.04 - 1e-9 for a,b in zip(times, times[1:])))
        self.assertFalse(any(m.data[-1] == 13 for m in port.sent[37:]))

    def test_nonblocking_init_keeps_input_loop_available(self):
        driver = PartyKeys(Port())
        driver.initialize(blocking=False)
        self.assertEqual(self.clock.value, 0)
        self.assertEqual(len(driver.port.sent), 1)
        for _ in range(3):
            self.clock.sleep(0.25)
            driver.show({60: (1, 2, 3)})
        self.assertEqual(len(driver.port.sent), 1)
        for _ in range(36):
            self.clock.sleep(0.25)
            before = self.clock.value
            driver.show({60: (1, 2, 3)})
            self.assertEqual(self.clock.value, before)
        self.assertFalse(driver.initializing)
        self.assertEqual(len(driver.port.sent), 37)
        self.clock.sleep(0.25)
        driver.show({64: (4, 5, 6)})
        self.assertEqual(driver.state[12], (0, 0, 0))
        self.assertEqual(driver.state[16], (4, 5, 6))

    def test_clear_skips_dark_and_reasserts_invalidated_keys(self):
        driver = PartyKeys(Port())
        driver.initialize()
        self.assertEqual(len(driver.port.sent), 37)
        driver.clear()
        self.assertEqual(len(driver.port.sent), 37)
        driver.invalidate_note(60)
        driver.clear()
        self.assertEqual(len(driver.port.sent), 38)
        self.assertEqual(driver.port.sent[-1].data[-1], 12)

    def test_failed_write_does_not_record_unsent_state(self):
        driver = PartyKeys(Port())
        driver.initialize()
        with patch.object(driver.port, "send", side_effect=RuntimeError("gone")):
            with self.assertRaises(RuntimeError):
                driver.show({60: (1, 2, 3)}, force=True)
        self.assertEqual(driver.state[12], (0, 0, 0))

    def test_exact_single_key_rgb_bytes(self):
        result = list(rgb_messages({17: (255, 0, 0), 18: (0, 255, 0)}))
        self.assertEqual(result[0].hex(), "F0 05 30 7F 7F 20 00 15 01 01 7F 00 00 00 00 01 11 F7")
        self.assertEqual(result[1].hex(), "F0 05 30 7F 7F 20 00 15 01 00 00 01 7F 00 00 01 12 F7")

    def test_no_bulk_frames_even_for_identical_colors(self):
        for colors in ({i: (0, 0, 0) for i in range(36)},
                       {i: (i, i * 2, i * 3) for i in range(36)}):
            packets = list(rgb_messages(colors))
            self.assertEqual(len(packets), 36)
            for packet in packets:
                self.assertEqual(len(packet.bytes()), 18)
                self.assertEqual(packet.data[7], 1)
                self.assertEqual(packet.data[14], 1)
                self.assertTrue(all(0 <= b < 128 for b in packet.data))

    def test_key_range_brightness_duplicate_and_release(self):
        port = Port()
        driver = PartyKeys(port, brightness=0.5)
        driver.initialize()
        self.assertEqual(port.sent[0].hex(), "F0 05 30 7F 7F 20 00 0F 01 F7")
        port.sent.clear()
        driver.show({47: (255, 0, 0), 48: (255, 0, 0), 83: (0, 128, 0), 84: (1, 1, 1)}, force=True)
        self.assertEqual(driver.state[0], (128, 0, 0))
        self.assertEqual(driver.state[35], (0, 64, 0))
        count = len(port.sent)
        driver.show({48: (255, 0, 0), 83: (0, 128, 0)})
        self.assertEqual(len(port.sent), count)
        driver.show({}, force=True)
        self.assertTrue(all(rgb == (0, 0, 0) for rgb in driver.state.values()))

    def test_shutdown_clear_even_on_failure(self):
        port = Port()
        with self.assertRaises(RuntimeError):
            with lights_session(port, SimpleNamespace(brightness=1, base_note=48)) as driver:
                driver.show({60: (1, 2, 3)}, force=True)
                raise RuntimeError("disconnected")
        expected = list(rgb_messages({12: (0, 0, 0)}))[0]
        self.assertEqual(port.sent[-1], expected)

    def test_port_selection_is_unambiguous(self):
        self.assertEqual(select_port(["IAC Bus", "Other"], None, "IAC"), "IAC Bus")
        with self.assertRaises(ValueError):
            select_port(["PartyKeys A", "PartyKeys B"], None, "partykeys")
        with self.assertRaises(ValueError):
            select_port(["Other"], "missing", "partykeys")

    def test_reject_invalid_protocol_values(self):
        for state in ({36: (0, 0, 0)}, {0: (256, 0, 0)}, {0: (1, 2)}):
            with self.assertRaises(ValueError):
                list(rgb_messages(state))


class PaletteTests(ClockedTests):
    def test_extended_midi_range_and_cleanup(self):
        # A joined/transposed keyboard can play beyond a single board's 48..83.
        driver = PalettePartyKeys(Port(), min_interval=.01)
        driver.initialize()
        driver.port.sent.clear()
        notes = {n: (0, 80, 255) for n in (0, 36, 47, 84, 98, 127)}
        driver.show(notes, force=True)
        pairs = [pair for msg in driver.port.sent
                 for pair in zip(msg.data[8::2], msg.data[9::2])]
        self.assertEqual(pairs, [(n, 9) for n in notes])
        driver.port.sent.clear()
        driver.clear()
        pairs = [pair for msg in driver.port.sent
                 for pair in zip(msg.data[8::2], msg.data[9::2])]
        self.assertEqual(pairs, [(n, 0) for n in notes])
        self.assertTrue(all(1 <= msg.data[7] <= 3 for msg in driver.port.sent))

    def test_extended_physical_feedback_reasserts_and_releases(self):
        driver = PalettePartyKeys(Port(), base_note=36, min_interval=.01)
        driver.initialize()
        driver.show({98: (0, 80, 255)}, force=True)
        driver.invalidate_note(98)
        driver.show({98: (0, 80, 255)}, force=True)
        self.assertEqual(tuple(driver.port.sent[-1].data[8:]), (98, 9))
        driver.invalidate_note(98)
        driver.show({}, force=True)
        self.assertEqual(tuple(driver.port.sent[-1].data[8:]), (98, 0))

    def test_init_only_then_exact_blue_yellow_red_and_individual_off(self):
        port = Port()
        times = []
        send = port.send
        port.send = lambda msg: (times.append(self.clock.value), send(msg))
        driver = PalettePartyKeys(port, brightness=0.2, min_interval=0.25)
        driver.initialize()
        self.assertEqual(len(port.sent), 1)  # No all-board startup clear.
        self.assertEqual(port.sent[0].hex(), "F0 05 30 7F 7F 20 00 0F 01 F7")
        for rgb, slot in (((0, 80, 255), 9), ((255, 255, 0), 5), ((255, 0, 0), 1)):
            driver.show({60: rgb}, force=True)
            self.assertEqual(port.sent[-1].hex(), f"F0 05 30 7F 7F 20 00 71 01 3C {slot:02X} F7")
        driver.clear()
        self.assertEqual(port.sent[-1].hex(), "F0 05 30 7F 7F 20 00 71 01 3C 00 F7")
        self.assertTrue(all(m.data[7] == 1 for m in port.sent[1:]))
        self.assertTrue(all(len(m.bytes()) == 12 for m in port.sent[1:]))
        self.assertTrue(all(b-a >= 0.25 for a,b in zip(times, times[1:])))
        count = len(port.sent)
        driver.clear()
        self.assertEqual(len(port.sent), count)

    def test_palette_default_uses_1ms_spacing(self):
        port = Port()
        times = []
        send = port.send
        port.send = lambda msg: (times.append(self.clock.value), send(msg))
        driver = PalettePartyKeys(port)
        driver.initialize()
        for _ in range(3):
            driver.show({60: (0,80,255), 64: (0,80,255), 67: (0,80,255)}, force=True)
            driver.clear()
        gaps = [b-a for a,b in zip(times[1:], times[2:])]
        self.assertTrue(all(abs(gap - 0.001) < 1e-9 for gap in gaps))
        self.assertEqual(len(port.sent), 7)
        self.assertTrue(all(m.data[7] == 3 for m in port.sent[1:]))
        self.assertTrue(all(len(m.bytes()) == 16 for m in port.sent[1:]))

    def test_explicit_fast_palette_rate_applies_to_chords_and_cleanup(self):
        for interval_ms in (1, 10, 20, 40):
            with self.subTest(interval_ms=interval_ms):
                args = parser().parse_args(['bridge', '--palette-interval-ms', str(interval_ms)])
                port = Port()
                times = []
                send = port.send
                port.send = lambda msg: (times.append(self.clock.value), send(msg))
                driver = make_driver(port, args)
                driver.initialize()
                driver.show({n: (0, 80, 255) for n in range(60, 66)}, force=True)
                driver.clear()
                self.assertEqual(len(port.sent), 5)
                self.assertTrue(all(abs(b-a-interval_ms/1000) < 1e-9 for a,b in zip(times[1:], times[2:])))
                args.protocol = 'rgb15'
                self.assertEqual(make_driver(Port(), args).min_interval, .001)
        self.assertEqual(PalettePartyKeys(Port(), min_interval=0).min_interval, .001)

    def test_batch_discards_obsolete_cues_and_turns_off_first(self):
        driver = PalettePartyKeys(Port())
        driver.initialize()
        blue = (0, 80, 255)
        driver.show({60: blue, 64: blue, 67: blue, 72: blue})
        self.assertEqual(driver.port.sent[-1].hex(),
                         "F0 05 30 7F 7F 20 00 71 03 3C 09 40 09 43 09 F7")
        self.clock.sleep(.25)
        driver.show({65: blue})
        self.assertEqual(driver.port.sent[-1].hex(),
                         "F0 05 30 7F 7F 20 00 71 03 3C 00 40 00 43 00 F7")
        self.clock.sleep(.25)
        driver.show({65: blue})
        self.assertEqual(driver.state[17], blue)
        self.assertEqual(driver.state[24], (0, 0, 0))  # Obsolete fourth note never lit.

    def test_failed_batch_does_not_record_any_keys(self):
        driver = PalettePartyKeys(Port())
        driver.initialize()
        with patch.object(driver.port, "send", side_effect=RuntimeError("gone")):
            with self.assertRaises(RuntimeError):
                driver.show({60: (0, 80, 255), 64: (0, 80, 255)}, force=True)
        self.assertEqual(driver.state[12], (0, 0, 0))
        self.assertEqual(driver.state[16], (0, 0, 0))

    def test_nonblocking_palette_init_never_sweeps_keyboard(self):
        driver = PalettePartyKeys(Port())
        driver.initialize(blocking=False)
        for _ in range(5):
            driver.show({})
            self.clock.sleep(0.25)
        self.assertFalse(driver.initializing)
        self.assertEqual(len(driver.port.sent), 1)
        driver.invalidate_note(83)
        driver.show({})
        self.assertEqual(driver.port.sent[-1].data[-2:], (83, 0))


class BridgeTests(ClockedTests):
    def test_synthesia_unplayed_cue_ends_on_common_off_channel(self):
        bridge = Bridge()
        for channel in (8, 11, 12):
            bridge.receive(mido.Message("note_on", note=64, channel=channel, velocity=1))
        bridge.receive(mido.Message("note_on", note=67, channel=9, velocity=1))
        self.assertEqual(bridge.lights()[64], (255, 255, 0))
        bridge.receive(mido.Message("note_off", note=64, channel=0))
        self.assertEqual(bridge.lights(), {67: (255, 255, 0)})
        self.assertFalse(any(key[1] == 64 for key in bridge.counts))
        bridge.receive(mido.Message("note_on", note=67, channel=0, velocity=0))
        self.assertEqual(bridge.lights(), {})


    def test_bridge_cli_defaults_left_blue_right_green(self):
        args = parser().parse_args(["bridge"])
        self.assertEqual(args.left_color, (0, 80, 255))
        self.assertEqual(args.right_color, (0, 255, 0))




    def test_clear_reassertion_after_local_key_effect(self):
        driver = PartyKeys(Port())
        driver.initialize()
        driver.show({}, force=True)
        before = len(driver.port.sent)
        driver.invalidate_note(60)
        driver.show({}, force=True)
        self.assertEqual(len(driver.port.sent), before + 1)
        self.assertEqual(driver.port.sent[-1], list(rgb_messages({12: (0, 0, 0)}))[0])

    def test_polling_input_parses_messages_without_installing_callback(self):
        rt = Mock()
        rt.get_ports.return_value = ["IAC Lights"]
        rt.get_message.side_effect = [([0x91, 60, 80], 0), ([0x81, 60, 0], 0.1), None]
        with patch("rtmidi.MidiIn", return_value=rt):
            with MidiInput("IAC Lights") as source:
                messages = list(source.iter_pending())
        self.assertEqual([m.type for m in messages], ["note_on", "note_off"])
        self.assertEqual(messages[0].channel, 1)
        rt.set_callback.assert_not_called()
        rt.close_port.assert_called_once()
        rt.delete.assert_called_once()

    def test_polling_input_disappears_before_open(self):
        rt = Mock()
        rt.get_ports.return_value = []
        with patch("rtmidi.MidiIn", return_value=rt):
            with self.assertRaises(OSError):
                MidiInput("IAC Lights")
        rt.delete.assert_called_once()

    def test_hands_share_pitch_and_release_independently(self):
        bridge = Bridge(left=(0, 0, 255), right=(255, 0, 0))
        for channel in (1, 6):
            bridge.receive(mido.Message("note_on", note=60, channel=channel))
        self.assertEqual(bridge.lights()[60], (255, 0, 255))
        bridge.receive(mido.Message("note_off", note=60, channel=1))
        self.assertEqual(bridge.lights()[60], (255, 0, 0))
        bridge.receive(mido.Message("note_on", note=60, channel=6, velocity=0))
        self.assertEqual(bridge.lights(), {})

    def test_duplicate_and_control_reset(self):
        bridge = Bridge()
        message = mido.Message("note_on", note=60, channel=11)
        bridge.receive(message)
        bridge.receive(message)
        bridge.receive(mido.Message("note_off", note=60, channel=11))
        self.assertIn(60, bridge.lights())
        bridge.receive(mido.Message("note_off", note=60, channel=11))
        self.assertEqual(bridge.lights(), {})
        bridge.receive(message)
        bridge.receive(mido.Message("control_change", channel=0, control=16, value=4))
        self.assertEqual(bridge.lights(), {})

    def test_custom_channels_and_channel_local_all_notes_off(self):
        bridge = Bridge(left_channels={15}, right_channels={14})
        for ch in (0, 14, 15):
            bridge.receive(mido.Message("note_on", channel=ch, note=60 + ch))
        self.assertEqual(set(bridge.lights()), {74, 75})
        bridge.receive(mido.Message("control_change", channel=15, control=123))
        self.assertEqual(set(bridge.lights()), {74})

    def test_preview_disconnection_detection(self):
        with patch("mido.get_output_names", return_value=[]):
            with self.assertRaises(OSError):
                check_connections(output_name="PartyKeys")


class MidiParsingTests(unittest.TestCase):
    def load(self, midi):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "song.mid"
            midi.save(path)
            return Song.load(path)

    def test_tempo_changes_across_tracks(self):
        midi = mido.MidiFile(ticks_per_beat=480)
        midi.tracks.append(mido.MidiTrack([
            mido.MetaMessage("set_tempo", tempo=500000),
            mido.MetaMessage("set_tempo", tempo=1000000, time=480),
        ]))
        midi.tracks.append(mido.MidiTrack([
            mido.Message("note_on", note=60),
            mido.Message("note_off", note=60, time=960),
        ]))
        result = self.load(midi)
        self.assertEqual(result.notes, [Note(0, 1.5, 60, 0, 1, 64)])
        self.assertEqual(result.duration, 1.5)

    def test_repeated_notes_fifo_zero_velocity_and_dangling_notes(self):
        midi = mido.MidiFile(ticks_per_beat=100)
        midi.tracks.append(mido.MidiTrack([
            mido.Message("note_on", note=60, velocity=70),
            mido.Message("note_on", note=60, time=100, velocity=90),
            mido.Message("note_on", note=60, time=100, velocity=0),
            mido.Message("note_on", note=62),
            mido.Message("note_off", note=60, time=100),
        ]))
        result = self.load(midi)
        self.assertEqual([(n.start, n.end, n.pitch) for n in result.notes],
                         [(0, 1, 60), (0.5, 1.5, 60), (1, 1.5, 62)])

    def test_simultaneous_tracks_keep_identity(self):
        midi = mido.MidiFile()
        for _ in range(2):
            midi.tracks.append(mido.MidiTrack([mido.Message("note_on", note=60),
                                               mido.Message("note_off", note=60, time=480)]))
        self.assertEqual({n.track for n in self.load(midi).notes}, {0, 1})

    def test_type_two_rejected(self):
        midi = mido.MidiFile(type=2)
        midi.tracks.append(mido.MidiTrack())
        with self.assertRaises(ValueError):
            self.load(midi)


class PreviewTests(unittest.TestCase):
    def test_restart_preroll(self):
        clock = Transport(10, speed=2)
        clock.seek(5)
        clock.restart(2)
        self.assertEqual(clock.position(), -4)

    def test_stages_and_boundaries(self):
        preview = Preview(song(note()), right=(0, 0, 200))
        self.assertEqual(preview.lights(-0.01), {})
        self.assertEqual(preview.lights(0)[60], (0, 0, 40))
        self.assertEqual(preview.lights(1)[60], (0, 0, 200))
        self.assertEqual(preview.lights(2)[60], (0, 255, 0))
        self.assertEqual(preview.lights(3), {})

    def test_overlap_active_priority_and_repeat_survives_release(self):
        preview = Preview(song(note(start=0, end=3), note(start=2, end=4)))
        self.assertEqual(preview.lights(1)[60], (0, 255, 0))
        self.assertEqual(preview.lights(3.5)[60], (0, 255, 0))
        self.assertEqual(preview.lights(4), {})

    def test_seek_and_speed_recompute_real_time_lookahead(self):
        preview = Preview(song(note(start=4, end=5)))
        self.assertEqual(preview.lights(1, 1), {})
        self.assertIn(60, preview.lights(1, 2))
        self.assertEqual(preview.lights(6, 2), {})
        self.assertIn(60, preview.lights(4.5, 1))
        self.assertEqual(preview.lights(0, 1), {})

    def test_zero_lookahead_and_track_priority(self):
        preview = Preview(song(note(channel=0, track=1)), left_tracks={1})
        self.assertEqual(preview.lights(1)[60], preview.left)
        immediate = Preview(song(note()), lookahead=0, near=0)
        self.assertEqual(immediate.lights(1.999), {})
        self.assertEqual(immediate.lights(2)[60], immediate.active_color)

    def test_clock_pause_seek_speed_without_drift(self):
        now = [10.0]
        clock = Transport(100, now=lambda: now[0])
        clock.play()
        now[0] += 2
        self.assertEqual(clock.position(), 2)
        clock.pause()
        now[0] += 10
        self.assertEqual(clock.position(), 2)
        clock.set_speed(0.5)
        clock.play()
        now[0] += 4
        self.assertEqual(clock.position(), 4)
        clock.seek(50)
        now[0] += 2
        self.assertEqual(clock.position(), 51)
        clock.set_speed(2)
        now[0] += 1
        self.assertEqual(clock.position(), 53)

    def test_bad_numeric_settings(self):
        for bad in (-1, float("nan"), float("inf"), 6):
            with self.assertRaises(ValueError):
                Preview(song(note()), lookahead=bad)
        with self.assertRaises(ValueError):
            Transport(10).set_speed(0)
        with self.assertRaises(ValueError):
            Transport(10).seek(float("nan"))

    def test_audio_seek_restore_and_no_repeated_onset(self):
        port = Port()
        data = song(note(start=0, end=2))
        data.events = [(0, mido.Message("program_change", program=5)),
                       (0, mido.Message("note_on", note=60)),
                       (2, mido.Message("note_off", note=60))]
        player = AudioPlayback(data, port)
        player.seek(1)
        self.assertEqual([m.type for m in port.sent], ["program_change", "note_on"])
        player.advance(1.5)
        self.assertEqual(len(port.sent), 2)
        player.advance(2)
        self.assertEqual(port.sent[-1].type, "note_off")
        self.assertEqual(port.panics, 1)

    def test_cli_channels_are_one_based(self):
        args = parser().parse_args(["bridge", "--left-channels", "2,12", "--right-channels", "7"])
        self.assertEqual(args.left_channels, {1, 11})
        self.assertEqual(args.right_channels, {6})

    def test_cli_preview_transport_and_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "short.mid"
            midi = mido.MidiFile(ticks_per_beat=480)
            midi.tracks.append(mido.MidiTrack([mido.Message("note_on", note=60),
                                               mido.Message("note_off", note=60, time=96)]))
            midi.save(path)
            command = [sys.executable, "-m", "partykeys_bridge", "preview", str(path),
                       "--dry-run", "--lookahead", "0.1", "--paused"]
            result = subprocess.run(command, input="play\npause\nseek 0.05\nspeed 2\nresume\nstop\nplay\n",
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("NOT synchronised with Synthesia", result.stdout)
            self.assertIn("2x", result.stdout)
            self.assertIn("Finished.", result.stdout)
            self.assertIn("0F 01 F7", result.stdout)


if __name__ == "__main__":
    unittest.main()
