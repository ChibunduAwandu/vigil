"""AdaBoost with decision stumps, in plain numpy.

Written out rather than imported for one practical reason: a Ready Trader Go
auto-trader may only use numpy, pandas and scipy, so a model that is to run
inside the trader has to be one we can evaluate without scikit-learn. The
implementation is the textbook discrete AdaBoost (Freund & Schapire) with a
shrinkage factor: each round picks the stump with the lowest weighted error.
The test suite checks its accuracy against scikit-learn's
``AdaBoostClassifier`` on the same data.

Labels are -1/+1 throughout. A fitted model is five small arrays, so it
serialises to a few kilobytes of JSON and scores a feature vector in
microseconds.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

_EPS = 1e-12


@dataclass
class AdaBoost:
    """An additive ensemble of stumps: ``F(x) = sum_m alpha_m * s_m * sign(x[f_m] - t_m)``.

    ``polarity`` is ``s_m``: +1 if the stump votes +1 above its threshold, -1 if it
    votes +1 at or below it.
    """

    features: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=int))
    thresholds: np.ndarray = field(default_factory=lambda: np.empty(0))
    polarity: np.ndarray = field(default_factory=lambda: np.empty(0))
    alphas: np.ndarray = field(default_factory=lambda: np.empty(0))
    train_errors: np.ndarray = field(default_factory=lambda: np.empty(0), repr=False)

    def __len__(self) -> int:
        return len(self.alphas)

    # -- inference -----------------------------------------------------------------------------

    def decision_function(self, X: np.ndarray, n_rounds: int | None = None) -> np.ndarray:
        """The ensemble margin ``F(x)``; its sign is the predicted class."""
        X = np.atleast_2d(np.asarray(X, dtype=float))
        m = len(self) if n_rounds is None else min(n_rounds, len(self))
        votes = np.where(X[:, self.features[:m]] > self.thresholds[:m], 1.0, -1.0)
        return votes @ (self.alphas[:m] * self.polarity[:m])

    def score_one(self, x: np.ndarray) -> float:
        """``decision_function`` for a single vector, without the 2-D round trip."""
        return float(np.where(x[self.features] > self.thresholds, self._weights, -self._weights).sum())

    def predict(self, X: np.ndarray) -> np.ndarray:
        return np.where(self.decision_function(X) >= 0.0, 1, -1)

    def staged_decision_function(self, X: np.ndarray) -> np.ndarray:
        """Margins after each boosting round: shape ``(rounds, samples)``."""
        X = np.atleast_2d(np.asarray(X, dtype=float))
        votes = np.where(X[:, self.features] > self.thresholds, 1.0, -1.0) * (self.alphas * self.polarity)
        return np.cumsum(votes, axis=1).T

    def feature_importances(self, n_features: int) -> np.ndarray:
        """Share of the total vote weight carried by each feature."""
        total = np.zeros(n_features)
        np.add.at(total, self.features, np.abs(self.alphas))
        return total / max(total.sum(), _EPS)

    @property
    def _weights(self) -> np.ndarray:
        cached = self.__dict__.get("_w")
        if cached is None or len(cached) != len(self.alphas):
            cached = self.__dict__["_w"] = self.alphas * self.polarity
        return cached

    # -- training ------------------------------------------------------------------------------

    @classmethod
    def fit(cls, X: np.ndarray, y: np.ndarray, *, n_rounds: int = 200, learning_rate: float = 1.0,
            sample_weight: np.ndarray | None = None) -> AdaBoost:
        """Boost ``n_rounds`` stumps on features ``X`` (n, d) and labels ``y`` in {-1, +1}."""
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float)
        n, d = X.shape
        if set(np.unique(y)) - {-1.0, 1.0}:
            raise ValueError("labels must be -1 or +1")
        if n < 2:
            raise ValueError("need at least two samples")

        # Sort every feature once. A stump's threshold sits between two consecutive
        # distinct values, so candidate i splits the sorted column after position i.
        order = np.argsort(X, axis=0, kind="stable")
        sorted_x = np.take_along_axis(X, order, axis=0)
        splittable = sorted_x[:-1] < sorted_x[1:]
        if not splittable.any():
            raise ValueError("every feature is constant; nothing to split on")
        midpoints = (sorted_x[:-1] + sorted_x[1:]) / 2.0
        # Between two neighbouring floats the mean rounds onto one of them. A stump sends what is
        # above its threshold one way, so a threshold equal to the upper value would put that value
        # on the wrong side; the lower value itself splits the pair correctly.
        midpoints = np.where(midpoints >= sorted_x[1:], sorted_x[:-1], midpoints)

        w = np.full(n, 1.0 / n) if sample_weight is None else np.asarray(sample_weight, dtype=float)
        w = w / w.sum()

        features, thresholds, polarity, alphas, errors = [], [], [], [], []
        for _ in range(n_rounds):
            # edge(j, i) = sum_k w_k y_k h(x_k) for the stump "+1 above split i of feature j"
            #            = (weighted label sum right of the split) - (the same left of it).
            signed = (w * y)[order]
            left = np.cumsum(signed, axis=0)[:-1]
            edge = np.where(splittable, signed.sum(axis=0) - 2.0 * left, 0.0)
            flat = int(np.argmax(np.abs(edge)))
            i, j = divmod(flat, d)
            gamma = edge[i, j]
            sign = 1.0 if gamma >= 0 else -1.0
            error = (1.0 - abs(gamma)) / 2.0
            if error >= 0.5 - 1e-10:
                break  # no stump beats a coin flip on the reweighted sample

            error = max(error, _EPS)
            alpha = learning_rate * 0.5 * np.log((1.0 - error) / error)
            threshold = midpoints[i, j]
            prediction = sign * np.where(X[:, j] > threshold, 1.0, -1.0)

            features.append(j)
            thresholds.append(threshold)
            polarity.append(sign)
            alphas.append(alpha)
            errors.append(error)

            w = w * np.exp(-alpha * y * prediction)
            w /= w.sum()

        return cls(np.array(features, dtype=int), np.array(thresholds), np.array(polarity), np.array(alphas),
                   np.array(errors))

    # -- persistence ---------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "features": self.features.tolist(),
            "thresholds": self.thresholds.tolist(),
            "polarity": self.polarity.tolist(),
            "alphas": self.alphas.tolist(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AdaBoost:
        return cls(np.array(data["features"], dtype=int), np.array(data["thresholds"], dtype=float),
                   np.array(data["polarity"], dtype=float), np.array(data["alphas"], dtype=float))


@dataclass
class PlattScaler:
    """Maps a margin to a probability: ``P(y = +1 | F) = 1 / (1 + exp(-(a*F + b)))``.

    Boosting margins are not probabilities, and Kelly sizing is only as good as
    the probability it is fed, so the map is fitted on held-out margins.
    """

    a: float = 2.0  # the uncalibrated AdaBoost link: F estimates half the log-odds
    b: float = 0.0

    def __call__(self, margin: np.ndarray | float) -> np.ndarray | float:
        return 1.0 / (1.0 + np.exp(-(self.a * np.asarray(margin) + self.b)))

    @classmethod
    def fit(cls, margin: np.ndarray, y: np.ndarray, *, iterations: int = 50, ridge: float = 1e-6) -> PlattScaler:
        """Logistic regression of the label on the margin, by Newton's method."""
        f = np.asarray(margin, dtype=float)
        t = (np.asarray(y) > 0).astype(float)
        design = np.column_stack([f, np.ones_like(f)])
        theta = np.zeros(2)
        for _ in range(iterations):
            p = 1.0 / (1.0 + np.exp(-(design @ theta)))
            gradient = design.T @ (p - t)
            curvature = (design * (p * (1.0 - p))[:, None]).T @ design + ridge * np.eye(2)
            step = np.linalg.solve(curvature, gradient)
            theta -= step
            if np.abs(step).max() < 1e-10:
                break
        return cls(float(theta[0]), float(theta[1]))
