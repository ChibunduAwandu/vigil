"""The simulator must behave like the exchange it stands in for."""
from __future__ import annotations

import csv

import pytest
from exchange_kit import BUY, ETF, FAK, FUTURE, SELL, Watcher, insert, play, quiet_market
from exchange_kit import scripted as following

from vigil.backtest import example_trader_factory, run_match
from vigil.reports.canonical import EventStreamHasher
from vigil.rtg import MAXIMUM_ASK, MINIMUM_BID, BaseAutoTrader, Lifespan, Side
from vigil.rtg.events import read_match_events
from vigil.rtg.marketdata import load_market_events
from vigil.sim import ExchangeConfig, Simulator
from vigil.strategies.badactors import LimitBreaker, QuoteStuffer, Reckless

MAX_ASK = MAXIMUM_ASK // 100 * 100
MIN_BID = (MINIMUM_BID + 100) // 100 * 100


def scripted(messages):
    """A trader that re-sends recorded order messages at the instants the exchange processed them."""

    class Scripted(BaseAutoTrader):
        def __init__(self, loop, name, secret):
            super().__init__(loop, name, secret)
            for record in messages[name]:
                loop.call_at(record.time, self.play, record)

        def play(self, record):
            if record.op == "Insert":
                self.send_insert_order(record.order_id, Side[record.side], int(record.price), record.volume,
                                       Lifespan[record.lifespan])
            elif record.op == "Cancel":
                self.send_cancel_order(record.order_id)
            elif record.op == "Hedge":
                side = Side[record.side]
                self.send_hedge_order(record.order_id, side, MAX_ASK if side == Side.BUY else MIN_BID, record.volume)
    return Scripted


def test_replaying_a_real_match_reproduces_it_exactly(capture):
    """Feed the simulator the orders the traders sent to the real exchange: it must produce the same match.

    The fixture is the output of ``ready_trader_go.exchange`` itself. If the
    simulator's event loop ordered anything differently from the real one,
    a fill would go to a different order and the streams would part ways.
    """
    real = list(read_match_events(capture / "match_events.csv"))
    automatic = set()  # a fill-and-kill order's unfilled remainder is cancelled by the book, not by a message
    lifespans = {}
    messages: dict[str, list] = {}
    for record in real:
        if not record.competitor:
            continue
        if record.op == "Insert":
            lifespans[(record.competitor, record.order_id)] = record.lifespan
        if record.op == "Cancel" and lifespans.get((record.competitor, record.order_id)) == "F":
            automatic.add(record.seq)
            continue
        if record.op in ("Insert", "Cancel", "Hedge"):
            messages.setdefault(record.competitor, []).append(record)

    config = ExchangeConfig.from_json(capture / "exchange.json")
    sim = Simulator(load_market_events(capture / "market_data.csv"), config, record="all")
    for name in sorted(messages):
        sim.add_trader(name, scripted(messages))
    result = sim.run()
    replayed = [tuple(r.to_row()) for r in result.records()]
    original = [tuple(r.to_row()) for r in real]

    # The real match ran a fraction of a second past the end of the data (its ticks have jitter),
    # so it has a few trailing events the replay stops short of. Everything before must be identical.
    assert len(original) - len(replayed) < 100
    assert replayed == original[:len(replayed)]
    assert len(replayed) > 10_000

    # And so must the exchange's own arithmetic: positions, fees and score at the same instant.
    with open(capture / "score_board.csv", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row["Operation"] == "Tick"]
    for name, team in result.teams.items():
        at_the_end = [row for row in rows if row["Team"] == name and float(row["Time"]) <= result.duration + 0.3]
        last = at_the_end[-1]
        assert (team.etf_position, team.future_position) == (int(last["EtfPosition"]), int(last["FuturePosition"]))
        assert round(team.total_fees * 100) == int(last["TotalFees"])


def test_same_inputs_same_match(session_one):
    digests = []
    for _ in range(2):
        result, _ = run_match(session_one, {"Example": example_trader_factory()}, record="all", end_time=30.0)
        hasher = EventStreamHasher()
        for record in result.records():
            hasher.update(record)
        digests.append((hasher.hexdigest(), result["Example"].pnl))
    assert digests[0] == digests[1]


def test_example_trader_plays_a_clean_match(session_one):
    result, _ = run_match(session_one, {"Example": example_trader_factory()}, end_time=60.0)
    team = result["Example"]
    assert team.status == "OK"
    assert team.traded_volume > 0
    assert team.hedges == team.fills  # it hedges every fill
    # The first tick is at the open and the match ends at the first one past the end time; each
    # comes within a fifth of an interval of when it is due.
    assert result.ticks in (241, 242) and 60.0 <= result.duration < 60.31

    on_the_grid, _ = run_match(session_one, {"Example": example_trader_factory()}, end_time=60.0,
                               exchange=ExchangeConfig(jitter=0.0))
    assert on_the_grid.ticks == 241 and on_the_grid.duration == pytest.approx(60.0)


def test_position_limit_breach_is_enforced(session_one):
    result, _ = run_match(session_one, {"Breaker": LimitBreaker}, end_time=20.0)
    team = result["Breaker"]
    assert team.status == "BREACH"
    assert team.breach_reason == "ETF position limit breached"
    assert abs(team.etf_position) > 100


def test_unhedged_lots_clock_runs_on_simulated_time(session_one):
    result, _ = run_match(session_one, {"Reckless": Reckless}, end_time=80.0)
    team = result["Reckless"]
    assert team.status == "BREACH"
    assert team.breach_reason == "held unhedged lots for longer than the time limit"
    assert team.breach_time == pytest.approx(70.0, abs=0.3)  # bought at 10 s, allowed 60 s


def test_message_rate_limit_is_enforced(session_one):
    class Flooder(BaseAutoTrader):
        def on_order_book_update_message(self, instrument, sequence_number, *book):
            if instrument == 1 and sequence_number == 8:
                for order_id in range(1, 61):
                    self.send_cancel_order(order_id)

    result, _ = run_match(session_one, {"Flooder": Flooder}, end_time=5.0)
    assert result["Flooder"].status == "BREACH"
    assert result["Flooder"].breach_reason == "message frequency limit breached"


def test_a_trader_that_counts_its_messages_can_stay_under_the_limit(session_one):
    """The books come out on a jittered timer, so five updates can fall within one second.

    Ten messages an update is then fifty, and one hedge more is a breach: the
    quote stuffer has to keep count to stay in the match, as it is meant to.
    """
    for seed in range(3):
        result, _ = run_match(session_one, {"Stuffer": QuoteStuffer, "Example": example_trader_factory()},
                              seed=seed, end_time=60.0, record="all")
        assert result["Stuffer"].status == "OK"
        sent = [r.time for r in result.records()
                if r.competitor == "Stuffer" and r.op in ("Insert", "Cancel", "Amend", "Hedge")]
        busiest = max(sum(1 for other in sent[i:i + 50] if other < time + 1.0) for i, time in enumerate(sent))
        assert 40 <= busiest <= 49  # close to the limit of fifty, and never at it


# -- timing ------------------------------------------------------------------------------------------

def test_the_first_books_are_published_as_the_market_opens():
    market = [insert(0.0, FUTURE, BUY, 100, 99900), insert(0.0, FUTURE, SELL, 100, 100000), *quiet_market()]
    _, traders = play(market, {"W": Watcher}, until=2.0)
    watcher = traders["W"]
    assert watcher.at[1] < 1e-3  # the first update comes at the open, not an interval after it
    assert watcher.shown[(1, FUTURE)] == (99900, 100000)  # and shows the orders the session opens with
    assert watcher.at[2] == pytest.approx(0.25, abs=0.2 * 0.25)  # later ones within a fifth of an interval of due


def test_a_tick_publishes_the_books_as_the_market_timer_left_them():
    """The exchange brings its books up to date on one timer and publishes them on another.

    With the market timer every tenth of a second and nothing else going on,
    an order that arrives at 0.22 s is not in the books published at 0.25 s:
    the timer that would have put it there last fired at 0.2 s.
    """
    config = ExchangeConfig(market_event_interval=0.1, jitter=0.0)
    market = [*quiet_market(bid=99800), insert(0.22, FUTURE, BUY, 5, 99900)]
    _, traders = play(market, {"W": Watcher}, until=2.0, config=config)
    shown = traders["W"].shown
    assert shown[(2, FUTURE)][0] == 99800 and shown[(3, FUTURE)][0] == 99900

    # A message from a trader does bring the books up to date (the exchange processes market
    # data before it acts on one), so a busy match sees fresher books than a quiet one.
    class Prodder(BaseAutoTrader):
        def __init__(self, loop, name, secret):
            super().__init__(loop, name, secret)
            loop.call_at(0.24, self.send_cancel_order, 1)  # a message about nothing, just before the tick

    _, traders = play(market, {"W": Watcher, "Prodder": Prodder}, until=2.0, config=config)
    assert traders["W"].shown[(2, FUTURE)][0] == 99900


def test_a_match_with_nobody_left_in_it_ends(session_one):
    result, _ = run_match(session_one, {"Breaker": LimitBreaker}, end_time=60.0)
    team = result["Breaker"]
    assert team.status == "BREACH"
    # The exchange stops at the next tick once its last competitor has gone; it does not play on.
    assert team.breach_time < result.duration < team.breach_time + 0.31


def test_the_reason_for_a_breach_is_known_when_the_market_causes_it():
    # Two resting bids of sixty lots are both filled by one order from the market: 120 lots, over the limit.
    # The breach is dated by the market's order; the exchange only gets round to saying so later.
    market = [*quiet_market(bid=99800), insert(3.013, ETF, SELL, 120, 99900, FAK)]
    rest = following({8: [lambda trader: trader.buy(99900, 60), lambda trader: trader.buy(99900, 60)]})
    result, _ = play(market, {"C": rest, "Bystander": following({})})
    team = result["C"]
    assert team.status == "BREACH" and team.breach_reason == "ETF position limit breached"
    assert team.breach_time == pytest.approx(3.013) and team.etf_position == 120


def test_the_faster_of_two_traders_gets_the_customer():
    # Both bid the same price on the same update; one customer then sells ten lots at it.
    market = [*quiet_market(bid=99800), insert(3.0, ETF, SELL, 10, 99900, FAK)]
    bid = following({8: [lambda trader: trader.buy(99900, 10)]})
    for fast, slow in (("A", "B"), ("B", "A")):
        result, _ = play(market, {"A": bid, "B": bid}, reactions={fast: (0.0005, 0.0), slow: (0.001, 0.0)})
        assert (result[fast].etf_position, result[slow].etf_position) == (10, 0)
    # With no reaction times the two arrive together and a coin decides: over seeds, each wins some.
    winners = {max(("A", "B"), key=lambda name: play(market, {"A": bid, "B": bid}, seed=seed)[0][name].etf_position)
               for seed in range(12)}
    assert winners == {"A", "B"}


def test_a_traders_messages_reach_the_exchange_in_the_order_it_sent_them():
    # An insert and its cancel, sent in one burst, with a reaction time that varies from burst to burst.
    def quote_and_pull(trader):
        trader.send_cancel_order(trader.buy(99900, 10))

    script = following({update: [quote_and_pull] for update in range(8, 60)})
    result, _ = play(quiet_market(bid=99800), {"C": script}, reactions={"C": (0.001, 0.0008)})
    team = result["C"]
    assert team.inserts == team.cancels == 52 and not team.errors  # no cancel ever overtook its order


def test_a_trader_whose_message_breaks_the_exchanges_handler_is_disconnected():
    """The exchange's hedge handler fails if the ETF has no price yet. asyncio then drops the connection."""
    market = [insert(0.01, FUTURE, BUY, 1000, 99900), insert(0.01, FUTURE, SELL, 1000, 100000)]
    result, _ = play(market, {"C": following({4: [lambda trader: trader.hedge(BUY, 100000, 5)]}),
                              "Bystander": following({})})
    team = result["C"]
    assert any("exchange handler failed" in text for _, _, text in team.errors)
    assert team.future_position == 5 and team.hedges == 1  # the hedge was booked before the handler fell over


def test_orders_cannot_be_sent_before_joining(session_one):
    sim = Simulator(load_market_events(session_one), end_time=1.0)
    sim.add_trader("A", example_trader_factory())
    with pytest.raises(ValueError):
        sim.add_trader("A", example_trader_factory())
    sim.run()
    with pytest.raises(RuntimeError):
        sim.run()
