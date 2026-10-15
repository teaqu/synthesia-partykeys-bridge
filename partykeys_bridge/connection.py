"""Recover hot-plugged MIDI endpoints without losing the live cue stream."""
import logging
import time

import mido
import rtmidi

from .driver import MidiInput, PalettePartyKeys, make_driver, octave_report, select_port

LOG = logging.getLogger("partykeys")
# InvalidPortError inherits ValueError, not RuntimeError/OSError. Catch the
# backend's base class specifically, without hiding configuration ValueErrors.
IO_ERRORS = (OSError, RuntimeError, rtmidi.RtMidiError)


class BridgeConnection:
    def __init__(self, args, bridge, print_port=None, now=time.monotonic):
        self.args, self.bridge, self.now = args, bridge, now
        self.ports = {"cue": None, "pressed": None, "output": None}
        self.names = {}
        self.status = {}
        self.driver = None
        self.next_scan = 0
        self.print_port = print_port
        self.keyboard_octaves = {}

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
        elif kind == "cue":
            self.bridge.active.clear()
            self.bridge.counts.clear()
        elif kind == "pressed":
            self.bridge.pressed.clear()
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
        try:
            inputs, outputs = mido.get_input_names(), mido.get_output_names()
        except IO_ERRORS as error:
            # CoreMIDI can change the list between getPortCount/getPortName.
            # An incomplete snapshot is not evidence that existing ports died.
            self.report("scan", f"MIDI enumeration changed during scan: {error}; retrying.", True)
            return
        self.status.pop("scan", None)
        self.report("inventory", f"MIDI inputs: {inputs}; outputs: {outputs}")
        specs = [("cue", inputs, self.args.input, "iac")]
        if not self.args.no_press_feedback:
            specs.append(("pressed", inputs, self.args.pressed_input, "partykeys"))
        if not self.args.dry_run:
            specs.append(("output", outputs, self.args.output, "partykeys"))
        for kind, available, requested, hint in specs:
            port = self.ports[kind]
            if port and self.names[kind] not in available:
                self.report(kind, f"{kind} port absent: {self.names[kind]}; waiting to reconnect.", True)
                self.drop(kind)
            if self.ports[kind]:
                continue
            name = self.resolve(kind, available, requested, hint)
            if name is None:
                continue
            if kind in ("pressed", "output") and name == self.names.get("cue"):
                raise ValueError("The Synthesia cue bus must differ from physical input and LED output")
            if kind == "cue" and name in (self.names.get("pressed"), self.names.get("output")):
                raise ValueError("The Synthesia cue bus must differ from physical input and LED output")
            try:
                self.ports[kind] = mido.open_output(name) if kind == "output" else MidiInput(name)
                self.names[kind] = name
                if kind == "output":
                    self.driver = make_driver(self.ports[kind], self.args)
                    self.restore_octaves()
                    self.driver.initialize(blocking=False)  # Clear gradually while inputs keep draining.
                self.report(kind, f"Connected {kind}: {name}")
            except IO_ERRORS as error:
                self.drop(kind)
                self.report(kind, f"{kind} open failed: {error}; retrying.", True)
        if self.args.dry_run and self.driver is None:
            self.driver = make_driver(self.print_port, self.args,
                                      now=self.print_port.now, sleep=self.print_port.sleep)
            self.restore_octaves()
            self.driver.initialize(blocking=False)

    def tick(self):
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
                        report = octave_report(message)
                        configured = getattr(self.args, "board", ())
                        if report is not None and any(
                                (channel, address) == (report[0], message.data[5])
                                for channel, address, _ in configured):
                            channel, octave = report
                            self.keyboard_octaves[channel] = octave
                            if isinstance(self.driver, PalettePartyKeys):
                                if self.driver.set_octave(channel, octave):
                                    # Held notes can be released under new pitches after
                                    # a shift. Do not leave the old pitches stuck blue.
                                    self.bridge.pressed.clear()
                        self.bridge.receive_pressed(message)
                        if self.driver and message.type in ("note_on", "note_off"):
                            self.driver.invalidate_note(message.note)
            except IO_ERRORS as error:
                self.drop(kind)
                self.report(kind, f"{kind} receive failed: {error}; retrying.", True)
        if self.driver:
            try:
                self.driver.show(self.bridge.lights())
            except IO_ERRORS as error:
                self.drop("output")
                self.report("output", f"LED send failed: {error}; retrying.", True)

    def close(self):
        try:
            if self.driver:
                try:
                    LOG.info("Clearing remaining LEDs with %g ms spacing before closing.", self.driver.min_interval * 1000)
                    self.driver.clear()
                except IO_ERRORS as error:
                    LOG.warning("LED cleanup unavailable: %s", error)
        finally:
            for kind in ("output", "pressed", "cue"):
                self.drop(kind)

    def run(self):
        print("Bridge running with automatic reconnection. Ctrl+C clears lights and exits.")
        try:
            while True:
                self.tick()
                time.sleep(0.01)
        finally:
            self.close()
