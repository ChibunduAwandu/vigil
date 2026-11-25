# Frozen synthetic challenge evaluation

The new evaluation uses separate scenario generators from the historical `badactors.py` traders. It freezes
the existing rulebook hash, ten scenario definitions and eight evaluation seeds in
[config/evaluation.json](../config/evaluation.json). Seed 11 is reserved for development checks; evaluation
uses 67, 101, 211, 307, 401, 503, 601 and 701. No control threshold was changed to improve these results.

**This is not an independent accuracy study.** The generators and expectations were authored with knowledge
of the detectors. Seeds were separated, but no external reviewer supplied blind labels, and the four historical
market sessions had already been inspected. External labeled data remains necessary for real-market validation.

## What is measured

Each seed produces ten 180-second tapes: flash bursts, slow order churn, off-market quoting, ordinary repricing,
large cancellations, reciprocal trading, unhedged exposure, a position breach, layering and a closing-price
trade. A corrupted copy of each tape removes one event and tests REC-01. That gives **80 behavioral episodes
and 80 gap episodes**. These are synthetic event scripts, not claims that the real exchange would accept every
script after a limit breach. Uncorrupted tapes reconcile against reconstructed matching and fees in tests.

The binary unit is (episode, participant, control). Repeated findings count once in precision/recall and
individually in the false-alert rate. A gap episode uses a synthetic stream participant; gap episodes are
excluded from the participant-hours denominator. True negatives are provided in the JSON but do not dominate
an advertised accuracy percentage. Detection delay uses actual first alert emission minus episode start;
finalization-only controls are measured at the end of the tape. Full labels, observed controls, hashes and
delays are in [evaluation.json](results/evaluation.json).

## Recorded results

| Control | TP | FP | FN | Precision | Recall |
| --- | ---: | ---: | ---: | ---: | ---: |
| LIM-01 | 8 | 0 | 0 | 100.0% | 100.0% |
| LIM-02 | 16 | 0 | 0 | 100.0% | 100.0% |
| LIM-03 | 8 | 0 | 0 | 100.0% | 100.0% |
| MKT-01 | 8 | 0 | 0 | 100.0% | 100.0% |
| MKT-02 | 0 | 16 | 0 | 0.0% | — |
| MKT-03 | 8 | 0 | 0 | 100.0% | 100.0% |
| MKT-04 | 8 | 0 | 0 | 100.0% | 100.0% |
| MKT-05 | 16 | 0 | 0 | 100.0% | 100.0% |
| MKT-06 | 8 | 0 | 0 | 100.0% | 100.0% |
| REC-01 | 80 | 0 | 0 | 100.0% | 100.0% |

Across these particular units, precision is **90.9%** and recall is
**100.0%**. The 16 false alerts over
4.4 synthetic participant-hours give **3.64 false alerts per participant-hour**.
Those numbers must not be extrapolated to a real exchange. MKT-02 has no positive labeled case in this new
challenge, so its recall is intentionally undefined; the earlier wash-trader cases remain in the historical
suite and unit tests.

The difficult negative is reciprocal trading by two participants labeled as ordinary counterparties. Pair
concentration and balanced flow produce wash-trading alerts, even though the tape contains no beneficial
ownership or intent evidence. The observable tape cannot establish the hidden label. This is a documented
false positive against the scenario expectation, not evidence that the detector can distinguish intent.

## Sensitivity and uncertainty

The runner scales `MKT-01.min_lots`, `MKT-02.min_round_trip_lots`, `MKT-03.burst_messages`,
`MKT-05.min_orders`, and `MKT-06.collar` together by 0.8, 1.0 and 1.2. It reports each result without
selecting an optimum. These scenarios lie far enough from the changed thresholds that the recorded scores
are identical across all three factors; this is limited sensitivity coverage, not proof of robustness near
threshold boundaries.

The JSON includes 95% percentile-bootstrap intervals from 1,000 seed-block resamples. Entire scenario sets
stay together within a sampled seed. Here the intervals collapse to the point estimates because price/time
variation did not change the confusion counts. Eight synthetic seeds do not represent real-market uncertainty.

## Reproduce or extend

```bash
vigil evaluate --out artifacts/evaluation.json
pytest tests/test_review.py
```

The evaluator refuses a changed rulebook hash. To conduct a new experiment, version the protocol and explain
the new labels/data before tuning; retain previous results. Next research steps are externally labeled matches,
more counterparties and regimes, near-boundary cases, and ownership/account context for wash-trading review.
Historical 36-match results are preserved separately in [RESULTS.md](RESULTS.md#surveillance).
