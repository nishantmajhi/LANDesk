"""Shared plumbing for the LANDesk tests.

The server and the viewer are scripts rather than an installed package, and
the viewer is loaded the same way the frozen build loads it, so both are
imported here by path once and reused.
"""

import asyncio
import importlib.util
import json
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "server")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


server = _load("landesk_server", "server/server.py")
protocol = _load("landesk_protocol", "shared/protocol.py")
auth = _load("landesk_auth", "shared/auth.py")
tlsutil = _load("landesk_tlsutil", "shared/tlsutil.py")
ratelimit = _load("landesk_ratelimit", "server/ratelimit.py")

P = protocol
TLS_AVAILABLE = tlsutil.CRYPTOGRAPHY_AVAILABLE


def load_viewer():
    """Import the viewer module. Pulls in tkinter, so only where it is used."""
    if "landesk_viewer" in sys.modules:
        return sys.modules["landesk_viewer"]
    return _load("landesk_viewer", "viewer/viewer.py")


# -- Stubs --------------------------------------------------------------------
class StubCapturer:
    """A screen capturer with no screen behind it."""

    def __init__(self, width=800, height=600, frame=b""):
        self._w, self._h = width, height
        self._seq = 0
        self._frame = frame
        if frame:
            self._seq = 1

    def push(self, frame):
        self._seq += 1
        self._frame = frame

    def get_frame(self):
        return self._seq, self._frame

    def get_resolution(self):
        return self._w, self._h


class RecordingInput:
    """Stands in for InputHandler and remembers what it was told to do."""

    def __init__(self):
        self.events = []

    def on_mouse_move(self, nx, ny):
        self.events.append(("move", nx, ny))

    def on_mouse_button(self, button, pressed):
        self.events.append(("button", button, pressed))

    def on_scroll(self, dx, dy):
        self.events.append(("scroll", dx, dy))

    def on_key(self, key, pressed):
        self.events.append(("key", key, pressed))


# -- Wire helpers -------------------------------------------------------------
def frame_bytes(msg_type, payload=b""):
    return struct.pack(">BI", msg_type, len(payload)) + payload


def json_frame(msg_type, data):
    return frame_bytes(msg_type, json.dumps(data).encode("utf-8"))


async def read_frame(reader, timeout=5.0):
    """Read one message without any of the checks the real ends apply."""
    header = await asyncio.wait_for(reader.readexactly(P.HEADER_SIZE), timeout)
    msg_type, length = struct.unpack(">BI", header)
    payload = await asyncio.wait_for(reader.readexactly(length), timeout) if length else b""
    return msg_type, payload


async def read_json_frame(reader, timeout=5.0):
    msg_type, payload = await read_frame(reader, timeout)
    return msg_type, json.loads(payload.decode("utf-8"))


class Harness:
    """A real server on an ephemeral port, with stubbed screen and input."""

    def __init__(self, password="hunter2", tls=False, clipboard=True,
                 max_clients=8, throttle=None, frame=b"", fps=20):
        self.password = password
        self.tls = tls
        self.capturer = StubCapturer(frame=frame)
        self.input = RecordingInput()
        self.throttle = throttle
        self.counts = []
        self._server = None
        self._clipboard = clipboard
        self._max_clients = max_clients
        self._fps = fps
        self.port = None
        self.channel = b""
        self.auth_config = None

    async def __aenter__(self):
        ssl_context, self.channel = server.build_tls(self.tls)
        self.auth_config = server.AuthConfig(self.password, required=bool(self.password))
        self._server = await server.create_server(
            "127.0.0.1", 0, self.capturer, self.input, self.auth_config, self._fps,
            on_client_count=self.counts.append, ssl_context=ssl_context,
            channel=self.channel, clipboard=self._clipboard,
            max_clients=self._max_clients, throttle=self.throttle)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    async def open(self):
        """Connect a raw client to this server."""
        ssl_ctx = tlsutil.client_context() if self.tls else None
        return await asyncio.open_connection("127.0.0.1", self.port, ssl=ssl_ctx)

    async def handshake(self, reader, writer, password=None, channel=None,
                        auth_payload=None):
        """Do the client half. Returns (ack dict, auth payload that was sent)."""
        writer.write(json_frame(P.MSG_HELLO, {"protocol": P.PROTOCOL_VERSION}))
        await writer.drain()
        msg_type, challenge = await read_json_frame(reader)
        if msg_type == P.MSG_HELLO_ACK:
            return challenge, None
        if auth_payload is None:
            key = auth.derive_key(
                self.password if password is None else password,
                bytes.fromhex(challenge["salt"]), challenge["iterations"])
            response = auth.client_response(
                key, bytes.fromhex(challenge["nonce"]),
                self.channel if channel is None else channel)
            auth_payload = {"response": response}
        writer.write(json_frame(P.MSG_AUTH, auth_payload))
        await writer.drain()
        _msg_type, ack = await read_json_frame(reader)
        return ack, auth_payload


def free_port():
    """A port that was free a moment ago, for the paths that need a number."""
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run(coro, timeout=30.0):
    """Run a coroutine with a ceiling, so a hung test fails instead of hanging."""
    return asyncio.run(asyncio.wait_for(coro, timeout))
