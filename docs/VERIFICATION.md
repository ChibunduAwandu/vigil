# Version 0.2 verification

Verified locally on **2026-11-25**, macOS ARM64 and Python **3.12.9**, against the separately downloaded,
checksummed exchange. Python dependencies are recorded in `uv.lock`.

| Check | Observed result |
| --- | --- |
| Main suite with actual exchange and local Anvil (`VIGIL_TEST_LIVE=1`, excluding Kafka/fault/network markers) | 283 passed. |
| Final focused CLI, validation, verifier and review regression run | 52 passed, including one additional deeply nested Kafka JSON case; other cases overlap the main suite. |
| Real Kafka round-trip | 1 passed. |
| Isolated Kafka fault suite | 5 passed: process kill, disk-write failure, output/commit boundary, broker restart, live rebalance. |
| Distinct Python cases across these runs | **290 passed**; three public API tests were not enabled. |
| Solidity | **30 passed**, including four fuzz tests with 512 runs each; formatting passed. |
| Ruff | Passed for `src`, `tests`, `scripts`, and `tools`. |
| README simulation | 66 alerts, 1,393 trade reports reconciled; the baseline participants remained unflagged. |
| Captured-match reproduction | Zero alerts; 36 trade reports reconciled; expected bytes recorded in the reproduction manifest. |
| Independent verifier | Version 1 and 2, canonical encoding, expected hashes, event hashes, Ed25519 signatures, and unbalanced/tampered Merkle proofs checked. |
| Offline viewer | Browser-tested filtering, finding selection, threshold details, event timeline and before/after books; payload-escaping and hash-mismatch tests pass. |
| Repeated-match retention | 1,000 matches / 106,000 events, zero retained reports and zero active matches at completion; allocation samples in the soak artifact. |

[GitHub Actions](https://github.com/ChibunduAwandu/vigil/actions/workflows/ci.yml) runs Python 3.11–3.13,
captured-match hash reproduction, the README simulation, Kafka fault injection, live exchange, and Solidity.
Use the run linked to a particular commit to assess hosted status; the table above records local evidence.

## Reproduce

```bash
uv sync --frozen --extra all
source .venv/bin/activate
python scripts/bootstrap_rtg.py
make tools
VIGIL_TEST_LIVE=1 pytest -m 'not kafka and not kafka_faults and not network' -ra
VIGIL_TEST_KAFKA_FAULTS=1 pytest tests/test_kafka_faults.py -v
make services
pytest -m kafka
make stop
make lint-python
make contract
vigil reproduce --check docs/results/reproduction.json
```

Public API checks are independently opt-in with `VIGIL_TEST_NETWORK=1`. Historical trading experiments were
not regenerated; their original methodology and limitations remain in [RESULTS.md](RESULTS.md).

The new [challenge evaluation](EVALUATION.md), [benchmark](results/benchmark.json),
[retention soak](results/soak.json), and [reproduction manifest](results/reproduction.json) identify their
inputs and software. They do not establish production readiness, externally validated detector accuracy,
legal compliance, or an independent security audit.
