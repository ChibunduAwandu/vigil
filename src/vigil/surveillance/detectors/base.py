"""The interface between the surveillance engine and its detectors."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from vigil.rtg.events import MatchRecord
from vigil.surveillance.alerts import Severity
from vigil.surveillance.market import Fill
from vigil.surveillance.rulebook import Control
from vigil.surveillance.state import OrderInfo, Participant

if TYPE_CHECKING:
    from vigil.surveillance.engine import SurveillanceEngine


class Detector:
    """One control from the rulebook.

    The engine calls the hooks below as it works through the event stream;
    a detector overrides the ones it needs and calls ``self.alert`` when it has
    something to report. Hooks only ever see competitors: the replayed
    market's own orders are not under surveillance.
    """

    control: str = ""  # key into the rulebook

    def __init__(self, engine: SurveillanceEngine):
        self.engine = engine
        self.cfg: Control = engine.rulebook.control(self.control)

    # -- hooks ---------------------------------------------------------------------------------

    def on_insert(self, who: Participant, order: OrderInfo, record: MatchRecord) -> None:
        """A competitor entered an order; the book has not yet been touched by it."""

    def on_cancel(self, who: Participant, order: OrderInfo, record: MatchRecord) -> None:
        """A competitor cancelled a resting order.

        Not called for the two kinds of cancellation that are the exchange's
        doing: the unfilled rest of a fill-and-kill order, and the orders of a
        participant it has cut off.
        """

    def on_amend(self, who: Participant, order: OrderInfo, record: MatchRecord) -> None:
        """A competitor reduced the volume of a resting order; ``record.volume`` is the (negative) change."""

    def on_message(self, who: Participant, record: MatchRecord, count: int) -> None:
        """A message the competitor sent and the exchange acted on: insert, cancel, amend or hedge.

        ``count`` is how many such messages the exchange's throttle window
        holds, this one included. Messages the exchange rejected, or that
        changed nothing, count against its limit too but leave no event, so
        this is a lower bound on the exchange's own count.
        """

    def on_cut_off(self, who: Participant, record: MatchRecord, reason: str) -> None:
        """The exchange has disconnected the competitor, as far as the stream shows (see ``Participant.cut_off``)."""

    def on_fill(self, fill: Fill) -> None:
        """An execution with a competitor on at least one side, as reconstructed by the replica."""

    def on_position(self, who: Participant, record: MatchRecord) -> None:
        """A reported trade or hedge has just been booked into the competitor's position."""

    def on_clock(self, now: float) -> None:
        """Simulated time has moved on (called a few times per second of match time)."""

    def finalize(self, end_time: float) -> None:
        """The match is over."""

    # -- helpers -------------------------------------------------------------------------------

    def param(self, name: str, default: Any = None) -> Any:
        return self.cfg.param(name, default)

    def alert(self, competitor: str, key: str, start: float, end: float, description: str, *,
              metrics: dict[str, Any] | None = None, evidence: tuple[int, ...] = (),
              orders: tuple[int, ...] = (), counterparties: tuple[str, ...] = (),
              severity: Severity | None = None, title: str | None = None) -> None:
        self.engine.raise_alert(self.cfg, competitor, key, start, end, description, metrics=metrics or {},
                                evidence=evidence, orders=orders, counterparties=counterparties,
                                severity=severity, title=title)
