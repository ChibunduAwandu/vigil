"""Backtests: run strategies over the sample sessions and tabulate what they earned."""
from __future__ import annotations

import functools
import importlib.util
import json
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from vigil.ml.train import session_key
from vigil.paths import REPO_ROOT, rtg_home
from vigil.rtg import BaseAutoTrader
from vigil.rtg.marketdata import load_market_events
from vigil.sim.engine import ExchangeConfig, Simulator, TraderFactory
from vigil.sim.result import MatchResult, TeamResult
from vigil.strategies.ensemble import PRESETS, StrategyConfig, trader_class

REACTIONS_FILE = REPO_ROOT / "docs" / "results" / "reaction_times.json"
EXAMPLE = "example"  # the kind of the stock trader, as ``vigil live`` names it

Reaction = tuple[float, float]  # typical time to answer the exchange and its spread, in seconds


def measured_reactions(path: str | os.PathLike = REACTIONS_FILE) -> dict[str, Reaction]:
    """Reaction times by kind of trader (``preset:full``, ``example``), as measured on the real exchange.

    Written by ``scripts/record_live_match.py``. Empty if no match has been
    recorded, in which case a simulated trader answers instantly.
    """
    path = Path(path)
    if not path.is_file():
        return {}
    return {kind: (entry["typical"], entry["jitter"]) for kind, entry in json.loads(path.read_text()).items()}


def reaction_of(kind: str, measured: Mapping[str, Reaction] | None) -> Reaction | None:
    """The reaction time to simulate a trader of ``kind`` with.

    The scripted abusive traders have never been timed on their own; they do
    about as little work per update as the stock example, so they are given
    its time.
    """
    if not measured:
        return None
    return measured.get(kind) or (measured.get(EXAMPLE) if kind.startswith("actor:") else None)


def strategy_factory(config: StrategyConfig, session: str | os.PathLike | None = None,
                     exchange: ExchangeConfig | None = None) -> TraderFactory:
    """A trader factory for ``config``.

    Given the session it is about to trade, the trader is handed the model
    that was fitted without that session, so the result is out of sample.
    Given the exchange it is about to trade on, it is told that exchange's
    limits and fees, which it otherwise takes to be the standard ones.
    """
    if session is not None:
        config = config.with_(holdout=session_key(session))
    if exchange is not None:
        config = config.with_(
            position_limit=exchange.position_limit, message_limit=exchange.message_frequency_limit,
            message_interval=exchange.message_frequency_interval, tick=int(exchange.tick_size * 100.0),
            maker_fee=exchange.maker_fee, taker_fee=exchange.taker_fee, book_interval=exchange.tick_interval)
    return functools.partial(trader_class(config), config=config)


def example_trader_factory() -> TraderFactory:
    """The example auto-trader that ships with Ready Trader Go, unmodified."""
    path = rtg_home() / "autotrader.py"
    spec = importlib.util.spec_from_file_location("rtg_example_autotrader", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.AutoTrader


def run_match(session: str | os.PathLike, traders: Mapping[str, TraderFactory], *,
              exchange: ExchangeConfig | None = None, seed: int = 0, record: str = "none",
              end_time: float | None = None, on_event: Callable[[Any], None] | None = None,
              reactions: Mapping[str, Reaction | None] | None = None,
              ) -> tuple[MatchResult, dict[str, BaseAutoTrader]]:
    """Play one match and return the result along with the trader objects (for their diagnostics).

    ``reactions`` gives teams their reaction times, by team name.
    """
    sim = Simulator(load_market_events(session), exchange, seed=seed, record=record, end_time=end_time,
                    on_event=on_event)
    objects = {name: sim.add_trader(name, factory, reaction=(reactions or {}).get(name))
               for name, factory in traders.items()}
    return sim.run(), objects


@dataclass
class StrategyScore:
    """One strategy over several runs: each session, under each seed, traded alone against the recorded market."""

    name: str
    sessions: list[str] = field(default_factory=list)  # the session of each run
    seeds: list[int] = field(default_factory=list)  # the seed of each run
    results: list[TeamResult] = field(default_factory=list)
    stats: list[dict[str, int]] = field(default_factory=list)

    @property
    def pnl(self) -> np.ndarray:
        return np.array([r.pnl for r in self.results])

    @property
    def mean_pnl(self) -> float:
        return float(self.pnl.mean())

    def by_session(self) -> dict[str, float]:
        """Mean P&L per session, over the seeds it was run under."""
        out: dict[str, list[float]] = {}
        for session, result in zip(self.sessions, self.results, strict=True):
            out.setdefault(session, []).append(result.pnl)
        return {session: float(np.mean(values)) for session, values in out.items()}

    @property
    def breaches(self) -> int:
        return sum(r.status != "OK" for r in self.results)

    def blocks(self, seconds: float = 60.0) -> np.ndarray:
        """P&L earned in each consecutive block of ``seconds``, all sessions end to end."""
        out = []
        for result in self.results:
            # Sessions end a fraction of a second short of a whole minute; the last block takes the rest.
            count = max(int(round(result.times[-1] / seconds)), 1)
            edges = np.arange(1, count + 1) * seconds
            index = np.minimum(np.searchsorted(result.times, edges, side="right") - 1, len(result.times) - 1)
            index[-1] = len(result.times) - 1
            out.append(np.diff(np.concatenate([[0.0], result.pnl_series[index]])))
        return np.concatenate(out)

    def risk(self) -> dict[str, float]:
        """Dispersion of the running P&L, pooled over sessions."""
        steps = np.concatenate([np.diff(r.pnl_series[r.marked_from:]) for r in self.results])
        blocks = self.blocks()
        return {
            "max_drawdown": float(max(r.max_drawdown for r in self.results)),
            "pnl_std_per_update": float(steps.std()),
            "pnl_std_per_minute": float(blocks.std(ddof=1)),
            "worst_minute": float(blocks.min()),
            "sharpe_per_minute": float(blocks.mean() / blocks.std(ddof=1)),
        }

    def versus(self, other: StrategyScore) -> dict[str, float]:
        """Paired comparison with another strategy over the same runs.

        Four sessions are too few to say much from their totals, so the
        comparison is made minute by minute: in each minute of each run the
        two strategies traded the same market, shown to them at the same
        instants, and the differences are averaged. Runs of one session
        under different seeds are not independent of each other, so the
        standard error is taken over session-minutes: the difference in a
        minute is first averaged over the seeds.
        """
        seeds = len(set(self.seeds)) or 1
        difference = (self.blocks() - other.blocks()).reshape(-1, seeds, self._blocks_per_run()).mean(axis=1).ravel()
        per_session = self._blocks_per_run()
        mean = float(difference.mean() * per_session)
        error = float(difference.std(ddof=1) / np.sqrt(len(difference)) * per_session)
        # The same comparison with whole sessions as the unit: fewer of them, and no assumption
        # that one minute of a session is independent of the next.
        ours, theirs = self.by_session(), other.by_session()
        by_session = np.array([ours[name] - theirs[name] for name in ours])
        spread = float(by_session.std(ddof=1)) if len(by_session) > 1 else 0.0
        return {
            "mean_difference": mean,
            "standard_error": error,
            "t": mean / error if error > 0 else float("nan"),
            "t_over_sessions": float(by_session.mean() / (spread / np.sqrt(len(by_session)))) if spread > 0 else None,
            "sessions": len(by_session),
            "relative": mean / other.mean_pnl if other.mean_pnl > 0 else float("nan"),
            "runs_ahead": int((self.pnl > other.pnl).sum()),
            "runs": len(self.results),
        }

    def _blocks_per_run(self) -> int:
        return len(self.blocks()) // len(self.results)


def _each(function: Callable, jobs: Iterable, workers: int) -> list:
    """``function`` over ``jobs``, in order; across processes if ``workers`` is more than one."""
    jobs = list(jobs)
    if workers <= 1 or len(jobs) <= 1:
        return [function(job) for job in jobs]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(function, jobs))


def _solo(job: tuple) -> tuple[TeamResult, dict[str, int]]:
    """One strategy, one session, one seed. A function of its own so that it can run in another process."""
    session, seed, name, config, exchange, reaction = job
    result, objects = run_match(session, {name: strategy_factory(config, session, exchange)}, exchange=exchange,
                                seed=seed, reactions={name: reaction})
    return result[name], dict(getattr(objects[name], "stats", {}))


def score_strategies(strategies: Mapping[str, StrategyConfig], sessions: Sequence[str | os.PathLike], *,
                     exchange: ExchangeConfig | None = None, seeds: Sequence[int] = (0,),
                     reactions: Mapping[str, Reaction] | None = None, workers: int = 1) -> dict[str, StrategyScore]:
    """Trade every strategy on every session under every seed, one strategy per match.

    The seed fixes the instants at which the exchange's timers fire, and so
    what a trader is shown and when. Every strategy sees the same ones.
    """
    # Runs are ordered by session, then by seed: versus() relies on it.
    jobs = [(session, seed, name, config, exchange, reaction_of(f"preset:{name}", reactions))
            for session in sessions for seed in seeds for name, config in strategies.items()]
    scores = {name: StrategyScore(name) for name in strategies}
    for (session, seed, name, *_), (result, stats) in zip(jobs, _each(_solo, jobs, workers), strict=True):
        score = scores[name]
        score.sessions.append(Path(session).name)
        score.seeds.append(seed)
        score.results.append(result)
        score.stats.append(stats)
    return scores


def ablation(sessions: Sequence[str | os.PathLike], *, exchange: ExchangeConfig | None = None,
             presets: Mapping[str, StrategyConfig] = PRESETS, seeds: Sequence[int] = (0,),
             reactions: Mapping[str, Reaction] | None = None, workers: int = 1) -> dict[str, Any]:
    """The component study: each preset adds one component to the one before it."""
    scores = score_strategies(presets, sessions, exchange=exchange, seeds=seeds, reactions=reactions,
                              workers=workers)
    names = list(presets)
    base = scores[names[0]]
    rows = []
    for position, name in enumerate(names):
        score = scores[name]
        totals: dict[str, float] = {}
        for stats in score.stats:
            for key, value in stats.items():
                totals[key] = totals.get(key, 0) + value
        rows.append({
            "strategy": name,
            "pnl": [round(value, 2) for value in score.by_session().values()],
            "pnl_by_run": [round(float(v), 2) for v in score.pnl],
            "mean_pnl": round(score.mean_pnl, 2),
            "vs_baseline": score.versus(base) if position else None,
            "vs_previous": score.versus(scores[names[position - 1]]) if position else None,
            "breaches": score.breaches,
            "errors": sum(len(result.errors) for result in score.results),
            "risk": score.risk(),
            "lots_per_session": {key: round(value / len(score.results)) for key, value in sorted(totals.items())},
        })
    return {"sessions": [Path(s).name for s in sessions], "seeds": list(seeds),
            "latency": (exchange or ExchangeConfig()).latency,
            "reaction_times": bool(reactions), "rows": rows}


EXAMPLE_TEAM = "example (Optiver)"


def _together(job: tuple) -> dict[str, tuple[float, str]]:
    """Every preset and the example trader in one match: each team's P&L and status.

    Who reaches the exchange first is drawn per team slot (and per seed and
    session), and so is the order teams are handed each update. ``rotation``
    moves every team along the slots, so that over a round of seeds each team
    has had each slot's luck.
    """
    session, seed, presets, exchange, reactions, rotation = job
    factories: dict[str, TraderFactory] = {name: strategy_factory(config, session, exchange)
                                           for name, config in presets.items()}
    factories[EXAMPLE_TEAM] = example_trader_factory()
    timing = {name: reaction_of(f"preset:{name}", reactions) for name in presets}
    timing[EXAMPLE_TEAM] = reaction_of(EXAMPLE, reactions)
    names = list(factories)
    slots = {names[(i + rotation) % len(names)]: f"team-{i}" for i in range(len(names))}
    ordered = sorted(names, key=lambda name: slots[name])
    result, _ = run_match(session, {slots[name]: factories[name] for name in ordered}, exchange=exchange, seed=seed,
                          reactions={slots[name]: timing[name] for name in ordered})
    return {name: (result[slots[name]].pnl, result[slots[name]].status) for name in names}


def tournament(sessions: Sequence[str | os.PathLike], *, exchange: ExchangeConfig | None = None,
               presets: Mapping[str, StrategyConfig] = PRESETS, seeds: Sequence[int] = (0,),
               reactions: Mapping[str, Reaction] | None = None, workers: int = 1) -> dict[str, Any]:
    """Every preset and the stock example trader in the same match, competing for the same customers.

    With several teams in a match, who gets a customer depends on who reaches
    the exchange first, and the seed decides that where the teams' reaction
    times do not. One seed is one draw; the spread over seeds says how much of
    a ranking is luck.
    """
    jobs = [(session, seed, dict(presets), exchange, dict(reactions or {}), index)
            for index, seed in enumerate(seeds) for session in sessions]
    outcomes = iter(_each(_together, jobs, workers))
    rows: dict[str, dict[str, Any]] = {}
    ranks: dict[str, list[int]] = {}
    for _ in seeds:
        totals: dict[str, float] = {}
        for session in sessions:
            for name, (pnl, status) in next(outcomes).items():
                row = rows.setdefault(name, {"team": name, "runs": {}, "status": []})
                row["runs"].setdefault(Path(session).name, []).append(round(pnl, 2))
                row["status"].append(status)
                totals[name] = totals.get(name, 0.0) + pnl
        for rank, name in enumerate(sorted(totals, key=lambda team: -totals[team]), 1):
            ranks.setdefault(name, []).append(rank)
    for name, row in rows.items():
        row["pnl"] = [round(float(np.mean(values)), 2) for values in row["runs"].values()]
        per_seed = np.array(list(row["runs"].values())).mean(axis=0)  # a seed's mean over the sessions
        row["mean_pnl"] = round(float(per_seed.mean()), 2)
        row["std_over_seeds"] = round(float(per_seed.std(ddof=1)), 2) if len(seeds) > 1 else None
        row["ranks"] = ranks[name]
        row["breaches"] = sum(status != "OK" for status in row["status"])
        del row["status"]
    ranked = sorted(rows.values(), key=lambda row: -row["mean_pnl"])
    return {"sessions": [Path(s).name for s in sessions], "seeds": list(seeds), "reaction_times": bool(reactions),
            "rows": ranked}


def format_ablation(report: dict[str, Any]) -> str:
    """The component study as a Markdown table."""
    lines = [
        "| strategy | " + " | ".join(report["sessions"]) + " | mean P&L | vs baseline | vs previous row | hedged lots |",
        "|---|" + "---:|" * (len(report["sessions"]) + 4),
    ]
    for row in report["rows"]:
        def cell(key: str, row=row) -> str:
            versus = row[key]
            if versus is None:
                return "-"
            return f"{versus['relative']:+.1%} (t = {versus['t']:+.1f})"
        lines.append(
            f"| {row['strategy']} | " + " | ".join(f"{v:,.0f}" for v in row["pnl"])
            + f" | {row['mean_pnl']:,.0f} | {cell('vs_baseline')} | {cell('vs_previous')} | "
            + f"{row['lots_per_session'].get('hedge_lots', 0):,} |")
    return "\n".join(lines)
