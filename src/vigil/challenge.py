"""Pitting the challengers against base Vigil.

    vigil challenge fit    # fit what each challenger fits, leave-one-session-out (models/challengers.json)
    vigil challenge run    # measure them: alone, head to head against base Vigil, and all in one field

Fitting a challenger for a session uses only the other sessions: the data
models (fill rates, reversion, order flow) are fitted to them, the one tuning
knob each challenger has is chosen by what it earns trading them, and the
reinforcement learner is trained on them. The boosted model those runs trade
with is the one that never saw the held-out session either.

Measuring follows the component study's method: the same sessions and seeds
for everyone, paired comparisons, and the honesty that four sessions demand.
"""
from __future__ import annotations

import functools
import math
import random
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from vigil.backtest import (
    Reaction,
    _each,
    measured_reactions,
    reaction_of,
    run_match,
    score_strategies,
    strategy_factory,
    tournament,
)
from vigil.ml.train import session_key
from vigil.paths import REPO_ROOT
from vigil.strategies.challengers import CHALLENGERS, DEFAULT_PATH, STRATEGIES, ChallengerParams
from vigil.strategies.ensemble import trader_class

BASE = "full"  # base Vigil
CONTROL = "full-copy"  # a second base Vigil in the field: how far apart luck alone puts two identical traders
# Reaction times from a match on the real exchange between base Vigil and the challengers (record_live_match.py
# --keep-timing --timing-file): the relative speeds that decide who gets a customer, measured together.
REACTIONS_FILE = REPO_ROOT / "docs" / "results" / "reaction_times_challengers.json"

# The one setting of each challenger that is chosen by what it earns, and the values tried.
KNOBS: dict[str, tuple[str, tuple[float, ...]]] = {
    "skew": ("weight", (0.5, 1.0, 2.0, 4.0)),
    "glft": ("gamma", (0.00001, 0.0001, 0.0005, 0.001, 0.002, 0.005, 0.01)),
    "ou": ("weight", (0.25, 0.5, 1.0)),
    "hawkes": ("weight", (0.5, 1.0, 2.0)),
    "skew_fast": ("weight", (0.125, 0.25, 0.5, 1.0, 2.0, 4.0)),
}


def study_reactions(measured: Mapping[str, Reaction] | None = None) -> dict[str, Reaction]:
    """Reaction times for every kind in the study.

    From a match in which base Vigil and the challengers were timed together on
    the real exchange, if one has been recorded; else as measured for the
    presets. A challenger that has never been timed there is given base Vigil's,
    whose handler it shares up to the point where its orders go out.
    """
    if measured is None:
        measured = measured_reactions(REACTIONS_FILE) or measured_reactions()
    out = dict(measured)
    base = out.get(f"preset:{BASE}")
    if base:
        for name in CHALLENGERS:
            out.setdefault(f"preset:{name}", base)
    return out


# -- fitting -----------------------------------------------------------------------------------------

def data_models(sessions: Sequence[str], engines: Sequence[str] | None = None) -> dict[str, dict[str, Any]]:
    """What the challengers fit straight from the market data, with no trading involved (empty for the rest)."""
    from vigil.strategies.challengers.glft import fit_fill_rates
    from vigil.strategies.challengers.hawkes import fit_hawkes
    from vigil.strategies.challengers.ou import fit_reversion
    sessions = [str(s) for s in sessions]
    fitters = {"glft": fit_fill_rates, "ou": fit_reversion, "hawkes": fit_hawkes}
    return {engine: fitters[engine](sessions) if engine in fitters else {}
            for engine in (CHALLENGERS if engines is None else engines)}


def _quietly() -> None:
    """The fold fitted on every session trades, knowingly, with the boosted model fitted on every session."""
    import logging
    logging.getLogger("TRADER").setLevel(logging.ERROR)


def _tuning_run(job: tuple) -> float:
    """One challenger, with one value of its knob, alone on one training session under one seed."""
    _quietly()
    engine, value, session, seed, params, holdout, reaction = job
    config = CHALLENGERS[engine].with_(tuning=value, holdout=holdout)
    factory = functools.partial(trader_class(config), config=config, params=params)
    result, _ = run_match(session, {engine: factory}, seed=seed, reactions={engine: reaction})
    return result[engine].pnl


def _rl_training(job: tuple) -> dict[str, Any]:
    """Train one Q-learning policy over simulated sessions. A function of its own to run in another process."""
    from vigil.strategies.challengers.rl import DISCOUNT, TRUST, QPolicy, RLTrader
    _quietly()
    sessions, holdout, params, episodes, seed, reaction = job
    policy, rng = QPolicy.double(TRUST), random.Random(seed)
    config = CHALLENGERS["rl"].with_(holdout=holdout)
    earned = []
    for episode in range(episodes):
        session = sessions[episode % len(sessions)]
        # Explore a good deal at first and hardly at all by the end.
        epsilon = max(0.02, 0.3 * (1.0 - episode / max(1.0, 0.8 * episodes)))
        factory = functools.partial(RLTrader, config=config, params=params, policy=policy,
                                    learning={"epsilon": epsilon, "discount": DISCOUNT, "seed": rng.randrange(1 << 30)})
        result, _ = run_match(session, {"rl": factory}, seed=rng.randrange(1000), reactions={"rl": reaction})
        earned.append(round(result["rl"].pnl, 2))
    return {"policy": policy.to_dict(), "episodes": episodes, "pnl_by_episode": earned}


def fit(sessions: Sequence[str], *, seeds: Sequence[int] = (0, 1), rl_episodes: int = 300, workers: int = 1,
        say: Callable[[str], None] = print, only: Sequence[str] | None = None,
        previous: ChallengerParams | None = None) -> ChallengerParams:
    """Everything the challengers fit, once per held-out session and once on all of them.

    With ``only``, just the challengers named are fitted, into ``previous``
    (by default what was saved last), and every other challenger's parameters
    are kept exactly as they were: a challenger added later does not refit,
    and so change, the ones already measured and played.
    """
    sessions = [str(s) for s in sessions]
    engines = list(CHALLENGERS) if only is None else list(only)
    unknown = [engine for engine in engines if engine not in CHALLENGERS]
    if unknown:
        raise ValueError(f"no challenger called {', '.join(unknown)}; there are {', '.join(CHALLENGERS)}")
    reactions = study_reactions()
    reaction = reaction_of(f"preset:{BASE}", reactions)
    folds = {session_key(held_out): [s for s in sessions if s != held_out] for held_out in sessions}
    folds["full"] = sessions
    started = time.time()
    fitted = {fold: data_models(train, engines) for fold, train in folds.items()}
    say(f"data models fitted for {len(folds)} folds in {time.time() - started:.0f} s")

    started = time.time()
    knobs = {engine: KNOBS[engine] for engine in engines if engine in KNOBS}
    jobs = [(engine, value, session, seed, fitted[fold], None if fold == "full" else fold, reaction)
            for fold, train in folds.items() for engine, (_, values) in knobs.items() for value in values
            for session in train for seed in seeds]
    outcomes = _each(_tuning_run, jobs, workers)
    tables: dict[str, dict[str, dict[float, list[float]]]] = {}
    for (engine, value, _, _, _, holdout, _), pnl in zip(jobs, outcomes, strict=True):
        tables.setdefault(holdout or "full", {}).setdefault(engine, {}).setdefault(value, []).append(pnl)
    for fold, by_engine in tables.items():
        for engine, by_value in by_engine.items():
            knob = KNOBS[engine][0]
            means = {value: float(np.mean(pnls)) for value, pnls in by_value.items()}
            fitted[fold][engine][knob] = max(means, key=means.get)
            fitted[fold][engine]["tried"] = {f"{value:g}": round(mean, 2) for value, mean in means.items()}
    say(f"{len(jobs)} tuning runs in {time.time() - started:.0f} s")

    if "rl" in engines:
        started = time.time()
        rl_jobs = [(folds[fold], None if fold == "full" else fold, fitted[fold], rl_episodes, 7919 * (i + 1),
                    reaction) for i, fold in enumerate(folds)]
        for fold, trained in zip(folds, _each(_rl_training, rl_jobs, workers), strict=True):
            fitted[fold]["rl"] = trained
        say(f"{len(rl_jobs)} reinforcement-learning policies, {rl_episodes} sessions each, in "
            f"{time.time() - started:.0f} s")

    full = fitted.pop("full")
    names = {session_key(s): Path(s).name for s in sessions}
    meta = {"sessions": names, "tuning_seeds": list(seeds), "rl_episodes": rl_episodes,
            "knobs": {engine: {"name": knob, "values": list(values)} for engine, (knob, values) in knobs.items()}}
    if only is None:
        return ChallengerParams(full=full, folds=fitted, meta=meta)

    params = previous or ChallengerParams.load(DEFAULT_PATH)
    if set(params.folds) != set(fitted) or params.meta.get("sessions") != names:
        raise ValueError("the parameters saved were fitted on other sessions; fit every challenger again")
    for engine in engines:
        params.full[engine] = full[engine]
        for fold, by_engine in fitted.items():
            params.folds[fold][engine] = by_engine[engine]
        if engine in knobs:
            params.meta.setdefault("knobs", {})[engine] = meta["knobs"][engine]
    return params


# -- measuring ---------------------------------------------------------------------------------------

def _duel(job: tuple) -> tuple[float, float, str, str]:
    """Base Vigil and one challenger in the same match, played twice with their luck exchanged.

    Who reaches the exchange first is drawn per team slot, and in a two-team
    match it moves either side's result by thousands of dollars. Played once
    with the challenger in each slot, the luck falls to each side once: two
    copies of base Vigil, paired so, come out exactly level. Returns each
    side's P&L averaged over the pair, and their statuses.
    """
    session, seed, name, reactions, *rest = job
    end_time = rest[0] if rest else None  # for short test runs
    timing = {BASE: reaction_of(f"preset:{BASE}", reactions), name: reaction_of(f"preset:{name}", reactions)}
    pnl = {BASE: 0.0, name: 0.0}
    status = {BASE: "OK", name: "OK"}
    for first, second in ((BASE, name), (name, BASE)):
        traders = {"one": strategy_factory(STRATEGIES[first], session),
                   "two": strategy_factory(STRATEGIES[second], session)}
        result, _ = run_match(session, traders, seed=seed, reactions={"one": timing[first], "two": timing[second]},
                              end_time=end_time)
        for slot, team in (("one", first), ("two", second)):
            pnl[team] += result[slot].pnl / 2.0
            if result[slot].status != "OK":
                status[team] = result[slot].status
    return pnl[BASE], pnl[name], status[BASE], status[name]


def evaluate(sessions: Sequence[str], *, seeds: Sequence[int] = (0, 1, 2), duel_seeds: Sequence[int] = tuple(range(8)),
             field_seeds: Sequence[int] | None = None, workers: int = 1,
             params: ChallengerParams | None = None) -> dict[str, Any]:
    """Every challenger measured against base Vigil three ways.

    Alone: each version trades each session by itself against the recorded
    market, under the same seeds, compared minute by minute with base Vigil.
    Head to head: base Vigil and one challenger in the same match, competing
    for the same customers. In a field: base Vigil, every challenger, the
    stock example trader and a second copy of base Vigil in one match; each
    team's result is compared with base Vigil's in the same match, and the
    copy's says how large a difference luck alone makes. The field is played
    under twice as many seeds as it has teams unless ``field_seeds`` says
    otherwise: every team then has every slot's luck twice.
    """
    sessions = [str(s) for s in sessions]
    if field_seeds is None:
        field_seeds = tuple(range(2 * (len(CHALLENGERS) + 3)))  # with base Vigil, its copy and the example
    params = params or ChallengerParams.load(DEFAULT_PATH)
    reactions = study_reactions()
    names = [BASE, *CHALLENGERS]
    strategies = {name: STRATEGIES[name] for name in names}

    scores = score_strategies(strategies, sessions, seeds=seeds, reactions=reactions, workers=workers)
    base = scores[BASE]
    alone = []
    for name in names:
        score = scores[name]
        totals: dict[str, float] = {}
        for stats in score.stats:
            for key, value in stats.items():
                totals[key] = totals.get(key, 0) + value
        alone.append({
            "strategy": name, "pnl": [round(v, 2) for v in score.by_session().values()],
            "pnl_by_run": [round(float(v), 2) for v in score.pnl], "mean_pnl": round(score.mean_pnl, 2),
            "vs_base": score.versus(base) if name != BASE else None, "breaches": score.breaches,
            "errors": sum(len(result.errors) for result in score.results), "risk": score.risk(),
            "lots_per_session": {key: round(value / len(score.results)) for key, value in sorted(totals.items())},
        })

    # Two questions, two sets of reaction times. With every challenger as quick as base Vigil, a difference
    # is the strategy's; with the times measured on the real exchange, it includes what the design costs in
    # speed, as measured in one match (and so with that match's noise).
    reactions = {**reactions, f"preset:{CONTROL}": reactions[f"preset:{BASE}"]}
    same_speed = dict(reactions)
    for name in CHALLENGERS:
        same_speed[f"preset:{name}"] = reactions[f"preset:{BASE}"]
    duels = {"same_speed": _duels(sessions, duel_seeds, same_speed, workers),
             "measured_speed": _duels(sessions, duel_seeds, reactions, workers)}
    teams = {**strategies, CONTROL: STRATEGIES[BASE]}
    field = {}
    for speed, timing in (("same_speed", same_speed), ("measured_speed", reactions)):
        field[speed] = tournament(sessions, presets=teams, seeds=tuple(field_seeds), reactions=timing, workers=workers)
        field[speed]["versus_base"] = _versus_base(field[speed])
    fits = {}
    for key, name in params.meta.get("sessions", {}).items():
        fold = params.folds.get(key, {})
        fits[name] = {engine: {k: v for k, v in fold.get(engine, {}).items() if k not in ("policy",)}
                      for engine in CHALLENGERS}
    # What each session does on its own, against which the fitted models (each fitted without it) can be read.
    from vigil.strategies.challengers.ou import fit_reversion
    reversion = {Path(s).name: {key: value for key, value in fit_reversion([s]).items()
                                if key in ("c", "kappa", "half_life_updates", "horizons", "slopes")} for s in sessions}
    return {"sessions": [Path(s).name for s in sessions], "seeds": list(seeds), "duel_seeds": list(duel_seeds),
            "field_seeds": list(field_seeds), "reaction_times": {kind: list(value) for kind, value in reactions.items()},
            "timed_on_the_exchange": sorted(measured_reactions(REACTIONS_FILE) or measured_reactions()),
            "fits": fits, "reversion_by_session": reversion, "knobs": params.meta.get("knobs", {}), "alone": alone,
            "duels": duels, "field": field}


def t_over_sessions(by_session: Mapping[str, Sequence[float]]) -> float | None:
    """A mean difference over its standard error, with the session as the unit.

    Matches of one session under different seeds share its market, so they are
    not independent, and the standard error comes from the spread of the
    sessions' means. Four means are few to estimate a spread from, and by
    chance they can lie close together; the spread is therefore never taken as
    smaller than the luck within a session implies on its own (the spread
    between its seeds, over their number). Two copies of base Vigil in one
    field showed why: their sessions' means once fell within a hundred dollars
    of one another, which made a difference of a few hundred look certain.
    """
    groups = [np.asarray(values, dtype=float) for values in by_session.values()]
    if len(groups) < 2:
        return None
    means = np.array([group.mean() for group in groups])
    within = [group.var(ddof=1) / len(group) for group in groups if len(group) > 1]
    variance = max(float(means.var(ddof=1)), float(np.mean(within)) if within else 0.0)
    return float(means.mean() / math.sqrt(variance / len(groups))) if variance > 0 else None


def _versus_base(field: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Each team's P&L less base Vigil's in the same match of a field, over every match."""
    rows = {row["team"]: row for row in field["rows"]}
    base = rows[BASE]["runs"]
    out = {}
    for team, row in rows.items():
        if team == BASE:
            continue
        by_session = {session: np.array(row["runs"][session]) - np.array(base[session]) for session in base}
        every = np.concatenate(list(by_session.values()))
        out[team] = {"difference": round(float(every.mean()), 2), "difference_sd": round(float(every.std(ddof=1)), 2),
                     "t_over_sessions": t_over_sessions(by_session), "ahead": int((every > 0).sum()),
                     "matches": len(every),
                     "by_session": {session: round(float(values.mean()), 2) for session, values in by_session.items()}}
    return out


def _duels(sessions: Sequence[str], seeds: Sequence[int], reactions: Mapping[str, Reaction],
           workers: int) -> list[dict[str, Any]]:
    """Every challenger against base Vigil, paired, on every session under every seed."""
    jobs = [(session, seed, name, reactions) for name in CHALLENGERS for session in sessions for seed in seeds]
    outcomes = _each(_duel, jobs, workers)
    duels: dict[str, dict[str, Any]] = {}
    for (session, seed, name, _), (ours, theirs, our_status, their_status) in zip(jobs, outcomes, strict=True):
        row = duels.setdefault(name, {"challenger": name, "matches": [], "breaches": 0})
        row["matches"].append({"session": Path(session).name, "seed": seed, "base": round(ours, 2),
                               "challenger": round(theirs, 2)})
        row["breaches"] += (our_status != "OK") + (their_status != "OK")
    for row in duels.values():
        base_pnl = np.array([m["base"] for m in row["matches"]])
        their_pnl = np.array([m["challenger"] for m in row["matches"]])
        difference = their_pnl - base_pnl
        by_session: dict[str, list[float]] = {}
        for match in row["matches"]:
            by_session.setdefault(match["session"], []).append(match["challenger"] - match["base"])
        row.update({
            "base_mean": round(float(base_pnl.mean()), 2), "challenger_mean": round(float(their_pnl.mean()), 2),
            "difference": round(float(difference.mean()), 2),
            "difference_sd": round(float(difference.std(ddof=1)), 2),
            "t_over_sessions": t_over_sessions(by_session),
            "challenger_ahead": int((difference > 0).sum()), "matches_played": len(difference),
            "difference_by_session": {name: round(float(np.mean(v)), 2) for name, v in by_session.items()},
        })
    return list(duels.values())
