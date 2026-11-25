"""Loading Ready Trader Go market data sessions."""
from __future__ import annotations

import csv
import functools
import hashlib
import os
from pathlib import Path

from vigil.rtg import Instrument, Lifespan, MarketEvent, MarketEventOperation, Side

# The exchange scales CSV prices (dollars) to integer cents; see market_events.INPUT_SCALING.
INPUT_SCALING = 100


@functools.lru_cache(maxsize=8)
def _load(path: str, mtime_ns: int) -> tuple[MarketEvent, ...]:
    events: list[MarketEvent] = []
    with open(path, newline="") as handle:
        reader = csv.reader(handle)
        next(reader)  # header
        for row in reader:
            # Parsed exactly as MarketEventsReader.reader does, so both see the same market.
            events.append(MarketEvent(
                float(row[0]),
                Instrument(int(row[1])),
                MarketEventOperation[row[2]],
                int(row[3]),
                Side[row[4]] if row[4] else None,
                int(float(row[5])) if row[5] else 0,
                int(float(row[6]) * INPUT_SCALING) if row[6] else 0,
                Lifespan[row[7]] if row[7] else None,
            ))
    return tuple(events)


def load_market_events(path: str | os.PathLike) -> tuple[MarketEvent, ...]:
    """Return the session's events. Cached: the events are never mutated by a replay."""
    resolved = Path(path).resolve()
    return _load(str(resolved), resolved.stat().st_mtime_ns)


@functools.lru_cache(maxsize=32)
def _sha256(path: str, mtime_ns: int) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_sha256(path: str | os.PathLike) -> str:
    resolved = Path(path).resolve()
    return _sha256(str(resolved), resolved.stat().st_mtime_ns)
