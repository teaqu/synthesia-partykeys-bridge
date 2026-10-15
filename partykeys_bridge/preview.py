"""Independent monotonic transport and event-driven LED preview scheduler."""
from bisect import bisect_right
import math
import time


class Transport:
    def __init__(self, duration, speed=1.0, now=time.monotonic, position=0.0):
        self.now, self.duration = now, duration
        self.anchor, self.wall = position, now()
        self.playing = False
        self.speed = 1.0
        self.set_speed(speed)

    def position(self):
        elapsed = (self.now() - self.wall) * self.speed if self.playing else 0
        return min(self.duration, self.anchor + elapsed)

    def pause(self):
        self.anchor = self.position()
        self.wall = self.now()
        self.playing = False

    def play(self):
        if not self.playing:
            self.wall = self.now()
            self.playing = True

    def seek(self, seconds):
        if not math.isfinite(seconds) or not 0 <= seconds <= self.duration:
            raise ValueError(f"Seek must be 0..{self.duration:.3f} seconds")
        self.anchor, self.wall = seconds, self.now()

    def restart(self, lookahead):
        self.anchor, self.wall = -lookahead * self.speed, self.now()

    def set_speed(self, speed):
        if not math.isfinite(speed) or not 0.05 <= speed <= 4:
            raise ValueError("Speed must be 0.05..4")
        self.anchor, self.wall = self.position(), self.now()
        self.speed = speed


class Preview:
    def __init__(self, song, lookahead=2.0, near=1.0, dim=0.2,
                 left=(0, 80, 255), right=(255, 100, 0), active=(0, 255, 0),
                 left_tracks=(), right_tracks=(), left_channels=(1,), right_channels=(0,)):
        if not math.isfinite(lookahead) or not 0 <= lookahead <= 5:
            raise ValueError("Lookahead must be 0..5 seconds")
        if not math.isfinite(near) or not 0 <= near <= lookahead:
            raise ValueError("Near stage must be 0..lookahead seconds")
        if not math.isfinite(dim) or not 0 <= dim <= 1:
            raise ValueError("Dim level must be 0..1")
        if set(left_tracks) & set(right_tracks) or set(left_channels) & set(right_channels):
            raise ValueError("Left/right track or channel selections must not overlap")
        self.song, self.lookahead, self.near, self.dim = song, lookahead, near, dim
        self.left, self.right, self.active_color = left, right, active
        self.left_tracks, self.right_tracks = set(left_tracks), set(right_tracks)
        self.left_channels, self.right_channels = set(left_channels), set(right_channels)
        self.speed, self.last = None, float("-inf")
        self.states = {}

    def hand_color(self, note):
        if note.track in self.left_tracks:
            return self.left
        if note.track in self.right_tracks:
            return self.right
        return self.left if note.channel in self.left_channels else self.right

    def rebuild(self, speed):
        self.speed = speed
        self.transitions = []
        for index, note in enumerate(self.song.notes):
            if note.end <= note.start:
                continue
            for moment, stage in ((note.start - self.lookahead * speed, 1),
                                  (note.start - self.near * speed, 2),
                                  (note.start, 3), (note.end, 4)):
                self.transitions.append((moment, index, stage))
        self.transitions.sort()
        self.times = [e[0] for e in self.transitions]
        self.cursor, self.last, self.states = 0, float("-inf"), {}

    def lights(self, position, speed=1.0):
        if speed != self.speed or position < self.last:
            self.rebuild(speed)
        stop = bisect_right(self.times, position)
        for _, index, stage in self.transitions[self.cursor:stop]:
            if stage == 4:
                self.states.pop(index, None)
            else:
                self.states[index] = stage
        self.cursor, self.last = stop, position
        lights, priority = {}, {}
        for index, stage in self.states.items():
            note = self.song.notes[index]
            rank = (stage, -note.start)
            color = self.active_color if stage == 3 else self.hand_color(note)
            if stage == 1:
                color = tuple(round(v * self.dim) for v in color)
            if rank > priority.get(note.pitch, (-1, 0)):
                lights[note.pitch], priority[note.pitch] = color, rank
        return lights


class AudioPlayback:
    """Optional channel-message playback; file SysEx is intentionally not sent."""
    def __init__(self, song, port):
        self.song, self.port = song, port
        self.times = [event[0] for event in song.events]
        self.cursor = 0

    def silence(self):
        self.port.panic()
        self.port.reset()

    def seek(self, position):
        self.silence()
        self.cursor = bisect_right(self.times, position)
        # Restore the latest controller/program/pitch state in original order.
        latest = {}
        for order, (_, message) in enumerate(self.song.events[:self.cursor]):
            if message.type in ("control_change", "program_change", "pitchwheel", "aftertouch"):
                key = (message.channel, message.type, getattr(message, "control", None))
                latest[key] = (order, message)
        for _, message in sorted(latest.values(), key=lambda item: item[0]):
            self.port.send(message)
        # Resume physically held notes. Sustain-only tails are not reconstructed.
        import mido
        for note in self.song.notes:
            if note.start <= position < note.end:
                self.port.send(mido.Message("note_on", note=note.pitch,
                                            channel=note.channel, velocity=note.velocity))

    def advance(self, position):
        stop = bisect_right(self.times, position)
        for _, message in self.song.events[self.cursor:stop]:
            self.port.send(message)
        self.cursor = stop
