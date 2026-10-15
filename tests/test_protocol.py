"""The wire format, and the two checks that happen before a payload is read."""

import asyncio
import struct
import unittest

from _support import P, run


class Framing(unittest.TestCase):
    def test_roundtrip(self):
        msg = P.encode_message(P.MSG_FRAME, b"jpeg-bytes")
        self.assertEqual(len(msg), P.HEADER_SIZE + 10)
        msg_type, length = P.decode_header(msg[:P.HEADER_SIZE])
        self.assertEqual((msg_type, length), (P.MSG_FRAME, 10))
        self.assertEqual(msg[P.HEADER_SIZE:], b"jpeg-bytes")

    def test_header_is_five_bytes_big_endian(self):
        self.assertEqual(P.encode_message(0x01, b"")[:5], b"\x01\x00\x00\x00\x00")

    def test_json_roundtrip(self):
        payload = P.encode_json(P.MSG_HELLO, {"protocol": 2})[P.HEADER_SIZE:]
        self.assertEqual(P.decode_json(payload), {"protocol": 2})

    def test_decode_json_rejects_non_objects(self):
        # Callers index the result; a bare list would surface as a TypeError
        # somewhere much less obvious.
        for payload in (b"[1, 2]", b'"text"', b"7", b"null"):
            with self.assertRaises(P.ProtocolError):
                P.decode_json(payload)

    def test_decode_json_rejects_junk(self):
        for payload in (b"{not json", b"\xff\xfe", b""):
            with self.assertRaises(P.ProtocolError):
                P.decode_json(payload)


class PayloadLimits(unittest.TestCase):
    def test_control_messages_are_capped_small(self):
        P.check_message(P.MSG_HELLO, P.DEFAULT_MAX_PAYLOAD, P.SERVER_ACCEPTS)
        with self.assertRaises(P.ProtocolError):
            P.check_message(P.MSG_HELLO, P.DEFAULT_MAX_PAYLOAD + 1, P.SERVER_ACCEPTS)

    def test_four_gigabyte_claim_is_refused(self):
        # The original read whatever length the header claimed, so one packet
        # asking for 4 GiB was enough to exhaust the server's memory.
        with self.assertRaises(P.ProtocolError):
            P.check_message(P.MSG_HELLO, 0xFFFFFFFF, P.SERVER_ACCEPTS)

    def test_frames_get_a_larger_cap_but_still_a_cap(self):
        P.check_message(P.MSG_FRAME, P.MAX_PAYLOAD[P.MSG_FRAME], P.CLIENT_ACCEPTS)
        with self.assertRaises(P.ProtocolError):
            P.check_message(P.MSG_FRAME, P.MAX_PAYLOAD[P.MSG_FRAME] + 1, P.CLIENT_ACCEPTS)

    def test_server_does_not_accept_frames(self):
        # A frame is server-to-viewer. Accepting the type on the server would
        # also hand a peer the 32 MiB ceiling that goes with it.
        self.assertNotIn(P.MSG_FRAME, P.SERVER_ACCEPTS)
        with self.assertRaises(P.ProtocolError):
            P.check_message(P.MSG_FRAME, 1024, P.SERVER_ACCEPTS)

    def test_viewer_does_not_accept_input_events(self):
        for msg_type in (P.MSG_KEY, P.MSG_MOUSE_MOVE, P.MSG_AUTH, P.MSG_HELLO):
            self.assertNotIn(msg_type, P.CLIENT_ACCEPTS)

    def test_unknown_type_is_refused(self):
        with self.assertRaises(P.ProtocolError):
            P.check_message(0x7F, 1, P.SERVER_ACCEPTS)


def _reader(data, eof=True):
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    if eof:
        reader.feed_eof()
    return reader


class ReadMessage(unittest.TestCase):
    def test_reads_a_whole_message(self):
        async def go():
            reader = _reader(P.encode_json(P.MSG_HELLO, {"protocol": 2}))
            return await P.read_message(reader, P.SERVER_ACCEPTS)
        msg_type, payload = run(go())
        self.assertEqual(msg_type, P.MSG_HELLO)
        self.assertEqual(P.decode_json(payload), {"protocol": 2})

    def test_oversized_header_costs_five_bytes(self):
        # The header is refused with no payload behind it at all, which is the
        # point: the check happens before anything is allocated.
        async def go():
            reader = _reader(struct.pack(">BI", P.MSG_HELLO, 0xFFFFFFFF), eof=False)
            await P.read_message(reader, P.SERVER_ACCEPTS)
        with self.assertRaises(P.ProtocolError):
            run(go(), timeout=5)

    def test_timeout_applies_to_a_stalled_peer(self):
        async def go():
            reader = _reader(b"", eof=False)   # connected, says nothing
            await P.read_message(reader, P.SERVER_ACCEPTS, timeout=0.05)
        with self.assertRaises(asyncio.TimeoutError):
            run(go(), timeout=5)

    def test_truncated_payload_raises_incomplete_read(self):
        async def go():
            reader = _reader(struct.pack(">BI", P.MSG_HELLO, 20) + b"short")
            await P.read_message(reader, P.SERVER_ACCEPTS)
        with self.assertRaises(asyncio.IncompleteReadError):
            run(go(), timeout=5)

    def test_read_json_returns_a_dict(self):
        async def go():
            reader = _reader(P.encode_json(P.MSG_AUTH, {"response": "abc"}))
            return await P.read_json(reader, {P.MSG_AUTH})
        msg_type, data = run(go())
        self.assertEqual((msg_type, data), (P.MSG_AUTH, {"response": "abc"}))


if __name__ == "__main__":
    unittest.main()
