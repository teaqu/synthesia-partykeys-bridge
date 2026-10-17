import unittest
from unittest.mock import patch

from partykeys_bridge.windows_midi import named_port, existing_route, CueRoute, WindowsVirtualInput


class WindowsMidiTests(unittest.TestCase):
    def setUp(self):
        # Unit tests must not depend on a user's running bridge instance.
        guard = patch("partykeys_bridge.windows_midi.native_is_running", return_value=False)
        guard.start()
        self.addCleanup(guard.stop)

    def test_winmm_suffix_and_ambiguous_names(self):
        self.assertEqual(named_port(["Synthesia Lights Other 1", "Synthesia Lights 2"], "Synthesia Lights"),
                         "Synthesia Lights 2")
        self.assertIsNone(named_port(["Other Synthesia Lights 1"], "Synthesia Lights"))
        with self.assertRaisesRegex(ValueError, "More than one"):
            named_port(["Synthesia Lights 1", "Synthesia Lights 2"], "Synthesia Lights")

    def test_existing_single_port_is_reused(self):
        with patch("mido.get_input_names", return_value=["Synthesia Lights 2"]), \
                patch("mido.get_output_names", return_value=["Synthesia Lights 3"]):
            self.assertEqual(existing_route("Synthesia Lights"), CueRoute("Synthesia Lights 2", "Synthesia Lights 3"))

    def test_native_pair_not_mistaken_for_single_port_or_taken_over(self):
        with patch("mido.get_input_names", return_value=["Synthesia Lights 2", "Synthesia Lights Bridge 3"]), \
                patch("mido.get_output_names", return_value=["Synthesia Lights 4"]):
            with self.assertRaisesRegex(ValueError, "already exists"):
                existing_route("Synthesia Lights")

    def test_missing_helper_has_actionable_error(self):
        helper = WindowsVirtualInput(helper="does-not-exist/PartyKeys.Midi.exe")
        with self.assertRaisesRegex(ValueError, "Extract the entire"):
            helper.start()
        helper.close()

    def test_half_route_never_creates_duplicate(self):
        with patch("mido.get_input_names", return_value=[]), \
                patch("mido.get_output_names", return_value=["Synthesia Lights 4"]):
            with self.assertRaisesRegex(ValueError, "Incomplete"):
                existing_route("Synthesia Lights")
