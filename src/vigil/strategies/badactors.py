"""Auto-traders that misbehave on purpose.

A surveillance system that has only ever seen well-behaved traders has not
been tested. Each class here commits one specific abuse, as plainly as the
exchange allows, so that there is a known answer to check the detectors
against: this trader should raise this alert, and the honest ones in the same
match should raise none.

They are ordinary Ready Trader Go auto-traders and run on the real exchange
too. None of them is good at making money, which is rather the point.
"""
from __future__ import annotations

import itertools
from collections import deque

from vigil.rtg import MAXIMUM_ASK, MINIMUM_BID, BaseAutoTrader, Instrument, Lifespan, Side

TICK = 100
WALL_LOTS = 1000  # a level at least this large is the standing market


class _Actor(BaseAutoTrader):
    """Shared plumbing: positions from fills, the books as last seen, and a hedger."""

    def __init__(self, loop, team_name: str, secret: str):
        super().__init__(loop, team_name, secret)
        self.ids = itertools.count(1)
        self.etf = 0
        self.future = 0
        self.pending_hedge = 0
        self.sides: dict[int, Side] = {}  # live ETF orders
        self.hedges: dict[int, int] = {}
        self.future_book = None
        self.etf_book = None
        self.seq = 0
        self.max_ask = MAXIMUM_ASK // TICK * TICK
        self.min_bid = (MINIMUM_BID + TICK) // TICK * TICK

    # -- feeds ---------------------------------------------------------------------------------

    def on_order_book_update_message(self, instrument, sequence_number, ask_prices, ask_volumes, bid_prices,
                                     bid_volumes) -> None:
        if instrument == Instrument.FUTURE:
            self.future_book = (ask_prices, ask_volumes, bid_prices, bid_volumes)
            self.seq = sequence_number
        else:
            self.etf_book = (ask_prices, ask_volumes, bid_prices, bid_volumes)
            if self.future_book[0][0] and self.future_book[2][0]:
                self.act()

    def on_order_filled_message(self, client_order_id: int, price: int, volume: int) -> None:
        side = self.sides.get(client_order_id)
        if side is None:
            return
        self.etf += volume if side == Side.BUY else -volume
        self.on_fill(client_order_id, side, price, volume)

    def on_order_status_message(self, client_order_id: int, fill_volume: int, remaining_volume: int,
                                fees: int) -> None:
        if remaining_volume == 0 and self.sides.pop(client_order_id, None) is not None:
            self.on_done(client_order_id)

    def on_hedge_filled_message(self, client_order_id: int, price: int, volume: int) -> None:
        lots = self.hedges.pop(client_order_id, 0)
        self.pending_hedge -= lots
        if volume:
            self.future += volume if lots > 0 else -volume

    def on_error_message(self, client_order_id: int, error_message: bytes) -> None:
        self.sides.pop(client_order_id, None)
        lots = self.hedges.pop(client_order_id, None)
        if lots is not None:
            self.pending_hedge -= lots

    # -- for subclasses ------------------------------------------------------------------------

    def act(self) -> None:
        """Called on every order book update once both books are known."""

    def on_fill(self, order_id: int, side: Side, price: int, volume: int) -> None:
        self.hedge()

    def on_done(self, order_id: int) -> None:
        pass

    # -- helpers -------------------------------------------------------------------------------

    @property
    def now(self) -> float:
        """Seconds into the match: the first order book update is published as the market opens."""
        return (self.seq - 1) * 0.25

    @property
    def fair(self) -> float:
        return (self.future_book[0][0] + self.future_book[2][0]) / 2.0

    def wall(self, side: Side) -> int | None:
        """Price of the standing market on ``side`` of the ETF book, if it is in view."""
        prices, volumes = (self.etf_book[2], self.etf_book[3]) if side == Side.BUY else (self.etf_book[0],
                                                                                        self.etf_book[1])
        return next((p for p, v in zip(prices, volumes, strict=True) if p and v >= WALL_LOTS), None)

    def insert(self, side: Side, price: int, volume: int, lifespan: Lifespan = Lifespan.GOOD_FOR_DAY) -> int:
        order_id = next(self.ids)
        self.sides[order_id] = side
        self.send_insert_order(order_id, side, price, volume, lifespan)
        return order_id

    def cancel(self, order_id: int | None) -> None:
        if order_id is not None and order_id in self.sides:
            self.send_cancel_order(order_id)

    def hedge(self, keep: int = 0) -> None:
        """Trade the future until at most ``keep`` lots are un-hedged."""
        delta = self.etf + self.future + self.pending_hedge
        if abs(delta) <= keep:
            return
        lots = -delta
        order_id = next(self.ids)
        self.hedges[order_id] = lots
        self.pending_hedge += lots
        if lots > 0:
            self.send_hedge_order(order_id, Side.BUY, self.max_ask, lots)
        else:
            self.send_hedge_order(order_id, Side.SELL, self.min_bid, -lots)


class Spoofer(_Actor):
    """Shows size it does not mean to trade, to colour the book while it works a real order.

    Each cycle it offers a small genuine order close to fair value on one side,
    where it will trade soon, and rests a large order on the other side a tick
    behind the standing market: in plain view, and with no prospect of trading.
    When the genuine order trades (or a few seconds pass) the large order is
    pulled; after a pause the two sides swap.
    """

    GENUINE_LOTS = 10
    SHOWN_LOTS = 60
    LAYERS = 1  # price levels the shown size is spread over
    PATIENCE = 16  # book updates before giving up on a cycle
    PAUSE = 4  # book updates between cycles

    def __init__(self, loop, team_name: str, secret: str):
        super().__init__(loop, team_name, secret)
        self.genuine: int | None = None
        self.shown: list[int] = []
        self.genuine_side = Side.SELL
        self.shown_price = 0
        self.started = 0
        self.resume = 8

    def act(self) -> None:
        if self.genuine is None and not self.shown:
            if self.seq >= self.resume and self.now < 880:
                self.begin()
        elif self.seq - self.started >= self.PATIENCE or self.exposed():
            self.end()

    def exposed(self) -> bool:
        """Has the standing market moved away and left the shown order where it would trade?"""
        if not self.shown:
            return False
        if self.genuine_side == Side.SELL:  # size is shown on the bid
            return self.etf_book[2][0] <= self.shown_price
        return self.etf_book[0][0] >= self.shown_price

    def begin(self) -> None:
        side = self.genuine_side
        other = Side.BUY if side == Side.SELL else Side.SELL
        wall = self.wall(other)
        prices = self.etf_book[2] if other == Side.BUY else self.etf_book[0]
        if wall is None or prices.index(wall) + self.LAYERS > 4:
            return  # the shown orders would fall outside the five levels the exchange publishes
        room = 100 - self.etf if other == Side.BUY else 100 + self.etf
        each = min(self.SHOWN_LOTS, room - 10) // self.LAYERS
        capacity = 100 + self.etf if side == Side.SELL else 100 - self.etf
        if each < 10 or capacity < self.GENUINE_LOTS:
            self.genuine_side = other
            return
        fair = self.fair
        if side == Side.SELL:
            price = int(fair // TICK) * TICK + TICK
            if price <= self.etf_book[2][0]:
                return  # would trade on arrival; wait for a quieter book
        else:
            price = int(-(-fair // TICK)) * TICK - TICK
            if self.etf_book[0][0] and price >= self.etf_book[0][0]:
                return
        self.genuine = self.insert(side, price, self.GENUINE_LOTS)
        outward = -TICK if other == Side.BUY else TICK
        self.shown_price = wall + outward
        self.shown = [self.insert(other, wall + outward * (layer + 1), each) for layer in range(self.LAYERS)]
        self.started = self.seq

    def end(self) -> None:
        for order_id in self.shown:
            self.cancel(order_id)
        self.cancel(self.genuine)
        self.shown, self.genuine = [], None
        self.genuine_side = Side.BUY if self.genuine_side == Side.SELL else Side.SELL
        self.resume = self.seq + self.PAUSE

    def on_fill(self, order_id: int, side: Side, price: int, volume: int) -> None:
        self.hedge()
        if order_id == self.genuine:
            self.end()  # the real order has traded: the show is over


class Layerer(Spoofer):
    """The same deception, with the shown size stacked over several price levels."""

    SHOWN_LOTS = 75
    LAYERS = 3


class WashTrader(_Actor):
    """One half of a pair that trades with itself to manufacture volume.

    The two halves take turns: one rests a bid at fair value, the other sells
    into it on the next book update, then they swap. Lots go round in a circle,
    prices match, and neither ends up with a position. Create the pair with
    ``wash_pair()``.
    """

    LOTS = 10
    PERIOD = 8  # book updates per leg

    def __init__(self, loop, team_name: str, secret: str, first: bool = True):
        super().__init__(loop, team_name, secret)
        self.first = first
        self.bid: int | None = None
        self.agreed = 0  # the price both halves derive from the previous update's fair value

    def act(self) -> None:
        agreed, self.agreed = self.agreed, int(self.fair // TICK) * TICK
        if self.seq < 8 or self.now > 885:
            self.cancel(self.bid)
            self.hedge()
            return
        phase = self.seq % self.PERIOD
        buying = ((self.seq // self.PERIOD) % 2 == 0) == self.first
        if buying:
            if phase == 0 and self.bid is None and self.etf + self.LOTS <= 100:
                best_ask = self.etf_book[0][0]
                if not best_ask or self.agreed < best_ask:  # never pay up to a resting offer
                    self.bid = self.insert(Side.BUY, self.agreed, self.LOTS)
            elif phase >= 3:
                self.cancel(self.bid)
        elif phase == 1 and self.etf - self.LOTS >= -100:
            # Sell into the partner's bid, which is the best bid if it stands at the agreed price.
            if self.etf_book[2][0] == agreed and self.etf_book[3][0] >= self.LOTS:
                self.insert(Side.SELL, agreed, self.LOTS, Lifespan.FILL_AND_KILL)
        self.hedge(keep=10)

    def on_fill(self, order_id: int, side: Side, price: int, volume: int) -> None:
        self.hedge(keep=10)

    def on_done(self, order_id: int) -> None:
        if order_id == self.bid:
            self.bid = None


def wash_pair() -> tuple:
    """Factories for the two halves of a wash-trading pair."""
    def first(loop, name, secret):
        return WashTrader(loop, name, secret, first=True)

    def second(loop, name, secret):
        return WashTrader(loop, name, secret, first=False)
    return first, second


class QuoteStuffer(_Actor):
    """Floods the book with orders it cancels a moment later, staying just under the message limit.

    The exchange allows fifty messages in any one second and disconnects the
    trader that sends a fifty-first. Its books come out four times a second,
    each up to a fifth of an interval early or late, so five updates can fall
    inside one second: ten messages on every update would come to fifty, and a
    single hedge on top would end the match. So the trader keeps count.
    """

    PER_UPDATE = 5
    LOTS = 1
    LIMIT = 49     # messages it lets itself send in any WINDOW, of the fifty the exchange allows in a second
    SPARE = 3      # of those, kept back for hedges
    WINDOW = 1.1   # seconds: longer than the exchange's, since messages take a varying time to reach it

    def __init__(self, loop, team_name: str, secret: str):
        super().__init__(loop, team_name, secret)
        self.flashed: list[int] = []
        self.sent: deque[float] = deque()

    def send_message(self, typ: int, data: bytes, length: int) -> None:
        self.sent.append(self.event_loop.time())
        super().send_message(typ, data, length)

    def room(self) -> int:
        """Messages it can still send now."""
        horizon = self.event_loop.time() - self.WINDOW
        while self.sent and self.sent[0] <= horizon:
            self.sent.popleft()
        return self.LIMIT - len(self.sent)

    def act(self) -> None:
        if self.room() > 0:
            self.hedge()  # one that had to wait
        room = self.room() - self.SPARE
        resting = [order_id for order_id in self.flashed if order_id in self.sides]
        for order_id in resting[:max(room, 0)]:
            self.cancel(order_id)
        self.flashed = resting[max(room, 0):]
        room -= len(resting) - len(self.flashed)
        wall = self.wall(Side.BUY)
        if wall is None or self.seq < 8:
            return
        # Priced behind the standing market: visible, and with no prospect of trading.
        self.flashed += [self.insert(Side.BUY, wall - TICK * (k + 1), self.LOTS)
                         for k in range(min(self.PER_UPDATE, room))]

    def on_fill(self, order_id: int, side: Side, price: int, volume: int) -> None:
        if self.room() > 0:
            self.hedge()


class CloseMarker(_Actor):
    """Builds a long position, then pays up in the last seconds to lift the closing price.

    The exchange values a position at the last traded price. Buying through
    the offers until one well above fair value has traded prints a high last
    price for the cost of a few lots, and the whole position is marked at it.
    """

    TARGET = 40
    CLOSING_SECONDS = 4.0
    SESSION = 900.0

    def __init__(self, loop, team_name: str, secret: str):
        super().__init__(loop, team_name, secret)
        self.bid: int | None = None
        self.bid_price = 0
        self.lift: int | None = None

    def act(self) -> None:
        if self.now >= self.SESSION - self.CLOSING_SECONDS:
            self.cancel(self.bid)
            if self.lift is not None or self.bid is not None:
                return  # one order at a time, so the position limit cannot be overrun
            # The cheapest print that is clearly above fair value: everything offered below the
            # first such price, and one lot at it.
            lots = 0
            for price, volume in zip(self.etf_book[0], self.etf_book[1], strict=True):
                if not price:
                    break
                if price >= self.fair + 2 * TICK:
                    if lots + 1 <= 100 - self.etf:
                        self.lift = self.insert(Side.BUY, price, lots + 1, Lifespan.FILL_AND_KILL)
                    break
                lots += volume
            return
        wall = self.wall(Side.BUY)
        if wall is None:
            return
        price = wall + TICK
        if self.bid is not None and price != self.bid_price:
            self.cancel(self.bid)
        elif self.bid is None and self.etf < self.TARGET:
            self.bid_price = price
            self.bid = self.insert(Side.BUY, price, min(20, self.TARGET - self.etf))

    def on_done(self, order_id: int) -> None:
        if order_id == self.bid:
            self.bid = None
        elif order_id == self.lift:
            self.lift = None


class Reckless(_Actor):
    """Takes a position and never hedges it, until the exchange throws it out."""

    LOTS = 30
    START = 40  # book update on which it buys

    def __init__(self, loop, team_name: str, secret: str):
        super().__init__(loop, team_name, secret)
        self.done = False

    def act(self) -> None:
        offer = self.wall(Side.SELL)
        if not self.done and self.seq >= self.START and offer:
            self.done = True
            self.insert(Side.BUY, offer, self.LOTS, Lifespan.FILL_AND_KILL)

    def on_fill(self, order_id: int, side: Side, price: int, volume: int) -> None:
        pass  # no hedge: that is the offence


class LimitBreaker(Reckless):
    """Buys more than the position limit allows in a single update."""

    LOTS = 60

    def act(self) -> None:
        offer = self.wall(Side.SELL)
        if not self.done and self.seq >= self.START and offer:
            self.done = True
            for _ in range(2):
                self.insert(Side.BUY, offer, self.LOTS, Lifespan.FILL_AND_KILL)

    def on_fill(self, order_id: int, side: Side, price: int, volume: int) -> None:
        self.hedge()


class FatFinger(_Actor):
    """An otherwise idle trader that once enters an order at a wildly wrong price."""

    AT = 400  # book update
    ERROR = 0.05

    def __init__(self, loop, team_name: str, secret: str):
        super().__init__(loop, team_name, secret)
        self.done = False

    def act(self) -> None:
        if not self.done and self.seq >= self.AT:
            self.done = True
            price = int(self.fair * (1 + self.ERROR)) // TICK * TICK  # a buy, five per cent above the market
            self.insert(Side.BUY, price, 5)


BAD_ACTORS = {
    "spoofer": Spoofer,
    "layerer": Layerer,
    "stuffer": QuoteStuffer,
    "close_marker": CloseMarker,
    "reckless": Reckless,
    "limit_breaker": LimitBreaker,
    "fat_finger": FatFinger,
}
