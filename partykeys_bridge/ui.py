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


def secondary_click_sequence(root):
    """Tk 8.7 normalized Aqua mouse numbering to match Windows and X11."""
    aqua = root.tk.call("tk", "windowingsystem") == "aqua"
    version = root.tk.call("package", "provide", "Tk")
    old_aqua = aqua and int(root.tk.call("package", "vcompare", version, "8.7a0")) < 0
    return "<Button-2>" if old_aqua else "<Button-3>"


class ColorButton(ttk.Button):
    """A real ttk button with a colourable face, including on macOS Aqua."""
    def __init__(self, master, color, **kwargs):
        self.styles = ttk.Style(master)
        if "PartyKeysColour.border" not in self.styles.element_names():
            # Aqua's native button face ignores background colours. Clone only
            # these drawing elements; leave the application's OS theme intact.
            for name, theme in (("border", "clam"), ("fill", "default"), ("focus", "default"),
                                ("padding", "default"), ("label", "default")):
                self.styles.element_create("PartyKeysColour." + name, "from", theme, name)
            self.styles.layout("PartyKeysColour.TButton", [
                ("PartyKeysColour.border", {"sticky": "nswe", "children": [
                    ("PartyKeysColour.fill", {"sticky": "nswe", "children": [
                        ("PartyKeysColour.focus", {"sticky": "nswe", "children": [
                            ("PartyKeysColour.padding", {"sticky": "nswe", "children": [
                                ("PartyKeysColour.label", {"sticky": "nswe"})]})]})]})]})])
        super().__init__(master, **kwargs)
        self.color_style = str(self) + ".PartyKeysColour.TButton"
        self.configure(style=self.color_style)
        self.set_color(color)

    @staticmethod
    def text_color(color):
        rgb = [int(color[i:i+2], 16) / 255 for i in (0, 2, 4)]
        linear = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in rgb]
        luminance = sum(c * weight for c, weight in zip(linear, (.2126, .7152, .0722)))
        return "#000000" if luminance > .179 else "#FFFFFF"

    def set_color(self, color):
        rgb = [int(color[i:i+2], 16) for i in (0, 2, 4)]
        hover = "".join(f"{round(c * .9):02X}" for c in rgb)
        pressed = "".join(f"{round(c * .75):02X}" for c in rgb)
        self.styles.configure(self.color_style, background="#" + color,
                              foreground=self.text_color(color), focuscolor=self.text_color(color),
                              bordercolor="#777777", lightcolor="#" + color, darkcolor="#" + color,
                              borderwidth=1, relief="solid", focusthickness=2, padding=(8, 5), anchor="center")
        self.styles.map(self.color_style,
                        background=[("pressed", "#" + pressed), ("active", "#" + hover)],
                        foreground=[("pressed", self.text_color(pressed)), ("active", self.text_color(hover))])


class StatusHandler(logging.Handler):
    def __init__(self, events):
        super().__init__(logging.INFO)
        self.events = events

    def emit(self, record):
        self.events.put(record.getMessage())


def save_config(path, practice, hand_colors, trail=None, bridge_brightness=None, bridge_enabled=None):
    """Preserve other settings and replace atomically so reload never sees half JSON."""
    path = Path(path)
    config = settings.load(path)
    config["practice"] = practice
    config["lighting"].update(hand_colors)
    if bridge_brightness is not None:
        config["lighting"]["bridge_brightness"] = bridge_brightness
    if bridge_enabled is not None:
        config["lighting"]["bridge_enabled"] = bridge_enabled
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
        self.held_events = queue.SimpleQueue()
        self.pressed = set()
        self.mouse_note = None
        self.stroke_erase = None
        self.stroke_notes = set()
        self.stroke_point = None
        self.live_lighting = LiveLighting()
        self.trail_enabled = tk.BooleanVar(value=config["trail"]["enabled"])
        self.trail_seconds = tk.StringVar(value=str(config["trail"]["duration_seconds"]))
        self.trail_brightness = tk.DoubleVar(value=config["trail"]["max_brightness"] * 100)
        self.trail_percent = tk.StringVar()
        self.bridge_brightness = tk.DoubleVar(value=config["lighting"]["bridge_brightness"] * 100)
        self.bridge_percent = tk.StringVar()
        self.bridge_enabled = tk.BooleanVar(value=config["lighting"]["bridge_enabled"])
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
        title_row = ttk.Frame(frame)
        title_row.pack(fill="x")
        ttk.Label(title_row, text="Practice lights", font=("", 18, "bold")).pack(side="left")
        ttk.Label(title_row, text="Sunken keys are pressed").pack(side="right")
        ttk.Label(frame, text="Choose Paint or Erase, then click or drag across keys. Right-click does the opposite. Synthesia cues appear over practice lights.",
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
            button = ColorButton(row, value, text=name, width=0,
                                 command=lambda c=value: self.set_brush(c))
            button.grid(row=0, column=index, sticky="ew", padx=2, pady=2)
            self.presets[name] = button
        for column in range(8):
            row.columnconfigure(column, weight=1, uniform="colours")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=5)
        ttk.Label(row, text="Mode:").pack(side="left", padx=(0, 8))
        self.paint_radio = ttk.Radiobutton(row, text="Paint", variable=self.paint_mode, value="paint", command=self.push_live)
        self.paint_radio.pack(side="left")
        self.erase_radio = ttk.Radiobutton(row, text="Erase", variable=self.paint_mode, value="erase", command=self.push_live)
        self.erase_radio.pack(side="left", padx=10)
        ttk.Checkbutton(row, text="Use physical keyboard to paint / erase", variable=self.capture_enabled,
                        command=self.set_capture).pack(side="left", padx=10)
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=10)
        ttk.Label(row, text="Practice brightness").pack(side="left")
        ttk.Scale(row, from_=0, to=100, variable=self.brightness, command=lambda _: self.changed()).pack(side="left", fill="x", expand=True, padx=12)
        ttk.Label(row, textvariable=self.percent, width=5).pack(side="right")
        self.canvas = tk.Canvas(frame, height=132, background="#e6e8ec", highlightthickness=0)
        self.canvas.pack(fill="x")
        scroll = ttk.Scrollbar(frame, orient="horizontal", command=self.canvas.xview)
        scroll.pack(fill="x")
        self.canvas.configure(xscrollcommand=scroll.set)
        self.keys = {}
        self.key_labels = {}
        self.held_marks = {}
        self.draw_keyboard()
        self.canvas.bind("<Button-1>", self.click)
        aqua = root.tk.call("tk", "windowingsystem") == "aqua"
        self.secondary_click = secondary_click_sequence(root)
        self.canvas.bind(self.secondary_click, lambda event: self.click(event, invert=True))
        for sequence in ("<B1-Motion>", self.secondary_click.replace("Button-", "B").replace(">", "-Motion>")):
            self.canvas.bind(sequence, self.drag)
        for sequence in ("<ButtonRelease-1>", self.secondary_click.replace("Button-", "ButtonRelease-")):
            self.canvas.bind(sequence, self.release_click)
        root.bind("<FocusOut>", self.release_click, add=True)
        if aqua:
            self.canvas.bind("<Control-Button-1>", lambda event: self.click(event, invert=True))
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
        row.pack(fill="x", pady=(8, 0))
        self.bridge_enable_toggle = ttk.Checkbutton(row, text="Enable bridge lights", variable=self.bridge_enabled,
                                                    command=self.changed)
        self.bridge_enable_toggle.pack(side="left")
        ttk.Label(row, text="Brightness").pack(side="left", padx=(12, 0))
        self.bridge_scale = ttk.Scale(row, from_=0, to=100, variable=self.bridge_brightness,
                                     command=lambda _: self.changed())
        self.bridge_scale.pack(side="left", fill="x", expand=True, padx=12)
        ttk.Label(row, textvariable=self.bridge_percent, width=5).pack(side="right")
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
        self.footer = ttk.Label(frame, text="In Synthesia: Music Output → Synthesia Lights → Key Lights → Finger-based channel.", wraplength=720)
        self.footer.pack(anchor="w", pady=(5, 0))
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
            self.key_labels[note] = self.canvas.create_text(x + 15, 114, text=note_name(note), font=("", 8), fill="#14161b", tags=(f"note:{note}",))
            self.draw_key_marks(note, x, 30, 130)
            white += 1
        white = 0
        for note in range(128):
            if note % 12 not in (1, 3, 6, 8, 10):
                white += 1
                continue
            x = white * 30 - 10
            self.keys[note] = self.canvas.create_rectangle(x, 0, x + 20, 78, fill="#252932", outline="#14161b", tags=(f"note:{note}",))
            self.draw_key_marks(note, x, 20, 78)
        self.canvas.configure(scrollregion=(0, 0, white * 30, 132))

    def draw_key_marks(self, note, x, width, height):
        tags = (f"note:{note}",)
        # Inset shadows and a lower face make a held key look physically down.
        self.held_marks[note] = (
            self.canvas.create_rectangle(x + 1, 0, x + width - 1, 7,
                                         width=0, state="hidden", tags=tags),
            self.canvas.create_line(x + 2, height - 2, x + 2, 8, x + width - 2, 8,
                                    width=2, state="hidden", tags=tags),
            self.canvas.create_line(x + 3, height - 2, x + width - 2, height - 2,
                                    width=2, state="hidden", tags=tags))

    @property
    def held_notes(self):
        return {note for _, note in self.pressed}

    def reveal_note(self, note):
        x1, _, x2, _ = self.canvas.coords(self.keys[note])
        left, width = self.canvas.canvasx(0), self.canvas.winfo_width()
        total = float(self.canvas.cget("scrollregion").split()[2])
        if x1 < left:
            self.canvas.xview_moveto(max(0, x1 - 30) / total)
        elif x2 > left + width:
            self.canvas.xview_moveto(max(0, x2 - width + 30) / total)

    def held_event(self, message):
        if message is None or message.type in ("reset", "stop"):
            self.pressed.clear()
        elif message.type == "control_change" and message.control in (120, 123):
            self.pressed = {key for key in self.pressed if key[0] != message.channel}
        elif message.type == "note_on" and message.velocity:
            self.pressed.add((message.channel, message.note))
            self.reveal_note(message.note)
        elif message.type in ("note_on", "note_off"):
            self.pressed.discard((message.channel, message.note))

    def note_at(self, x, y):
        # Only keys inside the visible canvas can be painted by a drag.
        if not 0 <= x < self.canvas.winfo_width() or not 0 <= y < self.canvas.winfo_height():
            return None
        x, y = self.canvas.canvasx(x), self.canvas.canvasy(y)
        for item in reversed(self.canvas.find_overlapping(x, y, x, y)):
            for tag in self.canvas.gettags(item):
                if tag.startswith("note:"):
                    return int(tag.split(":")[1])
        return None

    def click(self, event, invert=False):
        self.stroke_erase = (self.paint_mode.get() == "erase") != invert
        self.stroke_notes.clear()
        self.stroke_point = (event.x, event.y)
        return self.drag(event)

    def drag(self, event):
        if self.stroke_erase is None:
            return "break"
        x, y = self.stroke_point
        dx, dy = event.x - x, event.y - y
        # Cover the path between motion events so fast drags don't skip keys.
        steps = max(1, int(max(abs(dx), abs(dy)) / 4) + 1)
        changed = False
        for step in range(steps + 1):
            note = self.note_at(x + dx * step / steps, y + dy * step / steps)
            if note is not None and note not in self.stroke_notes:
                self.stroke_notes.add(note)
                self.paint_note(note, erase=self.stroke_erase, notify=False)
                changed = True
        previous = self.mouse_note
        self.mouse_note = self.note_at(event.x, event.y)
        self.stroke_point = (event.x, event.y)
        if changed:
            self.changed()
        elif self.mouse_note != previous:
            self.refresh()
        return "break"

    def release_click(self, event=None):
        self.stroke_erase = None
        self.stroke_notes.clear()
        self.stroke_point = None
        if self.mouse_note is not None:
            self.mouse_note = None
            self.refresh()

    def paint_note(self, note, erase=None, notify=True):
        if erase is None:
            erase = self.paint_mode.get() == "erase"
        if erase:
            self.selected.discard(note)
            self.note_colors.pop(note, None)
        else:
            self.selected.add(note)
            self.note_colors[note] = self.color
            self.enabled.set(True)
        if notify:
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
        held = self.held_notes
        for note, item in self.keys.items():
            selected = note in self.selected
            down = note in held or note == self.mouse_note
            fill = self.note_colors.get(note, self.color) if selected else ("252932" if note % 12 in (1, 3, 6, 8, 10) else "FFFFFF")
            rgb = [int(fill[i:i+2], 16) for i in (0, 2, 4)]
            if down:
                rgb = [round(c * .72) for c in rgb]
            fill = "#" + "".join(f"{c:02X}" for c in rgb)
            foreground = ColorButton.text_color(fill[1:])
            x1, _, x2, height = self.canvas.coords(item)
            self.canvas.coords(item, x1, 4 if down else 0, x2, height)
            self.canvas.itemconfigure(item, fill=fill, width=2 if selected else 1,
                                      outline=foreground if selected else ("#14161b" if note % 12 in (1, 3, 6, 8, 10) else "#a8adb5"))
            if note in self.key_labels:
                self.canvas.itemconfigure(self.key_labels[note], fill=foreground)
                self.canvas.coords(self.key_labels[note], (x1 + x2) / 2, height - 16 + (3 if down else 0))
            shadow = "#" + "".join(f"{round(c * .35):02X}" for c in rgb)
            edge = "#" + "".join(f"{round(c + (255 - c) * .35):02X}" for c in rgb)
            self.canvas.itemconfigure(self.held_marks[note][0], fill=shadow)
            self.canvas.itemconfigure(self.held_marks[note][1], fill=shadow)
            self.canvas.itemconfigure(self.held_marks[note][2], fill=edge)
            for marker in self.held_marks[note]:
                self.canvas.itemconfigure(marker, state="normal" if down else "hidden")
        self.swatch.set_color(self.color)
        self.percent.set(f"{round(self.brightness.get())}%")
        self.bridge_percent.set(f"{round(self.bridge_brightness.get())}%")
        self.trail_percent.set(f"{round(self.trail_brightness.get())}%")
        names = ", ".join(note_name(n) for n in sorted(held or self.selected))
        prefix = f"{len(self.selected)} selected" + (f" · {len(held)} held" if held else "")
        self.summary.set(prefix + (": " + names[:50] + ("…" if len(names) > 50 else "") if names else " — click to select"))

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
            self.live_lighting.publish(practice, trail, self.paint_mode.get() == "erase",
                                       bridge_brightness=round(self.bridge_brightness.get() / 100, 3),
                                       bridge_enabled=self.bridge_enabled.get())
        except ValueError:
            pass  # An incomplete spinbox edit must not interrupt live lighting.

    def save(self):
        self.saving = None
        try:
            practice, trail = self.lighting_values()
            config = save_config(self.path, practice, self.hand_colors, trail,
                                 bridge_brightness=round(self.bridge_brightness.get() / 100, 3),
                                 bridge_enabled=self.bridge_enabled.get())
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
        args.physical_key_events = self.held_events
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

    def drain_key_events(self):
        previous = set(self.pressed)
        while not self.held_events.empty():
            self.held_event(self.held_events.get())
        while not self.key_events.empty():
            note = self.key_events.get()
            if self.capture.is_set():
                self.paint_note(note)
        if self.pressed != previous:
            self.refresh()

    def poll(self):
        self.drain_key_events()
        while not self.events.empty():
            self.status.set(self.events.get())
        if self.worker and not self.worker.is_alive():
            if self.pressed:
                self.pressed.clear()
                self.refresh()
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
            window.canvas.event_generate(sequence, x=x, y=y)
            if sequence in ("<Button-1>", window.secondary_click, "<Control-Button-1>"):
                assert window.canvas.coords(window.keys[note])[1] == 4, "Mouse-pressed key did not sink"
            window.canvas.event_generate(sequence.replace("Button-", "ButtonRelease-"), x=x, y=y)
            assert window.mouse_note is None, "Mouse release retained a pressed key"
            assert window.canvas.coords(window.keys[note])[1] == 0, "Mouse-released key stayed sunken"

        def stroke(first, last, sequence="<Button-1>", y=100):
            def key_x(note):
                x1, _, x2, _ = window.canvas.coords(window.keys[note])
                return round((x1 + x2) / 2 - window.canvas.canvasx(0))
            x1, x2 = key_x(first), key_x(last)
            motion = sequence.replace("Button-", "B").replace(">", "-Motion>")
            window.canvas.event_generate(sequence, x=x1, y=y)
            # A single jump must cover all intervening keys, like a fast drag.
            window.canvas.event_generate(motion, x=x2, y=y)
            assert window.mouse_note == last, "Drag did not depress the current key"
            window.canvas.event_generate(sequence.replace("Button-", "ButtonRelease-"), x=x2, y=y)
            assert window.stroke_erase is None and window.mouse_note is None, "Release did not end the stroke"
            before = set(window.selected)
            window.canvas.event_generate(motion, x=key_x(69), y=y)
            assert window.selected == before, "Motion after release continued painting"

        def check_erase_and_close():
            try:
                assert 67 not in window.selected, "Physical-key erase mode was not handled"
                window.bridge_enable_toggle.invoke()
                assert not window.bridge_enabled.get(), "Bridge lights did not disable"
                assert not window.live_lighting.snapshot()[1][4], "Bridge disable was not published live"
                assert window.save(), "GUI settings did not save"
                saved = settings.load(args.config, required=True)
                assert saved["practice"]["note_colors"] == {"64": "FF0000"}
                assert saved["trail"] == dict(enabled=True, duration_seconds=12., max_brightness=.25)
                assert saved["lighting"]["bridge_brightness"] == .4, "Bridge brightness did not save"
                assert not saved["lighting"]["bridge_enabled"], "Bridge enable preference did not save"
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
                window.bridge_brightness.set(40)
                window.trail_seconds.set("12")
                window.changed()
                assert window.trail_percent.get() == "25%", "Trail brightness control did not update"
                assert window.bridge_percent.get() == "40%", "Bridge brightness control did not update"
                assert window.live_lighting.snapshot()[1][3] == .4, "Bridge brightness was not published live"
                root.after(100, check_erase_and_close)
            except Exception as error:
                fail(error)

        def exercise():
            try:
                assert root.winfo_viewable(), "GUI window did not become visible"
                assert window.canvas.winfo_width() > 100, "Keyboard canvas was not laid out"
                assert window.duration.winfo_ismapped(), "Trail controls were not laid out"
                assert window.bridge_scale.winfo_ismapped(), "Bridge brightness control was not laid out"
                assert window.footer.winfo_rooty() + window.footer.winfo_height() <= root.winfo_rooty() + root.winfo_height(), "Brightness row clipped the window footer"
                assert window.swatch.winfo_ismapped(), "Custom colour button was not laid out"
                for button in window.presets.values():
                    assert button.winfo_ismapped(), "Preset colour button was not laid out"
                    assert button.winfo_x() + button.winfo_width() <= button.master.winfo_width(), "Preset colour button was clipped"
                    assert button.winfo_width() >= button.winfo_reqwidth(), "Preset label was clipped"
                    assert not str(button.cget("image")), "Preset still uses a swatch image"
                    assert "PartyKeysColour.fill" in str(button.styles.layout(button.color_style)), "Preset has no face-filling element"
                    assert window.swatch.styles.lookup(button.color_style, "background").startswith("#"), "Preset has no coloured face"
                window.presets["Blue"].invoke()
                assert window.color == "0050FF", "Blue preset did not change the brush"
                window.paint_radio.invoke()
                stroke(60, 67)
                white_notes = {60, 62, 64, 65, 67}
                assert window.selected == white_notes, "Fast white-key drag skipped keys or painted black keys"
                window.erase_radio.invoke()
                stroke(67, 60)
                assert not window.selected, "Erase drag did not remove all crossed keys"
                stroke(60, 67, window.secondary_click)
                assert window.selected == white_notes and window.paint_mode.get() == "erase", "Right-drag did not invert Erase"
                window.paint_radio.invoke()
                stroke(67, 60, window.secondary_click)
                assert not window.selected, "Right-drag did not invert Paint"
                stroke(60, 64, y=40)
                assert window.selected == set(range(60, 65)), "Drag did not follow black and white key hit-testing"
                if root.tk.call("tk", "windowingsystem") == "aqua":
                    stroke(64, 60, "<Control-Button-1>", y=40)
                    assert not window.selected, "Mac Control-drag did not erase"
                else:
                    stroke(64, 60, window.secondary_click, y=40)
                # Losing focus also ends a stroke before any later motion.
                x1, _, x2, _ = window.canvas.coords(window.keys[60])
                x = round((x1 + x2) / 2 - window.canvas.canvasx(0))
                window.canvas.event_generate("<Button-1>", x=x, y=100)
                root.event_generate("<FocusOut>")
                assert window.stroke_erase is None and window.mouse_note is None, "Focus loss retained a drag stroke"
                window.clear()
                pointer(60, "<Button-1>")
                assert window.note_colors[60] == "0050FF", "Left-click did not follow Paint mode"
                pointer(61, "<Button-1>", y=40)
                assert 61 in window.selected, "Left-click did not paint a black key"
                pointer(61, window.secondary_click, y=40)
                assert 61 not in window.selected, "Right-click did not erase in Paint mode"
                assert 60 in window.selected, "Erasing a black key also erased the white key underneath"
                assert window.paint_mode.get() == "paint", "Right-click changed the selected mode"
                middle = "<Button-2>" if window.secondary_click == "<Button-3>" else "<Button-3>"
                pointer(61, middle, y=40)
                assert 61 not in window.selected and 60 in window.selected, "Middle-click edited the selection"
                window.erase_radio.invoke()
                pointer(60, "<Button-1>")
                assert 60 not in window.selected, "Left-click did not follow Erase mode"
                pointer(60, window.secondary_click)
                assert window.note_colors[60] == "0050FF", "Right-click did not paint in Erase mode"
                assert window.paint_mode.get() == "erase", "Right-click changed the selected mode"
                pointer(60, "<Button-1>")
                if root.tk.call("tk", "windowingsystem") == "aqua":
                    pointer(60, "<Control-Button-1>")
                    assert 60 in window.selected, "Mac Control-click did not paint in Erase mode"
                    pointer(60, "<Button-1>")
                    window.paint_radio.invoke()
                    pointer(60, "<Control-Button-1>")
                    assert 60 not in window.selected, "Mac Control-click did not erase in Paint mode"
                window.paint_radio.invoke()
                pointer(60, "<Button-1>")
                window.presets["Red"].invoke()
                pointer(64, "<Button-1>")
                assert window.note_colors[60] == "0050FF", "Changing brush recoloured an existing key"
                window.enable_toggle.invoke()
                assert not window.enabled.get(), "Practice lights did not disable"
                assert set(window.selected) == {60, 64}, "Disabling practice lights lost the selection"
                window.enable_toggle.invoke()
                assert window.enabled.get(), "Practice lights did not enable"
                # Physical key display remains active while paint capture is off.
                import mido
                from .practice import physical_key_event
                events_args = copy(args)
                events_args.physical_key_events = window.held_events
                def physical(message):
                    physical_key_event(events_args, message)
                    window.drain_key_events()
                physical(mido.Message("note_on", note=60, channel=0))
                physical(mido.Message("note_on", note=60, channel=1))
                physical(mido.Message("note_on", note=61))
                assert window.held_notes == {60, 61}, "Unarmed physical presses were not highlighted"
                assert window.selected == {60, 64}, "Unarmed physical presses painted the selection"
                assert window.canvas.itemcget(window.keys[60], "fill") == "#003AB8", "Pressed key did not shade its saved colour"
                assert window.canvas.coords(window.keys[60])[1] == 4, "Pressed key face did not sink"
                assert window.canvas.coords(window.key_labels[60])[1] == 117, "Pressed key label did not move down"
                assert window.canvas.itemcget(window.held_marks[61][1], "state") == "normal", "Black pressed key has no inset shadow"
                window.bridge_enable_toggle.invoke()
                assert not window.bridge_enabled.get(), "Bridge lights did not disable"
                assert window.canvas.coords(window.keys[60])[1] == 4, "Bridge disable hid physical GUI feedback"
                assert window.selected == {60, 64}, "Bridge disable changed practice selection"
                window.bridge_enable_toggle.invoke()
                assert window.bridge_enabled.get(), "Bridge lights did not enable"
                physical(mido.Message("note_off", note=60, channel=0))
                assert 60 in window.held_notes, "Releasing one channel released another channel's held key"
                physical(mido.Message("note_on", note=60, channel=1, velocity=0))
                assert 60 not in window.held_notes, "Velocity-zero note-on did not release the highlight"
                assert window.canvas.itemcget(window.held_marks[60][1], "state") == "hidden", "Released key retained pressed shadow"
                assert window.canvas.itemcget(window.keys[60], "fill") == "#0050FF", "Release did not restore the saved colour"
                assert window.canvas.coords(window.keys[60])[1] == 0, "Released key face stayed sunken"
                assert window.canvas.coords(window.key_labels[60])[1] == 114, "Released key label stayed down"
                physical(mido.Message("control_change", control=123))
                assert not window.held_notes, "All-notes-off retained held keys"
                physical(mido.Message("note_on", note=24))
                x1, _, x2, _ = window.canvas.coords(window.keys[24])
                assert window.canvas.canvasx(0) <= x1 < x2 <= window.canvas.canvasx(window.canvas.winfo_width()), "Played key was not revealed"
                physical(None)
                assert not window.held_notes, "Disconnected input retained held keys"
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
