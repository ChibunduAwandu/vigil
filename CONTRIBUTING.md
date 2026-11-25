# Development workflow

Vigil is published for engineering review. Before proposing reuse or contributing
code, read [NOTICE.md](NOTICE.md); no open-source license has been selected for
Vigil's own code.

## Set up

Use Python 3.11–3.13 and run commands from the repository root:

```bash
make setup PYTHON=python3.12
make lint-python
make test
```

`make setup` installs the Python extras and downloads the separate Ready Trader Go
dependency. Kafka, Anvil, live-exchange, and public-network tests skip when their
requirements are absent. Use `pytest -ra` to see the reasons.

For integration checks, install Java 17+ and then:

```bash
make tools
make services
VIGIL_TEST_LIVE=1 .venv/bin/pytest -ra
make contract
make lint
make stop
```

Public API tests are a separate opt-in:

```bash
VIGIL_TEST_NETWORK=1 .venv/bin/pytest -m network -ra
```

The tests clear public-chain environment variables and use local development
chains. `.env` is not loaded automatically.

## Make a change reviewable

- Keep exchange source outside the publication. Use `VIGIL_RTG_HOME` to test a
  separate local exchange installation.
- Put control thresholds and mappings in `config/rulebook.yaml`. Add a scenario
  that exercises the behavior, including a normal trading case where applicable.
- Preserve deterministic report serialization and alert identifiers. Delivery
  changes should cover duplicate, missing, and replayed events.
- After editing the Solidity contract, run `make contract` to regenerate the
  packaged ABI/bytecode and check both Python and Solidity tests.
- `docs/CONTROLS.md` and `docs/RESULTS.md` are generated. Run
  `.venv/bin/python scripts/render_docs.py` after changing their inputs. Historical
  result JSON is evidence, not a scratch output directory; do not replace it unless
  intentionally recording a new experiment.
- Keep credentials, toolchains, generated runs, and local reports out of commits.
  The checked-in example is a deliberately selected review artifact.

## Test layout

| Tests | Purpose |
| --- | --- |
| `test_surveillance.py`, `test_exchange_rules.py` | Detectors, malformed records, and reconciliation against exchange behavior. |
| `test_bus.py`, `test_kafka_adapter.py` | Deduplication, restart offsets, interleaved matches, and delivery failures. |
| `test_reports.py`, `test_chain.py` | Canonical reports, tampering, inclusion proofs, and registry integration. |
| `test_sim.py`, `test_live.py`, `test_live_tailer.py` | Deterministic replay and actual exchange execution. |
| `test_regulatory.py` | Incremental regulatory ingestion, deduplication, and API failure handling. |
| `test_strategy.py`, `test_kelly.py`, `test_ml.py`, `test_challengers.py` | Trading and forecasting experiments. |
| `contracts/test/ComplianceRegistry.t.sol` | Solidity invariants, access control, and Merkle proof tests. |
