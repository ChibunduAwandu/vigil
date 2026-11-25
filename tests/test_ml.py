from __future__ import annotations

import numpy as np
import pytest

from vigil.ml.adaboost import AdaBoost, PlattScaler
from vigil.ml.features import FEATURE_NAMES, N_FEATURES, WARMUP_UPDATES, FeatureState, features_from_tape
from vigil.ml.model import AlphaBundle
from vigil.ml.train import ALWAYS_WAIT, RULES, Session, directional_accuracy, hedge_timing, train_alpha_model
from vigil.paths import all_market_data_files
from vigil.sim.tape import load_tape
from vigil.strategies.ensemble import StrategyConfig


def _dataset(n=4000, seed=0):
    """Labels that depend on two features additively, plus noise and five irrelevant features."""
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, 7))
    logit = 1.5 * X[:, 0] - 1.0 * (X[:, 3] > 0.3) + 0.3 * rng.normal(size=n)
    return X, np.where(logit > 0, 1.0, -1.0)


def test_a_stump_separates_values_that_are_neighbouring_floats():
    """Halfway between two adjacent floats is one of them. The threshold must still put each on its own side."""
    for low in (1.0, 1.0000000000000002, 0.1, 3.0000000000000004, 1e-9, 123456.789):
        high = np.nextafter(low, np.inf)
        X = np.array([[low]] * 40 + [[high]] * 40)
        y = np.array([-1.0] * 40 + [1.0] * 40)
        model = AdaBoost.fit(X, y, n_rounds=1)
        assert (model.predict(X) == y).all(), (low, high, model.thresholds)


def test_adaboost_learns_and_generalises():
    X, y = _dataset()
    model = AdaBoost.fit(X[:3000], y[:3000], n_rounds=100)
    assert (model.predict(X[:3000]) == y[:3000]).mean() > 0.9
    assert (model.predict(X[3000:]) == y[3000:]).mean() > 0.88
    importance = model.feature_importances(7)
    assert importance.sum() == pytest.approx(1.0)
    assert importance[0] + importance[3] > 0.7  # it found the two features that matter


def test_adaboost_first_stump_is_the_best_single_split():
    X, y = _dataset(500, seed=1)
    model = AdaBoost.fit(X, y, n_rounds=1)
    stump_error = (model.predict(X) != y).mean()

    # Brute force: every feature, every threshold between neighbouring values, both polarities.
    best = 1.0
    for j in range(X.shape[1]):
        ordered = np.sort(X[:, j])
        for threshold in (ordered[:-1] + ordered[1:]) / 2:
            error = (np.where(X[:, j] > threshold, 1, -1) != y).mean()
            best = min(best, error, 1 - error)
    assert stump_error == pytest.approx(best)
    assert model.train_errors[0] == pytest.approx(stump_error)


def test_adaboost_exponential_loss_falls_every_round():
    X, y = _dataset(1500, seed=2)
    model = AdaBoost.fit(X, y, n_rounds=40)
    staged = model.staged_decision_function(X)
    loss = np.exp(-staged * y).mean(axis=1)
    assert (np.diff(loss) < 1e-12).all()
    assert (model.train_errors < 0.5).all()


def test_adaboost_matches_scikit_learn_in_accuracy():
    from sklearn.ensemble import AdaBoostClassifier
    from sklearn.tree import DecisionTreeClassifier
    X, y = _dataset(6000, seed=4)
    train, test = slice(0, 4500), slice(4500, None)
    ours = AdaBoost.fit(X[train], y[train], n_rounds=80, learning_rate=0.5)
    theirs = AdaBoostClassifier(DecisionTreeClassifier(max_depth=1), n_estimators=80, learning_rate=0.5,
                                random_state=0).fit(X[train], y[train])
    ours_accuracy = (ours.predict(X[test]) == y[test]).mean()
    theirs_accuracy = (theirs.predict(X[test]) == y[test]).mean()
    assert abs(ours_accuracy - theirs_accuracy) < 0.02
    assert (ours.predict(X[test]) == theirs.predict(X[test])).mean() > 0.95


def test_adaboost_scoring_paths_agree_and_survive_serialisation():
    X, y = _dataset(800, seed=5)
    model = AdaBoost.fit(X, y, n_rounds=30, learning_rate=0.3)
    margins = model.decision_function(X)
    assert [model.score_one(row) for row in X[:50]] == pytest.approx(margins[:50].tolist())
    assert model.staged_decision_function(X)[-1] == pytest.approx(margins)
    clone = AdaBoost.from_dict(model.to_dict())
    assert clone.decision_function(X) == pytest.approx(margins)


def test_adaboost_rejects_bad_input():
    with pytest.raises(ValueError):
        AdaBoost.fit(np.zeros((10, 2)), np.ones(10))  # constant features
    with pytest.raises(ValueError):
        AdaBoost.fit(np.random.default_rng(0).normal(size=(10, 2)), np.arange(10))  # labels not -1/+1


def test_platt_scaler_recovers_a_known_link():
    rng = np.random.default_rng(6)
    margin = rng.normal(size=20_000)
    probability = 1 / (1 + np.exp(-(1.7 * margin - 0.4)))
    y = np.where(rng.random(20_000) < probability, 1, -1)
    scaler = PlattScaler.fit(margin, y)
    assert scaler.a == pytest.approx(1.7, abs=0.08)
    assert scaler.b == pytest.approx(-0.4, abs=0.05)
    assert scaler(0.0) == pytest.approx(1 / (1 + np.exp(0.4)), abs=0.02)


# -- features --------------------------------------------------------------------------------------

def test_feature_state_matches_the_batch_path(session_one):
    tape = load_tape(session_one)
    X, mid = features_from_tape(tape)
    assert X.shape == (len(tape), N_FEATURES) and len(FEATURE_NAMES) == N_FEATURES
    assert np.isnan(X[:WARMUP_UPDATES - 1]).all() and not np.isnan(X[WARMUP_UPDATES:WARMUP_UPDATES + 500]).any()

    # Drive a second state by hand, through the message-shaped entry point a live trader uses.
    state = FeatureState()
    future = 0
    for i in range(200):
        buy, sell = tape.buy_volume[i, future], tape.sell_volume[i, future]
        if buy or sell:  # one synthetic trade tick carrying the same totals
            state.add_trades([round(tape.buy_value[i, future] / buy) if buy else 0], [int(buy)],
                             [round(tape.sell_value[i, future] / sell) if sell else 0], [int(sell)])
        vector = state.update(tape.ask_prices[i, future].tolist(), tape.ask_volumes[i, future].tolist(),
                              tape.bid_prices[i, future].tolist(), tape.bid_volumes[i, future].tolist())
        if i >= WARMUP_UPDATES - 1:
            # Everything but the trade VWAP is exact; that one was rounded to a whole price above.
            close = np.isclose(vector, X[i], atol=1e-9)
            close[FEATURE_NAMES.index("tvwap")] = abs(vector[FEATURE_NAMES.index("tvwap")] - X[i, 12]) < 0.01
            assert close.all(), (i, [FEATURE_NAMES[k] for k in np.where(~close)[0]])
        else:
            assert vector is None
    assert state.mid == mid[199]


def test_feature_state_skips_a_one_sided_book():
    state = FeatureState()
    assert state.update([0] * 5, [0] * 5, [100, 0, 0, 0, 0], [10, 0, 0, 0, 0]) is None
    assert state.updates == 0


def test_directional_accuracy_ignores_flat_updates_and_abstentions():
    call = np.array([1, 1, -1, 0, 1, -1])
    move = np.array([2, -1, -3, 5, 0, 0])
    accuracy, coverage = directional_accuracy(call, move)
    assert accuracy == pytest.approx(2 / 3)  # three decided on moving updates, two right
    assert coverage == pytest.approx(3 / 4)


def _quotes(bid, ask) -> Session:
    """A session that is nothing but the future's best bid and ask (cents)."""
    bid, ask = np.asarray(bid, float), np.asarray(ask, float)
    return Session("synthetic", "synthetic", np.zeros((len(bid), 1)), (bid + ask) / 2, bid, ask)


def test_hedge_timing_prices_a_wait_on_the_quotes_a_hedge_would_pay():
    # The ask falls a tick, then the bid rises a tick, then nothing happens.
    session = _quotes(bid=[1000, 1000, 1100, 1100], ask=[1200, 1100, 1200, 1200])
    rows = session.timing_rows()
    assert rows.tolist() == [0, 1, 2]  # the last update has no next one
    # A forecast that calls both moves: the purchase waits for the lower ask, the sale for the higher bid.
    right = hedge_timing(np.array([-1.0, 1.0, 0.0]), session, rows, threshold=0.05)
    assert right["waits"] == pytest.approx(2 / 6)  # two of six (update, direction) pairs
    assert right["gain_when_waiting"] == pytest.approx(1.0)
    assert right["gain_per_hedge"] == pytest.approx(2 / 6)
    assert right["half_spread"] == pytest.approx((2 + 1 + 1) / 3 / 2)
    # The opposite forecast holds the sale while the bid stays put and the purchase while the ask rises.
    wrong = hedge_timing(np.array([1.0, -1.0, 0.0]), session, rows, threshold=0.05)
    assert wrong["gain_per_hedge"] == pytest.approx(-1 / 6)
    # Below the threshold nothing waits, and nothing is gained or lost.
    timid = hedge_timing(np.array([0.04, -0.04, 0.0]), session, rows, threshold=0.05)
    assert timid["waits"] == 0.0 and timid["gain_per_hedge"] == 0.0


def test_hedge_timing_skips_updates_without_a_two_sided_book():
    session = _quotes(bid=[1000, 0, 1000, 1000], ask=[1100, 1100, 1100, 1100])
    assert session.timing_rows().tolist() == [2]


# -- the trained model -----------------------------------------------------------------------------

@pytest.fixture(scope="module")
def trained():
    return train_alpha_model(all_market_data_files(), n_rounds=60, learning_rate=0.3)


def test_training_is_leave_one_session_out(trained):
    bundle, report = trained
    sessions = [Session.from_file(path) for path in all_market_data_files()]
    assert set(bundle.folds) == {s.key for s in sessions}
    assert len(report["per_session"]) == 4
    # The model for a held-out session is not the model fitted on everything.
    for key, fold in bundle.folds.items():
        assert bundle.for_session(key) is fold
        assert fold.booster.to_dict() != bundle.full.booster.to_dict()
    assert bundle.for_session("a session never seen") is bundle.full
    assert bundle.for_session(None) is bundle.full


def test_model_beats_chance_and_the_momentum_rule_out_of_sample(trained):
    _, report = trained
    summary = report["summary"]
    assert 0.54 < summary["adaboost"]["accuracy"] < 0.65  # a real edge, and not a suspicious one
    assert summary["adaboost"]["min"] > 0.52  # in every session
    momentum = summary["momentum: last 4 updates"]
    assert momentum["adaboost_wins_sessions"] == 4
    assert momentum["adaboost_relative_gain"] > 0.10
    assert set(RULES) <= set(summary)


def test_the_forecast_improves_the_price_of_a_hedge_and_waiting_blindly_does_not(trained):
    _, report = trained
    timing = report["hedge_timing"]
    assert timing["defer_ticks"] == StrategyConfig().defer_ticks  # priced at the threshold the strategy uses
    for name in ("adaboost", "book pressure (microprice)"):
        entry = timing["signals"][name]
        assert entry["min"] > 0.02  # in every session
        assert entry["gain_per_hedge"] > 5 * entry["standard_error"]
        assert entry["gain_per_hedge"] < timing["half_spread"]  # it trims the cost of crossing; it does not pay it
    control = timing["signals"][ALWAYS_WAIT]
    assert control["waits"] == 1.0 and abs(control["gain_per_hedge"]) < 0.002


def test_model_probabilities_are_roughly_calibrated(trained):
    _, report = trained
    for scores in report["per_session"].values():
        assert scores["adaboost"]["log_loss"] < 0.6931  # better than always saying 50%
        for band in scores["adaboost"]["reliability"]:
            assert abs(band["predicted"] - band["observed"]) < 0.15


def test_bundle_round_trip(trained, tmp_path):
    bundle, _ = trained
    path = bundle.save(tmp_path / "model.json")
    loaded = AlphaBundle.load(path)
    x = np.linspace(-1, 1, N_FEATURES)
    assert loaded.full.probability_up(x) == pytest.approx(bundle.full.probability_up(x))
    assert loaded.full.expected_move(x) == pytest.approx(bundle.full.expected_move(x))
    assert set(loaded.folds) == set(bundle.folds)
