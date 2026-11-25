from __future__ import annotations

import math

import numpy as np
import pytest

from vigil.backtest import ablation, example_trader_factory, run_match, strategy_factory, tournament
from vigil.rtg import Instrument, Side, messages
from vigil.sim import ExchangeConfig
from vigil.strategies.ensemble import PRESETS, EnsembleTrader, StrategyConfig
from vigil.strategies.inventory import best_block


@pytest.mark.parametrize("preset", list(PRESETS))
def test_every_preset_trades_within_the_rules(session_one, preset):
    result, traders = run_match(session_one, {preset: strategy_factory(PRESETS[preset], session_one)}, end_time=120.0)
    team = result[preset]
    assert team.status == "OK" and not team.errors
    assert team.pnl > 0
    assert abs(team.etf_position) <= 100 and abs(team.future_position) <= 100
    stats = traders[preset].stats
    assert stats["errors"] == 0
    assert stats["quote_lots"] > 0


def test_presets_differ_by_one_component_each():
    names = list(PRESETS)
    changed = []
    for before, after in zip(names, names[1:], strict=False):
        a, b = PRESETS[before], PRESETS[after]
        changed.append({f for f in a.__dataclass_fields__ if getattr(a, f) != getattr(b, f)})
    assert changed == [{"inventory_control"}, {"mean_reversion"}, {"hedging"}, {"alpha"}, {"alpha"}]
    assert PRESETS["full"] == StrategyConfig()


def test_immediate_hedging_leaves_nothing_unhedged(session_one):
    result, _ = run_match(session_one, {"T": strategy_factory(PRESETS["mean_reversion"], session_one)}, end_time=120.0)
    team = result["T"]
    assert (team.etf_series + team.future_series == 0).all()


def test_kelly_hedging_carries_inventory_and_hedges_less(session_one):
    immediate, a = run_match(session_one, {"T": strategy_factory(PRESETS["mean_reversion"], session_one)}, end_time=300.0)
    kelly, b = run_match(session_one, {"T": strategy_factory(PRESETS["kelly"], session_one)}, end_time=300.0)
    assert b["T"].stats["hedge_lots"] < 0.6 * a["T"].stats["hedge_lots"]
    unhedged = abs(kelly["T"].etf_series + kelly["T"].future_series)
    assert unhedged.max() > 10  # it does go beyond the free allowance ...
    assert kelly["T"].status == "OK"  # ... and is never caught by the exchange's clock


class Wire:
    """Stands in for a trader's connection to the exchange: keeps what is written to it."""

    def __init__(self):
        self.sent: list[tuple[int, tuple]] = []

    def write(self, data: bytes) -> None:
        length, kind = messages.HEADER.unpack_from(data)
        layout = {messages.MessageType.HEDGE_ORDER: messages.HEDGE_MESSAGE,
                  messages.MessageType.INSERT_ORDER: messages.INSERT_MESSAGE,
                  messages.MessageType.CANCEL_ORDER: messages.CANCEL_MESSAGE,
                  messages.MessageType.AMEND_ORDER: messages.AMEND_MESSAGE}[kind]
        self.sent.append((kind, layout.unpack(data[messages.HEADER_SIZE:length])))

    def is_closing(self) -> bool:
        return False

    def hedges(self) -> list[int]:
        """Signed lots of each hedge order sent: positive buys the future."""
        return [volume if side == Side.BUY else -volume
                for kind, (_, side, _, volume) in ((k, body) for k, body in self.sent
                                                   if k == messages.MessageType.HEDGE_ORDER)]


class Clock:
    """An event loop that does nothing but tell a time that the test sets."""

    def __init__(self):
        self.now = 0.0

    def time(self) -> float:
        return self.now


@pytest.fixture
def idle_trader():
    """A trader connected to a wire that keeps what it sends, on a clock the test controls."""
    def make(preset, **settings):
        trader = EnsembleTrader(Clock(), "T", "secret", PRESETS[preset].with_(**settings))
        trader._connection_transport = trader.wire = Wire()
        return trader
    return make


def _book(asks, bids):
    pad = lambda levels: tuple(levels) + (0,) * (5 - len(levels))  # noqa: E731
    return pad([p for p, _ in asks]), pad([v for _, v in asks]), pad([p for p, _ in bids]), pad([v for _, v in bids])


def test_a_one_sided_future_book_leaves_only_the_free_allowance_unhedged(idle_trader):
    trader = idle_trader("kelly")
    allowance = float(trader.config.free_unhedged_lots)
    # With both sides there is a spread to save, and the band scales with it.
    trader._future = _book([(150100, 500)], [(150000, 500)])
    one_tick = trader._band()
    trader._future = _book([(150400, 500)], [(150000, 500)])
    four_ticks = trader._band()
    assert one_tick[0] == -one_tick[1] < 0 and four_ticks[1] == 4 * one_tick[1]
    assert one_tick[2] == four_ticks[2] == trader.config.carry_lots
    # With one side empty there is no spread to price the bet with.
    for book in (_book([], [(150000, 500)]), _book([(150100, 500)], [])):
        trader._future = book
        assert trader._band() == (-allowance, allowance, allowance)


@pytest.mark.parametrize("concave", [True, False])
def test_the_search_for_what_to_take_finds_what_a_full_search_finds(idle_trader, concave):
    """``_find_take`` stops early where it can. That must never change which block is taken."""
    rng = np.random.default_rng(7)
    for preset in ("baseline", "mean_reversion"):  # without and with a price on inventory
        trader = idle_trader(preset)
        assert trader._concave  # both tables are, which is what lets the search stop at the first loss
        trader._concave = concave  # and told otherwise, it must plod through every lot to the same answer
        limit, tick = trader.config.position_limit, trader.config.tick
        for _ in range(1500):
            mid = 150000 + 50
            trader.etf_position = int(rng.integers(-limit, limit + 1))
            offset = int(rng.integers(0, 40))
            sign = int(rng.choice([-1, 1]))
            # Levels run from rich to hopeless, best first, as a side of the book does.
            start = mid + sign * int(rng.integers(-2, 9)) * tick
            levels = [(start - sign * k * tick, int(rng.integers(1, 80))) for k in range(8)]
            hedge_price = mid + sign * 50
            margin = float(rng.uniform(0, 150))
            took = trader._find_take(levels, sign, hedge_price, mid, offset, margin)

            prices, edges = [], []  # every lot on offer, with no cut-off
            for price, volume in levels:
                fee = trader.config.taker_fee * price
                for _ in range(volume):
                    score = sign * (price - (mid if len(edges) < offset else hedge_price)) - fee - margin
                    edges.append(score / 100.0)
                    prices.append(price)
            edges, prices = edges[:2 * limit], prices[:2 * limit]
            lots = best_block(np.array(edges), trader._values, trader.etf_position, -sign, limit)
            assert took == ((prices[lots - 1], lots) if lots > 0 else None)
        assert Side.BUY in trader._quotes  # the trader was never connected; nothing was sent


def _score_every_price(trader, levels, sign, floor, fair, hedge_price, bonus):
    """Quote pricing as its docstring describes it, written the slow and obvious way."""
    cfg = trader.config
    tick = cfg.tick
    wall = next((price for price, volume in levels if volume >= cfg.wall_lots), None)
    if wall is not None:
        if sign * (floor - wall) >= 0:
            return None
        widest = wall - sign * tick
    else:
        widest = int(round(fair / tick)) * tick + sign * cfg.default_offset_ticks * tick
    widest = floor if sign * (widest - floor) < 0 else widest
    own = trader._quotes[Side.SELL if sign > 0 else Side.BUY]
    best, best_value, price = None, 0.0, floor
    while sign * (widest - price) >= 0:
        reach = max(cfg.customer_range_ticks - sign * (price - fair) / tick, 0.0)
        ahead = sum(volume for level, volume in levels
                    if sign * (price - level) > 0 or (level == price and (own is None or own.price != price)))
        edge = sign * (price - hedge_price) - cfg.maker_fee * price + bonus
        value = reach * math.exp(-ahead / cfg.customer_mean_lots) * edge
        if value >= best_value and edge > 0:
            best, best_value = price, value
        price += sign * tick
    return None if best is None or best <= 0 else (best, cfg.quote_lots)


def test_quote_pricing_finds_what_scoring_every_price_finds(idle_trader):
    from vigil.strategies.ensemble import _Order
    rng = np.random.default_rng(11)
    trader = idle_trader("kelly")
    tick = trader.config.tick
    for _ in range(3000):
        sign = int(rng.choice([-1, 1]))
        fair = 150000 + int(rng.integers(0, 2)) * 50
        # A side of the book, best price first, with or without a standing market in it.
        start = int(fair // tick) * tick + sign * int(rng.integers(0, 4)) * tick
        gaps = np.cumsum(rng.integers(1, 4, size=int(rng.integers(0, 6))))
        levels = [(start + sign * int(gap) * tick, int(rng.choice([3, 20, 60, 400, 1000, 10000]))) for gap in gaps]
        side = Side.SELL if sign > 0 else Side.BUY
        trader._quotes[side] = None
        if levels and rng.random() < 0.5:  # sometimes we already rest at one of those prices
            trader._quotes[side] = _Order(1, side, levels[int(rng.integers(len(levels)))][0], 50, True)
        floor = int(fair // tick) * tick + sign * int(rng.integers(-1, 12)) * tick
        hedge_price = fair + sign * int(rng.integers(0, 3)) * 50
        bonus = float(rng.uniform(-150, 300))
        assert trader._price_quote(levels, sign, floor, fair, hedge_price, bonus) == _score_every_price(
            trader, levels, sign, floor, fair, hedge_price, bonus)


def _show(trader, future, etf=None, update=None):
    """Hand the trader an order book update for the future, then one for the ETF."""
    update = update if update is not None else trader._seq + 1
    trader.on_order_book_update_message(Instrument.FUTURE, update, *future)
    trader.on_order_book_update_message(Instrument.ETF, update, *(etf or _book([], [])))


def test_the_future_limit_holds_even_if_a_hedge_in_flight_does_not_fill(idle_trader):
    trader = idle_trader("mean_reversion")  # hedges everything at once
    future = _book([(100000, 5000)], [(99900, 5000)])
    trader.etf_position, trader.future_position = 60, -60
    _show(trader, future)
    assert trader.wire.hedges() == []  # flat: nothing to do

    # A buy of 100 futures is in flight (we had just sold 100 ETF lots, say) when we buy 200.
    trader._hedges[999] = 100
    trader._hedge_pending = 100
    trader.etf_position = 160  # not a position the exchange allows, but the arithmetic must hold anyway
    trader._hedge()
    # Net of the pending buy we are 200 long and would sell 200. If that buy comes back unfilled
    # the future is still at -60, so no more than 40 may be sold: -60 - 40 = -100.
    assert trader.wire.hedges() == [-40]
    trader.on_hedge_filled_message(999, 0, 0)  # and it does come back unfilled
    assert trader.future_position == -60 and trader._hedge_pending == -40


def test_a_hedge_that_finds_nothing_to_trade_is_not_sent_again_at_once(idle_trader):
    trader = idle_trader("mean_reversion")
    future = _book([(100000, 5000)], [(99900, 5000)])
    _show(trader, future)
    trader.etf_position = 30
    trader._hedge()
    (order_id,) = trader._hedges
    assert trader.wire.hedges() == [-30]
    trader.on_hedge_filled_message(order_id, 0, 0)  # nothing on the other side of the future
    assert trader.wire.hedges() == [-30] and trader.stats["hedges_unfilled"] == 1
    trader._hedge()
    assert trader.wire.hedges() == [-30]  # still the same order book update: asking again changes nothing
    _show(trader, future)
    assert trader.wire.hedges() == [-30, -30]  # the next update brings the retry


def test_inventory_is_hedged_even_when_the_future_shows_one_side_only(idle_trader):
    trader = idle_trader("kelly")
    _show(trader, _book([(100000, 5000)], [(99900, 5000)]))
    trader.etf_position = 50
    before = len(trader.wire.sent)
    _show(trader, _book([], [(99900, 5000)]))  # the future's offers have gone
    # No spread to size a bet with, so only the free allowance is carried; the rest is sold.
    assert trader.wire.hedges() == [-40]
    # And with no fair value to price against, no new order is put into the ETF.
    assert not any(kind == messages.MessageType.INSERT_ORDER for kind, _ in trader.wire.sent[before:])


@pytest.mark.parametrize("speed, window", [(1.0, 1.0), (2.0, 0.25), (0.5, 4.0)])
def test_the_message_governor_uses_the_exchanges_window_at_any_speed(idle_trader, speed, window):
    """The exchange counts over its interval divided by its speed, in match time; on the wall clock that is
    the interval over the speed squared."""
    trader = idle_trader("full", alpha="none", speed=speed)
    clock = trader.event_loop
    allowed = sum(trader._may_send(priority=True) for _ in range(60))
    assert allowed == trader.config.message_limit - 2
    clock.now = 0.99 * window
    assert not trader._may_send(priority=True)  # the window has not passed: still full
    clock.now = 1.01 * window
    assert trader._may_send(priority=True)


def test_a_strategy_is_told_the_limits_of_the_exchange_it_trades_on(session_one):
    tight = ExchangeConfig(position_limit=50)
    result, traders = run_match(session_one, {"T": strategy_factory(PRESETS["full"], session_one, tight)},
                                exchange=tight, end_time=120.0)
    team = result["T"]
    assert team.status == "OK" and not team.errors and traders["T"].config.position_limit == 50
    assert 30 < max(abs(team.etf_series)) <= 50 and max(abs(team.future_series)) <= 50


def test_strategies_survive_each_other(session_one):
    traders = {name: strategy_factory(PRESETS[name], session_one) for name in ("full", "kelly", "baseline")}
    traders["example"] = example_trader_factory()
    result, _ = run_match(session_one, traders, end_time=200.0, seed=5)
    assert all(team.status == "OK" for team in result.teams.values())
    assert result["full"].pnl > result["example"].pnl


def test_tournament_is_played_over_seeds_and_says_how_much_is_luck(session_one):
    reactions = {"example": (0.0005, 0.00005), **{f"preset:{name}": (0.0008, 0.0002) for name in PRESETS}}
    report = tournament([session_one], seeds=(0, 1, 2), reactions=reactions,
                        exchange=None, presets={name: PRESETS[name] for name in ("baseline", "full")})
    assert report["seeds"] == [0, 1, 2] and report["reaction_times"] is True
    rows = {row["team"]: row for row in report["rows"]}
    assert set(rows) == {"baseline", "full", "example (Optiver)"}
    for row in rows.values():
        assert len(row["runs"]["market_data1.csv"]) == 3 and len(row["ranks"]) == 3 and row["breaches"] == 0
        assert row["std_over_seeds"] > 0  # the seed changes who is first to the customer
    assert rows["example (Optiver)"]["mean_pnl"] < min(rows["baseline"]["mean_pnl"], rows["full"]["mean_pnl"])


@pytest.mark.slow
def test_ablation_reports_every_preset(session_one):
    report = ablation([session_one], seeds=(0, 1))
    assert [row["strategy"] for row in report["rows"]] == list(PRESETS)
    assert all(row["breaches"] == 0 and row["errors"] == 0 for row in report["rows"])
    full, baseline = report["rows"][-1], report["rows"][0]
    assert len(full["pnl"]) == 1 and len(full["pnl_by_run"]) == 2  # one session, played under two seeds
    assert full["pnl"][0] == pytest.approx(sum(full["pnl_by_run"]) / 2, abs=0.01)
    assert full["mean_pnl"] > baseline["mean_pnl"]
    assert full["lots_per_session"]["hedge_lots"] < baseline["lots_per_session"]["hedge_lots"]
    versus = full["vs_baseline"]
    assert versus["runs"] == 2 and 0 <= versus["runs_ahead"] <= 2
    assert versus["mean_difference"] == pytest.approx(full["mean_pnl"] - baseline["mean_pnl"], rel=0.02)
