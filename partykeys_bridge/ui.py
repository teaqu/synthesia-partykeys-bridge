"""Small Tk interface shared by Windows, macOS and desktop Linux."""
from copy import copy
import json
import logging
from pathlib import Path
import queue
import threading
import tempfile
import tkinter as tk
from tkinter import ttk, colorchooser, messagebox

from . import settings
from .practice import note_name, LiveLighting


class ColorButton(ttk.Button):
    """Let the OS theme draw the button and text; show colour in an image.

    Aqua ignores tk.Button backgrounds, leaving custom text colours unreadable.
    Keep the image alive for as long as its button, including brush updates.
    """
    def __init__(self, master, color, **kwargs):
        self.color_image = tk.PhotoImage(master=master, width=16, height=16)
        self.color_image.put("#888888", to=(0, 0, 16, 16))
        self.set_color(color)
        super().__init__(master, image=self.color_image, compound="left", **kwargs)

    def set_color(self, color):
        self.color_image.put("#" + color, to=(1, 1, 15, 15))


class StatusHandler(logging.Handler):
    def __init__(self, events):
        super().__init__(logging.INFO)
        self.events = events

    def emit(self, record):
        self.events.put(record.getMessage())


def save_config(path, practice, hand_colors, trail=None):
    """Preserve other settings and replace atomically so reload never sees half JSON."""
    path = Path(path)
    config = settings.load(path)
    config["practice"] = practice
    config["lighting"].update(hand_colors)
    if trail is not None:
        config["trail"] = trail
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        settings.load(temporary, required=True)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return config


class Window:
    def __init__(self, root, args, auto_start=True):
        self.root, self.args = root, args
        self.path = Path(args.config or settings.default_path())
        config = settings.load(self.path)
        self.selected = set(config["practice"]["notes"])
        self.color = config["practice"]["color"].removeprefix("#")
        self.note_colors = {n: config["practice"]["note_colors"].get(str(n), self.color).removeprefix("#") for n in self.selected}
        self.paint_mode = tk.StringVar(value="paint")
        self.capture_enabled = tk.BooleanVar(value=False)
        self.capture = threading.Event()
        self.key_events = queue.SimpleQueue()
        self.live_lighting = LiveLighting()
        self.trail_enabled = tk.BooleanVar(value=config["trail"]["enabled"])
        self.trail_seconds = tk.StringVar(value=str(config["trail"]["duration_seconds"]))
        self.trail_brightness = tk.DoubleVar(value=config["trail"]["max_brightness"] * 100)
        self.trail_percent = tk.StringVar()
        self.hand_colors = {hand + "_color": config["lighting"][hand + "_color"] for hand in ("left", "right")}
        self.enabled = tk.BooleanVar(value=config["practice"]["enabled"])
        self.brightness = tk.DoubleVar(value=config["practice"]["brightness"] * 100)
        self.status = tk.StringVar(value="Ready")
        self.summary = tk.StringVar()
        self.percent = tk.StringVar()
        self.worker = None
        self.stop = threading.Event()
        self.events = queue.SimpleQueue()
        self.log_handler = StatusHandler(self.events)
        logging.getLogger("partykeys").addHandler(self.log_handler)
        self.saving = None
        self.closing = False
        root.title("PartyKeys · Practice lights")
        root.geometry("820x650")
        root.minsize(740, 650)
        frame = ttk.Frame(root, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Practice lights", font=("", 18, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Choose a colour. Left-click to paint; right-click to erase. Synthesia cues appear over practice lights.",
                  wraplength=700).pack(anchor="w", pady=(4, 14))
        row = ttk.Frame(frame)
        row.pack(fill="x")
        self.enable_toggle = ttk.Checkbutton(row, text="Enable practice lights", variable=self.enabled, command=self.changed)
        self.enable_toggle.pack(side="left")
        self.swatch = ColorButton(row, self.color, text="Practice colour…", command=self.pick_color)
        self.swatch.pack(side="right")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(10, 5))
        self.presets = {}
        for index, (name, value) in enumerate((("Blue", "0050FF"), ("Green", "00FF00"), ("Red", "FF0000"),
                            ("Orange", "FF6400"), ("Yellow", "FFFF00"), ("Purple", "B400FF"),
                            ("Cyan", "00FFB4"), ("White", "FFFFFF"))):
            button = ColorButton(row, value, text=name, width=0, padding=(5, 3),
                                 command=lambda c=value: self.set_brush(c))
            button.grid(row=index // 4, column=index % 4, sticky="ew", padx=2, pady=2)
            self.presets[name] = button
        for column in range(4):
            row.columnconfigure(column, weight=1, uniform="colours")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=5)
        ttk.Label(row, text="Physical keyboard:").pack(side="left", padx=(0, 8))
        ttk.Radiobutton(row, text="Paint", variable=self.paint_mode, value="paint", command=self.push_live).pack(side="left")
        ttk.Radiobutton(row, text="Erase", variable=self.paint_mode, value="erase", command=self.push_live).pack(side="left", padx=10)
        ttk.Checkbutton(row, text="Press keys to paint / erase", variable=self.capture_enabled,
                        command=self.set_capture).pack(side="left", padx=10)
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=10)
        ttk.Label(row, text="Brightness").pack(side="left")
        ttk.Scale(row, from_=0, to=100, variable=self.brightness, command=lambda _: self.changed()).pack(side="left", fill="x", expand=True, padx=12)
        ttk.Label(row, textvariable=self.percent, width=5).pack(side="right")
        self.canvas = tk.Canvas(frame, height=132, background="#e6e8ec", highlightthickness=0)
        self.canvas.pack(fill="x")
        scroll = ttk.Scrollbar(frame, orient="horizontal", command=self.canvas.xview)
        scroll.pack(fill="x")
        self.canvas.configure(xscrollcommand=scroll.set)
        self.keys = {}
        self.draw_keyboard()
        self.canvas.bind("<Button-1>", self.click)
        # Aqua numbers the secondary mouse button as 2; Windows/X11 use 3.
        self.secondary_click = "<Button-2>" if root.tk.call("tk", "windowingsystem") == "aqua" else "<Button-3>"
        self.canvas.bind(self.secondary_click, lambda event: self.click(event, erase=True))
        if root.tk.call("tk", "windowingsystem") == "aqua":
            self.canvas.bind("<Control-Button-1>", lambda event: self.click(event, erase=True))
        def show_start_octave():
            self.canvas.xview_moveto(max(0, (args.base_note // 12 * 7 * 30 - 30) / (75 * 30)))
        root.after(50, show_start_octave)
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(8, 10))
        ttk.Label(row, textvariable=self.summary).pack(side="left")
        ttk.Button(row, text="Clear selection", command=self.clear).pack(side="right")
        row = ttk.Frame(frame)
        row.pack(fill="x")
        ttk.Label(row, text="Synthesia colours:").pack(side="left")
        for hand in ("left", "right"):
            ttk.Button(row, text=hand.title() + " hand…", command=lambda h=hand: self.pick_hand(h)).pack(side="left", padx=5)
        self.toggle = ttk.Button(row, text="Start bridge", command=self.toggle_bridge)
        self.toggle.pack(side="right")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(12, 0))
        ttk.Checkbutton(row, text="Trail mode", variable=self.trail_enabled, command=self.changed).pack(side="left")
        ttk.Label(row, text="Fade duration:").pack(side="left", padx=(12, 6))
        self.duration = ttk.Spinbox(row, from_=.25, to=30, increment=.25, width=5,
                                   textvariable=self.trail_seconds, command=self.changed)
        self.duration.pack(side="left")
        self.duration.bind("<Return>", lambda _: self.changed())
        self.duration.bind("<FocusOut>", lambda _: self.changed())
        ttk.Label(row, text="seconds").pack(side="left", padx=6)
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(10, 0))
        ttk.Label(row, text="Trail max brightness").pack(side="left")
        ttk.Scale(row, from_=0, to=100, variable=self.trail_brightness,
                  command=lambda _: self.changed()).pack(side="left", fill="x", expand=True, padx=12)
        ttk.Label(row, textvariable=self.trail_percent, width=5).pack(side="right")
        ttk.Label(frame, textvariable=self.status, wraplength=720).pack(anchor="w", pady=(10, 0))
        ttk.Label(frame, text="In Synthesia: Music Output → Synthesia Lights → Key Lights → Finger-based channel.", wraplength=720).pack(anchor="w", pady=(5, 0))
        self.refresh()
        self.push_live()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)
        # Hardware is only opened by the bridge owned by this window.
        if auto_start:
            root.after(200, self.start_bridge)

    def draw_keyboard(self):
        white = 0
        for note in range(128):
            if note % 12 in (1, 3, 6, 8, 10):
                continue
            x = white * 30
            self.keys[note] = self.canvas.create_rectangle(x, 0, x + 30, 130, fill="white", outline="#a8adb5", tags=(f"note:{note}",))
            self.canvas.create_text(x + 15, 114, text=note_name(note), font=("", 8), fill="#14161b", tags=(f"note:{note}",))
            white += 1
        white = 0
        for note in range(128):
            if note % 12 not in (1, 3, 6, 8, 10):
                white += 1
                continue
            x = white * 30 - 10
            self.keys[note] = self.canvas.create_rectangle(x, 0, x + 20, 78, fill="#252932", outline="#14161b", tags=(f"note:{note}",))
        self.canvas.configure(scrollregion=(0, 0, white * 30, 132))

    def click(self, event, erase=False):
        item = self.canvas.find_withtag("current")
        if not item:
            return
        for tag in self.canvas.gettags(item[0]):
            if tag.startswith("note:"):
                note = int(tag.split(":")[1])
                self.paint_note(note, erase=erase)
                break
        return "break"

    def paint_note(self, note, erase=None):
        if erase is None:
            erase = self.paint_mode.get() == "erase"
        if erase:
            self.selected.discard(note)
            self.note_colors.pop(note, None)
        else:
            self.selected.add(note)
            self.note_colors[note] = self.color
            self.enabled.set(True)
        self.changed()

    def set_capture(self):
        if self.capture_enabled.get():
            self.capture.set()
            self.status.set("Keyboard painting on · Press keys to apply the current colour")
        else:
            self.capture.clear()
            self.status.set("Keyboard painting off · Your practice selection is saved")

    def set_brush(self, color):
        self.color = color
        self.paint_mode.set("paint")
        self.changed()

    def clear(self):
        self.selected.clear()
        self.note_colors.clear()
        self.changed()

    def refresh(self):
        for note, item in self.keys.items():
            fill = "#" + self.note_colors.get(note, self.color) if note in self.selected else ("#252932" if note % 12 in (1, 3, 6, 8, 10) else "white")
            self.canvas.itemconfigure(item, fill=fill)
        self.swatch.set_color(self.color)
        self.percent.set(f"{round(self.brightness.get())}%")
        self.trail_percent.set(f"{round(self.trail_brightness.get())}%")
        names = ", ".join(note_name(n) for n in sorted(self.selected))
        self.summary.set(f"{len(self.selected)} keys" + (": " + names[:60] + ("…" if len(names) > 60 else "") if names else " — click to select"))

    def changed(self):
        self.refresh()
        self.push_live()
        if self.saving:
            self.root.after_cancel(self.saving)
        self.saving = self.root.after(200, self.save)

    def lighting_values(self):
        return ({"enabled": self.enabled.get(), "notes": sorted(self.selected), "color": self.color,
                 "brightness": round(self.brightness.get() / 100, 3),
                 "note_colors": {str(n): self.note_colors[n] for n in sorted(self.selected)}},
                {"enabled": self.trail_enabled.get(), "duration_seconds": float(self.trail_seconds.get()),
                 "max_brightness": round(self.trail_brightness.get() / 100, 3)})

    def push_live(self):
        try:
            practice, trail = self.lighting_values()
            if not .25 <= trail["duration_seconds"] <= 30:
                return
            self.live_lighting.publish(practice, trail, self.paint_mode.get() == "erase")
        except ValueError:
            pass  # An incomplete spinbox edit must not interrupt live lighting.

    def save(self):
        self.saving = None
        try:
            practice, trail = self.lighting_values()
            config = save_config(self.path, practice, self.hand_colors, trail)
            self.status.set("Saved · Changes apply live" if config["live_reload"] else "Saved · Restart bridge to apply (live reload disabled)")
            return True
        except (OSError, ValueError) as error:
            self.status.set(f"Could not save: {error}")
            return False

    def pick_color(self):
        chosen = colorchooser.askcolor("#" + self.color, parent=self.root, title="Practice colour")[1]
        if chosen:
            self.set_brush(chosen[1:].upper())

    def pick_hand(self, hand):
        config = settings.load(self.path)
        rgb = settings.resolve_color(self.hand_colors[hand + "_color"], config["palette"])
        chosen = colorchooser.askcolor("#%02x%02x%02x" % rgb, parent=self.root, title=hand.title() + " hand colour")[1]
        if chosen:
            self.hand_colors[hand + "_color"] = chosen[1:].upper()
            self.changed()

    def start_bridge(self):
        if self.closing or (self.worker and self.worker.is_alive()) or not self.save():
            return
        from .cli import execute
        config = settings.load(self.path, required=True)
        args = copy(self.args)
        args.command = "bridge"
        settings.apply_startup(args, config, getattr(self.args, "startup_argv", ["bridge"]))
        # Preserve command-line routing options; the GUI edits only lighting.
        args.input, args.virtual_input = self.args.input, self.args.virtual_input
        args.practice = config["practice"]
        args.trail = config["trail"]
        args.practice_capture = self.capture
        args.practice_key_events = self.key_events
        args.live_lighting = self.live_lighting
        args.stop_event = self.stop = threading.Event()
        args.config_watcher = settings.ConfigWatcher(self.path, config, args, getattr(self.args, "startup_argv", ["bridge"]))
        def work():
            try:
                execute(args)
            except Exception as error:
                self.events.put(f"Bridge stopped: {error}")
            else:
                self.events.put("Bridge stopped · Lights cleared")
        self.worker = threading.Thread(target=work, name="partykeys-bridge", daemon=False)
        self.worker.start()
        self.toggle.configure(text="Stop bridge")
        self.status.set("Bridge running · Waiting for keyboard / Synthesia. Stop other bridge instances first.")

    def toggle_bridge(self):
        if self.worker and self.worker.is_alive():
            self.stop.set()
            self.toggle.configure(state="disabled")
            self.status.set("Stopping · Clearing lights…")
        else:
            self.start_bridge()

    def poll(self):
        while not self.key_events.empty():
            note = self.key_events.get()
            if self.capture.is_set():
                self.paint_note(note)
        while not self.events.empty():
            self.status.set(self.events.get())
        if self.worker and not self.worker.is_alive():
            self.toggle.configure(text="Start bridge", state="normal")
        if self.closing and (not self.worker or not self.worker.is_alive()):
            logging.getLogger("partykeys").removeHandler(self.log_handler)
            self.root.destroy()
            return
        self.root.after(16, self.poll)

    def close(self):
        if self.saving:
            self.root.after_cancel(self.saving)
            if not self.save():
                if not messagebox.askyesno("Unsaved changes", "Close without saving the latest changes?", parent=self.root):
                    return
        self.closing = True
        self.capture.clear()
        self.stop.set()
        self.toggle.configure(state="disabled")
        self.status.set("Closing · Clearing lights and releasing MIDI ports…")


def run(args):
    if getattr(args, "smoke_test", False):
        return smoke_test(args)
    root = tk.Tk()
    window = Window(root, args)
    try:
        root.mainloop()
    finally:
        window.stop.set()
        if window.worker:
            window.worker.join()
        logging.getLogger("partykeys").removeHandler(window.log_handler)
        try:
            root.destroy()
        except tk.TclError:
            pass


def smoke_test(args):
    """Launch the real frozen window on CI without touching MIDI or user settings."""
    failures = []
    with tempfile.TemporaryDirectory(prefix="partykeys-gui-") as folder:
        args = copy(args)
        args.config = str(Path(folder) / "config.json")
        Path(args.config).write_text(json.dumps(settings.DEFAULTS), encoding="utf-8")
        root = tk.Tk()
        window = Window(root, args, auto_start=False)
        root.geometry("740x650")  # Exercise the smallest supported layout.

        def fail(error):
            failures.append(str(error))
            root.destroy()

        root.report_callback_exception = lambda kind, error, tb: fail(error)

        def pointer(note, sequence, y=100):
            x1, _, x2, _ = window.canvas.coords(window.keys[note])
            x = round((x1 + x2) / 2 - window.canvas.canvasx(0))
            window.canvas.event_generate("<Motion>", x=x, y=y)
            window.canvas.event_generate(sequence, x=x, y=y)
            window.canvas.event_generate(sequence.replace("Button-", "ButtonRelease-"), x=x, y=y)

        def check_erase_and_close():
            try:
                assert 67 not in window.selected, "Physical-key erase mode was not handled"
                assert window.save(), "GUI settings did not save"
                saved = settings.load(args.config, required=True)
                assert saved["practice"]["note_colors"] == {"64": "FF0000"}
                assert saved["trail"] == dict(enabled=True, duration_seconds=12., max_brightness=.25)
                assert window.worker is None, "GUI smoke test opened a MIDI worker"
                window.close()
            except Exception as error:
                fail(error)

        def check_capture_and_close():
            try:
                assert 67 in window.selected, "Keyboard-paint event was not handled"
                assert window.note_colors[67] == "FF0000", "Keyboard-paint colour was not retained"
                window.paint_mode.set("erase")
                window.paint_note(60)
                assert 60 not in window.selected, "Erase did not remove the key"
                window.key_events.put(67)
                window.trail_enabled.set(True)
                window.trail_brightness.set(25)
                window.trail_seconds.set("12")
                window.changed()
                assert window.trail_percent.get() == "25%", "Trail brightness control did not update"
                root.after(100, check_erase_and_close)
            except Exception as error:
                fail(error)

        def exercise():
            try:
                assert root.winfo_viewable(), "GUI window did not become visible"
                assert window.canvas.winfo_width() > 100, "Keyboard canvas was not laid out"
                assert window.duration.winfo_ismapped(), "Trail controls were not laid out"
                assert window.swatch.winfo_ismapped(), "Custom colour button was not laid out"
                for button in window.presets.values():
                    assert button.winfo_ismapped(), "Preset colour button was not laid out"
                    assert button.winfo_x() + button.winfo_width() <= button.master.winfo_width(), "Preset colour button was clipped"
                    assert str(button.cget("image")), "Preset has no colour swatch"
                window.presets["Blue"].invoke()
                assert window.color == "0050FF", "Blue preset did not change the brush"
                window.paint_mode.set("erase")
                pointer(60, "<Button-1>")
                assert window.note_colors[60] == "0050FF", "Left-click did not paint in keyboard erase mode"
                pointer(61, "<Button-1>", y=40)
                assert 61 in window.selected, "Left-click did not paint a black key"
                window.paint_mode.set("paint")
                pointer(61, window.secondary_click, y=40)
                assert 61 not in window.selected, "Right-click did not erase a black key in keyboard paint mode"
                assert 60 in window.selected, "Erasing a black key also erased the white key underneath"
                if root.tk.call("tk", "windowingsystem") == "aqua":
                    pointer(60, "<Control-Button-1>")
                    assert 60 not in window.selected, "Mac Control-click did not erase"
                    pointer(60, "<Button-1>")
                window.presets["Red"].invoke()
                pointer(64, "<Button-1>")
                assert window.note_colors[60] == "0050FF", "Changing brush recoloured an existing key"
                window.enable_toggle.invoke()
                assert not window.enabled.get(), "Practice lights did not disable"
                assert set(window.selected) == {60, 64}, "Disabling practice lights lost the selection"
                window.enable_toggle.invoke()
                assert window.enabled.get(), "Practice lights did not enable"
                window.capture_enabled.set(True)
                window.set_capture()
                window.key_events.put(67)
                root.after(100, check_capture_and_close)
            except Exception as error:
                fail(error)

        root.after(150, exercise)
        root.after(10000, lambda: fail("GUI smoke test did not close within 10 seconds"))
        try:
            root.mainloop()
        finally:
            window.stop.set()
            logging.getLogger("partykeys").removeHandler(window.log_handler)
            try:
                root.destroy()
            except tk.TclError:
                pass
        if failures:
            raise RuntimeError("GUI smoke test failed: " + "; ".join(failures))
        print("PASS: GUI launched, controls painted/erased keys, keyboard capture and trail settings saved, window closed; no MIDI opened", flush=True)
