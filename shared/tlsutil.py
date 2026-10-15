"""
LANDesk - TLS setup

Everything this tool sends - the screen, every keystroke typed into it, the
clipboard - used to cross the network as plain TCP. This module wraps the
connection in TLS.

There is no certificate authority involved and nothing for a person to check.
The server makes a fresh self-signed certificate each time it starts, keeps
the private key in memory, and the viewer accepts whatever certificate it is
offered. On its own that stops passive recording but not a machine in the
middle. What closes that gap is `shared.auth`: the password proof is bound to
the certificate the client actually saw, so a relayed handshake authenticates
to nothing. The password, not the certificate, is the trust anchor.

Generating a certificate needs the `cryptography` package. If it is missing,
the server says so and stops rather than quietly falling back to plaintext -
a fallback nobody notices is worse than an error nobody expected. Passing
--tls-cert/--tls-key avoids the dependency, and --no-tls turns the whole
thing off deliberately.
"""

import hashlib
import os
import ssl
import tempfile

__all__ = [
    "CRYPTOGRAPHY_AVAILABLE", "TLSUnavailable",
    "generate_self_signed", "cert_fingerprint", "peer_fingerprint",
    "server_context", "client_context",
]

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the install
    CRYPTOGRAPHY_AVAILABLE = False


class TLSUnavailable(Exception):
    """TLS was asked for and cannot be provided."""


def generate_self_signed(common_name="landesk", days=365):
    """Return (cert_pem, key_pem) for a fresh throwaway certificate.

    P-256 rather than RSA because generating it is instant, which matters when
    it happens on every server start.
    """
    if not CRYPTOGRAPHY_AVAILABLE:
        raise TLSUnavailable(
            "TLS needs the 'cryptography' package (pip install cryptography), "
            "or pass --tls-cert and --tls-key, or --no-tls to run unencrypted.")
    import datetime

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def cert_fingerprint(cert_pem: bytes) -> bytes:
    """SHA-256 of the leaf certificate's DER encoding.

    Takes the first certificate in the file, so a bundle with intermediates
    still yields the leaf - the same certificate the peer will report.
    """
    text = cert_pem.decode("ascii", "replace")
    begin = text.find("-----BEGIN CERTIFICATE-----")
    end = text.find("-----END CERTIFICATE-----")
    if begin < 0 or end < 0:
        raise TLSUnavailable("no PEM certificate found")
    end += len("-----END CERTIFICATE-----")
    der = ssl.PEM_cert_to_DER_cert(text[begin:end] + "\n")
    return hashlib.sha256(der).digest()


def peer_fingerprint(writer) -> bytes:
    """SHA-256 of the certificate the peer presented, or b'' on plain TCP."""
    ssl_object = writer.get_extra_info("ssl_object")
    if ssl_object is None:
        return b""
    der = ssl_object.getpeercert(binary_form=True)
    return hashlib.sha256(der).digest() if der else b""


def _harden(ctx):
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.set_alpn_protocols(["landesk/2"])
    except NotImplementedError:  # pragma: no cover - very old OpenSSL
        pass
    return ctx


def server_context(cert_pem: bytes, key_pem: bytes):
    """Build a server SSLContext from PEM held in memory.

    ssl only loads certificates from files, so the pair goes through a
    private temporary file that is removed as soon as it has been read.
    """
    ctx = _harden(ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER))
    fd, path = tempfile.mkstemp(prefix="landesk-", suffix=".pem")
    try:
        os.write(fd, cert_pem + b"\n" + key_pem)
        os.close(fd)
        fd = None
        ctx.load_cert_chain(path)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(path)
        except OSError:  # pragma: no cover - best effort
            pass
    return ctx


def client_context():
    """Build a client SSLContext that accepts any certificate.

    Deliberate: there is no CA and no name to check. Authentication is the
    password proof in shared.auth, which is bound to whichever certificate
    this connection actually received.
    """
    ctx = _harden(ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx
