#!/usr/bin/env python3
"""Regenerates Bumper's server cert signed by the EXISTING CA.

Keeps the CA intact (phones need no reinstall) and widens the SANs to
cover the two-level subdomains the modern app uses (eco-es.api.ecovacs.com
and friends) plus the host IP.

Usage (from the project root):
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
    # ecovacs.com — one and two levels (the modern app uses X.api / X.openapi)
    "ecovacs.com", "*.ecovacs.com",
    "*.api.ecovacs.com", "*.openapi.ecovacs.com", "*.appsvr.ecovacs.com",
    # ecovacs.net
    "ecovacs.net", "*.ecovacs.net", "*.api.ecovacs.net",
    # ecouser.net — one, two and three levels (v2+)
    "ecouser.net", "*.ecouser.net", "*.api.ecouser.net", "*.msg.ecouser.net",
    "*.ww.ecouser.net", "*.dc-eu.ww.ecouser.net", "*.dc.ww.ecouser.net",
    "*.area.ww.ecouser.net",
]

IPS = ["127.0.0.1", "192.0.2.10"]  # example LAN IP; in production, the minipc's IP


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
        print(f"wrote /certs/{name}")

    print("SANs:", ", ".join(DOMAINS), "| IPs:", ", ".join(IPS))


if __name__ == "__main__":
    main()
