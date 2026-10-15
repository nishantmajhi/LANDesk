"""
LANDesk - Password authentication

The original handshake sent SHA-256(password) and the server compared it to
its own copy. That makes the hash the credential: anyone who sees one
handshake can authenticate forever without knowing the password. It is also
unsalted and single-round, so the password itself falls to a wordlist in
seconds.

This is a challenge-response instead. The password is never sent, and the
value that is sent is useless on any other connection:

    server -> CHALLENGE  salt, iterations, nonce
    client -> AUTH       HMAC(key, "client" || nonce || channel)
    server -> HELLO_ACK  HMAC(key, "server" || nonce || channel), if accepted

where key = PBKDF2-HMAC-SHA256(password, salt, iterations).

`channel` is the SHA-256 of the server's TLS certificate, which is what makes
this safe over a self-signed connection nobody verifies. A machine in the
middle has to present its own certificate, so the client's response is bound
to the wrong fingerprint and the real server rejects it. The response cannot
be relayed, and the attacker learns nothing it can replay. Over --no-tls the
channel is empty and only the replay protection remains.

The server also proves it knows the password, so a client is not talking a
session's worth of screen contents at an impostor.
"""

import hashlib
import hmac
import os

__all__ = [
    "PBKDF2_ITERATIONS", "SALT_BYTES", "NONCE_BYTES",
    "derive_key", "new_salt", "new_nonce",
    "client_response", "server_confirmation", "verify",
]

# PBKDF2 cost. The server derives its key once at startup and the viewer once
# per connection, so this is a one-off fraction of a second on either end, and
# it is the only thing standing between a captured handshake and an offline
# guess at the password.
PBKDF2_ITERATIONS = 210_000
SALT_BYTES = 16
NONCE_BYTES = 32

_CLIENT_TAG = b"landesk-auth-v2-client"
_SERVER_TAG = b"landesk-auth-v2-server"


def new_salt() -> bytes:
    return os.urandom(SALT_BYTES)


def new_nonce() -> bytes:
    return os.urandom(NONCE_BYTES)


def derive_key(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    """Stretch a password into a 32-byte key."""
    if iterations < 1:
        raise ValueError("iterations must be positive")
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def _mac(tag: bytes, key: bytes, nonce: bytes, channel: bytes) -> str:
    # The lengths are fixed (nonce is NONCE_BYTES, channel is a SHA-256 digest
    # or empty), so plain concatenation is not ambiguous. The tag keeps the
    # client's response from being replayed back as the server's proof.
    return hmac.new(key, tag + nonce + channel, hashlib.sha256).hexdigest()


def client_response(key: bytes, nonce: bytes, channel: bytes = b"") -> str:
    """What the client sends to prove it knows the password."""
    return _mac(_CLIENT_TAG, key, nonce, channel)


def server_confirmation(key: bytes, nonce: bytes, channel: bytes = b"") -> str:
    """What the server sends back to prove the same."""
    return _mac(_SERVER_TAG, key, nonce, channel)


def verify(expected: str, provided) -> bool:
    """Constant-time comparison that tolerates a peer sending the wrong type."""
    if not isinstance(provided, str):
        return False
    return hmac.compare_digest(expected, provided)
