"""Challengers: experimental versions of the Vigil trader, to be pitted against base Vigil.

Each is base Vigil (the ``full`` preset) with one part replaced by a technique
from the market-making literature, so that a difference in result can be put
down to that part:

``skew``
    Forecast-skewed quoting. Quotes are priced around where the boosted
    model says the future is heading, not where it is (Cartea, Jaimungal &
    Penalva, *Algorithmic and High-Frequency Trading*, ch. 10).
``glft``
    Optimal quoting with fitted fill rates. Quote distances come from the
    closed-form solution of Guéant, Lehalle & Fernandez-Tapia (2013) for a
    market maker with a position limit, with the rate at which customer
    orders reach each price fitted to the sessions.
``ou``
    Mean-reversion carrying. The future's deviation from its recent average
    is modelled as an Ornstein-Uhlenbeck process, and the inventory Vigil
    carries leans the way the price is expected to revert.
``hawkes``
    A self-exciting (Hawkes) model of the future's aggressive order flow,
    which arrives in bursts. Expected bursts widen the quotes, shrink them,
    and shrink the inventory carried.
``rl``
    Reinforcement learning. A tabular Q-learning policy, trained in the
    simulator, adjusts base Vigil's quotes.
``skew_fast``, ``rl_fast``
    ``skew`` and ``rl`` do work before they send that base Vigil does after,
    and on the real exchange they lost the race for customers for that alone.
    ``rl_fast`` makes ``rl``'s decisions with that work done before the update
    arrives; ``skew_fast`` skews by book pressure, which needs no model.

Whatever a challenger fits from data is fitted leave-one-session-out, as the
boosted model is: ``vigil challenge fit`` writes one set of parameters per
held-out session, and a challenger trading a session is handed the set that
never saw it.
"""
from __future__ import annotations

import functools
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vigil.paths import REPO_ROOT
from vigil.strategies.ensemble import PRESETS, StrategyConfig

DEFAULT_PATH = REPO_ROOT / "models" / "challengers.json"

_BASE = PRESETS["full"]

CHALLENGERS: dict[str, StrategyConfig] = {
    "skew": _BASE.with_(engine="skew"),
    "glft": _BASE.with_(engine="glft"),
    "ou": _BASE.with_(engine="ou"),
    "hawkes": _BASE.with_(engine="hawkes"),
    "rl": _BASE.with_(engine="rl"),
    "skew_fast": _BASE.with_(engine="skew_fast"),
    "rl_fast": _BASE.with_(engine="rl_fast"),
}

# Everything that can be entered as a team, ``preset:<name>``: the component study's presets and the challengers.
STRATEGIES: dict[str, StrategyConfig] = {**PRESETS, **CHALLENGERS}


@dataclass
class ChallengerParams:
    """What the challengers fitted: one set per held-out session, and one fitted on every session."""

    full: dict[str, Any] = field(default_factory=dict)
    folds: dict[str, dict[str, Any]] = field(default_factory=dict)  # held-out session key -> parameters
    meta: dict[str, Any] = field(default_factory=dict)

    def for_session(self, key: str | None) -> dict[str, Any]:
        return self.folds.get(key, self.full) if key else self.full

    def save(self, path: str | os.PathLike = DEFAULT_PATH) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"meta": self.meta, "full": self.full, "folds": self.folds},
                                   separators=(",", ":")))
        return path

    @classmethod
    def load(cls, path: str | os.PathLike = DEFAULT_PATH) -> ChallengerParams:
        payload = json.loads(Path(path).read_text())
        return cls(payload["full"], payload["folds"], payload.get("meta", {}))


@functools.lru_cache(maxsize=4)
def _cached(path: str, stamp: float) -> ChallengerParams:
    return ChallengerParams.load(path)


def params_for(config: StrategyConfig) -> dict[str, Any]:
    """The fitted parameters a trader of ``config`` should use: the set that never saw its session.

    Empty when nothing has been fitted yet; every challenger then falls back to
    defaults of its own, which are stated where it is defined.
    """
    path = Path(config.challenger_path or os.environ.get("VIGIL_CHALLENGERS") or DEFAULT_PATH)
    if not path.is_file():
        return {}
    return _cached(str(path), path.stat().st_mtime).for_session(config.holdout)


def _engines() -> dict[str, type]:
    from vigil.strategies.challengers.glft import GLFTTrader
    from vigil.strategies.challengers.hawkes import HawkesTrader
    from vigil.strategies.challengers.ou import OUTrader
    from vigil.strategies.challengers.rl import RLFastTrader, RLTrader
    from vigil.strategies.challengers.skew import SkewFastTrader, SkewTrader
    return {"skew": SkewTrader, "glft": GLFTTrader, "ou": OUTrader, "hawkes": HawkesTrader, "rl": RLTrader,
            "skew_fast": SkewFastTrader, "rl_fast": RLFastTrader}


ENGINES: dict[str, type] = _engines()
