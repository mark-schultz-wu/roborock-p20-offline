#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["cryptography"]
# ///
"""Mint a TLS leaf for the vacuum-facing endpoints.

Signs with the eMudhra "EM DV TLS CA - G3B" intermediate key (P-384 scalar
156 — that key was generated with a broken RNG; the 2025 disclosure
published the scalar), whose Comodo cross-sign (`inter-cross.pem`) chains
to the `AAA Certificate Services` root still trusted by the vacuum's
frozen factory trust store. The stack serves the result on :443/:555/:8881
(`tls.mode = "provided"`). Method: docs/WRITEUP.md.

    uv run apicn/mint_leaf.py            # SANs default from lab.env:
                                       #   STACK_HOST, TOKEN_HOST, MANGLED
                                       #   (= token host + "iot.roborock.com"),
                                       #   api-cn.roborock.com
    uv run apicn/mint_leaf.py --san dns.name --san other.name   # explicit
        [--valid-days 800] [--out-prefix apicn/leaf]

Writes <prefix>.key, <prefix>.pem, <prefix>-chain.pem (leaf + inter-cross).
Deploy: cp <prefix>-chain.pem local_roborock_server/data/certs/fullchain.pem
        cp <prefix>.key     local_roborock_server/data/certs/privkey.pem
then restart the stack (and go2rtc — it pins the leaf; compose.local.yaml).
The public chain certs are tracked; the minted leaf/key stay gitignored.
"""
from __future__ import annotations

import argparse
import datetime
import pathlib
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import labconfig  # noqa: E402  (repo root; lab.env values)

SCALAR = 156  # the eMudhra intermediate private scalar (2025 disclosure)
HERE = pathlib.Path(__file__).parent
INTER_CERT = HERE / "inter-cross.pem"
FIRMWARE_HOST = "api-cn.roborock.com"  # fixed vendor name the vacuum dials


def default_sans() -> list[str]:
    """Every name the vacuum dials, from lab.env — the same source
    tests/check_local.py asserts leaf SAN coverage against."""
    return [labconfig.STACK_HOST, labconfig.TOKEN_HOST, labconfig.MANGLED,
            FIRMWARE_HOST]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--san", action="append",
                    help="DNS SAN (repeatable); the first is also the CN. "
                         "Default: the names from lab.env that the vacuum dials")
    ap.add_argument("--valid-days", type=int, default=800)
    ap.add_argument("--out-prefix", default=str(HERE / "leaf"))
    a = ap.parse_args()

    raw = a.san if a.san else default_sans()
    sans = list(dict.fromkeys(s.strip().lower() for s in raw if s.strip()))
    assert sans, "no SANs to mint for (is lab.env's STACK_HOST set?)"
    inter_cert = x509.load_pem_x509_certificate(INTER_CERT.read_bytes())
    inter_key = ec.derive_private_key(SCALAR, ec.SECP384R1())
    leaf_key = ec.generate_private_key(ec.SECP384R1())
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)

    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, sans[0])]))
        .issuer_name(inter_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=a.valid_days))
        .add_extension(x509.SubjectAlternativeName(
            [x509.DNSName(s) for s in sans]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=False,
            crl_sign=False, encipher_only=None, decipher_only=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
            inter_key.public_key()), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(
            leaf_key.public_key()), critical=False)
    )
    cert = builder.sign(inter_key, hashes.SHA384())

    prefix = pathlib.Path(a.out_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix(".key").write_bytes(leaf_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    prefix.with_suffix(".pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    chain = cert.public_bytes(serialization.Encoding.PEM) + INTER_CERT.read_bytes()
    pathlib.Path(f"{prefix}-chain.pem").write_bytes(chain)

    print(f"CN={sans[0]}")
    print(f"SANs: {', '.join(sans)}")
    print(f"valid until {cert.not_valid_after_utc:%Y-%m-%d}")
    print(f"wrote {prefix.with_suffix('.key').name}, {prefix.with_suffix('.pem').name}, "
          f"{pathlib.Path(f'{prefix}-chain.pem').name}")


if __name__ == "__main__":
    main()