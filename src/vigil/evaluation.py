"""Frozen synthetic challenge episodes, independent of the historical bad-actor classes.

These scripts deliberately include ambiguous negatives. Their labels express review
expectations, not inferred legal intent. No threshold is selected using evaluation scores.
"""
from __future__ import annotations

import dataclasses
import json
import random
from collections import Counter
from pathlib import Path

from vigil.provenance import file_hash, software
from vigil.reports.canonical import EventStreamHasher
from vigil.rtg.events import MatchRecord
from vigil.surveillance.engine import SurveillanceEngine, header_for
from vigil.surveillance.rulebook import load_rulebook


class Tape:
    def __init__(self):
        self.rows = []
        self.next_id = 100

    def add(self, time, owner, op, order, instrument=None, side=None, volume=0, price=None, life=None, fee=None):
        self.rows.append(MatchRecord(len(self.rows), round(time, 6), owner, op, order,
                                     instrument, side, volume, price, life, fee))

    def insert(self, time, owner, side, volume, price, instrument=1):
        self.next_id += 1
        self.add(time, owner, "Insert", self.next_id, instrument, side, volume, price, "G")
        return self.next_id

    def cancel(self, time, owner, order, volume):
        self.add(time, owner, "Cancel", order, volume=-volume)

    def trade(self, time, owner, order, side, volume, price, maker=False):
        self.add(time, owner, "Trade", order, 1, side, volume, price,
                 fee=round(price * volume * (-.0001 if maker else .0002)))


def scenario(kind: str, seed: int, duration: float):
    rng, tape = random.Random(seed), Tape()
    shift = rng.randrange(-20, 21) * 100
    mid = 100000 + shift
    for instrument in (0, 1):
        tape.insert(.01 + instrument * .01, "", "B", 100000, mid - 100, instrument)
        tape.insert(.01 + instrument * .01, "", "A", 100000, mid + (200 if kind == "close" and instrument == 1 else 100), instrument)
    tape.insert(.03, "", "B", 1, mid + 100, 0)  # future last trade used for closing valuation
    labels, start = set(), 1. + rng.random()
    who = "Subject"
    if kind in ("flash_bursts", "slow_churn", "repricing", "large_cancels", "off_market"):
        if kind == "flash_bursts":
            schedule = [(start + burst * 5 + i * .015, 1, mid - 200) for burst in range(6) for i in range(22)]
            labels = {(who, c) for c in ("LIM-03", "MKT-03", "MKT-05")}
        elif kind == "slow_churn":
            schedule = [(start + i, 1, mid - 200) for i in range(120)]
            labels = {(who, "MKT-05")}
        elif kind == "repricing":
            schedule = [(start + i * 3, 20, mid - (1 + i % 3) * 100) for i in range(50)]
        elif kind == "large_cancels":
            schedule = [(start + i * 12, 80, mid - 100) for i in range(8)]
        else:
            schedule = [(start, 1, round(mid * .94 / 100) * 100)]
            labels = {(who, "MKT-06")}
        for when, volume, price in schedule:
            order = tape.insert(when, who, "B", volume, price)
            tape.cancel(when + (.004 if kind == "flash_bursts" else .4), who, order, volume)
    elif kind == "reciprocal":
        # Two otherwise legitimate counterparties alternating liquidity. No common-owner label.
        # Pair concentration is deliberately high: a difficult negative for a market-only detector.
        for n in range(8):
            when = start + n * 16
            seller, buyer = ("Subject", "Peer") if n % 2 else ("Peer", "Subject")
            order = tape.insert(when, seller, "A", 10, mid)
            taker = tape.insert(when + .1, buyer, "B", 10, mid)
            tape.trade(when + .1, seller, order, "A", 10, mid, True)
            tape.trade(when + .1, buyer, taker, "B", 10, mid)
    elif kind in ("exposure", "position", "close"):
        when = duration - 10 if kind == "close" else start
        volume = 120 if kind == "position" else 60
        price = mid + (200 if kind == "close" else 100)
        order = tape.insert(when, who, "B", volume, price)
        tape.trade(when, who, order, "B", volume, price)
        labels = {(who, "MKT-04")} if kind == "close" else {(who, "LIM-02")}
        if kind == "position":
            labels.add((who, "LIM-01"))
    elif kind == "layering":
        labels = {(who, "MKT-01")}
        for n in range(6):
            when = start + n * 12
            shown = [tape.insert(when, who, "B", 20, mid - 100 * level) for level in (1, 2, 3)]
            taker = tape.insert(when + .1, who, "A", 1, mid - 100)
            tape.trade(when + .1, who, taker, "A", 1, mid - 100)
            for order in shown:
                tape.cancel(when + .2, who, order, 20)
    else:
        raise ValueError(f"unknown evaluation case: {kind}")
    tape.insert(duration, "", "B", 1, mid - 1000, 0)
    # REC-01 is tested by a separate corrupted copy of every challenge tape.
    return tape.rows, labels, start


def score(counts):
    tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
    return {**dict(counts), "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None}


def evaluate(protocol_path: Path) -> dict:
    protocol = json.loads(protocol_path.read_text())
    if set(protocol["development_seeds"]) & set(protocol["evaluation_seeds"]):
        raise ValueError("development and evaluation seeds overlap")
    base = load_rulebook()
    if base.sha256 != protocol["rulebook_sha256"]:
        raise ValueError("rulebook changed since this protocol was frozen; version the protocol explicitly")
    episodes, sensitivities = [], []
    for factor in protocol["sensitivity_factors"]:
        controls = dict(base.controls)
        for control, parameter in (("MKT-06", "collar"), ("MKT-03", "burst_messages"),
                                   ("MKT-01", "min_lots"), ("MKT-02", "min_round_trip_lots"),
                                   ("MKT-05", "min_orders")):
            cfg = controls[control]
            controls[control] = dataclasses.replace(cfg, parameters={**cfg.parameters, parameter: cfg.parameters[parameter] * factor})
        rulebook = dataclasses.replace(base, controls=controls)
        all_counts = Counter(tp=0, fp=0, fn=0, tn=0)
        by_control = {c: Counter(tp=0, fp=0, fn=0, tn=0) for c in controls}
        by_seed = []
        false_alerts, hours = 0, 0.
        delays = []
        for seed in protocol["evaluation_seeds"]:
            seed_counts = Counter(tp=0, fp=0, fn=0, tn=0)
            for case in protocol["cases"]:
                records, labels, start = scenario(case, seed, protocol["duration_seconds"])
                for corrupt in (False, True):
                    name = f"challenge-{seed}-{case}" + ("-gap" if corrupt else "")
                    teams = ["Subject", "Peer"] if case == "reciprocal" else ["Subject"]
                    expected = {(t, "REC-01") for t in ["__stream__"]} if corrupt else labels
                    engine = SurveillanceEngine(header_for(name, "sim", teams=teams), rulebook)
                    emitted, hasher = {}, EventStreamHasher()
                    for index, record in enumerate(records):
                        hasher.update(record)
                        if corrupt and index == 5:
                            engine.gap(record.seq - 1, 1)
                            continue
                        for alert in engine.process(record):
                            emitted.setdefault((alert.competitor, alert.control), record.time)
                    result = engine.finalize()
                    observed = {(a.competitor, a.control) for a in result.alerts}
                    if corrupt:
                        # Reconciliation findings can identify the stream rather than a participant.
                        observed = {("__stream__", a.control) for a in result.alerts}
                    units = ["__stream__"] if corrupt else teams
                    for team in units:
                        for control in controls:
                            pair = team, control
                            key = "tp" if pair in expected and pair in observed else "fn" if pair in expected else "fp" if pair in observed else "tn"
                            all_counts[key] += 1
                            seed_counts[key] += 1
                            by_control[control][key] += 1
                    hours += (0 if corrupt else len(units)) * protocol["duration_seconds"] / 3600
                    false_alerts += sum((a.competitor, a.control) not in labels for a in result.alerts) if not corrupt else 0
                    detected_delays = [max(0., emitted.get(pair, protocol["duration_seconds"]) - start)
                                       for pair in expected & observed if not corrupt]
                    delays.extend(detected_delays)
                    if factor == 1:
                        episodes.append({"id": name, "seed": seed, "case": case, "corrupted": corrupt,
                                         "source_events_sha256": hasher.hexdigest(), "dropped_seq": 5 if corrupt else None,
                                         "expected": sorted(expected), "observed": sorted(observed),
                                         "detection_delays_seconds": detected_delays,
                                         "integrity": result.integrity})
            by_seed.append(seed_counts)
        rng, samples = random.Random(protocol["bootstrap_seed"]), []
        for _ in range(protocol["bootstrap_repetitions"]):
            sample = Counter(tp=0, fp=0, fn=0, tn=0)
            for chosen in rng.choices(by_seed, k=len(by_seed)):
                sample.update(chosen)
            samples.append(score(sample))
        def interval(metric, samples=samples):
            values = sorted(s[metric] for s in samples if s[metric] is not None)
            return [values[int(.025 * (len(values) - 1))], values[int(.975 * (len(values) - 1))]] if values else None
        sensitivities.append({"factor": factor, "overall": score(all_counts),
                              "per_control": {c: score(counts) for c, counts in by_control.items()},
                              "seed_bootstrap_95_percent": {m: interval(m) for m in ("precision", "recall")},
                              "false_alerts": false_alerts, "participant_hours": hours,
                              "false_alerts_per_participant_hour": false_alerts / hours,
                              "median_detection_seconds": sorted(delays)[len(delays) // 2] if delays else None})
    return {"schema": "vigil.evaluation/1", "protocol_sha256": file_hash(protocol_path),
            "rulebook_sha256": base.sha256, "generator_sha256": file_hash(Path(__file__)), "software": software(),
            "scope": protocol["scope"], "episodes": episodes, "sensitivity": sensitivities,
            "method": "Binary unit = (episode, participant, control); repeated alerts count once in precision/recall, "
                      "but individually in false-alert rate. Gap episodes evaluate REC-01 only on a synthetic stream unit. "
                      "Delay is first emission minus episode start; finalization-only findings use session end. "
                      "95% intervals resample seeds, retaining every scenario within a seed; they do not quantify "
                      "real-market uncertainty. Sensitivity scales five documented thresholds without selecting a winner."}
