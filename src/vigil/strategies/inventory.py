"""What inventory is worth: the market maker's position as a dynamic programme.

The exchange caps the ETF position at a hard limit. A market maker sitting at
its limit can only quote one side, so it earns half of what it could; one near
the limit is about to be in that spot. Inventory therefore has a price even
when it is fully hedged, and a trade that moves the position towards zero is
worth more than its edge while one that moves it away is worth less.

That price is computed rather than guessed. The model is the simplest one
that has the right shape: customer orders of random size arrive, equally
often on either side, and each trades with our quote for as many lots as the
position limit allows, earning ``edge`` per lot. The long-run value of
holding ``q`` lots relative to holding none, ``h(q)``, is the fixed point of
the average-reward Bellman equation, found by relative value iteration
(Puterman, *Markov Decision Processes*, ch. 8). How often orders arrive drops
out: a busier market earns more per second at every position alike, and only
differences between positions matter here.

The solution is close to a parabola, which is the quadratic inventory penalty
of Avellaneda & Stoikov (2008) arrived at from the other direction: there it
is a risk aversion, here it is the opportunity cost of a position limit.
"""
from __future__ import annotations

import functools

import numpy as np


@functools.lru_cache(maxsize=16)
def inventory_values(limit: int = 100, mean_lots: float = 46.0, edge: float = 4.0, tolerance: float = 1e-7,
                     max_iterations: int = 5000) -> np.ndarray:
    """``h[q + limit]``: the value in dollars of holding ``q`` lots, relative to holding none.

    ``mean_lots`` is the mean of the (geometric) customer order size and
    ``edge`` the dollars earned per lot filled.
    """
    if limit <= 0:
        return np.zeros(1)
    rate = 0.25  # any value in (0, 0.5] gives the same table; this one converges quickly
    positions = np.arange(-limit, limit + 1)
    sizes = np.arange(1, int(8 * mean_lots) + 1)
    weights = np.exp(-sizes / mean_lots)
    weights /= weights.sum()

    # A customer buy is filled from our offer: we sell min(size, limit + q) lots. And the mirror image.
    sold = np.minimum(sizes[None, :], (limit + positions)[:, None])
    bought = np.minimum(sizes[None, :], (limit - positions)[:, None])
    row = np.arange(len(positions))[:, None]
    earned = rate * edge * ((sold + bought) @ weights)

    h = np.zeros(len(positions))
    for _ in range(max_iterations):
        updated = earned + rate * (h[row - sold] @ weights) + rate * (h[row + bought] @ weights) + (1 - 2 * rate) * h
        updated -= updated[limit]  # anchor h(0) = 0; what is subtracted is the average reward
        if np.abs(updated - h).max() < tolerance:
            h = updated
            break
        h = updated
    h.setflags(write=False)
    return h


def flat_values(limit: int = 100) -> np.ndarray:
    """The table for a trader that puts no price on inventory."""
    h = np.zeros(2 * limit + 1)
    h.setflags(write=False)
    return h


def best_block(edges: np.ndarray, values: np.ndarray, position: int, direction: int, limit: int) -> int:
    """How many lots of an available block to trade.

    ``edges[k]`` is the profit in dollars of the ``k``-th lot on its own
    merits, ``direction`` is +1 to buy and -1 to sell, and ``values`` is the
    inventory table. Returns the volume that maximises profit plus the change
    in inventory value: possibly zero, possibly more lots than are profitable
    on their own if they bring the position home.
    """
    n = min(len(edges), limit - direction * position)
    if n <= 0:
        return 0
    lots = np.arange(1, n + 1)
    worth = np.cumsum(edges[:n]) + values[position + direction * lots + limit] - values[position + limit]
    best = int(np.argmax(worth))
    return int(lots[best]) if worth[best] > 0 else 0
