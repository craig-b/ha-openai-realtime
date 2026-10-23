"""The certificate the device connection is encrypted with.

The add-on makes its own on first start and keeps it. The firmware trusts
exactly this certificate (from its link), not a certificate authority, so it
names no host and never expires: the device reaches the add-on by whatever
name or address it's given, and a certificate that expired would need every
device re-flashed.
"""
import datetime
import hashlib
import logging
import os
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

logger = logging.getLogger(__name__)

CERTIFICATE_FILE = "certificate.pem"
KEY_FILE = "key.pem"
# RFC 5280 4.1.2.5: the notAfter of a certificate with no well-defined expiry.
NO_EXPIRY = datetime.datetime(9999, 12, 31, 23, 59, 59, tzinfo=datetime.timezone.utc)


def server_context(directory: str) -> ssl.SSLContext:
    """A TLS context with the add-on's certificate, made first if there is none."""
    folder = Path(directory)
    certificate, key = folder / CERTIFICATE_FILE, folder / KEY_FILE
    if not (certificate.exists() and key.exists()):
        _make_certificate(certificate, key)
    logger.info(
        f"🔐 For a firmware you build yourself, its va_certificate (also in {certificate}):\n"
        f"{certificate.read_text().strip()}"
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, key)
    return context


def link_fingerprint(port: int, secret: str, certificate: str) -> str:
    """A fingerprint of the link the add-on offers: SHA-256 over its port, the
    secret and the certificate (DER), 32 hex digits in fours. The integration
    shows the same when it asks the owner to confirm a link, so they can tell
    it's this add-on's, all of it."""
    digest = hashlib.sha256(f"{port}\n{secret}\n".encode() + ssl.PEM_cert_to_DER_cert(certificate.strip())).hexdigest()
    return " ".join(digest[i : i + 4] for i in range(0, 32, 4)).upper()


def _make_certificate(certificate: Path, key: Path) -> None:
    certificate.parent.mkdir(parents=True, exist_ok=True)
    # P-256: the curve the ESP32's hardware accelerates.
    private_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "OpenAI Realtime Voice Agent")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(NO_EXPIRY)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )
    # The key is written readable by its owner only, before it holds anything.
    descriptor = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as f:
        f.write(
            private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    logger.info(f"🔐 Made a new certificate for the device connection in {certificate.parent}")
