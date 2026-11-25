"""Risk-limit controls: the exchange's own rules, watched independently of the exchange."""
from __future__ import annotations

import collections
from dataclasses import dataclass

from vigil.rtg.events import ETF, FUTURE, MatchRecord
from vigil.surveillance.alerts import Severity
from vigil.surveillance.detectors.base import Detector
from vigil.surveillance.state import INSTRUMENT_NAMES, Participant


class PositionLimit(Detector):
    """LIM-01: a position beyond the exchange's limit."""

    control = "LIM-01"

    def __init__(self, engine):
        super().__init__(engine)
        self._over: set[tuple[str, int]] = set()

    def on_position(self, who: Participant, record: MatchRecord) -> None:
        limit = self.engine.position_limit
        for instrument, position in ((ETF, who.etf_position), (FUTURE, who.future_position)):
            key = (who.name, instrument)
            if abs(position) > limit:
                if key not in self._over:  # report the excursion once, when it begins
                    self._over.add(key)
                    name = INSTRUMENT_NAMES[instrument]
                    self.alert(
                        who.name, f"{instrument}:{record.seq}", record.time, record.time,
                        f"{name} position of {position:+d} lots exceeds the limit of {limit}.",
                        metrics={"instrument": name, "position": position, "limit": limit},
                        evidence=(record.seq,), orders=(record.order_id,))
            else:
                self._over.discard(key)


@dataclass
class _Excursion:
    side: int = 0  # +1 long beyond the allowance, -1 short beyond it, 0 inside
    since: float = 0.0
    seq: int = 0
    lots: int = 0  # the net position, as last seen while outside the allowance
    peak: int = 0
    warned: bool = False
    breached: bool = False


class UnhedgedExposure(Detector):
    """LIM-02: ETF and future positions left un-hedged beyond the allowance for too long.

    Mirrors ``ready_trader_go.unhedged_lots``: the clock starts when the net
    position leaves the allowance on one side and stops when it comes back,
    and going straight from one side to the other restarts it. The exchange
    keeps that clock on the wall, so in a match run at another speed the limit
    is that many times longer in match time.
    """

    control = "LIM-02"

    def __init__(self, engine):
        super().__init__(engine)
        self._state: dict[str, _Excursion] = collections.defaultdict(_Excursion)
        self._allowance = int(self.param("allowance_lots", 10))
        self._limit = float(self.param("time_limit", 60.0)) * engine.speed
        self._warn_after = self._limit * float(self.param("warn_fraction", 0.75))

    def on_position(self, who: Participant, record: MatchRecord) -> None:
        net = who.unhedged
        side = 1 if net > self._allowance else -1 if net < -self._allowance else 0
        state = self._state[who.name]
        if side != state.side:
            self._check(who, state, record.time)  # it may have run past the limit since anyone last looked
            self._end(who, state, record.time)
            self._state[who.name] = state = _Excursion(side=side, since=record.time, seq=record.seq)
        state.lots = net
        state.peak = max(state.peak, abs(net))
        self._check(who, state, record.time)

    def on_clock(self, now: float) -> None:
        for name, state in self._state.items():
            if state.side:
                self._check(self.engine.participants[name], state, now)

    def finalize(self, end_time: float) -> None:
        for name, state in self._state.items():
            who = self.engine.participants[name]
            self._check(who, state, end_time)  # an excursion can run out between the last clock step and the end
            self._end(who, state, end_time)

    def _end(self, who: Participant, state: _Excursion, now: float) -> None:
        if state.side:
            who.longest_unhedged = max(who.longest_unhedged, now - state.since)

    def _check(self, who: Participant, state: _Excursion, now: float) -> None:
        if not state.side:
            return
        elapsed = now - state.since
        metrics = {"unhedged_lots": state.lots, "peak_unhedged_lots": state.peak * state.side,
                   "allowance_lots": self._allowance, "seconds": round(elapsed, 3), "time_limit": self._limit}
        if elapsed >= self._limit - 1e-6:
            if not state.breached:
                # Reported as of the moment the limit ran out, whenever it is that we come to look.
                state.breached = state.warned = True
                metrics["seconds"] = self._limit
                self.alert(who.name, f"breach:{state.seq}", state.since, state.since + self._limit,
                           f"Held more than the allowance of {self._allowance} lots un-hedged for the whole of "
                           f"the {self._limit:.0f} s allowed ({abs(state.lots)} lots when last it traded).",
                           metrics=metrics, evidence=(state.seq,))
        elif elapsed >= self._warn_after and not state.warned:
            state.warned = True
            self.alert(who.name, f"warn:{state.seq}", state.since, now,
                       f"Un-hedged for {elapsed:.0f} s of the {self._limit:.0f} s allowed "
                       f"({abs(state.lots)} lots against an allowance of {self._allowance}).",
                       metrics=metrics, evidence=(state.seq,), severity=Severity.MEDIUM,
                       title="Un-hedged exposure: time limit approaching")


class MessageRate(Detector):
    """LIM-03: order messages arriving faster than the exchange's throttle allows.

    The exchange counts every message it receives and hangs up on the one that
    takes a participant's window past the limit. It never acts on that
    message, so the message is never in the event stream: a genuine stream
    holds at most the limit in any window. What a breach leaves behind is the
    limit reached and then the exchange cancelling the participant's orders
    or, if it had none resting, nothing at all from the participant again.
    Those are the two things this control looks for.

    Messages the exchange rejected, or that changed nothing, count towards its
    limit and leave no event, so the count here is a lower bound. A breach made
    up of such messages cannot be seen in the stream at all.
    """

    control = "LIM-03"

    def __init__(self, engine):
        super().__init__(engine)
        self._last_warning: dict[str, float] = {}
        self._reported: set[str] = set()
        self._warn_at = float(self.param("warn_fraction", 0.8)) * engine.message_limit
        self._cooldown = float(self.param("warn_cooldown", 30.0))

    def on_message(self, who: Participant, record: MatchRecord, count: int) -> None:
        now, limit = record.time, self.engine.message_limit
        interval = self.engine.message_interval
        metrics = {"messages": count, "limit": limit, "interval": interval}
        if count > limit:
            # Not something the exchange allows: the limits in the match header are not the ones
            # it was run with, or the stream is not the exchange's.
            self._breach(who, record.seq, who.message_times[0], now,
                         f"{count} order messages in {interval:g} s; the limit is {limit}.", metrics)
        elif count >= self._warn_at and now - self._last_warning.get(who.name, -1e9) >= self._cooldown:
            self._last_warning[who.name] = now
            self.alert(who.name, f"warn:{record.seq}", who.message_times[0], now,
                       f"{count} order messages in {interval:g} s, {count / limit:.0%} of the limit of {limit}.",
                       metrics=metrics, evidence=(record.seq,), severity=Severity.LOW,
                       title="Message rate: limit approaching")

    def on_cut_off(self, who: Participant, record: MatchRecord, reason: str) -> None:
        from vigil.surveillance.engine import MESSAGE_LIMIT
        if reason != MESSAGE_LIMIT:
            return
        limit, interval = self.engine.message_limit, self.engine.message_interval
        count = len(who.message_times)
        self._breach(who, record.seq, who.message_times[0], record.time,
                     f"{count} order messages in {interval:g} s, the most the exchange will act on, and then the "
                     f"exchange cancelled the participant's orders: it was disconnected for sending more.",
                     {"messages": count, "limit": limit, "interval": interval, "disconnected": True})

    def finalize(self, end_time: float) -> None:
        limit, interval = self.engine.message_limit, self.engine.message_interval
        quiet = float(self.param("silence_seconds", 5.0))
        for who in self.engine.participants.values():
            if who.last_message is None or who.messages_at_last < limit or who.cut_off is not None:
                continue
            silence = end_time - who.last_message
            if silence >= quiet:
                self._breach(who, who.last_message_seq, who.last_message - interval, who.last_message,
                             f"{who.messages_at_last} order messages in {interval:g} s, the most the exchange will "
                             f"act on, and then nothing for the remaining {silence:.0f} s of the match: what a "
                             f"participant disconnected for sending more leaves behind.",
                             {"messages": who.messages_at_last, "limit": limit, "interval": interval,
                              "silent_seconds": round(silence, 3)})

    def _breach(self, who: Participant, seq: int, start: float, end: float, description: str, metrics: dict) -> None:
        if who.name not in self._reported:
            self._reported.add(who.name)
            self.alert(who.name, f"breach:{seq}", start, end, description, metrics=metrics, evidence=(seq,))
