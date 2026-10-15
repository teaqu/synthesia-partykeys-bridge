"""PartyKeys 36 CMD 0x15, from allen4z/PartykeysProtocol/LED_PROTOCOL.md.

Mido supplies F0/F7 framing. All payload values must be seven-bit safe.
"""
import logging
import math
import time

import mido

HEADER = (0x05, 0x30, 0x7F, 0x7F, 0x20, 0x00)
BLACK = (0, 0, 0)
KEY_COUNT = 36
LOG = logging.getLogger("partykeys")


class MidiInput:
    """Poll RtMidi directly, avoiding callback-thread teardown on macOS.

    The default Mido RtMidi input installs a Python callback even when the
    caller polls it. A virtual-port integration test reproduced a native
    close_port hang in that path. RtMidi's own queue needs no Python callback.
    """
    def __init__(self, name, virtual=False):
        import rtmidi
        self.name = name
        self.rt = rtmidi.MidiIn()
        try:
            self.rt.ignore_types(sysex=False, timing=False, active_sense=True)
            if virtual:
                self.rt.open_virtual_port(name)
            else:
                names = self.rt.get_ports()
                if name not in names:
                    raise OSError(f"MIDI input disappeared: {name}")
                self.rt.open_port(names.index(name))
        except BaseException:
            self.rt.delete()
            raise

    def iter_pending(self):
        while True:
            event = self.rt.get_message()
            if event is None:
                break
            data, _delta = event
            yield mido.Message.from_bytes(data)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        try:
            self.rt.close_port()
        finally:
            self.rt.delete()


def rgb_messages(colors):
    """One key per RGB frame: the format verified on this keyboard."""
    for key, rgb in sorted(colors.items()):
        if not isinstance(key, int) or not 0 <= key < KEY_COUNT:
            raise ValueError("PartyKeys key index must be 0..35")
        if len(rgb) != 3 or any(not isinstance(v, int) or not 0 <= v <= 255 for v in rgb):
            raise ValueError("RGB requires three integers in 0..255")
        encoded = tuple(part for value in rgb for part in divmod(value, 128))
        yield mido.Message("sysex", data=(*HEADER, 0x15, 1, *encoded, 1, key))


class PartyKeys:
    MIN_INTERVAL = 0.25  # RGB15: faster writes failed on the user's unit.
    DEFAULT_INTERVAL = 0.25
    BATCH_SIZE = 1

    def __init__(self, port, brightness=1.0, base_note=48, min_interval=None,
                 now=None, sleep=None):
        if not math.isfinite(brightness) or not 0 <= brightness <= 1:
            raise ValueError("Brightness must be 0..1")
        if not 0 <= base_note <= 92:
            raise ValueError("Base note must be 0..92")
        if min_interval is None:
            min_interval = self.DEFAULT_INTERVAL
        if not math.isfinite(min_interval) or min_interval < 0:
            raise ValueError("Message interval must be finite and nonnegative")
        self.port, self.brightness, self.base_note = port, brightness, base_note
        self.state = {}
        # Apply the protocol-specific floor to every write, including cleanup.
        self.min_interval = max(self.MIN_INTERVAL, min_interval)
        self.now, self.sleep = now or time.monotonic, sleep or time.sleep
        self.last_send = float("-inf")
        self.initializing = False
        self.ready_at = 0

    @property
    def note_range(self):
        """Notes addressable by this protocol, before converting to key indices."""
        return range(self.base_note, self.base_note + KEY_COUNT)

    def _send(self, message):
        deadline = self.last_send + self.min_interval
        while self.now() < deadline:
            self.sleep(deadline - self.now())
        self.port.send(message)
        self.last_send = self.now()

    def _key_message(self, key, rgb):
        return next(rgb_messages({key: rgb}))

    def _batch_message(self, changes):
        key, rgb = next(iter(changes.items()))
        return self._key_message(key, rgb)

    def _write_changes(self, changes):
        keys = list(changes)
        for start in range(0, len(keys), self.BATCH_SIZE):
            batch = {key: changes[key] for key in keys[start:start + self.BATCH_SIZE]}
            self._send(self._batch_message(batch))
            self.state.update(batch)  # Commit only successfully written keys.
            LOG.debug("LED changes: %s", {self.base_note + key: rgb for key, rgb in batch.items()})

    def _color(self, rgb):
        return tuple(round(v * self.brightness) for v in rgb)

    def initialize(self, blocking=True):
        self._send(mido.Message("sysex", data=(*HEADER, 0x0F, 1)))
        self.state.clear()
        self.initializing = True
        self.ready_at = self.last_send + 1.0
        if blocking:
            self.sleep(1.0)
            self.clear()

    def _initialize_tick(self):
        if self.now() < self.ready_at or self.now() < self.last_send + self.min_interval:
            return
        for key in range(KEY_COUNT):
            if key not in self.state:
                self._send(self._key_message(key, BLACK))
                self.state[key] = BLACK
                break
        self.initializing = len(self.state) < KEY_COUNT

    def clear(self):
        # Unknown keys need explicit clearing; known dark keys need no traffic.
        # A full initial clear takes about nine seconds, never one bulk frame.
        if self.initializing and self.now() < self.ready_at:
            self.sleep(self.ready_at - self.now())
        self._write_changes({key: BLACK for key in (n - self.base_note for n in self.note_range)
                             if self.state.get(key) != BLACK})
        self.initializing = False

    def show(self, notes, force=False):
        """Send one latest batch per tick; force drains with the same pacing."""
        if self.initializing:
            if force:
                self.clear()
            else:
                self._initialize_tick()
                return
        desired = {note - self.base_note: BLACK for note in self.note_range}
        for note, rgb in notes.items():
            if note in self.note_range:
                desired[note - self.base_note] = self._color(rgb)
        changes = {key: rgb for key, rgb in desired.items() if self.state.get(key) != rgb}
        # Turn stale lights off before illuminating new cues. No queued old
        # chord can survive a new desired state on the following tick.
        ordered = sorted(changes, key=lambda key: (changes[key] != BLACK, key))
        if not force:
            if self.now() < self.last_send + self.min_interval:
                return
            ordered = ordered[:self.BATCH_SIZE]
        self._write_changes({key: changes[key] for key in ordered})

    def invalidate_note(self, note):
        # Physical press/release can change the local LED effect.
        if note in self.note_range:
            self.state.pop(note - self.base_note, None)


# Palette slots documented by luoshichuan-ai's bridge. RGB values below are
# color-selection references, not claims about the physical LED calibration.
PALETTE = {1: (255, 0, 0), 3: (255, 100, 0), 5: (255, 255, 0),
           7: (0, 255, 0), 8: (0, 255, 180), 9: (0, 80, 255),
           12: (180, 0, 255)}


class PalettePartyKeys(PartyKeys):
    """Short CMD71 messages, explicit per-note off, init-only connection.

    Based on the published bridge, with slower pacing required by this unit.
    Never emit CMD71 with a zero note count (the global all-off command).
    """
    MIN_INTERVAL = 0.01  # Explicit fast option; retain 250 ms default for older firmware.
    BATCH_SIZE = 3  # 16-byte maximum; 40 frames passed the v8.3 USB-error test.

    @property
    def note_range(self):
        # CMD71 addresses MIDI pitches, unlike RGB15's physical key indices.
        # Let the keyboard route pitches to its connected boards/octave range.
        return range(128)

    def _color(self, rgb):
        if not any(rgb):
            return BLACK
        peak = max(rgb)
        normalized = tuple(v * 255 / peak for v in rgb)
        return min(PALETTE.values(), key=lambda c: sum((a-b)**2 for a,b in zip(c, normalized)))

    def _key_message(self, key, rgb):
        return self._batch_message({key: rgb})

    def _batch_message(self, changes):
        pairs = []
        for key, rgb in changes.items():
            slot = 0 if rgb == BLACK else next(slot for slot, color in PALETTE.items() if color == rgb)
            pairs.extend((self.base_note + key, slot))
        return mido.Message("sysex", data=(*HEADER, 0x71, len(changes), *pairs))

    def initialize(self, blocking=True):
        # Match the referenced implementation's executable code (0F01), not
        # its conflicting 0F05 header comment. No full-board clear on connect.
        self._send(mido.Message("sysex", data=(*HEADER, 0x0F, 1)))
        self.state = {note - self.base_note: BLACK for note in self.note_range}
        self.ready_at = self.last_send + 1.0
        self.initializing = True
        if blocking:
            self.sleep(1.0)
            self.initializing = False

    def _initialize_tick(self):
        if self.now() >= self.ready_at:
            self.initializing = False


def make_driver(port, args, **clock):
    cls = PalettePartyKeys if getattr(args, "protocol", "rgb15") == "palette71" else PartyKeys
    interval = getattr(args, "palette_interval_ms", 250) / 1000 if cls is PalettePartyKeys else cls.DEFAULT_INTERVAL
    return cls(port, args.brightness, args.base_note, min_interval=interval, **clock)


def select_port(names, requested, hint):
    if requested in names:
        return requested
    if requested:
        matches = [n for n in names if requested.casefold() in n.casefold()]
    else:
        matches = [n for n in names if hint.casefold() in n.casefold()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"No matching MIDI port ({requested or hint!r}). Run 'partykeys ports' and specify its name.")
    raise ValueError(f"Ambiguous MIDI port: {matches}. Specify an exact name.")
