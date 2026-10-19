"""TLS cert auto-generation for the embedded servers (Phase FE).

Clean installs: if the material is missing at startup (CA / server
cert), a self-signed set is generated and the robot/app can pair
against it. In production the robots ALREADY trust the existing CA from
the Bumper era (``data/certs``) — hence the sacred rule:

    **existing certs are NEVER touched.**

Cases ``ensure_certs`` covers:

- everything present → no-op (only warns if the CA is missing: the
  embedded XMPP needs it to verify clients)
- full server pair, missing CA → a new CA is NOT generated (it would not
  sign the existing cert and would mislead the config) — warning
- half-present cert/key → error: a broken pair is not "fixed" by
  regenerating half of it (operator's call)
- missing pair, CA (+ its sibling key) present → the new pair is signed
  with the existing CA (robots that already trust it keep working —
  same role as ``tools/regen_server_cert.py``)
- nothing present → new CA + pair signed by it (first boot)

The CA is written next to its private key (sibling ``<ca>.key``) so
future certs can be signed without regenerating the CA (regenerating it
would force robots to re-pair).

Extensions: SKI+AKI on CA and leaf (Bumper's original generator
produced CAs without AKI, which ssl's strict mode in py≥3.13 rejects),
critical BasicConstraints and SANs with Ecovacs' domains — see
tests/test_servers_fe.py.
"""

from __future__ import annotations

import datetime
import ipaddress
import logging
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes
from cryptography.x509.oid import NameOID

log = logging.getLogger(__name__)

DAYS_VALID = 3650  # ~10 years, like Bumper's create_certs

# domains robots/app may use to reach the server (server cert SANs) —
# same repertoire as tools/regen_server_cert.py, trimmed to what the
# embedded flow uses (lookup/xmpp/mqtt, no app API)
SERVER_DNS_SANS = [
    "localhost",
    "ecouser.net", "*.ecouser.net", "*.msg.ecouser.net", "*.ww.ecouser.net",
    "ecovacs.net", "*.ecovacs.net",
    "ecovacs.com", "*.ecovacs.com", "*.api.ecovacs.com",
]


def _name(cn: str) -> x509.Name:
    return x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "deebot930-bridge"),
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
    ])


def _build_ca(key: PrivateKeyTypes, cn: str) -> x509.Certificate:
    """Self-signed CA with AKI/SKI — py≥3.13 strict mode requires it."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(_name(f"{cn} CA"))
        .issuer_name(_name(f"{cn} CA"))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=DAYS_VALID))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None),
                       critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False,
            key_encipherment=False, data_encipherment=False,
            key_agreement=False, key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(
            key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
            key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )


def _build_server_cert(ca_cert: x509.Certificate, ca_key: PrivateKeyTypes,
                       key: PrivateKeyTypes, cn: str) -> x509.Certificate:
    """Server cert signed by the CA, with SANs for robots/app."""
    now = datetime.datetime.now(datetime.timezone.utc)
    sans: list[x509.GeneralName] = [x509.DNSName(d) for d in SERVER_DNS_SANS]
    try:  # the CN is usually the announce_ip: IP SAN for whoever verifies
        sans.append(x509.IPAddress(ipaddress.ip_address(cn)))
    except ValueError:
        sans.insert(0, x509.DNSName(cn))
    sans.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    # order-preserving dedup (the CN may duplicate a default SAN)
    seen: set[str] = set()
    unique: list[x509.GeneralName] = []
    for san in sans:
        k = f"{type(san).__name__}:{san.value}"
        if k not in seen:
            seen.add(k)
            unique.append(san)
    return (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=DAYS_VALID))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                       critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False,
            key_encipherment=True, data_encipherment=False,
            key_agreement=False, key_cert_sign=False, crl_sign=False,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectAlternativeName(unique), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(
            key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
            ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )


def _atomic_write(path: Path, data: bytes, secret: bool = False) -> None:
    """Writes via tmp+replace: an aborted boot leaves no half-written certs.

    Secrets are created with 0600 from the start (os.open with mode) — a
    later chmod would leave a world-readable window.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    if secret:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    else:
        tmp.write_bytes(data)
    tmp.replace(path)


def _pem_cert(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _pem_key(key: PrivateKeyTypes) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


def ensure_certs(ca_file: str, server_cert: str, server_key: str,
                 cn: str = "bumper") -> bool:
    """Ensures the TLS material for the embedded servers.

    Returns True if usable server cert+key exist (or were created);
    False when it was not possible (broken config, unwritable dir…) —
    the caller decides then (serve disables the embedded TLS servers
    and clients keep their external config). Never touches existing
    certs.
    """
    ca, crt, key = Path(ca_file), Path(server_cert), Path(server_key)
    have_crt, have_key, have_ca = crt.is_file(), key.is_file(), ca.is_file()
    ca_key_path = ca.with_suffix(".key")

    if have_crt and have_key:
        if not have_ca:
            log.warning("server cert+key present but CA %s missing "
                        "— not generated (a new CA would not sign the "
                        "existing cert); the embedded XMPP needs it to "
                        "verify clients", ca)
        return True  # never regenerate what already exists

    if have_crt != have_key:
        log.error("half-present certs (%s=%s, %s=%s) — nothing "
                  "regenerated, remove the pair by hand or restore the "
                  "missing file",
                  crt, have_crt, key, have_key)
        return False

    try:
        # pair to generate: first make sure the destination IS writable
        # (failing here = not even the CA gets created somewhere valid)
        crt.parent.mkdir(parents=True, exist_ok=True)
        key.parent.mkdir(parents=True, exist_ok=True)

        if have_ca:
            if not ca_key_path.is_file():
                log.error("CA %s exists but its key %s is missing — "
                          "cannot sign the server cert", ca, ca_key_path)
                return False
            issuer_key = serialization.load_pem_private_key(
                ca_key_path.read_bytes(), password=None)
            issuer_cert = x509.load_pem_x509_certificate(ca.read_bytes())
            if issuer_key.public_key() != issuer_cert.public_key():
                log.error("key %s does not match CA %s (different public "
                          "keys) — nothing signed", ca_key_path, ca)
                return False
            log.info("signing the server cert with existing CA %s "
                     "(robots that already trust it need nothing)", ca)
        else:
            if ca_key_path.is_file():
                log.error("%s exists but not %s — inconsistent state, "
                          "nothing touched", ca_key_path, ca)
                return False
            issuer_key = rsa.generate_private_key(
                public_exponent=65537, key_size=2048)
            issuer_cert = _build_ca(issuer_key, cn)
            _atomic_write(ca, _pem_cert(issuer_cert))
            _atomic_write(ca_key_path, _pem_key(issuer_key), secret=True)
            log.info("new CA at %s (+ key %s) — first boot; robots "
                     "will have to pair against it", ca, ca_key_path)

        srv_key = rsa.generate_private_key(public_exponent=65537,
                                           key_size=2048)
        srv_cert = _build_server_cert(issuer_cert, issuer_key, srv_key, cn)
        _atomic_write(crt, _pem_cert(srv_cert))
        _atomic_write(key, _pem_key(srv_key), secret=True)
        log.info("server cert generated at %s / %s (CN=%s, SANs: %s)",
                 crt, key, cn,
                 ", ".join(SERVER_DNS_SANS[:3]) + ", …")
        return True
    except Exception:  # noqa: BLE001  (guarantee: ensure never blows up)
        log.exception("generating certs for (%s, %s, %s) failed",
                      ca_file, server_cert, server_key)
        return False
