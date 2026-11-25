"""The Kelly criterion, in the three forms the strategy uses.

Kelly (1956) asks how much of a bankroll to stake on a favourable bet so that
wealth grows fastest in the long run. The answer maximises the expected
logarithm of wealth, and the functions below are that one idea applied to a
binary bet, to a bet with a continuous payoff, and to the question of how much
un-hedged inventory to carry when hedging costs money.

Everything here is plain arithmetic on floats: no numpy, nothing that a Ready
Trader Go auto-trader is not allowed to import.
"""
from __future__ import annotations

import math


def kelly_fraction(p: float, b: float = 1.0) -> float:
    """Fraction of the bankroll to stake on a bet won with probability ``p`` at odds ``b``.

    A unit stake returns ``+b`` on a win and ``-1`` on a loss, and the optimum
    is ``f* = p - (1 - p) / b``. A negative value means the bet has negative
    edge and the other side is the one to take (at odds ``1/b``).

    >>> round(kelly_fraction(0.6, 1.0), 3)
    0.2
    """
    if not 0.0 <= p <= 1.0:
        raise ValueError("p must be a probability")
    if b <= 0.0:
        raise ValueError("odds must be positive")
    return p - (1.0 - p) / b


def growth_rate(f: float, p: float, b: float = 1.0) -> float:
    """Expected log-growth per bet when staking the fraction ``f``: ``p*ln(1+f*b) + (1-p)*ln(1-f)``."""
    if f >= 1.0 or f * b <= -1.0:
        return -math.inf
    return p * math.log1p(f * b) + (1.0 - p) * math.log1p(-f)


def kelly_size(bankroll: float, edge: float, variance: float, fraction: float = 1.0) -> float:
    """Units of exposure for a bet with a continuous payoff.

    If one unit earns ``edge`` on average with variance ``variance``, log-wealth
    grows fastest at ``bankroll * edge / variance`` units (the second-order
    expansion of ``E[log(1 + x*R/W)]``). ``fraction`` scales the answer:
    half-Kelly (0.5) gives up a quarter of the growth for half the volatility,
    and is the usual allowance for the edge being an estimate.
    """
    if variance <= 0.0 or bankroll <= 0.0:
        return 0.0
    return fraction * bankroll * edge / variance


def hedge_band(bankroll: float, drift: float, carry: float, variance: float,
               fraction: float = 0.5) -> tuple[float, float]:
    """The range of un-hedged inventory worth holding when hedging is costly.

    Holding ``r`` lots un-hedged for one period earns ``r * drift`` from the
    expected price move and, whichever way it points, saves ``carry`` per lot in
    expectation: the hedging cost that is never paid if customer flow takes
    the inventory away first. Its risk is ``r**2 * variance``. Maximising

        r * drift + |r| * carry - r**2 * variance / (2 * fraction * bankroll)

    gives no single optimum but a band: inside ``[lo, hi]`` the cost of trading
    outweighs the gain, so inventory is left alone; outside it, the inventory
    is hedged back to the nearer edge. This is the no-trade region of
    portfolio choice under transaction costs (Davis & Norman, 1990) with a
    fractional-Kelly investor.

    >>> hedge_band(1000.0, 0.0, 0.1, 1.0, fraction=0.5)
    (-50.0, 50.0)
    """
    if variance <= 0.0 or bankroll <= 0.0:
        return 0.0, 0.0
    scale = fraction * bankroll / variance
    return scale * (drift - carry), scale * (drift + carry)


def hedge_target(delta: float, lo: float, hi: float) -> float:
    """Where to hedge ``delta`` lots of inventory to, given the band.

    Hedging only ever takes risk off: paying the spread to *add* exposure is
    never worth it at the edges we deal in, so a band that lies wholly on the
    far side of zero sends the inventory to flat, not through it.

    >>> hedge_target(30, -12, 12), hedge_target(8, -12, 12), hedge_target(-30, 5, 20)
    (12, 8, 0.0)
    """
    if delta > 0:
        return min(delta, max(hi, 0.0))
    if delta < 0:
        return max(delta, min(lo, 0.0))
    return 0.0


def kelly_weights(mu: list[float], cov: list[list[float]], fraction: float = 1.0) -> list[float]:
    """Kelly-optimal exposure to several correlated bets at once: ``fraction * inverse(cov) @ mu``.

    Solved by Gaussian elimination with partial pivoting; ``cov`` must be
    symmetric positive definite.
    """
    n = len(mu)
    a = [list(map(float, row)) + [float(m)] for row, m in zip(cov, mu, strict=True)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise ValueError("covariance matrix is singular")
        a[col], a[pivot] = a[pivot], a[col]
        for row in range(col + 1, n):
            factor = a[row][col] / a[col][col]
            for k in range(col, n + 1):
                a[row][k] -= factor * a[col][k]
    weights = [0.0] * n
    for row in range(n - 1, -1, -1):
        rest = sum(a[row][k] * weights[k] for k in range(row + 1, n))
        weights[row] = (a[row][n] - rest) / a[row][row]
    return [fraction * w for w in weights]
