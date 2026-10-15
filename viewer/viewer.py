"""
LANDesk - Viewer
Run on the PC you sit at and want to control from.
"""

import argparse, asyncio, io, json, logging, os, ssl, sys, threading, time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox

if getattr(sys, 'frozen', False):
    base = sys._MEIPASS
else:
    base = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, base)
sys.path.insert(0, os.path.join(base, 'shared'))
from shared.protocol import *
from shared import auth as authlib
from shared import tlsutil

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [VIEWER] %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("landesk.viewer")

# Connections are encrypted unless --no-tls says otherwise, and the server
# has to have been started the same way. There is no automatic fallback: a
# viewer that quietly retried in plaintext when TLS failed would hand an
# attacker a way to ask for exactly that.
USE_TLS = True

try:
    from PIL import Image, ImageTk
    PIL_OK = True
except ImportError:
    PIL_OK = False

try:
    import pyperclip
    CLIP_OK = True
except ImportError:
    CLIP_OK = False

# ---------------------------------------------------------------------------
# Colour tokens
# ---------------------------------------------------------------------------
C = {
    "bg":       "#ffffff",
    "surf1":    "#f7f8fa",
    "surf2":    "#eef0f4",
    "text":     "#14161a",
    "text2":    "#4a4f59",
    "text3":    "#8a8f9c",
    "border":   "#e6e8ed",
    "border2":  "#c9ced9",
    "acc":      "#4f46e5",
    "acc_dark": "#3f37c9",
    "suc":      "#16a34a",   # green - used for Connect / primary buttons
    "suc_dark": "#0f7c39",
    "suc_bg":   "#eafbf1",
    "suc_bd":   "#bfe8cf",
    "suc_txt":  "#0f6b34",
    "dan":      "#dc2626",
    "dan_dark": "#b91c1c",
    "dan_bg":   "#fdecec",
    "dan_bd":   "#f3c1c1",
    "canvas":   "#ffffff",
}

FONT        = ("Segoe UI", 10)
FONT_BOLD   = ("Segoe UI Semibold", 10)
FONT_SMALL  = ("Segoe UI", 9)
FONT_MONO   = ("Cascadia Mono", 10)
FONT_MICRO  = ("Segoe UI", 8)
FONT_HEADER = ("Segoe UI Semibold", 13)


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

# max password width in characters before it stops growing
PW_MAX_CHARS = 20
PW_MAX_PX    = PW_MAX_CHARS * 8 + 24   # approx px per mono/segoe char + inner padding

# Shared control height so inputs and buttons line up visually
RADIUS      = 10
CTRL_HEIGHT = 38

# Max number of recently-used hosts remembered for the dropdown
MAX_HISTORY = 6


# ---------------------------------------------------------------------------
# Settings persistence - remembers last-used values between launches
# ---------------------------------------------------------------------------
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".landesk", "viewer_settings.json")


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
    def __init__(self, parent, text, command, style="default",
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
            "primary":       (C["suc"],     C["suc_dark"], "#ffffff", C["suc"]),
            "danger":        (C["dan"],     C["dan_dark"], "#ffffff", C["dan"]),
            "success-ghost": (C["suc_bg"],  "#d4eddb",     C["suc_txt"], C["suc_bd"]),
            "ghost":         (C["surf2"],   C["surf1"],    C["text2"],   C["border2"]),
            "ghost-disabled":(C["surf2"],   C["surf2"],    C["text3"],   C["border"]),
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
# Async network client
# ---------------------------------------------------------------------------
class LANDeskClient:
    """The viewer's half of the connection.

    Owns its own event loop on a background thread; the Tk callbacks handed to
    it are called from that thread, which is how the original was built and is
    left alone here.
    """

    # A hostile server could ask for a PBKDF2 cost that takes the viewer an
    # hour. The server's own default is 210,000.
    MAX_ITERATIONS = 5_000_000
    HANDSHAKE_TIMEOUT = 20.0

    def __init__(self, host, port, password,
                 on_frame, on_connected, on_error, on_disconnect, use_tls=None):
        self.host          = host
        self.port          = port
        # Kept rather than hashed: the password is never sent, it is stretched
        # per connection with the salt the server supplies and used to answer
        # a challenge. See shared/auth.py.
        self.password      = password
        self.use_tls       = USE_TLS if use_tls is None else use_tls
        self.on_frame      = on_frame
        self.on_connected  = on_connected
        self.on_error      = on_error
        self.on_disconnect = on_disconnect
        self._loop         = None
        self._queue        = None
        self._running      = False
        self._task         = None
        self._stop_requested = False

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._running = False
        self._stop_requested = True
        loop = self._loop
        if loop is None or loop.is_closed():
            # The connection already ended - a refused password, a server that
            # went away - and the loop closed with it. Clicking Disconnect
            # after that used to raise "Event loop is closed" out of the Tk
            # callback.
            return
        try:
            loop.call_soon_threadsafe(self._cancel_task)
        except RuntimeError:
            pass

    def _cancel_task(self):
        if self._task and not self._task.done():
            self._task.cancel()

    def send(self, msg_type, data):
        if self._loop and self._queue and self._running:
            self._loop.call_soon_threadsafe(
                self._queue.put_nowait, encode_json(msg_type, data))

    def send_clipboard(self, text):
        self.send(MSG_CLIPBOARD_SEND, {"text": text})

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._task = self._loop.create_task(self._connect())
        if self._stop_requested:
            # stop() was called before the task existed - cancel it as soon
            # as the loop starts running instead of missing the request.
            self._loop.call_soon(self._cancel_task)
        try:
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.on_error(str(e))
        finally:
            self._loop.close()

    async def _open(self):
        """Open the socket, wrapped in TLS unless that was turned off."""
        ssl_ctx = tlsutil.client_context() if self.use_tls else None
        return await asyncio.open_connection(self.host, self.port, ssl=ssl_ctx)

    async def _handshake(self, reader, writer):
        """Prove we know the password, and check the server does too.

        Returns the server's HELLO_ACK, or None after reporting the error.
        """
        # What the server's certificate hashes to, as this connection saw it.
        # Both password proofs cover it, so a machine in the middle presenting
        # its own certificate cannot pass either one along. Empty without TLS.
        channel = tlsutil.peer_fingerprint(writer)

        writer.write(encode_json(MSG_HELLO, {
            "protocol": PROTOCOL_VERSION,
            "client": "landesk-viewer",
        }))
        await writer.drain()

        msg_type, data = await read_json(
            reader, {MSG_CHALLENGE, MSG_HELLO_ACK}, self.HANDSHAKE_TIMEOUT)
        if msg_type == MSG_HELLO_ACK:
            # Refused before the challenge: wrong protocol version, or locked out.
            self.on_error(data.get("error", "Connection rejected"))
            return None

        try:
            salt = bytes.fromhex(data["salt"])
            nonce = bytes.fromhex(data["nonce"])
            iterations = int(data["iterations"])
        except (KeyError, TypeError, ValueError):
            self.on_error("The server sent a challenge this viewer cannot read.")
            return None
        if not 1 <= iterations <= self.MAX_ITERATIONS:
            self.on_error("The server asked for an unreasonable password cost (%d)."
                          % iterations)
            return None
        auth_required = bool(data.get("auth_required", True))

        if auth_required:
            # Stretching the password takes a fraction of a second; off the
            # event loop so the ping timer and cancellation still work.
            key = await asyncio.get_event_loop().run_in_executor(
                None, authlib.derive_key, self.password, salt, iterations)
            response = authlib.client_response(key, nonce, channel)
        else:
            key, response = b"", ""

        writer.write(encode_json(MSG_AUTH, {"response": response}))
        await writer.drain()

        msg_type, ack = await read_json(reader, {MSG_HELLO_ACK}, self.HANDSHAKE_TIMEOUT)
        if not ack.get("ok"):
            self.on_error(ack.get("error", "Connection rejected"))
            return None
        if auth_required and not authlib.verify(
                authlib.server_confirmation(key, nonce, channel), ack.get("server_proof")):
            # Right port, wrong server: it could not prove it knows the
            # password. Say so rather than stream a screen at it.
            self.on_error("The server could not prove it knows the password. "
                          "Refusing to continue.")
            return None
        return ack

    async def _connect(self):
        try:
            reader, writer = await self._open()
        except ssl.SSLError as e:
            self.on_error("Encrypted connection to %s:%s failed (%s). If that server "
                          "is running with --no-tls, this viewer needs --no-tls too."
                          % (self.host, self.port, e.reason or e))
            return
        except Exception as e:
            self.on_error(f"Cannot connect to {self.host}:{self.port} - {e}")
            return

        try:
            self._running = True
            self._queue   = asyncio.Queue()
            try:
                ack = await self._handshake(reader, writer)
            except asyncio.TimeoutError:
                self.on_error("The server stopped responding during the handshake.")
                return
            except ProtocolError as e:
                self.on_error("The server is not speaking the LANDesk protocol (%s). "
                              "If it is an older version, update both ends." % e)
                return
            except asyncio.IncompleteReadError:
                self.on_error("The server closed the connection during the handshake.")
                return
            if ack is None:
                return

            self.on_connected(ack.get("screen_width", 1920), ack.get("screen_height", 1080))
            await asyncio.gather(self._recv(reader), self._send_loop(writer))
        finally:
            self._running = False
            try:
                writer.close()
            except Exception:
                pass

    async def _recv(self, reader):
        while self._running:
            try:
                msg_type, payload = await read_message(reader, CLIENT_ACCEPTS)
                if msg_type == MSG_FRAME:
                    self.on_frame(payload)
                elif msg_type == MSG_CLIPBOARD_RECV and CLIP_OK:
                    text = decode_json(payload).get("text", "")
                    if isinstance(text, str):
                        pyperclip.copy(text)
            except ProtocolError as e:
                # The far end is sending something this protocol does not
                # allow - too large, or a type a viewer never receives.
                log.warning("Dropping connection: %s", e)
                self._running = False
                self.on_disconnect()
                break
            except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError,
                    ssl.SSLError, OSError):
                self._running = False
                self.on_disconnect()
                break

    async def _send_loop(self, writer):
        while self._running:
            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                writer.write(msg)
                await writer.drain()
            except asyncio.TimeoutError:
                writer.write(encode_message(MSG_PING, b""))
                await writer.drain()
            except Exception:
                self._running = False
                break


# ---------------------------------------------------------------------------
# Main viewer window
# ---------------------------------------------------------------------------
class ViewerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        _resolve_fonts()
        self._set_icon()
        if not PIL_OK:
            messagebox.showerror("Missing dependency",
                "Pillow is required:\n  pip install Pillow")
            self.destroy()
            return

        self.title("LANDesk")
        self.configure(bg=C["bg"])
        self.minsize(640, 500)

        self._client    = None
        self._connected = False
        self._screen_w  = 1920
        self._screen_h  = 1080
        self._rx = self._ry = 0
        self._rw = self._rh = 1
        self._photo     = None
        self._frames    = 0
        self._last_fps  = time.monotonic()
        self._fps       = 0
        self._fullscreen  = False
        self._scale_mode  = "fit"     # "fit" or "actual"
        self._history     = load_settings().get("history", [])

        self._build_ui()
        self._load_saved_settings()
        self._tick_fps()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<F11>",     lambda e: self._toggle_fullscreen())
        self.bind("<Escape>",  self._on_escape)

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

    # ----------------------------------------------------------------
    # UI construction
    # ----------------------------------------------------------------
    def _build_ui(self):
        # ---- Connect bar ------------------------------------------------
        # Grid layout so the password field has a hard max width while a
        # spacer column absorbs the extra space, keeping both buttons
        # pinned to the right-most edge of the bar.
        cbar = tk.Frame(self, bg=C["surf2"], pady=0)
        cbar.pack(fill="x")
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")

        # Column layout:
        # 0=Host lbl  1=host entry  2=history btn
        # 3=Port lbl  4=port entry
        # 5=Pw lbl    6=pw entry (fixed max width)  7=spacer(weight=1)
        # 8=connect btn  9=clip btn
        cbar.columnconfigure(7, weight=1)   # spacer grows, pushes buttons right
        cbar.rowconfigure(0, minsize=CTRL_HEIGHT + 16)

        def clbl(text, col):
            tk.Label(cbar, text=text.upper(), font=FONT_MICRO,
                     fg=C["text"], bg=C["surf2"]
                     ).grid(row=0, column=col, padx=(12 if col == 0 else 8, 4),
                             sticky="e", pady=10)

        def centry(col, var, width_px, show=""):
            e = RoundedEntry(
                cbar, textvariable=var, show=show, font=FONT_MONO,
                height=CTRL_HEIGHT, width=width_px, bg_outer=C["surf2"],
            )
            e.grid(row=0, column=col, sticky="w", pady=8)
            return e

        clbl("Host", 0)
        self._host_var    = tk.StringVar(value="192.168.1.")
        self._host_entry  = centry(1, self._host_var, width_px=155)

        self._history_btn = RoundedButton(cbar, text="\u25be", command=self._show_history,
                                           style="ghost", bg=C["surf2"], pad_x=8)
        self._history_btn.grid(row=0, column=2, padx=(4, 8), pady=8, sticky="w")

        clbl("Port", 3)
        self._port_var    = tk.StringVar(value="9000")
        self._port_entry  = centry(4, self._port_var, width_px=70)

        clbl("Password", 5)
        self._pw_var    = tk.StringVar()
        self._pw_entry  = RoundedEntry(
            cbar, textvariable=self._pw_var, show="*", font=FONT,
            height=CTRL_HEIGHT, width=PW_MAX_PX, bg_outer=C["surf2"],
            show_toggle=True,
        )
        self._pw_entry.grid(row=0, column=6, sticky="w", pady=8, padx=(0, 8))

        # Empty spacer column - absorbs remaining width so the password
        # field never grows past PW_MAX_PX while the buttons stay right.
        tk.Frame(cbar, bg=C["surf2"]).grid(row=0, column=7, sticky="ew")

        # Connect button - always right of the spacer, never shrinks
        self._conn_btn = RoundedButton(cbar, text="Connect",
                                        command=self._toggle_connect,
                                        style="primary", bg=C["surf2"])
        self._conn_btn.grid(row=0, column=8, padx=(0, 8), pady=8, sticky="e")

        # Send clipboard - rightmost, never shrinks
        self._clip_btn = RoundedButton(cbar, text="Send clipboard",
                                        command=self._send_clipboard,
                                        style="ghost-disabled", bg=C["surf2"])
        self._clip_btn.grid(row=0, column=9, padx=(0, 12), pady=8, sticky="e")
        self._clip_enabled = False

        # Enter key in any field triggers connect
        for w in [self._host_entry.entry, self._port_entry.entry, self._pw_entry.entry]:
            w.bind("<Return>", lambda e: (self._toggle_connect()
                                          if not self._connected else None))

        # ---- Canvas ----------------------------------------------------- 
        self._canvas = tk.Canvas(self, bg=C["canvas"],
                                  highlightthickness=0, cursor="crosshair")
        self._canvas.pack(fill="both", expand=True)

        # Placeholder text - recentred on every resize
        self._ph_id = self._canvas.create_text(
            0, 0,
            text="Enter the server IP above and click Connect",
            fill=C["text3"], font=("Segoe UI", 13), justify="center",
            tags="placeholder")
        self._canvas.bind("<Configure>", self._on_canvas_resize)

        # ---- Status bar -------------------------------------------------
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")
        sbar = tk.Frame(self, bg=C["surf2"])
        sbar.pack(fill="x")

        # Dot drawn as a true circle via Canvas oval
        self._dot_cv = tk.Canvas(sbar, width=12, height=12,
                                  bg=C["surf2"], highlightthickness=0)
        self._dot_cv.pack(side="left", padx=(14, 5), pady=8)
        self._dot = self._dot_cv.create_oval(2, 2, 10, 10,
                                              fill=C["text3"], outline="")

        self._status_lbl = tk.Label(sbar, text="Not connected",
                                     font=FONT_SMALL, fg=C["text3"], bg=C["surf2"])
        self._status_lbl.pack(side="left")

        self._kb_lbl = tk.Label(sbar, text="\u2328 Input active",
                                 font=FONT_SMALL, fg=C["acc"], bg=C["surf2"])
        # packed/unpacked dynamically - only shown while connected

        self._res_lbl = tk.Label(sbar, text="", font=FONT_MONO,
                                  fg=C["text3"], bg=C["surf2"])
        self._res_lbl.pack(side="right", padx=14, pady=8)

        self._scale_btn = RoundedButton(sbar, text="Fit", command=self._toggle_scale_mode,
                                         style="ghost", bg=C["surf2"], height=26, pad_x=10)
        self._scale_btn.pack(side="right", padx=(0, 10), pady=6)

        self._fs_btn = RoundedButton(sbar, text="\u26f6 Fullscreen", command=self._toggle_fullscreen,
                                      style="ghost", bg=C["surf2"], height=26, pad_x=10)
        self._fs_btn.pack(side="right", padx=(0, 4), pady=6)

        # ---- Input bindings ---------------------------------------------
        self._canvas.bind("<Motion>",        self._mouse_move)
        self._canvas.bind("<ButtonPress>",   self._mouse_press)
        self._canvas.bind("<ButtonRelease>", self._mouse_release)
        self._canvas.bind("<MouseWheel>",    self._scroll_win)
        self._canvas.bind("<Button-4>",      lambda e: self._scroll(0,  1))
        self._canvas.bind("<Button-5>",      lambda e: self._scroll(0, -1))
        self.bind("<KeyPress>",   self._key_press)
        self.bind("<KeyRelease>", self._key_release)

    def _on_canvas_resize(self, event):
        # Always keep placeholder perfectly centred
        self._canvas.coords(self._ph_id, event.width // 2, event.height // 2)

    # ----------------------------------------------------------------
    # Settings persistence
    # ----------------------------------------------------------------
    def _load_saved_settings(self):
        s = load_settings()
        if "host" in s:
            self._host_var.set(s["host"])
        if "port" in s:
            self._port_var.set(s["port"])
        if "password" in s:
            self._pw_var.set(s["password"])

    def _save_current_settings(self):
        save_settings({
            "host":     self._host_var.get(),
            "port":     self._port_var.get(),
            "password": self._pw_var.get(),
            "history":  self._history,
        })

    def _remember_host(self, host):
        if host in self._history:
            self._history.remove(host)
        self._history.insert(0, host)
        self._history = self._history[:MAX_HISTORY]
        self._save_current_settings()

    def _show_history(self):
        if not self._history:
            return
        menu = tk.Menu(self, tearoff=0, font=FONT_MONO,
                        bg=C["bg"], fg=C["text"],
                        activebackground=C["acc"], activeforeground="#ffffff")
        for h in self._history:
            menu.add_command(label=h, command=lambda h=h: self._host_var.set(h))
        try:
            x = self._history_btn.winfo_rootx()
            y = self._history_btn.winfo_rooty() + self._history_btn.winfo_height()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    # ----------------------------------------------------------------
    # Fullscreen / scale mode
    # ----------------------------------------------------------------
    def _toggle_fullscreen(self):
        self._fullscreen = not self._fullscreen
        self.attributes("-fullscreen", self._fullscreen)
        self._fs_btn.configure(text="\u26f6 Exit fullscreen" if self._fullscreen else "\u26f6 Fullscreen")

    def _on_escape(self, event):
        if self._fullscreen:
            self._toggle_fullscreen()
            return "break"
        # Not in fullscreen: let Escape forward to the remote session as normal.

    def _toggle_scale_mode(self):
        self._scale_mode = "actual" if self._scale_mode == "fit" else "fit"
        self._scale_btn.configure(text="1:1" if self._scale_mode == "actual" else "Fit")
        if self._connected:
            self._canvas.delete("frame")

    # ----------------------------------------------------------------
    # Close handling
    # ----------------------------------------------------------------
    def _on_close(self):
        if self._connected:
            if not messagebox.askyesno(
                    "Quit LANDesk Viewer",
                    "You're still connected to a remote session.\n\nDisconnect and quit?"):
                return
        self._save_current_settings()
        self.destroy()

    # ----------------------------------------------------------------
    # Connection logic
    # ----------------------------------------------------------------
    def _toggle_connect(self):
        if self._connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        host = self._host_var.get().strip()
        if not host:
            messagebox.showerror("Error", "Enter a host IP address.")
            return
        try:
            port = int(self._port_var.get())
        except ValueError:
            messagebox.showerror("Error", "Port must be a number.")
            return

        self._set_status("Connecting...", "off")
        self._conn_btn.configure(text="Connecting...")
        self._conn_btn.set_style("ghost-disabled")

        self._client = LANDeskClient(
            host=host, port=port, password=self._pw_var.get(),
            on_frame=self._on_frame,
            on_connected=self._on_connected,
            on_error=self._on_error,
            on_disconnect=self._on_disc)
        self._client.start()
        self._remember_host(host)

    def _disconnect(self):
        if self._client:
            self._client.stop()
            self._client = None
        self._connected = False
        # Restore placeholder
        self._ph_id = self._canvas.create_text(
            self._canvas.winfo_width() // 2,
            self._canvas.winfo_height() // 2,
            text="Enter the server IP above and click Connect",
            fill=C["text3"], font=("Segoe UI", 13), justify="center",
            tags="placeholder")
        self._conn_btn.configure(text="Connect")
        self._conn_btn.set_style("primary")
        self._set_clip_enabled(False)
        self._set_status("Not connected", "off")
        self._res_lbl.configure(text="")
        self._kb_lbl.pack_forget()

    def _on_connected(self, sw, sh):
        self._screen_w = sw
        self._screen_h = sh
        self._connected = True
        host = self._host_var.get().strip()
        self.after(0, lambda: (
            self._canvas.delete("placeholder"),
            self._conn_btn.configure(text="Disconnect"),
            self._conn_btn.set_style("danger"),
            self._set_clip_enabled(True),
            self._set_status(f"Connected to {host}", "on"),
            self._res_lbl.configure(text=f"{sw} x {sh}"),
            self._kb_lbl.pack(side="left", padx=(14, 0)),
        ))

    def _on_error(self, msg):
        self.after(0, lambda: (
            self._disconnect(),
            self._set_status(f"Error: {msg}", "off"),
            messagebox.showerror("LANDesk", msg, parent=self),
        ))

    def _on_disc(self):
        self.after(0, lambda: (
            self._disconnect(),
            self._set_status("Connection lost", "off"),
        ))

    def _set_status(self, msg, dot):
        self._status_lbl.configure(
            text=msg,
            fg=C["suc_txt"] if dot == "on" else C["text3"])
        self._dot_cv.itemconfigure(
            self._dot,
            fill=C["suc"] if dot == "on" else C["text3"])

    def _set_clip_enabled(self, enabled):
        self._clip_enabled = enabled
        self._clip_btn.set_style("success-ghost" if enabled else "ghost-disabled")

    # ----------------------------------------------------------------
    # Frame rendering
    # ----------------------------------------------------------------
    def _on_frame(self, jpeg):
        self.after(0, self._render, jpeg)

    def _render(self, jpeg):
        try:
            img = Image.open(io.BytesIO(jpeg))
        except Exception:
            return

        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        if cw < 2 or ch < 2:
            return

        if self._scale_mode == "actual":
            # True pixel size, centred - if the remote screen is larger than
            # the window, the excess is simply clipped by the canvas (no pan/scroll).
            nw, nh = img.width, img.height
        else:
            ir = img.width / img.height
            cr = cw / ch
            nw, nh = (cw, int(cw / ir)) if ir > cr else (int(ch * ir), ch)
            img = img.resize((nw, nh), Image.LANCZOS)

        photo = ImageTk.PhotoImage(img)

        self._rx = (cw - nw) // 2
        self._ry = (ch - nh) // 2
        self._rw = nw
        self._rh = nh

        self._canvas.delete("frame")
        self._canvas.create_image(self._rx, self._ry,
                                   anchor="nw", image=photo, tags="frame")
        self._photo  = photo
        self._frames += 1

    def _tick_fps(self):
        now     = time.monotonic()
        elapsed = now - self._last_fps
        if elapsed >= 1.0:
            self._fps      = int(self._frames / elapsed)
            self._frames   = 0
            self._last_fps = now
            if self._connected:
                sw, sh = self._screen_w, self._screen_h
                self._res_lbl.configure(
                    text=f"{sw} x {sh}  |  {self._fps} fps")
        self.after(500, self._tick_fps)

    # ----------------------------------------------------------------
    # Input forwarding
    # ----------------------------------------------------------------
    def _norm(self, cx, cy):
        return (max(0.0, min(1.0, (cx - self._rx) / self._rw)),
                max(0.0, min(1.0, (cy - self._ry) / self._rh)))

    def _mouse_move(self, e):
        if self._client:
            nx, ny = self._norm(e.x, e.y)
            self._client.send(MSG_MOUSE_MOVE, {"nx": nx, "ny": ny})

    _BTN = {1: "left", 2: "middle", 3: "right"}

    def _mouse_press(self, e):
        if self._client:
            self._client.send(MSG_MOUSE_BTN,
                {"button": self._BTN.get(e.num, "left"), "pressed": True})

    def _mouse_release(self, e):
        if self._client:
            self._client.send(MSG_MOUSE_BTN,
                {"button": self._BTN.get(e.num, "left"), "pressed": False})

    def _scroll_win(self, e):
        self._scroll(0, -1 if e.delta < 0 else 1)

    def _scroll(self, dx, dy):
        if self._client:
            self._client.send(MSG_SCROLL, {"dx": dx, "dy": dy})

    _SPECIAL = {
        "Return":"enter","Escape":"escape","Tab":"tab",
        "BackSpace":"backspace","Delete":"delete","space":"space",
        "Shift_L":"shift","Shift_R":"shift",
        "Control_L":"ctrl","Control_R":"ctrl",
        "Alt_L":"alt","Alt_R":"alt",
        "Left":"left","Right":"right","Up":"up","Down":"down",
        "Home":"home","End":"end","Prior":"page_up","Next":"page_down",
        "F1":"f1","F2":"f2","F3":"f3","F4":"f4","F5":"f5","F6":"f6",
        "F7":"f7","F8":"f8","F9":"f9","F10":"f10","F11":"f11","F12":"f12",
        "Super_L":"win","Super_R":"win","Caps_Lock":"caps_lock",
    }

    def _key_name(self, e):
        if e.keysym in self._SPECIAL:
            return self._SPECIAL[e.keysym]
        if len(e.char) == 1 and e.char.isprintable():
            return e.char
        return e.keysym.lower()

    def _key_press(self, e):
        if isinstance(self.focus_get(), tk.Entry):
            return
        if e.keysym == "F11":
            return
        if e.keysym == "Escape" and self._fullscreen:
            return
        if self._client:
            self._client.send(MSG_KEY, {"key": self._key_name(e), "pressed": True})

    def _key_release(self, e):
        if isinstance(self.focus_get(), tk.Entry):
            return
        if e.keysym == "F11":
            return
        if e.keysym == "Escape" and self._fullscreen:
            return
        if self._client:
            self._client.send(MSG_KEY, {"key": self._key_name(e), "pressed": False})

    def _send_clipboard(self):
        if not self._clip_enabled or not CLIP_OK:
            return
        try:
            text = pyperclip.paste()
            if text:
                self._client.send_clipboard(text)
                self._set_status("Clipboard sent.", "on")
        except Exception as ex:
            messagebox.showerror("Clipboard error", str(ex), parent=self)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LANDesk Viewer")
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--password", default="")
    parser.add_argument("--no-tls", action="store_true",
                        help="Connect without encryption, for a server started "
                             "with --no-tls. Everything you see and type crosses "
                             "the network in the clear.")
    args = parser.parse_args()
    USE_TLS = not args.no_tls
    if not USE_TLS:
        log.warning("TLS disabled - this session is not encrypted.")

    app = ViewerApp()
    if args.host:
        app._host_var.set(args.host)
        app._port_var.set(str(args.port))
        app._pw_var.set(args.password)
        app._connect()
    app.mainloop()
