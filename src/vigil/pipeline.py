"""From a match to its compliance report, in one process.

The same steps run here as in the streaming deployment (``vigil.bus``): a
header announces the match, its events pass through the surveillance engine
in order, and the engine's conclusions are frozen into a report. This module
does it with plain function calls; the bus does it across processes.
"""
from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vigil.backtest import EXAMPLE, Reaction, example_trader_factory, reaction_of, strategy_factory
from vigil.reports.builder import (
    ComplianceReport,
    build_report,
    outcomes_from_score_board,
    outcomes_from_simulation,
)
from vigil.reports.canonical import EventStreamHasher
from vigil.rtg.events import MatchHeader, MatchRecord, read_match_events
from vigil.rtg.marketdata import file_sha256, load_market_events
from vigil.sim.engine import ExchangeConfig, Simulator, TraderFactory
from vigil.sim.result import MatchResult
from vigil.strategies.badactors import BAD_ACTORS, wash_pair
from vigil.strategies.ensemble import PRESETS
from vigil.surveillance.alerts import Alert
from vigil.surveillance.engine import SurveillanceEngine, SurveillanceResult, header_for
from vigil.surveillance.rulebook import Rulebook, load_rulebook


@dataclass
class MatchAudit:
    """A match, what surveillance made of it, and the report that records it."""

    header: MatchHeader
    result: SurveillanceResult
    report: ComplianceReport
    simulation: MatchResult | None = None

    @property
    def alerts(self) -> list[Alert]:
        return self.result.alerts


def audit_records(header: MatchHeader, records: Iterable[MatchRecord], rulebook: Rulebook | None = None, *,
                  outcomes: dict[str, dict[str, Any]] | Callable[[], dict[str, dict[str, Any]]] | None = None,
                  regulatory_watch: dict[str, Any] | None = None,
                  on_alert: Callable[[Alert], None] | None = None) -> MatchAudit:
    """Run surveillance over an event stream and build the report.

    ``outcomes`` may be a function, called once the stream has ended: a live
    match's score board is only complete when the match is.
    """
    rulebook = rulebook or load_rulebook()
    engine = SurveillanceEngine(header, rulebook)
    hasher = EventStreamHasher()
    for record in records:
        hasher.update(record)
        fresh = engine.process(record)
        if fresh and on_alert:
            for alert in fresh:
                on_alert(alert)
    if callable(outcomes):
        outcomes = outcomes()
    announced = {alert.alert_id for alert in engine.alerts}  # before the end-of-session checks add theirs
    result = engine.finalize(outcomes)
    if on_alert:
        for alert in result.alerts:
            if alert.alert_id not in announced:
                on_alert(alert)
    report = build_report(result, rulebook, events_sha256=hasher.hexdigest(), outcomes=outcomes,
                          regulatory_watch=regulatory_watch)
    return MatchAudit(header, result, report)


def audit_csv(match_events: str | os.PathLike, *, match_id: str | None = None,
              exchange_json: str | os.PathLike | None = None, score_board: str | os.PathLike | None = None,
              rulebook: Rulebook | None = None, regulatory_watch: dict[str, Any] | None = None) -> MatchAudit:
    """Audit a match the real exchange has already played, from the files it left behind."""
    path = Path(match_events)
    config = ExchangeConfig.from_json(exchange_json) if exchange_json else ExchangeConfig()
    teams = sorted({record.competitor for record in read_match_events(path) if record.competitor})
    header = header_for(match_id or f"rtg-{file_sha256(path)[:12]}", "csv", teams=teams, limits=config.limits(),
                        fees=config.fees(), instrument=config.instrument(), speed=config.speed)
    outcomes = outcomes_from_score_board(score_board) if score_board else None
    return audit_records(header, read_match_events(path), rulebook, outcomes=outcomes,
                         regulatory_watch=regulatory_watch)


def simulation_header(match_id: str, session: str | os.PathLike, teams: Iterable[str],
                      config: ExchangeConfig | None = None) -> MatchHeader:
    config = config or ExchangeConfig()
    session = Path(session)
    # The simulator runs on match time whatever speed the configuration names, so its limits are
    # those of a match at normal speed.
    return header_for(match_id, "sim", teams=list(teams), limits=config.limits(), fees=config.fees(),
                      instrument=config.instrument(), market_data=session.name,
                      market_data_sha256=file_sha256(session))


def simulate_and_audit(session: str | os.PathLike, traders: Mapping[str, TraderFactory], *,
                       match_id: str | None = None, config: ExchangeConfig | None = None, seed: int = 0,
                       end_time: float | None = None, rulebook: Rulebook | None = None,
                       regulatory_watch: dict[str, Any] | None = None,
                       reactions: Mapping[str, Reaction | None] | None = None) -> MatchAudit:
    """Play a match in the simulator and audit it. ``reactions`` gives teams their reaction times, by name."""
    sim = Simulator(load_market_events(session), config, seed=seed, record="all", end_time=end_time)
    for name, factory in traders.items():
        sim.add_trader(name, factory, reaction=(reactions or {}).get(name))
    outcome = sim.run()
    if match_id is None:
        tag = hashlib.sha256(f"{sorted(traders)}|{seed}|{end_time}".encode()).hexdigest()[:8]
        match_id = f"sim-{Path(session).stem}-{tag}"
    header = simulation_header(match_id, session, traders, config)
    audit = audit_records(header, outcome.records(), rulebook, outcomes=outcomes_from_simulation(outcome),
                          regulatory_watch=regulatory_watch)
    audit.simulation = outcome
    return audit


# -- ready-made fields of traders ------------------------------------------------------------------

HONEST = ("VigilFull", "Baseline", "MeanRev", "Example")
_KINDS = {"VigilFull": "preset:full", "Baseline": "preset:baseline", "MeanRev": "preset:mean_reversion",
          "Example": EXAMPLE}


def reactions_of(field: Iterable[str], measured: Mapping[str, Reaction] | None) -> dict[str, Reaction | None]:
    """Reaction times for the teams of a field made by ``field_of``, from those measured on the real exchange."""
    return {name: reaction_of(_KINDS.get(name, "actor:"), measured) for name in field}


def field_of(session: str | os.PathLike, scenario: str = "clean") -> dict[str, TraderFactory]:
    """A field of traders for a named scenario: ``clean``, ``wash`` or any key of ``BAD_ACTORS``."""
    honest: dict[str, TraderFactory] = {
        "VigilFull": strategy_factory(PRESETS["full"], session),
        "Baseline": strategy_factory(PRESETS["baseline"], session),
        "MeanRev": strategy_factory(PRESETS["mean_reversion"], session),
        "Example": example_trader_factory(),
    }
    if scenario == "clean":
        return honest
    if scenario == "wash":
        # The pair needs a quiet price to meet at; the stock example trader quotes right there.
        first, second = wash_pair()
        del honest["Example"]
        return {**honest, "WashA": first, "WashB": second}
    if scenario == "rogues":
        first, second = wash_pair()
        del honest["Example"]
        return {**honest, "WashA": first, "WashB": second, "Spoofer": BAD_ACTORS["spoofer"],
                "Stuffer": BAD_ACTORS["stuffer"], "Closer": BAD_ACTORS["close_marker"],
                "Reckless": BAD_ACTORS["reckless"]}
    if scenario in BAD_ACTORS:
        return {**honest, scenario.title().replace("_", ""): BAD_ACTORS[scenario]}
    raise ValueError(f"unknown scenario {scenario!r}; choose from clean, wash, rogues, {', '.join(BAD_ACTORS)}")


SCENARIOS = ("clean", "wash", "rogues", *BAD_ACTORS)
