"""MIDI type 0/1 parsing with a global tempo map and preserved track identity."""
from collections import defaultdict, deque
from dataclasses import dataclass
import mido


@dataclass(frozen=True)
class Note:
    start: float
    end: float
    pitch: int
    channel: int
    track: int
    velocity: int


@dataclass
class Song:
    notes: list
    events: list
    duration: float
    tracks: list

    @classmethod
    def load(cls, path):
        midi = mido.MidiFile(path)
        if midi.type == 2 or midi.ticks_per_beat <= 0:
            raise ValueError("Use a type 0 or 1 MIDI file with PPQN timing (type 2/SMPTE unsupported)")
        raw, names = [], []
        for track_id, track in enumerate(midi.tracks):
            tick = 0
            names.append(track.name or f"Track {track_id}")
            for sequence, message in enumerate(track):
                tick += message.time
                raw.append((tick, track_id, sequence, message))
        raw.sort(key=lambda e: e[:3])
        tick, seconds, tempo = 0, 0.0, 500000
        held, notes, events = defaultdict(deque), [], []
        for absolute, track, _, message in raw:
            seconds += mido.tick2second(absolute - tick, midi.ticks_per_beat, tempo)
            tick = absolute
            if message.type == "set_tempo":
                if message.tempo <= 0:
                    raise ValueError("MIDI tempo must be positive")
                tempo = message.tempo
            if not message.is_meta and hasattr(message, "channel"):
                events.append((seconds, message.copy(time=0)))
            if message.type not in ("note_on", "note_off"):
                continue
            key = (track, message.channel, message.note)
            if message.type == "note_on" and message.velocity:
                held[key].append((seconds, message.velocity))
            elif held[key]:
                start, velocity = held[key].popleft()  # FIFO for repeated pitches.
                notes.append(Note(start, seconds, message.note, message.channel, track, velocity))
        for (track, channel, pitch), pending in held.items():
            for start, velocity in pending:
                notes.append(Note(start, seconds, pitch, channel, track, velocity))
        notes.sort(key=lambda n: (n.start, n.track, n.pitch))
        return cls(notes, events, seconds, names)
