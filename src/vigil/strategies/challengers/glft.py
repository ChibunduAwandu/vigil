"""Optimal quoting with fitted fill rates: Guéant, Lehalle & Fernandez-Tapia (2013).

Base Vigil chooses its quote prices by a search: every price inside the
standing market is scored by the chance a customer order reaches it, the
volume queued ahead, and the edge. This version takes them instead from the
closed-form solution of the market-making problem in "Dealing with the
inventory risk: a solution to the market making problem" (Mathematics and
Financial Economics 7, 2013), which extends Avellaneda & Stoikov (2008) to a
market maker whose inventory is bounded, as it is here by the position limit.

The model: the mid price moves as Brownian motion with volatility ``sigma``;
orders that reach a quote ``delta`` ticks from the mid arrive at the rate
``A * exp(-k * delta)``; the market maker maximises expected CARA utility with
risk aversion ``gamma``. Writing the value function as
``-exp(-gamma (x + q s)) v_q^(-gamma / k)`` turns the problem into a linear
system ``dv/dt = M v`` with ``M`` tridiagonal,

    M[q, q] = alpha q^2,  M[q, q +- 1] = -eta,
    alpha = k gamma sigma^2 / 2,  eta = A (1 + gamma / k)^-(1 + k / gamma),

and the optimal distances of the bid and the ask from the mid are

    delta_bid(q) = ln(v_q / v_{q+1}) / k + ln(1 + gamma / k) / gamma
    delta_ask(q) = ln(v_q / v_{q-1}) / k + ln(1 + gamma / k) / gamma.

Far from the end of the session ``v`` is proportional to the eigenvector of
``M`` for its smallest eigenvalue, which makes the quotes depend on inventory
alone. The unit of inventory and of arrivals here is one lot. ``A`` and ``k``
are fitted to where the sessions' customer orders are priced
(``fit_fill_rates``); ``gamma`` is fitted leave-one-session-out.

Two things the model leaves out matter here. Customer orders are priced so
widely (``k`` is about 0.07 a tick) that its unconstrained quotes sit some
thirteen ticks from the mid, beyond the standing market's walls, where
nothing trades. The quantity it maximises rises all the way to its optimum,
so the best quote that can trade is the one just inside the wall: that is
where the quotes go, unless the inventory term pulls one in closer. And it
leaves out competition: it assumes an order that reaches a price fills the
quote there, whatever else is queued in front of it.
"""
from __future__ import annotations

import functools
import math
from collections.abc import Sequence

import numpy as np

from vigil.rtg import Instrument, MarketEventOperation, Side
from vigil.strategies.challengers.base import Challenger

DEFAULT_A = 23.0  # lots a second, per side, of customer orders priced to reach the mid
DEFAULT_K = 0.073  # per tick: how fast that thins out with distance from the mid
DEFAULT_GAMMA = 0.005  # per tick-lot: risk aversion with one lot as the unit; fitted, see fit_fill_rates
_SIGMA_RANGE = (0.2, 5.0)  # ticks per square-root second: what the tables are computed over


def glft_offsets(A: float, k: float, gamma: float, sigma: float, limit: int) -> tuple[list[float], list[float]]:
    """Optimal (bid, ask) distances from the mid in ticks, indexed by ``q + limit``, in the stationary regime.

    ``inf`` where the model shows no quote: buying at the long limit, selling at the short one.

    The eigenvector's entries fall away like ``exp(-c q^2)`` and underflow towards the limits, but
    the quotes need only the ratios of neighbouring entries. Those follow from the eigenvalue
    equation by a recursion from the limit inwards, ``r_Q = eta / (alpha Q^2 - lambda)`` and
    ``r_q = eta / (alpha q^2 - lambda - eta r_{q+1})`` with ``r_q = f_q / f_{q-1}``, which picks out
    the decaying solution and stays accurate. The ground state is symmetric, ``f_-q = f_q``.
    """
    if A <= 0 or k <= 0 or gamma <= 0 or sigma < 0 or limit < 1:
        raise ValueError("glft_offsets needs positive A, k, gamma, limit and a non-negative sigma")
    alpha = 0.5 * k * gamma * sigma * sigma
    eta = A * (1.0 + gamma / k) ** -(1.0 + k / gamma)
    q = np.arange(-limit, limit + 1, dtype=float)
    M = np.diag(alpha * q * q) - eta * (np.eye(2 * limit + 1, k=1) + np.eye(2 * limit + 1, k=-1))
    lowest = float(np.linalg.eigvalsh(M)[0])
    ratio = [0.0] * (limit + 2)  # ratio[q] = f_q / f_{q-1} for q = 1..limit
    following = 0.0
    for level in range(limit, 0, -1):
        ratio[level] = following = eta / (alpha * level * level - lowest - eta * following)

    def up(level: int) -> float:  # f_level / f_(level - 1), for any level in -limit+1..limit
        return ratio[level] if level >= 1 else 1.0 / ratio[1 - level]

    base = math.log1p(gamma / k) / gamma
    bid = [math.inf] * (2 * limit + 1)
    ask = [math.inf] * (2 * limit + 1)
    for index, level in enumerate(range(-limit, limit + 1)):
        if level < limit:
            bid[index] = -math.log(up(level + 1)) / k + base
        if level > -limit:
            ask[index] = math.log(up(level)) / k + base
    return bid, ask


def _sigma_key(sigma: float) -> int:
    """Volatility rounded to 5% steps (on a log scale), so that tables can be shared."""
    sigma = min(max(sigma, _SIGMA_RANGE[0]), _SIGMA_RANGE[1])
    return round(math.log(sigma) * 20.0)


@functools.lru_cache(maxsize=4096)
def _table(A: float, k: float, gamma: float, key: int, limit: int) -> tuple[tuple[float, ...], tuple[float, ...]]:
    bid, ask = glft_offsets(A, k, gamma, math.exp(key / 20.0), limit)
    return tuple(bid), tuple(ask)


def fit_fill_rates(sessions: Sequence[str], deltas: Sequence[float] = tuple(np.arange(0.0, 8.5, 0.5)),
                   wall_lots: int = 1000) -> dict[str, float | list[float]]:
    """Fit ``A * exp(-k * delta)`` to the lots per second, per side, of customer orders priced at least
    ``delta`` ticks through the future's mid: the volume that would reach a quote there.

    Customer orders are the ETF orders of the replayed market below the size of
    the standing market's walls. Where each is priced is measured against the
    future's mid as traders were last shown it before the order arrived.
    """
    from vigil.rtg.marketdata import load_market_events
    from vigil.sim.tape import load_tape
    through: list[float] = []
    volume: list[float] = []
    seconds = 0.0
    for session in sessions:
        events = load_market_events(session)
        tape = load_tape(session)
        times, mid = tape.times, tape.mid(Instrument.FUTURE)
        for event in events:
            if event.instrument != Instrument.ETF or event.operation != MarketEventOperation.INSERT \
                    or event.volume >= wall_lots:
                continue
            at = int(np.searchsorted(times, event.time, side="right")) - 1
            if at < 0 or math.isnan(mid[at]):
                continue
            # A customer buy reaches an ask delta ticks above the mid if its limit is at least that high.
            through.append((event.price - mid[at]) / 100.0 if event.side == Side.BUY else (mid[at] - event.price) / 100.0)
            volume.append(event.volume)
        seconds += events[-1].time
    through_array, volume_array = np.array(through), np.array(volume, dtype=float)
    rates = [float(volume_array[through_array >= delta].sum() / seconds / 2.0) for delta in deltas]
    slope, intercept = np.polyfit(np.array(deltas), np.log(rates), 1)
    fitted = intercept + slope * np.array(deltas)
    residual = float(((np.log(rates) - fitted) ** 2).sum())
    total = float(((np.log(rates) - np.mean(np.log(rates))) ** 2).sum())
    return {"A": float(math.exp(intercept)), "k": float(-slope), "r2": 1.0 - residual / total,
            "deltas": [float(d) for d in deltas], "rates": rates,
            "customer_orders_per_second": len(volume) / seconds, "customer_mean_lots": float(volume_array.mean())}


class GLFTTrader(Challenger):
    name = "glft"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.A = float(self.fitted.get("A", DEFAULT_A))
        self.k = float(self.fitted.get("k", DEFAULT_K))
        self.gamma = self.knob("gamma", DEFAULT_GAMMA)
        # Work the tables out now, while nothing is racing, for every volatility the session may show.
        low, high = (_sigma_key(bound) for bound in _SIGMA_RANGE)
        for key in range(low, high + 1):
            _table(self.A, self.k, self.gamma, key, self.config.position_limit)

    def offsets(self) -> tuple[tuple[float, ...], tuple[float, ...]]:
        """The quote distances, by inventory, at the volatility the future is showing now."""
        sigma = math.sqrt(max(self._variance, 0.0) / self.config.book_interval)  # ticks per root second
        return _table(self.A, self.k, self.gamma, _sigma_key(sigma), self.config.position_limit)

    def _decide_quotes(self, asks, bids, future_ask, future_bid, mid) -> None:
        cfg = self.config
        closing = self._match_time() >= cfg.session_seconds - cfg.flatten_seconds
        self._want_quote[Side.SELL] = self._want_quote[Side.BUY] = None
        if not cfg.quote:
            return
        bid_offsets, ask_offsets = self.offsets()
        at = self.etf_position + cfg.position_limit
        if not (closing and self.etf_position <= 0):
            self._want_quote[Side.SELL] = self._place(asks, +1, mid + ask_offsets[at] * cfg.tick, future_bid)
        if not (closing and self.etf_position >= 0):
            self._want_quote[Side.BUY] = self._place(bids, -1, mid - bid_offsets[at] * cfg.tick, future_ask)

    def _place(self, levels: list[tuple[int, int]], sign: int, target: float, far: float) -> tuple[int, int] | None:
        """The quote for a model price of ``target`` cents: on the tick grid, away from the mid.

        A price at or beyond the standing market would never be reached, so the
        quote goes no further out than just inside it. It is never through the
        future either: selling the ETF below the future's bid, or buying above its
        offer, gives away what a hedge would get. At the position limit the model
        shows no quote on the side that would breach it.
        """
        if not math.isfinite(target):
            return None
        tick = self.config.tick
        price = (math.ceil(target / tick) if sign > 0 else math.floor(target / tick)) * tick
        for level, volume in levels:
            if volume >= self.config.wall_lots:
                if sign * (price - level) >= 0:
                    price = level - sign * tick
                break
        price = max(price, far + tick) if sign > 0 else min(price, far - tick)
        return int(price), self._quote_size()
