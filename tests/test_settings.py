from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import mido

from partykeys_bridge.bridge import Bridge
from partykeys_bridge.cli import parser, main
from partykeys_bridge.driver import PalettePartyKeys
from partykeys_bridge import settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.json"
        self.config = deepcopy(settings.DEFAULTS)
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.config), encoding="utf-8")

    def test_reload_recolours_active_notes_without_full_clear(self):
        args = parser().parse_args(["bridge"])
        settings.apply_startup(args, self.config, [])
        bridge = Bridge(args.left_color, args.right_color)
        sent = []
        clock = [0.0]
        def sleep(seconds): clock[0] += seconds
        driver = PalettePartyKeys(SimpleNamespace(send=sent.append), min_interval=.01,
                                  now=lambda: clock[0], sleep=sleep)
        driver.initialize()
        bridge.receive(mido.Message("note_on", channel=1, note=60))
        bridge.receive(mido.Message("note_on", channel=6, note=64))
        driver.show(bridge.lights(), force=True)
        watcher = settings.ConfigWatcher(self.path, deepcopy(self.config), args, [])
        self.config["palette"]["blue"] = "FF0000"
        self.save()
        watcher.poll()
        watcher.apply(SimpleNamespace(args=args, bridge=bridge, driver=driver))
        self.assertEqual(bridge.counts, {(1, 60): 1, (6, 64): 1})
        before = len(sent)
        driver.show(bridge.lights(), force=True)
        self.assertEqual(len(sent) - before, 1)
        self.assertEqual(sent[-1].data[-4:], (0x71, 1, 60, 1))
        bridge.receive(mido.Message("note_off", channel=0, note=60))
        bridge.receive(mido.Message("note_off", channel=0, note=64))
        driver.show(bridge.lights(), force=True)
        self.assertEqual(sent[-1].data[-6:], (0x71, 2, 60, 0, 64, 0))
        self.path.write_text('{"lighting":', encoding="utf-8")
        with self.assertLogs("partykeys", level="WARNING"):
            watcher.poll()
        self.assertTrue(watcher.pending.empty())

    def test_live_background_reload_and_clean_stop(self):
        self.config["poll_interval_ms"] = 100
        self.save()
        args = parser().parse_args(["bridge"])
        settings.apply_startup(args, self.config, [])
        watcher = settings.ConfigWatcher(self.path, settings.load(self.path), args, [])
        watcher.start()
        try:
            self.config["lighting"]["left_color"] = "purple"
            self.save()
            colors, _ = watcher.pending.get(timeout=3)
            self.assertEqual(colors[0], (180, 0, 255))
        finally:
            watcher.close()
        self.assertFalse(watcher.thread.is_alive())

    def test_explicit_cli_overrides_config_and_existing_bus_stays_existing(self):
        argv = ["bridge", "--input", "Other MIDI", "--left-color=FFFFFF", "--palette-interval-ms", "40"]
        args = parser().parse_args(argv)
        settings.apply_startup(args, self.config, argv)
        self.assertFalse(args.virtual_input)
        self.assertEqual(args.left_color, (255,255,255))
        self.assertEqual(args.input, "Other MIDI")
        self.assertEqual(args.palette_interval_ms, 40)

    def test_bad_config_stops_before_opening_hardware(self):
        self.config["midi"]["create_virtual_input"] = "yes"
        self.save()
        with patch("partykeys_bridge.cli.execute") as execute:
            self.assertEqual(main(["bridge", "--config", str(self.path)]), 1)
        execute.assert_not_called()

    def test_disable_reload_requires_restart_to_enable(self):
        self.config["live_reload"] = False
        watcher = settings.ConfigWatcher(self.path, self.config, parser().parse_args(["bridge"]), [])
        watcher.start()
        self.assertIsNone(watcher.thread)
        watcher.close()

    def test_shipped_config_matches_defaults(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(settings.load(root / "config.json"), settings.DEFAULTS)
