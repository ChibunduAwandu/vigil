# Changelog

## 0.2.0 — Review and reliability release

- Validate Kafka envelopes, headers, record fields and end markers; preserve rejected bytes/keys with offsets.
- Constrain artifact paths, write reports atomically, and archive replaced report versions.
- Expire quiet matches through idle polls, bound active/event/report retention, and rebuild revoked partitions.
- Persist replay context in SQLite, freezing regulatory data, source/exchange fingerprints, rules and runtime.
- Separate stable version-2 reports from delivery-attempt diagnostics; preserve version-1 verification.
- Exercise real process death, disk failure, output-before-commit failure, broker restart and group rebalance.
- Add a frozen synthetic challenge, explicit difficult negatives, per-control metrics and seed-bootstrap intervals.
- Add runtime JSON/Prometheus metrics, local replay/signing benchmarks and a repeated-match retention soak.
- Add an offline evidence viewer, Ed25519 attestations, and a standalone report/Merkle verifier.
- Pin Python dependencies with `uv.lock`; enable the Python matrix and integration CI; publish reproduction inputs.

See [migration and operations](docs/OPERATIONS.md), [evaluation limitations](docs/EVALUATION.md), and the
[threat model](docs/THREAT_MODEL.md). This remains a research prototype; no production certification or
independent real-market accuracy claim is made. Vigil's own licensing remains unselected.
