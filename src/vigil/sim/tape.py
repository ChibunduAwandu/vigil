"""Recording what an auto-trader is shown.

``TapeRecorder`` is an auto-trader that never trades: it logs into the match
like any other team and writes down every order book update and trade tick it
receives. The result is, by construction, exactly the information a live
strategy has at each instant, which makes it the right input for research:
features built from a tape can be rebuilt tick by tick inside a running trader.
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from vigil.paths import artifacts_dir
from vigil.rtg import TOP_LEVEL_COUNT, BaseAutoTrader, Instrument, MarketEvent
from vigil.rtg.marketdata import file_sha256, load_market_events
from vigil.sim.engine import ExchangeConfig, Simulator

N_INSTRUMENTS = len(Instrument)


@dataclass(frozen=True)
class Tape:
    """The information feed of one session, tick by tick.

    Arrays indexed ``[tick, instrument, level]`` hold the five best prices
    (cents) and volumes per side at each order book update; zeros pad missing
    levels, as on the wire. ``buy_volume``/``sell_volume`` ``[tick, instrument]``
    are the lots traded since the previous update, split by which side was the
    aggressor, and ``buy_value``/``sell_value`` the matching price*volume sums.
    """

    times: np.ndarray
    ask_prices: np.ndarray
    ask_volumes: np.ndarray
    bid_prices: np.ndarray
    bid_volumes: np.ndarray
    buy_volume: np.ndarray
    sell_volume: np.ndarray
    buy_value: np.ndarray
    sell_value: np.ndarray

    def __len__(self) -> int:
        return len(self.times)

    def mid(self, instrument: int = Instrument.FUTURE) -> np.ndarray:
        """Mid price in cents; NaN where one side of the book is empty."""
        bid = self.bid_prices[:, instrument, 0].astype(float)
        ask = self.ask_prices[:, instrument, 0].astype(float)
        mid = (bid + ask) / 2.0
        mid[(bid == 0) | (ask == 0)] = np.nan
        return mid

    def save(self, path: str | os.PathLike) -> None:
        np.savez_compressed(path, **{name: getattr(self, name) for name in self.__dataclass_fields__})

    @classmethod
    def load(cls, path: str | os.PathLike) -> Tape:
        with np.load(path) as data:
            return cls(**{name: data[name] for name in cls.__dataclass_fields__})


class TapeRecorder(BaseAutoTrader):
    """A silent team that records the information feed."""

    def __init__(self, loop, team_name: str, secret: str):
        super().__init__(loop, team_name, secret)
        self._books: list[tuple] = []
        self._pending: list[tuple | None] = [None] * N_INSTRUMENTS
        self._flow = np.zeros((N_INSTRUMENTS, 4))  # buy volume, sell volume, buy value, sell value
        self._flows: list[np.ndarray] = []
        self._times: list[float] = []

    def on_order_book_update_message(self, instrument: int, sequence_number: int, ask_prices: Sequence[int],
                                     ask_volumes: Sequence[int], bid_prices: Sequence[int],
                                     bid_volumes: Sequence[int]) -> None:
        self._pending[instrument] = (ask_prices, ask_volumes, bid_prices, bid_volumes)
        if all(book is not None for book in self._pending):  # the ETF update closes the tick
            self._books.append(tuple(self._pending))
            self._flows.append(self._flow.copy())
            self._times.append(self.event_loop.time())
            self._pending = [None] * N_INSTRUMENTS
            self._flow[:] = 0.0

    def on_trade_ticks_message(self, instrument: int, sequence_number: int, ask_prices: Sequence[int],
                               ask_volumes: Sequence[int], bid_prices: Sequence[int],
                               bid_volumes: Sequence[int]) -> None:
        flow = self._flow[instrument]
        for price, volume in zip(ask_prices, ask_volumes, strict=True):  # lifted offers: the buyer was aggressive
            flow[0] += volume
            flow[2] += price * volume
        for price, volume in zip(bid_prices, bid_volumes, strict=True):  # hit bids: the seller was aggressive
            flow[1] += volume
            flow[3] += price * volume

    def tape(self) -> Tape:
        books = np.array(self._books, dtype=np.int64).reshape(-1, N_INSTRUMENTS, 4, TOP_LEVEL_COUNT)
        flows = np.array(self._flows).reshape(-1, N_INSTRUMENTS, 4)
        return Tape(
            times=np.array(self._times),
            ask_prices=books[:, :, 0], ask_volumes=books[:, :, 1],
            bid_prices=books[:, :, 2], bid_volumes=books[:, :, 3],
            buy_volume=flows[:, :, 0], sell_volume=flows[:, :, 1],
            buy_value=flows[:, :, 2], sell_value=flows[:, :, 3],
        )


# Bump when the simulator changes what an observer is shown, or when: tapes on disk are then stale.
TAPE_VERSION = 2


def record_tape(market_events: Sequence[MarketEvent], config: ExchangeConfig | None = None, seed: int = 0) -> Tape:
    """Replay a session with nobody trading and return what an observer saw.

    ``seed`` fixes the instants at which the exchange's timers fire, which is
    when the observer is shown the books and how stale they are.
    """
    sim = Simulator(market_events, config, record="none", seed=seed)
    recorder = sim.add_trader("Observer", TapeRecorder)
    sim.run()
    return recorder.tape()


def load_tape(market_data: str | os.PathLike, config: ExchangeConfig | None = None, *, cache: bool = True,
              seed: int = 0) -> Tape:
    """The tape of a market data file, cached on disk by content hash."""
    path = Path(market_data)
    config = config or ExchangeConfig()
    key = (f"{file_sha256(path)[:16]}-v{TAPE_VERSION}-{config.tick_interval:g}-{config.market_event_interval:g}"
           f"-{config.jitter:g}-{seed}")
    cached = artifacts_dir() / "cache" / f"tape-{key}.npz"
    if cache and cached.is_file():
        return Tape.load(cached)
    tape = record_tape(load_market_events(path), config, seed)
    if cache:
        cached.parent.mkdir(parents=True, exist_ok=True)
        tape.save(cached)
    return tape
