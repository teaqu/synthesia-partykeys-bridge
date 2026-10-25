"""Recover hot-plugged MIDI endpoints without losing the live cue stream."""
import logging
import sys
import time
import threading

import mido
import rtmidi

from .driver import MidiInput, PalettePartyKeys, make_driver, octave_report, select_port
from .windows_midi import WindowsVirtualInput, existing_route
from .practice import capture_key, paint_practice, physical_key_event

LOG = logging.getLogger("partykeys")
# InvalidPortError inherits ValueError, not RuntimeError/OSError. Catch the
# backend's base class specifically, without hiding configuration ValueErrors.
IO_ERRORS = (OSError, RuntimeError, rtmidi.RtMidiError)


class BridgeConnection:
    def __init__(self, args, bridge, print_port=None, now=time.monotonic, config_watcher=None):
        self.config_watcher = config_watcher
        self.virtual_input = getattr(args, "virtual_input", False)
        if self.virtual_input and sys.platform not in ("darwin", "win32"):
            raise ValueError("--virtual-input is supported on macOS and Windows")
        self.windows_virtual = self.virtual_input and sys.platform == "win32"
        self.windows_device = None
        self.windows_route = None
        self.cue_name = args.input or "Synthesia Lights"
        if self.virtual_input and not self.cue_name.strip():
            raise ValueError("Virtual MIDI destination name cannot be empty")
        self.args, self.bridge, self.now = args, bridge, now
        self.ports = {"cue": None, "pressed": None, "output": None}
        self.names = {}
        self.status = {}
        self.driver = None
        self.topology = None
        self.auto_board_routes = (getattr(args, "protocol", "rgb15") == "palette71"
                                  and not getattr(args, "board", ()) and not args.dry_run)
        self.next_scan = 0
        self.clear_generation = self.bridge.clear_generation
        self.print_port = print_port
        self.keyboard_octaves = {}
        self.live_generation = -1
        self.practice_erase = False

    def restore_octaves(self):
        if isinstance(self.driver, PalettePartyKeys):
            for channel, octave in self.keyboard_octaves.items():
                self.driver.set_octave(channel, octave)

    def report(self, key, message, warning=False):
        if self.status.get(key) != message:
            (LOG.warning if warning else LOG.info)(message)
            self.status[key] = message

    def drop(self, kind):
        port, self.ports[kind] = self.ports[kind], None
        if kind == "output":
            self.driver = None
            self.topology = None
        elif kind == "cue":
            self.bridge.active.clear()
            self.bridge.counts.clear()
        elif kind == "pressed":
            if port:
                physical_key_event(self.args)
            if self.auto_board_routes and self.ports["output"]:
                self.drop("output")
        if port:
            try:
                if hasattr(port, "close"):
                    port.close()
            except Exception as error:
                self.report(f"close-{kind}", f"{kind} close failed: {error}", True)

    def resolve(self, kind, available, requested, hint):
        # Once selected, reconnect only the exact same endpoint. Never silently
        # switch to another keyboard or IAC bus when the selected one vanishes.
        if kind in self.names:
            return self.names[kind] if self.names[kind] in available else None
        try:
            return select_port(available, requested, hint)
        except ValueError as error:
            self.report(kind, str(error) + " Waiting for connection.", True)
            return None

    def refresh(self):
        if self.windows_virtual:
            if self.windows_route is None:
                self.windows_route = existing_route(self.cue_name)
                if self.windows_route is None:
                    self.windows_device = WindowsVirtualInput(self.cue_name)
                    self.windows_route = self.windows_device.start()
                    self.ports["cue"] = self.windows_device
                    self.names["cue"] = self.cue_name
                self.args.input = self.windows_route.input
                LOG.info("In Synthesia, select Music Output -> %s -> Key Lights -> Finger-based channel. "
                         "Keep this bridge running.", self.windows_route.synthesia_output)
            if self.windows_device:
                self.windows_device.check()
        try:
            inputs, outputs = mido.get_input_names(), mido.get_output_names()
        except IO_ERRORS as error:
            # CoreMIDI can change the list between getPortCount/getPortName.
            # An incomplete snapshot is not evidence that existing ports died.
            self.report("scan", f"MIDI enumeration changed during scan: {error}; retrying.", True)
            return
        self.status.pop("scan", None)
        self.report("inventory", f"MIDI inputs: {inputs}; outputs: {outputs}")
        specs = [] if self.windows_device else [("cue", inputs, self.args.input, "iac")]
        if not self.args.dry_run:
            specs.append(("pressed", inputs, self.args.keyboard_input, "partykeys"))
        if not self.args.dry_run:
            specs.append(("output", outputs, self.args.output, "partykeys"))
        for kind, available, requested, hint in specs:
            virtual = kind == "cue" and self.virtual_input and not self.windows_virtual
            if virtual:
                if self.ports[kind]:
                    continue  # A virtual destination isn't an enumerated MIDI source.
                name = self.cue_name
                if name in inputs or name in outputs:
                    raise ValueError(f"MIDI destination {name!r} already exists. Stop the other bridge or choose another --input name.")
            else:
                if kind != "cue":
                    available = [name for name in available if name != self.names.get("cue")]
                    if self.windows_route:
                        available = [name for name in available
                                     if name not in (self.windows_route.input, self.windows_route.synthesia_output)]
            port = self.ports[kind]
            if not virtual and port and self.names[kind] not in available:
                self.report(kind, f"{kind} port absent: {self.names[kind]}; waiting to reconnect.", True)
                self.drop(kind)
            if self.ports[kind]:
                continue
            if not virtual:
                name = self.resolve(kind, available, requested, hint)
            if name is None:
                continue
            if kind in ("pressed", "output") and name == self.names.get("cue"):
                raise ValueError("The Synthesia cue bus must differ from physical input and LED output")
            if kind == "cue" and name in (self.names.get("pressed"), self.names.get("output")):
                raise ValueError("The Synthesia cue bus must differ from physical input and LED output")
            try:
                if virtual:
                    self.ports[kind] = MidiInput(name, virtual=True)
                else:
                    self.ports[kind] = mido.open_output(name) if kind == "output" else MidiInput(name)
                self.names[kind] = name
                if kind == "output":
                    if self.auto_board_routes:
                        from .routing import discover_boards
                        if not self.ports["pressed"]:
                            raise OSError("Board discovery is waiting for the PartyKeys physical input")
                        self.args.board, octaves = discover_boards(self.ports["pressed"], self.ports[kind])
                        self.keyboard_octaves.update(octaves)
                    self.driver = make_driver(self.ports[kind], self.args)
                    self.restore_octaves()
                    self.driver.initialize(blocking=False)  # Clear gradually while inputs keep draining.
                    if isinstance(self.driver, PalettePartyKeys):
                        self.driver.request_full_clear()
                    if self.auto_board_routes:
                        from .routing import BoardWatcher
                        self.topology = BoardWatcher(self.args.board, self.keyboard_octaves, now=self.now)
                self.report(kind, f"Connected {kind}: {name}")
                if virtual:
                    LOG.info("In Synthesia, select Music Output → %s → Key Lights → Finger-based channel. Keep this bridge running.", name)
            except IO_ERRORS as error:
                self.drop(kind)
                self.report(kind, f"{kind} open failed: {error}; retrying.", True)
        if self.args.dry_run and self.driver is None:
            self.driver = make_driver(self.print_port, self.args,
                                      now=self.print_port.now, sleep=self.print_port.sleep)
            self.restore_octaves()
            self.driver.initialize(blocking=False)

    def tick(self):
        if self.config_watcher:
            self.config_watcher.apply(self)
        live = getattr(self.args, "live_lighting", None)
        if live is not None:
            generation, value = live.snapshot()
            if value is not None and generation != self.live_generation:
                practice, trail, self.practice_erase, brightness, enabled = value
                self.bridge.set_practice(practice)
                self.bridge.set_trail(trail)
                self.bridge.set_brightness(brightness)
                self.bridge.enabled = enabled
                self.live_generation = generation
        if self.now() >= self.next_scan:
            self.next_scan = self.now() + 1
            self.refresh()
        for kind in ("cue", "pressed"):
            if not self.ports[kind]:
                continue
            try:
                for message in self.ports[kind].iter_pending():
                    LOG.debug("%s MIDI: %s", kind, message)
                    if kind == "cue":
                        self.bridge.receive(message)
                    else:
                        physical_key_event(self.args, message)
                        captured = capture_key(self.args, message)
                        if captured is not None:
                            self.bridge.set_practice(paint_practice(self.bridge.practice, captured, self.practice_erase))
                        if self.topology:
                            self.topology.receive(message)
                        report = octave_report(message)
                        configured = getattr(self.args, "board", ())
                        if report is not None and any(
                                (channel, address) == (report[0], message.data[5])
                                for channel, address, _ in configured):
                            channel, octave = report
                            self.keyboard_octaves[channel] = octave
                            if isinstance(self.driver, PalettePartyKeys):
                                self.driver.set_octave(channel, octave)
                        if self.driver and message.type in ("note_on", "note_off"):
                            self.driver.invalidate_note(message.note)
            except IO_ERRORS as error:
                self.drop(kind)
                self.report(kind, f"{kind} receive failed: {error}; retrying.", True)
        if self.driver:
            try:
                if self.bridge.clear_generation != self.clear_generation:
                    self.clear_generation = self.bridge.clear_generation
                    if isinstance(self.driver, PalettePartyKeys):
                        self.driver.request_full_clear()
                if self.topology:
                    update = self.topology.tick(self.driver)
                    if update:
                        boards, octaves, reinitialize = update
                        try:
                            self.driver.replace_boards(boards, octaves, reinitialize)
                        except ValueError as error:
                            self.topology.request()
                            self.report("topology", f"Invalid keyboard topology; LED writes suspended: {error}", True)
                            return
                        self.args.board = boards
                        self.keyboard_octaves.update(octaves)
                        self.report("topology", f"Keyboard routes updated: {boards}")
                    if self.topology.suspended:
                        return
                self.driver.show(self.bridge.lights())
            except IO_ERRORS as error:
                self.drop("output")
                self.report("output", f"LED send failed: {error}; retrying.", True)

    def close(self):
        if self.config_watcher:
            self.config_watcher.close()
        try:
            if self.driver:
                try:
                    LOG.info("Clearing remaining LEDs with %g ms spacing before closing.", self.driver.min_interval * 1000)
                    self.driver.clear()
                except IO_ERRORS as error:
                    LOG.warning("LED cleanup unavailable: %s", error)
        finally:
            try:
                for kind in ("output", "pressed", "cue"):
                    self.drop(kind)
            finally:
                if self.windows_device:
                    self.windows_device.close()

    def run(self):
        LOG.info("Keyboard connection running with automatic reconnection.")
        try:
            if self.config_watcher:
                self.config_watcher.start()
            stop = getattr(self.args, "stop_event", None) or threading.Event()
            while not stop.is_set():
                self.tick()
                stop.wait(0.001)
        finally:
            self.close()
