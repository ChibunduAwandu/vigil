"""Ed25519 report attestations: a small off-chain baseline for the registry."""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from vigil.reports.canonical import canonical_json
from vigil.storage import atomic_write


def keygen(directory: Path) -> tuple[Path, Path]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    private, public = directory / "private.pem", directory / "public.pem"
    if private.exists() or public.exists():
        raise ValueError("key files already exist; choose a new directory")
    key = Ed25519PrivateKey.generate()
    atomic_write(private, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()))
    atomic_write(public, key.public_key().public_bytes(serialization.Encoding.PEM,
                                                      serialization.PublicFormat.SubjectPublicKeyInfo))
    return private, public


def sign(report: Path, key_path: Path, output: Path) -> dict:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from vigil.reports.builder import ComplianceReport
    document = ComplianceReport.load(report)
    if document.defects():
        raise ValueError("report has internal defects: " + "; ".join(document.defects()))
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("key must be Ed25519")
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    result = {"schema": "vigil.signature/1", "algorithm": "Ed25519", "report_sha256": document.sha256,
              "public_key_sha256": hashlib.sha256(public).hexdigest(),
              "signature_base64": base64.b64encode(key.sign(b"vigil.signature/1\0" + bytes.fromhex(document.sha256))).decode()}
    atomic_write(output, canonical_json(result))
    return result
