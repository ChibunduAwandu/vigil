"""Market-conduct controls: patterns of trading that mislead or disrupt."""
from __future__ import annotations

import collections
from dataclasses import dataclass

from vigil.rtg.events import ETF, FUTURE, MatchRecord
from vigil.surveillance.alerts import Severity
from vigil.surveillance.detectors.base import Detector
from vigil.surveillance.market import MARKET, Fill
from vigil.surveillance.state import OrderInfo, Participant

# == MKT-01 ======================================================================================

@dataclass
class _Shown:
    """Size that was shown and withdrawn: a spoofing candidate until proven a re-quote."""

    order: OrderInfo
    lots: int  # how much of it was withdrawn
    withdrawn: float
    seq: int  # of the cancellation or amendment that withdrew it
    opposite_lots: int
    confirm_after: float


@dataclass
class _Pull:
    """One withdrawal of shown size: everything taken off one side of the book in one go."""

    time: float
    shown: list[_Shown]

    @property
    def lots(self) -> int:
        return sum(item.lots for item in self.shown)

    @property
    def levels(self) -> int:
        return len({item.order.price for item in self.shown})


class Spoofing(Detector):
    """MKT-01: size shown on one side while trading on the other, then withdrawn.

    An episode is a block of orders that (a) was large, (b) was placed where
    the market could see it but not where it would trade, (c) was withdrawn
    soon after with next to nothing executed, (d) sat in the book while the
    participant executed on the opposite side, and (e) was not replaced. The
    last test is what separates this from market making: a market maker who
    pulls a quote puts another one back, because the quote was there to trade.

    Size can be withdrawn by cancelling an order or by amending it down, and
    the two are treated alike. A replacement has to be a real one: an order
    that rests, priced near the one it replaces, and it makes good only as
    much withdrawn size as it is itself large enough to answer for.

    The block may be a single order or several at neighbouring prices pulled
    in quick succession; the second is layering, and is reported as such.
    """

    control = "MKT-01"

    def __init__(self, engine):
        super().__init__(engine)
        self._fills: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self._pending: dict[str, list[_Shown]] = collections.defaultdict(list)
        self._episodes: dict[str, list[_Pull]] = collections.defaultdict(list)  # not yet reported
        self._last_alert: dict[str, float] = {}
        self._history = float(self.param("max_lifetime", 5.0)) + 1.0

    def on_fill(self, fill: Fill) -> None:
        if fill.instrument != ETF:
            return
        for name, side in ((fill.buyer, "B"), (fill.seller, "A")):
            if name != MARKET:
                fills = self._fills[name]
                fills.append((fill.time, side, fill.volume))
                while fills and fills[0][0] < fill.time - self._history:
                    fills.popleft()

    def on_insert(self, who: Participant, order: OrderInfo, record: MatchRecord) -> None:
        pending = self._pending.get(who.name)
        if not pending:
            return
        # A fresh resting order on the same side, soon after the withdrawal and at about the same
        # price, is a re-quote. An order that does not rest replaces nothing.
        if order.lifespan != "G":
            return
        reach = float(self.param("requote_ticks", 15)) * self.engine.tick
        answers_for = order.volume / float(self.param("requote_ratio", 0.5))
        kept = []
        for shown in pending:
            if (shown.order.side == order.side and record.time <= shown.confirm_after
                    and shown.lots <= answers_for and abs(order.price - shown.order.price) <= reach):
                answers_for -= shown.lots
            else:
                kept.append(shown)
        self._pending[who.name] = kept

    def on_cancel(self, who: Participant, order: OrderInfo, record: MatchRecord) -> None:
        self._withdrawn(who, order, record)

    def on_amend(self, who: Participant, order: OrderInfo, record: MatchRecord) -> None:
        self._withdrawn(who, order, record)

    def _withdrawn(self, who: Participant, order: OrderInfo, record: MatchRecord) -> None:
        self._settle(who.name, record.time)
        lots = -record.volume
        if order.lifespan != "G" or lots < int(self.param("min_order_lots", 10)):
            return
        if record.time - order.entered > float(self.param("max_lifetime", 5.0)):
            return
        if order.filled > float(self.param("max_fill_ratio", 0.1)) * order.volume:
            return
        if order.levels_ahead >= int(self.param("visible_levels", 5)) or order.was_marketable():
            return  # out of sight it colours nothing; marketable, it was meant to trade
        opposite = "A" if order.side == "B" else "B"
        traded = sum(volume for when, side, volume in self._fills[who.name]
                     if side == opposite and when >= order.entered)
        if traded < int(self.param("min_opposite_lots", 1)):
            return
        self._pending[who.name].append(_Shown(
            order, lots, record.time, record.seq, traded, record.time + float(self.param("requote_window", 0.3))))

    def on_clock(self, now: float) -> None:
        for name in list(self._pending):
            self._settle(name, now)

    def finalize(self, end_time: float) -> None:
        for name in list(self._pending):
            self._settle(name, float("inf"))
        for name, episodes in self._episodes.items():
            if episodes and name in self._last_alert:
                self._report(name)  # what has accumulated since the last alert

    def _settle(self, name: str, now: float) -> None:
        """Turn candidates whose time to be re-quoted has passed into withdrawals, and alert if they pile up."""
        pending = self._pending.get(name)
        if not pending:
            return
        # Size taken off one side in quick succession is one withdrawal, however many orders it
        # was spread over and however the cancellations were spaced.
        gap = float(self.param("pull_gap", 0.5))
        minimum = int(self.param("min_lots", 40))
        pulls: list[_Pull] = []
        waiting: list[_Shown] = []
        for side in ("A", "B"):
            group: list[_Shown] = []
            for shown in [*sorted((s for s in pending if s.order.side == side), key=lambda s: s.withdrawn), None]:
                if shown is not None and (not group or shown.withdrawn - group[-1].withdrawn <= gap):
                    group.append(shown)
                    continue
                if group:
                    if now <= group[-1].withdrawn + gap or any(now <= item.confirm_after for item in group):
                        waiting.extend(group)  # more may yet join it, or part of it be re-quoted
                    elif sum(item.lots for item in group) >= minimum:
                        pulls.append(_Pull(group[-1].withdrawn, group))
                group = [shown] if shown is not None else []
        self._pending[name] = waiting
        if not pulls:
            return

        episodes = self._episodes[name]
        episodes.extend(sorted(pulls, key=lambda pull: pull.time))
        latest = episodes[-1].time
        window = float(self.param("window", 60.0))
        if name not in self._last_alert:
            # Not yet a pattern: it becomes one when enough episodes fall inside one window.
            episodes[:] = [pull for pull in episodes if pull.time >= latest - window]
            if len(episodes) < int(self.param("min_episodes", 3)):
                return
        elif latest - self._last_alert[name] < window:
            return  # one alert per window; the next, or the end of the match, reports what piles up
        self._report(name)

    def _report(self, name: str) -> None:
        episodes = self._episodes[name]
        every = [item for pull in episodes for item in pull.shown]
        first, latest = min(item.order.entered for item in every), episodes[-1].time
        levels = max(pull.levels for pull in episodes)
        layering = levels >= int(self.param("layering_levels", 3))
        shown_lots = sum(pull.lots for pull in episodes)
        traded_lots = sum(max(item.opposite_lots for item in pull.shown) for pull in episodes)
        mean_life = sum(item.withdrawn - item.order.entered for item in every) / len(every)
        self.alert(
            name, f"{every[0].order.order_id}", first, latest,
            f"{len(episodes)} times in {latest - first:.0f} s, size was shown and withdrawn unexecuted "
            f"({shown_lots} lots in all, resting {mean_life:.2f} s on average) while {traded_lots} lots traded "
            f"on the opposite side"
            + (f"; each time up to {levels} price levels were pulled together." if layering else "."),
            metrics={"episodes": len(episodes), "shown_lots": shown_lots, "opposite_traded_lots": traded_lots,
                     "mean_lifetime": round(mean_life, 3), "levels_pulled_together": levels,
                     "window": float(self.param("window", 60.0))},
            evidence=tuple(seq for item in every for seq in (item.order.seq, item.seq)),
            orders=tuple(dict.fromkeys(item.order.order_id for item in every)),
            title="Layering" if layering else None)
        self._last_alert[name] = latest
        self._episodes[name] = []


# == MKT-02 ======================================================================================

@dataclass
class _PairTrade:
    time: float
    seq: int
    buyer: str
    aggressor: str
    volume: int
    offset: float  # trade price relative to fair value, ticks


class WashTrading(Detector):
    """MKT-02: two participants passing the same lots back and forth.

    Counterparties are known from the replica, so every execution between two
    competitors is attributed to the pair. Two competitors trading with each
    other in both directions is ordinary: one picks off the other's stale
    quotes one way, then the other way. What marks a wash is that the trading
    is reciprocal *and* either

    * concentrated: each of the two does most of its business with the other,
      which no one trading for profit against a whole market does; or
    * circular: lots that go one way come straight back at the same price
      relative to fair value, *with the roles reversed*: whoever rested the
      order on the way out hits the other's order on the way back. A trader
      picking off a market maker is the aggressor in both directions; two
      parties taking turns to post and to hit are handing lots round a circle.
    """

    control = "MKT-02"

    def __init__(self, engine):
        super().__init__(engine)
        self._trades: dict[tuple[str, str], collections.deque[_PairTrade]] = collections.defaultdict(
            collections.deque)
        self._volume: dict[str, collections.deque[tuple[float, int]]] = collections.defaultdict(collections.deque)
        self._last_alert: dict[tuple[str, str], float] = {}
        self._window = float(self.param("window", 120.0))

    def on_fill(self, fill: Fill) -> None:
        if fill.instrument != ETF:
            return
        for name in (fill.aggressor, fill.passive):
            if name != MARKET:
                self._volume[name].append((fill.time, fill.volume))
        if MARKET in (fill.aggressor, fill.passive) or fill.aggressor == fill.passive:
            return
        fair = self.engine.replica.mid(FUTURE)
        if fair is None:
            return
        pair = tuple(sorted((fill.aggressor, fill.passive)))
        trades = self._trades[pair]
        trades.append(_PairTrade(fill.time, fill.seq, fill.buyer, fill.aggressor, fill.volume,
                                 (fill.price - fair) / self.engine.tick))
        while trades and trades[0].time < fill.time - self._window:
            trades.popleft()
        self._evaluate(pair, trades, fill.time)

    def _traded(self, name: str, now: float) -> int:
        volume = self._volume[name]
        while volume and volume[0][0] < now - self._window:
            volume.popleft()
        return sum(lots for _, lots in volume)

    def _evaluate(self, pair: tuple[str, str], trades: collections.deque[_PairTrade], now: float) -> None:
        first, second = pair
        bought = {first: 0, second: 0}
        count = {first: 0, second: 0}
        for trade in trades:
            bought[trade.buyer] += trade.volume
            count[trade.buyer] += 1
        total = bought[first] + bought[second]
        if min(bought.values()) < int(self.param("min_round_trip_lots", 30)):
            return
        if min(count.values()) < int(self.param("min_trades_each_way", 2)):
            return
        imbalance = abs(bought[first] - bought[second]) / total
        if imbalance > float(self.param("max_imbalance", 0.35)):
            return
        if now - self._last_alert.get(pair, -1e9) < self._window:
            return

        share = min(total / max(self._traded(name, now), 1) for name in pair)
        returned = self._returned(pair, trades)
        returned_share = 2 * returned / total
        concentrated = share >= float(self.param("min_pair_share", 0.5))
        circular = (returned >= int(self.param("min_returned_lots", 60))
                    and returned_share >= float(self.param("min_returned_share", 0.75)))
        if not (concentrated or circular):
            return
        self._last_alert[pair] = now

        for name, other in ((first, second), (second, first)):
            self.alert(
                name, f"{other}:{trades[0].seq}", trades[0].time, now,
                f"Bought {bought[name]} and sold {bought[other]} lots against {other} in {len(trades)} "
                f"executions over {now - trades[0].time:.0f} s. Trading between the two made up at least "
                f"{share:.0%} of each one's volume in that time, and {returned} lots were handed straight "
                f"back at a matching price.",
                metrics={"lots_bought": bought[name], "lots_sold": bought[other], "imbalance": round(imbalance, 3),
                         "pair_share_of_volume": round(share, 3), "returned_lots": returned,
                         "returned_share": round(returned_share, 3), "executions": len(trades),
                         "window": self._window},
                evidence=tuple(t.seq for t in trades), counterparties=(other,))

    def _returned(self, pair: tuple[str, str], trades: collections.deque[_PairTrade]) -> int:
        """Lots that went from one to the other and came back promptly, at a matching price, roles reversed.

        "Matching" is judged relative to fair value at the time of each leg, so
        a pair that re-pegs its price as the market moves is still caught.
        """
        first, second = pair
        patience = float(self.param("max_return_seconds", 10.0))
        tolerance = float(self.param("max_price_gap_ticks", 1.5))
        # Per holder: lots it bought from the other and has not handed back, oldest first.
        waiting: dict[str, list[tuple[float, int, float, str]]] = {first: [], second: []}
        returned = 0
        for trade in trades:
            seller = second if trade.buyer == first else first
            remaining = trade.volume
            still_waiting = []
            for when, lots, offset, aggressor in waiting[seller]:
                if trade.time - when > patience:
                    continue  # held too long to count as handed straight back
                if remaining and abs(trade.offset - offset) <= tolerance and aggressor != trade.aggressor:
                    take = min(lots, remaining)
                    returned += take
                    remaining -= take
                    lots -= take
                if lots:
                    still_waiting.append((when, lots, offset, aggressor))  # this trade did not answer it; a later one may
            waiting[seller] = still_waiting
            if remaining:
                waiting[trade.buyer].append((trade.time, remaining, trade.offset, trade.aggressor))
        return returned


# == MKT-03 ======================================================================================

class QuoteStuffing(Detector):
    """MKT-03: bursts of orders entered and cancelled at once, with nothing traded."""

    control = "MKT-03"

    def __init__(self, engine):
        super().__init__(engine)
        self._messages: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self._last_fill: dict[str, float] = {}
        self._bursts: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self._last_alert: dict[str, float] = {}

    def on_position(self, who: Participant, record: MatchRecord) -> None:
        if record.instrument == ETF:
            self._last_fill[who.name] = record.time

    def on_message(self, who: Participant, record: MatchRecord, count: int) -> None:
        now = record.time
        messages = self._messages[who.name]
        messages.append((now, record.seq, record.op, record.order_id))
        while messages and messages[0][0] <= now - 1.0:
            messages.popleft()
        if len(messages) < int(self.param("burst_messages", 24)):
            return
        if self._last_fill.get(who.name, -1e9) > now - 1.0:
            return  # something traded: whatever this is, it is not orders with no intent to trade
        entered = [who.orders[order_id] for _, _, op, order_id in messages
                   if op == "Insert" and order_id in who.orders]
        if not entered:
            return
        life = float(self.param("max_order_life", 0.5))
        flashed = sum(1 for order in entered
                      if order.removed is not None and order.filled == 0 and order.removed - order.entered <= life)
        if flashed < float(self.param("burst_cancel_ratio", 0.8)) * len(entered):
            return
        bursts = self._bursts[who.name]
        if bursts and now - bursts[-1][0] < 1.0:
            return  # still the same burst
        bursts.append((now, record.seq, len(messages), flashed))
        window = float(self.param("window", 60.0))
        while bursts and bursts[0][0] < now - window:
            bursts.popleft()
        if len(bursts) < int(self.param("min_bursts", 5)) or now - self._last_alert.get(who.name, -1e9) < window:
            return
        self._last_alert[who.name] = now
        peak = max(burst[2] for burst in bursts)
        self.alert(
            who.name, f"{bursts[0][1]}", bursts[0][0] - 1.0, now,
            f"{len(bursts)} bursts of up to {peak} order messages a second in {now - bursts[0][0] + 1:.0f} s, "
            f"in which orders were cancelled within {life:g} s of entry and none traded.",
            metrics={"bursts": len(bursts), "peak_messages_per_second": peak,
                     "orders_flashed": sum(burst[3] for burst in bursts), "window": window},
            evidence=tuple(burst[1] for burst in bursts))


# == MKT-04 ======================================================================================

@dataclass
class _Print:
    time: float
    seq: int
    aggressor: str
    order_id: int
    side: str
    price: int
    volume: int
    through: float  # ticks paid through fair value (positive: the aggressor paid up)
    position_after: int


class MarkingTheClose(Detector):
    """MKT-04: paying through fair value at the close to move the mark on a position.

    The exchange values an ETF position at the last traded price (within a
    band around the future). A participant who is long therefore gains from a
    high last print, and can buy one. The tell is an aggressive trade in the
    closing seconds that is uneconomic in itself (priced through fair value)
    and that adds to the very position whose valuation it improves.

    Whose trade closed the session is read off the book, not off the
    participants' own trades: the last print may be one between two orders of
    the market's, and then nobody marked anything. A print that would have
    paid had it stood, and was then traded over by someone else, is still an
    attempt, and is reported as one.
    """

    control = "MKT-04"

    def __init__(self, engine):
        super().__init__(engine)
        self._prints: collections.deque[_Print] = collections.deque()
        self._window = float(self.param("closing_window", 30.0))
        self._unbooked = (-1, 0)  # (sequence number of the incoming order, lots it has traded so far)

    def on_fill(self, fill: Fill) -> None:
        if fill.instrument != ETF or fill.aggressor == MARKET:
            return
        fair = self.engine.replica.mid(FUTURE)
        if fair is None:
            return
        sign = 1 if fill.aggressor_side == "B" else -1
        # None of this order's fills has been booked yet: the exchange's trade reports follow.
        earlier = self._unbooked[1] if self._unbooked[0] == fill.seq else 0
        self._unbooked = (fill.seq, earlier + fill.volume)
        position = self.engine.participants[fill.aggressor].etf_position + sign * (earlier + fill.volume)
        prints = self._prints
        prints.append(_Print(fill.time, fill.seq, fill.aggressor, fill.aggressor_order, fill.aggressor_side,
                             fill.price, fill.volume, sign * (fill.price - fair) / self.engine.tick, position))
        while prints and prints[0].time < fill.time - self._window:
            prints.popleft()

    def finalize(self, end_time: float) -> None:
        engine = self.engine
        closing = engine.last_etf_fill  # the trade that left the ETF's last traded price where it is
        future_price = engine.replica.last_traded(FUTURE)
        if closing is None or future_price is None:
            return
        band = round(engine.etf_clamp * future_price)
        band -= band % engine.tick
        mark = min(max(closing.price, future_price - band), future_price + band)  # as the score board marks it
        minimum = float(self.param("min_through_ticks", 1.5))

        by_name: dict[str, list[_Print]] = collections.defaultdict(list)
        for this in self._prints:
            if this.time < end_time - self._window:
                continue
            adds = this.position_after > 0 if this.side == "B" else this.position_after < 0
            if this.through >= minimum and adds:
                by_name[this.aggressor].append(this)

        def marked_at(price: int) -> int:
            return min(max(price, future_price - band), future_price + band)

        worthwhile = float(self.param("min_benefit", 25.0))
        for name, prints in by_name.items():
            who = engine.participants[name]
            benefit = who.etf_position * (mark - future_price) / 100.0
            set_the_close = closing.aggressor == name and any(this.seq == closing.seq for this in prints)
            # What the boldest of its prints would have been worth to the position it ended with,
            # had that print been the session's last.
            boldest = max(prints, key=lambda this: this.through)
            aimed_at = who.etf_position * (marked_at(boldest.price) - future_price) / 100.0
            orders = tuple(dict.fromkeys(this.order_id for this in prints))
            metrics = {"orders": len(orders), "executions": len(prints), "lots": sum(p.volume for p in prints),
                       "max_ticks_through_fair": round(boldest.through, 2),
                       "closing_position": who.etf_position, "closing_mark": mark,
                       "future_price": future_price, "mark_benefit": round(benefit, 2),
                       "benefit_aimed_at": round(aimed_at, 2),
                       "set_the_closing_price": set_the_close, "closing_window": self._window}
            evidence = tuple(dict.fromkeys(this.seq for this in prints))
            plural = "order" if len(orders) == 1 else "orders"
            if set_the_close and benefit >= worthwhile:
                self.alert(
                    name, "close", prints[0].time, end_time,
                    f"In the last {end_time - prints[0].time:.1f} s, {len(orders)} aggressive {plural} traded up "
                    f"to {boldest.through:.1f} ticks through fair value, and the last of them set the closing "
                    f"price, which marks a position of {who.etf_position:+d} lots ${benefit:,.2f} in the "
                    f"participant's favour.", metrics=metrics, evidence=evidence, orders=orders)
            elif aimed_at >= worthwhile or len(orders) >= int(self.param("min_attempt_orders", 3)):
                outcome = (f"The last of them did set the closing price, but it moved the mark on the "
                           f"participant's {who.etf_position:+d} lots by only ${benefit:,.2f}."
                           if set_the_close else
                           f"Had the boldest of them been the session's last trade it would have marked the "
                           f"participant's {who.etf_position:+d} lots ${aimed_at:,.2f} in its favour; the "
                           f"session's last trade was another.")
                self.alert(
                    name, "close-attempt", prints[0].time, end_time,
                    f"{len(orders)} aggressive {plural} in the closing {self._window:g} s traded up to "
                    f"{boldest.through:.1f} ticks through fair value in the direction of the participant's "
                    f"position. {outcome}",
                    metrics=metrics, evidence=evidence, orders=orders, severity=Severity.MEDIUM,
                    title="Marking the close: attempt")


# == MKT-05 ======================================================================================

class OrderToTradeRatio(Detector):
    """MKT-05: far more order messages than executions, over the whole session."""

    control = "MKT-05"

    def finalize(self, end_time: float) -> None:
        for who in self.engine.participants.values():
            orders = who.order_messages()
            if orders < int(self.param("min_orders", 200)):
                continue
            by_number, by_volume = who.order_to_trade()
            limit_number = float(self.param("max_ratio_by_number", 40.0))
            limit_volume = float(self.param("max_ratio_by_volume", 80.0))
            if by_number <= limit_number and by_volume <= limit_volume:
                continue
            self.alert(
                who.name, "session", who.first_seen or 0.0, end_time,
                f"{orders} order messages against {len(who.executed_orders)} executed orders: a ratio of "
                f"{by_number:,.1f} by number (limit {limit_number:g}) and {by_volume:,.1f} by volume "
                f"(limit {limit_volume:g}).",
                metrics={"order_messages": orders, "executed_orders": len(who.executed_orders),
                         "order_message_lots": who.message_lots, "executed_lots": who.executed_lots,
                         "ratio_by_number": round(by_number, 2), "ratio_by_volume": round(by_volume, 2),
                         "limit_by_number": limit_number, "limit_by_volume": limit_volume})


# == MKT-06 ======================================================================================

class OffMarketOrder(Detector):
    """MKT-06: an order priced implausibly far from the market."""

    control = "MKT-06"
    COOLDOWN = 10.0  # seconds: orders this close together are reported as one incident

    def __init__(self, engine):
        super().__init__(engine)
        self._last: dict[str, float] = {}
        self._flagged: dict[tuple[str, int], float] = {}

    def on_insert(self, who: Participant, order: OrderInfo, record: MatchRecord) -> None:
        if order.reference is None or record.instrument != ETF:
            return
        deviation = (order.price - order.reference) / order.reference
        collar = float(self.param("collar", 0.03))
        if abs(deviation) <= collar:
            return
        self._flagged[(who.name, order.order_id)] = deviation
        if record.time - self._last.get(who.name, -1e9) < self.COOLDOWN:
            return
        self._last[who.name] = record.time
        side = "buy" if order.side == "B" else "sell"
        self.alert(
            who.name, f"order:{record.seq}", record.time, record.time,
            f"Order to {side} {order.volume} lots at {order.price / 100:,.2f} is {abs(deviation):.1%} away from "
            f"the reference price of {order.reference / 100:,.2f} (collar {collar:.0%}).",
            metrics={"price": order.price, "reference": order.reference, "deviation": round(deviation, 5),
                     "collar": collar, "volume": order.volume},
            evidence=(record.seq,), orders=(order.order_id,))

    def on_fill(self, fill: Fill) -> None:
        # Such an order actually trading is worse than it merely being entered.
        if fill.instrument != ETF or fill.aggressor == MARKET:
            return
        key = (fill.aggressor, fill.aggressor_order)
        deviation = self._flagged.get(key)
        if deviation is None:
            return
        fair = self.engine.replica.mid(FUTURE)
        if fair is None or abs(fill.price - fair) / fair <= float(self.param("collar", 0.03)):
            return
        del self._flagged[key]
        self.alert(
            fill.aggressor, f"fill:{fill.seq}", fill.time, fill.time,
            f"An off-market order traded {fill.volume} lots at {fill.price / 100:,.2f}, "
            f"{abs(fill.price - fair) / fair:.1%} away from the reference price of {fair / 100:,.2f}.",
            metrics={"price": fill.price, "reference": fair, "volume": fill.volume},
            evidence=(fill.seq,), orders=(fill.aggressor_order,),
            severity=Severity.parse(self.param("executed_severity", "HIGH")),
            title="Off-market execution")
