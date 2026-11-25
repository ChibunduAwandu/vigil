"""Features of the future's order book, computed one update at a time.

There is a single implementation, ``FeatureState``, and both sides use it: the
training set is built by pushing a recorded tape through it, and the live
trader pushes the information feed through it as messages arrive. A feature
can therefore never mean one thing offline and another online.

All features are expressed in ticks or as ratios, so a model fitted on one
price level carries over to another.
"""
from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence

import numpy as np

from vigil.rtg import Instrument
from vigil.sim.tape import Tape

FEATURE_NAMES: tuple[str, ...] = (
    "spread",  # best ask - best bid
    "imb1", "imb2", "imb3", "imb5",  # (bid depth - ask depth) / (bid depth + ask depth) over the top k levels
    "micro",  # size-weighted touch price - mid
    "wmid5",  # depth-weighted price of the five levels - mid
    "ofi", "ofi_ema",  # order flow imbalance at the touch (Cont, Kukanov & Stoikov), and its EMA
    "tflow", "tflow_ema",  # signed share of traded volume since the last update, and its EMA
    "tint",  # traded volume relative to its recent average
    "tvwap",  # volume-weighted trade price since the last update - mid
    "ret1", "ret2", "ret4", "ret8",  # mid change over the last k updates
    "mr16", "mr64",  # mid - EMA(mid): how far price has run from its recent average
    "vol",  # EMA of |ret1|
    "dimb1",  # change in imb1
    "lbid1", "lask1",  # log of touch size relative to its recent average
    "dbid", "dask",  # last move of each touch price
)
N_FEATURES = len(FEATURE_NAMES)
FEATURE_INDEX = {name: i for i, name in enumerate(FEATURE_NAMES)}

WARMUP_UPDATES = 64  # updates needed before the slowest average (mr64) means anything


def _alpha(span: int) -> float:
    return 2.0 / (span + 1.0)


class FeatureState:
    """Turns the future's book updates and trade ticks into a feature vector."""

    def __init__(self, tick_size: int = 100):
        self.tick = float(tick_size)  # cents
        self.updates = 0
        self.mid = math.nan  # cents, as of the last update
        self._mids: deque[float] = deque(maxlen=9)
        self._prev_touch: tuple[float, float, float, float] | None = None  # bid price, bid size, ask price, ask size
        self._prev_imb1 = 0.0
        self._depth = 0.0
        self._ofi_ema = 0.0
        self._tflow_ema = 0.0
        self._tvol_ema = 0.0
        self._ema16 = 0.0
        self._ema64 = 0.0
        self._vol = 0.0
        self._trades = [0.0, 0.0, 0.0, 0.0]  # buy volume, sell volume, buy value, sell value

    @property
    def ready(self) -> bool:
        return self.updates >= WARMUP_UPDATES

    def add_trades(self, ask_prices: Sequence[int], ask_volumes: Sequence[int], bid_prices: Sequence[int],
                   bid_volumes: Sequence[int]) -> None:
        """Fold in a trade ticks message for the future."""
        trades = self._trades
        for price, volume in zip(ask_prices, ask_volumes, strict=True):  # offers lifted: buyer was the aggressor
            trades[0] += volume
            trades[2] += price * volume
        for price, volume in zip(bid_prices, bid_volumes, strict=True):  # bids hit: seller was the aggressor
            trades[1] += volume
            trades[3] += price * volume

    def add_flow(self, buy_volume: float, sell_volume: float, buy_value: float, sell_value: float) -> None:
        """The same as ``add_trades`` for flow that has already been aggregated (a tape row)."""
        trades = self._trades
        trades[0] += buy_volume
        trades[1] += sell_volume
        trades[2] += buy_value
        trades[3] += sell_value

    def update(self, ask_prices: Sequence[int], ask_volumes: Sequence[int], bid_prices: Sequence[int],
               bid_volumes: Sequence[int]) -> np.ndarray | None:
        """Fold in an order book update for the future and return the feature vector.

        Returns ``None`` while warming up, or when one side of the book is empty
        (such an update is skipped entirely).
        """
        buy_volume, sell_volume, buy_value, sell_value = self._trades
        self._trades = [0.0, 0.0, 0.0, 0.0]

        bid, ask = float(bid_prices[0]), float(ask_prices[0])
        if bid <= 0.0 or ask <= 0.0:
            return None
        bid_size, ask_size = float(bid_volumes[0]), float(ask_volumes[0])
        tick = self.tick
        mid = (bid + ask) / 2.0
        first = self.updates == 0

        x = np.empty(N_FEATURES)
        x[0] = (ask - bid) / tick

        bid_depth = ask_depth = 0.0
        bid_value = ask_value = 0.0
        k = 1
        for level in range(5):
            bid_depth += bid_volumes[level]
            ask_depth += ask_volumes[level]
            bid_value += bid_prices[level] * bid_volumes[level]
            ask_value += ask_prices[level] * ask_volumes[level]
            if level in (0, 1, 2, 4):
                x[k] = (bid_depth - ask_depth) / (bid_depth + ask_depth)
                k += 1
        imb1 = x[1]
        x[5] = ((ask * bid_size + bid * ask_size) / (bid_size + ask_size) - mid) / tick
        x[6] = ((bid_value / bid_depth + ask_value / ask_depth) / 2.0 - mid) / tick

        touch = bid_size + ask_size
        self._depth = touch if first else self._depth + _alpha(40) * (touch - self._depth)
        if self._prev_touch is None:
            ofi = 0.0
            dbid = dask = 0.0
        else:
            prev_bid, prev_bid_size, prev_ask, prev_ask_size = self._prev_touch
            ofi = ((bid_size if bid >= prev_bid else 0.0) - (prev_bid_size if bid <= prev_bid else 0.0)
                   - (ask_size if ask <= prev_ask else 0.0) + (prev_ask_size if ask >= prev_ask else 0.0))
            ofi /= self._depth
            dbid = (bid - prev_bid) / tick
            dask = (ask - prev_ask) / tick
        self._ofi_ema = ofi if first else self._ofi_ema + _alpha(4) * (ofi - self._ofi_ema)
        x[7] = ofi
        x[8] = self._ofi_ema

        traded = buy_volume + sell_volume
        tflow = (buy_volume - sell_volume) / max(traded, 1.0)
        self._tflow_ema = tflow if first else self._tflow_ema + _alpha(4) * (tflow - self._tflow_ema)
        self._tvol_ema = traded if first else self._tvol_ema + _alpha(40) * (traded - self._tvol_ema)
        x[9] = tflow
        x[10] = self._tflow_ema
        x[11] = traded / max(self._tvol_ema, 1.0)
        x[12] = ((buy_value + sell_value) / traded - mid) / tick if traded > 0 else 0.0

        mids = self._mids
        mids.append(mid)
        n = len(mids)
        for slot, lag in ((13, 1), (14, 2), (15, 4), (16, 8)):
            x[slot] = (mid - mids[n - 1 - lag]) / tick if n > lag else 0.0

        self._ema16 = mid if first else self._ema16 + _alpha(16) * (mid - self._ema16)
        self._ema64 = mid if first else self._ema64 + _alpha(64) * (mid - self._ema64)
        x[17] = (mid - self._ema16) / tick
        x[18] = (mid - self._ema64) / tick

        move = abs(x[13])
        self._vol = move if first else self._vol + _alpha(20) * (move - self._vol)
        x[19] = self._vol

        x[20] = 0.0 if first else imb1 - self._prev_imb1
        x[21] = math.log((bid_size + 1.0) / self._depth)
        x[22] = math.log((ask_size + 1.0) / self._depth)
        x[23] = dbid
        x[24] = dask

        self._prev_touch = (bid, bid_size, ask, ask_size)
        self._prev_imb1 = imb1
        self.mid = mid
        self.updates += 1
        return x if self.updates >= WARMUP_UPDATES else None


def features_from_tape(tape: Tape) -> tuple[np.ndarray, np.ndarray]:
    """Run a tape through ``FeatureState``.

    Returns ``(X, mid)`` with one row per order book update. Rows where no
    vector was produced (warm-up, one-sided book) are NaN in ``X``; ``mid`` is
    in cents and NaN where the book was one-sided.
    """
    future = Instrument.FUTURE
    state = FeatureState()
    X = np.full((len(tape), N_FEATURES), np.nan)
    ask_prices = tape.ask_prices[:, future].tolist()
    ask_volumes = tape.ask_volumes[:, future].tolist()
    bid_prices = tape.bid_prices[:, future].tolist()
    bid_volumes = tape.bid_volumes[:, future].tolist()
    flows = np.column_stack([tape.buy_volume[:, future], tape.sell_volume[:, future],
                             tape.buy_value[:, future], tape.sell_value[:, future]]).tolist()
    for i in range(len(tape)):
        state.add_flow(*flows[i])
        vector = state.update(ask_prices[i], ask_volumes[i], bid_prices[i], bid_volumes[i])
        if vector is not None:
            X[i] = vector
    return X, tape.mid(future)
