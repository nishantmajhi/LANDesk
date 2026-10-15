"""
LANDesk - Server GUI
Compiled into LANDesk-Server.exe via PyInstaller.
"""

import sys, os, socket, threading, asyncio, importlib.util, logging, json
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox

if getattr(sys, 'frozen', False):
    base = sys._MEIPASS
else:
    base = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, base)
sys.path.insert(0, os.path.join(base, 'shared'))

# ---------------------------------------------------------------------------
# Colour tokens
# ---------------------------------------------------------------------------
C = {
    "bg":       "#ffffff",
    "surf1":    "#f7f8fa",
    "surf2":    "#eef0f4",
    "text":     "#14161a",   # near-black, cool-toned for all labels - high contrast
    "text2":    "#4a4f59",
    "text3":    "#8a8f9c",   # only for hints/micro labels
    "border":   "#e6e8ed",
    "border2":  "#c9ced9",
    "acc":      "#4f46e5",   # indigo - used for IP chip text/border and focus rings
    "acc_dark": "#3f37c9",
    "suc":      "#16a34a",   # green - used for Start server / primary buttons
    "suc_dark": "#0f7c39",
    "suc_bg":   "#eafbf1",
    "suc_bd":   "#bfe8cf",
    "suc_txt":  "#0f6b34",
    "card_grn_bg": "#f7fdf9",   # IP card panel - subtle green, quieter than suc_bg
    "card_grn_bd": "#d6ecdf",   # IP card border - soft green, quieter than suc
    "dan":      "#dc2626",
    "dan_dark": "#b91c1c",
    "dan_bg":   "#fdecec",
    "dan_bd":   "#f3c1c1",
    "log_bg":   "#f7f8fa",
}

FONT        = ("Segoe UI", 10)
FONT_BOLD   = ("Segoe UI Semibold", 10)
FONT_SMALL  = ("Segoe UI", 9)
FONT_MONO   = ("Cascadia Mono", 10)
FONT_MICRO  = ("Segoe UI", 8)
FONT_HEADER = ("Segoe UI Semibold", 13)

# Max width in pixels for Port and Password inputs in server
INPUT_MAX_W = 320   # comfortably wide but not full-width

# Shared control height so inputs and buttons line up visually
RADIUS      = 10
CTRL_HEIGHT = 38


def _resolve_fonts():
    """Fall back to guaranteed-present fonts if the preferred ones aren't
    installed on this machine. Must run after a Tk root exists."""
    global FONT_BOLD, FONT_MONO, FONT_HEADER
    families = set(tkfont.families())
    if "Segoe UI Semibold" not in families:
        FONT_BOLD   = ("Segoe UI", 10, "bold")
        FONT_HEADER = ("Segoe UI", 13, "bold")
    if "Cascadia Mono" not in families:
        FONT_MONO = ("Consolas", 10)


def get_lan_ips():
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if ip.startswith(('192.168.', '10.', '172.')) and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips or ['(not detected)']


# ---------------------------------------------------------------------------
# Settings persistence - remembers last-used values between launches
# ---------------------------------------------------------------------------
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".landesk", "server_settings.json")


def load_settings():
    try:
        with open(SETTINGS_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data):
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def _round_rect_points(x1, y1, x2, y2, r):
    return [
        x1 + r, y1,   x2 - r, y1,
        x2,     y1,   x2,     y1 + r,
        x2,     y2 - r, x2,   y2,
        x2 - r, y2,   x1 + r, y2,
        x1,     y2,   x1,     y2 - r,
        x1,     y1 + r, x1,   y1,
    ]


# ---------------------------------------------------------------------------
# Rounded-corner frame (drawn with Canvas) - use `.inner` as the content parent
# ---------------------------------------------------------------------------
class RoundedFrame(tk.Canvas):
    def __init__(self, parent, radius=RADIUS, bg="#ffffff", border="#e2e2df",
                 bg_outer=None, **kw):
        bg_outer = bg_outer if bg_outer is not None else parent["bg"]
        super().__init__(parent, bg=bg_outer, highlightthickness=0, bd=0, **kw)
        self._r       = radius
        self._fill    = bg
        self._border  = border
        self._rect    = None
        self.inner = tk.Frame(self, bg=bg)
        self._win  = self.create_window(0, 0, window=self.inner, anchor="nw")
        self._last_wh = None
        self.bind("<Configure>", self._redraw)

    def _redraw(self, _=None):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4 or h < 4:
            return
        if (w, h) == self._last_wh:
            return
        self._last_wh = (w, h)
        if self._rect:
            self.delete(self._rect)
        r = min(self._r, h // 2, w // 2)
        self._rect = self.create_polygon(
            *_round_rect_points(1, 1, w - 1, h - 1, r),
            smooth=True, fill=self._fill, outline=self._border, width=1)
        self.tag_lower(self._rect)
        self.coords(self._win, 1, 1)
        self.itemconfigure(self._win, width=w - 2, height=h - 2)


# ---------------------------------------------------------------------------
# Rounded button - Canvas-drawn, same external API as the old Label version
# (constructor, .configure(text=...), .set_style(...))
# ---------------------------------------------------------------------------
class RoundedButton(tk.Canvas):
    def __init__(self, parent, text, command, style="ghost",
                 font=FONT, height=CTRL_HEIGHT, radius=RADIUS, pad_x=16, **kw):
        bg_outer = kw.pop("bg", None) or parent["bg"]
        super().__init__(parent, bg=bg_outer, highlightthickness=0, bd=0,
                          height=height, cursor="hand2", takefocus=1, **kw)
        self._cmd     = command
        self._font    = font
        self._text    = text
        self._radius  = radius
        self._pad_x   = pad_x
        self._height  = height
        self._rect    = None
        self._txt_id  = None
        self._last_wh = None
        self._focused = False
        self.set_style(style)
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>",  lambda e: (self.focus_set(), self._fire()))
        self.bind("<Enter>",     self._on_enter)
        self.bind("<Leave>",     self._on_leave)
        self.bind("<Return>",    lambda e: self._fire())
        self.bind("<space>",     lambda e: self._fire())
        self.bind("<FocusIn>",   self._on_focus_in)
        self.bind("<FocusOut>",  self._on_focus_out)
        self.after(0, self._draw)

    def _measure_width(self):
        f = tkfont.Font(font=self._font)
        return f.measure(self._text) + self._pad_x * 2

    def _draw(self, force=False):
        w = max(self.winfo_width(), self._measure_width(), 1)
        h = self._height
        if not force and (w, h) == self._last_wh:
            return
        self._last_wh = (w, h)
        self.delete("all")
        r = min(self._radius, h // 2)
        outline, lw = (C["acc"], 2) if self._focused else (self._bd, 1)
        self._rect = self.create_polygon(
            *_round_rect_points(1, 1, w - 1, h - 1, r),
            smooth=True, fill=self._bg, outline=outline, width=lw)
        self._txt_id = self.create_text(w // 2, h // 2, text=self._text,
                                         fill=self._fg, font=self._font)
        self.configure(width=w, height=h)

    def _fire(self):
        if self._style != "ghost-disabled":
            self._cmd()

    def set_style(self, style):
        self._style = style
        styles = {
            "primary":        (C["suc"],     C["suc_dark"], "#ffffff", C["suc"]),
            "danger":         (C["dan"],     C["dan_dark"], "#ffffff", C["dan"]),
            "ghost":          (C["surf2"],   C["surf1"],    C["text2"], C["border2"]),
            "ghost-danger":   (C["dan_bg"],  "#f9d6d3",     C["dan_dark"], C["dan_bd"]),
            "ghost-disabled": (C["surf2"],   C["surf2"],    C["text3"], C["border"]),
        }
        bg, bg_h, fg, bd = styles.get(style, styles["ghost"])
        self._bg, self._bg_h, self._fg, self._bd = bg, bg_h, fg, bd
        self._draw(force=True)

    def _on_enter(self, _):
        if self._style != "ghost-disabled" and self._rect:
            self.itemconfigure(self._rect, fill=self._bg_h)

    def _on_leave(self, _):
        if self._rect:
            self.itemconfigure(self._rect, fill=self._bg)

    def _on_focus_in(self, _):
        self._focused = True
        self._draw(force=True)

    def _on_focus_out(self, _):
        self._focused = False
        self._draw(force=True)

    def configure(self, **kw):
        if "text" in kw:
            self._text = kw.pop("text")
            self._draw(force=True)
        if kw:
            super().configure(**kw)

    config = configure


# ---------------------------------------------------------------------------
# Rounded entry - Canvas-drawn container hosting a borderless tk.Entry
# ---------------------------------------------------------------------------
class RoundedEntry(tk.Canvas):
    def __init__(self, parent, textvariable, show="", font=FONT,
                 radius=RADIUS, height=CTRL_HEIGHT, width=160,
                 bg_fill=None, border=None, focus_border=None,
                 bg_outer=None, justify="left", show_toggle=False, **kw):
        bg_outer     = bg_outer if bg_outer is not None else parent["bg"]
        bg_fill      = bg_fill or C["bg"]
        border       = border or C["border2"]
        focus_border = focus_border or C["acc"]
        super().__init__(parent, bg=bg_outer, highlightthickness=0, bd=0,
                          width=width, height=height, **kw)
        self._r            = radius
        self._fill         = bg_fill
        self._border       = border
        self._focus_border = focus_border
        self._rect         = None
        self._show_char    = show
        self._revealed     = False

        self.entry = tk.Entry(
            self, textvariable=textvariable, show=show, font=font,
            bg=bg_fill, fg=C["text"], insertbackground=C["text"],
            relief="flat", bd=0, highlightthickness=0, justify=justify)
        self._win = self.create_window(0, 0, window=self.entry, anchor="w")

        self._toggle_lbl = None
        self._toggle_win = None
        if show_toggle:
            self._toggle_lbl = tk.Label(self, text="Show", font=FONT_MICRO,
                                         fg=C["acc"], bg=bg_fill, cursor="hand2")
            self._toggle_win = self.create_window(0, 0, window=self._toggle_lbl, anchor="e")
            self._toggle_lbl.bind("<Button-1>", self._toggle_reveal)

        self._last_wh = None
        self.bind("<Configure>", self._redraw)
        self.entry.bind("<FocusIn>",  lambda e: self._border_color(self._focus_border))
        self.entry.bind("<FocusOut>", lambda e: self._border_color(self._border))

    def _toggle_reveal(self, _):
        self._revealed = not self._revealed
        self.entry.configure(show="" if self._revealed else self._show_char)
        self._toggle_lbl.configure(text="Hide" if self._revealed else "Show")

    def _redraw(self, _=None):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4 or h < 4:
            return
        if (w, h) == self._last_wh:
            return
        self._last_wh = (w, h)
        if self._rect:
            self.delete(self._rect)
        r = min(self._r, h // 2)
        self._rect = self.create_polygon(
            *_round_rect_points(1, 1, w - 1, h - 1, r),
            smooth=True, fill=self._fill, outline=self._border, width=1)
        self.tag_lower(self._rect)
        pad_x = 10
        toggle_w = 0
        if self._toggle_lbl is not None:
            toggle_w = self._toggle_lbl.winfo_reqwidth() + 8
            self.coords(self._toggle_win, w - pad_x, h // 2)
        self.coords(self._win, pad_x, h // 2)
        self.itemconfigure(self._win, width=max(1, w - 2 * pad_x - toggle_w))

    def _border_color(self, color):
        if self._rect:
            self.itemconfigure(self._rect, outline=color)


# ---------------------------------------------------------------------------
# Pill widget - oval dot + label, proper circle dot via Canvas
# ---------------------------------------------------------------------------
class Pill(tk.Frame):
    def __init__(self, parent, **kw):
        bg = kw.pop("bg", C["surf2"])
        super().__init__(parent, bg=bg, **kw)
        self._pill_bg = bg

        self._dot_cv = tk.Canvas(self, width=10, height=10,
                                  bg=bg, highlightthickness=0)
        self._dot_cv.pack(side="left", padx=(0, 5))
        self._oval = self._dot_cv.create_oval(1, 1, 9, 9,
                                               fill=C["text3"], outline="")

        self._lbl = tk.Label(self, font=FONT_SMALL, bg=bg)
        self._lbl.pack(side="left")
        self.set("off", "Stopped")

    def set(self, state, text):
        if state == "on":
            dot_fill = C["suc"]
            fg       = C["suc_txt"]
        else:
            dot_fill = C["text3"]
            fg       = C["text3"]
        self._dot_cv.itemconfigure(self._oval, fill=dot_fill)
        self._lbl.configure(text=text, fg=fg)


# ---------------------------------------------------------------------------
# Server GUI
# ---------------------------------------------------------------------------
class ServerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        _resolve_fonts()
        self.title("LANDesk")
        self.configure(bg=C["bg"])
        self.resizable(True, True)
        self.minsize(500, 480)
        self._set_icon()
        self._server_running = False
        self._client_count = 0
        self._build_ui()
        self._load_saved_settings()
        self._center()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_icon(self):
        try:
            self.iconbitmap(default=os.path.join(base, "assets", "icon.ico"))
        except Exception:
            pass
        try:
            self._icon_img = tk.PhotoImage(file=os.path.join(base, "assets", "icon.png"))
            self.iconphoto(True, self._icon_img)
        except Exception:
            pass

    def _center(self):
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

    def _on_close(self):
        if self._server_running:
            if not messagebox.askyesno(
                    "Quit LANDesk Server",
                    "The server is still running and a viewer may be connected.\n\n"
                    "Stop the server and quit?"):
                return
        self._save_current_settings()
        import os as _os
        _os._exit(0)

    def _load_saved_settings(self):
        s = load_settings()
        if "port" in s:
            self._port_var.set(s["port"])
        if "password" in s:
            self._pw_var.set(s["password"])
        if "quality" in s:
            self._quality_var.set(s["quality"])
        if "fps" in s:
            self._fps_var.set(s["fps"])

    def _save_current_settings(self):
        save_settings({
            "port":     self._port_var.get(),
            "password": self._pw_var.get(),
            "quality":  self._quality_var.get(),
            "fps":      self._fps_var.get(),
        })

    def _copy_ip(self, ip, lbl):
        if ip.startswith("("):
            return
        self.clipboard_clear()
        self.clipboard_append(ip)
        original = lbl.cget("text")
        lbl.configure(text="Copied!")
        self.after(900, lambda: lbl.configure(text=original))

    def _build_ui(self):
        # ----------------------------------------------------------------
        # Top bar: title | pill | Start button
        # ----------------------------------------------------------------
        topbar = tk.Frame(self, bg=C["surf2"])
        topbar.pack(fill="x")
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")

        tk.Label(topbar, text="LANDesk - Server",
                 font=FONT_HEADER,
                 fg=C["text"], bg=C["surf2"]
                 ).pack(side="left", padx=20, pady=13)

        right = tk.Frame(topbar, bg=C["surf2"])
        right.pack(side="right", padx=14, pady=8)

        self._pill = Pill(right, bg=C["surf2"])
        self._pill.pack(side="left", padx=(0, 12))

        self._start_btn = RoundedButton(right, text="Start server",
                                         command=self._toggle,
                                         style="primary", bg=C["surf2"])
        self._start_btn.pack(side="left")

        # ----------------------------------------------------------------
        # Body
        # ----------------------------------------------------------------
        body = tk.Frame(self, bg=C["bg"], padx=20, pady=18)
        body.pack(fill="both", expand=True)

        # -- IP card ------------------------------------------------------
        ip_card = RoundedFrame(body, bg=C["card_grn_bg"], border=C["card_grn_bd"],
                                bg_outer=C["bg"])
        ip_card.pack(fill="x", pady=(0, 18))

        ip_inner = tk.Frame(ip_card.inner, bg=C["card_grn_bg"], padx=14, pady=12)
        ip_inner.pack(fill="both", expand=True)

        # Label: black for contrast
        tk.Label(ip_inner,
                 text="YOUR LAN IP - SHARE WITH THE VIEWER (CLICK TO COPY)",
                 font=FONT_MICRO, fg=C["text2"], bg=C["card_grn_bg"],
                 anchor="w").pack(anchor="w", pady=(0, 8))

        chips = tk.Frame(ip_inner, bg=C["card_grn_bg"])
        chips.pack(anchor="w")

        for ip in get_lan_ips():
            # Soft green tint background + green border/text - echoes the
            # Start server button's color without matching its saturation,
            # so it reads as "green" without competing for attention.
            chip = RoundedFrame(chips, radius=6, bg=C["suc_bg"], border=C["suc"],
                                 bg_outer=C["card_grn_bg"], height=28)
            chip.pack(side="left", padx=(0, 8))
            chip_lbl = tk.Label(chip.inner, text=ip, font=FONT_MONO,
                                 fg=C["suc_txt"], bg=C["suc_bg"], padx=10, cursor="hand2")
            chip_lbl.pack(fill="both", expand=True)
            chip_lbl.bind("<Button-1>", lambda e, ip=ip, lbl=chip_lbl: self._copy_ip(ip, lbl))
            self.update_idletasks()
            chip.configure(width=chip_lbl.winfo_reqwidth())

        # Size the card to fit its content (label + chip row + padding)
        # instead of the Canvas widget's large default height.
        self.update_idletasks()
        ip_card.configure(height=ip_inner.winfo_reqheight())

        # -- Port + Password row -------------------------------------------
        grid_row = tk.Frame(body, bg=C["bg"])
        grid_row.pack(fill="x", pady=(0, 18))
        grid_row.columnconfigure(0, weight=1)
        grid_row.columnconfigure(1, weight=1)

        self._port_var = tk.StringVar(value="9000")
        self._pw_var   = tk.StringVar()

        self._build_input_col(grid_row, col=0, label="PORT",
                               var=self._port_var, mono=True)
        self._build_input_col(grid_row, col=1, label="PASSWORD",
                               var=self._pw_var, show="*", mono=False,
                               show_toggle=True)

        # -- Quality + FPS sliders row ------------------------------------
        slider_row = tk.Frame(body, bg=C["bg"])
        slider_row.pack(fill="x")
        slider_row.columnconfigure(0, weight=1)
        slider_row.columnconfigure(1, weight=1)

        self._quality_var = tk.IntVar(value=55)
        self._fps_var     = tk.IntVar(value=20)

        self._build_slider_col(slider_row, col=0, label="QUALITY",
                                var=self._quality_var,
                                from_=15, to=90,
                                hint_l="Fast", hint_r="Crisp")
        self._build_slider_col(slider_row, col=1, label="FPS",
                                var=self._fps_var,
                                from_=5,  to=30,
                                hint_l="5", hint_r="30")

        # ----------------------------------------------------------------
        # Log
        # ----------------------------------------------------------------
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")

        log_card = RoundedFrame(self, bg=C["log_bg"], border=C["border"],
                                 bg_outer=C["bg"])
        log_card.pack(fill="x", padx=20, pady=20)

        log_hdr = tk.Frame(log_card.inner, bg=C["log_bg"], padx=20, pady=7)
        log_hdr.pack(fill="x")
        tk.Label(log_hdr, text="Log", font=FONT_SMALL,
                 fg=C["text2"], bg=C["log_bg"]).pack(side="left")

        log_body = tk.Frame(log_card.inner, bg=C["log_bg"])
        log_body.pack(fill="both", expand=True)

        log_scroll = tk.Scrollbar(log_body)
        log_scroll.pack(side="right", fill="y")

        self._log_box = tk.Text(
            log_body, height=6,
            bg=C["log_bg"], fg=C["text2"],
            font=FONT_MONO, relief="flat", bd=0,
            state="disabled", wrap="word",
            padx=20, pady=10,
            selectbackground=C["acc"], selectforeground="#fff",
            yscrollcommand=log_scroll.set,
        )
        self._log_box.pack(side="left", fill="both", expand=True)
        log_scroll.configure(command=self._log_box.yview)
        self._log("Ready - configure settings and click Start server.")

        self.update_idletasks()
        log_card.configure(height=log_card.inner.winfo_reqheight())

    def _build_input_col(self, parent, col, label, var, show="", mono=True, show_toggle=False):
        """Build a label + rounded entry inside a grid column, capped at INPUT_MAX_W."""
        padx = (0, 12) if col == 0 else (12, 0)
        frame = tk.Frame(parent, bg=C["bg"])
        frame.grid(row=0, column=col, sticky="nsew", padx=padx)

        # Label in black for contrast
        tk.Label(frame, text=label, font=FONT_MICRO,
                 fg=C["text"], bg=C["bg"],
                 anchor="w").pack(anchor="w", pady=(0, 5))

        # Entry is capped at a fixed max width (not reactively resized -
        # reactively resizing a canvas that's also pack(fill="x") fights
        # the geometry manager and never settles).
        entry = RoundedEntry(
            frame, textvariable=var, show=show,
            font=FONT_MONO if mono else FONT,
            height=CTRL_HEIGHT, width=INPUT_MAX_W,
            bg_outer=C["bg"], show_toggle=show_toggle,
        )
        entry.pack(anchor="w")

    def _build_slider_col(self, parent, col, label, var, from_, to, hint_l, hint_r):
        padx = (0, 12) if col == 0 else (12, 0)
        frame = tk.Frame(parent, bg=C["bg"])
        frame.grid(row=0, column=col, sticky="nsew", padx=padx, pady=(0, 4))

        hdr = tk.Frame(frame, bg=C["bg"])
        hdr.pack(fill="x")

        # Label black for contrast
        tk.Label(hdr, text=label, font=FONT_MICRO,
                 fg=C["text"], bg=C["bg"],
                 anchor="w").pack(side="left", pady=(0, 4))

        # Current value in blue accent
        tk.Label(hdr, textvariable=var,
                 font=(FONT_MONO[0], FONT_MONO[1], "bold"),
                 fg=C["acc"], bg=C["bg"]
                 ).pack(side="right", pady=(0, 4))

        tk.Scale(frame, variable=var, from_=from_, to=to,
                 orient="horizontal", showvalue=False,
                 bg=C["bg"], fg=C["text"],
                 activebackground=C["acc"],
                 troughcolor=C["border2"],
                 highlightthickness=0,
                 sliderlength=16, bd=0,
                 ).pack(fill="x")

        hints = tk.Frame(frame, bg=C["bg"])
        hints.pack(fill="x")
        tk.Label(hints, text=hint_l, font=FONT_MICRO,
                 fg=C["text3"], bg=C["bg"]).pack(side="left")
        tk.Label(hints, text=hint_r, font=FONT_MICRO,
                 fg=C["text3"], bg=C["bg"]).pack(side="right")

    # ----------------------------------------------------------------
    # Log
    # ----------------------------------------------------------------
    def _log(self, msg: str):
        def _do():
            self._log_box.configure(state="normal")
            self._log_box.insert("end", msg + "\n")
            self._log_box.see("end")
            self._log_box.configure(state="disabled")
        self.after(0, _do)

    # ----------------------------------------------------------------
    # Start / Stop
    # ----------------------------------------------------------------
    def _toggle(self):
        if self._server_running:
            self._log("Stopping server...")
            self._save_current_settings()
            import os as _os
            _os._exit(0)
        else:
            self._start()

    def _start(self):
        try:
            port = int(self._port_var.get())
        except ValueError:
            messagebox.showerror("Error", "Port must be a number.")
            return

        password = self._pw_var.get().strip()
        quality  = int(self._quality_var.get())
        fps      = int(self._fps_var.get())

        if not password:
            if not messagebox.askyesno("No password set",
                "No password set - anyone on your LAN can connect.\n\nContinue anyway?"):
                return

        self._server_running = True
        self._client_count = 0
        self._pill.set("on", "Running")
        self._start_btn.configure(text="Stop server")
        self._start_btn.set_style("ghost-danger")
        self._log(f"\nStarting on port {port}  |  quality={quality}  fps={fps}")
        self._log("Password: " + ("enabled" if password else "DISABLED - open to LAN"))
        self._save_current_settings()

        threading.Thread(
            target=self._run_server,
            args=(port, quality, fps, password, not password),
            daemon=True).start()

    def _run_server(self, port, quality, fps, password, allow_no_password=False):
        if getattr(sys, 'frozen', False):
            server_path = os.path.join(sys._MEIPASS, 'server.py')
        else:
            server_path = os.path.join(os.path.dirname(__file__), 'server.py')

        spec = importlib.util.spec_from_file_location("landesk_server", server_path)
        srv  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(srv)

        class GUIHandler(logging.Handler):
            def __init__(self_, app):
                super().__init__()
                self_.app = app
            def emit(self_, record):
                self_.app._log(self_.format(record))

        handler = GUIHandler(self)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        srv.log.handlers.clear()
        srv.log.addHandler(handler)
        srv.log.propagate = False

        try:
            # 0.0.0.0 rather than the command line's loopback default: a
            # server nobody else can reach is not what this window is for.
            # Running without a password still needs saying so, and the only
            # thing that says it is the dialog the user just answered.
            asyncio.run(srv.main("0.0.0.0", port, quality, fps, password,
                                  on_client_count=self._on_client_count,
                                  allow_no_password=allow_no_password))
        except Exception as e:
            self._log(f"Server error: {e}")
        finally:
            self._log("Server stopped.")
            self.after(0, self._on_stopped)

    def _on_client_count(self, count):
        self.after(0, self._update_client_count, count)

    def _update_client_count(self, count):
        self._client_count = count
        if self._server_running:
            if count == 0:
                self._pill.set("on", "Running")
            else:
                self._pill.set("on", f"Running \u00b7 {count} viewer{'s' if count != 1 else ''}")

    def _on_stopped(self):
        self._server_running = False
        self._client_count = 0
        self._pill.set("off", "Stopped")
        self._start_btn.configure(text="Start server")
        self._start_btn.set_style("primary")


if __name__ == "__main__":
    app = ServerApp()
    app.mainloop()
