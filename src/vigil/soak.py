"""Repeated monitor lifecycle measurement with a streaming, non-retaining transport."""
from __future__ import annotations

import gc
import time
import tracemalloc

from vigil.bus import topics
from vigil.bus.base import Message
from vigil.bus.services import Monitor
from vigil.evaluation import scenario
from vigil.provenance import environment
from vigil.reports.canonical import EventStreamHasher
from vigil.surveillance.engine import header_for


def soak(matches: int = 1000) -> dict:
    if not 10 <= matches <= 100000:
        raise ValueError("matches must be between 10 and 100000")
    records, _, _ = scenario("repricing", 11, 180)
    hasher = EventStreamHasher()
    for record in records:
        hasher.update(record)
    class StreamingBus:
        def consume(self, *args, **kwargs):
            offset = 0
            for n in range(matches):
                name = str(n)
                yield Message(topics.EVENTS, name, topics.header_message(header_for(name, "sim", teams=["Subject"])), 0, offset)
                offset += 1
                for record in records:
                    yield Message(topics.EVENTS, name, topics.event_message(name, record), 0, offset)
                    offset += 1
                yield Message(topics.EVENTS, name, topics.end_message(name, len(records), hasher.hexdigest()), 0, offset)
                offset += 1
        def publish(self, *args, **kwargs):
            pass  # downstream accepts output without retaining it in this benchmark
        def flush(self):
            pass
        def commit(self, *args, **kwargs):
            pass
    samples, completed = [], 0
    def saved(audit):
        nonlocal completed
        completed += 1
        if completed in (10, matches // 2, matches):
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            samples.append({"matches": completed, "current_python_bytes": current, "peak_python_bytes": peak})
    tracemalloc.start()
    started = time.perf_counter()
    monitor = Monitor(StreamingBus(), on_report=saved)
    retained = monitor.run(retain_reports=0)
    elapsed = time.perf_counter() - started
    tracemalloc.stop()
    return {"schema": "vigil.soak/1", "matches": completed, "events_per_match": len(records),
            "retained_reports": len(retained), "active_matches_at_end": len(monitor._matches),
            "elapsed_seconds": elapsed, "samples": samples, "environment": environment(),
            "scope": "Repeated finite matches through Monitor with a streaming test transport. Tracemalloc enabled; "
                     "no Kafka, filesystem, context-history or native-client memory. This tests lifecycle retention, "
                     "not long-running production resource use."}
