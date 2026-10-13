#!/usr/bin/env python3
"""Regenera el cert de servidor de Bumper firmado por el CA EXISTENTE.

Mantiene el CA intacto (los moviles no necesitan reinstalar nada) y amplia
los SANs para cubrir los subdominios de doble nivel que usa la app moderna
(eco-es.api.ecovacs.com y similares) mas la IP del host.

Uso (desde el raiz del proyecto):
  docker run --rm -v "$(pwd)/data/certs:/certs" -v "$(pwd)/tools:/tools" \
      python:3-alpine sh -c "pip -q install cryptography && python /tools/regen_server_cert.py"
"""

import datetime
import ipaddress

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

DOMAINS = [
    "localhost",
    # ecovacs.com — simple y doble nivel (la app moderna usa X.api / X.openapi)
    "ecovacs.com", "*.ecovacs.com",
    "*.api.ecovacs.com", "*.openapi.ecovacs.com", "*.appsvr.ecovacs.com",
    # ecovacs.net
    "ecovacs.net", "*.ecovacs.net", "*.api.ecovacs.net",
    # ecouser.net — simple, doble y triple nivel (v2+)
    "ecouser.net", "*.ecouser.net", "*.api.ecouser.net", "*.msg.ecouser.net",
    "*.ww.ecouser.net", "*.dc-eu.ww.ecouser.net", "*.dc.ww.ecouser.net",
    "*.area.ww.ecouser.net",
]

IPS = ["127.0.0.1", "192.0.2.10"]  # dev; en produccion, IP del minipc


def main() -> None:
    with open("/certs/ca.key", "rb") as f:
        ca_key = serialization.load_pem_private_key(f.read(), password=None)
    with open("/certs/ca.crt", "rb") as f:
        ca_cert = x509.load_pem_x509_certificate(f.read())

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)

    cert = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name(
                [
                    x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Bumper"),
                    x509.NameAttribute(NameOID.COMMON_NAME, "Bumper Server"),
                ]
            )
        )
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(d) for d in DOMAINS]
                + [x509.IPAddress(ipaddress.ip_address(i)) for i in IPS]
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    for name, data in [
        ("bumper.key", key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )),
        ("bumper.crt", cert.public_bytes(serialization.Encoding.PEM)),
    ]:
        with open(f"/certs/{name}", "wb") as f:
            f.write(data)
        print(f"escrito /certs/{name}")

    print("SANs:", ", ".join(DOMAINS), "| IPs:", ", ".join(IPS))


if __name__ == "__main__":
    main()
