"""What every challenger shares: base Vigil, plus the parameters fitted for it."""
from __future__ import annotations

from typing import Any

from vigil.strategies.ensemble import EnsembleTrader, StrategyConfig


class Challenger(EnsembleTrader):
    """Base Vigil with one part replaced. ``name`` is the challenger's engine name in ``CHALLENGERS``.

    ``params`` are the fitted parameters of every challenger for the session
    being traded (``ChallengerParams.for_session``); left out, they are read
    from ``models/challengers.json`` for the session the settings name.
    ``uses`` names the challenger whose fitted parameters this one trades with,
    when it is not its own.
    """

    name = ""
    uses = ""

    def __init__(self, loop, team_name: str, secret: str, config: StrategyConfig | None = None, model=None,
                 params: dict[str, Any] | None = None):
        super().__init__(loop, team_name, secret, config, model)
        if params is None:
            from vigil.strategies.challengers import params_for
            params = params_for(self.config)
        self.fitted: dict[str, Any] = dict(params.get(self.uses or self.name, {}))

    def knob(self, key: str, default: float) -> float:
        """The challenger's tuning knob: the settings' ``tuning`` if given, else the value fitted for it."""
        if self.config.tuning is not None:
            return float(self.config.tuning)
        return float(self.fitted.get(key, default))
