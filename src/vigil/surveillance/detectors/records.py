"""Books-and-records control: does the reported stream agree with itself, and with the exchange's score?"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from vigil.rtg.events import MatchRecord
from vigil.surveillance.alerts import Severity
from vigil.surveillance.detectors.base import Detector
from vigil.surveillance.market import ExpectedReport

EXCHANGE = "(exchange)"  # findings about the record itself are not attributable to a participant


class TradeReconciliation(Detector):
    """REC-01: every reported execution must be the one that the order stream implies.

    The engine replays the orders through the matching rules and so knows
    which trades the exchange ought to report for each order that arrives: for
    whom, on which order, which way, at what price and with what fee. A report
    that differs, is missing or is surplus means the record cannot be relied
    on: either it was altered, or events were lost on the way. Hedges are
    checked too, as far as they can be: the price of each must be one the
    future's book would have given for that volume at that moment. The same
    goes for a stream that runs backwards in time, refers to orders that do
    not exist, or contains events that cannot be read.

    What the stream cannot show about itself is a hedge that was removed,
    added, or changed in volume: a hedge leaves no trace in the book. That is
    caught only against a second record, the exchange's score board, where one
    is supplied (``score``).
    """

    control = "REC-01"

    def __init__(self, engine):
        super().__init__(engine)
        self.checked = 0
        self.hedges_checked = 0
        self.discrepancies = 0
        self._budget = int(self.param("max_alerts", 20))

    # -- trade reports -------------------------------------------------------------------------

    def matched(self) -> None:
        self.checked += 1

    def mismatch(self, record: MatchRecord, expected: ExpectedReport) -> None:
        """A report on the right order that says something other than what the replay produces."""
        self.checked += 1
        self._raise(record.competitor, record.seq, record.time, "mismatch", record.order_id,
                    f"The reported trade on order {record.order_id} ({_side(record.side)} {record.volume} lots at "
                    f"{record.price}, fee {record.fee}) differs from the trade the replay of the order stream "
                    f"produces ({_side(expected.side)} {expected.volume} lots at {expected.price}, fee "
                    f"{expected.fee}).", expected=expected)

    def unexpected(self, record: MatchRecord) -> None:
        """A report of a trade the replay does not produce at all."""
        self.checked += 1
        self._raise(record.competitor, record.seq, record.time, "unexpected", record.order_id,
                    f"The exchange reported a trade of {record.volume} lots at {record.price} on order "
                    f"{record.order_id}, which the replay of the order stream does not produce.")

    def missing(self, expected: ExpectedReport) -> None:
        """A trade the replay produces, for which no report arrived before the next order did."""
        self._raise(expected.competitor, expected.seq, expected.time,
                    f"missing:{expected.order_id}:{expected.price}", expected.order_id,
                    f"The replay of the order stream produces a trade of {expected.volume} lots at "
                    f"{expected.price} on order {expected.order_id}, of which there is no report.",
                    expected=expected)

    # -- hedges --------------------------------------------------------------------------------

    def hedge(self, record: MatchRecord, possible: set[int]) -> None:
        self.hedges_checked += 1
        if record.price in possible:
            return
        best = f"{min(possible)}" if len(possible) == 1 else f"between {min(possible)} and {max(possible)}"
        self._raise(record.competitor, record.seq, record.time, "hedge", record.order_id,
                    f"A hedge of {record.volume} lots was reported at {record.price}; the future's book at that "
                    + (f"moment gives {best}." if possible else "moment had nothing for it to trade against."),
                    extra={"possible_prices": sorted(possible)})

    # -- the stream itself ---------------------------------------------------------------------

    def anomaly(self, record: MatchRecord, what: str, *, kind: str = "anomaly",
                severity: Severity | None = None) -> None:
        self._raise(record.competitor or EXCHANGE, record.seq, record.time, kind, record.order_id, what,
                    severity=severity)

    # -- the exchange's own score ----------------------------------------------------------------

    def score(self, outcomes: Mapping[str, Mapping[str, Any]]) -> int:
        """Compare final positions with the exchange's score board. Returns how many teams were compared.

        Positions are exact in both records, so any difference means events
        were added to, removed from or altered in the stream (or the score
        board). This is what catches a hedge that was tampered with.
        """
        compared = 0
        for name, outcome in outcomes.items():
            who = self.engine.participants.get(name)
            positions = (outcome.get("etf_position"), outcome.get("future_position"))
            if who is None or None in positions:
                continue
            compared += 1
            ours = (who.etf_position, who.future_position)
            if ours != positions:
                self._raise(name, self.engine.events, self.engine.now, "score", 0,
                            f"The event stream leaves {name} holding {ours[0]:+d} ETF and {ours[1]:+d} future "
                            f"lots; the exchange's score board says {positions[0]:+d} and {positions[1]:+d}.",
                            extra={"from_the_stream": list(ours), "score_board": list(positions)})
        return compared

    def _raise(self, competitor: str, seq: int, time: float, kind: str, order_id: int, description: str, *,
               expected: ExpectedReport | None = None, extra: dict[str, Any] | None = None,
               severity: Severity | None = None) -> None:
        self.discrepancies += 1
        if self._budget <= 0:
            return  # a broken stream breaks everywhere; the count is kept, the alerts are not
        self._budget -= 1
        metrics: dict[str, Any] = {"kind": kind.split(":")[0], **(extra or {})}
        if expected is not None:
            metrics["expected"] = {"competitor": expected.competitor, "order_id": expected.order_id,
                                   "side": expected.side, "price": expected.price, "volume": expected.volume,
                                   "fee": expected.fee}
        # The count makes the key unique: one event can be wrong in more than one way.
        self.alert(competitor or EXCHANGE, f"{kind}:{seq}:{self.discrepancies}", time, time, description,
                   metrics=metrics, evidence=(seq,), orders=(order_id,) if order_id else (), severity=severity)


def _side(side: str | None) -> str:
    return {"A": "sell", "B": "buy"}.get(side, "?")
