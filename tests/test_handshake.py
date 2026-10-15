"""The handshake, against a real server on a real socket.

Each of these was written against the original code first, where the first
four of them passed - which is to say the attack worked. They are the four P1
findings in review.md.
"""

import asyncio
import struct
import time
import unittest

from _support import (P, Harness, TLS_AVAILABLE, auth, free_port, ratelimit, run,
                      server, json_frame, frame_bytes, read_json_frame, read_frame)

PASSWORD = "hunter2"


def setUpModule():
    # Every rejection sleeps for AUTH_FAIL_DELAY. That is the point of it, and
    # test_rejection_is_slow checks it is still there, but the rest of the file
    # does not need to spend a second per wrong password.
    global _REAL_DELAY
    _REAL_DELAY = server.AUTH_FAIL_DELAY
    server.AUTH_FAIL_DELAY = 0.01


def tearDownModule():
    server.AUTH_FAIL_DELAY = _REAL_DELAY


class HappyPath(unittest.TestCase):
    def test_correct_password_gets_in(self):
        async def go():
            async with Harness(PASSWORD) as h:
                reader, writer = await h.open()
                ack, _ = await h.handshake(reader, writer)
                writer.close()
                return ack
        ack = run(go())
        self.assertTrue(ack["ok"])
        self.assertEqual(ack["screen_width"], 800)
        self.assertEqual(ack["screen_height"], 600)
        self.assertEqual(ack["protocol"], P.PROTOCOL_VERSION)

    def test_server_proves_it_knows_the_password(self):
        # Without this a viewer would hand a session's worth of screen to
        # anything that answered the port and said yes.
        async def go():
            async with Harness(PASSWORD) as h:
                reader, writer = await h.open()
                writer.write(json_frame(P.MSG_HELLO, {"protocol": P.PROTOCOL_VERSION}))
                await writer.drain()
                _t, challenge = await read_json_frame(reader)
                key = auth.derive_key(PASSWORD, bytes.fromhex(challenge["salt"]),
                                      challenge["iterations"])
                nonce = bytes.fromhex(challenge["nonce"])
                writer.write(json_frame(P.MSG_AUTH, {
                    "response": auth.client_response(key, nonce, h.channel)}))
                await writer.drain()
                _t, ack = await read_json_frame(reader)
                writer.close()
                return ack, auth.server_confirmation(key, nonce, h.channel)
        ack, expected = run(go())
        self.assertTrue(ack["ok"])
        self.assertEqual(ack["server_proof"], expected)

    def test_input_reaches_the_handler(self):
        async def go():
            async with Harness(PASSWORD) as h:
                reader, writer = await h.open()
                await h.handshake(reader, writer)
                writer.write(json_frame(P.MSG_MOUSE_MOVE, {"nx": 0.25, "ny": 0.75}))
                writer.write(json_frame(P.MSG_KEY, {"key": "a", "pressed": True}))
                await writer.drain()
                for _ in range(50):
                    if len(h.input.events) >= 2:
                        break
                    await asyncio.sleep(0.02)
                writer.close()
                return h.input.events
        events = run(go())
        self.assertIn(("move", 0.25, 0.75), events)
        self.assertIn(("key", "a", True), events)

    def test_frames_arrive(self):
        async def go():
            async with Harness(PASSWORD, frame=b"\xff\xd8jpeg", fps=30) as h:
                reader, writer = await h.open()
                await h.handshake(reader, writer)
                msg_type, payload = await read_frame(reader)
                writer.close()
                return msg_type, payload
        msg_type, payload = run(go())
        self.assertEqual(msg_type, P.MSG_FRAME)
        self.assertEqual(payload, b"\xff\xd8jpeg")

    def test_the_same_frame_is_not_sent_twice(self):
        # The capturer is not producing anything new, so after the first frame
        # the stream should be quiet rather than repeating itself.
        async def go():
            async with Harness(PASSWORD, frame=b"one", fps=30) as h:
                reader, writer = await h.open()
                await h.handshake(reader, writer)
                first = await read_frame(reader)
                try:
                    second = await asyncio.wait_for(read_frame(reader), 0.4)
                except asyncio.TimeoutError:
                    second = None
                h.capturer.push(b"two")
                third = await read_frame(reader)
                writer.close()
                return first, second, third
        first, second, third = run(go())
        self.assertEqual(first[1], b"one")
        self.assertIsNone(second)
        self.assertEqual(third[1], b"two")


class Authentication(unittest.TestCase):
    def test_wrong_password_is_refused(self):
        async def go():
            async with Harness(PASSWORD) as h:
                reader, writer = await h.open()
                ack, _ = await h.handshake(reader, writer, password="wrong")
                writer.close()
                return ack
        ack = run(go())
        self.assertFalse(ack["ok"])
        self.assertIn("password", ack["error"].lower())

    def test_captured_handshake_does_not_replay(self):
        # P1. The original sent SHA-256(password) as the credential, so
        # anyone who saw one handshake could authenticate forever.
        async def go():
            async with Harness(PASSWORD) as h:
                reader, writer = await h.open()
                ack, captured = await h.handshake(reader, writer)
                writer.close()
                reader2, writer2 = await h.open()
                replayed, _ = await h.handshake(reader2, writer2, auth_payload=captured)
                writer2.close()
                return ack, replayed
        ack, replayed = run(go())
        self.assertTrue(ack["ok"], "the legitimate handshake should succeed")
        self.assertFalse(replayed["ok"], "the replayed one should not")

    def test_a_relayed_response_does_not_authenticate(self):
        # The channel binding. A machine in the middle has to present its own
        # certificate, so what it collects is bound to the wrong fingerprint.
        async def go():
            async with Harness(PASSWORD) as h:
                reader, writer = await h.open()
                ack, _ = await h.handshake(reader, writer, channel=b"\x99" * 32)
                writer.close()
                return ack
        self.assertFalse(run(go())["ok"])

    def test_missing_response_is_refused(self):
        async def go():
            async with Harness(PASSWORD) as h:
                results = []
                for payload in ({}, {"response": None}, {"response": ""},
                                {"response": []}, {"response": "0" * 64}):
                    reader, writer = await h.open()
                    ack, _ = await h.handshake(reader, writer, auth_payload=payload)
                    writer.close()
                    results.append(ack["ok"])
                return results
        self.assertEqual(run(go()), [False] * 5)

    def test_session_starts_unauthenticated_even_with_no_password(self):
        # P1. The original computed authenticated = (password_hash == ""), so
        # a server started without a password had already authenticated every
        # connection before the client said anything.
        class FakeWriter:
            def get_extra_info(self, _name):
                return ("127.0.0.1", 1234)

        session = server.ClientSession(
            None, FakeWriter(), None, None,
            server.AuthConfig("", required=False), 20)
        self.assertFalse(session.authenticated)

    def test_no_password_server_still_requires_the_handshake(self):
        async def go():
            async with Harness("") as h:
                reader, writer = await h.open()
                ack, _ = await h.handshake(reader, writer, auth_payload={"response": ""})
                writer.close()
                return ack
        ack = run(go())
        self.assertTrue(ack["ok"])
        self.assertEqual(ack["server_proof"], "")

    def test_rejection_is_slow(self):
        # P2. The original answered a wrong password immediately: 968 guesses
        # a second on loopback. A delay on every rejection is half the fix.
        self.assertGreaterEqual(_REAL_DELAY, 0.5)

        async def go():
            server.AUTH_FAIL_DELAY = 0.35
            try:
                async with Harness(PASSWORD) as h:
                    reader, writer = await h.open()
                    started = time.monotonic()
                    await h.handshake(reader, writer, password="wrong")
                    writer.close()
                    return time.monotonic() - started
            finally:
                server.AUTH_FAIL_DELAY = 0.01
        self.assertGreaterEqual(run(go()), 0.3)

    def test_repeated_failures_lock_the_address_out(self):
        # P2, the other half: the delay alone still allows a slow grind.
        async def go():
            throttle = ratelimit.AuthThrottle(max_failures=3, lockout=60.0)
            async with Harness(PASSWORD, throttle=throttle) as h:
                answers = []
                for _ in range(4):
                    reader, writer = await h.open()
                    ack, _ = await h.handshake(reader, writer, password="wrong")
                    writer.close()
                    answers.append(ack["error"])
                # Even the right password is refused while locked out.
                reader, writer = await h.open()
                correct, _ = await h.handshake(reader, writer)
                writer.close()
                return answers, correct
        answers, correct = run(go())
        self.assertIn("Wrong password", answers[0])
        self.assertIn("Too many failed attempts", answers[3])
        self.assertFalse(correct["ok"])
        self.assertIn("Too many failed attempts", correct["error"])


class MalformedInput(unittest.TestCase):
    def test_four_gigabyte_payload_claim_is_refused_at_once(self):
        # P1. The original called readexactly(length) with whatever the header
        # claimed. This test hung against it until the timeout fired.
        async def go():
            async with Harness(PASSWORD) as h:
                reader, writer = await h.open()
                writer.write(struct.pack(">BI", P.MSG_HELLO, 0xFFFFFFFF) + b"{}")
                await writer.drain()
                # The server drops the connection rather than answering, so
                # what we are waiting for is the close.
                with self.assertRaises(asyncio.IncompleteReadError):
                    await asyncio.wait_for(reader.readexactly(1), 3.0)
                writer.close()
        run(go(), timeout=15)

    def test_a_frame_sent_to_the_server_is_refused(self):
        async def go():
            async with Harness(PASSWORD) as h:
                reader, writer = await h.open()
                await h.handshake(reader, writer)
                writer.write(struct.pack(">BI", P.MSG_FRAME, 30 * 1024 * 1024))
                await writer.drain()
                with self.assertRaises(asyncio.IncompleteReadError):
                    await asyncio.wait_for(reader.readexactly(10 ** 7), 3.0)
                writer.close()
        run(go(), timeout=15)

    def test_skipping_the_handshake_gets_nowhere(self):
        async def go():
            async with Harness(PASSWORD) as h:
                reader, writer = await h.open()
                writer.write(json_frame(P.MSG_MOUSE_MOVE, {"nx": 0.5, "ny": 0.5}))
                await writer.drain()
                with self.assertRaises(asyncio.IncompleteReadError):
                    await asyncio.wait_for(reader.readexactly(1), 3.0)
                writer.close()
                return h.input.events
        self.assertEqual(run(go(), timeout=15), [])

    def test_version_one_client_gets_told_why(self):
        # An old viewer sends its password hash in the HELLO and no version.
        async def go():
            async with Harness(PASSWORD) as h:
                reader, writer = await h.open()
                writer.write(json_frame(P.MSG_HELLO, {"password_hash": "0" * 64}))
                await writer.drain()
                _t, ack = await read_json_frame(reader)
                writer.close()
                return ack
        ack = run(go())
        self.assertFalse(ack["ok"])
        self.assertIn("version", ack["error"].lower())

    def test_silence_during_the_handshake_times_out(self):
        original = server.HANDSHAKE_TIMEOUT
        server.HANDSHAKE_TIMEOUT = 0.3

        async def go():
            async with Harness(PASSWORD) as h:
                reader, writer = await h.open()   # connect, then say nothing
                with self.assertRaises(asyncio.IncompleteReadError):
                    await asyncio.wait_for(reader.readexactly(1), 3.0)
                writer.close()
        try:
            run(go(), timeout=15)
        finally:
            server.HANDSHAKE_TIMEOUT = original

    def test_junk_payload_does_not_take_the_server_down(self):
        async def go():
            async with Harness(PASSWORD) as h:
                reader, writer = await h.open()
                writer.write(frame_bytes(P.MSG_HELLO, b"\x00not json at all"))
                await writer.drain()
                writer.close()
                # The server survives it and the next client still gets in.
                reader2, writer2 = await h.open()
                ack, _ = await h.handshake(reader2, writer2)
                writer2.close()
                return ack
        self.assertTrue(run(go())["ok"])

    def test_too_many_connections_are_refused(self):
        async def go():
            async with Harness(PASSWORD, max_clients=2) as h:
                held = [await h.open() for _ in range(2)]
                reader, writer = await h.open()
                with self.assertRaises(asyncio.IncompleteReadError):
                    await asyncio.wait_for(reader.readexactly(1), 3.0)
                writer.close()
                for _r, w in held:
                    w.close()
        run(go(), timeout=15)


@unittest.skipUnless(TLS_AVAILABLE, "cryptography is not installed")
class OverTLS(unittest.TestCase):
    def test_handshake_works_over_tls(self):
        async def go():
            async with Harness(PASSWORD, tls=True) as h:
                reader, writer = await h.open()
                ack, _ = await h.handshake(reader, writer)
                self.assertNotEqual(h.channel, b"")
                writer.close()
                return ack
        self.assertTrue(run(go())["ok"])

    def test_the_channel_binding_is_the_servers_certificate(self):
        from _support import tlsutil

        async def go():
            async with Harness(PASSWORD, tls=True) as h:
                reader, writer = await h.open()
                seen = tlsutil.peer_fingerprint(writer)
                writer.close()
                return seen, h.channel
        seen, expected = run(go())
        self.assertEqual(seen, expected)
        self.assertEqual(len(seen), 32)

    def test_a_plaintext_client_cannot_talk_to_a_tls_server(self):
        async def go():
            async with Harness(PASSWORD, tls=True) as h:
                reader, writer = await asyncio.open_connection("127.0.0.1", h.port)
                writer.write(json_frame(P.MSG_HELLO, {"protocol": P.PROTOCOL_VERSION}))
                await writer.drain()
                with self.assertRaises((asyncio.IncompleteReadError,
                                        ConnectionResetError, OSError)):
                    await asyncio.wait_for(reader.readexactly(1), 5.0)
                writer.close()
        run(go(), timeout=20)

    def test_a_relay_with_its_own_certificate_gets_nothing(self):
        # The full attack: a machine in the middle terminates TLS with its own
        # certificate, collects the viewer's response, and offers it to the
        # real server. The response is bound to the certificate the viewer
        # saw, which is not the one the real server has.
        from _support import tlsutil

        async def go():
            async with Harness(PASSWORD, tls=True) as h:
                relay_cert, relay_key = tlsutil.generate_self_signed("relay")
                relay_channel = tlsutil.cert_fingerprint(relay_cert)
                self.assertNotEqual(relay_channel, h.channel)

                # What the viewer would have sent to the relay.
                reader, writer = await h.open()
                writer.write(json_frame(P.MSG_HELLO, {"protocol": P.PROTOCOL_VERSION}))
                await writer.drain()
                _t, challenge = await read_json_frame(reader)
                key = auth.derive_key(PASSWORD, bytes.fromhex(challenge["salt"]),
                                      challenge["iterations"])
                stolen = auth.client_response(
                    key, bytes.fromhex(challenge["nonce"]), relay_channel)
                writer.write(json_frame(P.MSG_AUTH, {"response": stolen}))
                await writer.drain()
                _t, ack = await read_json_frame(reader)
                writer.close()
                return ack
        self.assertFalse(run(go(), timeout=20)["ok"])


class Configuration(unittest.TestCase):
    def test_a_password_is_required(self):
        # P1. --host defaulted to every interface and --password to empty.
        with self.assertRaises(ValueError) as caught:
            run(server.main("127.0.0.1", free_port(), 50, 20, ""))
        self.assertIn("password is required", str(caught.exception))

    def test_no_password_has_to_be_asked_for(self):
        # Not asserting it starts - that needs a screen. Asserting that the
        # explicit flag is what gets past the check.
        async def go():
            task = asyncio.ensure_future(
                server.main("127.0.0.1", free_port(), 50, 20, "", allow_no_password=True,
                            use_tls=False))
            await asyncio.sleep(0.6)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return True
        self.assertTrue(run(go(), timeout=20))

    def test_the_default_host_is_loopback(self):
        import argparse
        import re
        source = open(server.__file__, encoding="utf-8").read()
        match = re.search(r'"--host",\s*default="([^"]*)"', source)
        self.assertEqual(match.group(1), "127.0.0.1")

    def test_settings_are_validated(self):
        for args in ((0, 50, 20, 8), (70000, 50, 20, 8), (9000, 0, 20, 8),
                     (9000, 96, 20, 8), (9000, 50, 0, 8), (9000, 50, 61, 8),
                     (9000, 50, 20, 0)):
            with self.assertRaises(ValueError):
                server.validate_settings(*args)

    def test_sane_settings_pass(self):
        server.validate_settings(9000, 50, 20, 8)

    def test_tls_needs_both_halves_of_a_certificate(self):
        with self.assertRaises(ValueError):
            server.build_tls(True, tls_cert="cert.pem")
        with self.assertRaises(ValueError):
            server.build_tls(True, tls_key="key.pem")

    def test_no_tls_means_no_channel_binding(self):
        ctx, channel = server.build_tls(False)
        self.assertIsNone(ctx)
        self.assertEqual(channel, b"")


if __name__ == "__main__":
    unittest.main()
