"""The device connection's certificate (app/tls.py)."""

import hashlib
import hmac
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


def test_the_link_fingerprint_covers_the_whole_link_under_the_add_ons_key(tmp_path):
    tls.server_context(str(tmp_path))
    pem = (tmp_path / tls.CERTIFICATE_FILE).read_text()
    key = tls.fingerprint_key(str(tmp_path))
    der = x509.load_pem_x509_certificate(pem.encode()).public_bytes(serialization.Encoding.DER)
    digest = hmac.new(bytes.fromhex(key), b"8080\n0123456789abcdef\n" + der, hashlib.sha256).hexdigest().upper()
    assert tls.link_fingerprint(8080, "0123456789abcdef", pem, key) == " ".join(
        digest[i : i + 4] for i in range(0, 32, 4)
    )
    for other in ((8443, "0123456789abcdef", pem, key), (8080, "fedcba9876543210", pem, key)):
        assert tls.link_fingerprint(*other) != tls.link_fingerprint(8080, "0123456789abcdef", pem, key)


def test_the_fingerprint_key_is_random_kept_and_private(tmp_path):
    key = tls.fingerprint_key(str(tmp_path))
    assert len(bytes.fromhex(key)) == 32
    assert tls.fingerprint_key(str(tmp_path)) == key
    assert stat.S_IMODE((tmp_path / tls.FINGERPRINT_KEY_FILE).stat().st_mode) == 0o600
