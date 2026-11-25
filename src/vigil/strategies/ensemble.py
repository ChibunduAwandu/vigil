"""The Vigil auto-trader: market making with mean-reversion, momentum and Kelly-sized inventory.

How money is made on Ready Trader Go
------------------------------------
The ETF's book holds a wide standing market a few ticks either side of the
future's price, and a stream of customer orders priced at random around it.
A customer order that crosses the standing market trades with whoever shows
the best price, so the carrier strategy is a market maker that shows a better
price than the standing market and lays each fill off in the future.

Four things are layered on that carrier, each of which can be switched off so
its contribution can be measured (see ``PRESETS``):

* **Mean reversion.** Customer orders that do not cross rest in the book at
  prices unrelated to fair value. The ETF converges to the future, so a bid
  resting above the future's offer (or an offer below its bid) by more than
  costs plus a volatility margin is taken, and the basis is locked in with a
  hedge.
* **Kelly sizing.** Hedging costs half the future's spread, and inventory
  left un-hedged is often taken away again by the next customer for nothing.
  How much un-hedged inventory to carry is a bet with an edge (the saved
  spread) and a variance; the Kelly criterion sizes it, and the exchange's
  un-hedged-lots rule caps it.
* **Momentum.** The future's order book leans before its price moves. A
  forecast of the next move, from book pressure alone ("momentum"), decides
  when a hedge that is due is sent: if the price is about to move the
  inventory's way, the hedge waits an update.
* **AdaBoost.** Replaces the book-pressure rule with the boosted model's
  calibrated probability that the future ticks up.

Speed
-----
Several traders answer each order book update and the exchange serves them
in the order they arrive, so the first to arrive gets the customer. On the
real exchange a few tens of microseconds in the handler decide that. The
handler is therefore ordered by urgency: the orders that race other traders
(takes and quotes) are worked out from the books alone and sent first, and
only then are the features updated, the forecast made and the hedge decided.
Nothing races for a hedge: it trades against the future's book without
taking anything from it.

Safety
------
Position limits are enforced by arithmetic, not by hope: an order is only
sent if it cannot breach a limit even when every other live order fills
first, and capacity held by an order being cancelled is only reused once the
exchange has confirmed the cancel.
"""
from __future__ import annotations

import collections
import itertools
import json
import math
import os
from dataclasses import asdict, dataclass, fields, replace
from typing import Any

import numpy as np

from vigil.ml.features import FEATURE_INDEX, FeatureState
from vigil.ml.model import DEFAULT_MODEL_PATH, AlphaBundle, AlphaModel
from vigil.rtg import MAXIMUM_ASK, MINIMUM_BID, BaseAutoTrader, Instrument, Lifespan, Side
from vigil.strategies.inventory import flat_values, inventory_values
from vigil.strategies.kelly import hedge_band, hedge_target

_MICRO = FEATURE_INDEX["micro"]
_RET1 = FEATURE_INDEX["ret1"]


@dataclass(frozen=True)
class StrategyConfig:
    # -- the exchange's rules (exchange.json and unhedged_lots.py) -----------------------------
    position_limit: int = 100
    tick: int = 100  # cents
    maker_fee: float = -0.0001
    taker_fee: float = 0.0002
    free_unhedged_lots: int = 10  # may be held un-hedged indefinitely
    unhedged_time_limit: float = 60.0  # seconds allowed beyond that
    message_limit: int = 50  # per message interval
    message_interval: float = 1.0  # seconds, at the exchange's normal speed
    book_interval: float = 0.25  # seconds of match time between order book updates
    session_seconds: float = 900.0
    speed: float = 1.0  # the exchange's "Speed" setting: match time runs this many times faster than the wall clock

    # -- market making --------------------------------------------------------------------------
    quote: bool = True
    quote_lots: int = 100  # most lots shown per side
    margin_ticks: float = 1.0  # a quote must clear the hedge price by at least this
    wall_lots: int = 1000  # a level this large is the standing market: quote just inside it
    default_offset_ticks: int = 5  # where to quote if no standing market is visible
    customer_range_ticks: float = 32.0  # customer limits reach this far through fair value
    customer_mean_lots: float = 46.0  # average customer order
    customer_edge_ticks: float = 4.0  # what such a fill earns per lot, net of its hedge

    # -- inventory ----------------------------------------------------------------------------------
    inventory_control: bool = True  # price the position limit (see strategies/inventory.py)

    # -- mean reversion ---------------------------------------------------------------------------
    mean_reversion: bool = True
    entry_z: float = 0.5  # extra edge demanded, in standard deviations of a one-update move

    # -- momentum / forecast: when to send a hedge that is due ------------------------------------
    alpha: str = "adaboost"  # "none", "momentum" (book pressure) or "adaboost"
    alpha_weight: float = 1.0
    defer_ticks: float = 0.05  # a hedge waits while the forecast favours the inventory by this much

    # -- hedging ------------------------------------------------------------------------------------
    hedging: str = "kelly"  # "immediate", "band" (fixed at the free allowance) or "kelly"
    kelly_fraction: float = 0.5
    bankroll: float = 1000.0  # dollars of risk capital at the open; profits are added as they come
    offset_probability: float = 0.8  # chance that customer flow removes a carried lot before we must hedge it
    carry_horizon: float = 10.0  # seconds a carried lot is typically held: the horizon of the bet's risk
    carry_lots: int = 100  # most un-hedged lots ever carried, whatever Kelly says
    carry_seconds: float = 30.0  # how long inventory above the free allowance may be carried

    # -- end of session -----------------------------------------------------------------------------
    flatten_seconds: float = 15.0  # stop carrying inventory this long before the close

    # -- model ------------------------------------------------------------------------------------
    model_path: str | None = None
    holdout: str | None = None  # key of the session being traded, so its fold model is used

    # -- experimental versions (strategies/challengers/) ------------------------------------------
    engine: str = "ensemble"  # the trader that runs: "ensemble" is base Vigil; otherwise a challenger's name
    challenger_path: str | None = None  # their fitted parameters (default models/challengers.json)
    tuning: float | None = None  # a challenger's one tuning knob, if not the value fitted for it

    def with_(self, **changes: Any) -> StrategyConfig:
        return replace(self, **changes)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StrategyConfig:
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown strategy settings: {sorted(unknown)}")
        return cls(**data)


_BASELINE = StrategyConfig(inventory_control=False, mean_reversion=False, alpha="none", hedging="immediate")

# Each preset adds one component to the one before it, so differences in P&L are attributable.
PRESETS: dict[str, StrategyConfig] = {
    "baseline": _BASELINE,
    "inventory": _BASELINE.with_(inventory_control=True),
    "mean_reversion": _BASELINE.with_(inventory_control=True, mean_reversion=True),
    "kelly": _BASELINE.with_(inventory_control=True, mean_reversion=True, hedging="kelly"),
    "momentum": _BASELINE.with_(inventory_control=True, mean_reversion=True, hedging="kelly", alpha="momentum"),
    "full": StrategyConfig(),
}


class _Order:
    """One of our live ETF orders, as last confirmed by the exchange."""

    __slots__ = ("cancelling", "fees", "is_quote", "order_id", "price", "remaining", "side")

    def __init__(self, order_id: int, side: Side, price: int, volume: int, is_quote: bool):
        self.order_id = order_id
        self.side = side
        self.price = price
        self.remaining = volume
        self.is_quote = is_quote
        self.cancelling = False
        self.fees = 0


class EnsembleTrader(BaseAutoTrader):
    """See the module docstring."""

    def __init__(self, loop, team_name: str, secret: str, config: StrategyConfig | None = None,
                 model: AlphaModel | None = None):
        super().__init__(loop, team_name, secret)
        self.config = config = config or StrategyConfig()
        self._ids = itertools.count(1)
        self._min_bid = (MINIMUM_BID + config.tick) // config.tick * config.tick
        self._max_ask = MAXIMUM_ASK // config.tick * config.tick

        # Positions and cash, from fills.
        self.etf_position = 0
        self.future_position = 0
        self.cash = 0  # cents
        self._orders: dict[int, _Order] = {}
        self._quotes: dict[Side, _Order | None] = {Side.BUY: None, Side.SELL: None}
        self._hedges: dict[int, int] = {}  # hedge order id -> signed lots in flight
        self._hedge_pending = 0
        self._hedge_failed = -1  # the book update during which a hedge last came back unfilled

        # Market state.
        self._future: tuple | None = None
        self._etf: tuple | None = None
        self._features = FeatureState(config.tick)
        self._forecast = 0.0  # expected mid move over the next update, ticks
        self._variance = 1.0  # of that move, ticks squared
        self._seq = 0
        self._unlearned = False  # the latest future book has not been through the features yet

        # Intentions, recomputed on every ETF update and worked off as the exchange confirms.
        self._want_take: dict[Side, tuple[int, int] | None] = {Side.BUY: None, Side.SELL: None}
        self._want_quote: dict[Side, tuple[int, int] | None] = {Side.BUY: None, Side.SELL: None}

        self._sent: collections.deque[float] = collections.deque()
        self._over_since: float | None = None  # when un-hedged inventory last left the free allowance
        self._held = (0.0, 0.0)  # the band of un-hedged inventory, as the last hedging decision left it

        self._model = model
        if config.alpha in ("adaboost", "momentum") and model is None:
            path = config.model_path or os.environ.get("VIGIL_MODEL") or DEFAULT_MODEL_PATH
            if config.alpha == "adaboost" or os.path.exists(path):
                bundle = AlphaBundle.load(path)
                self._model = bundle.for_session(config.holdout)
                if self._model is bundle.full and bundle.folds:
                    # Fine on data the model has never seen. On one of the sessions it was fitted
                    # to, this is the model that has seen the answers: name the session (holdout).
                    self.logger.warning("trading with the model fitted on every sample session%s",
                                        "" if config.holdout is None else f" (no fold for session {config.holdout})")
        # Expected move per tick of microprice displacement; measured alongside the model.
        self._momentum_scale = self._model.momentum_scale if self._model is not None else 0.3

        if config.inventory_control:
            self._values = inventory_values(config.position_limit, config.customer_mean_lots,
                                            config.customer_edge_ticks * config.tick / 100.0)
        else:
            self._values = flat_values(config.position_limit)
        # The most a single lot's worth of position can be worth, in cents: no lot is taken at a loss
        # greater than this, whatever it does for the inventory.
        self._lot_value_cap = 100.0 * float(np.abs(np.diff(self._values)).max())
        # The table in cents as a plain list (indexing it is on the hot path), and whether each
        # further lot of inventory is worth less than the last, which lets a search stop early.
        self._value_cents = [100.0 * float(value) for value in self._values]
        self._concave = bool((np.diff(self._values, 2) <= 1e-9).all())

        # Diagnostics, read by the backtester.
        self.stats = collections.Counter()

    # == information feed =========================================================================

    def on_order_book_update_message(self, instrument: int, sequence_number: int, ask_prices, ask_volumes,
                                     bid_prices, bid_volumes) -> None:
        if instrument == Instrument.FUTURE:
            # Kept, not yet studied: the ETF's book follows at once, and then there are orders to
            # get out before anyone else's.
            self._future = (ask_prices, ask_volumes, bid_prices, bid_volumes)
            self._seq = sequence_number
            self._unlearned = True
            self._future_book()
        else:
            self._etf = (ask_prices, ask_volumes, bid_prices, bid_volumes)
            self._on_tick()

    def _future_book(self) -> None:
        """What to work out from the future's book in the moment before the ETF's arrives. Nothing, here."""

    def _learn(self) -> None:
        """Bring the volatility estimate and the forecast up to date with the latest future book."""
        if not self._unlearned:
            return
        self._unlearned = False
        vector = self._features.update(*self._future)
        if vector is not None:
            self._variance += (vector[_RET1] ** 2 - self._variance) / 40.0
            self._forecast = self.config.alpha_weight * self._predict(vector)
        else:
            self._forecast = 0.0

    def on_trade_ticks_message(self, instrument: int, sequence_number: int, ask_prices, ask_volumes, bid_prices,
                               bid_volumes) -> None:
        if instrument == Instrument.FUTURE:
            self._features.add_trades(ask_prices, ask_volumes, bid_prices, bid_volumes)

    def _predict(self, vector) -> float:
        mode = self.config.alpha
        if mode == "adaboost":
            return self._model.expected_move(vector)
        if mode == "momentum":
            # Book pressure: the size-weighted touch price sits towards the side about to give way.
            return self._momentum_scale * float(vector[_MICRO])
        return 0.0

    # == execution feed ===========================================================================

    def on_order_filled_message(self, client_order_id: int, price: int, volume: int) -> None:
        order = self._orders.get(client_order_id)
        if order is None:
            return
        if order.side == Side.BUY:
            self.etf_position += volume
            self.cash -= price * volume
        else:
            self.etf_position -= volume
            self.cash += price * volume
        self.stats["quote_lots" if order.is_quote else "take_lots"] += volume
        self._hedge()

    def on_order_status_message(self, client_order_id: int, fill_volume: int, remaining_volume: int,
                                fees: int) -> None:
        order = self._orders.get(client_order_id)
        if order is None:
            return
        self.cash -= fees - order.fees
        order.fees = fees
        order.remaining = remaining_volume
        if remaining_volume == 0:
            self._retire(order)
            self._work()

    def on_hedge_filled_message(self, client_order_id: int, price: int, volume: int) -> None:
        lots = self._hedges.pop(client_order_id, 0)
        self._hedge_pending -= lots
        if volume > 0:
            signed = volume if lots > 0 else -volume
            self.future_position += signed
            self.cash -= price * signed
            self.stats["hedge_lots"] += volume
        elif lots:
            # Nothing to trade against. Asking again at once would get the same answer and use up
            # the message allowance; the next order book update will bring us back.
            self._hedge_failed = self._seq
            self.stats["hedges_unfilled"] += 1
            return
        self._hedge()

    def on_error_message(self, client_order_id: int, error_message: bytes) -> None:
        self.logger.warning("error with order %d: %s", client_order_id, error_message.decode(errors="replace"))
        self.stats["errors"] += 1
        order = self._orders.get(client_order_id)
        if order is not None:
            self._retire(order)
        lots = self._hedges.pop(client_order_id, None)
        if lots is not None:
            self._hedge_pending -= lots

    def _retire(self, order: _Order) -> None:
        self._orders.pop(order.order_id, None)
        if self._quotes[order.side] is order:
            self._quotes[order.side] = None

    # == decisions ================================================================================

    def _on_tick(self) -> None:
        """Runs once per order book update, when both books are fresh."""
        future, etf = self._future, self._etf
        if future is None:
            return
        if future[0][0] == 0 or future[2][0] == 0:
            # No two-sided future, so no fair value to quote or take against. Inventory is still
            # hedged: the exchange fills a hedge from whichever side of the future there is.
            self._want_take = {Side.BUY: None, Side.SELL: None}
            self._want_quote = {Side.BUY: None, Side.SELL: None}
            self._work()
            self._learn()
            self._hedge()
            return

        future_ask, future_bid = future[0][0], future[2][0]
        mid = (future_ask + future_bid) / 2.0
        asks = self._others(etf[0], etf[1], Side.SELL)
        bids = self._others(etf[2], etf[3], Side.BUY)
        self._trade(asks, bids, future_ask, future_bid, mid)

    def _trade(self, asks: list[tuple[int, int]], bids: list[tuple[int, int]], future_ask: int, future_bid: int,
               mid: float) -> None:
        """The decisions of one update, in order of urgency. The experimental versions reorder them."""
        # First the orders that race: what to take and where to quote, from the books alone.
        self._decide_takes(asks, bids, future_ask, future_bid, mid)
        self._decide_quotes(asks, bids, future_ask, future_bid, mid)
        self._work()

        # Those are on their way. Now there is time to think: the features and the forecast, which
        # only the hedge needs, and nobody races for a hedge.
        self._learn()
        self._hedge()

    def _decide_takes(self, asks: list[tuple[int, int]], bids: list[tuple[int, int]], future_ask: int,
                      future_bid: int, mid: float) -> None:
        """Mean reversion: take resting orders that are rich or cheap to the future."""
        cfg = self.config
        # Un-hedged inventory as it will stand once the hedge decided below has gone out. (The band
        # is the one the last hedging decision used; it moves slowly, and working it out afresh
        # is part of that decision, not of this one.)
        delta = int(hedge_target(self.etf_position + self.future_position + self._hedge_pending, *self._held))
        margin = cfg.entry_z * math.sqrt(self._variance) * cfg.tick
        self._want_take[Side.SELL] = self._want_take[Side.BUY] = None
        if cfg.mean_reversion:
            # Selling ETF is hedged by buying the future at its offer. Lots that merely cancel
            # inventory we already carry need no hedge, so for those the mid is the yardstick.
            self._want_take[Side.SELL] = self._find_take(bids, +1, future_ask, mid, max(delta, 0), margin)
            self._want_take[Side.BUY] = self._find_take(asks, -1, future_bid, mid, max(-delta, 0), margin)

    def _decide_quotes(self, asks: list[tuple[int, int]], bids: list[tuple[int, int]], future_ask: float,
                       future_bid: float, mid: float) -> None:
        """Market making: one quote a side, priced for expected profit."""
        cfg = self.config
        tick = cfg.tick
        closing = self._match_time() >= cfg.session_seconds - cfg.flatten_seconds
        self._want_quote[Side.SELL] = self._want_quote[Side.BUY] = None
        if cfg.quote:
            floor = self._quote_margin() * tick
            # Inventory has a price (see strategies/inventory.py): a fill that takes the position
            # towards zero is worth that much more than its edge, one that adds to it that much less.
            sell_bonus = self._inventory_bonus(-1)
            buy_bonus = self._inventory_bonus(+1)
            ask_floor = int(math.ceil((future_ask + floor - sell_bonus) / tick)) * tick
            bid_cap = int(math.floor((future_bid - floor + buy_bonus) / tick)) * tick
            if not (closing and self.etf_position <= 0):  # near the close, only quote to flatten
                self._want_quote[Side.SELL] = self._price_quote(asks, +1, ask_floor, mid, future_ask, sell_bonus)
            if not (closing and self.etf_position >= 0):
                self._want_quote[Side.BUY] = self._price_quote(bids, -1, bid_cap, mid, future_bid, buy_bonus)

    def _others(self, prices, volumes, side: Side) -> list[tuple[int, int]]:
        """A side of the ETF book with our own resting volume taken out."""
        mine: dict[int, int] = {}
        for order in self._orders.values():
            if order.side == side:
                mine[order.price] = mine.get(order.price, 0) + order.remaining
        levels = []
        for price, volume in zip(prices, volumes, strict=True):
            if price == 0:
                break
            volume -= mine.get(price, 0)
            if volume > 0:
                levels.append((price, volume))
        return levels

    def _inventory_bonus(self, direction: int) -> float:
        """Value in cents per lot of a typical fill that moves the ETF position in ``direction``."""
        limit = self.config.position_limit
        position = self.etf_position
        lots = int(min(self.config.customer_mean_lots, limit - direction * position))
        if lots <= 0:
            return 0.0
        values = self._value_cents
        return (values[position + direction * lots + limit] - values[position + limit]) / lots

    def _find_take(self, levels: list[tuple[int, int]], sign: int, hedge_price: float, mid: float, offset: int,
                   margin: float) -> tuple[int, int] | None:
        """The (limit price, volume) of a fill-and-kill order that sweeps what is worth taking.

        ``sign`` is +1 when we would sell (``levels`` are bids), -1 when we would
        buy. Each lot on offer is scored by its edge over the price it can be
        hedged at (or over the mid, for lots that merely cancel inventory we
        already carry un-hedged), less the margin; how many to take is then
        settled jointly with what the change in position is worth.
        """
        cfg = self.config
        limit = cfg.position_limit
        direction = -sign  # selling takes the ETF position down
        room = limit - direction * self.etf_position  # lots before the position limit, going that way
        values, at = self._value_cents, self.etf_position + limit
        concave, cap = self._concave, self._lot_value_cap
        worth = best_worth = 0.0
        taken = best = best_price = 0
        # The same answer as inventory.best_block over every lot on offer, found one lot at a time
        # so that on the usual update, when nothing is worth taking, the search ends at the first.
        for price, volume in levels:
            fee = cfg.taker_fee * price
            hedged = sign * (price - hedge_price) - fee - margin
            carried = sign * (price - mid) - fee - margin
            if (carried if taken < offset else hedged) < -cap:
                break  # not worth a lot at any inventory, and the levels beyond only get worse
            for _ in range(volume):
                if taken >= room:
                    return (best_price, best) if best else None
                gain = (carried if taken < offset else hedged) + values[at + direction] - values[at]
                if gain <= 0.0 and concave:
                    # Each further lot is worth no more than this one: the best has been passed.
                    return (best_price, best) if best else None
                worth += gain
                taken += 1
                at += direction
                if worth > best_worth:
                    best_worth, best, best_price = worth, taken, price
        return (best_price, best) if best else None

    def _price_quote(self, levels: list[tuple[int, int]], sign: int, floor: int, fair: float,
                     hedge_price: float, bonus: float) -> tuple[int, int] | None:
        """Choose a quote price by expected profit. ``sign`` is +1 for our ask, -1 for our bid.

        A customer order reaches a price ``d`` ticks through fair value with
        probability proportional to ``(range - d)``, and fills us only for the
        part of it that the volume queued ahead of us leaves over. Widening the
        quote earns more per lot and fills less often; the best trade-off is
        found by scoring every price from the tightest we accept to just inside
        the standing market. ``bonus`` is what the trade is worth beyond its
        edge, in cents per lot: the value of the capacity it frees.
        """
        cfg = self.config
        tick = cfg.tick
        wall = None
        for level, volume in levels:
            if volume >= cfg.wall_lots:
                wall = level
                break
        if wall is not None:
            if sign * (floor - wall) >= 0:
                return None  # nothing to earn inside the standing market
            widest = wall - sign * tick
        else:
            widest = int(round(fair / tick)) * tick + sign * cfg.default_offset_ticks * tick
        if sign * (widest - floor) < 0:
            widest = floor

        mean, reach_of, maker = cfg.customer_mean_lots, cfg.customer_range_ticks, cfg.maker_fee
        own = self._quotes[Side.SELL if sign > 0 else Side.BUY]
        resting_at = own.price if own is not None else None
        best, best_value = None, 0.0
        price, step = floor, sign * tick
        # ``levels`` runs from the best price outwards and so does the search, so the volume at
        # better prices than ours is kept as a running total rather than added up for every price.
        better, nearest, count = 0, 0, len(levels)
        while sign * (widest - price) >= 0:
            while nearest < count and sign * (price - levels[nearest][0]) > 0:
                better += levels[nearest][1]
                nearest += 1
            ahead = better
            if nearest < count and levels[nearest][0] == price and resting_at != price:
                ahead += levels[nearest][1]  # we would join the back of the queue at that price
            edge = sign * (price - hedge_price) - maker * price + bonus
            if edge > 0:
                reach = reach_of - sign * (price - fair) / tick
                value = reach * math.exp(-ahead / mean) * edge if reach > 0.0 else 0.0
                if value >= best_value:  # ties go to the wider price
                    best, best_value = price, value
            price += step
        if best is None or best <= 0:
            return None
        return best, self._quote_size()

    def _quote_margin(self) -> float:
        """Ticks a quote must clear its hedge price by."""
        return self.config.margin_ticks

    def _quote_size(self) -> int:
        """Most lots a quote shows."""
        return self.config.quote_lots

    # -- hedging -----------------------------------------------------------------------------------

    def _band(self) -> tuple[float, float, float]:
        """How much un-hedged inventory to hold: ``(lowest, highest, ceiling)`` in lots.

        Inside ``[lowest, highest]`` nothing is hedged. Beyond it the excess is
        hedged, unless the forecast says waiting is worth it, and then never
        beyond ``ceiling``.
        """
        cfg = self.config
        if cfg.hedging == "immediate" or self._future is None:
            return 0.0, 0.0, 0.0
        if self._match_time() >= cfg.session_seconds - cfg.flatten_seconds:
            return 0.0, 0.0, 0.0

        # Above the free allowance the exchange starts a clock; stay well inside it.
        allowance = cfg.free_unhedged_lots
        if self._future[0][0] == 0 or self._future[2][0] == 0:
            # One side of the future is empty, so there is no spread to price the bet with. Carry
            # only what the exchange lets anyone carry for ever.
            return -float(allowance), float(allowance), float(allowance)
        cap = cfg.carry_lots
        now = self.event_loop.time()
        unhedged = abs(self.etf_position + self.future_position)
        if unhedged > allowance:
            if self._over_since is None:
                self._over_since = now
            if now - self._over_since >= cfg.carry_seconds:
                cap = allowance
        else:
            self._over_since = None

        if cfg.hedging == "band":
            return -float(allowance), float(allowance), float(allowance)

        # The bet: a lot left un-hedged saves half the future's spread if customer flow takes it
        # away before we have to hedge it, and is exposed to the future's moves until then.
        # Kelly sizes the bet against the running bankroll. The forecast is left out of it: it
        # sees one update ahead and the bet runs for many (its job is timing, in _hedge).
        dollars_per_tick = cfg.tick / 100.0
        half_spread = (self._future[0][0] - self._future[2][0]) / 200.0  # dollars: what a hedge costs
        bankroll = cfg.bankroll + max(self._mark_to_market(), 0.0)
        variance = self._variance * dollars_per_tick ** 2 * cfg.carry_horizon / cfg.book_interval
        lo, hi = hedge_band(bankroll, self._carry_drift(), half_spread * cfg.offset_probability,
                            variance * self._carry_risk(), cfg.kelly_fraction)
        return max(lo, -cap), min(hi, cap), float(cap)

    def _carry_drift(self) -> float:
        """Expected dollars per lot that a carried lot earns from the price moving over the carry horizon.

        None here: the forecast sees one update ahead and the bet runs for many
        (its job is timing, in _hedge). A version that models the price over the
        horizon says otherwise.
        """
        return 0.0

    def _carry_risk(self) -> float:
        """How much riskier than usual carrying inventory is expected to be over the horizon (1 is usual)."""
        return 1.0

    def _hedge(self) -> None:
        """Trade the future until un-hedged inventory is back inside the band."""
        delta = self.etf_position + self.future_position + self._hedge_pending
        lo, hi, ceiling = self._band()
        self._held = (lo, hi)
        target = int(hedge_target(delta, lo, hi))  # truncation leans towards flat
        if target != delta and abs(delta) <= ceiling and delta * self._forecast >= self.config.defer_ticks * abs(delta):
            # The hedge is due, but the future is forecast to move in the inventory's favour over
            # the next update. Waiting is free, so wait; the forecast is re-read every update.
            self.stats["deferred"] += 1
            return
        lots = target - delta
        # The future position limit must hold whichever of the hedges in flight fill: a hedge
        # comes back unfilled when there is nothing on the other side of the future.
        limit = self.config.position_limit
        buying = sum(pending for pending in self._hedges.values() if pending > 0)
        selling = -sum(pending for pending in self._hedges.values() if pending < 0)
        lots = max(min(lots, limit - self.future_position - buying), -limit - self.future_position + selling)
        if lots == 0 or self._hedge_failed == self._seq or not self._may_send(priority=True):
            return
        order_id = next(self._ids)
        self._hedges[order_id] = lots
        self._hedge_pending += lots
        if lots > 0:
            self.send_hedge_order(order_id, Side.BUY, self._max_ask, lots)
        else:
            self.send_hedge_order(order_id, Side.SELL, self._min_bid, -lots)

    def _match_time(self) -> float:
        """Seconds into the match, from the number of the latest order book update (the first is at the open)."""
        return (self._seq - 1) * self.config.book_interval

    def _mark_to_market(self) -> float:
        """Running profit in dollars, with both legs marked at the future's mid."""
        if self._future is None or self._future[0][0] == 0 or self._future[2][0] == 0:
            return 0.0
        mid = (self._future[0][0] + self._future[2][0]) / 2.0
        return (self.cash + (self.etf_position + self.future_position) * mid) / 100.0

    # -- order management --------------------------------------------------------------------------

    def _room(self, side: Side) -> int:
        """Lots we could still trade on ``side`` if every live order on that side filled first."""
        working = sum(order.remaining for order in self._orders.values() if order.side == side)
        if side == Side.BUY:
            return self.config.position_limit - self.etf_position - working
        return self.config.position_limit + self.etf_position - working

    def _work(self) -> None:
        for side in (Side.SELL, Side.BUY):
            self._work_side(side)

    def _work_side(self, side: Side) -> None:
        """Move one step from the orders we have towards the orders we want.

        Anything that needs capacity held by a live order waits for the
        exchange to confirm that order is gone; its final status message
        brings us back here.
        """
        quote = self._quotes[side]
        room = self._room(side)
        opposite = self._quotes[Side.BUY if side == Side.SELL else Side.SELL]
        taking = any(not order.is_quote and order.side == side for order in self._orders.values())

        take = self._want_take[side]
        if take is not None and not taking:
            price, volume = take
            if room >= volume or (room > 0 and quote is None):
                if self._crosses(side, price, opposite):
                    self._cancel(opposite)  # our own quote is in the way; take once it is gone
                    return
                if not self._may_send():
                    return
                self._want_take[side] = None
                self._insert(side, price, min(volume, room), Lifespan.FILL_AND_KILL)
                return  # the quote is sized once the take has settled
            if quote is not None:
                self._cancel(quote)  # free the capacity; the take goes out when the cancel is confirmed
                return
            self._want_take[side] = None  # no capacity at all on this side

        if taking:
            return

        want = self._want_quote[side]
        if quote is not None:
            if quote.cancelling:
                return
            size = min(want[1], room + quote.remaining) if want is not None else 0
            if want is None or quote.price != want[0] or quote.remaining * 2 < size:
                self._cancel(quote)
            return
        if want is None:
            return
        price, size = want
        size = min(size, room)
        if size <= 0 or self._crosses(side, price, opposite) or not self._may_send():
            return
        self._quotes[side] = self._insert(side, price, size, Lifespan.GOOD_FOR_DAY)

    @staticmethod
    def _crosses(side: Side, price: int, opposite: _Order | None) -> bool:
        """The exchange rejects an order that would trade with one of our own."""
        if opposite is None:
            return False
        return price <= opposite.price if side == Side.SELL else price >= opposite.price

    def _insert(self, side: Side, price: int, volume: int, lifespan: Lifespan) -> _Order:
        order = _Order(next(self._ids), side, price, volume, lifespan == Lifespan.GOOD_FOR_DAY)
        self._orders[order.order_id] = order
        self.send_insert_order(order.order_id, side, price, volume, lifespan)
        return order

    def _cancel(self, order: _Order | None) -> None:
        if order is None or order.cancelling or not self._may_send():
            return
        order.cancelling = True
        self.send_cancel_order(order.order_id)

    def _may_send(self, priority: bool = False) -> bool:
        """Our own message-rate governor: hedges may use the last few slots, quotes may not."""
        now = self.event_loop.time()
        sent = self._sent
        # The exchange throttles over its interval divided by its speed, measured in match time,
        # which itself runs at that speed: on our clock the window is the interval over speed squared.
        window = self.config.message_interval / self.config.speed ** 2
        while sent and now - sent[0] >= window:
            sent.popleft()
        budget = self.config.message_limit - (2 if priority else 8)
        if len(sent) >= budget:
            self.stats["throttled"] += 1
            return False
        sent.append(now)
        return True


def load_config() -> StrategyConfig:
    """Strategy settings for a trader started by the exchange's own launcher.

    ``VIGIL_STRATEGY`` names a preset or a challenger, or a JSON file of
    settings (which may name the one it starts from under ``"preset"``); the
    default is the full strategy.
    """
    from vigil.strategies.challengers import STRATEGIES
    choice = os.environ.get("VIGIL_STRATEGY", "full")
    if choice in STRATEGIES:
        return STRATEGIES[choice]
    with open(choice) as handle:
        settings = json.load(handle)
    base = STRATEGIES[settings.pop("preset", "full")]
    return base.with_(**settings)


def trader_class(config: StrategyConfig) -> type[EnsembleTrader]:
    """The trader that runs ``config``: base Vigil, or the challenger it names."""
    if config.engine == "ensemble":
        return EnsembleTrader
    from vigil.strategies.challengers import ENGINES
    try:
        return ENGINES[config.engine]
    except KeyError:
        raise ValueError(f"unknown engine {config.engine!r}; choose from ensemble, {', '.join(ENGINES)}") from None


class AutoTrader:
    """Entry point with the constructor signature Ready Trader Go expects: builds whichever trader the
    settings name."""

    def __new__(cls, loop, team_name: str, secret: str):
        config = load_config()
        return trader_class(config)(loop, team_name, secret, config)


def describe(config: StrategyConfig) -> dict[str, Any]:
    return asdict(config)
