"""How long each trader takes from being handed an order book update to sending its first order.

    python scripts/handler_times.py [--session 3] [--seeds 3] [--challengers]

Several traders answer each update and the exchange serves their orders in the order they arrive,
so this is the number that decides who trades (docs/ARCHITECTURE.md, "Getting there first"). It is
measured in the simulator, in one process, in a match between the six presets and the example
trader, with logging set up as the exchange's launcher sets it up: the example trader writes a line
to its log on every update before it sends anything, and in a real match it pays for that.

An update is two books, the future's and then the ETF's, which the exchange publishes one after the
other. The time is taken from each: from the future's, the first to arrive, and from the ETF's, on
which base Vigil and its versions act. What a trader does with the future's book it does while the
exchange is still publishing the ETF's; the example trader acts on the future's book alone, and has
no time from the ETF's.
"""
from __future__ import annotations

import argparse
import logging
import statistics
import tempfile
import time
from pathlib import Path

from vigil.backtest import (
    EXAMPLE,
    example_trader_factory,
    measured_reactions,
    reaction_of,
    run_match,
    strategy_factory,
)
from vigil.paths import market_data_file
from vigil.rtg import Instrument
from vigil.strategies.challengers import CHALLENGERS
from vigil.strategies.ensemble import PRESETS


def timed(factory, taken: dict[str, list[int]]):
    """The same trader, noting the time from each update's books to its first message after them."""
    def make(loop, team_name, secret):
        trader = factory(loop, team_name, secret)
        state = {"update": None, "first": 0, "etf": None, "waiting": False}
        on_book, send = trader.on_order_book_update_message, trader.send_message

        def book(instrument, sequence_number, *levels):
            now = time.perf_counter_ns()
            if sequence_number != state["update"]:
                state.update(update=sequence_number, first=now, etf=None, waiting=True)
            if instrument == Instrument.ETF:
                state["etf"] = now
            on_book(instrument, sequence_number, *levels)

        def message(*args):
            if state["waiting"]:
                now = time.perf_counter_ns()
                taken["first"].append(now - state["first"])
                if state["etf"] is not None:
                    taken["etf"].append(now - state["etf"])
                state["waiting"] = False
            send(*args)

        trader.on_order_book_update_message, trader.send_message = book, message
        return trader
    return make


def measure(session: Path, seeds: int, challengers: bool = False) -> dict[str, dict[str, list[int]]]:
    measured = measured_reactions()
    teams = {"full": PRESETS["full"], **CHALLENGERS} if challengers else PRESETS
    taken: dict[str, dict[str, list[int]]] = {name: {"first": [], "etf": []} for name in (*teams, EXAMPLE)}
    field = {name: timed(strategy_factory(teams[name], session), taken[name]) for name in teams}
    field[EXAMPLE] = timed(example_trader_factory(), taken[EXAMPLE])
    reactions = {name: reaction_of(EXAMPLE if name == EXAMPLE else f"preset:{name}", measured)
                 or reaction_of("preset:full", measured) for name in field}
    for seed in range(seeds):
        run_match(session, field, seed=seed, reactions=reactions)
    return taken


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--session", default="3", help="a sample session (1 to 4) or a market data file")
    parser.add_argument("--seeds", type=int, default=3)
    parser.add_argument("--challengers", action="store_true", help="time base Vigil and the challengers instead")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as scratch:
        # What ready_trader_go.application sets up for a trader's process.
        logging.basicConfig(filename=str(Path(scratch) / "autotrader.log"), level=logging.INFO,
                            format="%(asctime)s [%(levelname)-7s] [%(name)s] %(message)s")
        taken = measure(market_data_file(args.session), args.seeds, args.challengers)
        logging.shutdown()
    print("microseconds from an order book update to the first order message\n")
    print(f"{'':16s}{'from the future book':>30s}{'from the ETF book':>20s}")
    print(f"{'trader':16s}{'median':>10s}{'10th pct':>10s}{'90th pct':>10s}{'median':>20s}{'updates answered':>18s}")
    for name, times in sorted(taken.items(), key=lambda item: statistics.median(item[1]["first"])):
        first, etf = sorted(times["first"]), times["etf"]
        from_etf = f"{statistics.median(etf) / 1000:.1f}" if len(etf) > len(first) // 2 else "-"
        print(f"{name:16s}{statistics.median(first) / 1000:10.1f}{first[len(first) // 10] / 1000:10.1f}"
              f"{first[len(first) * 9 // 10] / 1000:10.1f}{from_etf:>20s}{len(first):18,}")
