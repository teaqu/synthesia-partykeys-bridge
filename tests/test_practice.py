from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import mido

from partykeys_bridge.bridge import Bridge
from partykeys_bridge.driver import PalettePartyKeys, BLACK
from partykeys_bridge.practice import ExactRGB
from partykeys_bridge import settings
from partykeys_bridge.cli import parser


class PracticeTests(unittest.TestCase):
    def test_cue_takes_priority_and_release_stop_and_reset_restore_background(self):
        bridge = Bridge()
        bridge.set_practice(dict(enabled=True, notes=[60, 64], color="FF0000", brightness=.1))
        faint = (26, 0, 0)
        self.assertEqual(bridge.lights(), {60: faint, 64: faint})
        for release in (mido.Message("note_off", channel=0, note=60),
                        mido.Message("stop"), mido.Message("reset"),
                        mido.Message("control_change", control=16, channel=0)):
            bridge.receive(mido.Message("note_on", channel=6, note=60))
            self.assertEqual(bridge.lights()[60], (255, 255, 0))
            self.assertNotIsInstance(bridge.lights()[60], ExactRGB)
            bridge.receive(release)
            self.assertEqual(bridge.lights()[60], faint)
            self.assertIsInstance(bridge.lights()[60], ExactRGB)
        bridge.set_practice(dict(enabled=False, notes=[60], color="FF0000", brightness=.1))
        self.assertEqual(bridge.lights(), {})

    def test_mixed_protocol_directed_rgb_and_octave_mapping_and_pacing(self):
        now, sent = [0.0], []
        def sleep(seconds): now[0] += seconds
        driver = PalettePartyKeys(SimpleNamespace(send=lambda m: sent.append((now[0], m))),
                                  boards=[(1, 0x3f, 48), (2, 0x3e, 84)], min_interval=.01,
                                  now=lambda: now[0], sleep=sleep)
        driver.initialize()
        driver.show({60: ExactRGB((2, 4, 8)), 90: ExactRGB((8, 4, 2))}, force=True)
        rgb = [(t, m) for t, m in sent if m.data[6] == 0x15]
        self.assertEqual(len(rgb), 2)
        self.assertEqual(rgb[0][1].data[2:4], (1, 0x3f))
        self.assertEqual(rgb[0][1].data[-1], 12)
        self.assertEqual(rgb[1][1].data[2:4], (2, 0x3e))
        self.assertEqual(rgb[1][1].data[-1], 6)
        self.assertAlmostEqual(rgb[1][0] - rgb[0][0], .01)
        # Cue overrides use the fast palette and restore exact RGB afterwards.
        driver.show({60: (0, 255, 0)}, force=True)
        self.assertEqual(driver.state[12], (0, 255, 0))
        self.assertGreaterEqual(sent[-1][0] - rgb[-1][0], .01 - 1e-9)
        driver.show({60: ExactRGB((2, 4, 8))}, force=True)
        driver.set_octave(1, -1)
        driver.show({48: ExactRGB((2, 4, 8))}, force=True)
        self.assertEqual(sent[-1][1].data[-1], 12)
        driver.request_full_clear()
        driver.clear()
        self.assertTrue(all(value == BLACK for value in driver.state.values()))

    def test_live_edit_preserves_cues_and_rejects_invalid_practice_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            config = deepcopy(settings.DEFAULTS)
            path.write_text(json.dumps(config))
            args = parser().parse_args(["bridge"])
            settings.apply_startup(args, config, [])
            watcher = settings.ConfigWatcher(path, deepcopy(config), args, [])
            bridge = Bridge()
            bridge.receive(mido.Message("note_on", channel=1, note=60))
            config["practice"].update(enabled=True, notes=[60, 64], brightness=.1)
            path.write_text(json.dumps(config))
            watcher.poll()
            watcher.apply(SimpleNamespace(bridge=bridge, args=args, driver=None))
            self.assertIn((1, 60), bridge.active)
            self.assertIn(64, bridge.lights())
            for field, value in (("notes", [True]), ("notes", [128]), ("notes", "60"),
                                 ("color", "blue"), ("brightness", float("nan")),
                                 ("brightness", 1.1), ("enabled", 1)):
                bad = deepcopy(config)
                bad["practice"][field] = value
                path.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    settings.load(path)

    def test_ui_saves_preserve_routing_and_old_config_gets_defaults(self):
        from partykeys_bridge.ui import save_config
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text('{"midi": {"input": "My bus"}}')
            self.assertFalse(settings.load(path)["practice"]["enabled"])
            practice = dict(enabled=True, notes=[48, 60], color="ABCDEF", brightness=.07)
            saved = save_config(path, practice, {"left_color": "FF0000"})
            self.assertEqual(saved["midi"]["input"], "My bus")
            self.assertEqual(settings.load(path)["practice"], dict(practice, note_colors={}))
            self.assertFalse(path.with_suffix(".json.tmp").exists())
