"""Reinforcement learning: a tabular Q-learning policy that adjusts base Vigil's quotes.

Base Vigil's quote prices come from a model of where customer orders are
priced and what is queued in front. Rather than learn quoting from nothing,
which four fifteen-minute sessions are far too little data for, this version
learns corrections to it (residual policy learning): at each update the
policy chooses, for each side, to quote one tick closer to the mid than base
Vigil would, at its price, one tick further out, or not at all, and learns
from the outcome which choice pays in which situation (Sutton & Barto,
*Reinforcement Learning: An Introduction*, ch. 6; for market making, Spooner
et al., "Market Making via Reinforcement Learning", 2018).

The situation (state) is coarse on purpose, 180 cells: the ETF position in
five bands, the inventory carried un-hedged in three, the forecast's sign,
whether the future's spread is one tick or wider, and whether volatility is
above its recent norm. The reward for a decision is what the trades made
until the next decision earned against the future's mid (the profit of
trading, with the price moves of inventory already held left out: they are
noise to the quoting decision). Values are learnt with double Q-learning,
exploring by epsilon-greedy choice, over simulated sessions; the policy that
trades a session was trained on the other three.

What it took, measured on training sessions only. Plain Q-learning with a
short horizon (discount 0.9) learnt a policy that earned 9 to 16 per cent less
than base Vigil on the very sessions it was trained on: it overrated whichever
correction had been lucky, and could not see the cost of filling the position
limit. Double Q-learning and a longer horizon (0.99) narrowed that. What closed
it was safe policy improvement with baseline bootstrapping: an action is only
taken where it has been tried ``TRUST`` times; elsewhere base Vigil's is kept.
With that, the policy does what base Vigil does wherever the evidence for
doing otherwise is thin, and on four sessions that is nearly everywhere.
"""
from __future__ import annotations

import random
from typing import Any

from vigil.rtg import Side
from vigil.strategies.challengers.base import Challenger

OFFSETS = (-1, 0, 1, None)  # ticks further from the mid than base Vigil's price; None shows no quote
ACTIONS = tuple((ask, bid) for ask in OFFSETS for bid in OFFSETS)
NEUTRAL = ACTIONS.index((0, 0))  # quote as base Vigil would: what an untrained policy does
INVENTORY_BANDS = (-60, -20, 20, 60)
N_STATES = 5 * 3 * 3 * 2 * 2
TRUST = 300  # visits before a state-action's estimated value is acted on
DISCOUNT = 0.99  # per update: a horizon of about a hundred updates, twenty-five seconds


class QPolicy:
    """Action values by state, with visit counts, as plain lists.

    With ``twin`` values kept as well, learning is double Q-learning (van
    Hasselt, 2010): each step updates one table, choosing the next action by it
    and valuing that action by the other. Plain Q-learning takes the largest of
    several noisy estimates as its target and so overrates whichever action has
    been lucky; with sixteen actions and rewards as noisy as a market's, that
    bias is large. Decisions use the two tables' average.
    """

    def __init__(self, values: list[list[float]] | None = None, visits: list[list[int]] | None = None,
                 twin: list[list[float]] | None = None, trust: int = 0):
        self.values = values or [[0.0] * len(ACTIONS) for _ in range(N_STATES)]
        self.visits = visits or [[0] * len(ACTIONS) for _ in range(N_STATES)]
        self.twin = twin
        # Safe policy improvement with baseline bootstrapping (Laroche, Trichelair & Tachet des Combes,
        # ICML 2019): where an action has been tried fewer than ``trust`` times in a state, the estimate
        # of its value is not trusted, and the baseline's action (base Vigil's) is kept.
        self.trust = trust

    def worth(self, state: int, action: int) -> float:
        if self.twin is None:
            return self.values[state][action]
        return 0.5 * (self.values[state][action] + self.twin[state][action])

    def best(self, state: int) -> int:
        """The action of highest value among those tried often enough to be judged; between equals, base Vigil's."""
        visits = self.visits[state]
        allowed = [action for action in range(len(ACTIONS)) if action == NEUTRAL or visits[action] >= self.trust]
        return max(allowed, key=lambda action: (self.worth(state, action), action == NEUTRAL))

    def choose(self, state: int, epsilon: float, rng: random.Random) -> int:
        if epsilon > 0.0 and rng.random() < epsilon:
            return rng.randrange(len(ACTIONS))
        return self.best(state)

    def learn(self, state: int, action: int, reward: float, following: int, discount: float,
              rng: random.Random | None = None) -> None:
        """One learning step, with a learning rate that falls as the pair is visited more."""
        self.visits[state][action] += 1
        rate = max(1.0 / self.visits[state][action] ** 0.6, 0.01)
        if self.twin is None:
            target = reward + discount * max(self.values[following])
            self.values[state][action] += rate * (target - self.values[state][action])
            return
        update, other = (self.values, self.twin) if (rng or random).random() < 0.5 else (self.twin, self.values)
        row = update[following]
        chosen = max(range(len(row)), key=row.__getitem__)
        target = reward + discount * other[following][chosen]
        update[state][action] += rate * (target - update[state][action])

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"values": [[round(v, 4) for v in row] for row in self.values], "visits": self.visits,
                               "trust": self.trust}
        if self.twin is not None:
            out["twin"] = [[round(v, 4) for v in row] for row in self.twin]
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QPolicy:
        twin = data.get("twin")
        return cls([list(map(float, row)) for row in data["values"]], [list(map(int, row)) for row in data["visits"]],
                   [list(map(float, row)) for row in twin] if twin is not None else None, int(data.get("trust", 0)))

    @classmethod
    def double(cls, trust: int = 0) -> QPolicy:
        return cls(twin=[[0.0] * len(ACTIONS) for _ in range(N_STATES)], trust=trust)


class RLTrader(Challenger):
    name = "rl"

    def __init__(self, loop, team_name: str, secret: str, config=None, model=None, params=None,
                 policy: QPolicy | None = None, learning: dict[str, float] | None = None):
        """``policy`` and ``learning`` are for training: a shared table to learn into, and
        ``{"epsilon": ..., "discount": ..., "seed": ...}``. Without them the trader plays the
        fitted policy greedily and learns nothing."""
        super().__init__(loop, team_name, secret, config, model, params)
        if policy is None:
            stored = self.fitted.get("policy")
            policy = QPolicy.from_dict(stored) if stored else QPolicy()
        self.policy = policy
        self.learning = learning
        self._rng = random.Random(learning.get("seed", 0) if learning else 0)
        self._previous: tuple[int, int] | None = None  # (state, action) of the last decision
        self._marked: tuple[float, int, float] | None = None  # (cash, inventory, mid) at that decision
        self._slow_variance = None

    def state(self, future_ask: float, future_bid: float) -> int:
        position = self.etf_position
        inventory = sum(position > edge for edge in INVENTORY_BANDS)
        carried = self.etf_position + self.future_position + self._hedge_pending
        unhedged = 0 if carried < -10 else (2 if carried > 10 else 1)
        forecast = 0 if self._forecast < -0.05 else (2 if self._forecast > 0.05 else 1)
        wide = int(future_ask - future_bid > self.config.tick)
        if self._slow_variance is None:
            self._slow_variance = self._variance
        self._slow_variance += (self._variance - self._slow_variance) / 400.0
        volatile = int(self._variance > self._slow_variance)
        return (((inventory * 3 + unhedged) * 3 + forecast) * 2 + wide) * 2 + volatile

    def _decide_quotes(self, asks, bids, future_ask, future_bid, mid) -> None:
        super()._decide_quotes(asks, bids, future_ask, future_bid, mid)
        state = self.state(future_ask, future_bid)
        inventory = self.etf_position + self.future_position
        if self.learning is not None and self._previous is not None:
            cash, held, then = self._marked
            # What the trades since the last decision earned against the mid: the change in value with
            # the inventory then held marked unchanged. Or, if asked, the whole change in value.
            reward = (self.cash - cash + (inventory - held) * mid) / 100.0
            if self.learning.get("reward") == "pnl":
                reward += held * (mid - then) / 100.0
            self.policy.learn(*self._previous, reward, state, self.learning.get("discount", DISCOUNT), self._rng)
        epsilon = self.learning.get("epsilon", 0.0) if self.learning is not None else 0.0
        action = self.policy.choose(state, epsilon, self._rng)
        self._previous, self._marked = (state, action), (self.cash, inventory, mid)
        self._apply(action)

    def _apply(self, action: int) -> None:
        """Move or withdraw the quotes base Vigil wants, as ``action`` says."""
        if action != NEUTRAL:
            self.stats["departures"] += 1  # updates on which the policy did not quote as base Vigil would
        ask_offset, bid_offset = ACTIONS[action]
        tick = self.config.tick
        for side, offset, sign in ((Side.SELL, ask_offset, +1), (Side.BUY, bid_offset, -1)):
            want = self._want_quote[side]
            if want is None:
                continue
            self._want_quote[side] = None if offset is None else (want[0] + sign * offset * tick, want[1])


class RLFastTrader(RLTrader):
    """``rl``'s policy, decision for decision, with the deciding done before the update arrives.

    ``rl`` works out its state and searches its table on every update before it
    sends anything, which base Vigil does not, and on the real exchange that put
    its orders behind base Vigil's on four updates in five. Nearly all of the
    state is known before the update arrives: the forecast and the volatility
    come from the update before, the position and the hedges in flight from the
    fills since. This version works out the policy's choice whenever one of
    those changes, for either width of the future's spread, so that on the
    update itself it only reads the spread and looks the choice up. It plays
    ``rl``'s fitted policy; the training is ``rl``'s.
    """

    name = "rl_fast"
    uses = "rl"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.learning is not None:
            raise ValueError("rl_fast only plays a trained policy; it is rl that learns")
        self._choice = [self.policy.best(state) for state in range(N_STATES)]
        self._tick = self.config.tick
        self._next_slow = 0.0
        self._ready = (NEUTRAL, NEUTRAL)  # the choice with the future's spread one tick wide, and wider
        self._prepare()

    def _prepare(self) -> None:
        """The next decision, for either width of spread, from all its state but that, worked out as ``rl`` does.

        The state is (((inventory * 3 + unhedged) * 3 + forecast) * 2 + wide) * 2 + volatile (``rl.state``).
        """
        variance, forecast = self._variance, self._forecast
        slow = variance if self._slow_variance is None else self._slow_variance
        slow += (variance - slow) / 400.0
        self._next_slow = slow
        position = self.etf_position
        carried = position + self.future_position + self._hedge_pending
        state = ((sum(position > edge for edge in INVENTORY_BANDS) * 3
                  + (0 if carried < -10 else (2 if carried > 10 else 1))) * 12
                 + (0 if forecast < -0.05 else (8 if forecast > 0.05 else 4)) + (variance > slow))
        self._ready = (self._choice[state], self._choice[state + 2])

    def _trade(self, asks, bids, future_ask, future_bid, mid) -> None:
        self._decide_takes(asks, bids, future_ask, future_bid, mid)
        Challenger._decide_quotes(self, asks, bids, future_ask, future_bid, mid)  # base Vigil's quotes
        self._slow_variance = self._next_slow
        action = self._ready[future_ask - future_bid > self._tick]
        if action != NEUTRAL:
            self._apply(action)
        self._work()
        self._learn()
        self._hedge()

    # Whatever moves the state between updates prepares the next decision again, off the racing path.

    def _learn(self) -> None:
        super()._learn()
        self._prepare()

    def _hedge(self) -> None:
        super()._hedge()
        self._prepare()

    def on_order_filled_message(self, client_order_id: int, price: int, volume: int) -> None:
        super().on_order_filled_message(client_order_id, price, volume)
        self._prepare()

    def on_hedge_filled_message(self, client_order_id: int, price: int, volume: int) -> None:
        super().on_hedge_filled_message(client_order_id, price, volume)
        self._prepare()

    def on_error_message(self, client_order_id: int, error_message: bytes) -> None:
        super().on_error_message(client_order_id, error_message)
        self._prepare()
