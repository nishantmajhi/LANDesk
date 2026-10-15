"""The real viewer client against the real server, over a real socket.

Everything else in this suite tests one end at a time. This is the one that
would catch the two halves of the handshake drifting apart, so it uses
viewer.py's own LANDeskClient rather than a reimplementation of it - including
its background thread and its own event loop, which is how it runs in the app.
"""

import asyncio
import json
import struct
import threading
import unittest

from _support import (P, StubCapturer, RecordingInput, TLS_AVAILABLE, auth,
                      load_viewer, server, tlsutil)

PASSWORD = "hunter2"
TIMEOUT = 20.0


class ServerThread:
    """A LANDesk server on its own thread, the way the GUI runs one."""

    def __init__(self, password=PASSWORD, tls=True, frame=b"", handler=None):
        self.password = password
        self.tls = tls
        self.capturer = StubCapturer(frame=frame)
        self.input = RecordingInput()
        self.handler = handler          # a bare protocol handler, for fakes
        self.port = None
        self.channel = b""
        self._loop = None
        self._server = None
        self._thread = None

    async def _start(self):
        ssl_context, self.channel = server.build_tls(self.tls)
        if self.handler is not None:
            async def handle(reader, writer):
                await self.handler(self, reader, writer)
            kwargs = {"ssl": ssl_context} if ssl_context else {}
            self._server = await asyncio.start_server(handle, "127.0.0.1", 0, **kwargs)
        else:
            self._server = await server.create_server(
                "127.0.0.1", 0, self.capturer, self.input,
                server.AuthConfig(self.password, required=bool(self.password)), 30,
                ssl_context=ssl_context, channel=self.channel)
        self.port = self._server.sockets[0].getsockname()[1]

    def __enter__(self):
        ready = threading.Event()
        failure = []

        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._start())
            except Exception as e:     # pragma: no cover - a broken test setup
                failure.append(e)
                ready.set()
                return
            ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        if not ready.wait(TIMEOUT) or failure:
            raise AssertionError("server thread did not start: %r" % (failure or "timeout"))
        return self

    def __exit__(self, *exc):
        async def shutdown():
            # Cancel the session handlers before waiting on the server:
            # wait_closed() does not return while one is still running, and
            # a handler parked on a read would hold shutdown for its timeout.
            self._server.close()
            pending = [t for t in asyncio.all_tasks(self._loop)
                       if t is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await self._server.wait_closed()

        done = asyncio.run_coroutine_threadsafe(shutdown(), self._loop)
        try:
            done.result(TIMEOUT)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(TIMEOUT)


class Result:
    """Collects what the client tells its callbacks, from the client's thread."""

    def __init__(self):
        self.connected = threading.Event()
        self.errored = threading.Event()
        self.framed = threading.Event()
        self.size = None
        self.error = None
        self.frames = []

    def on_connected(self, w, h):
        self.size = (w, h)
        self.connected.set()

    def on_error(self, message):
        self.error = message
        self.errored.set()

    def on_frame(self, payload):
        self.frames.append(payload)
        self.framed.set()

    def on_disconnect(self):
        pass

    def client(self, viewer, port, password=PASSWORD, tls=True):
        return viewer.LANDeskClient(
            host="127.0.0.1", port=port, password=password,
            on_frame=self.on_frame, on_connected=self.on_connected,
            on_error=self.on_error, on_disconnect=self.on_disconnect,
            use_tls=tls)


def _read(reader, timeout=TIMEOUT):
    return asyncio.wait_for(reader.readexactly(P.HEADER_SIZE), timeout)


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.viewer = load_viewer()

    def _session(self, tls):
        result = Result()
        with ServerThread(tls=tls, frame=b"\xff\xd8screen") as srv:
            client = result.client(self.viewer, srv.port, tls=tls)
            client.start()
            self.assertTrue(result.connected.wait(TIMEOUT),
                            "did not connect: %s" % result.error)
            self.assertEqual(result.size, (800, 600))
            self.assertTrue(result.framed.wait(TIMEOUT), "no frame arrived")
            self.assertEqual(result.frames[0], b"\xff\xd8screen")

            client.send(P.MSG_MOUSE_MOVE, {"nx": 0.5, "ny": 0.25})
            for _ in range(int(TIMEOUT * 20)):
                if srv.input.events:
                    break
                threading.Event().wait(0.05)
            self.assertIn(("move", 0.5, 0.25), srv.input.events)
            client.stop()
        self.assertIsNone(result.error)

    @unittest.skipUnless(TLS_AVAILABLE, "cryptography is not installed")
    def test_a_whole_session_over_tls(self):
        self._session(tls=True)

    def test_a_whole_session_without_tls(self):
        self._session(tls=False)

    def test_wrong_password_is_reported_to_the_user(self):
        result = Result()
        with ServerThread(tls=False) as srv:
            client = result.client(self.viewer, srv.port, password="wrong", tls=False)
            client.start()
            self.assertTrue(result.errored.wait(TIMEOUT))
            client.stop()
        self.assertIn("password", result.error.lower())
        self.assertFalse(result.connected.is_set())

    @unittest.skipUnless(TLS_AVAILABLE, "cryptography is not installed")
    def test_plaintext_server_is_not_silently_accepted(self):
        # A viewer that fell back to plaintext when TLS failed would let an
        # attacker ask for exactly that. It reports instead.
        result = Result()
        with ServerThread(tls=False) as srv:
            client = result.client(self.viewer, srv.port, tls=True)
            client.start()
            self.assertTrue(result.errored.wait(TIMEOUT))
            client.stop()
        self.assertFalse(result.connected.is_set())


class ImpostorServer(unittest.TestCase):
    """A server that accepts anything but cannot prove it knows the password."""

    @classmethod
    def setUpClass(cls):
        cls.viewer = load_viewer()

    @staticmethod
    async def _impostor(srv, reader, writer):
        header = await reader.readexactly(P.HEADER_SIZE)
        _t, length = struct.unpack(">BI", header)
        await reader.readexactly(length)
        nonce = auth.new_nonce()
        writer.write(P.encode_json(P.MSG_CHALLENGE, {
            "protocol": P.PROTOCOL_VERSION,
            "auth_required": True,
            "salt": auth.new_salt().hex(),
            "iterations": 1000,
            "nonce": nonce.hex(),
        }))
        await writer.drain()
        header = await reader.readexactly(P.HEADER_SIZE)
        _t, length = struct.unpack(">BI", header)
        await reader.readexactly(length)
        # Says yes to whatever it was sent, and makes up a proof.
        writer.write(P.encode_json(P.MSG_HELLO_ACK, {
            "ok": True, "protocol": P.PROTOCOL_VERSION,
            "screen_width": 800, "screen_height": 600, "fps": 20,
            "server_proof": "0" * 64,
        }))
        await writer.drain()
        await asyncio.sleep(5)

    def test_viewer_refuses_a_server_that_cannot_prove_the_password(self):
        result = Result()
        with ServerThread(tls=False, handler=self._impostor) as srv:
            client = result.client(self.viewer, srv.port, tls=False)
            client.start()
            self.assertTrue(result.errored.wait(TIMEOUT))
            client.stop()
        self.assertFalse(result.connected.is_set())
        self.assertIn("prove", result.error.lower())


if __name__ == "__main__":
    unittest.main()
