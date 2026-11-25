"""Rebuilding the market from the order stream.

The exchange's event stream says who inserted, amended and cancelled what, and
which competitor orders traded. It does not say who was on the other side of a
trade, what the book looked like when an order arrived, or what traded between
the replayed market's own orders. All of that is recoverable, because matching
is deterministic: feed the same orders through the same matching engine in the
same sequence and the same trades come out.

``MarketReplica`` does exactly that, with the exchange's own ``OrderBook``. It
is the surveillance engine's independent copy of the market, and the source of
two things the detectors rely on: the state of the book at any instant, and
every execution with both counterparties attached. It also yields, as a free
by-product, a check on the exchange itself: the fills it reports must be the
fills the replay produces, and the price of every hedge must be one the
future's book would have given.
"""
from __future__ import annotations

import collections
from dataclasses import dataclass

from vigil.rtg import TOP_LEVEL_COUNT, Instrument, IOrderListener, Lifespan, Order, OrderBook, Side
from vigil.rtg.events import AMEND, CANCEL, INSERT, MatchRecord

MARKET = ""  # the owner of orders that come from the replayed market data
_FAR_BID, _FAR_ASK = 1, 2 ** 31 - 1  # limit prices that never bind


class TrackedOrder(Order):
    """An order in the replica's books, with who owns it and where it came from."""

    __slots__ = ("entered", "owner", "seq")


@dataclass(frozen=True, slots=True)
class Fill:
    """One execution: a resting order traded against an incoming one."""

    seq: int  # sequence number of the insert that caused it
    time: float
    instrument: int
    price: int
    volume: int
    aggressor: str
    aggressor_order: int
    aggressor_side: str  # "B" if the incoming order was a buy
    passive: str
    passive_order: int
    passive_entered: float  # when the resting order went into the book

    @property
    def buyer(self) -> str:
        return self.aggressor if self.aggressor_side == "B" else self.passive

    @property
    def seller(self) -> str:
        return self.passive if self.aggressor_side == "B" else self.aggressor


@dataclass(frozen=True, slots=True)
class ExpectedReport:
    """A trade the exchange should report for a competitor's order, as the replay predicts it."""

    competitor: str
    order_id: int
    instrument: int
    side: str
    price: int
    volume: int
    fee: int
    seq: int  # of the insert that caused the trade
    time: float

    def is_reported_by(self, record: MatchRecord) -> bool:
        return (self.competitor, self.order_id, self.instrument, self.side, self.price, self.volume, self.fee) == (
            record.competitor, record.order_id, record.instrument, record.side, record.price, record.volume,
            record.fee)


class MarketReplica(IOrderListener):
    """Shadow order books for both instruments, driven by match records."""

    def __init__(self, maker_fee: float = -0.0001, taker_fee: float = 0.0002):
        self.books = (OrderBook(Instrument.FUTURE, 0.0, 0.0), OrderBook(Instrument.ETF, maker_fee, taker_fee))
        # Live orders per instrument, as the exchange keeps them: a cancel or an amend names an
        # order by owner and number alone, and the same number may be in use in both books.
        self._live: tuple[dict[tuple[str, int], TrackedOrder], ...] = ({}, {})
        self.expected: collections.deque[ExpectedReport] = collections.deque()
        self.automatic_cancels: set[tuple[str, int]] = set()  # fill-and-kill remainders the book cancelled
        self.market_volume = [0, 0]  # lots traded per instrument, all participants
        self.ambiguous = 0  # cancels and amends that could have meant either of two orders
        self._aggressor: TrackedOrder | None = None
        self._fills: list[Fill] = []
        self._seq = 0
        self._time = 0.0
        self._levels = ([0] * TOP_LEVEL_COUNT, [0] * TOP_LEVEL_COUNT, [0] * TOP_LEVEL_COUNT, [0] * TOP_LEVEL_COUNT)

    # -- replay --------------------------------------------------------------------------------

    def apply(self, record: MatchRecord) -> list[Fill]:
        """Apply an ``Insert``, ``Amend`` or ``Cancel`` record and return the executions it caused."""
        self._seq, self._time = record.seq, record.time
        if record.op == INSERT:
            order = TrackedOrder(record.order_id, Instrument(record.instrument), Lifespan[record.lifespan],
                                 Side[record.side], int(record.price), record.volume, self)
            order.owner, order.seq, order.entered = record.competitor, record.seq, record.time
            self._aggressor, self._fills = order, []
            self.books[order.instrument].insert(record.time, order)
            self._aggressor = None
            return self._fills
        order = self._find(record)
        if order is not None:
            if record.op == CANCEL:
                self.books[order.instrument].cancel(record.time, order)
            elif record.op == AMEND and record.volume < 0:
                self.books[order.instrument].amend(record.time, order, order.volume + record.volume)
        return []

    def _find(self, record: MatchRecord) -> TrackedOrder | None:
        """The live order a cancel or amend refers to.

        The record gives no instrument. Where the number is live in both
        books, the volume removed says which: a cancel removes all that is
        left of an order, an amend no more than is left.
        """
        key = (record.competitor, record.order_id)
        found = [orders[key] for orders in self._live if key in orders]
        if len(found) < 2:
            return found[0] if found else None
        removed = -record.volume
        if record.op == CANCEL:
            fitting = [order for order in found if order.remaining_volume == removed]
        else:
            fitting = [order for order in found if order.remaining_volume >= removed]
        if len(fitting) != 1:
            self.ambiguous += 1
        return (fitting or found)[0]

    def is_live(self, competitor: str, order_id: int) -> bool:
        key = (competitor, order_id)
        return key in self._live[0] or key in self._live[1]

    # -- IOrderListener: called by the order books ---------------------------------------------
    #
    # What is tracked, and when it stops being tracked, follows the exchange's own bookkeeping
    # (MarketEventsReader and Competitor) line for line, so that a number used twice is resolved
    # here as it is there.

    def on_order_placed(self, now: float, order: TrackedOrder) -> None:
        self._live[order.instrument][(order.owner, order.client_order_id)] = order

    def on_order_cancelled(self, now: float, order: TrackedOrder, volume_removed: int) -> None:
        if self._live[order.instrument].pop((order.owner, order.client_order_id), None) is None:
            # Never rested: this is the book discarding the rest of a fill-and-kill order.
            self.automatic_cancels.add((order.owner, order.client_order_id))

    def on_order_amended(self, now: float, order: TrackedOrder, volume_removed: int) -> None:
        if order.remaining_volume == 0:
            self._live[order.instrument].pop((order.owner, order.client_order_id), None)

    def on_order_filled(self, now: float, order: TrackedOrder, price: int, volume: int, fee: int) -> None:
        if order.owner != MARKET:
            self.expected.append(ExpectedReport(order.owner, order.client_order_id, int(order.instrument),
                                                "AB"[int(order.side)], price, volume, fee, self._seq, self._time))
        if order.remaining_volume == 0:
            self._live[order.instrument].pop((order.owner, order.client_order_id), None)
        if order is self._aggressor:
            return  # the incoming order's own callback: one per price level, already counted below
        aggressor = self._aggressor
        self.market_volume[order.instrument] += volume
        self._fills.append(Fill(
            seq=self._seq, time=self._time, instrument=int(order.instrument), price=price, volume=volume,
            aggressor=aggressor.owner, aggressor_order=aggressor.client_order_id,
            aggressor_side="AB"[int(aggressor.side)], passive=order.owner,
            passive_order=order.client_order_id, passive_entered=order.entered))

    # -- the book, as of now -------------------------------------------------------------------

    def best_bid(self, instrument: int) -> int | None:
        return self.books[instrument].best_bid()

    def best_ask(self, instrument: int) -> int | None:
        return self.books[instrument].best_ask()

    def mid(self, instrument: int = Instrument.FUTURE) -> float | None:
        return self.books[instrument].midpoint_price()

    def last_traded(self, instrument: int) -> int | None:
        return self.books[instrument].last_traded_price()

    def top_levels(self, instrument: int) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
        """The five best ``(price, volume)`` levels: asks first, then bids."""
        ask_prices, ask_volumes, bid_prices, bid_volumes = self._levels
        self.books[instrument].top_levels(ask_prices, ask_volumes, bid_prices, bid_volumes)
        return ([(p, v) for p, v in zip(ask_prices, ask_volumes, strict=True) if p],
                [(p, v) for p, v in zip(bid_prices, bid_volumes, strict=True) if p])

    def hedge_prices(self, side: str, volume: int, reported: int | float | None = None) -> set[int]:
        """The prices at which the exchange could have filled a hedge of ``volume`` lots right now.

        A hedge trades against the future's book without changing it, at the
        average price of the levels its limit price reaches, and is booked for
        the whole volume asked for. The limit is not in the event stream, so
        there is one candidate for each depth the limit might have reached; and
        with nothing at all on the other side the exchange uses the last
        traded price. If ``reported`` is the first candidate, the one for a
        limit that did not bind, the rest are not worked out.
        """
        book = self.books[Instrument.FUTURE]
        selling = side == "A"
        book_side = Side.ASK if selling else Side.BID
        traded, average = book.try_trade(book_side, _FAR_BID if selling else _FAR_ASK, volume)
        if traded and average == reported:
            return {average}
        asks, bids = self.top_levels(Instrument.FUTURE)
        levels = bids if selling else asks
        prices = {average} if traded else set()
        for limit, _ in levels:
            traded, average = book.try_trade(book_side, limit, volume)
            if traded:
                prices.add(average)
        if not levels and book.last_traded_price() is not None:
            prices.add(book.last_traded_price())
        return prices
