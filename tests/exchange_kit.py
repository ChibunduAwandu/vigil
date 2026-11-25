"""Hand-made markets and scripted traders, played on the exchange's own classes through the simulator.

For the cases the sample sessions never produce: a team thrown out with orders
resting, a future that has never traded, an order number used twice. What
comes out is a real event stream, made by the exchange's matching and risk
code, that the surveillance engine can be held to.
"""
from __future__ import annotations

import itertools
from collections.abc import Callable, Iterable, Mapping

from vigil.pipeline import MatchAudit, audit_records
from vigil.reports.builder import outcomes_from_simulation
from vigil.rtg import BaseAutoTrader, Instrument, Lifespan, MarketEvent, MarketEventOperation, Side
from vigil.rtg.events import MatchRecord
from vigil.sim.engine import ExchangeConfig, Simulator
from vigil.sim.result import MatchResult
from vigil.surveillance.engine import header_for

FUTURE, ETF = Instrument.FUTURE, Instrument.ETF
BUY, SELL = Side.BUY, Side.SELL
GFD, FAK = Lifespan.GOOD_FOR_DAY, Lifespan.FILL_AND_KILL

_ids = itertools.count(1000)


def insert(time: float, instrument: Instrument, side: Side, volume: int, price: int, lifespan: Lifespan = GFD,
           order_id: int | None = None) -> MarketEvent:
    """An order from the replayed market. Prices are in cents."""
    return MarketEvent(time, instrument, MarketEventOperation.INSERT, order_id if order_id is not None else next(_ids),
                       side, volume, price, lifespan)


def cancel(time: float, instrument: Instrument, order_id: int) -> MarketEvent:
    return MarketEvent(time, instrument, MarketEventOperation.CANCEL, order_id, None, 0, 0, None)


def amend(time: float, instrument: Instrument, order_id: int, change: int) -> MarketEvent:
    return MarketEvent(time, instrument, MarketEventOperation.AMEND, order_id, None, change, 0, None)


def quiet_market(bid: int = 99900, ask: int = 100000, depth: int = 1000) -> list[MarketEvent]:
    """Both books two-sided at the same prices, and one trade in the future so that it has a last price."""
    return [
        insert(0.01, FUTURE, BUY, 100 * depth, bid), insert(0.01, FUTURE, SELL, 100 * depth, ask),
        insert(0.02, FUTURE, BUY, 1, ask, FAK),
        insert(0.03, ETF, BUY, depth, bid), insert(0.03, ETF, SELL, depth, ask),
    ]


class Scripted(BaseAutoTrader):
    """Does what its script says on each order book update: ``{update number: [action(trader), ...]}``."""

    SCRIPT: Mapping[int, Iterable[Callable]] = {}

    def __init__(self, loop, team_name: str, secret: str):
        super().__init__(loop, team_name, secret)
        self.ids = itertools.count(1)
        self.heard: list[tuple] = []

    def on_order_book_update_message(self, instrument, sequence_number, ask_prices, ask_volumes, bid_prices,
                                     bid_volumes) -> None:
        if instrument == ETF:
            for action in self.SCRIPT.get(sequence_number, ()):
                action(self)

    def on_error_message(self, client_order_id: int, error_message: bytes) -> None:
        self.heard.append(("error", client_order_id, error_message))

    def on_order_filled_message(self, client_order_id: int, price: int, volume: int) -> None:
        self.heard.append(("fill", client_order_id, price, volume))

    def on_hedge_filled_message(self, client_order_id: int, price: int, volume: int) -> None:
        self.heard.append(("hedge", client_order_id, price, volume))

    # Shorthand for scripts.

    def buy(self, price: int, volume: int, lifespan: Lifespan = GFD) -> int:
        order_id = next(self.ids)
        self.send_insert_order(order_id, BUY, price, volume, lifespan)
        return order_id

    def sell(self, price: int, volume: int, lifespan: Lifespan = GFD) -> int:
        order_id = next(self.ids)
        self.send_insert_order(order_id, SELL, price, volume, lifespan)
        return order_id

    def hedge(self, side: Side, price: int, volume: int) -> int:
        order_id = next(self.ids)
        self.send_hedge_order(order_id, side, price, volume)
        return order_id


def scripted(script: Mapping[int, Iterable[Callable]]) -> type[Scripted]:
    return type("ScriptedTrader", (Scripted,), {"SCRIPT": dict(script)})


class Watcher(BaseAutoTrader):
    """Sends nothing; keeps the best bid and offer of each book as it is shown them, by update number."""

    def __init__(self, loop, team_name: str, secret: str):
        super().__init__(loop, team_name, secret)
        self.shown: dict[tuple[int, int], tuple[int, int]] = {}  # (update, instrument) -> (best bid, best ask)
        self.at: dict[int, float] = {}

    def on_order_book_update_message(self, instrument, sequence_number, ask_prices, ask_volumes, bid_prices,
                                     bid_volumes) -> None:
        self.shown[(sequence_number, int(instrument))] = (bid_prices[0], ask_prices[0])
        self.at[sequence_number] = self.event_loop.time()


def play(market: Iterable[MarketEvent], traders: Mapping[str, Callable], *, until: float = 20.0,
         config: ExchangeConfig | None = None, seed: int = 0,
         reactions: Mapping[str, tuple[float, float]] | None = None,
         ) -> tuple[MatchResult, dict[str, BaseAutoTrader]]:
    """Play a match on hand-made market data, which is kept going until ``until`` seconds."""
    events = sorted([*market, insert(until, FUTURE, BUY, 1, 1)], key=lambda event: event.time)
    simulator = Simulator(events, config, seed=seed, record="all")
    objects = {name: simulator.add_trader(name, factory, reaction=(reactions or {}).get(name))
               for name, factory in traders.items()}
    return simulator.run(), objects


def audit(result: MatchResult | Iterable[MatchRecord], teams: Iterable[str] = (), *,
          config: ExchangeConfig | None = None, check_score: bool = False, **header) -> MatchAudit:
    """Run surveillance over a played match (or any stream of records)."""
    config = config or ExchangeConfig()
    records = result.records() if isinstance(result, MatchResult) else result
    names = list(teams) or (list(result.teams) if isinstance(result, MatchResult) else [])
    outcomes = outcomes_from_simulation(result) if check_score and isinstance(result, MatchResult) else None
    return audit_records(header_for("kit", "sim", teams=names, limits=config.limits(), fees=config.fees(),
                                    instrument=config.instrument(), **header), records, outcomes=outcomes)
