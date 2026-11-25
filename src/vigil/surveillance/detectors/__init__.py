"""The controls."""
from vigil.surveillance.detectors.base import Detector
from vigil.surveillance.detectors.conduct import (
    MarkingTheClose,
    OffMarketOrder,
    OrderToTradeRatio,
    QuoteStuffing,
    Spoofing,
    WashTrading,
)
from vigil.surveillance.detectors.limits import MessageRate, PositionLimit, UnhedgedExposure
from vigil.surveillance.detectors.records import EXCHANGE, TradeReconciliation

ALL_DETECTORS: tuple[type[Detector], ...] = (
    PositionLimit, UnhedgedExposure, MessageRate,
    Spoofing, WashTrading, QuoteStuffing, MarkingTheClose, OrderToTradeRatio, OffMarketOrder,
    TradeReconciliation,
)

__all__ = ["ALL_DETECTORS", "EXCHANGE", "Detector", "MarkingTheClose", "MessageRate", "OffMarketOrder",
           "OrderToTradeRatio", "PositionLimit", "QuoteStuffing", "Spoofing", "TradeReconciliation",
           "UnhedgedExposure", "WashTrading"]
