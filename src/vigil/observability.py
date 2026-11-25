"""Bounded runtime counters and processing-time histograms, without a telemetry service."""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

from vigil.storage import atomic_write

BOUNDS = tuple(10**power for power in (-6, -5, -4, -3, -2, -1, 0, 1))


class Metrics:
    def __init__(self):
        self.counts = Counter()
        self.buckets = [0] * (len(BOUNDS) + 1)
        self.seconds = 0.0
        self.started = time.monotonic()
        self.lag: dict[str, int] = {}

    def observe(self, seconds: float) -> None:
        self.counts["processed_messages"] += 1
        self.seconds += seconds
        index = next((i for i, bound in enumerate(BOUNDS) if seconds <= bound), len(BOUNDS))
        self.buckets[index] += 1

    def snapshot(self, active: int) -> dict:
        return {"uptime_seconds": round(time.monotonic() - self.started, 3), "counters": dict(self.counts),
                "active_matches": active, "consumer_lag_events": self.lag,
                "processing_seconds": {"bounds": [*BOUNDS, "+Inf"], "counts": self.buckets,
                                       "sum": self.seconds}}

    def save(self, path: Path, active: int) -> None:
        snapshot = self.snapshot(active)
        atomic_write(path, (json.dumps(snapshot, indent=2) + "\n").encode())
        lines = [f"vigil_{name}_total {count}" for name, count in sorted(self.counts.items())]
        lines.append(f"vigil_active_matches {active}")
        for partition, lag in sorted(self.lag.items()):
            lines.append(f'vigil_consumer_lag_events{{partition={json.dumps(partition)}}} {lag}')
        cumulative = 0
        for upper, count in zip([*BOUNDS, "+Inf"], self.buckets, strict=True):
            cumulative += count
            lines.append(f'vigil_processing_seconds_bucket{{le="{upper}"}} {cumulative}')
        lines += [f"vigil_processing_seconds_sum {self.seconds}", f"vigil_processing_seconds_count {cumulative}"]
        atomic_write(path.with_suffix(".prom"), ("\n".join(lines) + "\n").encode())
