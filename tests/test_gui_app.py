import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from partykeys_bridge import gui_app, settings
from partykeys_bridge.cli import main


class GuiAppTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.app = self.root / "PartyKeys Lights.app"
        self.exe = self.app / "Contents" / "MacOS" / "PartyKeys Lights"
        self.exe.parent.mkdir(parents=True)
        self.exe.write_bytes(b"unchanged application")
        self.home = self.root / "home"
        for target, value in (("sys.platform", "darwin"), ("sys.frozen", True),
                              ("sys.executable", str(self.exe))):
            item = patch(target, value, create=True)
            item.start()
            self.addCleanup(item.stop)
        item = patch("pathlib.Path.home", return_value=self.home)
        item.start()
        self.addCleanup(item.stop)

    def test_app_settings_live_outside_bundle_and_survive_app_replacement(self):
        expected = self.home / "Library/Application Support/PartyKeys Lights/config.json"
        self.assertEqual(settings.default_path(), expected)
        settings.prepare_app_config()
        config = settings.load(expected, required=True)
        config["practice"]["notes"] = [60, 64]
        expected.write_text(json.dumps(config))
        with patch("sys.executable", str(self.root / "new/PartyKeys Lights.app/Contents/MacOS/PartyKeys Lights")):
            settings.prepare_app_config()
            self.assertEqual(settings.default_path(), expected)
        self.assertEqual(settings.load(expected)["practice"]["notes"], [60, 64])
        self.assertEqual(self.exe.read_bytes(), b"unchanged application")
        self.assertFalse((self.exe.parent / "config.json").exists())

    def test_valid_sibling_config_is_imported_once_without_overwriting_settings(self):
        seed = self.app.parent / "config.json"
        seed.write_text('{"midi":{"input":"My bus"},"practice":{"notes":[48,60]}}')
        settings.prepare_app_config()
        self.assertEqual(settings.load(settings.default_path())["midi"]["input"], "My bus")
        seed.write_text('{"midi":{"input":"Changed seed"}}')
        settings.prepare_app_config()
        self.assertEqual(settings.load(settings.default_path())["midi"]["input"], "My bus")

    def test_bad_existing_settings_are_reported_without_replacing_them(self):
        path = settings.default_path()
        path.parent.mkdir(parents=True)
        path.write_text("invalid JSON")
        with patch("tkinter.messagebox.showerror") as error, patch("partykeys_bridge.gui_app.cli.main") as run:
            self.assertEqual(gui_app.main([]), 1)
        error.assert_called_once()
        run.assert_not_called()
        self.assertEqual(path.read_text(), "invalid JSON")

    def test_invalid_sibling_config_does_not_block_first_launch(self):
        seed = self.app.parent / "config.json"
        seed.write_text("invalid JSON")
        with self.assertLogs("partykeys", level="WARNING"):
            settings.prepare_app_config()
        self.assertEqual(settings.load(settings.default_path()), settings.DEFAULTS)
        self.assertEqual(seed.read_text(), "invalid JSON")

    def test_finder_defaults_to_gui_and_explicit_config_is_preserved(self):
        argv = ["--config", str(self.root / "custom.json")]
        Path(argv[1]).write_text("{}")
        with patch("partykeys_bridge.gui_app.cli.main", return_value=0) as run:
            self.assertEqual(gui_app.main(argv), 0)
        run.assert_called_once_with(["gui", *argv])
        self.assertFalse(settings.default_path().exists())

    def test_smoke_entry_does_not_initialize_or_read_user_settings(self):
        with patch("partykeys_bridge.gui_app.cli.main", return_value=0) as run, \
                patch("partykeys_bridge.settings.prepare_app_config") as prepare, \
                patch("partykeys_bridge.settings.load") as load:
            self.assertEqual(gui_app.main(["--smoke-test"]), 0)
        run.assert_called_once_with(["gui", "--smoke-test"])
        prepare.assert_not_called()
        load.assert_not_called()

    def test_console_and_windows_default_paths_stay_beside_the_executable(self):
        with patch("sys.executable", str(self.root / "partykeys")):
            self.assertEqual(settings.default_path(), self.root / "config.json")
        with patch("sys.platform", "win32"):
            self.assertEqual(settings.default_path(), self.exe.parent / "config.json")

    def test_windowed_error_path_does_not_require_console_streams(self):
        with patch("sys.stdout", None), patch("sys.stderr", None), \
                patch("partykeys_bridge.cli.execute", side_effect=RuntimeError("Backend unavailable")):
            with self.assertLogs("partykeys", level="ERROR") as logs:
                self.assertEqual(main(["gui"]), 1)
        self.assertIn("Backend unavailable", logs.output[0])
