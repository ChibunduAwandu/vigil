"""Training and honest evaluation of the alpha model.

The data is four fifteen-minute sessions. Every number reported here is
leave-one-session-out: to score session ``k`` the model is fitted on the other
three, and the probability calibration it ships with is itself cross-fitted
inside those three. Nothing is tuned on the session being scored.

"Decision accuracy" means: of the updates on which a signal made a directional
call *and* the mid price then moved, the share where the call matched the move.
Updates where the price did not move are neither right nor wrong. The same
definition is applied to the boosted model and to the hand-written rules it is
compared against.

Accuracy is not what the strategy is paid for, so the report also prices the
one decision the forecast is used for. A hedge crosses the future's spread; the
strategy puts one off for an update when the forecast says the price is about
to move its way. ``hedge_timing`` measures what that is worth, in ticks per
hedge, on the prices a hedge would really have paid.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from vigil.ml.adaboost import AdaBoost, PlattScaler
from vigil.ml.features import FEATURE_INDEX, FEATURE_NAMES, features_from_tape
from vigil.ml.model import AlphaBundle, AlphaModel
from vigil.rtg import Instrument
from vigil.rtg.marketdata import file_sha256
from vigil.sim.tape import load_tape

TICK = 100.0  # cents


@dataclass
class Session:
    """One market data session as the model sees it."""

    key: str  # content hash prefix; identifies the session whatever the file is called
    name: str
    X: np.ndarray  # (updates, features); NaN rows where no vector was available
    mid: np.ndarray  # cents
    bid: np.ndarray  # the future's best bid, cents; 0 where there was none
    ask: np.ndarray  # the future's best ask, cents; 0 where there was none

    @classmethod
    def from_file(cls, path: str | os.PathLike) -> Session:
        path = Path(path)
        tape = load_tape(path)
        X, mid = features_from_tape(tape)
        return cls(session_key(path), path.name, X, mid,
                   tape.bid_prices[:, Instrument.FUTURE, 0].astype(float),
                   tape.ask_prices[:, Instrument.FUTURE, 0].astype(float))

    def supervised(self, horizon: int) -> tuple[np.ndarray, np.ndarray]:
        """Feature rows paired with the mid move (ticks) over the next ``horizon`` updates."""
        move = np.full(len(self.mid), np.nan)
        move[:-horizon] = (self.mid[horizon:] - self.mid[:-horizon]) / TICK
        valid = ~np.isnan(self.X).any(axis=1) & ~np.isnan(move)
        return self.X[valid], move[valid]

    def timing_rows(self) -> np.ndarray:
        """Updates with a feature vector and a two-sided future book both then and one update later."""
        two_sided = (self.bid > 0) & (self.ask > 0)
        usable = ~np.isnan(self.X).any(axis=1) & two_sided
        usable[:-1] &= two_sided[1:]
        usable[-1] = False
        return np.flatnonzero(usable)


def session_key(path: str | os.PathLike) -> str:
    return file_sha256(path)[:16]


def _column(name: str) -> int:
    return FEATURE_INDEX[name]


# Hand-written signals: each maps the feature matrix to a call of +1 (up), -1 (down) or 0 (no call).
RULES: dict[str, Callable[[np.ndarray], np.ndarray]] = {
    "momentum: last 4 updates": lambda X: np.sign(X[:, _column("ret4")]),
    "momentum: above/below EMA16": lambda X: np.sign(X[:, _column("mr16")]),
    "mean reversion: fade last move": lambda X: -np.sign(X[:, _column("ret1")]),
    "mean reversion: fade EMA16 gap": lambda X: -np.sign(X[:, _column("mr16")]),
    "order-flow momentum: OFI EMA": lambda X: np.sign(X[:, _column("ofi_ema")]),
    "book imbalance: top level": lambda X: np.sign(X[:, _column("imb1")]),
}


def directional_accuracy(call: np.ndarray, move: np.ndarray) -> tuple[float, float]:
    """``(accuracy, coverage)``: accuracy over decided updates, and the share of moving updates decided."""
    moved = move != 0
    decided = moved & (call != 0)
    if not decided.any():
        return float("nan"), 0.0
    accuracy = float((np.sign(call[decided]) == np.sign(move[decided])).mean())
    return accuracy, float(decided.sum() / max(moved.sum(), 1))


def hedge_timing(forecast: np.ndarray, session: Session, rows: np.ndarray, threshold: float) -> dict[str, float]:
    """What putting a hedge off for one update, on a forecast's say-so, does to its price.

    ``forecast`` is the expected move of the future's mid over the next update,
    in ticks, at each of ``rows``. A purchase that is due waits when the price
    is forecast to fall by ``threshold`` or more, and then pays the next ask
    instead of this one; a sale waits when it is forecast to rise, and receives
    the next bid. The gain is averaged over every update and both directions,
    counting zero where the hedge did not wait: ticks per hedge, if a hedge were
    as likely to fall due on one update as another. Which updates hedges do
    fall due on is the strategy's business, so this is what the signal offers,
    not what a strategy collects; the backtest measures that.
    """
    return _timing(forecast <= -threshold, forecast >= threshold, session, rows)


def _timing(buy_waits: np.ndarray, sell_waits: np.ndarray, session: Session, rows: np.ndarray) -> dict[str, float]:
    buy_gain = (session.ask[rows] - session.ask[rows + 1]) / TICK
    sell_gain = (session.bid[rows + 1] - session.bid[rows]) / TICK
    gain = np.concatenate([np.where(buy_waits, buy_gain, 0.0), np.where(sell_waits, sell_gain, 0.0)])
    waited = np.concatenate([buy_waits, sell_waits])
    return {
        "gain_per_hedge": float(gain.mean()),
        "standard_error": float(gain.std(ddof=1) / np.sqrt(len(gain))),
        "waits": float(waited.mean()),
        "gain_when_waiting": float(gain[waited].mean()) if waited.any() else 0.0,
        # What a hedge pays to cross, for scale: half the future's spread at these updates.
        "half_spread": float((session.ask[rows] - session.bid[rows]).mean() / (2.0 * TICK)),
    }


# The forecasts whose timing is priced: name -> expected move in ticks, from a model and feature rows.
# These are the two the strategy can run on (``alpha="adaboost"`` and ``alpha="momentum"``).
TIMING_SIGNALS: dict[str, Callable[[AlphaModel, np.ndarray], np.ndarray]] = {
    "adaboost": lambda model, X: (2.0 * model.probabilities(X) - 1.0) * model.move_scale,
    "book pressure (microprice)": lambda model, X: model.momentum_scale * X[:, _column("micro")],
}
ALWAYS_WAIT = "always wait"  # the control: waiting whatever the forecast says earns nothing


def _fit(X: np.ndarray, move: np.ndarray, n_rounds: int, learning_rate: float) -> AdaBoost:
    moved = move != 0
    return AdaBoost.fit(X[moved], np.sign(move[moved]), n_rounds=n_rounds, learning_rate=learning_rate)


def _calibration(margin: np.ndarray, move: np.ndarray) -> tuple[PlattScaler, float, float]:
    """Probability map, move scale and payoff ratio from held-out margins."""
    moved = move != 0
    scaler = PlattScaler.fit(margin[moved], np.sign(move[moved]))
    lean = 2.0 * np.asarray(scaler(margin)) - 1.0
    move_scale = float((lean * move).sum() / max((lean * lean).sum(), 1e-12))
    right = moved & (np.sign(lean) == np.sign(move))
    wrong = moved & (np.sign(lean) == -np.sign(move))
    payoff = float(np.abs(move[right]).mean() / np.abs(move[wrong]).mean()) if right.any() and wrong.any() else 1.0
    return scaler, move_scale, payoff


def _cross_fit_margins(parts: Sequence[tuple[np.ndarray, np.ndarray]], n_rounds: int,
                       learning_rate: float) -> tuple[np.ndarray, np.ndarray]:
    """Margins for every row, each produced by a model that did not see that row's session."""
    margins, moves = [], []
    for held_out, (X_out, move_out) in enumerate(parts):
        X_in = np.vstack([X for i, (X, _) in enumerate(parts) if i != held_out])
        move_in = np.concatenate([m for i, (_, m) in enumerate(parts) if i != held_out])
        margins.append(_fit(X_in, move_in, n_rounds, learning_rate).decision_function(X_out))
        moves.append(move_out)
    return np.concatenate(margins), np.concatenate(moves)


def _build(parts: Sequence[tuple[np.ndarray, np.ndarray]], horizon: int, n_rounds: int,
           learning_rate: float) -> AlphaModel:
    """Fit on all of ``parts``; calibrate on margins cross-fitted within ``parts``."""
    X = np.vstack([X for X, _ in parts])
    move = np.concatenate([m for _, m in parts])
    booster = _fit(X, move, n_rounds, learning_rate)
    if len(parts) > 1:
        scaler, move_scale, payoff = _calibration(*_cross_fit_margins(parts, n_rounds, learning_rate))
    else:  # a single session cannot be cross-fitted; fall back to the in-sample margins
        scaler, move_scale, payoff = _calibration(booster.decision_function(X), move)
    micro = X[:, _column("micro")]
    momentum_scale = float((micro * move).sum() / max((micro * micro).sum(), 1e-12))
    return AlphaModel(booster, scaler, horizon, move_scale, payoff, momentum_scale)


def _score(model: AlphaModel, X: np.ndarray, move: np.ndarray) -> dict[str, Any]:
    p = model.probabilities(X)
    call = np.sign(p - 0.5)
    accuracy, coverage = directional_accuracy(call, move)
    moved = move != 0
    up = (move[moved] > 0).astype(float)
    clipped = np.clip(p[moved], 1e-9, 1 - 1e-9)
    confident = {}
    for threshold in (0.55, 0.60, 0.65):
        sure = np.abs(p - 0.5) >= threshold - 0.5
        acc, cov = directional_accuracy(np.where(sure, call, 0.0), move)
        confident[f"{threshold:.2f}"] = {"accuracy": acc, "coverage": cov}
    # Reliability: within each band of predicted probability, how often did the price in fact rise?
    edges = np.array([0.0, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 1.0])
    bands = []
    for low, high in zip(edges[:-1], edges[1:], strict=True):
        inside = (p[moved] >= low) & (p[moved] < high)
        if inside.sum() >= 20:
            bands.append({"band": f"{low:.2f}-{high:.2f}", "n": int(inside.sum()),
                          "predicted": float(p[moved][inside].mean()), "observed": float(up[inside].mean())})
    return {
        "n": int(len(move)),
        "moved": float(moved.mean()),
        "accuracy": accuracy,
        "coverage": coverage,
        "log_loss": float(-(up * np.log(clipped) + (1 - up) * np.log(1 - clipped)).mean()),
        "brier": float(((clipped - up) ** 2).mean()),
        "expected_move_corr": float(np.corrcoef((2 * p - 1) * model.move_scale, move)[0, 1]),
        "when_confident": confident,
        "reliability": bands,
    }


def train_alpha_model(paths: Sequence[str | os.PathLike], *, horizon: int = 1, n_rounds: int = 200,
                      learning_rate: float = 0.2, defer_ticks: float = 0.05) -> tuple[AlphaBundle, dict[str, Any]]:
    """Fit the bundle and return it with its leave-one-session-out report.

    ``defer_ticks`` is the forecast at which the strategy puts a hedge off
    (``StrategyConfig.defer_ticks``); it is used only to price that decision.
    """
    sessions = [Session.from_file(path) for path in paths]
    if len({s.key for s in sessions}) != len(sessions):
        raise ValueError("the same session was given twice")
    parts = [s.supervised(horizon) for s in sessions]

    folds: dict[str, AlphaModel] = {}
    per_session: dict[str, Any] = {}
    for k, session in enumerate(sessions):
        rest = [part for i, part in enumerate(parts) if i != k]
        if not rest:
            break
        model = folds[session.key] = _build(rest, horizon, n_rounds, learning_rate)
        X, move = parts[k]
        scores = {"adaboost": _score(model, X, move)}
        for name, rule in RULES.items():
            accuracy, coverage = directional_accuracy(rule(X), move)
            scores[name] = {"accuracy": accuracy, "coverage": coverage}
        staged = model.booster.staged_decision_function(X)
        checkpoints = [r for r in (1, 5, 10, 25, 50, 100, 150, 200, 300, 400) if r <= len(model.booster)]
        scores["adaboost"]["by_rounds"] = {
            str(r): directional_accuracy(np.sign(staged[r - 1]), move)[0] for r in checkpoints}
        rows = session.timing_rows()
        scores["hedge_timing"] = {name: hedge_timing(forecast(model, session.X[rows]), session, rows, defer_ticks)
                                  for name, forecast in TIMING_SIGNALS.items()}
        everywhere = np.ones(len(rows), dtype=bool)
        scores["hedge_timing"][ALWAYS_WAIT] = _timing(everywhere, everywhere, session, rows)
        per_session[session.name] = scores

    full = _build(parts, horizon, n_rounds, learning_rate)
    report = _summarise(per_session, full, sessions, horizon, n_rounds, learning_rate)
    report["hedge_timing"] = _summarise_timing(per_session, defer_ticks)
    bundle = AlphaBundle(full, folds, meta={
        "horizon": horizon, "n_rounds": n_rounds, "learning_rate": learning_rate,
        "sessions": {s.key: s.name for s in sessions},
        "oos_accuracy": report["summary"]["adaboost"]["accuracy"] if per_session else None,
    })
    return bundle, report


def _summarise_timing(per_session: dict[str, Any], defer_ticks: float) -> dict[str, Any]:
    signals: dict[str, Any] = {}
    for name in (*TIMING_SIGNALS, ALWAYS_WAIT) if per_session else ():
        parts = [scores["hedge_timing"][name] for scores in per_session.values()]
        gains = [part["gain_per_hedge"] for part in parts]
        signals[name] = {
            "gain_per_hedge": float(np.mean(gains)),
            "min": float(np.min(gains)),
            "max": float(np.max(gains)),
            # The sessions are independent, so the error of their mean is the root sum of squares over n.
            "standard_error": float(np.sqrt(sum(part["standard_error"] ** 2 for part in parts)) / len(parts)),
            "waits": float(np.mean([part["waits"] for part in parts])),
            "gain_when_waiting": float(np.mean([part["gain_when_waiting"] for part in parts])),
        }
    half_spread = [scores["hedge_timing"][ALWAYS_WAIT]["half_spread"] for scores in per_session.values()]
    return {"defer_ticks": defer_ticks, "half_spread": float(np.mean(half_spread)) if half_spread else None,
            "signals": signals}


def _summarise(per_session: dict[str, Any], full: AlphaModel, sessions: Sequence[Session], horizon: int,
               n_rounds: int, learning_rate: float) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    if per_session:
        signals = ["adaboost", *RULES]
        for signal in signals:
            accuracies = [scores[signal]["accuracy"] for scores in per_session.values()]
            summary[signal] = {
                "accuracy": float(np.mean(accuracies)),
                "min": float(np.min(accuracies)),
                "max": float(np.max(accuracies)),
                "coverage": float(np.mean([scores[signal]["coverage"] for scores in per_session.values()])),
            }
        best = summary["adaboost"]["accuracy"]
        for signal in RULES:
            base = summary[signal]["accuracy"]
            summary[signal]["adaboost_relative_gain"] = best / base - 1.0
            summary[signal]["adaboost_point_gain"] = best - base
            summary[signal]["adaboost_wins_sessions"] = int(sum(
                scores["adaboost"]["accuracy"] > scores[signal]["accuracy"] for scores in per_session.values()))
    importance = full.booster.feature_importances(len(FEATURE_NAMES))
    ranked = sorted(zip(FEATURE_NAMES, importance.tolist(), strict=True), key=lambda kv: -kv[1])
    return {
        "protocol": "leave-one-session-out; calibration cross-fitted inside each training set",
        "horizon_updates": horizon,
        "n_rounds": n_rounds,
        "learning_rate": learning_rate,
        "sessions": [s.name for s in sessions],
        "summary": summary,
        "per_session": per_session,
        "full_model": {
            "rounds": len(full.booster),
            "platt": [full.scaler.a, full.scaler.b],
            "move_scale_ticks": full.move_scale,
            "payoff_ratio": full.payoff_ratio,
            "momentum_scale": full.momentum_scale,
            "feature_importance": [{"feature": name, "weight": weight} for name, weight in ranked if weight > 0],
        },
    }
