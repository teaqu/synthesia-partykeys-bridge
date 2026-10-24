"""Synthesia finger-based key-light messages, not ordinary performance MIDI."""
from .practice import practice_lights
from .practice import ExactRGB
import time
LEFT_CHANNELS = {1, 2, 3, 4, 5, 11}  # Mido uses zero-based channels.
RIGHT_CHANNELS = {0, 6, 7, 8, 9, 10, 12}


class Bridge:
    def __init__(self, left=(255, 255, 0), right=(255, 255, 0),
                 left_channels=None, right_channels=None, now=None):
        self.left, self.right = left, right
        self.left_channels = LEFT_CHANNELS if left_channels is None else set(left_channels)
        self.right_channels = RIGHT_CHANNELS if right_channels is None else set(right_channels)
        if self.left_channels & self.right_channels:
            raise ValueError("Left and right channels must not overlap")
        self.active = {}
        self.counts = {}
        self.clear_generation = 0
        self.practice = {"enabled": False, "notes": [], "color": "0050FF", "brightness": 0.08}
        self.now = now or time.monotonic
        self.trail = {"enabled": False, "duration_seconds": 1.5, "max_brightness": .35}
        self.released = {}

    def set_trail(self, config):
        self.trail = {"max_brightness": .35, **config}
        if not config["enabled"]:
            self.released.clear()

    def set_practice(self, config):
        self.practice = dict(config)

    def set_colors(self, left, right):
        self.left, self.right = left, right
        self.active = {key: left if key[0] in self.left_channels else right for key in self.active}

    def receive(self, message):
        before = self._cue_lights()
        self._receive(message)
        after = self._cue_lights()
        if (message.type in ("reset", "stop") or
                (message.type == "control_change" and message.control in (16, 120, 123))):
            self.released.clear()
            return
        if self.trail["enabled"]:
            for note in before.keys() - after.keys():
                self.released[note] = (self.now(), before[note])
        for note in after:
            self.released.pop(note, None)

    def _receive(self, message):
        if message.type in ("reset", "stop"):
            self.clear_generation += 1
            self.active.clear()
            self.counts.clear()
        elif message.type == "control_change":
            if message.channel == 0 and message.control == 16:
                self.clear_generation += 1
                self.active.clear()  # Synthesia play/hand-selection state change.
                self.counts.clear()
            elif message.control in (120, 123):
                self.clear_generation += 1
                self.active = {k: v for k, v in self.active.items() if k[0] != message.channel}
                self.counts = {k: v for k, v in self.counts.items() if k[0] != message.channel}
        elif message.type in ("note_on", "note_off"):
            if message.channel == 0 and (message.type == "note_off" or message.velocity == 0):
                # Observed Synthesia finger-light stream: onset on the finger
                # channel, release on channel 1 (Mido 0), for that pitch across
                # all finger channels. A missed/unused cue must still go dark.
                self.active = {k: v for k, v in self.active.items() if k[1] != message.note}
                self.counts = {k: v for k, v in self.counts.items() if k[1] != message.note}
                return
            if message.channel not in self.left_channels | self.right_channels:
                return
            key = (message.channel, message.note)
            if message.type == "note_on" and message.velocity:
                # MIDI overlapping onsets require matching releases. RGB output
                # still deduplicates identical illumination in the driver.
                self.counts[key] = self.counts.get(key, 0) + 1
                self.active[key] = self.left if message.channel in self.left_channels else self.right
            else:
                remaining = self.counts.get(key, 0) - 1
                if remaining > 0:
                    self.counts[key] = remaining
                else:
                    self.counts.pop(key, None)
                    self.active.pop(key, None)

    def _cue_lights(self):
        # Component-wise blend when both hands request the same physical key.
        cues = {}
        for (_, note), rgb in self.active.items():
            cues[note] = tuple(max(a, b) for a, b in zip(cues.get(note, (0, 0, 0)), rgb))
        return cues

    def lights(self):
        lights = practice_lights(self.practice)
        now = self.now()
        for note, (released_at, color) in list(self.released.items()):
            duration = self.trail["duration_seconds"]
            age = max(0., now - released_at)
            remaining = 1 - age / duration
            if remaining <= 0:
                self.released.pop(note, None)
                continue
            # Start dim immediately, then visibly halve in at most 120 ms.
            # Duration controls the remaining tail, never a full-bright hold.
            initial_fade = 1 - .5 * min(age / min(.12, duration / 4), 1.)
            gain = self.trail["max_brightness"] * initial_fade * remaining
            faded = tuple(round(v * gain) for v in color)
            if not any(faded):
                continue
            lights[note] = ExactRGB(max(a, b) for a, b in zip(lights.get(note, (0, 0, 0)), faded))
        lights.update(self._cue_lights())
        return lights
