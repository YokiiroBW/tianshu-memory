"""Generate disposable TLS credentials; run with an explicitly chosen test Python.

Uses cryptography from the test tooling runtime, never an application dependency.
No certificate store is opened or modified. Output belongs under this checkout's .runtime.
"""

import ipaddress
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def generate(directory):
    directory = directory.resolve()
    if not directory.is_relative_to(Path(__file__).resolve().parents[1] / ".runtime"):
        raise ValueError("TLS fixture output must be under this checkout's .runtime")
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)

    def key():
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def certificate(name, subject_key, issuer_name, issuer_key, *, ca=False, **options):
        builder = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(issuer_name)
            .public_key(subject_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=2))
            .not_valid_after(now + timedelta(days=options.get("days", 2)))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=0 if ca else None), True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=not ca,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=ca,
                    crl_sign=ca,
                    encipher_only=False,
                    decipher_only=False,
                ),
                True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(subject_key.public_key()), False
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), False
            )
        )
        if not ca:
            builder = builder.add_extension(
                x509.SubjectAlternativeName(
                    [x509.IPAddress(ipaddress.ip_address(options.get("ip", "127.0.0.1")))]
                ),
                False,
            ).add_extension(
                x509.ExtendedKeyUsage([options.get("purpose", ExtendedKeyUsageOID.SERVER_AUTH)]),
                False,
            )
        return builder.sign(issuer_key, hashes.SHA256())

    ca_key = key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TS-032 isolated test CA")])
    ca = certificate(ca_name, ca_key, ca_name, ca_key, ca=True)
    (directory / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    wrong_key = key()
    wrong_ca = certificate(ca_name, wrong_key, ca_name, wrong_key, ca=True)
    (directory / "wrong-ca.pem").write_bytes(wrong_ca.public_bytes(serialization.Encoding.PEM))
    for label, options in {
        "server": {},
        "wrong-host": {"ip": "127.0.0.2"},
        "client-only": {"purpose": ExtendedKeyUsageOID.CLIENT_AUTH},
        "expired": {"days": -1},
    }.items():
        leaf_key = key()
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TS-032 test issuer")])
        leaf = certificate(name, leaf_key, ca_name, ca_key, **options)
        (directory / f"{label}.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
        (directory / f"{label}.key").write_bytes(
            leaf_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )


if __name__ == "__main__":
    generate(Path(sys.argv[1]))
