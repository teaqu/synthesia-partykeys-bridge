import unittest
from types import SimpleNamespace

import mido

from partykeys_bridge.bridge import Bridge
from partykeys_bridge.driver import BLACK, PalettePartyKeys


class CleanupTests(unittest.TestCase):
    def test_sweep_clears_cached_black_keys_and_restores_only_latest_cue(self):
        clock, sent = [0.0], []
        driver = PalettePartyKeys(
            SimpleNamespace(send=sent.append), boards=[(1, 0x3f, 48)],
            min_interval=.01, now=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
        driver.initialize()
        sent.clear()
        self.assertTrue(all(value == BLACK for value in driver.state.values()))
        driver.request_full_clear()
        driver.show({60: (0, 80, 255)})
        remaining = len(driver.full_clear)
        driver.request_full_clear()
        self.assertEqual(len(driver.full_clear), remaining)
        while driver.full_clear is not None:
            clock[0] += .02
            driver.show({64: (0, 255, 0)})
        cleared = [note for message in sent
                   for note, slot in zip(message.data[8::2], message.data[9::2])
                   if slot == 0]
        self.assertEqual(sorted(cleared), list(range(48, 84)))
        clock[0] += .02
        driver.show({64: (0, 255, 0)})
        self.assertEqual(sent[-1].data[-4:], (0x71, 1, 64, 7))
        self.assertEqual(driver.state[12], BLACK)

    def test_song_reset_and_stop_clear_live_state_and_request_sweep(self):
        for message in (mido.Message('stop'), mido.Message('reset'),
                        mido.Message('control_change', channel=0, control=16),
                        mido.Message('control_change', channel=0, control=123)):
            with self.subTest(message=message):
                bridge = Bridge()
                bridge.receive(mido.Message('note_on', channel=0, note=60))
                bridge.receive(message)
                self.assertEqual(bridge.lights(), {})
                self.assertEqual(bridge.clear_generation, 1)
