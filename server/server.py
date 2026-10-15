"""
LANDesk Server
Run this on the PC you want to control remotely.
It captures the screen and accepts mouse/keyboard commands over TLS.

Usage:
    python server.py --password secret [--host 127.0.0.1] [--port 9000]
                     [--quality 50] [--fps 20]

The password is required. To run without one - which lets anyone who can
reach the port watch your screen and drive your mouse - you have to say
--no-password out loud. --host defaults to loopback; widen it deliberately.
"""

import argparse
import asyncio
import getpass
import io
import logging
import math
import os
import ssl
import sys
import threading
import time
import traceback

# Add the parent dir so we can import shared, and this dir so we can import
# ratelimit. server_gui.py loads this file by path, which puts neither on
# sys.path by itself.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_HERE, ".."), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from shared.protocol import *
from shared import auth as authlib
from shared import tlsutil
from ratelimit import AuthThrottle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SERVER] %(levelname)s %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("landesk.server")

# How long a connection may take over each step of the handshake, and how long
# an established session may go without saying anything. The viewer pings once
# a second, so a minute of silence means the peer is gone.
HANDSHAKE_TIMEOUT = 15.0
IDLE_TIMEOUT = 60.0
# Every rejection waits this long first, so a wrong guess costs a second even
# before the lockout counter notices it.
AUTH_FAIL_DELAY = 1.0

# What an authenticated session may send. HELLO and AUTH belong to the
# handshake and are not accepted again afterwards.
SESSION_ACCEPTS = SERVER_ACCEPTS - {MSG_HELLO, MSG_AUTH}

# -- Optional imports (graceful errors) ----------------------------------------
try:
    import mss
    import mss.tools
    MSS_OK = True
except ImportError:
    MSS_OK = False
    log.warning("mss not installed - screen capture disabled. Run: pip install mss")

try:
    from PIL import Image
    PIL_OK = True
except ImportError:
    PIL_OK = False
    log.warning("Pillow not installed. Run: pip install Pillow")

try:
    from pynput.mouse import Button, Controller as MouseController
    from pynput.keyboard import Key, Controller as KeyboardController
    PYNPUT_OK = True
except ImportError:
    PYNPUT_OK = False
    log.warning("pynput not installed - input injection disabled. Run: pip install pynput")

try:
    import pyperclip
    CLIP_OK = True
except ImportError:
    CLIP_OK = False


# -- Screen capturer (runs in a background thread) -----------------------------
class ScreenCapturer:
    def __init__(self, quality=50, fps=20):
        self.quality = quality
        self.fps = fps
        self._frame: bytes = b""
        self._seq = 0
        self._lock = threading.Lock()
        self._running = False
        self._thread = None
        self._screen_w = 1920
        self._screen_h = 1080

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def get_frame(self):
        """Return (sequence number, JPEG bytes).

        The sequence number lets a sender tell a fresh capture from the one it
        already sent, which is how a slow connection skips to the current
        screen instead of working through a backlog.
        """
        with self._lock:
            return self._seq, self._frame

    def get_resolution(self):
        return self._screen_w, self._screen_h

    def _capture_loop(self):
        if not (MSS_OK and PIL_OK):
            log.error("Cannot capture: mss or Pillow missing.")
            return
        interval = 1.0 / self.fps
        with mss.MSS() as sct:
            # monitors[0] is the combined virtual screen and fails with BitBlt
            # on many Windows setups. monitors[1] is always the primary monitor.
            monitor = sct.monitors[1]
            self._screen_w = monitor["width"]
            self._screen_h = monitor["height"]
            while self._running:
                t0 = time.monotonic()
                try:
                    raw = sct.grab(monitor)
                except Exception as e:
                    log.error("Screen capture failed: %s", e)
                    time.sleep(1.0)
                    continue
                img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=self.quality, optimize=False)
                frame = buf.getvalue()
                with self._lock:
                    self._frame = frame
                    self._seq += 1
                elapsed = time.monotonic() - t0
                sleep = max(0.0, interval - elapsed)
                time.sleep(sleep)


# -- Input handler -------------------------------------------------------------
class InputHandler:
    """Translates protocol messages into real mouse/keyboard events.

    Everything reaching these methods came off the network, so each one checks
    its arguments. The alternative is a peer ending the session with a
    mistyped coordinate, or putting the pointer where the screen is not.
    """

    # Map string key names -> pynput Key enum (subset of common keys)
    _KEY_MAP = {
        "enter": "enter", "return": "enter", "escape": "esc", "esc": "esc",
        "tab": "tab", "backspace": "backspace", "delete": "delete",
        "space": "space", "caps_lock": "caps_lock",
        "shift": "shift", "ctrl": "ctrl", "alt": "alt",
        "left": "left", "right": "right", "up": "up", "down": "down",
        "home": "home", "end": "end", "page_up": "page_up", "page_down": "page_down",
        "f1": "f1", "f2": "f2", "f3": "f3", "f4": "f4", "f5": "f5", "f6": "f6",
        "f7": "f7", "f8": "f8", "f9": "f9", "f10": "f10", "f11": "f11", "f12": "f12",
        "win": "cmd", "cmd": "cmd", "super": "cmd",
    }
    # A scroll bigger than this is a peer being silly, not a person scrolling.
    MAX_SCROLL = 100

    def __init__(self, screen_w, screen_h):
        self.screen_w = screen_w
        self.screen_h = screen_h
        if PYNPUT_OK:
            self.mouse = MouseController()
            self.keyboard = KeyboardController()
        else:
            self.mouse = None
            self.keyboard = None

    def to_pixels(self, nx, ny):
        """Normalised 0..1 coordinates -> a point on the screen, or None.

        Clamped rather than rejected at the edges: a viewer that rounds 1.0 up
        by a fraction should still reach the last column of pixels.
        """
        try:
            fx, fy = float(nx), float(ny)
        except (TypeError, ValueError):
            return None
        if math.isnan(fx) or math.isnan(fy):
            return None
        fx = min(max(fx, 0.0), 1.0)
        fy = min(max(fy, 0.0), 1.0)
        return (min(int(fx * self.screen_w), self.screen_w - 1),
                min(int(fy * self.screen_h), self.screen_h - 1))

    def resolve_key(self, name):
        """Map a key name to a pynput key, a single character, or None.

        Anything else - a multi-character name that is not in the map, a
        number, a null - is not a key this can press, and pressing it would
        raise inside pynput.
        """
        if not isinstance(name, str) or not name:
            return None
        lowered = name.lower()
        if lowered in self._KEY_MAP:
            return getattr(Key, self._KEY_MAP[lowered]) if PYNPUT_OK else lowered
        return name if len(name) == 1 else None

    def clamp_scroll(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        try:
            n = int(value)
        except (TypeError, ValueError, OverflowError):
            return 0
        return min(max(n, -self.MAX_SCROLL), self.MAX_SCROLL)

    def on_mouse_move(self, nx, ny):
        point = self.to_pixels(nx, ny)
        if point is None or not PYNPUT_OK:
            return
        self.mouse.position = point

    def on_mouse_button(self, button, pressed):
        if not PYNPUT_OK:
            return
        btn_map = {"left": Button.left, "right": Button.right, "middle": Button.middle}
        btn = btn_map.get(button)
        if btn is None:
            return
        if pressed:
            self.mouse.press(btn)
        else:
            self.mouse.release(btn)

    def on_scroll(self, dx, dy):
        dx, dy = self.clamp_scroll(dx), self.clamp_scroll(dy)
        if not PYNPUT_OK or (dx == 0 and dy == 0):
            return
        self.mouse.scroll(dx, dy)

    def on_key(self, key_name, pressed):
        key = self.resolve_key(key_name)
        if key is None or not PYNPUT_OK:
            return
        if pressed:
            self.keyboard.press(key)
        else:
            self.keyboard.release(key)


# -- Authentication configuration ----------------------------------------------
class AuthConfig:
    """The server's half of the password exchange.

    The key is stretched once here, at startup, so verifying a connection
    costs one HMAC rather than a PBKDF2 run - otherwise the cost meant to slow
    an attacker down would be a way to exhaust the server instead. The salt is
    fresh per run and travels in the challenge; nothing derived from the
    password is written anywhere.
    """

    def __init__(self, password, required=True, iterations=authlib.PBKDF2_ITERATIONS):
        self.required = bool(required)
        self.iterations = iterations
        self.salt = authlib.new_salt()
        self.key = authlib.derive_key(password, self.salt, iterations) if self.required else b""


# -- Client connection handler -------------------------------------------------
class ClientSession:
    def __init__(self, reader, writer, capturer: ScreenCapturer,
                 input_handler: InputHandler, auth_config: AuthConfig, fps: int,
                 on_count=None, throttle=None, channel=b"", clipboard=True):
        self.reader = reader
        self.writer = writer
        self.capturer = capturer
        self.input = input_handler
        self.auth = auth_config
        self.fps = fps
        self.addr = writer.get_extra_info("peername")
        # Never anything but False here. The original set this from whether a
        # password had been configured, which authenticated every connection
        # to a server started without one before the client said a word.
        self.authenticated = False
        self.on_count = on_count
        self.throttle = throttle
        self.channel = channel
        self.clipboard = clipboard and CLIP_OK

    async def run(self):
        log.info("New connection from %s", self.addr)
        counted = False
        try:
            if not await self._handshake():
                return
            if self.on_count:
                self.on_count(1)
                counted = True
            # Start streaming and receiving concurrently
            await asyncio.gather(
                self._stream_frames(),
                self._receive_input(),
            )
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError,
                asyncio.CancelledError):
            log.info("Client %s disconnected.", self.addr)
        except asyncio.TimeoutError:
            log.warning("Client %s timed out.", self.addr)
        except ProtocolError as e:
            log.warning("Protocol violation from %s: %s", self.addr, e)
        except ssl.SSLError as e:
            log.warning("TLS error from %s: %s", self.addr, e)
        except Exception:
            # Anything reaching here is a bug in this program rather than a
            # peer misbehaving, so it gets a traceback and not one line of str().
            log.error("Unhandled session error from %s:\n%s",
                      self.addr, traceback.format_exc())
        finally:
            if counted and self.on_count:
                self.on_count(-1)
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass

    async def _reject(self, reason):
        log.warning("Rejecting %s: %s", self.addr, reason)
        try:
            self.writer.write(encode_json(MSG_HELLO_ACK, {"ok": False, "error": reason}))
            await self.writer.drain()
        except (ConnectionResetError, BrokenPipeError, ssl.SSLError):
            pass

    async def _handshake(self) -> bool:
        """Run the three-step handshake. Returns True if the client is in."""
        if self.throttle:
            wait = self.throttle.retry_after(self.addr)
            if wait > 0:
                await self._reject("Too many failed attempts - try again in %d seconds"
                                   % math.ceil(wait))
                return False

        msg_type, data = await read_json(self.reader, {MSG_HELLO}, HANDSHAKE_TIMEOUT)
        version = data.get("protocol")
        if version != PROTOCOL_VERSION:
            await self._reject(
                "Protocol version mismatch: this server speaks %d, the viewer speaks %s. "
                "Update both ends."
                % (PROTOCOL_VERSION, version if version is not None else "1"))
            return False

        nonce = authlib.new_nonce()
        self.writer.write(encode_json(MSG_CHALLENGE, {
            "protocol": PROTOCOL_VERSION,
            "auth_required": self.auth.required,
            "salt": self.auth.salt.hex(),
            "iterations": self.auth.iterations,
            "nonce": nonce.hex(),
        }))
        await self.writer.drain()

        msg_type, data = await read_json(self.reader, {MSG_AUTH}, HANDSHAKE_TIMEOUT)

        confirmation = ""
        if self.auth.required:
            expected = authlib.client_response(self.auth.key, nonce, self.channel)
            if not authlib.verify(expected, data.get("response")):
                locked = self.throttle.record_failure(self.addr) if self.throttle else False
                await asyncio.sleep(AUTH_FAIL_DELAY)
                await self._reject("Wrong password")
                if locked:
                    log.warning("Locking out %s after repeated failures.", self.addr)
                return False
            if self.throttle:
                self.throttle.record_success(self.addr)
            # Proves to the viewer that this server knows the password too, so
            # a session is not opened to whatever answered the port.
            confirmation = authlib.server_confirmation(self.auth.key, nonce, self.channel)

        self.authenticated = True
        w, h = self.capturer.get_resolution()
        self.writer.write(encode_json(MSG_HELLO_ACK, {
            "ok": True,
            "protocol": PROTOCOL_VERSION,
            "screen_width": w,
            "screen_height": h,
            "fps": self.fps,
            "clipboard": self.clipboard,
            "server_proof": confirmation,
        }))
        await self.writer.drain()
        log.info("Client %s authenticated. Screen: %dx%d", self.addr, w, h)
        return True

    async def _stream_frames(self):
        interval = 1.0 / self.fps
        loop = asyncio.get_event_loop()
        last_seq = -1
        while True:
            t0 = loop.time()
            seq, frame = self.capturer.get_frame()
            if frame and seq != last_seq:
                self.writer.write(encode_message(MSG_FRAME, frame))
                # drain() is the backpressure: a viewer that cannot keep up
                # holds this loop here, and when it clears we take the current
                # frame rather than the one we were holding, so a slow link
                # drops frames instead of falling further behind.
                await self.writer.drain()
                last_seq = seq
            elapsed = loop.time() - t0
            await asyncio.sleep(max(0.0, interval - elapsed))

    async def _receive_input(self):
        while True:
            msg_type, payload = await read_message(self.reader, SESSION_ACCEPTS, IDLE_TIMEOUT)

            if msg_type == MSG_MOUSE_MOVE:
                d = decode_json(payload)
                self.input.on_mouse_move(d.get("nx"), d.get("ny"))

            elif msg_type == MSG_MOUSE_BTN:
                d = decode_json(payload)
                self.input.on_mouse_button(d.get("button"), bool(d.get("pressed")))

            elif msg_type == MSG_KEY:
                d = decode_json(payload)
                self.input.on_key(d.get("key"), bool(d.get("pressed")))

            elif msg_type == MSG_SCROLL:
                d = decode_json(payload)
                self.input.on_scroll(d.get("dx", 0), d.get("dy", 0))

            elif msg_type == MSG_CLIPBOARD_SEND:
                if self.clipboard:
                    d = decode_json(payload)
                    text = d.get("text", "")
                    if isinstance(text, str):
                        pyperclip.copy(text)
                        log.info("Clipboard updated from remote.")

            elif msg_type == MSG_PING:
                self.writer.write(encode_message(MSG_PONG, b""))
                await self.writer.drain()


# -- Main server ---------------------------------------------------------------
def validate_settings(port, quality, fps, max_clients):
    """Reject settings that would otherwise fail later, or confusingly."""
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535, got %r" % (port,))
    if not 1 <= quality <= 95:
        raise ValueError("quality must be between 1 and 95, got %r" % (quality,))
    if not 1 <= fps <= 60:
        # fps 0 divided into 1.0 in two places and took the server with it.
        raise ValueError("fps must be between 1 and 60, got %r" % (fps,))
    if max_clients < 1:
        raise ValueError("max-clients must be at least 1, got %r" % (max_clients,))


def build_tls(use_tls, tls_cert=None, tls_key=None):
    """Return (ssl_context, channel_binding); (None, b"") when TLS is off."""
    if not use_tls:
        return None, b""
    if bool(tls_cert) != bool(tls_key):
        raise ValueError("--tls-cert and --tls-key must be given together")
    if tls_cert:
        with open(tls_cert, "rb") as f:
            cert_pem = f.read()
        with open(tls_key, "rb") as f:
            key_pem = f.read()
    else:
        cert_pem, key_pem = tlsutil.generate_self_signed()
    return tlsutil.server_context(cert_pem, key_pem), tlsutil.cert_fingerprint(cert_pem)


async def create_server(host, port, capturer, input_handler, auth_config, fps,
                        on_client_count=None, ssl_context=None, channel=b"",
                        clipboard=True, max_clients=8, throttle=None):
    """Bind the listening socket.

    Split out of main() so a test can drive a real server on an ephemeral port
    without a screen, a certificate on disk, or a command line.
    """
    throttle = throttle if throttle is not None else AuthThrottle()
    client_count = 0
    open_connections = 0

    def _count(delta):
        nonlocal client_count
        client_count += delta
        if on_client_count:
            on_client_count(client_count)

    async def handle_client(reader, writer):
        nonlocal open_connections
        if open_connections >= max_clients:
            # Without this, opening sockets and saying nothing is enough to
            # keep every legitimate viewer out.
            log.warning("Refusing %s: %d connections already open.",
                        writer.get_extra_info("peername"), open_connections)
            writer.close()
            return
        open_connections += 1
        try:
            session = ClientSession(reader, writer, capturer, input_handler, auth_config,
                                    fps, on_count=_count, throttle=throttle,
                                    channel=channel, clipboard=clipboard)
            await session.run()
        finally:
            open_connections -= 1

    kwargs = {}
    if ssl_context is not None:
        kwargs["ssl"] = ssl_context
        kwargs["ssl_handshake_timeout"] = HANDSHAKE_TIMEOUT
    return await asyncio.start_server(handle_client, host, port, **kwargs)


async def main(host, port, quality, fps, password, on_client_count=None,
               allow_no_password=False, use_tls=True, tls_cert=None, tls_key=None,
               clipboard=True, max_clients=8):
    validate_settings(port, quality, fps, max_clients)
    if not password and not allow_no_password:
        raise ValueError(
            "A password is required. Pass --password, or --no-password if you "
            "really do want anyone who can reach this port to have your screen "
            "and your keyboard.")

    ssl_context, channel = build_tls(use_tls, tls_cert, tls_key)
    auth_config = AuthConfig(password, required=bool(password))

    capturer = ScreenCapturer(quality=quality, fps=fps)
    capturer.start()

    # Wait one frame so resolution is known
    await asyncio.sleep(0.3)
    w, h = capturer.get_resolution()
    input_handler = InputHandler(w, h)

    server = await create_server(host, port, capturer, input_handler, auth_config, fps,
                                 on_client_count=on_client_count, ssl_context=ssl_context,
                                 channel=channel, clipboard=clipboard,
                                 max_clients=max_clients)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    log.info("LANDesk server listening on %s", addrs)
    log.info("Screen: %dx%d | Quality: %d%% | FPS: %d | Max viewers: %d",
             w, h, quality, fps, max_clients)
    log.info("Encryption: %s", "TLS 1.2+" if ssl_context else "NONE - traffic is readable")
    log.info("Clipboard sharing: %s", "on" if clipboard else "off")
    if not password:
        log.warning("NO PASSWORD - anyone who can reach this port has your screen "
                    "and your keyboard.")
    if host not in ("127.0.0.1", "::1", "localhost"):
        log.warning("Listening on %s, which other machines can reach.", host)

    async with server:
        await server.serve_forever()


def resolve_password(args):
    """Work out the password without putting it in the process list.

    Order: --no-password, then --password, then $LANDESK_PASSWORD, then a
    prompt. --password is still accepted because scripts use it, but any user
    on the machine can read another user's command line, so it is not what the
    documentation recommends.
    """
    if args.no_password:
        return ""
    if args.password:
        return args.password
    env = os.environ.get("LANDESK_PASSWORD")
    if env:
        return env
    # Only prompt when there is a person to answer it. getpass on Windows
    # reads the console directly, so prompting a process whose output is a
    # pipe or a log file would wait for input that is never coming.
    if _is_interactive():
        try:
            return getpass.getpass("Connection password: ")
        except (EOFError, OSError):
            return ""
    return ""


def _is_interactive():
    for stream in (sys.stdin, sys.stdout):
        try:
            if stream is None or not stream.isatty():
                return False
        except (AttributeError, ValueError, OSError):
            return False
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LANDesk Server")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address (default: 127.0.0.1, this machine only). "
                             "Use 0.0.0.0 to accept connections from the LAN.")
    parser.add_argument("--port", type=int, default=9000, help="TCP port (default: 9000)")
    parser.add_argument("--quality", type=int, default=50,
                        help="JPEG quality 1-95 (default: 50). Higher = better image, "
                             "more bandwidth.")
    parser.add_argument("--fps", type=int, default=20, help="Target FPS 1-60 (default: 20)")
    parser.add_argument("--password", default="",
                        help="Connection password. Prefer $LANDESK_PASSWORD or the "
                             "prompt: a command line is visible to other users.")
    parser.add_argument("--no-password", action="store_true",
                        help="Run with no password at all. Anyone who can reach the "
                             "port gets full control.")
    parser.add_argument("--no-tls", action="store_true",
                        help="Disable encryption. Your screen and your keystrokes "
                             "cross the network in the clear.")
    parser.add_argument("--tls-cert", help="PEM certificate to use instead of a generated one")
    parser.add_argument("--tls-key", help="PEM private key for --tls-cert")
    parser.add_argument("--no-clipboard", action="store_true",
                        help="Do not accept clipboard content from the viewer")
    parser.add_argument("--max-clients", type=int, default=8,
                        help="Concurrent viewers allowed (default: 8)")
    args = parser.parse_args()

    try:
        password = resolve_password(args)
        asyncio.run(main(args.host, args.port, args.quality, args.fps, password,
                         allow_no_password=args.no_password,
                         use_tls=not args.no_tls,
                         tls_cert=args.tls_cert, tls_key=args.tls_key,
                         clipboard=not args.no_clipboard,
                         max_clients=args.max_clients))
    except KeyboardInterrupt:
        log.info("Server stopped.")
    except (ValueError, OSError, tlsutil.TLSUnavailable) as e:
        log.error("%s", e)
        sys.exit(1)
