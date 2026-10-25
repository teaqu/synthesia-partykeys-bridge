"""Persistent practice targets, independent of Synthesia's transient cue state."""
from copy import deepcopy
import threading


class LiveLighting:
    """Immutable snapshots pass GUI edits directly to the MIDI worker."""
    def __init__(self):
        self.lock = threading.Lock()
        self.generation = 0
        self.value = None

    def publish(self, practice, trail, erase=False):
        with self.lock:
            self.generation += 1
            self.value = (deepcopy(practice), dict(trail), erase)

    def snapshot(self):
        with self.lock:
            return self.generation, self.value


def paint_practice(config, note, erase=False):
    config = deepcopy(config)
    notes = set(config["notes"])
    colors = config.setdefault("note_colors", {})
    if erase:
        notes.discard(note)
        colors.pop(str(note), None)
    else:
        notes.add(note)
        colors[str(note)] = config["color"]
        config["enabled"] = True
    config["notes"] = sorted(notes)
    return config


class ExactRGB(tuple):
    """A colour that must bypass the firmware's fixed-brightness palette."""


def practice_lights(config):
    if not config["enabled"] or not config["brightness"]:
        return {}
    lights = {}
    for note in config["notes"]:
        value = config.get("note_colors", {}).get(str(note), config["color"]).removeprefix("#")
        color = ExactRGB(round(int(value[i:i + 2], 16) * config["brightness"])
                         for i in (0, 2, 4))
        if any(color):
            lights[note] = color
    return lights


def capture_key(args, message):
    capture = getattr(args, "practice_capture", None)
    if (capture is not None and capture.is_set() and message.type == "note_on"
            and message.velocity > 0):
        args.practice_key_events.put(message.note)
        return message.note
    return None


def physical_key_event(args, message=None):
    """Notify the GUI of physical key state independently of paint capture.

    None means the input disconnected. Only the GUI consumes this stream;
    it must never change practice colours or send MIDI output.
    """
    events = getattr(args, "physical_key_events", None)
    if events is not None and (message is None or message.type in (
            "note_on", "note_off", "reset", "stop") or
            (message.type == "control_change" and message.control in (120, 123))):
        events.put(message.copy() if message is not None else None)


def note_name(note):
    return f'{("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")[note % 12]}{note // 12 - 1}'
