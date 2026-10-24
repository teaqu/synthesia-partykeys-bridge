"""Validated JSON settings with background reloading of colours and brightness."""
from copy import deepcopy
import json
import logging
import math
from pathlib import Path
import queue
import sys
import threading

LOG = logging.getLogger("partykeys")
DEFAULTS = {
    "version": 1, "live_reload": True, "poll_interval_ms": 500,
    "palette": {"blue": "0050FF", "green": "00FF00", "red": "FF0000", "orange": "FF6400",
                "yellow": "FFFF00", "purple": "B400FF", "cyan": "00FFB4"},
    "lighting": {"left_color": "blue", "right_color": "green",
                 "rgb_brightness": 1.0},
    "midi": {"create_virtual_input": True, "input": "Synthesia Lights",
             "palette_interval_ms": 1},
    "practice": {"enabled": False, "notes": [], "color": "0050FF", "brightness": 0.08, "note_colors": {}},
    "trail": {"enabled": False, "duration_seconds": 1.5, "max_brightness": .35},
}


def rgb(value):
    if not isinstance(value, str) or len(value.removeprefix("#")) != 6:
        raise ValueError("Colours must be six-digit RGB strings such as 0050FF")
    value = value.removeprefix("#")
    try:
        return tuple(int(value[i:i+2], 16) for i in (0, 2, 4))
    except ValueError:
        raise ValueError("Colours must be six-digit RGB strings such as 0050FF") from None


def resolve_color(value, palette):
    return rgb(palette.get(value, value)) if isinstance(value, str) else rgb(value)


def default_path():
    root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    return root / "config.json"


def load(path, required=False):
    config = deepcopy(DEFAULTS)
    if path and Path(path).is_file():
        try:
            incoming = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            raise ValueError(f"Could not read config.json: {error}") from error
        if not isinstance(incoming, dict) or set(incoming) - set(config):
            raise ValueError("Invalid or unknown config section")
        for section, value in incoming.items():
            if section in ("version", "live_reload", "poll_interval_ms"):
                config[section] = value
            elif section == "palette":
                if not isinstance(value, dict):
                    raise ValueError("palette must map names to six-digit RGB strings")
                config[section].update(value)
            else:
                if not isinstance(value, dict) or set(value) - set(config[section]):
                    raise ValueError(f"Invalid or unknown settings in {section}")
                config[section].update(value)
    elif required:
        raise ValueError(f"Config file not found: {path}")
    if type(config["version"]) is not int or config["version"] != 1:
        raise ValueError("Unsupported config version")
    for section, key in ((None, "live_reload"),
                         ("midi", "create_virtual_input")):
        if type((config if section is None else config[section])[key]) is not bool:
            raise ValueError(f"{key} must be true or false")
    poll = config["poll_interval_ms"]
    if type(poll) is not int or not 100 <= poll <= 10000:
        raise ValueError("poll_interval_ms must be 100..10000")
    for name, value in config["palette"].items():
        if not isinstance(name, str) or not name.strip() or len(name) > 40:
            raise ValueError("Palette names must be nonempty strings of at most 40 characters")
        rgb(value)
    for key in ("left_color", "right_color"):
        resolve_color(config["lighting"][key], config["palette"])
    brightness = config["lighting"]["rgb_brightness"]
    if type(brightness) not in (int, float) or not math.isfinite(brightness) or not 0 <= brightness <= 1:
        raise ValueError("lighting.rgb_brightness must be between 0 and 1")
    practice = config["practice"]
    if type(practice["enabled"]) is not bool:
        raise ValueError("practice.enabled must be true or false")
    notes = practice["notes"]
    if not isinstance(notes, list) or any(type(n) is not int or not 0 <= n <= 127 for n in notes):
        raise ValueError("practice.notes must be a list of MIDI notes from 0 to 127")
    rgb(practice["color"])
    colors = practice["note_colors"]
    if not isinstance(colors, dict):
        raise ValueError("practice.note_colors must map MIDI note numbers to RGB colours")
    for note, value in colors.items():
        if not isinstance(note, str) or note not in {str(n) for n in range(128)}:
            raise ValueError("practice.note_colors keys must be MIDI note numbers from 0 to 127")
        rgb(value)
    trail = config["trail"]
    duration = trail["duration_seconds"]
    if type(trail["enabled"]) is not bool:
        raise ValueError("trail.enabled must be true or false")
    if type(duration) not in (int, float) or not math.isfinite(duration) or not .25 <= duration <= 30:
        raise ValueError("trail.duration_seconds must be between 0.25 and 30")
    maximum = trail["max_brightness"]
    if type(maximum) not in (int, float) or not math.isfinite(maximum) or not 0 <= maximum <= 1:
        raise ValueError("trail.max_brightness must be between 0 and 1")
    brightness = practice["brightness"]
    if type(brightness) not in (int, float) or not math.isfinite(brightness) or not 0 <= brightness <= 1:
        raise ValueError("practice.brightness must be between 0 and 1")
    if not isinstance(config["midi"]["input"], str) or not config["midi"]["input"].strip():
        raise ValueError("midi.input must be a nonempty MIDI destination name")
    if type(config["midi"]["palette_interval_ms"]) is not int or config["midi"]["palette_interval_ms"] not in (1,10,20,40,100,250):
        raise ValueError("midi.palette_interval_ms must be 1, 10, 20, 40, 100, or 250")
    return config


def explicit(argv, option):
    return any(value == option or value.startswith(option + "=") for value in argv)


def apply_startup(args, config, argv):
    mappings = (("--virtual-input", "virtual_input", config["midi"]["create_virtual_input"]),
                ("--input", "input", config["midi"]["input"]),
                ("--palette-interval-ms", "palette_interval_ms", config["midi"]["palette_interval_ms"]),
                ("--brightness", "brightness", config["lighting"]["rgb_brightness"]))
    for option, name, value in mappings:
        if not explicit(argv, option):
            setattr(args, name, value)
    # An explicitly selected existing bus retains its traditional CLI meaning.
    if explicit(argv, "--input") and not explicit(argv, "--virtual-input"):
        args.virtual_input = False
    for hand in ("left", "right"):
        if not explicit(argv, "--" + hand + "-color"):
            setattr(args, hand + "_color", resolve_color(config["lighting"][hand + "_color"], config["palette"]))


class ConfigWatcher:
    """Disk reads happen off the MIDI thread; invalid edits keep the last settings."""
    def __init__(self, path, config, args, argv):
        self.path, self.config, self.args, self.argv = Path(path), config, args, tuple(argv)
        self.pending = queue.Queue(maxsize=1)
        self.stopped = threading.Event()
        self.thread = None
        self.last_error = None
        self.practice_pending = queue.Queue(maxsize=1)

    def poll(self):
        try:
            config = load(self.path, required=True)
        except (OSError, ValueError) as error:
            if str(error) != self.last_error:
                LOG.warning("Config edit ignored; retaining the last valid settings: %s", error)
                self.last_error = str(error)
            return
        self.last_error = None
        if config == self.config:
            return
        previous, self.config = self.config, config
        if config["midi"] != previous["midi"]:
            LOG.info("MIDI connection settings changed; restart the bridge to apply them")
        colors = tuple(getattr(self.args, hand + "_color") if explicit(self.argv, "--" + hand + "-color")
                       else resolve_color(config["lighting"][hand + "_color"], config["palette"])
                       for hand in ("left", "right"))
        brightness = self.args.brightness if explicit(self.argv, "--brightness") else config["lighting"]["rgb_brightness"]
        try:
            self.pending.get_nowait()
        except queue.Empty:
            pass
        self.pending.put_nowait((colors, brightness))
        try:
            self.practice_pending.get_nowait()
        except queue.Empty:
            pass
        self.practice_pending.put_nowait((config["practice"], config["trail"]))

    def start(self):
        if self.thread or not self.config["live_reload"]:
            return
        def watch():
            while not self.stopped.wait(self.config["poll_interval_ms"] / 1000):
                self.poll()
                if not self.config["live_reload"]:
                    break
        self.thread = threading.Thread(target=watch, daemon=True)
        self.thread.start()

    def apply(self, session):
        try:
            practice, trail = self.practice_pending.get_nowait()
            if getattr(session.args, "live_lighting", None) is None:
                session.bridge.set_practice(practice)
                session.bridge.set_trail(trail)
        except queue.Empty:
            pass
        try:
            colors, brightness = self.pending.get_nowait()
        except queue.Empty:
            return
        session.bridge.set_colors(*colors)
        session.args.brightness = brightness
        if session.driver:
            session.driver.brightness = brightness
        LOG.info("Applied config: left #%02X%02X%02X, right #%02X%02X%02X",
                 *colors[0], *colors[1])

    def close(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=1)
