"""The device connection's certificate (app/tls.py)."""

import hashlib
import stat

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from app import tls


def test_the_certificate_never_expires_and_names_no_host(tmp_path):
    tls.server_context(str(tmp_path))
    cert = x509.load_pem_x509_certificate((tmp_path / tls.CERTIFICATE_FILE).read_bytes())
    assert cert.not_valid_after_utc == tls.NO_EXPIRY
    assert cert.issuer == cert.subject
    names = [e.oid for e in cert.extensions]
    assert x509.oid.ExtensionOID.SUBJECT_ALTERNATIVE_NAME not in names


def test_only_the_owner_can_read_the_key(tmp_path):
    tls.server_context(str(tmp_path))
    mode = stat.S_IMODE((tmp_path / tls.KEY_FILE).stat().st_mode)
    assert mode == 0o600


def test_the_link_fingerprint_covers_port_secret_and_certificate(tmp_path):
    tls.server_context(str(tmp_path))
    pem = (tmp_path / tls.CERTIFICATE_FILE).read_text()
    der = x509.load_pem_x509_certificate(pem.encode()).public_bytes(serialization.Encoding.DER)
    digest = hashlib.sha256(b"8080\n0123456789abcdef\n" + der).hexdigest().upper()
    assert tls.link_fingerprint(8080, "0123456789abcdef", pem) == " ".join(digest[i : i + 4] for i in range(0, 32, 4))
    assert tls.link_fingerprint(8443, "0123456789abcdef", pem) != tls.link_fingerprint(8080, "0123456789abcdef", pem)
