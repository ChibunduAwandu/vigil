"""What the surveillance engine knows about each participant."""
from __future__ import annotations

import collections
import statistics
from dataclasses import dataclass, field
from typing import Any

from vigil.rtg import MAX_UNHEDGED_LOTS
from vigil.rtg.events import ETF, FUTURE, MatchRecord


@dataclass(slots=True)
class OrderInfo:
    """The life of one competitor order, with the state of the book when it arrived."""

    order_id: int
    seq: int
    entered: float
    side: str  # "A" or "B"
    price: int
    volume: int
    lifespan: str  # "F" or "G"
    reference: float | None = None  # the future's mid when the order arrived: fair value
    best_same: int | None = None  # best price on the order's own side just before it arrived
    best_opposite: int | None = None
    levels_ahead: int = 0  # price levels on its own side that were better than its price
    filled: int = 0
    removed: float | None = None  # when it left the book (filled, cancelled or amended away)
    cancelled_volume: int = 0
    cancel_seq: int | None = None

    @property
    def sign(self) -> int:
        return 1 if self.side == "B" else -1

    @property
    def remaining(self) -> int:
        return self.volume - self.filled - self.cancelled_volume

    def ticks_behind_touch(self, tick: int) -> float | None:
        """How far the order was placed behind the best price on its side (negative: it improved it)."""
        if self.best_same is None:
            return None
        return self.sign * (self.best_same - self.price) / tick

    def was_marketable(self) -> bool:
        if self.best_opposite is None:
            return False
        return self.price >= self.best_opposite if self.side == "B" else self.price <= self.best_opposite


@dataclass
class Participant:
    """Positions, activity and risk figures for one team, kept up to date event by event."""

    name: str
    etf_position: int = 0
    future_position: int = 0
    cash: int = 0  # cents, net of fees
    fees: int = 0  # cents; negative when rebates outweigh fees
    inserts: int = 0
    cancels: int = 0  # cancellations the participant sent
    automatic_cancels: int = 0  # unfilled remainders of fill-and-kill orders, cancelled by the book
    exchange_cancels: int = 0  # orders the exchange cancelled when it cut the participant off
    amends: int = 0
    hedges: int = 0
    trade_reports: int = 0
    message_lots: int = 0  # lots carried by every order message, as the order-to-trade ratio counts them
    executed_lots: int = 0
    hedged_lots: int = 0
    maker_lots: int = 0
    taker_lots: int = 0
    orders: dict[int, OrderInfo] = field(default_factory=dict)
    executed_orders: set[int] = field(default_factory=set)
    first_seen: float | None = None
    last_seen: float = 0.0

    # The exchange's own view of the participant, rebuilt from the stream: enough to tell when it
    # would have cut the participant off, and so whose doing a later cancellation was.
    message_times: collections.deque[float] = field(default_factory=collections.deque)
    last_message: float | None = None
    last_message_seq: int = 0
    messages_at_last: int = 0  # how many messages its window held when the last one arrived
    unhedged_side: int = 0  # +1 long beyond the exchange's allowance, -1 short beyond it, 0 inside
    unhedged_since: float = 0.0
    cut_off: float | None = None  # when the exchange disconnected it, as far as the stream shows
    cut_off_reason: str | None = None

    # Running extremes and exposure clocks, for the report.
    max_abs_etf: int = 0
    max_abs_future: int = 0
    max_abs_unhedged: int = 0
    longest_unhedged: float = 0.0
    time_at_limit: float = 0.0
    _limit_since: float | None = None

    @property
    def unhedged(self) -> int:
        return self.etf_position + self.future_position

    def touch(self, time: float) -> None:
        if self.first_seen is None:
            self.first_seen = time
        self.last_seen = time

    def count_message(self, time: float, window: float, seq: int = 0) -> int:
        """Note a message the participant sent; returns how many its window now holds.

        The window is the exchange's (``FrequencyLimiter``): messages strictly
        less than ``window`` old, the new one included.
        """
        self.drop_old_messages(time, window)
        self.message_times.append(time)
        self.last_message, self.last_message_seq, self.messages_at_last = time, seq, len(self.message_times)
        return len(self.message_times)

    def drop_old_messages(self, time: float, window: float) -> int:
        times = self.message_times
        while times and times[0] <= time - window:
            times.popleft()
        return len(times)

    def apply_execution(self, record: MatchRecord, position_limit: int) -> None:
        """Book a reported trade or hedge into positions and cash."""
        signed = record.volume if record.side == "B" else -record.volume
        value = int(round(record.price * record.volume))
        self.cash -= value if record.side == "B" else -value
        if record.fee:
            self.cash -= record.fee
            self.fees += record.fee
        if record.instrument == FUTURE:
            self.future_position += signed
            self.hedges += 1
            self.hedged_lots += record.volume
        else:
            self.etf_position += signed
            self.trade_reports += 1
            self.executed_lots += record.volume
            self.executed_orders.add(record.order_id)
        self.max_abs_etf = max(self.max_abs_etf, abs(self.etf_position))
        self.max_abs_future = max(self.max_abs_future, abs(self.future_position))
        self.max_abs_unhedged = max(self.max_abs_unhedged, abs(self.unhedged))

        # The exchange's un-hedged clock (unhedged_lots.py): it starts when the net position leaves
        # the allowance on one side and restarts if it crosses straight to the other.
        net = self.unhedged
        side = 1 if net > MAX_UNHEDGED_LOTS else -1 if net < -MAX_UNHEDGED_LOTS else 0
        if side != self.unhedged_side:
            self.unhedged_side, self.unhedged_since = side, record.time

        at_limit = abs(self.etf_position) >= position_limit
        if at_limit and self._limit_since is None:
            self._limit_since = record.time
        elif not at_limit and self._limit_since is not None:
            self.time_at_limit += record.time - self._limit_since
            self._limit_since = None

    def close(self, end_time: float) -> None:
        if self._limit_since is not None:
            self.time_at_limit += end_time - self._limit_since
            self._limit_since = None

    def order_to_trade(self) -> tuple[float, float]:
        """The ratio of unexecuted orders to transactions, by number and by volume.

        As Delegated Regulation (EU) 2017/566 (RTS 9) defines it in Article 3:
        orders over transactions, less one. An order is any message that
        enters, modifies or cancels one (Article 1), counted as the annex
        prescribes: an entry or a cancellation once, a modification twice (it
        withdraws one order and enters another), and an immediate order twice
        if its remainder is cancelled. Cancellations made by the venue when a
        participant loses its connection are left out, as Article 1 says. A
        transaction is an order executed in whole or in part, and volume is
        lots: those each message carried, over those executed.
        """
        by_number = self.order_messages() / max(len(self.executed_orders), 1) - 1.0
        by_volume = self.message_lots / max(self.executed_lots, 1) - 1.0
        return by_number, by_volume

    def order_messages(self) -> int:
        """Order messages, counted as the annex to RTS 9 counts them."""
        return self.inserts + self.cancels + self.automatic_cancels + 2 * self.amends

    def summary(self, mark: tuple[float, float] | None = None) -> dict[str, Any]:
        """Figures for the report. ``mark`` is ``(future price, ETF price)`` in cents."""
        by_number, by_volume = self.order_to_trade()
        lifetimes = [o.removed - o.entered for o in self.orders.values()
                     if o.removed is not None and o.lifespan == "G"]
        out = {
            "orders": self.inserts,
            "cancels": self.cancels,
            "amends": self.amends,
            "unfilled_immediate_orders": self.automatic_cancels,
            "order_messages": self.order_messages(),
            "order_message_lots": self.message_lots,
            "hedges": self.hedges,
            "executed_orders": len(self.executed_orders),
            "executed_lots": self.executed_lots,
            "maker_lots": self.maker_lots,
            "taker_lots": self.taker_lots,
            "hedged_lots": self.hedged_lots,
            "fees": round(self.fees / 100.0, 2),
            "etf_position": self.etf_position,
            "future_position": self.future_position,
            "max_abs_etf_position": self.max_abs_etf,
            "max_abs_future_position": self.max_abs_future,
            "max_abs_unhedged": self.max_abs_unhedged,
            "longest_unhedged_seconds": round(self.longest_unhedged, 3),
            "seconds_at_position_limit": round(self.time_at_limit, 3),
            "order_to_trade_by_number": round(by_number, 2),
            "order_to_trade_by_volume": round(by_volume, 2),
            "cancel_ratio": round(self.cancels / self.inserts, 4) if self.inserts else 0.0,
            "median_order_lifetime": round(statistics.median(lifetimes), 3) if lifetimes else None,
            "cut_off": None if self.cut_off is None else {
                "time": round(self.cut_off, 6), "reason": self.cut_off_reason,
                "orders_cancelled_by_exchange": self.exchange_cancels},
        }
        if mark is not None:
            future_price, etf_price = mark
            value = self.cash + self.future_position * future_price + self.etf_position * etf_price
            out["profit_or_loss"] = round(value / 100.0, 2)
        return out


INSTRUMENT_NAMES = {FUTURE: "future", ETF: "ETF"}
