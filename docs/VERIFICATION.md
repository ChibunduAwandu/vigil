# Publication verification

Verified locally on **2026-11-24**, macOS ARM64, Python **3.12**, using a fresh
download of the exchange dependency pinned in `config/rtg-source.json`.

| Check | Result |
| --- | --- |
| Full Python suite with `VIGIL_TEST_LIVE=1` | 244 passed, 4 skipped. This included local Anvil integration and actual exchange processes. |
| Kafka test with the local broker running | 1 passed. This was the Kafka test skipped in the full run. |
| Captured-exchange replay and fixture checks after excluding upstream input data | 24 passed. The input excerpt is regenerated from the external dependency. |
| Foundry contract suite | 30 passed; four fuzz tests each ran 512 cases. |
| Ruff and Solidity formatting | Passed. |
| README simulation | 66 alerts; 1,393 reported trades reconciled; six abusive traders flagged and three baseline traders clean. |
| README captured-match audit | 0 alerts; all 36 reported trades reconciled. |
| Example report | Canonical JSON validated; report and alert Merkle root are internally consistent. |
| Publication checks | Local Markdown targets exist; no local `.env` values found in staged files; original exchange CSV bytes preserved in Git. |

There are **245 distinct passing Python tests** across the full suite and Kafka
run. The three remaining tests use public APIs and were not enabled. The 24-test
fixture check reruns tests already counted in that total.

The Kafka cleanup test covers a terminal delivery failure even when the producer's
queue is empty. The monitor regression verifies that a failed report delivery does
not commit the match's input offsets, allowing a restart from its original header.

The GitHub Actions template is in [ci.yml](ci.yml). Hosted CI and the Python
3.11/3.13 matrix have not run: the available publishing credentials lacked the
permission to upload Actions workflows. These are local verification results,
not a hosted CI status or a production certification.

To reproduce the Python checks with the optional toolchains installed:

```bash
make services
VIGIL_TEST_LIVE=1 .venv/bin/pytest -ra
make lint
make contract
make stop
```

Public API checks are independently opt-in with `VIGIL_TEST_NETWORK=1`. Re-running
the historical trading experiments is outside this verification; their recorded
methodology and limitations are in [RESULTS.md](RESULTS.md).
