from types import SimpleNamespace
import tkinter as tk
import unittest

from partykeys_bridge.ui import secondary_click_sequence


class MouseBindingTests(unittest.TestCase):
    def test_secondary_button_across_tk_versions_and_platforms(self):
        # Exercise Tcl's actual version comparison, without creating a window.
        interpreter = tk.Tcl()
        for platform, version, expected in (
            ("aqua", "8.6.14", "<Button-2>"),
            ("aqua", "8.7a5", "<Button-3>"),
            ("aqua", "8.7", "<Button-3>"),
            ("aqua", "9.0.1", "<Button-3>"),
            ("win32", "8.6.14", "<Button-3>"),
            ("x11", "8.6.14", "<Button-3>"),
        ):
            with self.subTest(platform=platform, version=version):
                def call(*args):
                    if args == ("tk", "windowingsystem"):
                        return platform
                    if args == ("package", "provide", "Tk"):
                        return version
                    return interpreter.call(*args)
                root = SimpleNamespace(tk=SimpleNamespace(call=call))
                self.assertEqual(secondary_click_sequence(root), expected)
