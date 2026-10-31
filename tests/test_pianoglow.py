import unittest
import mido
from partykeys_bridge.bridge import Bridge


class PianoGlowTests(unittest.TestCase):
    def test_practice_hint_survives_performance_release_and_resync(self):
        bridge = Bridge(right=(0, 255, 0))
        guide_on = mido.Message.from_bytes([0x9f, 60, 0])
        bridge.receive(guide_on)
        bridge.receive(guide_on)
        self.assertEqual(bridge.lights(), {60: (0, 255, 0)})
        bridge.receive(mido.Message('note_on', channel=0, note=60, velocity=80))
        bridge.receive(mido.Message('note_off', channel=0, note=60))
        self.assertEqual(bridge.lights(), {60: (0, 255, 0)})
        bridge.receive(mido.Message.from_bytes([0x8f, 60, 0]))
        self.assertEqual(bridge.lights(), {})

    def test_chord_changes_and_reset(self):
        bridge = Bridge()
        for note in (60, 64, 67):
            bridge.receive(mido.Message.from_bytes([0x9f, note, 0]))
        bridge.receive(mido.Message.from_bytes([0x8f, 64, 0]))
        self.assertEqual(set(bridge.lights()), {60, 67})
        bridge.receive(mido.Message('stop'))
        self.assertEqual(bridge.lights(), {})

    def test_explicit_channel_16_mapping_retains_standard_note_semantics(self):
        bridge = Bridge(left_channels={15}, right_channels={0})
        bridge.receive(mido.Message.from_bytes([0x9f, 60, 0]))
        self.assertEqual(bridge.lights(), {})
        bridge.receive(mido.Message.from_bytes([0x9f, 60, 90]))
        self.assertIn(60, bridge.lights())
        bridge.receive(mido.Message('note_off', channel=0, note=60))
        self.assertEqual(bridge.lights(), {})

    def test_positive_channel_16_note_is_not_a_guide(self):
        bridge = Bridge()
        bridge.receive(mido.Message.from_bytes([0x9f, 60, 90]))
        self.assertEqual(bridge.lights(), {})
