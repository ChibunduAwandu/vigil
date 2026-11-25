"""Mean-reversion carrying: the future's deviation from its recent average as an Ornstein-Uhlenbeck process.

What the measurements say. The gap between the ETF and the future closes in a
second or two, and it closes because someone takes the mispriced orders that
open it, which base Vigil already does. What does revert slowly enough to hold
a position on is the future itself: over ten to thirty seconds it falls back
towards its recent average in three of the four sample sessions (and runs
away from it in the fourth, which a fitted model will get wrong).

The model. Let ``x`` be the future's mid less its exponential average, in
ticks. If ``x`` reverts as an Ornstein-Uhlenbeck process, the expected change
in the price over the next ``h`` updates is ``-c (1 - exp(-kappa h)) x``: a
fraction of the deviation, growing with the horizon. ``c`` and ``kappa`` are
fitted to the regression of the price change on ``x`` at several horizons,
leave-one-session-out; a negative ``c`` is momentum, and is allowed.

The position. The only position a trader here can hold in the future's
direction is inventory it leaves un-hedged, and base Vigil already chooses how
much of that to carry by the Kelly criterion, with the price's drift over the
carry horizon set to zero. This version puts the model's drift there instead,
so the band of inventory it carries leans the way the price is expected to
go, within the same limits and the exchange's un-hedged-lots rule.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

from vigil.rtg import Instrument
from vigil.strategies.challengers.base import Challenger

DEFAULT_SPAN = 64  # updates (sixteen seconds) in the average the deviation is measured from
HORIZONS = (4, 8, 16, 40, 80, 120)  # updates at which the reversion is measured when fitting


def _ema(values: np.ndarray, span: int) -> np.ndarray:
    weight = 2.0 / (span + 1.0)
    out = np.empty_like(values)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = out[i - 1] + weight * (values[i] - out[i - 1])
    return out


def fit_reversion(sessions: Sequence[str], span: int = DEFAULT_SPAN,
                  horizons: Sequence[int] = HORIZONS) -> dict[str, float | list[float]]:
    """``reversion_from_mids`` on the future's mid, in ticks, as traders were shown it in each session."""
    from vigil.sim.tape import load_tape
    mids = []
    for session in sessions:
        mid = load_tape(session).mid(Instrument.FUTURE) / 100.0
        for i in range(1, len(mid)):
            if math.isnan(mid[i]):
                mid[i] = mid[i - 1]
        mids.append(mid)
    return reversion_from_mids(mids, span, horizons)


def reversion_from_mids(mids: Sequence[np.ndarray], span: int = DEFAULT_SPAN,
                        horizons: Sequence[int] = HORIZONS) -> dict[str, float | list[float]]:
    """Fit ``E[price change over h updates] = -c (1 - exp(-kappa h)) x`` to price series, pooled.

    The slope at each horizon is the least-squares regression of the change on
    the deviation; ``c`` and ``kappa`` then fit those slopes (``kappa`` by a fine
    search, ``c`` in closed form for each ``kappa``).
    """
    deviation: list[np.ndarray] = []
    changes: dict[int, list[np.ndarray]] = {h: [] for h in horizons}
    for mid in mids:
        x = mid - _ema(mid, span)
        reach = max(horizons)
        start = 4 * span  # past the average's warm-up
        deviation.append(x[start:len(mid) - reach])
        for h in horizons:
            changes[h].append(mid[start + h:len(mid) - reach + h] - mid[start:len(mid) - reach])
    x = np.concatenate(deviation)
    centred = x - x.mean()
    slopes = []
    for h in horizons:
        y = np.concatenate(changes[h])
        slopes.append(float(np.dot(centred, y - y.mean()) / np.dot(centred, centred)))
    h = np.array(horizons, dtype=float)
    target = np.array(slopes)
    best = (math.inf, 0.0, 0.0)
    for kappa in np.exp(np.linspace(np.log(1e-3), np.log(2.0), 400)):
        shape = -(1.0 - np.exp(-kappa * h))
        c = float(np.dot(shape, target) / np.dot(shape, shape))
        error = float(((c * shape - target) ** 2).sum())
        if error < best[0]:
            best = (error, c, float(kappa))
    _, c, kappa = best
    return {"span": span, "c": c, "kappa": kappa, "half_life_updates": math.log(2.0) / kappa,
            "horizons": list(horizons), "slopes": slopes}


class OUTrader(Challenger):
    name = "ou"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        cfg = self.config
        self.span = int(self.fitted.get("span", DEFAULT_SPAN))
        c, kappa = float(self.fitted.get("c", 0.0)), float(self.fitted.get("kappa", 0.0))
        horizon = cfg.carry_horizon / cfg.book_interval
        # Expected change in the price over the carry horizon, per tick of deviation: the share of the
        # deviation the price gives back (negative: it reverts; positive: it runs on).
        self.response = -c * (1.0 - math.exp(-kappa * horizon))
        self.weight = self.knob("weight", 1.0)
        self._average: float | None = None
        self._updates = 0

    def _learn(self) -> None:
        fresh = self._unlearned
        super()._learn()
        future = self._future
        if fresh and future is not None and future[0][0] and future[2][0]:
            mid = (future[0][0] + future[2][0]) / 2.0
            if self._average is None:
                self._average = mid
            else:
                self._average += 2.0 / (self.span + 1.0) * (mid - self._average)
            self._updates += 1

    def deviation(self) -> float:
        """How far the future's mid is from its average, in ticks; zero until the average has warmed up."""
        future = self._future
        if self._average is None or self._updates < self.span or not (future[0][0] and future[2][0]):
            return 0.0
        return ((future[0][0] + future[2][0]) / 2.0 - self._average) / self.config.tick

    def _carry_drift(self) -> float:
        # Dollars per lot that a carried long lot is expected to earn over the horizon (a tick is a dollar a lot).
        return self.weight * self.response * self.deviation() * self.config.tick / 100.0
