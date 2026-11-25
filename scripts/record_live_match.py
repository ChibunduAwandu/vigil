"""Add a match played on the real exchange to docs/results/live_matches.json.

    vigil live --session 3 --match-id my-match --team VigilFull=preset:full --team Example=example
    python scripts/record_live_match.py artifacts/runs/my-match

Everything recorded is read from the files the exchange left in the run directory: its score
board, and an audit of its event file. Two things are then done with it.

How long each team took to answer the exchange is measured and kept, by kind of trader, in
docs/results/reaction_times.json, which the backtests use from then on: on the real exchange the
faster of two traders gets the customer, and a simulation that ignores that flatters the slow one.

And the same field is played on the same market data in the simulator, with those reaction times,
once per seed, to see how far the simulator is from the real thing. docs/RESULTS.md is rendered
from the files this writes (scripts/render_docs.py).

That comparison is like for like only while the traders are the ones that played the match. A run
directory says which those were (traders.sha256), and a match whose traders have changed since is
not simulated again: what was recorded for it at the time is kept.
"""
from __future__ import annotations

import argparse
import functools
import json
from pathlib import Path

import numpy as np

from vigil.backtest import REACTIONS_FILE, example_trader_factory, run_match
from vigil.live import FINGERPRINT_FILE, LiveMatch, entries, reaction_times, traders_fingerprint
from vigil.pipeline import audit_csv
from vigil.strategies.challengers import STRATEGIES
from vigil.strategies.ensemble import trader_class

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "results" / "live_matches.json"
SEEDS = tuple(range(8))


def simulated(run_dir: Path, kinds: dict[str, str], market_data: Path,
              reactions: dict[str, tuple[float, float]] | None) -> dict[str, list[float]] | None:
    """Each team's P&L with the same field in the simulator, per seed; None if the field cannot be rebuilt."""
    factories = {}
    for name, kind in kinds.items():
        if kind == "example":
            factories[name] = example_trader_factory()
        elif kind.startswith("preset:"):
            # The same settings the live trader ran with, down to the model that never saw the session.
            settings = json.loads((run_dir / f"{LiveMatch.module_name(name)}.strategy.json").read_text())
            config = STRATEGIES[settings.pop("preset")].with_(**settings)
            factories[name] = functools.partial(trader_class(config), config=config)
        else:
            return None
    results = [run_match(market_data, factories, seed=seed, reactions=reactions)[0] for seed in SEEDS]
    return {name: [round(result[name].pnl, 2) for result in results] for name in kinds}


def record(run_dir: Path, note: str | None = None, previous: dict | None = None, *, keep_simulation: bool = False,
           as_played: bool | None = None) -> dict:
    """The entry for one match. ``previous`` is what was recorded for it before, if anything.

    ``as_played`` answers, for a run directory that does not say which traders
    played it, whether the simulation recorded for it used those traders.
    """
    config = json.loads((run_dir / "exchange.json").read_text())
    market_data = Path(config["Engine"]["MarketDataFile"])
    kinds = entries(run_dir)
    audit = audit_csv(run_dir / "match_events.csv", exchange_json=run_dir / "exchange.json",
                      score_board=run_dir / "score_board.csv", match_id=run_dir.name)
    document = audit.report.document
    timing = reaction_times(run_dir)
    measured = {name: (entry["typical"], entry["jitter"]) for name, entry in timing.items()}

    previous = previous or {}
    today = traders_fingerprint()
    played_with = (run_dir / FINGERPRINT_FILE).read_text().strip() if (run_dir / FINGERPRINT_FILE).is_file() else None
    has_simulation = bool(previous.get("simulator_seeds"))
    # Simulating again would play today's traders. Where those are known not to be the ones that
    # played the match, a like-for-like simulation already on record is worth more than a new one.
    outdated = played_with is not None and played_with != today and previous.get("simulated_as_played") is True
    if has_simulation and (keep_simulation or outdated):
        instant = {name: team["simulated_pnl_without_reaction_times"] for name, team in previous["teams"].items()}
        timed = {name: team["simulated_pnl"] for name, team in previous["teams"].items()}
        seeds, simulated_with = previous["simulator_seeds"], previous.get("simulated_with")
        if as_played is None:
            as_played = previous.get("simulated_as_played")
    else:
        replayable = config["Engine"]["Speed"] == 1.0
        instant = simulated(run_dir, kinds, market_data, None) if replayable else None
        timed = simulated(run_dir, kinds, market_data, measured) if replayable else None
        seeds, simulated_with = (list(SEEDS) if timed else []), (today if timed else None)
        if played_with is not None:
            as_played = played_with == today
    teams = {}
    for name, entry in document["participants"].items():
        metrics, exchange = entry["metrics"], entry["exchange"]
        teams[name] = {
            "entered_as": kinds[name],
            "status": exchange["status"],
            "exchange_pnl": exchange["profit_or_loss"],
            "audit_pnl": metrics["profit_or_loss"],
            "alerts": entry["alerts"],
            "etf_position": metrics["etf_position"],
            "future_position": metrics["future_position"],
            "max_abs_unhedged": metrics["max_abs_unhedged"],
            "longest_unhedged_seconds": metrics["longest_unhedged_seconds"],
            "seconds_at_position_limit": metrics["seconds_at_position_limit"],
            "reaction": timing.get(name),
            "simulated_pnl": timed[name] if timed else None,
            "simulated_pnl_without_reaction_times": instant[name] if instant else None,
        }
    integrity = document["summary"]["integrity"]
    return {
        "match_id": run_dir.name,
        # A match cut short plays a copy of the head of a session, which carries no name of its own.
        "market_data": market_data.name if market_data.parent != run_dir else "part of a session",
        "seconds": document["match"]["duration_seconds"],
        "events": document["match"]["events"],
        "events_sha256": document["match"]["events_sha256"],
        "trade_reports_checked": integrity["trade_reports_checked"],
        "hedges_checked": integrity["hedges_checked"],
        "scores_checked": integrity["scores_checked"],
        "discrepancies": integrity["discrepancies"],
        "alerts": document["summary"]["alerts"],
        "simulator_seeds": seeds,
        "played_with": played_with,
        "simulated_with": simulated_with,
        "simulated_as_played": as_played if seeds else None,
        "note": note if note is not None else previous.get("note"),
        "teams": teams,
    }


def keep_reaction_times(entry: dict, path: Path = REACTIONS_FILE) -> dict:
    """Make this match's reaction times, by kind of trader, the table the backtests read.

    The table is replaced, not added to: the times that matter are relative to
    the other teams in the same match, so those of two matches do not mix. The
    challengers' study keeps a table of its own (``--timing-file``), from a match
    between base Vigil and the challengers, so as not to displace the presets'.
    """
    by_kind: dict[str, list[dict]] = {}
    for team in entry["teams"].values():
        if team["reaction"] and team["entered_as"] != "custom":
            by_kind.setdefault(team["entered_as"], []).append(team["reaction"])
    table = {kind: {**{key: float(np.mean([m[key] for m in measured])) for key in ("median", "spread", "typical",
                                                                                   "jitter")},
                    "ticks": int(sum(m["ticks"] for m in measured)), "match": entry["match_id"]}
             for kind, measured in sorted(by_kind.items())}
    path.write_text(json.dumps(table, indent=1) + "\n")
    return table


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", type=Path, help="a run directory written by `vigil live` (artifacts/runs/<id>)")
    parser.add_argument("--note", help="anything about the conditions of the match worth keeping with its result")
    parser.add_argument("--keep-timing", action="store_true",
                        help="make this match's reaction times the ones the backtests use "
                             "(docs/results/reaction_times.json); choose a match with every kind of trader in it")
    parser.add_argument("--timing-file", type=Path, default=REACTIONS_FILE,
                        help="with --keep-timing: the table to write (default docs/results/reaction_times.json; the "
                             "challengers' study reads docs/results/reaction_times_challengers.json)")
    parser.add_argument("--keep-simulation", action="store_true",
                        help="leave the simulator's results for this match as they were recorded, and bring "
                             "only what is read from the exchange's files up to date")
    parser.add_argument("--as-played", choices=("yes", "no"),
                        help="for a run directory with no traders.sha256 (one older than that file): whether the "
                             "simulation recorded for it was made with the traders that played the match")
    args = parser.parse_args()
    matches = json.loads(RECORD.read_text()) if RECORD.exists() else {}
    entry = matches[args.run_dir.name] = record(
        args.run_dir.resolve(), args.note, matches.get(args.run_dir.name), keep_simulation=args.keep_simulation,
        as_played={"yes": True, "no": False, None: None}[args.as_played])
    RECORD.write_text(json.dumps(matches, indent=1) + "\n")
    print(f"recorded {entry['match_id']}: {entry['events']:,} events; {entry['trade_reports_checked']:,} trade reports, "
          f"{entry['hedges_checked']:,} hedges and {entry['scores_checked']} final positions checked, "
          f"{entry['discrepancies']} discrepancies; {entry['alerts']} alerts")
    if entry["simulator_seeds"]:
        print("simulated with " + {True: "the traders that played the match", False: "traders changed since the match",
                                   None: "traders that may have changed since the match (see --as-played)"}
              [entry["simulated_as_played"]]
              + ("" if entry["simulated_with"] == traders_fingerprint() else "; kept from an earlier recording"))
    for name, team in entry["teams"].items():
        sims = team["simulated_pnl"]
        reaction = team["reaction"]
        print(f"  {name:14s} {team['entered_as']:22s} {team['status']:6s} exchange {team['exchange_pnl']:>10,.2f}  "
              f"audit {team['audit_pnl']:>10,.2f}  "
              + (f"simulator {np.mean(sims):>9,.0f} +- {np.std(sims, ddof=1):,.0f}  " if sims else "")
              + (f"reaction {reaction['median'] * 1000:.2f} ms (for a simulation: {reaction['typical'] * 1000:.2f} "
                 f"+- {reaction['jitter'] * 1000:.2f})" if reaction else ""))
    if args.keep_timing:
        kept = keep_reaction_times(entry, args.timing_file.resolve())
        print(f"reaction times for {len(kept)} kinds of trader are now in {args.timing_file}")
