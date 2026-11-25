"""The match-event record: the one data shape every stage of the pipeline shares.

Ready Trader Go reports everything that happens in a match as a flat stream of
events (``match_events.csv`` on disk, the heads-up-display feed on the wire).
``MatchRecord`` is a faithful, immutable copy of one such event plus the
sequence number that fixes its position in the stream. Order matters: the
surveillance engine rebuilds the order books by replaying the stream, so a
reordered stream rebuilds a different market.
"""
from __future__ import annotations

import csv
import os
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from typing import Any

from vigil.rtg import MatchEvent, MatchEventOperation

INSERT = "Insert"
AMEND = "Amend"
CANCEL = "Cancel"
HEDGE = "Hedge"
TRADE = "Trade"
OPERATIONS = (AMEND, CANCEL, INSERT, HEDGE, TRADE)

FUTURE = 0
ETF = 1

CSV_HEADER = ["Time", "Competitor", "Operation", "OrderId", "Instrument", "Side", "Volume", "Price", "Lifespan", "Fee"]

_OP_NAME = {op: op.name.capitalize() for op in MatchEventOperation}


def _number(text: str) -> int | float:
    value = float(text)
    return int(value) if value.is_integer() else value


@dataclass(frozen=True, slots=True)
class MatchRecord:
    """One event of a Ready Trader Go match.

    ``competitor`` is the team name, or ``""`` for orders that come from the
    replayed market data. ``side`` is ``"A"`` (ask/sell) or ``"B"`` (bid/buy),
    prices are in cents, and for ``Amend``/``Cancel`` the ``volume`` is the
    (negative) change in the order's volume.
    """

    seq: int
    time: float
    competitor: str
    op: str
    order_id: int
    instrument: int | None = None
    side: str | None = None
    volume: int = 0
    price: int | float | None = None
    lifespan: str | None = None
    fee: int | None = None

    @property
    def is_market(self) -> bool:
        return self.competitor == ""

    @classmethod
    def from_rtg(cls, seq: int, event: MatchEvent) -> MatchRecord:
        return cls(
            seq=seq,
            time=round(event.time, 6),
            competitor=event.competitor,
            op=_OP_NAME[event.operation],
            order_id=event.order_id,
            instrument=int(event.instrument) if event.instrument is not None else None,
            side="AB"[int(event.side)] if event.side is not None else None,
            volume=event.volume,
            price=event.price,
            lifespan="FG"[int(event.lifespan)] if event.lifespan is not None else None,
            fee=event.fee,
        )

    @classmethod
    def from_row(cls, seq: int, row: list[str]) -> MatchRecord:
        """Parse one data row of ``match_events.csv``."""
        return cls(
            seq=seq,
            time=float(row[0]),
            competitor=row[1],
            op=row[2],
            order_id=int(row[3]),
            instrument=int(row[4]) if row[4] else None,
            side=row[5] or None,
            volume=int(row[6]) if row[6] else 0,
            price=_number(row[7]) if row[7] else None,
            lifespan=row[8] or None,
            fee=int(row[9]) if len(row) > 9 and row[9] else None,
        )

    def to_row(self) -> list[Any]:
        return [self.time, self.competitor, self.op, self.order_id, self.instrument, self.side, self.volume,
                self.price, self.lifespan, self.fee]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MatchRecord:
        return cls(**data)


@dataclass(frozen=True)
class MatchHeader:
    """What a consumer needs to know about a match before its first event."""

    match_id: str
    source: str  # "sim", "live" or "csv"
    started_at: str  # UTC, ISO 8601
    teams: list[str] = field(default_factory=list)
    market_data: str | None = None
    market_data_sha256: str | None = None
    limits: dict[str, Any] = field(default_factory=dict)
    fees: dict[str, float] = field(default_factory=dict)
    instrument: dict[str, float] = field(default_factory=dict)
    # The exchange's clock speed. Event times are match time, which runs this many times faster
    # than the wall clock; two of the exchange's limits are kept on the wall clock all the same.
    speed: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MatchHeader:
        return cls(**data)


def read_match_events(path: str | os.PathLike) -> Iterator[MatchRecord]:
    """Stream the records of a ``match_events.csv`` written by the exchange (or by the simulator)."""
    with open(path, newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None:
            return
        if header[:4] != CSV_HEADER[:4]:
            raise ValueError(f"{path}: not a match events file (header {header!r})")
        for seq, row in enumerate(reader):
            if row:
                yield MatchRecord.from_row(seq, row)


def write_match_events(path: str | os.PathLike, records: Iterable[MatchRecord]) -> int:
    """Write records in the exchange's own CSV layout, so the HUD can replay the file."""
    count = 0
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        for record in records:
            writer.writerow(record.to_row())
            count += 1
    return count
