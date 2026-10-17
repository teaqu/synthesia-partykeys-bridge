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


def octave_report(message):
    """Decode a keyboard CMD18 report, never a song's MIDI/transposition data."""
    if message.type != "sysex":
        return None
    data = message.data
    if (len(data) < 8 or data[0] not in (5, 0x7f) or data[1] != 0x30
            or data[2:4] not in ((0x20, 0), (0x7e, 0x7e))
            or not 1 <= data[4] <= 15 or not 0 <= data[5] <= 0x6f
            or data[6] != 0x18):
        return None
    octave = data[7] - 128 if data[7] >= 64 else data[7]
    return (data[4], octave) if -3 <= octave <= 3 else None


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


def rgb_messages(colors, channel=0x7f, address=0x7f):
    """One key per RGB frame: the format verified on this keyboard."""
    for key, rgb in sorted(colors.items()):
        if not isinstance(key, int) or not 0 <= key < KEY_COUNT:
            raise ValueError("PartyKeys key index must be 0..35")
        if len(rgb) != 3 or any(not isinstance(v, int) or not 0 <= v <= 255 for v in rgb):
            raise ValueError("RGB requires three integers in 0..255")
        encoded = tuple(part for value in rgb for part in divmod(value, 128))
        yield mido.Message("sysex", data=(5, 0x30, channel, address, 0x20, 0, 0x15, 1, *encoded, 1, key))


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
        ordered = sorted(changes, key=lambda key: (self._priority(changes[key]), key))
        if not force:
            if self.now() < self.last_send + self.min_interval:
                return
            ordered = self._batch_keys(ordered, changes)
        self._write_changes({key: changes[key] for key in ordered})

    def _priority(self, color):
        return 0 if color == BLACK else 1

    def _batch_keys(self, ordered, changes):
        return ordered[:self.BATCH_SIZE]

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

    def __init__(self, *args, boards=(), initial_octave=0, **kwargs):
        super().__init__(*args, **kwargs)
        if not isinstance(initial_octave, int) or not -3 <= initial_octave <= 3:
            raise ValueError("Initial octave must be -3..3")
        self.routes = {}
        self.boards = tuple(boards)
        self.octaves = {channel: initial_octave for channel, _, _ in self.boards}
        destinations = set()
        for channel, address, lowest in self.boards:
            if not (1 <= channel <= 15 and 0 <= address <= 0x6f and 0 <= lowest <= 92):
                raise ValueError("Board requires channel 1..15, address 0..111, and lowest note 0..92")
            destination = (channel, address)
            if destination in destinations:
                raise ValueError("Each board destination must be unique")
            destinations.add(destination)
            for note in range(lowest, lowest + KEY_COUNT):
                if note in self.routes:
                    raise ValueError("Board note ranges must not overlap")
                self.routes[note] = destination
            LOG.info("Board channel %d address %#x: MIDI %d–%d", channel, address, lowest, lowest + 35)
        self.routes = self._shifted_routes(self.octaves)
        self.pending_modes = []
        self.full_clear = None

    def replace_boards(self, boards, octaves=None, reinitialize=False):
        """Validate new maps before swapping; never send using removed routes."""
        candidate = PalettePartyKeys(self.port, self.brightness, self.base_note,
                                     min_interval=self.min_interval, now=self.now, sleep=self.sleep,
                                     boards=boards)
        for channel, octave in (self.octaves if octaves is None else octaves).items():
            if channel in candidate.octaves:
                candidate.set_octave(channel, octave)
        # Keep only stable known-dark physical keys. Reassert lit targets and
        # explicitly clear unknown keys on newly attached/reinitialized boards.
        old_destinations = {(ch,addr) for ch,addr,_ in self.boards}
        destinations = {(ch,addr) for ch,addr,_ in candidate.boards}
        self.pending_modes = sorted(destinations if reinitialize else destinations - old_destinations)
        self.state = {key: value for key,value in self.state.items()
                      if value == BLACK and self.base_note + key in candidate.routes
                      and candidate.routes[self.base_note + key] not in self.pending_modes
                      and candidate.routes[self.base_note + key] == self.routes.get(self.base_note + key)
                      and candidate.octaves.get(candidate.routes[self.base_note + key][0])
                          == self.octaves.get(candidate.routes[self.base_note + key][0])}
        self.boards, self.routes, self.octaves = candidate.boards, candidate.routes, candidate.octaves
        self.full_clear = [note - self.base_note for note in self.note_range]

    def request_full_clear(self):
        if self.full_clear is None:
            self.full_clear = [note - self.base_note for note in self.note_range]
            LOG.info("Clearing every mapped key before restoring current lights (%d keys)", len(self.full_clear))

    def _clear_batch(self):
        keys = self._batch_keys(self.full_clear, {key:BLACK for key in self.full_clear})
        self._write_changes({key:BLACK for key in keys})
        done = {key for key in keys if self.state.get(key) == BLACK}
        self.full_clear = [key for key in self.full_clear if key not in done]
        if not self.full_clear:
            self.full_clear = None
            LOG.info("Full mapped-key clear complete")

    def clear(self):
        if self.initializing and self.now() < self.ready_at:
            self.sleep(self.ready_at - self.now())
        while self.full_clear is not None:
            self._clear_batch()
        super().clear()

    def show(self, notes, force=False):
        if self.pending_modes:
            if self.now() < self.last_send + self.min_interval:
                return
            channel, address = self.pending_modes.pop(0)
            self._send(mido.Message("sysex", data=(5,0x30,channel,address,0x20,0,0x0f,1)))
            self.initializing, self.ready_at = True, self.last_send + 1.0
            return
        if self.full_clear is not None:
            if self.initializing:
                self._initialize_tick()
                if not force:
                    return
                if self.now() < self.ready_at:
                    self.sleep(self.ready_at - self.now())
                self.initializing = False
            if not force and self.now() < self.last_send + self.min_interval:
                return
            if force:
                while self.full_clear is not None:
                    self._clear_batch()
            else:
                self._clear_batch()
            return
        super().show(notes, force)

    def _shifted_routes(self, octaves):
        routes = {}
        for channel, address, lowest in self.boards:
            low = lowest + 12 * octaves[channel]
            for note in range(max(0, low), min(128, low + KEY_COUNT)):
                if note in routes:
                    raise ValueError("Shifted board note ranges overlap")
                routes[note] = (channel, address)
        return routes

    def set_octave(self, channel, octave):
        if channel not in self.octaves or self.octaves[channel] == octave:
            return False
        if not isinstance(octave, int) or not -3 <= octave <= 3:
            raise ValueError("Octave must be -3..3")
        updated = dict(self.octaves)
        updated[channel] = octave
        try:
            routes = self._shifted_routes(updated)
        except ValueError:
            LOG.warning("Ignoring octave report: shifted board ranges overlap")
            return False
        # Firmware can alter its LEDs when shifting. Repaint affected physical
        # keys at their new pitches, including explicit offs for inactive keys.
        self.state = {key: rgb for key, rgb in self.state.items()
                      if self.routes.get(self.base_note + key, (None,))[0] != channel
                      and routes.get(self.base_note + key) == self.routes.get(self.base_note + key)}
        self.octaves, self.routes = updated, routes
        LOG.info("Keyboard octave changed: channel %d, %+d; MIDI ranges %s", channel, octave,
                 [(base + 12 * updated[ch], base + 12 * updated[ch] + 35)
                  for ch, _, base in self.boards])
        return True

    @property
    def note_range(self):
        # Explicit destinations prevent broadcast mirroring on joined boards.
        return tuple(sorted(self.routes)) if self.boards else range(128)

    def _write_changes(self, changes):
        # Keep each packet on one board, including release/cleanup packets.
        # Preserve the caller's off-before-on order across board boundaries.
        batch = {}
        destination = None
        for key, rgb in changes.items():
            route = self.routes.get(self.base_note + key, (0x7f, 0x7f))
            if batch and (route != destination or len(batch) == self.BATCH_SIZE):
                super()._write_changes(batch)
                batch = {}
            destination = route
            batch[key] = rgb
        if batch:
            super()._write_changes(batch)

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
        destinations = {self.routes.get(self.base_note + key, (0x7f, 0x7f)) for key in changes}
        if len(destinations) != 1:
            raise ValueError("Palette packet must target exactly one board")
        channel, address = destinations.pop()
        for key, rgb in changes.items():
            slot = 0 if rgb == BLACK else next(slot for slot, color in PALETTE.items() if color == rgb)
            pairs.extend((self.base_note + key, slot))
        return mido.Message("sysex", data=(5, 0x30, channel, address, 0x20, 0, 0x71, len(changes), *pairs))

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
    boards = getattr(args, "board", ())
    if boards and cls is not PalettePartyKeys:
        raise ValueError("--board requires --protocol palette71")
    interval = getattr(args, "palette_interval_ms", 250) / 1000 if cls is PalettePartyKeys else cls.DEFAULT_INTERVAL
    options = {"boards": boards, "initial_octave": getattr(args, "initial_octave", 0)} if cls is PalettePartyKeys else {}
    return cls(port, args.brightness, args.base_note, min_interval=interval, **options, **clock)


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
