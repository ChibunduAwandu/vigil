"""A self-exciting (Hawkes) model of the future's aggressive order flow.

What the measurements say. The ETF's customer orders arrive as a Poisson
process: their counts in a second have a variance equal to their mean, and
there are no bursts to predict. The future's aggressive orders, the ones that
cross its spread and move its price, are another matter: their counts vary
five to twenty-five times as much as a Poisson process's would. They come in
bursts, and a burst means the price is about to move more than usual.

The model. Traders see the future's trades as the volume lifted from its
offers (buyer-initiated) and hit on its bids (seller-initiated) since the last
update. Each kind is modelled as a discrete-time Hawkes process with an
exponential kernel (Hawkes 1971; Bacry, Mastromatteo & Muzy 2015):

    E[v_i(t+1)] = mu_i + sum_j alpha_ij S_j(t),   S_j(t) = sum_{u <= t} beta^(t-u) v_j(u),

so each lot traded raises the expected volume of the next updates, by an
amount that decays by ``beta`` an update. ``mu``, ``alpha`` (kept
non-negative) and ``beta`` are fitted by least squares, leave-one-session-
out. The expected activity is then mapped, by two more regressions, to the
squared move of the future's mid at the next update and to its variance over
the horizon inventory is carried for (ten seconds).

How much it knows. Per update the model says little: it explains a few per
cent of the next update's volume, and its expected activity has a correlation
of about 0.03 with the size of the next move. Over ten seconds it says more:
a correlation of 0.14 to 0.17 with the variance that follows, in three of the
four sessions.

The use. When the model expects more activity than usual, quotes are put
further from the hedge price and shown in smaller size (where and how much to
quote), and the inventory carried un-hedged is sized for the greater risk
over the carry horizon, which is where the model has something to say.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

from vigil.rtg import Instrument
from vigil.strategies.challengers.base import Challenger

BETAS = (0.3, 0.5, 0.7, 0.8, 0.85, 0.9, 0.95)


def _excitation(volume: np.ndarray, beta: float) -> np.ndarray:
    """``S(t) = sum_{u <= t} beta^(t-u) v(u)``."""
    out = np.empty_like(volume, dtype=float)
    running = 0.0
    for t, v in enumerate(volume):
        running = beta * running + v
        out[t] = running
    return out


def _nonnegative_fit(X: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Least squares for ``y ~ X`` with every coefficient but the first (the intercept) at least zero.

    Two regressors, so every subset of them can simply be tried.
    """
    best, best_error = None, math.inf
    for keep in ((1, 2), (1,), (2,), ()):
        columns = [0, *keep]
        coefficients, *_ = np.linalg.lstsq(X[:, columns], y, rcond=None)
        if any(c < 0 for c in coefficients[1:]):
            continue
        full = np.zeros(X.shape[1])
        full[columns] = coefficients
        error = float(((X @ full - y) ** 2).sum())
        if error < best_error:
            best, best_error = full, error
    return best


def fit_hawkes(sessions: Sequence[str], betas: Sequence[float] = BETAS, horizon: int = 40) -> dict[str, object]:
    """Fit the bivariate model to the future's buyer- and seller-initiated volume per update, pooled over sessions.

    ``horizon`` is the carry horizon in updates, over which the variance that
    follows a given level of expected activity is also fitted.
    """
    from vigil.sim.tape import load_tape
    tapes = [load_tape(session) for session in sessions]
    flows = [(tape.buy_volume[:, Instrument.FUTURE].astype(float), tape.sell_volume[:, Instrument.FUTURE].astype(float))
             for tape in tapes]
    mids = []
    for tape in tapes:
        mid = tape.mid(Instrument.FUTURE) / 100.0
        for i in range(1, len(mid)):
            if math.isnan(mid[i]):
                mid[i] = mid[i - 1]
        mids.append(mid)
    return hawkes_from_flows(flows, mids, betas, horizon)


def hawkes_from_flows(flows: Sequence[tuple[np.ndarray, np.ndarray]], mids: Sequence[np.ndarray],
                      betas: Sequence[float] = BETAS, horizon: int = 40) -> dict[str, object]:
    """The fit of ``fit_hawkes``, on (buyer-initiated, seller-initiated) volume per update and mids in ticks."""
    best = None
    for beta in betas:
        rows, targets = [], []
        for buy, sell in flows:
            sb, ss = _excitation(buy, beta), _excitation(sell, beta)
            rows.append(np.column_stack([np.ones(len(buy) - 1), sb[:-1], ss[:-1]]))
            targets.append(np.column_stack([buy[1:], sell[1:]]))
        X, Y = np.vstack(rows), np.vstack(targets)
        coefficients = [_nonnegative_fit(X, Y[:, i]) for i in range(2)]
        error = sum(float(((X @ c - Y[:, i]) ** 2).sum()) for i, c in enumerate(coefficients))
        if best is None or error < best[0]:
            total = float(((Y - Y.mean(axis=0)) ** 2).sum())
            best = (error, beta, coefficients, 1.0 - error / total)
    _, beta, (buy_fit, sell_fit), r2 = best
    mu = [float(buy_fit[0]), float(sell_fit[0])]
    alpha = [[float(buy_fit[1]), float(buy_fit[2])], [float(sell_fit[1]), float(sell_fit[2])]]
    branching = float(max(abs(np.linalg.eigvals(np.array(alpha) / (1.0 - beta)))))

    # From expected activity to the expected squared move of the mid at the next update, and how
    # quickly a burst dies away.
    activity, squared, ahead_activity, ahead = [], [], [], []
    for mid, (buy, sell) in zip(mids, flows, strict=True):
        sb, ss = _excitation(buy, beta), _excitation(sell, beta)
        expected = (mu[0] + alpha[0][0] * sb + alpha[0][1] * ss) + (mu[1] + alpha[1][0] * sb + alpha[1][1] * ss)
        move = np.diff(mid)
        good = ~np.isnan(move)
        activity.append(expected[:-1][good])
        squared.append(move[good] ** 2)
        # Variance over the next ``horizon`` updates: the squared moves summed.
        total = np.concatenate([[0.0], np.cumsum(np.nan_to_num(move) ** 2)])
        count = len(move) - horizon
        ahead_activity.append(expected[:count])
        ahead.append(total[np.arange(count) + horizon] - total[np.arange(count)])
    a, s = np.concatenate(activity), np.concatenate(squared)
    slope, intercept = np.polyfit(a, s, 1)
    b, v = np.concatenate(ahead_activity), np.concatenate(ahead)
    horizon_slope, horizon_intercept = np.polyfit(b, v, 1)
    return {"beta": float(beta), "mu": mu, "alpha": alpha, "r2": float(r2), "branching_ratio": branching,
            "move_intercept": float(intercept), "move_slope": float(slope), "mean_squared_move": float(s.mean()),
            "move_correlation": float(np.corrcoef(a, s)[0, 1]), "horizon": horizon,
            "horizon_intercept": float(horizon_intercept), "horizon_slope": float(horizon_slope),
            "mean_horizon_variance": float(v.mean()), "horizon_correlation": float(np.corrcoef(b, v)[0, 1]),
            "mean_activity": float(a.mean())}


class HawkesTrader(Challenger):
    name = "hawkes"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        fitted = self.fitted
        self.beta = float(fitted.get("beta", 0.8))
        self.mu = list(fitted.get("mu", [0.0, 0.0]))
        self.alpha = [list(row) for row in fitted.get("alpha", [[0.0, 0.0], [0.0, 0.0]])]
        self.move_intercept = float(fitted.get("move_intercept", 1.0))
        self.move_slope = float(fitted.get("move_slope", 0.0))
        self.mean_squared_move = float(fitted.get("mean_squared_move", 1.0))
        self.horizon_intercept = float(fitted.get("horizon_intercept", 1.0))
        self.horizon_slope = float(fitted.get("horizon_slope", 0.0))
        self.mean_horizon_variance = float(fitted.get("mean_horizon_variance", 1.0))
        self.weight = self.knob("weight", 1.0)
        self._volume = [0.0, 0.0]  # buyer- and seller-initiated lots since the last update
        self._excited = [0.0, 0.0]
        self.next_ratio = 1.0  # expected squared move at the next update, relative to usual
        self.horizon_ratio = 1.0  # the same, averaged over the carry horizon

    def on_trade_ticks_message(self, instrument: int, sequence_number: int, ask_prices, ask_volumes, bid_prices,
                               bid_volumes) -> None:
        super().on_trade_ticks_message(instrument, sequence_number, ask_prices, ask_volumes, bid_prices, bid_volumes)
        if instrument == Instrument.FUTURE:
            self._volume[0] += sum(ask_volumes)  # offers lifted: the buyer was aggressive
            self._volume[1] += sum(bid_volumes)

    def _on_tick(self) -> None:
        # Bring the flow model up to date before anything is decided: unlike the features it is a few
        # multiplications, and the quotes about to be sent are the ones it should shape.
        if self._unlearned:
            self._update_flow()
        super()._on_tick()

    def _update_flow(self) -> None:
        beta, excited, volume = self.beta, self._excited, self._volume
        excited[0] = beta * excited[0] + volume[0]
        excited[1] = beta * excited[1] + volume[1]
        volume[0] = volume[1] = 0.0
        mu, alpha = self.mu, self.alpha
        activity = (mu[0] + alpha[0][0] * excited[0] + alpha[0][1] * excited[1]
                    + mu[1] + alpha[1][0] * excited[0] + alpha[1][1] * excited[1])
        weight = self.weight
        ratio = max(self.move_intercept + self.move_slope * activity, 0.0) / self.mean_squared_move
        self.next_ratio = max(1.0 + weight * (ratio - 1.0), 0.25)
        ratio = max(self.horizon_intercept + self.horizon_slope * activity, 0.0) / self.mean_horizon_variance
        self.horizon_ratio = max(1.0 + weight * (ratio - 1.0), 0.25)

    def _quote_margin(self) -> float:
        # Further from the hedge price when the future is expected to move more: by the expected move's
        # size, and never closer than base Vigil's margin.
        return self.config.margin_ticks * math.sqrt(max(self.next_ratio, 1.0))

    def _quote_size(self) -> int:
        return max(10, int(self.config.quote_lots / max(self.next_ratio, 1.0)))

    def _carry_risk(self) -> float:
        return self.horizon_ratio
