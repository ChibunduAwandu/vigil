"""The challengers: experimental versions of Vigil, each with one part replaced."""
from __future__ import annotations

import functools
import json
import math
import random

import numpy as np
import pytest

from vigil.backtest import run_match, strategy_factory
from vigil.rtg import Instrument, Side
from vigil.strategies.challengers import CHALLENGERS, STRATEGIES, ChallengerParams, params_for
from vigil.strategies.challengers.glft import glft_offsets
from vigil.strategies.challengers.hawkes import hawkes_from_flows
from vigil.strategies.challengers.ou import reversion_from_mids
from vigil.strategies.challengers.rl import ACTIONS, N_STATES, NEUTRAL, QPolicy
from vigil.strategies.challengers.skew import SkewFastTrader, SkewTrader
from vigil.strategies.ensemble import PRESETS, trader_class


@pytest.fixture
def unfitted(tmp_path):
    """Challenger settings that read no fitted parameters: each runs on its own defaults."""
    return lambda name: CHALLENGERS[name].with_(challenger_path=str(tmp_path / "none.json"))


@pytest.mark.parametrize("name", list(CHALLENGERS))
def test_every_challenger_trades_within_the_rules(session_one, unfitted, name):
    config = unfitted(name)
    result, traders = run_match(session_one, {name: strategy_factory(config, session_one)}, end_time=120.0)
    team = result[name]
    assert type(traders[name]).__name__.lower().startswith(name.replace("_", ""))
    assert team.status == "OK" and not team.errors
    assert abs(team.etf_position) <= 100 and abs(team.future_position) <= 100
    assert traders[name].stats["errors"] == 0 and traders[name].stats["quote_lots"] > 0


def test_every_challenger_can_be_entered_as_a_team(monkeypatch):
    from vigil.live import KINDS
    from vigil.strategies.ensemble import AutoTrader, load_config
    assert {f"preset:{name}" for name in CHALLENGERS} <= set(KINDS)
    assert set(PRESETS) <= set(STRATEGIES)
    monkeypatch.setenv("VIGIL_STRATEGY", "glft")
    assert load_config().engine == "glft"

    class Loop:
        def time(self):
            return 0.0

    assert type(AutoTrader(Loop(), "T", "secret")).__name__ == "GLFTTrader"
    monkeypatch.setenv("VIGIL_STRATEGY", "full")
    assert type(AutoTrader(Loop(), "T", "secret")).__name__ == "EnsembleTrader"
    with pytest.raises(ValueError, match="unknown engine"):
        trader_class(PRESETS["full"].with_(engine="nonsense"))


def test_a_challenger_trades_with_the_parameters_that_never_saw_its_session(tmp_path):
    path = tmp_path / "challengers.json"
    ChallengerParams(full={"skew": {"weight": 9.0}}, folds={"held": {"skew": {"weight": 2.0}}}).save(path)
    config = CHALLENGERS["skew"].with_(challenger_path=str(path))
    assert params_for(config.with_(holdout="held"))["skew"]["weight"] == 2.0
    assert params_for(config.with_(holdout="other"))["skew"]["weight"] == 9.0  # a session no fold left out
    assert params_for(config.with_(challenger_path=str(tmp_path / "missing.json"))) == {}
    assert json.loads(path.read_text())["folds"]["held"]["skew"]["weight"] == 2.0


# -- forecast-skewed quoting ------------------------------------------------------------------------

def _book(asks, bids):
    pad = lambda levels: tuple(levels) + (0,) * (5 - len(levels))  # noqa: E731
    return pad([p for p, _ in asks]), pad([v for _, v in asks]), pad([p for p, _ in bids]), pad([v for _, v in bids])


class _Clock:
    def time(self):
        return 0.0


def _quotes_with_forecast(forecast: float, weight: float = 4.0):
    trader = SkewTrader(_Clock(), "T", "secret", CHALLENGERS["skew"].with_(tuning=weight), params={})
    trader._future = _book([(150100, 500)], [(150000, 500)])
    trader._etf = _book([(150300, 20), (150700, 10000)], [(149800, 20), (149400, 10000)])
    trader._seq = 100
    trader._learn = lambda: setattr(trader, "_forecast", forecast)  # the forecast, as the model would set it
    trader._work = lambda: None  # decide, but send nothing
    trader._on_tick()
    return trader._want_quote[Side.SELL], trader._want_quote[Side.BUY]


def _quotes_with_pressure(bid_size: int, ask_size: int, weight: float):
    trader = SkewFastTrader(_Clock(), "T", "secret", CHALLENGERS["skew_fast"].with_(tuning=weight), params={})
    trader._learn = trader._work = lambda: None  # decide, but neither think nor send
    trader.on_order_book_update_message(Instrument.FUTURE, 100, *_book([(150100, ask_size)], [(150000, bid_size)]))
    trader.on_order_book_update_message(Instrument.ETF, 100, *_book([(150300, 20), (150700, 10000)],
                                                                    [(149800, 20), (149400, 10000)]))
    return trader._want_quote[Side.SELL], trader._want_quote[Side.BUY]


def test_book_pressure_skews_the_fast_version_without_waiting_for_the_model():
    # Nineteen lots bid for every one offered: the future is about to tick up, and a strong enough lean
    # withdraws the offer, as the model's forecast does for skew; the reverse withdraws the bid.
    ask, bid = _quotes_with_pressure(950, 50, weight=60.0)
    assert ask is None and bid is not None
    ask, bid = _quotes_with_pressure(50, 950, weight=60.0)
    assert ask is not None and bid is None
    # A balanced touch leans nowhere, however heavily it is weighted.
    assert _quotes_with_pressure(500, 500, weight=60.0) == _quotes_with_pressure(500, 500, weight=0.0)
    assert all(quote is not None for quote in _quotes_with_pressure(500, 500, weight=60.0))


def test_skewed_quotes_move_the_way_the_forecast_points():
    ask, bid = _quotes_with_forecast(0.0)
    assert ask is not None and bid is not None
    # Expecting the future to rise eight ticks (2 ticks, weighted 4), selling anywhere inside the standing
    # market would be selling below where the price is going: the offer is withdrawn, the bid stays.
    ask, bid = _quotes_with_forecast(+2.0)
    assert ask is None and bid is not None
    ask, bid = _quotes_with_forecast(-2.0)
    assert ask is not None and bid is None
    # Unweighted, the same forecast moves nothing that far.
    assert all(quote is not None for quote in _quotes_with_forecast(+2.0, weight=0.0))


# -- optimal quoting with fitted fill rates ---------------------------------------------------------

def test_the_textbook_quotes_have_the_shape_the_model_gives_them():
    A, k, gamma, sigma, limit = 23.0, 0.07, 0.005, 1.1, 100
    bid, ask = glft_offsets(A, k, gamma, sigma, limit)
    assert math.isinf(bid[-1]) and math.isinf(ask[0])  # nothing more is bought at the long limit
    for q in range(-limit + 1, limit):
        assert bid[q + limit] == pytest.approx(ask[-q + limit])  # long and short are mirror images
    assert all(b2 > b1 for b1, b2 in zip(bid[:-2], bid[1:-1], strict=True))  # the longer, the less eager to buy
    assert all(a2 < a1 for a1, a2 in zip(ask[1:-1], ask[2:], strict=True))  # and the more eager to sell
    # As risk aversion vanishes the half-spread's first term tends to 1/k, as in Avellaneda & Stoikov; what
    # remains at zero inventory is the price of the position limit, a tick or two here.
    tiny_bid, _ = glft_offsets(A, k, 1e-7, sigma, 3)
    assert 1.0 / k < tiny_bid[3] < 1.0 / k + 3.0


def test_the_stationary_quotes_are_the_long_horizon_limit_of_the_exact_solution():
    """Integrate ``dv/dt = M v`` back from ``v(T) = 1`` exactly, for a small position limit, and compare."""
    A, k, gamma, sigma, limit = 20.0, 0.1, 0.05, 1.0, 6
    alpha = 0.5 * k * gamma * sigma ** 2
    eta = A * (1 + gamma / k) ** -(1 + k / gamma)
    q = np.arange(-limit, limit + 1)
    M = np.diag(alpha * q ** 2) - eta * (np.eye(len(q), k=1) + np.eye(len(q), k=-1))
    values, vectors = np.linalg.eigh(M)
    tau = 400.0  # far from the end of the session
    v = vectors @ (np.exp(-(values - values[0]) * tau) * (vectors.T @ np.ones(len(q))))
    base = math.log1p(gamma / k) / gamma
    exact_bid = [math.log(v[i] / v[i + 1]) / k + base for i in range(len(q) - 1)]
    exact_ask = [math.log(v[i] / v[i - 1]) / k + base for i in range(1, len(q))]
    bid, ask = glft_offsets(A, k, gamma, sigma, limit)
    assert bid[:-1] == pytest.approx(exact_bid, abs=1e-6) and ask[1:] == pytest.approx(exact_ask, abs=1e-6)


def test_fill_rates_fitted_to_a_session_thin_out_with_distance(session_one):
    from vigil.strategies.challengers.glft import fit_fill_rates
    fit = fit_fill_rates([str(session_one)])
    assert fit["k"] > 0 and fit["A"] > 0 and fit["r2"] > 0.9
    assert 1.5 < fit["customer_orders_per_second"] < 2.5 and 35 < fit["customer_mean_lots"] < 60
    assert all(later <= earlier for earlier, later in zip(fit["rates"], fit["rates"][1:], strict=False))


# -- mean-reversion carrying ------------------------------------------------------------------------

def test_the_reversion_fit_recovers_a_known_process():
    rng = np.random.default_rng(3)
    n, kappa = 60_000, 0.02
    level = np.cumsum(rng.normal(0.0, 0.05, n))  # a slowly wandering average
    x = np.zeros(n)
    for t in range(1, n):
        x[t] = x[t - 1] * math.exp(-kappa) + rng.normal(0.0, 0.5)
    reverting = reversion_from_mids([level + x])
    assert reverting["c"] > 0.3 and 0.005 < reverting["kappa"] < 0.08
    assert reverting["slopes"][-1] < reverting["slopes"][0] < 0  # more of the deviation is given back, given longer
    walk = reversion_from_mids([np.cumsum(rng.normal(0.0, 0.5, n))])
    assert abs(walk["slopes"][-1]) < 0.1  # a random walk gives nothing back


# -- the order-flow model ---------------------------------------------------------------------------

def test_the_flow_fit_recovers_a_known_self_exciting_process():
    rng = np.random.default_rng(5)
    # Each event adds alpha / (1 - beta) expected events in all: branching ratios of 0.3 and 0.25, so it stays stationary.
    mu, alpha, beta, n = np.array([2.0, 3.0]), np.array([[0.06, 0.01], [0.01, 0.05]]), 0.8, 60_000
    counts, excited = np.zeros((n, 2)), np.zeros(2)
    for t in range(n):
        counts[t] = rng.poisson(mu + alpha @ excited)
        excited = beta * excited + counts[t]
    mid = np.cumsum(rng.normal(0.0, 1.0, n))
    fit = hawkes_from_flows([(counts[:, 0], counts[:, 1])], [mid])
    assert fit["beta"] == pytest.approx(beta, abs=0.06)
    assert fit["alpha"][0][0] == pytest.approx(0.06, rel=0.3) and fit["alpha"][1][1] == pytest.approx(0.05, rel=0.3)
    assert fit["mu"][1] > fit["mu"][0]
    true_branching = max(abs(np.linalg.eigvals(alpha / (1 - beta))))
    assert fit["branching_ratio"] == pytest.approx(true_branching, abs=0.1)


# -- reinforcement learning -------------------------------------------------------------------------

def test_the_learner_moves_towards_the_reward_and_keeps_what_it_learnt():
    policy = QPolicy()
    assert policy.best(0) == NEUTRAL  # untrained, it quotes as base Vigil would
    policy.learn(0, 3, reward=10.0, following=1, discount=0.9)
    assert policy.values[0][3] == pytest.approx(10.0) and policy.best(0) == 3  # first visit: the whole step
    policy.learn(0, 3, reward=0.0, following=1, discount=0.9)
    assert 0 < policy.values[0][3] < 10.0
    again = QPolicy.from_dict(json.loads(json.dumps(policy.to_dict())))
    assert sum(again.values, []) == pytest.approx(sum(policy.values, []), abs=1e-4) and again.visits == policy.visits  # kept to 4 places
    rng = random.Random(1)
    assert {policy.choose(0, 1.0, rng) for _ in range(500)} == set(range(len(ACTIONS)))  # epsilon 1: anything


def test_a_policy_that_never_quotes_shows_no_quotes(session_one, unfitted):
    never = QPolicy()
    silent = ACTIONS.index((None, None))
    for row in never.values:
        row[silent] = 1.0
    config = unfitted("rl")
    factory = functools.partial(trader_class(config), config=config, params={"rl": {"policy": never.to_dict()}})
    result, traders = run_match(session_one, {"rl": factory}, end_time=60.0)
    assert traders["rl"].stats["quote_lots"] == 0 and result["rl"].status == "OK"


def test_the_learner_keeps_base_vigils_choice_until_the_evidence_says_otherwise():
    """Safe policy improvement: an action tried fewer than ``trust`` times is not acted on, however good it looks."""
    policy = QPolicy.double(trust=3)
    rng = random.Random(2)
    for _ in range(2):
        policy.learn(0, 5, reward=50.0, following=0, discount=0.0, rng=rng)
    assert policy.worth(0, 5) > 0 and policy.best(0) == NEUTRAL  # promising, but tried only twice
    policy.learn(0, 5, reward=50.0, following=0, discount=0.0, rng=rng)
    assert policy.best(0) == 5  # the third try clears the bar
    stored = QPolicy.from_dict(json.loads(json.dumps(policy.to_dict())))
    assert stored.trust == 3 and stored.twin is not None and stored.best(0) == 5


def test_a_paired_duel_leaves_two_equals_exactly_level(session_one, monkeypatch):
    """Played twice with the luck of reaching the exchange first exchanged, two copies of one trader tie."""
    from vigil import challenge
    from vigil.backtest import measured_reactions
    from vigil.strategies import challengers
    monkeypatch.setitem(challengers.STRATEGIES, "twin", PRESETS["full"])
    monkeypatch.setattr(challenge, "STRATEGIES", challengers.STRATEGIES)
    reactions = challenge.study_reactions(measured_reactions())
    reactions["preset:twin"] = reactions["preset:full"]
    base, twin, *_ = challenge._duel((str(session_one), 3, "twin", reactions, 60.0))
    assert base == pytest.approx(twin, abs=1e-6) and base != 0


def test_t_over_sessions_does_not_trust_four_means_that_agree_by_chance():
    """Four sessions' means can lie close together by luck; the luck within each bounds the standard error."""
    from vigil.challenge import t_over_sessions
    noisy = {session: [mean - 1000.0, mean + 1000.0] * 8
             for session, mean in zip("abcd", (-310.0, -300.0, -320.0, -326.0), strict=True)}
    assert -3.0 < t_over_sessions(noisy) < -2.0  # from the spread between the sessions' means alone: about -55
    steady = {session: [mean - 1.0, mean + 1.0] * 8
              for session, mean in zip("abcd", (1000.0, 2000.0, 3000.0, 4000.0), strict=True)}
    assert t_over_sessions(steady) == pytest.approx(2500.0 / math.sqrt(np.var([1, 2, 3, 4], ddof=1) * 1e6 / 4))
    assert t_over_sessions({"a": [1.0, 2.0]}) is None and t_over_sessions({"a": [0.0], "b": [0.0]}) is None


def test_a_field_compares_every_team_with_base_vigil_match_by_match():
    from vigil.challenge import BASE, _versus_base
    field = {"rows": [{"team": BASE, "runs": {"s1": [10.0, 20.0], "s2": [30.0, 40.0]}},
                      {"team": "other", "runs": {"s1": [15.0, 15.0], "s2": [36.0, 44.0]}}]}
    other = _versus_base(field)["other"]
    assert other["difference"] == pytest.approx(2.5) and other["matches"] == 4 and other["ahead"] == 3
    assert other["by_session"] == {"s1": 0.0, "s2": 5.0} and BASE not in _versus_base(field)


def test_rl_fast_makes_every_decision_rl_makes(session_one, unfitted):
    """The same policy gives the same events, update for update, though rl_fast decides before each update."""
    rng = random.Random(5)
    restless = QPolicy([[rng.uniform(-1.0, 1.0) for _ in ACTIONS] for _ in range(N_STATES)])  # departs everywhere
    params = {"rl": {"policy": restless.to_dict()}}
    streams, departures = [], []
    for engine in ("rl", "rl_fast"):
        events: list[tuple] = []

        def keep(event, events=events):
            if getattr(event, "competitor", None) == "team":
                events.append((round(event.time, 9), event.operation, event.side, event.price, event.volume))

        config = unfitted(engine)
        teams = {"team": functools.partial(trader_class(config), config=config, params=params),
                 "rival": strategy_factory(PRESETS["full"], session_one)}  # fills and hedges to move the state
        _, traders = run_match(session_one, teams, end_time=180.0, on_event=keep)
        streams.append(events)
        departures.append(traders["team"].stats["departures"])
    assert streams[0] == streams[1] and len(streams[0]) > 100
    assert departures[0] == departures[1] > 100


def test_fitting_one_challenger_keeps_every_other_as_it_was(monkeypatch):
    import copy

    from vigil import challenge
    from vigil.paths import all_market_data_files
    from vigil.strategies.challengers import DEFAULT_PATH
    saved = ChallengerParams.load(DEFAULT_PATH)
    monkeypatch.setattr(challenge, "_tuning_run", lambda job: -abs(job[1] - 1.0))  # the knob pays best at 1
    params = challenge.fit(all_market_data_files(), only=["skew_fast"], previous=copy.deepcopy(saved),
                           say=lambda _: None)
    assert all(fold["skew_fast"]["weight"] == 1.0 for fold in params.folds.values())
    assert params.full["skew_fast"]["weight"] == 1.0
    for engine in ("skew", "glft", "ou", "hawkes", "rl"):
        assert params.full[engine] == saved.full[engine]
        assert all(params.folds[key][engine] == saved.folds[key][engine] for key in saved.folds)
    with pytest.raises(ValueError, match="no challenger called"):
        challenge.fit(all_market_data_files(), only=["nonsense"], previous=saved, say=lambda _: None)
    with pytest.raises(ValueError, match="other sessions"):
        challenge.fit(all_market_data_files()[:2], only=["skew_fast"], previous=copy.deepcopy(saved),
                      say=lambda _: None)
