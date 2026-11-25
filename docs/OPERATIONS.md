# Operating and reviewing Vigil 0.2

## Locked setup and exact reproduction

Use Python 3.11–3.13 and [uv](https://docs.astral.sh/uv/). `uv.lock` pins the full dependency graph and hashes;
CI uses uv 0.12.22. The exchange installer separately pins and checksums its source and data.

```bash
uv sync --frozen --extra all
source .venv/bin/activate
python scripts/bootstrap_rtg.py
vigil reproduce --check docs/results/reproduction.json
python -I tools/verify_report.py artifacts/reproduction/report.json \
  --events tests/fixtures/live_capture/match_events.csv
```

`artifacts/reproduction/manifest.json` records source/config/fixture/lockfile hashes, package versions, Python,
platform, Git commit and working-tree state. Exact report equality is checked in CI on three Python versions.
Environment metadata stays outside the report so a different platform does not itself change the audit hash.
For the release's trusted report hash, use the checked manifest at the **v0.2.0 tag**, not an unreviewed mutable
copy. A non-locked `pip install -e '.[dev]'` remains available for the basic example.

## Failure behavior

The monitor uses at-least-once delivery. Outputs may repeat after a crash: downstream alert consumers must
deduplicate by `alert_id`, and report consumers by report hash plus report ID. Alert timestamps describe
market events; receipt time and delivery attempts are not part of the stable report.

| Trigger | Behavior |
| --- | --- |
| Invalid UTF-8/JSON, type, header, key mismatch, or end marker | Persist raw value/key and source offsets in `artifacts/quarantine/`, publish a rejection receipt, invalidate affected active coverage; no input commit if quarantine/output fails |
| Sequence gap | Emit REC-01 and stop interpreting the unreliable suffix; preserve source hashing and incomplete coverage |
| No end marker | Idle polls continue; after `--stale-after` seconds write an incomplete report |
| Active-match budget exhausted | Fail with input uncommitted; increase capacity/budget or resolve the unfinished matches before restarting |
| Per-match event budget exceeded | Stop surveillance, mark coverage incomplete; consume/hash the remaining tape without growing detector state |
| Rebalance/revocation | Drop state for revoked partitions; new owner replays from retained header |
| Report write or Kafka output failure | Fail before input commit; replay on restart |
| Anchor failure | Keep report, increment `anchor_failures`, continue; explicitly retry `vigil chain anchor REPORT` |

Default budgets are 16 active matches and 1,000,000 events per match. These are finite **event-count bounds**,
not a fixed-byte memory cap; choose smaller budgets for small hosts. Finished reports are not retained by the
CLI. Library callers can choose `retain_reports` (default 256). The first 1,024 gap intervals are retained for
late-event classification; later gaps remain counted and make coverage incomplete.

Keep `artifacts/monitor-contexts.sqlite3` with its WAL files on durable local storage. Each header offset and
consumer group freezes its regulatory snapshot, rulebook hash, and software fingerprint. Restart under changed
code/rules fails closed: restore the prior environment, or deliberately audit under a new group and report IDs.
Multiple workers on one host must share this context store and artifact directory. Multi-host context consensus
and snapshot checkpoints are not implemented. SQLite context history, report versions, and quarantined bytes
grow on disk; archive them under an explicit retention policy after the corresponding Kafka replay horizon.
Do not put the database on a network filesystem and assume SQLite WAL provides distributed coordination.

Use unique report IDs across runs. An incomplete report later replaced by a complete replay is archived under
`versions/<old-sha>.json`. The chain's write-once report ID cannot be amended: use a new ID for a new filing.
Source validation cannot authenticate a dishonest publisher. Local Kafka setup is loopback-only development
infrastructure; broker ACLs/TLS, storage permissions, and supervision belong to a deployment configuration.

## Metrics and failure tests

```bash
vigil monitor --group review --no-regulatory --metrics artifacts/metrics/review.json
VIGIL_TEST_KAFKA_FAULTS=1 pytest tests/test_kafka_faults.py -v
vigil benchmark tests/fixtures/live_capture/match_events.csv --out artifacts/benchmark.json
vigil soak --matches 1000 --out artifacts/soak.json
```

The monitor writes JSON and Prometheus text snapshots, including active matches, processed/rejected/duplicate/
missing events, completed/incomplete reports, alerts, partition changes, anchor failures, and processing-time
histogram buckets. Consumer lag measures log-end minus the last event handed to the application, **not committed
lag**; unfinished matches intentionally retain old committed offsets. Per-process counters reset on restart.
Give workers distinct `--metrics` files. Snapshots are local files, not an HTTP endpoint.

The fault suite starts a private real KRaft broker and actual monitor subprocesses. It kills a worker mid-match,
forces a report-write error, kills after output/before commit, restarts the broker while publishing, and joins
another worker during an unfinished match. It compares full report hashes with an uninterrupted reference.
It does not stop an existing development broker. `VIGIL_TOPIC_PREFIX` isolates topics for experiments.

The benchmark reports p50/p95/p99 handler times, replay throughput, separately measured Python allocation peak,
and SHA-256/Ed25519 timings with environment/input hashes. It excludes broker/network IO and does not measure
production capacity. The soak repeatedly completes 1,000 finite matches through a streaming test transport
and records live/peak Python allocations without retaining outputs; it is not a multi-day Kafka soak. `pytest` JUnit durations include fixture setup and recovery waits, not pure recovery latency.

## Evidence viewer and signatures

```bash
vigil view artifacts/reports/quickstart/report.json \
  --events artifacts/reports/quickstart/match_events.csv --out artifacts/evidence.html
# Open artifacts/evidence.html in your browser.
vigil attest keygen artifacts/keys/reviewer
vigil attest sign artifacts/reports/quickstart/report.json \
  --key artifacts/keys/reviewer/private.pem --out artifacts/quickstart.signature.json
python -I tools/verify_report.py artifacts/reports/quickstart/report.json \
  --signature artifacts/quickstart.signature.json --public-key artifacts/keys/reviewer/public.pem
```

The viewer filters findings by control/participant and displays thresholds, metrics, evidence events, and
before/after order-book levels. It verifies the event-file hash before including book snapshots. It is a local,
self-contained HTML file with no external requests; embedded evidence is escaped and rendered as text. The view
limits displayed evidence to 100 references per alert and 5,000 rows overall; the JSON/CSV retain everything.
See the [threat model](THREAT_MODEL.md) for how to trust hashes, signatures, and registry filings.

## Report schema migration

Version 2 moves transport-attempt counters from `match.transit` into `report.delivery.json`, linked by report
SHA-256. Only stable completeness and the reconstruction boundary remain in `match.coverage`. Identical input,
software, rulebook and frozen regulatory context now give identical report bytes even if one delivery contains
duplicates. The source-code fingerprint is included in `software`. Versions 1 and 2 remain readable/verifiable;
new reports use version 2 and therefore have different hashes from version 1. Existing chain filings remain
valid for their original bytes. Delivery sidecars are diagnostics and are not signed or anchored.
