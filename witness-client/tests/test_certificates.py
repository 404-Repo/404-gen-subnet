"""Exercise provider authentication against constrained wildcard certificates."""

import datetime
import ssl
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.utils import CryptographyDeprecationWarning
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from cryptography.x509.verification import VerificationError

from witness_client.transport import _check_certificates

if TYPE_CHECKING:
    from witness_client._tls import TLSConnection


@pytest.mark.parametrize("legacy_root", [False, True])
@pytest.mark.parametrize(
    "case", ["valid", "permitted", "excluded", "hostname", "expired"]
)
def test_provider_certificate_policy(
    tmp_path: Path, case: str, legacy_root: bool
) -> None:
    now = datetime.datetime.now(datetime.UTC)
    keys = [ec.generate_private_key(ec.SECP256R1()) for _ in range(3)]
    names = [
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, label)])
        for label in ("Root", "Intermediate", "*.example.test")
    ]
    certificates: list[x509.Certificate] = []
    for index in range(3):
        ca = index < 2
        issuer = max(0, index - 1)
        builder = (
            x509.CertificateBuilder()
            .subject_name(names[index])
            .issuer_name(names[issuer])
            .public_key(keys[index].public_key())
            .serial_number(1 if index == 0 else x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=2))
            .not_valid_after(
                now + datetime.timedelta(days=-1 if case == "expired" and not ca else 1)
            )
            .add_extension(
                x509.BasicConstraints(ca=ca, path_length=None), critical=True
            )
            .add_extension(
                x509.KeyUsage(True, False, False, False, False, ca, ca, False, False),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(keys[index].public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    keys[issuer].public_key()
                ),
                critical=False,
            )
        )
        if index == 1 and case in ("permitted", "excluded"):
            builder = builder.add_extension(
                x509.NameConstraints(
                    permitted_subtrees=(
                        [x509.DNSName("allowed.example.test")]
                        if case == "permitted"
                        else None
                    ),
                    excluded_subtrees=(
                        [x509.DNSName("blocked.example.test")]
                        if case == "excluded"
                        else None
                    ),
                ),
                critical=True,
            )
        if not ca:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.DNSName("*.example.test")]),
                critical=False,
            ).add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
            )
        certificates.append(builder.sign(keys[issuer], hashes.SHA256()))
    root_der = certificates[0].public_bytes(serialization.Encoding.DER)
    if legacy_root:
        # Keep the trusted public key and change only the anchor's serial to zero.
        version_and_serial = b"\xa0\x03\x02\x01\x02\x02\x01\x01"
        assert root_der.count(version_and_serial) == 1
        root_der = root_der.replace(
            version_and_serial, version_and_serial[:-1] + b"\x00"
        )
    ca_file = tmp_path / "root.pem"
    ca_file.write_text(ssl.DER_cert_to_PEM_cert(root_der), encoding="ascii")
    connection = cast(
        "TLSConnection",
        SimpleNamespace(
            session=SimpleNamespace(
                serverCertChain=SimpleNamespace(
                    x509List=[
                        SimpleNamespace(
                            bytes=cert.public_bytes(serialization.Encoding.DER)
                        )
                        for cert in reversed(certificates[1:])
                    ]
                )
            )
        ),
    )
    host = "unrelated.test" if case == "hostname" else "blocked.example.test"
    with warnings.catch_warnings():
        warnings.simplefilter("error", CryptographyDeprecationWarning)
        if case == "valid":
            _check_certificates(connection, host, ca_file)
        else:
            # Regressions for GHSA-m2h6-j472-rp4c and GHSA-m959-cc7f-wv43,
            # through the client's actual certificate-validation entry point.
            with pytest.raises(VerificationError):
                _check_certificates(connection, host, ca_file)
