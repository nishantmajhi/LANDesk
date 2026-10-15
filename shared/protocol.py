"""
LANDesk - Shared Protocol Definitions

Wire format, message types, and the framed-read helper both ends use.

A message is a five-byte header followed by a payload:

    [1 byte type] [4 bytes payload length, big-endian] [payload]

Every payload has a maximum size that depends on its type, and each end
declares which types it is willing to receive. Both checks happen against the
header, before a single byte of payload is read, so a peer cannot make us
allocate memory by claiming a large length.
"""

import asyncio
import json
import struct

__all__ = [
    "PROTOCOL_VERSION",
    "MSG_FRAME", "MSG_MOUSE_MOVE", "MSG_MOUSE_BTN", "MSG_KEY", "MSG_HELLO",
    "MSG_HELLO_ACK", "MSG_PING", "MSG_PONG", "MSG_SCROLL",
    "MSG_CLIPBOARD_SEND", "MSG_CLIPBOARD_RECV", "MSG_CHALLENGE", "MSG_AUTH",
    "HEADER_SIZE", "MAX_PAYLOAD", "DEFAULT_MAX_PAYLOAD",
    "SERVER_ACCEPTS", "CLIENT_ACCEPTS",
    "ProtocolError",
    "encode_message", "decode_header", "encode_json", "decode_json",
    "check_message", "read_message", "read_json",
]

# The wire format changed in version 2: the handshake became a three-step
# challenge-response and gained the CHALLENGE and AUTH message types. A
# version 1 client and a version 2 server cannot talk to each other, so the
# HELLO carries the version and a mismatch is reported rather than guessed at.
PROTOCOL_VERSION = 2

# Message type constants
MSG_FRAME      = 0x01   # Screen frame (JPEG bytes)
MSG_MOUSE_MOVE = 0x02   # Mouse move event
MSG_MOUSE_BTN  = 0x03   # Mouse button event
MSG_KEY        = 0x04   # Keyboard event
MSG_HELLO      = 0x05   # Handshake step 1: client announces itself
MSG_HELLO_ACK  = 0x06   # Handshake step 4: server accepts or rejects
MSG_PING       = 0x07
MSG_PONG       = 0x08
MSG_SCROLL     = 0x09   # Mouse scroll event
MSG_CLIPBOARD_SEND = 0x0A  # Viewer -> server clipboard content
MSG_CLIPBOARD_RECV = 0x0B  # Server -> viewer clipboard content
MSG_CHALLENGE  = 0x0C   # Handshake step 2: server sends salt and nonce
MSG_AUTH       = 0x0D   # Handshake step 3: client sends its response

# Header: [1 byte type] [4 bytes payload length]
HEADER_SIZE = 5

# Per-type payload ceilings. Everything that is not listed is a small JSON
# control message and gets DEFAULT_MAX_PAYLOAD. The frame cap is generous
# because a JPEG of a 4K screen at high quality is a few megabytes; the
# clipboard cap is what a person plausibly copies, not what fits in RAM.
DEFAULT_MAX_PAYLOAD = 8 * 1024              # 8 KiB
MAX_PAYLOAD = {
    MSG_FRAME:          32 * 1024 * 1024,   # 32 MiB
    MSG_CLIPBOARD_SEND:      1024 * 1024,   # 1 MiB
    MSG_CLIPBOARD_RECV:      1024 * 1024,   # 1 MiB
}

# Which message types each end will accept. A server has no business being
# sent a screen frame, so refusing the type also refuses its 32 MiB ceiling.
SERVER_ACCEPTS = frozenset({
    MSG_HELLO, MSG_AUTH, MSG_MOUSE_MOVE, MSG_MOUSE_BTN, MSG_KEY, MSG_SCROLL,
    MSG_CLIPBOARD_SEND, MSG_PING,
})
CLIENT_ACCEPTS = frozenset({
    MSG_CHALLENGE, MSG_HELLO_ACK, MSG_FRAME, MSG_CLIPBOARD_RECV,
    MSG_PING, MSG_PONG,
})


class ProtocolError(Exception):
    """A peer sent something that is not valid on this connection."""


def encode_message(msg_type: int, payload: bytes) -> bytes:
    """Pack a message with its header."""
    header = struct.pack(">BI", msg_type, len(payload))
    return header + payload


def decode_header(header: bytes):
    """Unpack header -> (msg_type, payload_length)."""
    return struct.unpack(">BI", header)


def encode_json(msg_type: int, data: dict) -> bytes:
    payload = json.dumps(data).encode("utf-8")
    return encode_message(msg_type, payload)


def decode_json(payload: bytes) -> dict:
    """Decode a JSON payload, rejecting anything that is not an object.

    Callers index the result, so a bare list or number reaching them would
    surface as a TypeError somewhere less obvious than here.
    """
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ProtocolError("payload is not valid JSON: %s" % e) from e
    if not isinstance(data, dict):
        raise ProtocolError("expected a JSON object, got %s" % type(data).__name__)
    return data


def check_message(msg_type: int, length: int, accepts) -> None:
    """Validate a decoded header before the payload is read.

    Raises ProtocolError if the type is not one this end accepts or the
    declared length is above that type's ceiling.
    """
    if msg_type not in accepts:
        raise ProtocolError("unexpected message type 0x%02X" % msg_type)
    limit = MAX_PAYLOAD.get(msg_type, DEFAULT_MAX_PAYLOAD)
    if length > limit:
        raise ProtocolError(
            "message type 0x%02X declared %d bytes, limit is %d"
            % (msg_type, length, limit))


async def read_message(reader, accepts, timeout=None):
    """Read one framed message. Returns (msg_type, payload).

    The header is validated with check_message before the payload is read, so
    an oversized or unexpected message costs five bytes rather than whatever
    the sender asked for. `timeout` covers each read individually, which is
    what stops a peer holding a connection open by sending a header and then
    nothing.
    """
    header = await _read(reader, HEADER_SIZE, timeout)
    msg_type, length = decode_header(header)
    check_message(msg_type, length, accepts)
    payload = await _read(reader, length, timeout) if length else b""
    return msg_type, payload


async def read_json(reader, accepts, timeout=None):
    """read_message, with the payload decoded as a JSON object."""
    msg_type, payload = await read_message(reader, accepts, timeout)
    return msg_type, decode_json(payload)


async def _read(reader, n, timeout):
    if timeout is None:
        return await reader.readexactly(n)
    return await asyncio.wait_for(reader.readexactly(n), timeout)
