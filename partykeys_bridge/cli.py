import argparse
from contextlib import contextmanager, ExitStack
import logging
import math
import os
import queue
import select
import signal
import sys
import threading
import time

import mido

from .bridge import Bridge
from .connection import BridgeConnection
from .driver import PartyKeys, select_port, make_driver
from .preview import AudioPlayback, Preview, Transport
from .song import Song
from . import settings

LOG = logging.getLogger("partykeys")


def color(value):
    try:
        value = value.removeprefix("#")
        if len(value) != 6:
            raise ValueError()
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        raise argparse.ArgumentTypeError("Use a six-digit RGB hex color, e.g. 0050FF")


def numbers(value):
    try:
        return {int(x) for x in value.split(",") if x.strip()}
    except ValueError:
        raise argparse.ArgumentTypeError("Use comma-separated integers")


def channels(value):
    selected = numbers(value)
    if not selected <= set(range(1, 17)):
        raise argparse.ArgumentTypeError("MIDI channels are 1..16")
    return {n - 1 for n in selected}


def finite(value):
    try:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError()
        return result
    except ValueError:
        raise argparse.ArgumentTypeError("Use a finite number")


def board(value):
    try:
        parts = tuple(int(part, 0) for part in value.split(":"))
        if len(parts) != 3:
            raise ValueError()
        return parts
    except ValueError:
        raise argparse.ArgumentTypeError("Use CHANNEL:ADDRESS:LOWEST_NOTE, e.g. 1:0x3f:48")


def parser():
    p = argparse.ArgumentParser(description="PartyKeys 36: Synthesia lights and independent MIDI preview")
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("ports", help="List MIDI inputs and outputs")
    inspect = commands.add_parser("inspect", help="Show MIDI tracks and duration without hardware")
    inspect.add_argument("file")
    for mode in ("bridge", "preview", "test", "gui"):
        sub = commands.add_parser(mode)
        sub.add_argument("--output", help="Exact MIDI output name or unique substring (auto: PartyKeys)")
        sub.add_argument("--protocol", choices=("palette71", "rgb15"),
                         default="palette71" if mode in ("bridge", "gui") else "rgb15",
                         help="palette71 uses preset colors and fixed device brightness")
        sub.add_argument("--brightness", type=finite, default=1.0)
        sub.add_argument("--palette-interval-ms", type=int, choices=(1, 10, 20, 40, 100, 250), default=1,
                         help="LED message spacing in milliseconds; default 1")
        sub.add_argument("--base-note", type=int, default=48, help="MIDI note of leftmost key; default 48")
        sub.add_argument("--board", type=board, action="append", default=[],
                         help="Palette board CHANNEL:ADDRESS:LOWEST_NOTE at octave zero; repeat for joined boards")
        sub.add_argument("--initial-octave", type=int, choices=range(-3, 4), default=0,
                         help="Current board octave on startup; live hardware reports update it automatically")
        sub.add_argument("--debug", action="store_true")
        sub.add_argument("--dry-run", action="store_true", help="Print LED SysEx; do not open hardware outputs")
        if mode == "test":
            sub.add_argument("--note", type=int, default=60)
            sub.add_argument("--color", type=color, default=(0, 80, 255))
            sub.add_argument("--seconds", type=finite, default=2.0)
            sub.add_argument("--sweep", action="store_true", help="Test all 36 keys, one at a time")
            continue
        sub.add_argument("--left-color", type=color, default=(0, 80, 255))
        sub.add_argument("--right-color", type=color, default=(0, 255, 0) if mode == "bridge" else (255, 100, 0))
        sub.add_argument("--left-channels", type=channels, default=None if mode in ("bridge", "gui") else {1})
        sub.add_argument("--right-channels", type=channels, default=None if mode in ("bridge", "gui") else {0})
        if mode in ("bridge", "gui"):
            sub.add_argument("--config", help="JSON settings file (default: config.json beside the executable)")
            sub.add_argument("--input", help="Cue MIDI input or unique substring; with --virtual-input, name to create")
            sub.add_argument("--virtual-input", action="store_true",
                             help="Create cue ports automatically on macOS or Windows 11 25H2+ (experimental API); default: Synthesia Lights")
            sub.add_argument("--keyboard-input", help="Keyboard input for board and octave reports (auto: PartyKeys)")
        else:
            sub.add_argument("file")
            sub.add_argument("--lookahead", type=finite, default=2.0, help="Advance lighting in real seconds, 0..5")
            sub.add_argument("--near", type=finite, help="Bright stage begins this many real seconds early; default min(1, lookahead)")
            sub.add_argument("--dim", type=finite, default=0.2)
            sub.add_argument("--active-color", type=color, default=(0, 255, 0))
            sub.add_argument("--left-tracks", type=numbers, default=set(), help="Zero-based track indices override channels")
            sub.add_argument("--right-tracks", type=numbers, default=set())
            sub.add_argument("--speed", type=finite, default=1.0)
            sub.add_argument("--latency-ms", type=finite, default=0, help="Advance LEDs to compensate measured latency, 0..1000")
            sub.add_argument("--audio-output", help="Optional separate MIDI synth output; PartyKeys has no sound engine")
            sub.add_argument("--paused", action="store_true", help="Wait for play command")
    return p


class PrintPort:
    # Advance only simulated waits: dry runs never own a hardware output.
    def __init__(self):
        self.offset = 0.0

    def now(self):
        return time.monotonic() + self.offset

    def sleep(self, seconds):
        self.offset += seconds

    def send(self, message):
        print("LED:", message.hex(), flush=True)


class Commands:
    """Nonblocking line input without TextIO read-ahead hiding pasted commands."""
    def __init__(self):
        self.buffer, self.open = b"", True
        self.chunks = None
        if sys.platform == "win32":
            # Windows select() accepts sockets, not console or pipe handles.
            # Read the raw descriptor on a daemon thread so stdin never blocks
            # MIDI playback and no buffered-reader lock is held at shutdown.
            self.chunks = queue.SimpleQueue()
            threading.Thread(target=self._read_windows, args=(sys.stdin.fileno(), self.chunks),
                             daemon=True).start()

    @staticmethod
    def _read_windows(fd, chunks):
        try:
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                chunks.put(chunk)
        except OSError:
            pass  # A closed console is EOF.
        finally:
            chunks.put(b"")

    def poll(self):
        chunk = None
        if self.open:
            if self.chunks is not None:
                try:
                    chunk = self.chunks.get_nowait()
                except queue.Empty:
                    pass
            elif select.select([sys.stdin], [], [], 0)[0]:
                chunk = os.read(sys.stdin.fileno(), 4096)
        if chunk:
            self.buffer += chunk
        elif chunk == b"":
            self.open = False
            if self.buffer and not self.buffer.endswith(b"\n"):
                self.buffer += b"\n"
        if b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            return line.decode("utf-8", errors="replace").strip().split()
        return None


def inventory():
    inputs, outputs = mido.get_input_names(), mido.get_output_names()
    print("MIDI inputs:", *(f"\n  {n}" for n in inputs), "" if inputs else " (none)")
    print("MIDI outputs:", *(f"\n  {n}" for n in outputs), "" if outputs else " (none)")
    return inputs, outputs


@contextmanager
def lights_session(port, args):
    clock = dict(now=port.now, sleep=port.sleep) if isinstance(port, PrintPort) else {}
    driver = make_driver(port, args, **clock)
    try:
        driver.initialize()
        yield driver
    finally:
        try:
            driver.clear()
        except Exception as error:
            LOG.warning("Could not clear LEDs (device disconnected?): %s", error)


def check_connections(input_name=None, output_name=None, audio_name=None):
    if input_name and input_name not in mido.get_input_names():
        raise OSError(f"MIDI input disconnected: {input_name}. Reconnect, then restart the bridge.")
    outputs = mido.get_output_names() if output_name or audio_name else []
    for name in (output_name, audio_name):
        if name and name not in outputs:
            raise OSError(f"MIDI output disconnected: {name}. Reconnect, then restart.")


def run_preview(args, song, driver, preview, audio, output_name, audio_name):
    transport = Transport(song.duration, args.speed, position=-args.lookahead * args.speed)
    commands = Commands()
    next_check = next_status = 0.0
    print("Independent playback: NOT synchronised with Synthesia.")
    print("Commands + Enter: play, pause, resume, stop, seek <seconds>, speed <factor>, status, quit")
    if not args.paused:
        transport.play()
    try:
        while True:
            command = commands.poll()
            if command:
                try:
                    name = command[0]
                    if name in ("q", "quit", "exit"):
                        break
                    if name in ("pause", "stop"):
                        transport.pause()
                        if name == "stop":
                            transport.restart(args.lookahead)
                        if audio:
                            audio.silence()
                        driver.show({}, force=True)
                    elif name in ("play", "resume"):
                        if not transport.playing:
                            if transport.position() >= song.duration:
                                transport.restart(args.lookahead)
                            if audio:
                                audio.seek(transport.position())
                            transport.play()
                    elif name == "seek" and len(command) == 2:
                        transport.seek(float(command[1]))
                        if audio:
                            if transport.playing:
                                audio.seek(transport.position())
                            else:
                                audio.silence()
                    elif name == "speed" and len(command) == 2:
                        transport.set_speed(float(command[1]))
                    elif name != "status":
                        print("Unknown command or missing value.")
                    next_status = 0
                except ValueError as error:
                    print(f"Invalid command: {error}")
            position = transport.position()
            if transport.playing:
                if audio:
                    audio.advance(position)
                lights = preview.lights(position + args.latency_ms / 1000 * transport.speed, transport.speed)
                driver.show(lights)
                if position >= song.duration:
                    transport.pause()
                    if audio:
                        audio.silence()
                    driver.show({}, force=True)
                    print("Finished.")
                    if not sys.stdin.isatty():
                        break
            elif not commands.open and not commands.buffer:
                break
            now = time.monotonic()
            if now >= next_check:
                check_connections(output_name=output_name, audio_name=audio_name)
                next_check = now + 1
            if now >= next_status:
                print(f"{position:.2f}/{song.duration:.2f}s  {transport.speed:g}x  {'playing' if transport.playing else 'paused'}")
                next_status = now + 5
            time.sleep(0.01)
    finally:
        if audio:
            try:
                audio.silence()
            except Exception as error:
                LOG.warning("Could not silence audio output: %s", error)


def execute(args):
    if args.command == "gui":
        from .ui import run
        run(args)
        return
    if args.command == "inspect":
        song = Song.load(args.file)
        print(f"{len(song.notes)} notes; {song.duration:.3f} seconds")
        for index, name in enumerate(song.tracks):
            notes = [n for n in song.notes if n.track == index]
            print(f"  Track {index}: {name}; {len(notes)} notes; channels {sorted({n.channel + 1 for n in notes})}")
        return
    mido.set_backend("mido.backends.rtmidi")
    if args.command == "ports":
        inventory()
        return
    # Validate before opening a MIDI output or changing the keyboard.
    make_driver(None, args)
    bridge = preview = song = None
    if args.command == "bridge":
        bridge = Bridge(args.left_color, args.right_color, args.left_channels, args.right_channels)
        bridge.set_practice(getattr(args, "practice", settings.DEFAULTS["practice"]))
        bridge.set_trail(getattr(args, "trail", settings.DEFAULTS["trail"]))
        BridgeConnection(args, bridge, PrintPort(), config_watcher=getattr(args, "config_watcher", None)).run()
        return
    elif args.command == "preview":
        if not 0 <= args.latency_ms <= 1000:
            raise ValueError("Latency compensation must be 0..1000 ms")
        song = Song.load(args.file)
        if not (args.left_tracks | args.right_tracks) <= set(range(len(song.tracks))):
            raise ValueError("Track index outside file; use 'partykeys inspect FILE'")
        Transport(song.duration, args.speed)
        preview = Preview(song, args.lookahead, min(1, args.lookahead) if args.near is None else args.near,
                          args.dim, args.left_color, args.right_color, args.active_color,
                          args.left_tracks, args.right_tracks, args.left_channels, args.right_channels)
        if args.dry_run and args.audio_output:
            raise ValueError("--dry-run cannot be combined with --audio-output")
    elif args.seconds <= 0 or not args.base_note <= args.note < args.base_note + 36:
        raise ValueError("Test duration must be positive and test note must be inside the physical key range")
    if not args.dry_run:
        inputs, outputs = inventory()
    else:
        inputs, outputs = [], []
        print("Dry run: MIDI hardware enumeration and outputs skipped.")
    output_name = None if args.dry_run else select_port(outputs, args.output, "partykeys")
    with ExitStack() as stack:
        port = PrintPort() if args.dry_run else stack.enter_context(mido.open_output(output_name))
        audio_name = None
        audio = None
        if args.command == "preview" and args.audio_output:
            audio_name = select_port(outputs, args.audio_output, "")
            if audio_name == output_name or "partykeys" in audio_name.casefold():
                raise ValueError("Select a separate synthesizer for --audio-output")
            audio = AudioPlayback(song, stack.enter_context(mido.open_output(audio_name)))
        driver = stack.enter_context(lights_session(port, args))
        if preview:
            run_preview(args, song, driver, preview, audio, output_name, audio_name)
        else:
            notes = range(args.base_note, args.base_note + 36) if args.sweep else [args.note]
            for note in notes:
                print(f"Lighting MIDI {note}, physical index {note - args.base_note}")
                driver.show({note: args.color}, force=True)
                time.sleep(args.seconds)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if getattr(args, "debug", False) else logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    previous = signal.getsignal(signal.SIGTERM)
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        if args.command in ("bridge", "gui"):
            args.startup_argv = argv
            config_required = bool(args.config)
            config_path = args.config or settings.default_path()
            args.config = str(config_path)
            args.practice = settings.DEFAULTS["practice"]
            args.trail = settings.DEFAULTS["trail"]
            if config_required or config_path.is_file():
                config = settings.load(config_path, required=config_required)
                settings.apply_startup(args, config, argv)
                args.practice = config["practice"]
                args.trail = config["trail"]
                args.config_watcher = settings.ConfigWatcher(config_path, config, args, argv)
        execute(args)
        return 0
    except KeyboardInterrupt:
        print("Stopped; LED cleanup attempted.")
        return 0
    except (OSError, EOFError, ValueError, RuntimeError, ImportError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
