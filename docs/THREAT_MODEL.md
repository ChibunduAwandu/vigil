# Integrity, authenticity, and trust boundaries

Vigil records what its configured controls concluded from supplied events. An internally valid report can
still describe fabricated events or a poor detector. Hashing, signing, and anchoring solve narrower problems.

## Assets and adversaries

The assets are source-event bytes, the frozen rulebook and regulatory snapshot, report bytes, alert membership,
report availability, and the identity of the reporter. Consider an event publisher that lies, a consumer that
crashes, a local artifact that is changed, an unauthorized registry caller, and a compromised reporter key.
The host OS, Python environment, exchange implementation, and privileged registry administrator are trusted.

| Boundary / failure | Implemented defense | Remaining assumption |
| --- | --- | --- |
| Malformed Kafka JSON, duplicate keys, invalid fields | Strict parsing and shape/range checks; rejected bytes and key retained in local quarantine; rejection receipt on Kafka | Broker message-size limits and authenticated publishers are deployment responsibilities |
| Missing, reordered, duplicate events | Sequence checks, deterministic IDs, source hashes; stop reconstructing after the first gap | A publisher can forge a complete, self-consistent tape; no exchange signature authenticates the source |
| Crash before input commit | Atomic report writes, synchronous output flush, explicit offsets retaining unfinished headers | Kafka retains the header and all subsequent events; durable artifact storage survives the host |
| Consumer reassignment | Discard revoked in-memory state; replay the retained header on the new owner | Workers share the same software, rulebook, and durable context store; SQLite is a single-host baseline |
| Local report replacement | SHA-256, alert Merkle root, archived previous versions; optional Ed25519 signature or registry filing | The expected hash, public key, chain and registry address must come from a separate trusted channel |
| Compromised signing key | Signature verifies only the key that signed those bytes | No automatic key rotation/revocation or trusted timestamp exists in the local signature scheme |
| Unauthorized chain caller | Reporter authorization and write-once report IDs, tested in Solidity | An authorized malicious reporter can anchor false findings; administrator compromise remains in scope for deployment review |
| Chain outage or reorganization | Local report persists even when anchoring fails; failure counter; explicit retry using the CLI | A transaction receipt is not finality; users select chain-specific confirmation and reorg policies |
| Report unavailable | Full report stays off chain; optional URI in filing | A hash cannot restore a lost report; keep backed-up/versioned storage and access controls |
| Sensitive metadata | Default anchor URI empty; offline viewer makes no requests | Filing IDs, hashes, reporter addresses and any supplied URI are public; never put private evidence or secrets in a public URI |

## Why a chain at all?

| Approach | What an independent reviewer gets | Costs and limits |
| --- | --- | --- |
| SHA-256 alone | Byte equality with a previously trusted hash | No signer, timestamp, or publication history |
| Ed25519 signed report | Authenticity relative to a trusted public key; local verification | Small local CPU/storage cost; signer can issue conflicting reports; no independent timestamp or global ordering |
| Append-only audit log | Ordered publication and, with witnesses/checkpoints, evidence of log equivocation | Requires operating/trusting a log and distributing authenticated checkpoints; comparison only, not implemented here |
| Solidity registry | Shared filing history and authorization; first filing for an ID cannot be replaced through the contract | Transaction fees, finality assumptions, public metadata, chain availability; does not verify detector correctness |

For a single reviewer who already trusts the author's public key, the signature baseline is usually enough.
The registry is an experiment in independently observable publication and ordering. See the measured local
hash/sign/verify timings in [benchmark.json](results/benchmark.json). Contract tests report gas usage; a fee
estimate is `gas_used × gas_price_gwei × 10⁻⁹ ETH`, before any chain-specific extra fees. No dollar-cost or
production-throughput claim is made.

## Independent verification

`tools/verify_report.py` imports neither Vigil nor the exchange. Report hashing, strict JSON decoding, summary
checks, Merkle tree reconstruction, and proof checking use the Python standard library. Ed25519 verification
optionally imports `cryptography`. The Merkle verifier uses a separate recursive implementation and is checked
against unbalanced trees and tampered proofs. This is independent code, not an independent security audit.

Report encoding is Python JSON with sorted string keys, compact separators, UTF-8, literal non-ASCII, and no
non-finite numbers or duplicate keys. It is **not RFC 8785 / JCS**. Preserve the exact report bytes, particularly
numeric representations. Leaves are `SHA256(0x00 || canonical_alert)`, internal nodes are
`SHA256(0x01 || left || right)`, and the empty root is `SHA256(empty)`. Split at the largest power of two
strictly smaller than the subtree size; do not duplicate an odd leaf.

The signing payload is `b"vigil.signature/1\0" + bytes.fromhex(report_sha256)`. The envelope includes the
report hash and the SHA-256 of the PEM-encoded public key. Distribute that public key separately; accepting a
key bundled by an untrusted sender does not authenticate the sender. Key generation refuses existing files
and writes private keys with mode 0600. Local unencrypted keys are a demonstration; use managed signing
infrastructure for a real deployment.
