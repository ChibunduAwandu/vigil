"""Forecast-skewed quoting.

Base Vigil prices its quotes around the future's mid and uses its forecast for
one thing only, the timing of hedges. Cartea, Jaimungal & Penalva
(*Algorithmic and High-Frequency Trading*, 2015, ch. 10) show that a market
maker with a short-term signal should instead move both quotes in the
direction of the expected drift: with the price expected to rise, sell
higher and buy more readily, and the reverse. Here every price the quotes are
worked out from (the fair value and the hedge prices) is shifted by
``weight`` times the boosted model's expected move over the next update.
``weight`` 1 is what the model says; it is fitted leave-one-session-out.

The forecast takes time to compute, and base Vigil sends its quotes before
computing it, so as to reach the exchange first. This version keeps the takes
first and lets a resting quote keep its place, but works out new quote prices
only once the forecast is in: its quotes reach the exchange later.
"""
from __future__ import annotations

from vigil.rtg import Side
from vigil.strategies.challengers.base import Challenger

DEFAULT_WEIGHT = 1.0
DEFAULT_FAST_WEIGHT = 1.0


class SkewTrader(Challenger):
    name = "skew"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.weight = self.knob("weight", DEFAULT_WEIGHT)

    def _trade(self, asks, bids, future_ask, future_bid, mid) -> None:
        self._decide_takes(asks, bids, future_ask, future_bid, mid)
        self._keep_quotes()
        self._work()
        self._learn()
        drift = self.weight * self._forecast * self.config.tick  # cents
        self._decide_quotes(asks, bids, future_ask + drift, future_bid + drift, mid + drift)
        self._work()
        self._hedge()

    def _keep_quotes(self) -> None:
        """Until the new prices are known, want the quotes there are, as they are: none is moved or added."""
        for side in (Side.SELL, Side.BUY):
            quote = self._quotes[side]
            keep = quote is not None and not quote.cancelling
            self._want_quote[side] = (quote.price, quote.remaining) if keep else None


class SkewFastTrader(Challenger):
    """Forecast-skewed quoting at base Vigil's speed.

    ``skew`` waits for the boosted model before it quotes, and on the real
    exchange that put its quotes behind base Vigil's on four updates in five.
    This version skews by book pressure instead: how far the future's touch
    price, weighted by the size on each side, sits from its mid (the microprice
    less the mid). It needs nothing but the future's book, which the exchange
    publishes just before the ETF's, so it is worked out on the future's book
    and the quotes leave on the ETF's as soon as base Vigil's do; the model
    still runs afterwards, to time the hedges as base Vigil's does. Its sign is
    that of the top-of-book imbalance, which calls the next move's direction
    about as often as the model does (57.9 against 58.8 per cent, RESULTS.md);
    each tick of it is worth the expected move the model was measured to see in
    it (``momentum_scale``), times ``weight``, fitted leave-one-session-out.
    """

    name = "skew_fast"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.weight = self.knob("weight", DEFAULT_FAST_WEIGHT)
        self._lean = self.weight * self._momentum_scale  # cents of drift per cent of microprice displacement
        self._skewed = (0.0, 0.0, 0.0)  # the future's ask, bid and mid, moved by the lean

    def _future_book(self) -> None:
        ask_prices, ask_volumes, bid_prices, bid_volumes = self._future
        ask, bid = ask_prices[0], bid_prices[0]
        if ask and bid:  # else there is no trading on this update
            ask_size, bid_size = ask_volumes[0], bid_volumes[0]
            mid = (ask + bid) / 2.0
            drift = self._lean * ((ask * bid_size + bid * ask_size) / (bid_size + ask_size) - mid)
            self._skewed = (ask + drift, bid + drift, mid + drift)

    def _trade(self, asks, bids, future_ask, future_bid, mid) -> None:
        self._decide_takes(asks, bids, future_ask, future_bid, mid)
        self._decide_quotes(asks, bids, *self._skewed)
        self._work()
        self._learn()
        self._hedge()
