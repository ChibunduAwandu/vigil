"""The deployable alpha model: a boosted classifier plus what is needed to act on it."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from vigil.ml.adaboost import AdaBoost, PlattScaler
from vigil.ml.features import FEATURE_NAMES
from vigil.paths import REPO_ROOT

DEFAULT_MODEL_PATH = REPO_ROOT / "models" / "alpha_adaboost.json"


@dataclass
class AlphaModel:
    """Predicts the direction of the future's mid price ``horizon`` book updates ahead.

    ``move_scale`` converts a probability into an expected move in ticks,
    ``E[move] = (2p - 1) * move_scale``, and ``payoff_ratio`` is the average size
    of a move called correctly over one called wrongly: the ``b`` of the Kelly
    formula. Both are measured on held-out predictions, never on the fit.
    ``momentum_scale`` is the same kind of constant for the rule the model is
    compared with: the expected move per tick of microprice displacement.
    """

    booster: AdaBoost
    scaler: PlattScaler
    horizon: int = 1
    move_scale: float = 0.0
    payoff_ratio: float = 1.0
    momentum_scale: float = 0.3
    feature_names: tuple[str, ...] = FEATURE_NAMES

    def probability_up(self, x: np.ndarray) -> float:
        return float(self.scaler(self.booster.score_one(x)))

    def probabilities(self, X: np.ndarray) -> np.ndarray:
        return np.asarray(self.scaler(self.booster.decision_function(X)))

    def expected_move(self, x: np.ndarray) -> float:
        """Expected mid move over the horizon, in ticks (positive is up)."""
        return (2.0 * self.probability_up(x) - 1.0) * self.move_scale

    def to_dict(self) -> dict[str, Any]:
        return {
            "booster": self.booster.to_dict(),
            "platt": [self.scaler.a, self.scaler.b],
            "horizon": self.horizon,
            "move_scale": self.move_scale,
            "payoff_ratio": self.payoff_ratio,
            "momentum_scale": self.momentum_scale,
            "feature_names": list(self.feature_names),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AlphaModel:
        names = tuple(data["feature_names"])
        if names != FEATURE_NAMES:
            raise ValueError("model was trained on a different feature set; retrain it (vigil train)")
        return cls(AdaBoost.from_dict(data["booster"]), PlattScaler(*data["platt"]), data["horizon"],
                   data["move_scale"], data["payoff_ratio"], data.get("momentum_scale", 0.3), names)


@dataclass
class AlphaBundle:
    """One model per held-out session, plus one fitted on everything.

    A backtest on session ``k`` must not use a model that saw session ``k``.
    ``for_session`` hands out the model whose training set excluded it, so
    every simulated result is out of sample; a session the bundle has never
    seen gets the model fitted on all the data.
    """

    full: AlphaModel
    folds: dict[str, AlphaModel] = field(default_factory=dict)  # held-out session key -> model
    meta: dict[str, Any] = field(default_factory=dict)

    def for_session(self, key: str | None) -> AlphaModel:
        return self.folds.get(key, self.full) if key else self.full

    def save(self, path: str | os.PathLike = DEFAULT_MODEL_PATH) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "meta": self.meta,
            "full": self.full.to_dict(),
            "folds": {key: model.to_dict() for key, model in self.folds.items()},
        }
        path.write_text(json.dumps(payload, separators=(",", ":")))
        return path

    @classmethod
    def load(cls, path: str | os.PathLike = DEFAULT_MODEL_PATH) -> AlphaBundle:
        payload = json.loads(Path(path).read_text())
        return cls(AlphaModel.from_dict(payload["full"]),
                   {key: AlphaModel.from_dict(value) for key, value in payload["folds"].items()},
                   payload.get("meta", {}))
