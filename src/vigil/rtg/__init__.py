"""Bridge to the Ready Trader Go distribution.

Ready Trader Go is used as an unmodified, separately installed dependency: importing this
package puts its directory on ``sys.path`` and re-exports the pieces of the
exchange that the rest of Vigil builds on (the matching engine, the competitor
risk checks, the wire protocol). Nothing in here re-implements exchange logic.
"""
from __future__ import annotations

import sys

from vigil.paths import rtg_home


def _ensure_importable() -> None:
    home = rtg_home()
    if not (home / "ready_trader_go" / "__init__.py").is_file():
        raise ImportError(
            f"Ready Trader Go not found at {home}. Run 'python scripts/bootstrap_rtg.py' from the "
            "repository root, or set VIGIL_RTG_HOME to the directory containing the 'ready_trader_go' package."
        )
    path = str(home)
    if path not in sys.path:
        sys.path.insert(0, path)


_ensure_importable()

from ready_trader_go import messages  # noqa: E402
from ready_trader_go.account import AccountFactory, CompetitorAccount  # noqa: E402
from ready_trader_go.base_auto_trader import BaseAutoTrader  # noqa: E402
from ready_trader_go.competitor import Competitor, CompetitorManager  # noqa: E402
from ready_trader_go.limiter import FrequencyLimiter  # noqa: E402
from ready_trader_go.market_events import MarketEvent, MarketEventOperation, MarketEventsReader  # noqa: E402
from ready_trader_go.match_events import MatchEvent, MatchEventOperation, MatchEvents  # noqa: E402
from ready_trader_go.order_book import (  # noqa: E402
    MAXIMUM_ASK,
    MINIMUM_BID,
    TOP_LEVEL_COUNT,
    IOrderListener,
    Order,
    OrderBook,
)
from ready_trader_go.types import IController, IExecutionConnection, Instrument, Lifespan, Side  # noqa: E402
from ready_trader_go.unhedged_lots import (  # noqa: E402
    MAX_UNHEDGED_LOTS,
    UNHEDGED_LOTS_TIME_LIMIT,
    UnhedgedLots,
    UnhedgedLotsFactory,
)

__all__ = [
    "MAXIMUM_ASK",
    "MAX_UNHEDGED_LOTS",
    "MINIMUM_BID",
    "TOP_LEVEL_COUNT",
    "UNHEDGED_LOTS_TIME_LIMIT",
    "AccountFactory",
    "BaseAutoTrader",
    "Competitor",
    "CompetitorAccount",
    "CompetitorManager",
    "FrequencyLimiter",
    "IController",
    "IExecutionConnection",
    "IOrderListener",
    "Instrument",
    "Lifespan",
    "MarketEvent",
    "MarketEventOperation",
    "MarketEventsReader",
    "MatchEvent",
    "MatchEventOperation",
    "MatchEvents",
    "Order",
    "OrderBook",
    "Side",
    "UnhedgedLots",
    "UnhedgedLotsFactory",
    "messages",
]
