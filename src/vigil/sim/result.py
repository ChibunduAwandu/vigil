"""What a simulated match produces: the score board and the event log."""
from __future__ import annotations

import csv
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from vigil.rtg import Competitor, CompetitorAccount, MatchEvent, MatchEventOperation
from vigil.rtg.events import MatchRecord, write_match_events

SCORE_BOARD_HEADER = ["Time", "Team", "Operation", "BuyVolume", "SellVolume", "EtfPosition", "FuturePosition",
                      "EtfPrice", "FuturePrice", "TotalFees", "AccountBalance", "ProfitOrLoss", "Status"]


class ScoreRecorder:
    """In-memory stand-in for the exchange's ``ScoreBoardWriter`` (same three entry points)."""

    def __init__(self) -> None:
        self.rows: list[tuple] = []
        self._closed = False

    def close(self) -> None:
        """The match is over: like the exchange's writer, record nothing more."""
        self._closed = True

    def _add(self, now: float, name: str, operation: str, account: CompetitorAccount, etf_price: int | None,
             future_price: int | None, status: str | None) -> None:
        if self._closed:
            return
        self.rows.append((round(now, 6), name, operation, account.buy_volume, account.sell_volume,
                          account.etf_position, account.future_position, etf_price, future_price,
                          account.total_fees, account.account_balance, account.profit_or_loss, status))

    def tick(self, now: float, name: str, account: CompetitorAccount, etf_price: int | None,
             future_price: int | None, status: str | None = None) -> None:
        self._add(now, name, "Tick", account, etf_price, future_price, status)

    def breach(self, now: float, name: str, account: CompetitorAccount, etf_price: int | None,
               future_price: int | None) -> None:
        self._add(now, name, "Breach", account, etf_price, future_price, None)

    def disconnect(self, now: float, name: str, account: CompetitorAccount, etf_price: int | None,
                   future_price: int | None) -> None:
        self._add(now, name, "Disconnect", account, etf_price, future_price, None)

    def write_csv(self, path: str | os.PathLike) -> None:
        """Write the rows in the layout of the exchange's ``score_board.csv``."""
        with open(path, "w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(SCORE_BOARD_HEADER)
            writer.writerows(self.rows)


@dataclass
class TeamResult:
    """How one team fared. Money is in dollars (the exchange keeps cents)."""

    name: str
    status: str  # "OK" or "BREACH"
    pnl: float
    max_drawdown: float
    total_fees: float
    buy_volume: int
    sell_volume: int
    etf_position: int
    future_position: int
    breach_time: float | None = None
    breach_reason: str | None = None
    inserts: int = 0
    cancels: int = 0
    amends: int = 0
    hedges: int = 0
    fills: int = 0
    errors: list[tuple[float, int, str]] = field(default_factory=list)
    marked_from: int = 0  # first score tick at which both instruments had traded (see build_result)
    times: np.ndarray = field(default_factory=lambda: np.empty(0), repr=False)
    pnl_series: np.ndarray = field(default_factory=lambda: np.empty(0), repr=False)
    etf_series: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=int), repr=False)
    future_series: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=int), repr=False)

    @property
    def traded_volume(self) -> int:
        return self.buy_volume + self.sell_volume

    def summary(self) -> dict[str, Any]:
        return {
            "team": self.name, "status": self.status, "pnl": round(self.pnl, 2),
            "max_drawdown": round(self.max_drawdown, 2), "fees": round(self.total_fees, 2),
            "volume": self.traded_volume, "etf_position": self.etf_position,
            "future_position": self.future_position, "inserts": self.inserts, "cancels": self.cancels,
            "hedges": self.hedges, "fills": self.fills, "breach_time": self.breach_time,
            "breach_reason": self.breach_reason,
        }


@dataclass
class MatchResult:
    teams: dict[str, TeamResult]
    duration: float
    ticks: int
    market_events: int
    score: ScoreRecorder = field(repr=False)
    raw_events: list[MatchEvent] = field(default_factory=list, repr=False)

    def records(self) -> Iterator[MatchRecord]:
        """The recorded match events, in stream order."""
        for seq, event in enumerate(self.raw_events):
            yield MatchRecord.from_rtg(seq, event)

    def write_match_events(self, path: str | os.PathLike) -> int:
        return write_match_events(path, self.records())

    def __getitem__(self, team: str) -> TeamResult:
        return self.teams[team]


def build_result(*, teams: list[tuple[str, Competitor, list, list[int]]], score: ScoreRecorder,
                 events: list[MatchEvent], duration: float, ticks: int, market_events: int) -> MatchResult:
    by_team: dict[str, list[tuple]] = {name: [] for name, *_ in teams}
    breaches: dict[str, float] = {}
    for row in score.rows:
        if row[2] == "Tick":
            by_team[row[1]].append(row)
        elif row[2] == "Breach":
            breaches.setdefault(row[1], row[0])

    results: dict[str, TeamResult] = {}
    for name, competitor, errors, count in teams:
        account = competitor.account
        rows = by_team[name]
        breach_time = breaches.get(name)
        # The breach is announced to the trader in the last thing the exchange sends before it hangs
        # up. (Not looked up by time: a resting order filled by the market breaches at the time
        # of the market's order, and the error goes out when the exchange gets round to that.)
        reason = errors[-1][2] if breach_time is not None and errors else None
        # Until an instrument has traded the exchange has no price for it and marks positions in
        # it at zero, so an early position shows up as a wild swing in the running P&L. Risk
        # statistics start from the first tick at which both instruments have a price.
        pnl_series = np.array([r[11] for r in rows], dtype=float) / 100.0
        marked_from = next((i for i, r in enumerate(rows) if r[7] is not None and r[8] is not None), len(rows))
        marked = pnl_series[marked_from:]
        drawdown = float((np.maximum.accumulate(marked) - marked).max()) if len(marked) else 0.0
        results[name] = TeamResult(
            name=name,
            status=competitor.status,
            pnl=account.profit_or_loss / 100.0,
            max_drawdown=drawdown,
            total_fees=account.total_fees / 100.0,
            buy_volume=account.buy_volume,
            sell_volume=account.sell_volume,
            etf_position=account.etf_position,
            future_position=account.future_position,
            breach_time=breach_time,
            breach_reason=reason,
            amends=count[MatchEventOperation.AMEND],
            cancels=count[MatchEventOperation.CANCEL],
            inserts=count[MatchEventOperation.INSERT],
            hedges=count[MatchEventOperation.HEDGE],
            fills=count[MatchEventOperation.TRADE],
            errors=list(errors),
            marked_from=marked_from,
            times=np.array([r[0] for r in rows], dtype=float),
            pnl_series=pnl_series,
            etf_series=np.array([r[5] for r in rows], dtype=int),
            future_series=np.array([r[6] for r in rows], dtype=int),
        )
    return MatchResult(teams=results, duration=duration, ticks=ticks, market_events=market_events, score=score,
                       raw_events=events)
