from __future__ import annotations

import math

import numpy as np
import pytest

from vigil.strategies.inventory import best_block, flat_values, inventory_values
from vigil.strategies.kelly import (
    growth_rate,
    hedge_band,
    hedge_target,
    kelly_fraction,
    kelly_size,
    kelly_weights,
)


def test_the_examples_in_the_docstrings_are_true():
    import doctest

    from vigil.strategies import inventory, kelly
    for module in (kelly, inventory):
        outcome = doctest.testmod(module)
        assert outcome.failed == 0, module.__name__
    assert doctest.testmod(kelly).attempted >= 2


def test_kelly_fraction_known_values():
    assert kelly_fraction(0.6, 1.0) == pytest.approx(0.2)  # the classic 60/40 even-money bet
    assert kelly_fraction(0.5, 1.0) == pytest.approx(0.0)  # no edge, no bet
    assert kelly_fraction(0.5, 2.0) == pytest.approx(0.25)
    assert kelly_fraction(0.4, 1.0) < 0  # negative edge: the other side is the bet
    with pytest.raises(ValueError):
        kelly_fraction(1.2)
    with pytest.raises(ValueError):
        kelly_fraction(0.5, 0.0)


@pytest.mark.parametrize("p, b", [(0.55, 1.0), (0.6, 1.0), (0.7, 0.5), (0.4, 3.0)])
def test_kelly_fraction_maximises_growth(p, b):
    best = kelly_fraction(p, b)
    grid = np.linspace(0.0, 0.99, 991)
    numeric = grid[np.argmax([growth_rate(f, p, b) for f in grid])]
    assert best == pytest.approx(numeric, abs=2e-3)
    assert growth_rate(best, p, b) >= growth_rate(best / 2, p, b) > 0  # half-Kelly still grows, more slowly
    assert growth_rate(min(2 * best, 0.99), p, b) < growth_rate(best, p, b)  # over-betting costs growth


def test_growth_rate_is_ruin_at_full_stake():
    assert growth_rate(1.0, 0.9) == -math.inf


def test_kelly_size_scales_with_bankroll_and_edge():
    assert kelly_size(1000.0, 0.1, 2.0) == pytest.approx(50.0)
    assert kelly_size(2000.0, 0.1, 2.0) == pytest.approx(100.0)
    assert kelly_size(1000.0, 0.1, 2.0, fraction=0.5) == pytest.approx(25.0)
    assert kelly_size(1000.0, -0.1, 2.0) < 0
    assert kelly_size(0.0, 0.1, 2.0) == 0.0 and kelly_size(1000.0, 0.1, 0.0) == 0.0


def test_hedge_band_is_centred_on_the_drift_optimum():
    lo, hi = hedge_band(1000.0, 0.0, 0.1, 1.0, fraction=0.5)
    assert (lo, hi) == (-50.0, 50.0)
    lo, hi = hedge_band(1000.0, 0.05, 0.1, 1.0, fraction=0.5)  # an upward drift favours holding longs
    assert hi == pytest.approx(75.0) and lo == pytest.approx(-25.0)
    assert (hi + lo) / 2 == pytest.approx(kelly_size(1000.0, 0.05, 1.0, fraction=0.5))
    # No saving from carrying: the band collapses onto the Kelly position.
    assert hedge_band(1000.0, 0.05, 0.0, 1.0) == (pytest.approx(25.0), pytest.approx(25.0))


def test_hedge_target_only_ever_reduces_risk():
    assert hedge_target(30, -12, 12) == 12  # back to the edge of the band, not to zero
    assert hedge_target(-30, -12, 12) == -12
    assert hedge_target(8, -12, 12) == 8  # inside: leave it alone
    assert hedge_target(-30, 5, 20) == 0  # the band lies across zero: go flat, not long
    assert hedge_target(30, -20, -5) == 0
    assert hedge_target(0, 5, 20) == 0  # never add exposure by trading the hedge
    for delta in range(-100, 101, 7):
        for lo, hi in ((-10, 10), (-40, 5), (3, 60), (0, 0)):
            assert abs(hedge_target(delta, lo, hi)) <= abs(delta)


def test_kelly_weights_solves_the_linear_system():
    rng = np.random.default_rng(3)
    a = rng.normal(size=(4, 4))
    cov = a @ a.T + 0.5 * np.eye(4)
    mu = rng.normal(size=4)
    expected = np.linalg.solve(cov, mu)
    assert kelly_weights(mu.tolist(), cov.tolist()) == pytest.approx(expected.tolist())
    assert kelly_weights(mu.tolist(), cov.tolist(), fraction=0.5) == pytest.approx((expected / 2).tolist())
    with pytest.raises(ValueError):
        kelly_weights([1.0, 1.0], [[1.0, 1.0], [1.0, 1.0]])


# -- the value of inventory ------------------------------------------------------------------------

def test_inventory_values_shape():
    values = inventory_values(100, 46.0, 4.0)
    assert len(values) == 201
    assert values[100] == 0.0  # flat is the reference point
    assert np.allclose(values, values[::-1], atol=1e-6)  # long and short are worth the same
    assert (values <= 1e-9).all()  # and no position is worth more than none
    right = values[100:]
    assert (np.diff(right) < 0).all()  # the further from flat, the worse
    assert (np.diff(right, 2) < 1e-6).all()  # and ever more steeply


def test_inventory_values_scale_with_the_edge():
    assert np.allclose(inventory_values(100, 46.0, 8.0), 2 * inventory_values(100, 46.0, 4.0), atol=1e-5)


def test_best_block_weighs_edge_against_inventory():
    values = inventory_values(100, 46.0, 4.0)
    rich = np.full(50, 3.0)  # 50 lots on offer, each worth 3 dollars on its own merits
    assert best_block(rich, values, 0, -1, 100) == 50  # worth selling all of them
    thin = np.full(50, 0.2)
    assert 0 < best_block(thin, values, 0, -1, 100) < 50  # a thin edge is not worth much capacity
    assert best_block(thin, values, 90, -1, 100) == 50  # unless selling is what frees capacity
    assert best_block(thin, values, -90, -1, 100) == 0  # and nothing at all if it uses up the last of it
    assert best_block(rich, values, -90, -1, 100) == 10  # a rich edge is taken, but never past the limit
    losing = np.full(50, -0.5)
    assert best_block(losing, values, 0, -1, 100) == 0
    assert best_block(losing, values, 100, -1, 100) > 0  # a small loss can be worth taking at the limit
    assert best_block(rich, flat_values(100), -100, -1, 100) == 0  # no room at all
