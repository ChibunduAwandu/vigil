> For version 0.2 report schemas, signatures, metrics, fault tests and locked replay, see [OPERATIONS.md](OPERATIONS.md).

# CLI workflows

Start with the [README quick start](../README.md#quick-start). Commands below assume
the virtual environment is active and you are in the repository root. Run
`vigil --help` or `vigil <command> --help` for every option.

## Audit a captured match

The short test capture makes this reproducible without waiting for a live match:

```bash
vigil audit tests/fixtures/live_capture/match_events.csv \
  --exchange-json tests/fixtures/live_capture/exchange.json \
  --score-board tests/fixtures/live_capture/score_board.csv \
  --match-id capture-review --no-regulatory
vigil report artifacts/reports/capture-review/report.json
```

Use the exchange configuration and score board from the same match when available.
The configuration supplies the limits and fees; the score board adds an independent
check of final positions and P&L. Reconciliation checks the record, while the
surveillance controls look for suspicious behavior in that record.

## Monitor through Kafka

Install the `kafka` extra and Java 17+, then start a local broker:

```bash
python -m pip install -e '.[kafka]'
scripts/bootstrap_tools.sh kafka
scripts/kafka_local.sh start
```

Terminal A:

```bash
vigil monitor --group review --no-regulatory
```

Terminal B:

```bash
vigil simulate --session 2 --scenario rogues --match-id kafka-review --publish --no-regulatory
# Or publish a captured match:
vigil produce tests/fixtures/live_capture/match_events.csv \
  --exchange-json tests/fixtures/live_capture/exchange.json --match-id capture-kafka
```

The publisher's report is in `artifacts/runs/<match-id>/`; the monitor's report is
in `artifacts/reports/<match-id>/`. Simulation reports can additionally contain exchange
outcomes, so their hashes need not equal monitor reports. Compare alert IDs and alert
Merkle roots, and check `match.coverage.complete`. Delivery counters now live separately
in `report.delivery.json` and do not change the version-2 report hash.

A new consumer group starts at the earliest retained event. `--once` exits after
the first completed match, which may be an older one on an existing broker. Reuse
the group to resume from committed offsets, or leave the monitor running until
your desired match appears. Stop it with Ctrl-C.

Offsets are committed after reporting and flushing output, held back to the header
of the oldest unfinished match on each partition. Restarts may publish alerts and
report receipts again. Consumers should use stable identifiers to deduplicate.
Recovery requires that Kafka still retains the original header and events. Use
workers on the same host with shared durable context storage; partition-rebalance replay
is tested. Multi-host coordination is not implemented. See [OPERATIONS.md](OPERATIONS.md).

```bash
scripts/kafka_local.sh stop
```

## Anchor and verify on a local Ethereum chain

```bash
python -m pip install -e '.[chain]'
scripts/bootstrap_tools.sh foundry
scripts/anvil_local.sh start
export VIGIL_ETH_RPC=http://127.0.0.1:8545
unset VIGIL_ETH_PRIVATE_KEY VIGIL_REGISTRY ETHERSCAN_API_KEY

vigil chain anchor artifacts/reports/quickstart/report.json
vigil chain verify artifacts/reports/quickstart/report.json
vigil chain about artifacts/reports/quickstart/report.json
vigil chain history
```

The CLI deploys a registry automatically when none is known on the local development
chain. A filing stores the report hash, event-stream hash, rulebook hash, alert
Merkle root, and metadata. A report identifier can be filed only once; a changed
report with that identifier is rejected.

To prove one alert belongs to a report, copy an alert ID from the JSON or Markdown:

```bash
vigil chain proof artifacts/reports/quickstart/report.json <alert-id>
scripts/anvil_local.sh stop
```

Reformatting or changing even one byte of the canonical report changes its SHA-256.
The proof verifies inclusion in the anchored report; it does not establish that an
alert's interpretation is correct. Verification also depends on selecting a
trusted registry address, rather than trusting an address supplied by an unknown
report.

Public-network settings are documented in [`.env.example`](../.env.example).
The CLI reads exported environment variables and does not load `.env` on its own.
Local examples need no public-network key or funds.

## Run the actual exchange

```bash
vigil live --session 1 --seconds 30 --match-id live-review --no-regulatory \
  --team VigilFull=preset:full --team Example=example --team Spoofer=actor:spoofer
```

This launches one process per trader and tails the exchange's event CSV. Files and
the local audit are kept in `artifacts/runs/live-review/`. Add `--publish` to send
events through Kafka to a separate monitor. A custom trader can be supplied as
`--team Mine=path/to/autotrader.py`.

The optional exchange viewer needs `python -m pip install -e '.[hud]'`. Use `--hud`
for a live window or `vigil replay artifacts/runs/live-review/match_events.csv`
afterward. Leave the viewer off when measuring timing: it shares CPU with traders.

## Ingest regulatory documents

```bash
vigil regulatory scrape --years 1
vigil regulatory show
vigil regulatory scrape --every 3600
```

These commands use the public Federal Register API and need internet access.
The persistent register deduplicates document numbers and hashes content to
recognize revisions. The recurring command stays running; it does not install a
system scheduler. New reports include the local register snapshot when available.
Use `--no-regulatory` on an audit or monitor to omit that input for repeatable demos.

## Full demo and research commands

`make demo` starts local services, ingests regulatory documents, audits and anchors
a simulated match, attempts verification of a tampered copy, and monitors a live
match through Kafka. Run `make setup` and `make tools` first. It needs Java, network
access for ingestion, and several minutes. Stop services afterward with `make stop`.

The research commands are optional and overwrite tracked models or result data:

| Command | Purpose |
| --- | --- |
| `vigil train` | Fit and evaluate the AdaBoost forecasting model. |
| `vigil backtest` | Compare strategy components and tournaments. |
| `vigil calibrate` | Evaluate surveillance against the scenario suite. |
| `make results` | Run those measurements and regenerate the results page. |
| `make challengers` | Fit and evaluate the experimental strategy variants. |

See [RESULTS.md](RESULTS.md) for methodology and historical measurements. Historical
live comparisons cannot all be regenerated from today's code; the results page
states which trader versions each comparison used.

## Common setup issues

- **Python 3.14:** use 3.11–3.13. The external exchange uses asyncio behavior removed
  in 3.14.
- **Exchange missing:** run `python scripts/bootstrap_rtg.py`. If you already have
  the matching distribution elsewhere, export `VIGIL_RTG_HOME=/absolute/path`.
- **Exchange download fails:** rerun the setup command. Partial downloads are not
  installed. Existing source/data that differ are left untouched; use
  `--destination /another/path` and set `VIGIL_RTG_HOME` to that path.
- **Kafka or chain unavailable:** these are optional for direct audits. Start the
  relevant local service before using `--publish` or `--anchor`.
- **Tests skipped:** `pytest -ra` gives the missing dependency or opt-in variable.
- **Different report hash:** keep the software, model, match ID/header, rulebook,
  regulatory snapshot and software fixed when comparing a replay.
