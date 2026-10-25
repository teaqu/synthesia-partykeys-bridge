from copy import deepcopy
import json
from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace
import unittest

import mido

from partykeys_bridge.bridge import Bridge
from partykeys_bridge.practice import capture_key, practice_lights, ExactRGB, LiveLighting, paint_practice, physical_key_event
from partykeys_bridge import settings
from partykeys_bridge.driver import PalettePartyKeys


class TrailAndColorTests(unittest.TestCase):
    def test_disabling_bridge_hides_cues_and_trails_but_keeps_practice_and_input_state(self):
        now = [0.]
        bridge = Bridge(left=(0, 255, 0), now=lambda: now[0])
        bridge.set_practice(dict(enabled=True, notes=[60], color="FF0000", brightness=.1))
        bridge.set_trail(dict(enabled=True, duration_seconds=2., max_brightness=.2))
        bridge.receive(mido.Message("note_on", channel=1, note=64))
        bridge.receive(mido.Message("note_off", note=64))
        bridge.receive(mido.Message("note_on", channel=1, note=60))
        bridge.enabled = False
        self.assertEqual(bridge.lights(), {60: (26, 0, 0)})
        bridge.enabled = True
        self.assertEqual(bridge.lights()[60], (0, 255, 0))
        self.assertEqual(bridge.lights()[64], (0, 51, 0))
        bridge.enabled = False
        bridge.receive(mido.Message("note_off", note=60))
        now[0] = 3.
        bridge.enabled = True
        self.assertFalse(bridge.active)
        self.assertEqual(bridge.lights(), {60: (26, 0, 0)})

    def test_bridge_brightness_updates_held_cues_without_dimming_background_layers(self):
        bridge = Bridge(left=(0, 255, 0), now=lambda: 0.)
        bridge.set_practice(dict(enabled=True, notes=[60], color="FF0000", brightness=.1))
        bridge.set_trail(dict(enabled=True, duration_seconds=2., max_brightness=.2))
        bridge.receive(mido.Message("note_on", channel=1, note=64))
        bridge.receive(mido.Message("note_off", note=64))
        bridge.receive(mido.Message("note_on", channel=1, note=60))
        original = bridge.lights()[60]
        self.assertNotIsInstance(original, ExactRGB)
        bridge.set_brightness(.4)
        self.assertEqual(bridge.lights()[60], (0, 102, 0))
        self.assertIsInstance(bridge.lights()[60], ExactRGB)
        self.assertEqual(bridge.lights()[64], (0, 51, 0))
        bridge.set_brightness(0)
        self.assertEqual(bridge.lights()[60], (26, 0, 0))
        self.assertEqual(bridge.lights()[64], (0, 51, 0))
        bridge.set_brightness(1)
        self.assertEqual(bridge.lights()[60], original)
        self.assertNotIsInstance(bridge.lights()[60], ExactRGB)
        self.assertEqual(bridge.counts, {(1, 60): 1})

    def test_dimmed_bridge_cues_use_rgb_instead_of_fixed_brightness_palette(self):
        now, sent = [0.], []
        driver = PalettePartyKeys(SimpleNamespace(send=sent.append), boards=[(1, 63, 48)],
                                  now=lambda: now[0], sleep=lambda t: now.__setitem__(0, now[0] + t))
        driver.initialize()
        bridge = Bridge(left=(0, 255, 0))
        bridge.receive(mido.Message("note_on", channel=1, note=60))
        driver.show(bridge.lights(), force=True)
        self.assertEqual(sent[-1].data[6], 0x71)
        bridge.set_brightness(.2)
        driver.show(bridge.lights(), force=True)
        self.assertEqual(sent[-1].data[6], 0x15)
        self.assertEqual(driver.state[12], (0, 51, 0))
        bridge.set_brightness(1)
        driver.show(bridge.lights(), force=True)
        self.assertEqual(sent[-1].data[6], 0x71)

    def test_long_trail_is_dim_on_release_and_visibly_fades_immediately(self):
        now = [0.]
        bridge = Bridge(left=(255, 0, 0), now=lambda: now[0])
        bridge.set_trail(dict(enabled=True, duration_seconds=30., max_brightness=.4))
        bridge.receive(mido.Message("note_on", channel=1, note=60))
        self.assertEqual(bridge.lights()[60], (255, 0, 0))
        bridge.receive(mido.Message("note_off", channel=0, note=60))
        self.assertEqual(bridge.lights()[60], (102, 0, 0))
        now[0] = .06
        self.assertLess(bridge.lights()[60][0], 80)
        now[0] = .12
        self.assertLessEqual(bridge.lights()[60][0], 51)
        now[0] = 10.
        self.assertGreater(bridge.lights()[60][0], 0)
        now[0] = 30.
        self.assertNotIn(60, bridge.lights())

    def test_trail_brightness_changes_apply_live_and_zero_hides_only_the_trail(self):
        now = [0.]
        bridge = Bridge(left=(0, 255, 0), now=lambda: now[0])
        bridge.set_trail(dict(enabled=True, duration_seconds=12., max_brightness=.8))
        bridge.receive(mido.Message("note_on", channel=1, note=60))
        bridge.receive(mido.Message("note_off", channel=0, note=60))
        self.assertEqual(bridge.lights()[60], (0, 204, 0))
        bridge.set_trail(dict(enabled=True, duration_seconds=12., max_brightness=.2))
        self.assertEqual(bridge.lights()[60], (0, 51, 0))
        bridge.set_practice(dict(enabled=True, notes=[60], color="FF0000", brightness=.1))
        bridge.set_trail(dict(enabled=True, duration_seconds=12., max_brightness=0))
        self.assertEqual(bridge.lights()[60], (26, 0, 0))

    def test_rgb_and_palette_can_both_send_at_1ms(self):
        now, sent = [0.], []
        driver = PalettePartyKeys(SimpleNamespace(send=lambda msg: sent.append((now[0], msg))),
                                  boards=[(1, 63, 48)], min_interval=.001,
                                  now=lambda: now[0], sleep=lambda t: now.__setitem__(0, now[0] + t))
        driver.initialize()
        driver.show({60: ExactRGB((50, 0, 0)), 64: ExactRGB((0, 50, 0))}, force=True)
        driver.show({60: (0, 255, 0)}, force=True)
        for (before, _), (after, _) in zip(sent[1:], sent[2:]):
            self.assertAlmostEqual(after - before, .001)
        self.assertTrue(any(msg.data[6] == 0x15 for _, msg in sent))
        self.assertTrue(any(msg.data[6] == 0x71 for _, msg in sent))

    def test_live_snapshots_and_capture_need_no_config_file(self):
        live = LiveLighting()
        practice = dict(enabled=True, notes=[60], color="FF0000", brightness=.2, note_colors={})
        live.publish(practice, dict(enabled=False, duration_seconds=1.5))
        generation, value = live.snapshot()
        self.assertEqual(generation, 1)
        practice["notes"].append(65)
        self.assertEqual(value[0]["notes"], [60])
        painted = paint_practice(value[0], 64)
        self.assertEqual(painted["note_colors"]["64"], "FF0000")
        self.assertEqual(value[0]["notes"], [60])
        self.assertNotIn(64, paint_practice(painted, 64, erase=True)["notes"])

    def test_normal_rgb_updates_do_not_sleep_on_midi_thread(self):
        now, sent = [0.], []
        driver = PalettePartyKeys(SimpleNamespace(send=sent.append), boards=[(1, 63, 48)],
                                  now=lambda: now[0], sleep=lambda t: now.__setitem__(0, now[0] + t))
        driver.initialize()
        driver.show({60: ExactRGB((50, 0, 0))})
        driver.sleep = lambda _: self.fail("Live output must not sleep on the MIDI thread")
        count = len(sent)
        now[0] += .0005
        driver.show({60: ExactRGB((40, 0, 0))})
        self.assertEqual(len(sent), count)
        now[0] += .3
        driver.show({60: ExactRGB((30, 0, 0))})
        self.assertEqual(driver.state[12], (30, 0, 0))
        now[0] += .3
        driver.show({})  # RGB clear and palette off are separate nonblocking ticks.
        now[0] += .3
        driver.show({})
        self.assertEqual(driver.state[12], (0, 0, 0))

    def test_rgb_layer_clears_before_palette_and_after_note_invalidation(self):
        now, sent = [0.], []
        driver = PalettePartyKeys(SimpleNamespace(send=sent.append), boards=[(1, 63, 48)],
                                  now=lambda: now[0], sleep=lambda t: now.__setitem__(0, now[0] + t))
        driver.initialize()
        driver.show({60: ExactRGB((25, 0, 0))}, force=True)
        sent.clear()
        driver.show({60: (0, 255, 0)}, force=True)
        self.assertEqual([m.data[6] for m in sent], [0x15, 0x71])
        self.assertEqual(sent[0].data[8:14], (0, 0, 0, 0, 0, 0))
        driver.show({60: ExactRGB((25, 0, 0))}, force=True)
        driver.invalidate_note(60)
        sent.clear()
        driver.show({}, force=True)
        self.assertEqual([m.data[6] for m in sent], [0x15, 0x71])
        self.assertFalse(driver.rgb_latched)

    def test_multiple_fading_keys_get_fair_updates(self):
        now = [0.]
        driver = PalettePartyKeys(SimpleNamespace(send=lambda _: None), boards=[(1, 63, 48)],
                                  now=lambda: now[0], sleep=lambda t: now.__setitem__(0, now[0] + t))
        driver.initialize()
        driver.show({60: ExactRGB((100, 0, 0)), 64: ExactRGB((0, 100, 0))})
        now[0] += .3
        driver.show({60: ExactRGB((90, 0, 0)), 64: ExactRGB((0, 90, 0))})
        self.assertEqual(driver.state[16], (0, 90, 0))

    def test_per_key_colors_and_legacy_color_fallback(self):
        config = dict(enabled=True, notes=[60, 64, 67], color="0000FF", brightness=.2,
                      note_colors={"60": "FF0000", "64": "00FF00"})
        self.assertEqual(practice_lights(config), {60: (51, 0, 0), 64: (0, 51, 0), 67: (0, 0, 51)})
        config["brightness"] = 0
        self.assertEqual(practice_lights(config), {})

    def test_capture_only_physical_note_on_and_only_while_armed(self):
        args = SimpleNamespace(practice_capture=threading.Event(), practice_key_events=queue.SimpleQueue())
        capture_key(args, mido.Message("note_on", note=60))
        self.assertTrue(args.practice_key_events.empty())
        args.practice_capture.set()
        capture_key(args, mido.Message("note_on", note=60, velocity=80))
        capture_key(args, mido.Message("note_on", note=60, velocity=0))
        capture_key(args, mido.Message("note_off", note=60))
        self.assertEqual(args.practice_key_events.get_nowait(), 60)
        self.assertTrue(args.practice_key_events.empty())

    def test_gui_key_stream_includes_releases_and_resets_without_paint_capture(self):
        args = SimpleNamespace(physical_key_events=queue.SimpleQueue())
        messages = [mido.Message("note_on", note=60),
                    mido.Message("note_off", note=60),
                    mido.Message("note_on", note=61, velocity=0),
                    mido.Message("control_change", channel=2, control=123),
                    mido.Message("control_change", channel=2, control=120),
                    mido.Message("stop"), mido.Message("reset")]
        for message in messages:
            physical_key_event(args, message)
        physical_key_event(args, mido.Message("control_change", control=64))
        physical_key_event(args, mido.Message("clock"))
        for expected in messages:
            actual = args.physical_key_events.get_nowait()
            self.assertEqual(actual, expected)
            self.assertIsNot(actual, expected)
        self.assertTrue(args.physical_key_events.empty())
        physical_key_event(args)
        self.assertIsNone(args.physical_key_events.get_nowait())

    def test_trail_fades_after_release_and_retrigger_restores_full_color(self):
        now = [10.]
        bridge = Bridge(left=(200, 100, 0), now=lambda: now[0])
        bridge.set_trail(dict(enabled=True, duration_seconds=2.))
        on = mido.Message("note_on", channel=1, note=60, velocity=80)
        off = mido.Message("note_off", channel=0, note=60)
        bridge.receive(on)
        now[0] += 10
        self.assertEqual(bridge.lights()[60], (200, 100, 0))
        bridge.receive(off)
        now[0] += 1
        self.assertEqual(bridge.lights()[60], (18, 9, 0))
        self.assertIsInstance(bridge.lights()[60], ExactRGB)
        bridge.receive(on)
        self.assertEqual(bridge.lights()[60], (200, 100, 0))
        self.assertFalse(bridge.released)
        bridge.receive(off)
        now[0] += 2
        self.assertNotIn(60, bridge.lights())

    def test_trail_preserves_practice_and_clears_on_stop_or_disable(self):
        now = [0.]
        bridge = Bridge(left=(200, 0, 0), now=lambda: now[0])
        bridge.set_practice(dict(enabled=True, notes=[60], color="FF0000", brightness=.1))
        bridge.set_trail(dict(enabled=True, duration_seconds=2.))
        for stop in (mido.Message("stop"), mido.Message("reset"),
                     mido.Message("control_change", channel=0, control=16)):
            bridge.receive(mido.Message("note_on", channel=1, note=60))
            bridge.receive(mido.Message("note_off", channel=0, note=60))
            now[0] += 1.9
            self.assertEqual(bridge.lights()[60], (26, 0, 0))
            bridge.receive(stop)
            self.assertFalse(bridge.released)
            self.assertEqual(bridge.lights()[60], (26, 0, 0))
        bridge.receive(mido.Message("note_on", channel=1, note=64))
        bridge.receive(mido.Message("note_off", channel=0, note=64))
        bridge.set_trail(dict(enabled=False, duration_seconds=2.))
        self.assertNotIn(64, bridge.lights())

    def test_invalid_colors_and_trail_settings_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            for section, key, value in (("practice", "note_colors", {"128": "FF0000"}),
                                        ("lighting", "bridge_brightness", -0.1),
                                        ("lighting", "bridge_brightness", 1.1),
                                        ("lighting", "bridge_brightness", float("nan")),
                                        ("lighting", "bridge_enabled", "yes"),
                                        ("practice", "note_colors", {"60": "wrong"}),
                                        ("trail", "duration_seconds", 0),
                                        ("trail", "duration_seconds", float("nan")),
                                        ("trail", "max_brightness", -0.1),
                                        ("trail", "max_brightness", 1.1),
                                        ("trail", "max_brightness", float("nan")),
                                        ("trail", "enabled", "yes")):
                config = deepcopy(settings.DEFAULTS)
                config[section][key] = value
                path.write_text(json.dumps(config))
                with self.assertRaises(ValueError):
                    settings.load(path)
