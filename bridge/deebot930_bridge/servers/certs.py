"""Auto-generación de certs TLS para los servidores embebidos (Fase FE).

Instalaciones limpias: si al arrancar falta el material (CA / cert de
servidor), se genera un juego autofirmado y el robot/app pueden emparejar
contra él. En producción los robots YA confían en la CA existente de la
época de Bumper (``data/certs``) — por eso la regla es sagrada:

    **los certs existentes JAMÁS se tocan.**

Casos que cubre ``ensure_certs``:

- todo presente → no-op (solo avisa si falta la CA: el XMPP embebido la
  necesita para verificar clientes)
- par de servidor completo, CA ausente → NO se genera una CA nueva (no
  firmaría el cert existente y engañaría al config) — warning
- cert y key a medias → error: un par roto no se "arregla" regenerando
  la mitad (decisión del operador)
- par ausente, CA (+ su key hermana) presente → el par nuevo se firma con
  la CA existente (los robots que ya la confían siguen funcionando —
  mismo papel que ``tools/regen_server_cert.py``)
- nada presente → CA nueva + par firmado por ella (primer arranque)

La CA se escribe junto a su clave privada (``<ca>.key`` hermana) para poder
firmar certs futuros sin regenerar la CA (si se regenerase, los robots
tendrían que re-emparejar).

Extensiones: SKI+AKI en CA y leaf (el generador original de Bumper producía
CAs sin AKI que el modo estricto de ssl en py≥3.13 rechaza), BasicConstraints
crítico y SANs con los dominios de Ecovacs — ver tests/test_servers_fe.py.
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

DAYS_VALID = 3650  # ~10 años, como el create_certs de Bumper

# dominios por los que robots/app pueden llegar al servidor (SANs del cert
# de servidor) — mismo repertorio que tools/regen_server_cert.py, recortado
# a los que usa el flujo embebido (lookup/xmpp/mqtt, sin la API de la app)
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
    """CA autofirmada con AKI/SKI — py≥3.13 estricto la exige."""
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
    """Cert de servidor firmado por la CA, con SANs para robots/app."""
    now = datetime.datetime.now(datetime.timezone.utc)
    sans: list[x509.GeneralName] = [x509.DNSName(d) for d in SERVER_DNS_SANS]
    try:  # el CN suele ser la announce_ip: SAN de IP para quien verifique
        sans.append(x509.IPAddress(ipaddress.ip_address(cn)))
    except ValueError:
        sans.insert(0, x509.DNSName(cn))
    sans.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    # dedup preservando el orden (el CN puede duplicar un SAN por defecto)
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
    """Escribe vía tmp+replace: un arranque abortado no deja certs a medias."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    if secret:
        os.chmod(tmp, 0o600)
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
    """Garantiza el material TLS de los servidores embebidos.

    Devuelve True si hay (o hubo) cert+key de servidor utilizables; False
    si no se pudo (config rota, directorio no escribible…) — en ese caso el
    llamador decide (serve desactiva los servidores TLS embebidos y los
    clientes siguen con su config externa). Nunca toca certs existentes.
    """
    ca, crt, key = Path(ca_file), Path(server_cert), Path(server_key)
    have_crt, have_key, have_ca = crt.is_file(), key.is_file(), ca.is_file()
    ca_key_path = ca.with_suffix(".key")

    if have_crt and have_key:
        if not have_ca:
            log.warning("cert+key de servidor presentes pero falta la CA %s "
                        "— no se genera (una CA nueva no firmaría el cert "
                        "existente); el XMPP embebido la necesita para "
                        "verificar clientes", ca)
        return True  # jamás se regenera lo que ya existe

    if have_crt != have_key:
        log.error("certs a medias (%s=%s, %s=%s) — no se regenera nada, "
                  "retira el par a mano o repón el fichero que falta",
                  crt, have_crt, key, have_key)
        return False

    try:
        # par a generar: primero asegurar que se PUEDE escribir donde toca
        # (falla aquí = no se crea ni la CA en un destino válido)
        crt.parent.mkdir(parents=True, exist_ok=True)
        key.parent.mkdir(parents=True, exist_ok=True)

        if have_ca:
            if not ca_key_path.is_file():
                log.error("la CA %s existe pero falta su clave %s — no se "
                          "puede firmar el cert de servidor", ca, ca_key_path)
                return False
            issuer_key = serialization.load_pem_private_key(
                ca_key_path.read_bytes(), password=None)
            issuer_cert = x509.load_pem_x509_certificate(ca.read_bytes())
            log.info("firmando el cert de servidor con la CA existente %s "
                     "(los robots que ya la confían no necesitan nada)", ca)
        else:
            if ca_key_path.is_file():
                log.error("existe %s pero no %s — estado inconsistente, no "
                          "se toca nada", ca_key_path, ca)
                return False
            issuer_key = rsa.generate_private_key(
                public_exponent=65537, key_size=2048)
            issuer_cert = _build_ca(issuer_key, cn)
            _atomic_write(ca, _pem_cert(issuer_cert))
            _atomic_write(ca_key_path, _pem_key(issuer_key), secret=True)
            log.info("CA nueva en %s (+ clave %s) — primer arranque; los "
                     "robots deberán emparejar contra ella", ca, ca_key_path)

        srv_key = rsa.generate_private_key(public_exponent=65537,
                                           key_size=2048)
        srv_cert = _build_server_cert(issuer_cert, issuer_key, srv_key, cn)
        _atomic_write(crt, _pem_cert(srv_cert))
        _atomic_write(key, _pem_key(srv_key), secret=True)
        log.info("cert de servidor generado en %s / %s (CN=%s, SANs: %s)",
                 crt, key, cn,
                 ", ".join(SERVER_DNS_SANS[:3]) + ", …")
        return True
    except Exception:  # noqa: BLE001  (garantía: ensure nunca revienta)
        log.exception("generando certs para (%s, %s, %s) falló",
                      ca_file, server_cert, server_key)
        return False
