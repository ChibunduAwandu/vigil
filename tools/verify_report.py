#!/usr/bin/env python3
"""Independent report verifier. Uses only the standard library unless verifying Ed25519 signatures.

No imports from Vigil or Ready Trader Go. A trusted hash, Merkle root or public key
must come from a separate trusted channel; internal consistency alone is not authenticity.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
from pathlib import Path

SEVERITIES = ["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"]


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def read_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def finite(literal):
        value = float(literal)
        if not math.isfinite(value):
            raise ValueError("non-finite number")
        return value

    def invalid(value):
        raise ValueError("non-JSON numeric constant")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_float=finite, parse_constant=invalid)


def hash_bytes(data):
    return hashlib.sha256(data).digest()


def root(items):
    if not items:
        return hash_bytes(b"")
    if len(items) == 1:
        return hash_bytes(b"\x00" + canonical(items[0]))
    split = 1
    while split * 2 < len(items):
        split *= 2
    return hash_bytes(b"\x01" + root(items[:split]) + root(items[split:]))


def check_proof(proof, trusted_root):
    size, index = proof["size"], proof["index"]
    if type(size) is not int or type(index) is not int or not 0 <= index < size < 2**63:
        raise ValueError("invalid proof size or index")
    siblings = [bytes.fromhex(s) for s in proof["proof"]]
    if any(len(s) != 32 for s in siblings):
        raise ValueError("invalid sibling size")

    def reconstruct(n, i):
        if n == 1:
            return hash_bytes(b"\x00" + canonical(proof["alert"]))
        if not siblings:
            raise ValueError("short inclusion proof")
        sibling = siblings.pop()
        split = 1
        while split * 2 < n:
            split *= 2
        if i < split:
            return hash_bytes(b"\x01" + reconstruct(split, i) + sibling)
        return hash_bytes(b"\x01" + sibling + reconstruct(n - split, i - split))

    calculated = reconstruct(size, index).hex()
    if siblings or calculated != trusted_root:
        raise ValueError("inclusion proof does not match trusted root")
    return calculated


def verify(path, expected=None, events=None):
    raw = Path(path).read_bytes()
    digest = hash_bytes(raw).hex()
    document = read_json(raw)
    if document.get("schema") not in ("vigil.compliance-report/1", "vigil.compliance-report/2"):
        raise ValueError("unsupported report schema")
    if raw != canonical(document):
        raise ValueError("report is not in the documented canonical encoding")
    alerts = document["alerts"]
    if not isinstance(alerts, list) or any(not isinstance(a, dict) for a in alerts):
        raise ValueError("invalid alerts")
    identifiers = [a["alert_id"] for a in alerts]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("duplicate alert identifiers")
    levels = [SEVERITIES.index(a["severity"]) for a in alerts]
    worst = SEVERITIES[max(levels)] if levels else None
    if document["summary"]["alerts"] != len(alerts) or document["summary"]["worst_severity"] != worst:
        raise ValueError("summary contradicts alerts")
    if root(alerts).hex() != document["alerts_merkle_root"]:
        raise ValueError("alert Merkle root mismatch")
    for value in (document["match"]["events_sha256"], document["rulebook"]["sha256"]):
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("invalid evidence hash")
    if expected and digest != expected:
        raise ValueError("report SHA-256 does not match the trusted hash")
    if events and hash_bytes(Path(events).read_bytes()).hex() != document["match"]["events_sha256"]:
        raise ValueError("event file does not match report")
    return {"report_sha256": digest, "alerts": len(alerts), "merkle_root": root(alerts).hex(),
            "trusted_hash_matched": bool(expected), "event_file_matched": bool(events)}


def verify_signature(digest, signature_path, public_key_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    public = Path(public_key_path).read_bytes()
    key = load_pem_public_key(public)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("trusted public key must be Ed25519")
    signature = read_json(Path(signature_path).read_bytes())
    if signature["schema"] != "vigil.signature/1" or signature["report_sha256"] != digest:
        raise ValueError("signature is for a different report or scheme")
    if signature["public_key_sha256"] != hash_bytes(public).hex():
        raise ValueError("signature does not name the trusted public key")
    key.verify(base64.b64decode(signature["signature_base64"], validate=True),
               b"vigil.signature/1\0" + bytes.fromhex(digest))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", nargs="?")
    parser.add_argument("--sha256", help="expected report hash obtained through a trusted channel")
    parser.add_argument("--events", help="original match_events.csv to fingerprint")
    parser.add_argument("--proof", help="alert proof JSON; requires --root")
    parser.add_argument("--root", help="trusted alert Merkle root")
    parser.add_argument("--signature")
    parser.add_argument("--public-key", help="trusted Ed25519 PEM public key")
    args = parser.parse_args(argv)
    try:
        if args.proof:
            if not args.root:
                raise ValueError("--proof requires an independently trusted --root")
            print(json.dumps({"verified_root": check_proof(read_json(Path(args.proof).read_bytes()), args.root)}))
        else:
            if not args.report:
                raise ValueError("a report path is required")
            result = verify(args.report, args.sha256, args.events)
            if args.signature:
                if not args.public_key:
                    raise ValueError("--signature requires a trusted --public-key")
                verify_signature(result["report_sha256"], args.signature, args.public_key)
                result["signature_verified"] = True
            result["note"] = "Internal consistency alone does not establish authenticity or truthful source events."
            print(json.dumps(result, indent=2))
        return 0
    except Exception as error:
        # A standalone verifier must fail closed, including on malformed shapes and invalid signatures.
        print(f"FAILED: {type(error).__name__}: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
