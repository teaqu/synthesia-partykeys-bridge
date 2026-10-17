"""Expose one Synthesia output and receive its cues through a private native MIDI connection."""
from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time

import mido

LOG = logging.getLogger("partykeys")


@dataclass(frozen=True)
class CueRoute:
    input: str
    synthesia_output: str


def named_port(names, base):
    # WinMM appends a numeric index. Never select another bus by substring.
    matches = [name for name in names
               if re.fullmatch(re.escape(base) + r"(?: \d+)?", name, re.IGNORECASE)]
    if len(matches) > 1:
        raise ValueError(f"More than one MIDI port named {base!r}; use --input with an exact port name")
    return matches[0] if matches else None


def existing_route(name):
    if native_is_running(name):
        raise ValueError(f"Another bridge already owns MIDI destination {name!r}; close it first")
    inputs, outputs = mido.get_input_names(), mido.get_output_names()
    if any(named_port(ports, name + " Bridge") for ports in (inputs, outputs)):
        raise ValueError(f"A {name!r} bridge route already exists. Close the other bridge; "
                         "if it crashed, remove its pair in MIDI Settings before restarting.")
    source, destination = named_port(inputs, name), named_port(outputs, name)
    if source and destination:
        return CueRoute(source, destination)
    if source or destination:
        raise ValueError(f"Incomplete {name!r} MIDI connection; repair it or select --input explicitly")
    return None


def native_is_running(name):
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenMutexW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.OpenMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    mutex_name = "Local\\PartyKeysMidi-" + hashlib.sha256(name.encode("utf-8")).hexdigest().upper()
    handle = kernel.OpenMutexW(0x00100000, False, mutex_name)
    if handle:
        kernel.CloseHandle(handle)
        return True
    return False


def helper_path():
    root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    return root / "native-midi-helper" / "PartyKeys.Midi.exe"


class WindowsVirtualInput:
    def __init__(self, name="Synthesia Lights", helper=None):
        self.name = name
        self.helper = Path(helper) if helper else helper_path()
        self.process = None
        self.messages = queue.Queue()
        self.error = None
        self.reader = None

    def start(self):
        if not self.name.strip() or len(self.name) > 24:
            raise ValueError("Windows MIDI port name must be between 1 and 24 characters")
        if not self.helper.is_file():
            raise ValueError("The Windows MIDI API helper is missing. Extract the entire Windows "
                             "download, including native-midi-helper. Source installs must build it first. "
                             "Alternatively use loopMIDI with --input \"Synthesia Lights\".")
        events = queue.Queue()
        process = self.process = subprocess.Popen(
            [str(self.helper), "--name", self.name], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        def read():
            parser = mido.Parser()
            try:
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                        if event.get("status") == "midi":
                            parser.feed(event["data"])
                            for message in parser:
                                self.messages.put(message)
                        else:
                            if event.get("status") == "error":
                                self.error = event.get("message", "MIDI helper failed")
                            events.put(event)
                    except (ValueError, TypeError, KeyError, AttributeError) as error:
                        self.error = "Invalid data from MIDI helper: " + str(error)
                        events.put({"status": "error", "message": self.error})
            finally:
                events.put(None)

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    event = events.get(timeout=max(0.01, deadline - time.monotonic()))
                except queue.Empty:
                    raise ValueError("Timed out starting the Windows MIDI API helper") from None
                if event is None:
                    raise ValueError("Windows MIDI API helper exited before creating its device")
                if event.get("status") == "error":
                    raise ValueError("Windows MIDI API: " + event.get("message", "unknown error") +
                                     ". Use loopMIDI and --input \"Synthesia Lights\" as a fallback.")
                if event.get("status") == "ready":
                    break
                if time.monotonic() >= deadline:
                    raise ValueError("Timed out starting the Windows MIDI API helper")
            # The UMP-only receiver must not appear in Synthesia's WinMM lists.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                destination = named_port(mido.get_output_names(), self.name)
                if destination and process.poll() is None:
                    ports = mido.get_input_names() + mido.get_output_names()
                    if any(named_port([port], self.name + " Bridge") for port in ports):
                        raise ValueError("This Windows MIDI service does not hide the receiver port; "
                                         "use the earlier two-port build on this PC")
                    return CueRoute(self.name, destination)
                time.sleep(0.1)
            raise ValueError("Windows created the connection but did not expose its MIDI 1.0 output. "
                             "Use loopMIDI and --input \"Synthesia Lights\" on this PC.")
        except BaseException:
            self.close()
            raise

    def check(self):
        if self.error:
            raise OSError(self.error)
        if self.process is None or self.process.poll() is not None:
            raise OSError("Windows MIDI helper stopped; restart the bridge to recreate the cue device")

    def iter_pending(self):
        self.check()
        while True:
            try:
                yield self.messages.get_nowait()
            except queue.Empty:
                return

    def close(self):
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None:
                try:
                    process.stdin.write("quit\n")
                    process.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                    LOG.warning("MIDI helper did not shut down cleanly; its temporary loopback may remain until the MIDI service restarts")
        finally:
            process.stdin.close()
            if self.reader:
                self.reader.join(timeout=1)
            process.stdout.close()
            self.process = None
