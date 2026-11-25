"""Deterministic replay of a Ready Trader Go match in simulated time.

The real exchange runs on the wall clock: a fifteen-minute session takes
fifteen minutes and two runs never see quite the same interleaving of events.
That is fine for a competition and useless for research, so this module swaps
the wall clock for a discrete-event loop and keeps everything else.

"Everything else" is literal. The order books, the matching, the fee
calculation, the per-competitor risk checks, the unhedged-lots timer and the
message-rate limiter are the exchange's own classes, driven here exactly the
way ``ready_trader_go.exchange`` drives them. Auto-traders are ordinary
``BaseAutoTrader`` subclasses: their outgoing bytes are decoded from the wire
format and their callbacks are invoked with the same arguments the network
layer would pass. An ``autotrader.py`` that runs here runs unchanged against
the real exchange, and produces the same ``match_events.csv`` layout.

Timing is the exchange's too, as far as it can be written down. Market data is
applied by one timer and order books are published by another, each with the
exchange's own jitter, so what a trader is shown at a tick is up to a twentieth
of a second old, as it is in a real match. The jitter is drawn from a seeded
generator: the same seed gives the same match, event for event.

What a trader does with what it is shown takes time, and on the real exchange
the faster of two traders gets the customer. Each trader can be given a
reaction time (``add_trader(..., reaction=...)``), measured from a real match
by ``vigil.live.reaction_times``; without one its orders arrive the instant
it sends them, which is the best case and flatters whoever would really be
slow. ``latency`` adds a fixed delay to every trader on top.
"""
from __future__ import annotations

import asyncio.events
import collections
import heapq
import itertools
import json
import os
import random
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from vigil.rtg import (
    TOP_LEVEL_COUNT,
    AccountFactory,
    BaseAutoTrader,
    Competitor,
    CompetitorManager,
    FrequencyLimiter,
    IController,
    IExecutionConnection,
    Instrument,
    MarketEvent,
    MarketEventsReader,
    MatchEvent,
    MatchEvents,
    OrderBook,
    UnhedgedLotsFactory,
    messages,
)
from vigil.sim.result import MatchResult, ScoreRecorder, build_result

# Same-instant ordering: market data first, then timers, then trader messages, then the tick.
_SLICE, _TIMER, _MESSAGE, _TICK = range(4)
_OPEN = 1e-6  # the exchange's clock reads a moment past zero when its timers first fire

_ERROR, _HEDGE_FILLED, _ORDER_FILLED, _ORDER_STATUS = range(4)

TraderFactory = Callable[[Any, str, str], BaseAutoTrader]


@dataclass(frozen=True)
class ExchangeConfig:
    """The knobs of ``exchange.json`` that shape a match."""

    tick_interval: float = 0.25
    market_event_interval: float = 0.05
    maker_fee: float = -0.0001
    taker_fee: float = 0.0002
    etf_clamp: float = 0.002
    tick_size: float = 1.00
    active_order_count_limit: int = 10
    active_volume_limit: int = 200
    message_frequency_interval: float = 1.0
    message_frequency_limit: int = 50
    position_limit: int = 100
    latency: float = 0.0  # seconds from a trader sending a message to the exchange acting on it
    jitter: float = 0.2  # each timer fires within this fraction of its interval of when it is due (timer.py)
    speed: float = 1.0  # of the exchange's clock; the simulator itself always runs on match time

    @classmethod
    def from_json(cls, path: str | os.PathLike, **overrides: Any) -> ExchangeConfig:
        """Read an ``exchange.json`` as shipped with Ready Trader Go."""
        with open(path) as handle:
            raw = json.load(handle)
        engine, fees, inst, limits = raw["Engine"], raw["Fees"], raw["Instrument"], raw["Limits"]
        values = dict(
            tick_interval=engine["TickInterval"],
            market_event_interval=engine["MarketEventInterval"],
            maker_fee=fees["Maker"],
            taker_fee=fees["Taker"],
            etf_clamp=inst["EtfClamp"],
            tick_size=inst["TickSize"],
            active_order_count_limit=limits["ActiveOrderCountLimit"],
            active_volume_limit=limits["ActiveVolumeLimit"],
            message_frequency_interval=limits["MessageFrequencyInterval"],
            message_frequency_limit=limits["MessageFrequencyLimit"],
            position_limit=limits["PositionLimit"],
            speed=engine.get("Speed", 1.0),
        )
        values.update(overrides)
        return cls(**values)

    def limits(self) -> dict[str, Any]:
        """In the exchange's own key names."""
        return {
            "ActiveOrderCountLimit": self.active_order_count_limit,
            "ActiveVolumeLimit": self.active_volume_limit,
            "MessageFrequencyInterval": self.message_frequency_interval,
            "MessageFrequencyLimit": self.message_frequency_limit,
            "PositionLimit": self.position_limit,
        }

    def fees(self) -> dict[str, float]:
        return {"Maker": self.maker_fee, "Taker": self.taker_fee}

    def instrument(self) -> dict[str, float]:
        return {"EtfClamp": self.etf_clamp, "TickSize": self.tick_size}


class _Handle:
    """A cancellable scheduled call (the simulated ``asyncio.TimerHandle``)."""

    __slots__ = ("args", "callback", "cancelled_")

    def __init__(self, callback: Callable[..., Any], args: tuple):
        self.callback = callback
        self.args = args
        self.cancelled_ = False

    def cancel(self) -> None:
        self.cancelled_ = True

    def cancelled(self) -> bool:
        return self.cancelled_


class SimLoop:
    """The part of ``asyncio.AbstractEventLoop`` that exchange and trader code touch, on simulated time."""

    def __init__(self, sim: Simulator):
        self._sim = sim

    def time(self) -> float:
        return self._sim.now

    def call_at(self, when: float, callback: Callable[..., Any], *args: Any) -> _Handle:
        handle = _Handle(callback, args)
        self._sim._push(max(when, self._sim.now), _TIMER, handle)
        return handle

    def call_later(self, delay: float, callback: Callable[..., Any], *args: Any) -> _Handle:
        return self.call_at(self._sim.now + delay, callback, *args)

    def call_soon(self, callback: Callable[..., Any], *args: Any) -> _Handle:
        return self.call_at(self._sim.now, callback, *args)

    def stop(self) -> None:
        """Auto-traders stop their loop when they lose the exchange; there is nothing to stop here."""

    def is_closed(self) -> bool:
        return False


class _TimerStub:
    """Stands in for the exchange's tick ``Timer``: the simulator fires the callbacks itself."""

    def __init__(self) -> None:
        self.timer_started: list[Callable] = []
        self.timer_stopped: list[Callable] = []
        self.timer_ticked: list[Callable] = []
        self.shutdown_requested: str | None = None

    def shutdown(self, now: float, reason: str) -> None:
        self.shutdown_requested = reason


class _Feed:
    """Hands the pre-parsed market events to ``MarketEventsReader`` in place of its reader thread's queue."""

    def __init__(self, events: Iterable[MarketEvent]):
        self._events = iter(events)
        self.count = 0

    def get(self) -> MarketEvent | None:
        event = next(self._events, None)
        if event is not None:
            self.count += 1
        return event


class _Trader:
    """Book-keeping for one connected auto-trader."""

    __slots__ = ("arrival", "burst", "closed", "competitor", "delay", "detached", "errors", "limiter", "name",
                 "reaction", "rng", "trader")

    def __init__(self, name: str, trader: BaseAutoTrader, limiter: FrequencyLimiter,
                 reaction: tuple[float, float] | None, rng: random.Random):
        self.name = name
        self.trader = trader
        self.limiter = limiter
        self.competitor: Competitor | None = None
        self.closed = False  # the exchange has closed the execution channel
        self.detached = False  # ... and the competitor has been told the connection is gone
        self.errors: list[tuple[float, int, str]] = []
        self.reaction = reaction  # (typical, spread) of the time it takes to answer, in seconds
        self.rng = rng
        self.burst = -1.0  # the instant of the burst of messages the current delay was drawn for
        self.delay = 0.0
        self.arrival = 0.0  # when its latest message reaches the exchange: a connection keeps its order

    def reaction_time(self, now: float) -> float:
        """How long this burst of messages takes to reach the exchange: one draw per instant of sending."""
        if self.reaction is None:
            return 0.0
        if now != self.burst:
            typical, spread = self.reaction
            self.burst, self.delay = now, max(0.1 * typical, self.rng.gauss(typical, spread))
        return self.delay


class _TraderTransport:
    """What the trader believes is its TCP connection to the exchange."""

    def __init__(self, sim: Simulator, handle: _Trader):
        self._sim = sim
        self._handle = handle

    def write(self, data: bytes) -> None:
        self._sim._on_trader_bytes(self._handle, data)

    def is_closing(self) -> bool:
        return self._handle.closed

    def close(self) -> None:
        self._sim._request_close(self._handle)

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return default


class _Channel(IExecutionConnection):
    """What the exchange believes is its connection to the trader.

    Messages are queued rather than delivered on the spot: on a socket the
    trader could not react in the middle of the exchange's matching loop either.
    """

    def __init__(self, sim: Simulator, handle: _Trader):
        self._sim = sim
        self._handle = handle

    def close(self) -> None:
        self._sim._request_close(self._handle)

    # What is written after the exchange has hung up goes nowhere, as on a closed socket.

    def send_error(self, client_order_id: int, error_message: bytes) -> None:
        if self._handle.closed:
            return
        text = error_message[:50].rstrip(b"\x00")  # the wire field is 50 bytes wide
        self._handle.errors.append((self._sim.now, client_order_id, text.decode(errors="replace")))
        self._sim._outbox.append((self._handle, _ERROR, (client_order_id, text)))

    def send_hedge_filled(self, client_order_id: int, average_price: int, volume: int) -> None:
        if not self._handle.closed:
            self._sim._outbox.append((self._handle, _HEDGE_FILLED, (client_order_id, average_price, volume)))

    def send_order_filled(self, client_order_id: int, price: int, volume: int) -> None:
        if not self._handle.closed:
            self._sim._outbox.append((self._handle, _ORDER_FILLED, (client_order_id, price, volume)))

    def send_order_status(self, client_order_id: int, fill_volume: int, remaining_volume: int, fees: int) -> None:
        if not self._handle.closed:
            self._sim._outbox.append(
                (self._handle, _ORDER_STATUS, (client_order_id, fill_volume, remaining_volume, fees)))


class Simulator(IController):
    """One Ready Trader Go match on a simulated clock.

    >>> sim = Simulator(load_market_events(path))
    >>> sim.add_trader("TraderOne", AutoTrader)
    >>> result = sim.run()
    """

    def __init__(self, market_events: Sequence[MarketEvent], config: ExchangeConfig | None = None, *,
                 seed: int = 0, record: str = "all",
                 on_event: Callable[[MatchEvent], None] | None = None,
                 end_time: float | None = None):
        """``record`` selects which match events are kept on the result: ``"all"``,
        ``"competitors"`` (skip the replayed market's own orders) or ``"none"``.
        ``end_time`` stops the match early, for short test runs.
        """
        if record not in ("all", "competitors", "none"):
            raise ValueError(f"record must be 'all', 'competitors' or 'none', not {record!r}")
        self.config = config or ExchangeConfig()
        self.now = 0.0
        self.finished = False
        self.loop = SimLoop(self)

        self._end_time = end_time
        self._heap: list[tuple[float, int, int, Any]] = []
        self._order = itertools.count()
        self._seed = seed
        # What tells one session's market from another's, cheaply: a trader's luck in reaching the exchange
        # first is drawn per seed, per session and per team, so that one seed is not the same luck in
        # every session.
        events = market_events if isinstance(market_events, Sequence) else list(market_events)
        market_events = events
        self._market = (f"{len(events)}:{events[0].order_id}:{events[-1].time:.6f}" if events else "empty")
        self._rng = random.Random(seed)  # the order in which traders are handed each message
        self._timer_rng = random.Random(f"{seed}/timers")
        self._outbox: collections.deque[tuple[_Trader, int, tuple]] = collections.deque()
        self._closing: collections.deque[_Trader] = collections.deque()
        self._traders: list[_Trader] = []
        self._secrets: dict[str, str] = {}
        self._exhausted = False
        self._ticks = 0
        self._trade_tick_seq = [1 for _ in Instrument]

        self.future_book = OrderBook(Instrument.FUTURE, 0.0, 0.0)
        self.etf_book = OrderBook(Instrument.ETF, self.config.maker_fee, self.config.taker_fee)
        self.match_events = MatchEvents()
        self._events: list[MatchEvent] = []
        self._op_counts: dict[str, list[int]] = {}
        self.match_events.event_occurred.append(self._count_event)
        if record == "all":
            self.match_events.event_occurred.append(self._events.append)
        elif record == "competitors":
            self.match_events.event_occurred.append(self._keep_competitor_event)
        if on_event is not None:
            self.match_events.event_occurred.append(on_event)

        self._feed = _Feed(market_events)
        self._reader = MarketEventsReader("<memory>", None, self.future_book, self.etf_book, self.match_events)
        self._reader.queue = self._feed
        self._reader.task_complete.append(self._on_market_data_exhausted)

        self._score = ScoreRecorder()
        self._timer = _TimerStub()
        self._manager = CompetitorManager(
            self.config.limits(), self._secrets, AccountFactory(self.config.etf_clamp, self.config.tick_size),
            self.etf_book, self.future_book, self.match_events, self._score, self.config.tick_size, self._timer,
            UnhedgedLotsFactory())
        self._manager.controller = self

        # Scratch lists the books fill in; copied to tuples before they reach a trader.
        self._ask_prices = [0] * TOP_LEVEL_COUNT
        self._ask_volumes = [0] * TOP_LEVEL_COUNT
        self._bid_prices = [0] * TOP_LEVEL_COUNT
        self._bid_volumes = [0] * TOP_LEVEL_COUNT

    # -- setup ---------------------------------------------------------------------------------

    def add_trader(self, name: str, factory: TraderFactory, secret: str = "secret", *,
                   reaction: tuple[float, float] | None = None) -> BaseAutoTrader:
        """Connect and log in an auto-trader. ``factory`` is called as ``factory(loop, name, secret)``.

        ``reaction`` is how long the trader takes to answer what the exchange
        sends it: the typical time and its spread, in seconds. Every burst of
        messages it sends reaches the exchange that much later (one draw per
        burst), and never ahead of what it sent before.
        """
        if self.now > 0.0:
            raise RuntimeError("traders must join before the match starts")
        if name in self._secrets:
            raise ValueError(f"duplicate team name: {name!r}")
        trader = factory(self.loop, name, secret)
        limiter = FrequencyLimiter(self.config.message_frequency_interval, self.config.message_frequency_limit)
        handle = _Trader(name, trader, limiter, reaction, random.Random(f"{self._seed}/{self._market}/{name}"))
        trader._connection_transport = _TraderTransport(self, handle)
        self._secrets[name] = secret
        self._op_counts[name] = [0] * 5
        self._manager.on_competitor_connect()
        limiter.check_event(0.0)  # the login message counts towards the rate limit
        handle.competitor = self._manager.login_competitor(name, secret, _Channel(self, handle))
        self._traders.append(handle)
        return trader

    @property
    def team_names(self) -> list[str]:
        return [handle.name for handle in self._traders]

    # -- IController ---------------------------------------------------------------------------

    def advance_time(self) -> float:
        """Apply market data up to the current instant and return it (the exchange's ``Controller.advance_time``)."""
        self._reader.process_market_events(self.now)
        return self.now

    # -- main loop -----------------------------------------------------------------------------

    def run(self) -> MatchResult:
        """Play the match to the end of the market data and return the outcome."""
        if self.finished:
            raise RuntimeError("a Simulator runs one match; create a new one to run again")

        # The exchange's unhedged-lots timer asks asyncio for "the running loop". Make that us.
        previous = asyncio.events._get_running_loop()
        asyncio.events._set_running_loop(self.loop)
        try:
            for callback in self._timer.timer_started:
                callback(self._timer, 0.0)
            # Both of the exchange's timers fire once as the market opens, the market data's first.
            self._push(_OPEN, _SLICE, 1)
            self._push(_OPEN, _TICK, 1)

            while self._heap and not self.finished:
                when, kind, _, payload = heapq.heappop(self._heap)
                self.now = when
                if kind == _MESSAGE:
                    self._handle_message(*payload)
                elif kind == _SLICE:
                    self.advance_time()
                    if not self._exhausted:
                        self._push(self._due(payload, self.config.market_event_interval), _SLICE, payload + 1)
                elif kind == _TICK:
                    self._handle_tick(payload)
                elif not payload.cancelled_:  # _TIMER
                    payload.callback(*payload.args)
                self._flush()
        finally:
            asyncio.events._set_running_loop(previous)

        self.finished = True
        return build_result(
            teams=[(h.name, h.competitor, h.errors, self._op_counts[h.name]) for h in self._traders],
            score=self._score, events=self._events, duration=self.now, ticks=self._ticks,
            market_events=self._feed.count)

    # -- internals -----------------------------------------------------------------------------

    def _push(self, when: float, kind: int, payload: Any) -> None:
        heapq.heappush(self._heap, (when, kind, next(self._order), payload))

    def _due(self, fired: int, interval: float) -> float:
        """When a timer fires next, having fired ``fired`` times: on its grid, give or take its jitter."""
        jitter = self.config.jitter
        return fired * interval + (self._timer_rng.uniform(-jitter, jitter) * interval if jitter else 0.0)

    def _keep_competitor_event(self, event: MatchEvent) -> None:
        if event.competitor:
            self._events.append(event)

    def _count_event(self, event: MatchEvent) -> None:
        if event.competitor:
            self._op_counts[event.competitor][event.operation] += 1

    def _on_market_data_exhausted(self, _: Any) -> None:
        self._exhausted = True

    def _on_trader_bytes(self, handle: _Trader, data: bytes) -> None:
        if handle.closed:
            return
        length, typ = messages.HEADER.unpack_from(data)
        payload = bytes(data[messages.HEADER_SIZE:length])
        handle.arrival = max(self.now + self.config.latency + handle.reaction_time(self.now), handle.arrival)
        self._push(handle.arrival, _MESSAGE, (handle, typ, payload, length))

    def _handle_message(self, handle: _Trader, typ: int, payload: bytes, length: int) -> None:
        """Mirror of ``ExecutionConnection.on_message`` for a logged-in competitor."""
        if handle.closed:
            return
        now = self.advance_time()
        competitor = handle.competitor

        if handle.limiter.check_event(now):
            competitor.hard_breach(now, 0, b"message frequency limit breached")
            return

        try:
            if typ == messages.MessageType.INSERT_ORDER and length == messages.INSERT_MESSAGE_SIZE:
                competitor.on_insert_message(now, *messages.INSERT_MESSAGE.unpack(payload))
            elif typ == messages.MessageType.CANCEL_ORDER and length == messages.CANCEL_MESSAGE_SIZE:
                competitor.on_cancel_message(now, *messages.CANCEL_MESSAGE.unpack(payload))
            elif typ == messages.MessageType.HEDGE_ORDER and length == messages.HEDGE_MESSAGE_SIZE:
                competitor.on_hedge_message(now, *messages.HEDGE_MESSAGE.unpack(payload))
            elif typ == messages.MessageType.AMEND_ORDER and length == messages.AMEND_MESSAGE_SIZE:
                competitor.on_amend_message(now, *messages.AMEND_MESSAGE.unpack(payload))
            else:
                self._request_close(handle)  # the exchange hangs up on anything it cannot parse
        except Exception as error:  # noqa: BLE001
            # The exchange's own handlers can fail (a hedge while the ETF has no price, for one).
            # asyncio then drops the connection the message came in on, and so do we.
            handle.errors.append((now, 0, f"exchange handler failed: {type(error).__name__}: {error}"))
            self._request_close(handle)

    def _handle_tick(self, number: int) -> None:
        # The exchange's tick publishes the books as they stand. It does not bring the market data
        # up to date first: that is the other timer's job, and the traders' messages'.
        now = self.now
        self._ticks = number

        # Scores first, then the books: the order in which the exchange registered its tick callbacks.
        for callback in self._timer.timer_ticked:
            callback(self._timer, now, number)

        snapshots = []
        for book in (self.future_book, self.etf_book):
            book.top_levels(self._ask_prices, self._ask_volumes, self._bid_prices, self._bid_volumes)
            snapshots.append((int(book.instrument), tuple(self._ask_prices), tuple(self._ask_volumes),
                              tuple(self._bid_prices), tuple(self._bid_volumes)))
        for handle in self._audience():
            for instrument, ask_prices, ask_volumes, bid_prices, bid_volumes in snapshots:
                handle.trader.on_order_book_update_message(instrument, number, ask_prices, ask_volumes,
                                                           bid_prices, bid_volumes)

        if (self._exhausted or self._timer.shutdown_requested
                or (self._end_time is not None and now >= self._end_time)):
            self._finish(now)  # out of market data, out of competitors, or out of time
        else:
            self._push(self._due(number, self.config.tick_interval), _TICK, number + 1)

    def _finish(self, now: float) -> None:
        self.finished = True
        # The exchange stops its writers before it disconnects the competitors, so neither the
        # cancellation of whatever they still had resting nor their disconnection is recorded.
        self.match_events.event_occurred.clear()
        self._score.close()
        for callback in self._timer.timer_stopped:
            callback(self._timer, now)  # disconnects every competitor, which cancels their resting orders
        self._flush(deliver=False)

    def _audience(self) -> list[_Trader]:
        """Connected traders in a random (but seeded) order, so no team always reacts first."""
        live = [handle for handle in self._traders if not handle.closed]
        if len(live) > 1:
            self._rng.shuffle(live)
        return live

    def _request_close(self, handle: _Trader) -> None:
        if not handle.closed:
            handle.closed = True
            self._closing.append(handle)

    def _flush(self, deliver: bool = True) -> None:
        """Deliver what the exchange has queued for the traders, then tidy up closed connections."""
        while True:
            busy = False

            while self._outbox:
                busy = True
                handle, kind, args = self._outbox.popleft()
                if handle.detached or not deliver:
                    continue  # everything written before the hang-up still arrives; nothing after
                trader = handle.trader
                if kind == _ORDER_STATUS:
                    trader.on_order_status_message(*args)
                elif kind == _ORDER_FILLED:
                    trader.on_order_filled_message(*args)
                elif kind == _HEDGE_FILLED:
                    trader.on_hedge_filled_message(*args)
                else:
                    trader.on_error_message(*args)

            for book in (self.future_book, self.etf_book):
                if book.trade_ticks(self._ask_prices, self._ask_volumes, self._bid_prices, self._bid_volumes):
                    busy = True
                    instrument = int(book.instrument)
                    self._trade_tick_seq[instrument] += 1
                    ticks = (tuple(self._ask_prices), tuple(self._ask_volumes),
                             tuple(self._bid_prices), tuple(self._bid_volumes))
                    if deliver:
                        for handle in self._audience():
                            handle.trader.on_trade_ticks_message(instrument, self._trade_tick_seq[instrument],
                                                                 *ticks)

            while self._closing:
                busy = True
                handle = self._closing.popleft()
                handle.competitor.on_connection_lost(self.now)
                self._manager.on_competitor_disconnect()
                handle.detached = True

            if not busy:
                return
