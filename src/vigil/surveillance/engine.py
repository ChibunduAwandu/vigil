"""The surveillance engine: match events in, alerts out.

The engine is a plain state machine over the event stream. It does not care
where the events come from (a CSV written by the exchange, the simulator, a
Kafka topic fed by a live match), only that they arrive in the exchange's
sequence. Feeding it the same stream twice produces the same alerts with the
same identifiers, so a consumer that starts a match again from an earlier
offset reaches the same conclusions under the same names, and whoever receives
them twice can tell.

It never raises on what it is fed. An event that cannot be read is itself a
finding about the record (control REC-01), and the engine carries on; a gap in
the stream is a finding too, after which the engine stops, because from there
on it no longer knows what the books looked like.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from vigil.rtg import UNHEDGED_LOTS_TIME_LIMIT
from vigil.rtg.events import AMEND, CANCEL, ETF, FUTURE, HEDGE, INSERT, TRADE, MatchHeader, MatchRecord
from vigil.surveillance.alerts import Alert, Severity, alert_id
from vigil.surveillance.detectors import ALL_DETECTORS, Detector, TradeReconciliation
from vigil.surveillance.market import MARKET, Fill, MarketReplica
from vigil.surveillance.rulebook import Control, Rulebook, load_rulebook
from vigil.surveillance.state import OrderInfo, Participant

CLOCK_STEP = 0.25  # seconds of match time between calls to the detectors' clocks

# Why the exchange cuts a participant off (competitor.py, execution.py).
POSITION_LIMIT = "position limit"
UNHEDGED_TIME = "un-hedged time limit"
MESSAGE_LIMIT = "message limit"


@dataclass
class SurveillanceResult:
    """Everything the engine concluded about one match."""

    header: MatchHeader
    alerts: list[Alert]
    participants: dict[str, dict[str, Any]]
    market: dict[str, Any]
    integrity: dict[str, Any]
    rulebook_sha256: str
    controls_run: list[str] = field(default_factory=list)

    @property
    def match_id(self) -> str:
        return self.header.match_id

    def alerts_for(self, competitor: str) -> list[Alert]:
        return [alert for alert in self.alerts if alert.competitor == competitor]

    def by_control(self) -> dict[str, list[Alert]]:
        out: dict[str, list[Alert]] = {}
        for alert in self.alerts:
            out.setdefault(alert.control, []).append(alert)
        return out

    def worst(self) -> Severity | None:
        return max((alert.severity for alert in self.alerts), default=None)


class SurveillanceEngine:
    def __init__(self, header: MatchHeader, rulebook: Rulebook | None = None,
                 detectors: Sequence[type[Detector]] | None = None):
        self.header = header
        self.match_id = header.match_id
        self.rulebook = rulebook or load_rulebook()

        limits, instrument, fees = header.limits, header.instrument, header.fees
        self.speed = float(header.speed) or 1.0
        self.position_limit = int(limits.get("PositionLimit", 100))
        self.message_limit = int(limits.get("MessageFrequencyLimit", 50))
        # The exchange throttles over its interval divided by its clock speed, in match time, and
        # runs its un-hedged timer on the wall clock: sixty seconds of it, however fast the match.
        self.message_interval = float(limits.get("MessageFrequencyInterval", 1.0)) / self.speed
        self.unhedged_seconds = UNHEDGED_LOTS_TIME_LIMIT * self.speed
        self.tick = int(float(instrument.get("TickSize", 1.0)) * 100.0)  # as account.py computes it
        self.etf_clamp = float(instrument.get("EtfClamp", 0.002))

        self.replica = MarketReplica(float(fees.get("Maker", -0.0001)), float(fees.get("Taker", 0.0002)))
        self.participants: dict[str, Participant] = {name: Participant(name) for name in header.teams}
        self.alerts: list[Alert] = []
        self.events = 0
        self.now = 0.0
        self.last_etf_fill: Fill | None = None  # the trade that set the ETF's last traded price
        self.stopped_at: int | None = None  # sequence number after which the stream could not be followed
        self.score_checked = 0

        self._fresh: list[Alert] = []
        self._raised: set[str] = set()
        self._clock = 0.0
        self._misordered = False
        self._detectors = [cls(self) for cls in (detectors or ALL_DETECTORS) if cls.control in self.rulebook.controls]
        self._records = next((d for d in self._detectors if isinstance(d, TradeReconciliation)), None)

        # Hooks are dispatched only to the detectors that override them.
        def overriding(hook: str) -> list:
            return [getattr(d, hook) for d in self._detectors if getattr(type(d), hook) is not getattr(Detector, hook)]
        self._on_insert = overriding("on_insert")
        self._on_cancel = overriding("on_cancel")
        self._on_amend = overriding("on_amend")
        self._on_message = overriding("on_message")
        self._on_cut_off = overriding("on_cut_off")
        self._on_fill = overriding("on_fill")
        self._on_position = overriding("on_position")
        self._on_clock = overriding("on_clock")

    # -- input ---------------------------------------------------------------------------------

    def process(self, record: MatchRecord) -> list[Alert]:
        """Consume the next event. Returns the alerts it gave rise to, if any."""
        self.events += 1
        if self.stopped_at is not None:
            return []
        problem = _unreadable(record)
        if problem:
            if self._records:
                self._records.anomaly(record, f"Event {record.seq} cannot be read: {problem}.", kind="unreadable")
            return self._take_fresh()
        if record.time < self.now - 1e-6 and self._records:
            self._records.anomaly(record, f"Event at {record.time:.6f} s arrived after one at {self.now:.6f} s.")
        self.now = max(self.now, record.time)
        if self.now - self._clock >= CLOCK_STEP:
            # Time has passed: let whatever runs on the clock catch up before this event changes anything.
            self._clock = self.now
            self._run_clock(record.seq)

        op = record.op
        if op == INSERT:
            self._insert(record)
        elif op == CANCEL:
            self._cancel(record)
        elif op == AMEND:
            self._amend(record)
        elif op == TRADE:
            self._trade(record)
        else:
            self._hedge(record)
        return self._take_fresh()

    def _run_clock(self, seq: int) -> None:
        for who in self.participants.values():
            if who.cut_off is None and who.unhedged_side and self._breach(who, self.now) == UNHEDGED_TIME:
                # The exchange's timer has run out. Nothing in the stream marks the moment unless
                # the participant had orders resting, so mark it here.
                expiry = who.unhedged_since + self.unhedged_seconds
                self._cut_off(who, MatchRecord(seq, expiry, who.name, "Timer", 0), UNHEDGED_TIME)
        for hook in self._on_clock:
            hook(self.now)

    def _take_fresh(self) -> list[Alert]:
        if self._fresh:
            fresh, self._fresh = self._fresh, []
            return fresh
        return []

    def gap(self, after: int, missing: int) -> list[Alert]:
        """Tell the engine that ``missing`` events after sequence number ``after`` never arrived.

        The order books cannot be rebuilt across a hole: every position, every
        fill and every price after it would be a guess, and alerts raised on
        guesses accuse people of things that did not happen. So the engine says
        what is missing, once, and stops following the match.
        """
        if self.stopped_at is None:
            self.stopped_at = after
            if self._records:
                self._records.anomaly(
                    MatchRecord(after, self.now, MARKET, "Gap", 0),
                    f"{missing} event{'' if missing == 1 else 's'} after sequence number {after} did not arrive. "
                    "The state of the order books is not known from there on; surveillance of this match "
                    "stopped at that point and nothing later in it has been examined.",
                    kind="gap", severity=Severity.CRITICAL)
        return self._take_fresh()

    def run(self, records: Iterable[MatchRecord]) -> SurveillanceResult:
        for record in records:
            self.process(record)
        return self.finalize()

    def finalize(self, outcomes: Mapping[str, Mapping[str, Any]] | None = None) -> SurveillanceResult:
        """Close the match: run the end-of-session checks and summarise.

        ``outcomes`` is the exchange's own final word on each team, where there
        is one (its score board); the positions in it are held against the
        positions the stream adds up to.
        """
        end = self.now
        replica = self.replica
        if self.stopped_at is None:
            self._settle_reports()
            if self._records and replica.ambiguous:
                self._records.anomaly(
                    MatchRecord(self.events, end, MARKET, "End", 0),
                    f"{replica.ambiguous} cancellations or amendments named an order number that was live in both "
                    "books and could have meant either. The books after them may not be the exchange's.")
            self._run_clock(self.events)
            for who in self.participants.values():
                who.close(end)
            for detector in self._detectors:
                detector.finalize(end)
            if self._records and outcomes:
                self.score_checked = self._records.score(outcomes)
        self._fresh = []

        # Mark positions exactly as the exchange's score board does (Competitor.on_timer_tick): the
        # future at its last trade, the ETF at its last trade held within the clamp around the
        # future, and an instrument that has never traded at zero.
        future_price = replica.last_traded(FUTURE)
        etf_price = replica.last_traded(ETF)
        future_mark = future_price or 0
        band = round(self.etf_clamp * future_mark)
        band -= band % self.tick
        mark = (float(future_mark), float(min(max(etf_price or 0, future_mark - band), future_mark + band)))

        records = self._records
        self.alerts.sort(key=lambda alert: (alert.time_end, alert.control, alert.competitor, alert.alert_id))
        return SurveillanceResult(
            header=self.header,
            alerts=list(self.alerts),
            participants={name: who.summary(mark) for name, who in sorted(self.participants.items())},
            market={
                "events": self.events,
                "duration": round(end, 6),
                "etf_volume": replica.market_volume[ETF],
                "future_volume": replica.market_volume[FUTURE],
                "closing_future_price": future_price,
                "closing_etf_price": etf_price,
                "closing_etf_mark": mark[1],
            },
            integrity={
                "trade_reports_checked": records.checked if records else 0,
                "hedges_checked": records.hedges_checked if records else 0,
                "scores_checked": self.score_checked,
                "discrepancies": records.discrepancies if records else 0,
                "stopped_after_sequence": self.stopped_at,
            },
            rulebook_sha256=self.rulebook.sha256,
            controls_run=[d.control for d in self._detectors],
        )

    # -- called by the detectors ---------------------------------------------------------------

    def raise_alert(self, control: Control, competitor: str, key: str, start: float, end: float,
                    description: str, *, metrics: dict[str, Any], evidence: tuple[int, ...],
                    orders: tuple[int, ...], counterparties: tuple[str, ...], severity: Severity | None,
                    title: str | None) -> None:
        identifier = alert_id(self.match_id, control.key, competitor, key)
        if identifier in self._raised:
            return
        self._raised.add(identifier)
        alert = Alert(
            alert_id=identifier, match_id=self.match_id, control=control.key, title=title or control.title,
            severity=severity or control.severity, competitor=competitor, time_start=round(start, 6),
            time_end=round(end, 6), description=description, metrics=metrics, evidence=tuple(evidence),
            orders=tuple(orders), counterparties=tuple(counterparties), regulations=control.regulations)
        self.alerts.append(alert)
        self._fresh.append(alert)

    # -- what the exchange does to a participant -------------------------------------------------

    def _who(self, record: MatchRecord) -> Participant:
        who = self.participants.get(record.competitor)
        if who is None:
            who = self.participants[record.competitor] = Participant(record.competitor)
        who.touch(record.time)
        return who

    def _message(self, who: Participant, record: MatchRecord) -> None:
        """Count a message the participant sent, and let the detectors see it."""
        if who.cut_off is not None:
            # It is still sending, so it was not cut off after all: the limits in the header are
            # not the ones the exchange was run with. The alert that said so stands.
            who.cut_off = who.cut_off_reason = None
        count = who.count_message(record.time, self.message_interval, record.seq)
        for hook in self._on_message:
            hook(who, record, count)

    def _breach(self, who: Participant, now: float) -> str | None:
        """The rule of the exchange's under which it would have disconnected the participant by ``now``."""
        if abs(who.etf_position) > self.position_limit or abs(who.future_position) > self.position_limit:
            return POSITION_LIMIT
        if who.unhedged_side and now - who.unhedged_since >= self.unhedged_seconds - 1e-6:
            return UNHEDGED_TIME
        if who.drop_old_messages(now, self.message_interval) >= self.message_limit:
            # The exchange acts on at most the limit in any window. One more and it hangs up
            # instead; that message is never in the stream, only what the exchange then did.
            return MESSAGE_LIMIT
        return None

    def _cut_off(self, who: Participant, record: MatchRecord, reason: str) -> None:
        if who.cut_off is None:
            who.cut_off, who.cut_off_reason = record.time, reason
            for hook in self._on_cut_off:
                hook(who, record, reason)

    # -- event handlers ------------------------------------------------------------------------

    def _settle_reports(self) -> None:
        """The trades of the last order to arrive have all been reported by now, or never will be."""
        expected = self.replica.expected
        if not expected and not self._misordered:
            return
        records = self._records
        if records:
            if expected:
                while expected:
                    records.missing(expected.popleft())
            elif self._misordered:
                records.anomaly(MatchRecord(self.events, self.now, MARKET, "Order", 0),
                                "The trades of one order were reported in a different sequence from the one in "
                                "which the matching engine makes them.")
        expected.clear()
        self._misordered = False

    def _insert(self, record: MatchRecord) -> None:
        replica = self.replica
        self._settle_reports()

        order = None
        if record.competitor != MARKET:
            who = self._who(record)
            own, other = (replica.best_bid, replica.best_ask) if record.side == "B" else (replica.best_ask,
                                                                                         replica.best_bid)
            price = int(record.price)
            asks, bids = replica.top_levels(record.instrument)
            if record.side == "B":
                ahead = sum(1 for level, _ in bids if level > price)
            else:
                ahead = sum(1 for level, _ in asks if level < price)
            order = OrderInfo(record.order_id, record.seq, record.time, record.side, price,
                              record.volume, record.lifespan, reference=replica.mid(FUTURE),
                              best_same=own(record.instrument), best_opposite=other(record.instrument),
                              levels_ahead=ahead)
            who.orders[record.order_id] = order
            who.inserts += 1
            who.message_lots += record.volume
            for hook in self._on_insert:
                hook(who, order, record)
            self._message(who, record)

        for fill in replica.apply(record):
            if fill.instrument == ETF:
                self.last_etf_fill = fill
            aggressor, passive = fill.aggressor, fill.passive
            if aggressor == MARKET and passive == MARKET:
                continue
            if aggressor != MARKET:
                self.participants[aggressor].taker_lots += fill.volume
                order.filled += fill.volume
            if passive != MARKET:
                resting = self.participants[passive]
                resting.maker_lots += fill.volume
                info = resting.orders.get(fill.passive_order)
                if info is not None:
                    info.filled += fill.volume
                    if info.remaining <= 0 and not replica.is_live(passive, fill.passive_order):
                        info.removed = record.time
            for hook in self._on_fill:
                hook(fill)

        if order is not None and not replica.is_live(record.competitor, record.order_id):
            order.removed = record.time  # traded in full on arrival, or fill-and-kill

    def _cancel(self, record: MatchRecord) -> None:
        replica = self.replica
        key = (record.competitor, record.order_id)
        if key in replica.automatic_cancels:
            # The unfilled remainder of a fill-and-kill order: the book's doing, not the participant's.
            replica.automatic_cancels.discard(key)
            if record.competitor != MARKET:
                who = self._who(record)
                who.automatic_cancels += 1
                who.message_lots += -record.volume
                order = who.orders.get(record.order_id)
                if order is not None:
                    order.cancelled_volume = -record.volume
            return
        live = replica.is_live(*key)
        replica.apply(record)
        if record.competitor == MARKET:
            return
        who = self._who(record)
        order = who.orders.get(record.order_id)
        if not live or order is None:
            if self._records:
                self._records.anomaly(record, f"Cancellation of order {record.order_id}, which is not in the book.")
            return
        order.removed = record.time
        order.cancelled_volume += -record.volume
        order.cancel_seq = record.seq

        # When the exchange disconnects a participant it cancels whatever the participant has
        # resting. Those cancellations are in the stream like any other, but nobody sent them.
        reason = who.cut_off_reason or self._breach(who, record.time)
        if reason:
            self._cut_off(who, record, reason)
            who.exchange_cancels += 1
            return
        who.cancels += 1
        who.message_lots += -record.volume
        for hook in self._on_cancel:
            hook(who, order, record)
        self._message(who, record)

    def _amend(self, record: MatchRecord) -> None:
        replica = self.replica
        live = replica.is_live(record.competitor, record.order_id)
        replica.apply(record)
        if record.competitor == MARKET:
            return
        who = self._who(record)
        order = who.orders.get(record.order_id)
        if not live or order is None:
            if self._records:
                self._records.anomaly(record, f"Amendment of order {record.order_id}, which is not in the book.")
            return
        who.amends += 1
        # An amendment withdraws what was left of the order and enters what is left after it.
        before = order.remaining
        who.message_lots += before + (before + record.volume)
        order.cancelled_volume += -record.volume
        if not replica.is_live(record.competitor, record.order_id):
            order.removed = record.time
        for hook in self._on_amend:
            hook(who, order, record)
        self._message(who, record)

    def _trade(self, record: MatchRecord) -> None:
        if record.competitor == MARKET:
            if self._records:
                self._records.anomaly(record, "A trade report that names no competitor.")
            return
        records = self._records
        expected = self.replica.expected
        if records:
            # The exchange reports an order's trades in the sequence the matching loop makes
            # them. Look for this one among those still owed, so that a report gone missing is
            # laid at the door of the order it was for, and not of whichever came next.
            at = next((i for i, item in enumerate(expected) if item.is_reported_by(record)), None)
            if at is not None:
                self._misordered = self._misordered or at > 0
                del expected[at]
                records.matched()
            else:
                at = next((i for i, item in enumerate(expected)
                           if (item.competitor, item.order_id) == (record.competitor, record.order_id)), None)
                if at is not None:
                    altered = expected[at]
                    del expected[at]
                    records.mismatch(record, altered)
                else:
                    records.unexpected(record)
        who = self._who(record)
        who.apply_execution(record, self.position_limit)
        for hook in self._on_position:
            hook(who, record)
        if abs(who.etf_position) > self.position_limit:
            self._cut_off(who, record, POSITION_LIMIT)

    def _hedge(self, record: MatchRecord) -> None:
        if record.competitor == MARKET:
            if self._records:
                self._records.anomaly(record, "A hedge that names no competitor.")
            return
        who = self._who(record)
        if self._records:
            self._records.hedge(record, self.replica.hedge_prices(record.side, record.volume, record.price))
        who.apply_execution(record, self.position_limit)
        self._message(who, record)
        for hook in self._on_position:
            hook(who, record)
        if abs(who.future_position) > self.position_limit:
            self._cut_off(who, record, POSITION_LIMIT)


def _unreadable(record: MatchRecord) -> str | None:
    """Why an event cannot be processed, or ``None`` if it can.

    The exchange never writes such events; a stream that has one has been
    damaged or tampered with. Checked here so that nothing downstream has to
    wonder whether a field is there.
    """
    if not isinstance(record.time, (int, float)) or not math.isfinite(record.time) or record.time < 0:
        return "it has no time"
    if not isinstance(record.order_id, int) or not isinstance(record.competitor, str):
        return "it has no order number or no owner"
    op = record.op
    if op in (CANCEL, AMEND):
        return None if isinstance(record.volume, int) and record.volume <= 0 else "it removes no volume"
    if op not in (INSERT, TRADE, HEDGE):
        return f"the operation {op!r} is not one the exchange records"
    if record.instrument not in (FUTURE, ETF) or record.side not in ("A", "B"):
        return "it names no instrument or no side"
    if not isinstance(record.volume, int) or record.volume <= 0:
        return "it has no volume"
    if not isinstance(record.price, (int, float)) or not math.isfinite(record.price) or record.price <= 0:
        return "it has no price"
    if op == INSERT and (record.lifespan not in ("F", "G") or record.price != int(record.price)):
        return "it has no lifespan or a price that is not a whole number of cents"
    if op == HEDGE and record.instrument != FUTURE:
        return "it hedges in something other than the future"
    if op == TRADE and record.fee is not None and not isinstance(record.fee, int):
        return "its fee is not a whole number of cents"
    return None


def header_for(match_id: str, source: str = "csv", *, teams: Sequence[str] = (), limits: dict | None = None,
               fees: dict | None = None, instrument: dict | None = None, market_data: str | None = None,
               market_data_sha256: str | None = None, started_at: str = "", speed: float = 1.0) -> MatchHeader:
    """A header with the exchange's standard settings filled in for anything not given."""
    return MatchHeader(
        match_id=match_id, source=source, started_at=started_at, teams=list(teams),
        market_data=market_data, market_data_sha256=market_data_sha256,
        limits=limits or {"ActiveOrderCountLimit": 10, "ActiveVolumeLimit": 200, "MessageFrequencyInterval": 1.0,
                          "MessageFrequencyLimit": 50, "PositionLimit": 100},
        fees=fees or {"Maker": -0.0001, "Taker": 0.0002},
        instrument=instrument or {"EtfClamp": 0.002, "TickSize": 1.0}, speed=speed)
