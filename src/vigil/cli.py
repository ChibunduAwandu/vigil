"""The ``vigil`` command."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from vigil.net import ApiError, redact
from vigil.paths import REPO_ROOT, all_market_data_files, artifacts_dir, market_data_file

RESULTS_DIR = REPO_ROOT / "docs" / "results"
EXPLORER_CHAINS = {1: "Ethereum mainnet", 11155111: "Sepolia"}  # where Etherscan can give a second opinion


def _say(*parts: Any) -> None:
    print(*parts, flush=True)


def _table(header: list[str], rows: list[list[Any]]) -> str:
    cells = [header] + [[str(cell) for cell in row] for row in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip() for row in cells]
    lines.insert(1, "  ".join("-" * width for width in widths))
    return "\n".join(lines)


# -- shared steps ----------------------------------------------------------------------------------

def _regulatory_watch(args: argparse.Namespace) -> dict[str, Any] | None:
    if getattr(args, "no_regulatory", False):
        return None
    from vigil.regulatory.store import Register, default_path
    if not default_path().is_file():
        return None
    register = Register()
    try:
        return register.snapshot()
    finally:
        register.close()


def _registry(rpc: str | None = None, address: str | None = None, *, signing: bool = False):
    """The registry to use, deploying one if this is a development chain that has none."""
    from vigil.chain.devnet import DEV_CHAIN_ID
    from vigil.chain.registry import Registry, connect
    w3, account = connect(rpc)
    if signing and account is None:
        raise SystemExit("anchoring needs a signing key: set VIGIL_ETH_PRIVATE_KEY")
    try:
        return Registry.at_known_address(w3, address)
    except LookupError as error:
        if w3.eth.chain_id != DEV_CHAIN_ID or not signing or address:
            raise
        registry = Registry.deploy(w3)
        _say(f"deployed a registry at {registry.address} on the local development chain "
             f"({str(error).split(';')[0]})")
        return registry


def _anchor(report, path: Path, uri: str = "", registry=None) -> tuple[dict[str, Any] | None, bool]:
    """File ``report`` and leave the receipt beside ``path``. Returns the receipt and whether filing failed.

    A report that is already on file under its identifier is not a failure;
    a *different* report on file under that identifier is, and so is a chain
    that cannot be reached. The report file is written either way.
    """
    from web3.exceptions import Web3Exception

    from vigil.chain.registry import AlreadyAnchored
    try:
        registry = registry or _registry(signing=True)
        receipt = registry.anchor(report, uri).to_dict()
    except AlreadyAnchored as error:
        entry = error.filed
        if error.same:
            # Perhaps it was filed by an earlier run that was cut off before it kept the receipt.
            recovered = None
            if not path.with_name("anchor.json").is_file():
                try:
                    recovered = registry.receipt_for(report)
                except (Web3Exception, ConnectionError, TimeoutError) as lookup:
                    _say(f"  (the receipt of the filing could not be rebuilt: {lookup})")
            if recovered is None:
                _say(f"report {report.report_id} is already anchored (entry {entry['sequence']}); nothing to do")
                return None, False
            _say(f"report {report.report_id} is already anchored (entry {entry['sequence']}); its receipt was "
                 "rebuilt from the chain's log of the filing")
            receipt = recovered.to_dict()
            path.with_name("anchor.json").write_text(json.dumps(receipt, indent=2) + "\n")
            return receipt, False
        _say(f"NOT anchored: a different report is on file under the identifier {report.report_id} "
             f"(entry {entry['sequence']}, sha256 {entry['report_hash']}). The registry keeps the first.")
        return None, True
    except ValueError as error:  # the report itself is at fault, and filing it again will not help
        _say(f"NOT anchored: {error}")
        return None, True
    except (ConnectionError, LookupError) as error:
        _say(f"not anchored: {error}\n  once that is put right: vigil chain anchor {path}")
        return None, True
    except (Web3Exception, TimeoutError) as error:
        _say(f"NOT CONFIRMED: the filing was sent but its outcome could not be read back ({error}).\n"
             f"  Run vigil chain anchor {path} again: nothing is ever filed twice, and if this filing went "
             "through its receipt is recovered.")
        return None, True
    path.with_name("anchor.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt, False


def _explorer_check(report, contract: str, chain_id: int) -> bool:
    """Ask Etherscan, which reads the chain from nodes of its own, whether it saw the same filing.

    Says nothing where there is no explorer to ask (a development chain) or no
    key to ask with. Returns False only if the explorer contradicts the file: an
    explorer that cannot be reached, or has not yet indexed the block, is no
    evidence either way.
    """
    key = os.environ.get("ETHERSCAN_API_KEY")
    if chain_id not in EXPLORER_CHAINS or not key:
        return True
    from vigil.chain import etherscan
    try:
        opinion = etherscan.second_opinion(report, contract, chain_id)
    except Exception as error:  # noqa: BLE001 - whatever went wrong, it is not a verdict on the report
        _say(f"  explorer      Etherscan could not be asked: {str(error).replace(key, '<key>')}")
        return True
    if not opinion["found"]:
        _say("  explorer      Etherscan has no record of this filing yet; it may still be indexing the block")
        return True
    if opinion["agrees"]:
        _say(f"  explorer      Etherscan agrees: filed in block {opinion['block_number']:,} "
             f"(tx {opinion['transaction']})")
        return True
    _say(f"  FAILED: Etherscan's record of the filing (tx {opinion['transaction']}) does not match this file")
    return False


def _finish(audit, args: argparse.Namespace, *, simulation=None) -> int:
    """Write the report, anchor it if asked, and print the outcome. Returns the process exit code."""
    from vigil.reports.render import render_markdown
    report = audit.report
    out = Path(args.out) if getattr(args, "out", None) else artifacts_dir() / "reports" / report.report_id
    out.mkdir(parents=True, exist_ok=True)
    path = report.save(out / "report.json")
    if simulation is not None:
        simulation.write_match_events(out / "match_events.csv")
        simulation.score.write_csv(out / "score_board.csv")
    receipt = None
    failed = False
    if getattr(args, "anchor", False):
        receipt, failed = _anchor(report, path, getattr(args, "uri", None) or "")
    (out / "report.md").write_text(render_markdown(report, receipt))

    summary = report.document["summary"]
    _say(f"\nreport {report.report_id}: {summary['alerts']} alerts"
         + (f", worst {summary['worst_severity']}" if summary["alerts"] else "")
         + f"; record {'reconciles' if not summary['integrity']['discrepancies'] else 'DOES NOT RECONCILE'} "
           f"({summary['integrity']['trade_reports_checked']:,} trades checked)")
    rows = []
    for name, entry in report.document["participants"].items():
        exchange = entry["exchange"] or {}
        rows.append([name, entry["alerts"] or "-", entry["worst_severity"] or "-",
                     ", ".join(entry["controls_triggered"]) or "-", exchange.get("status", "-"),
                     f"{entry['metrics'].get('profit_or_loss', 0):,.2f}"])
    _say(_table(["team", "alerts", "worst", "controls", "exchange", "P&L ($)"], rows))
    _say(f"\n  {path}\n  {out / 'report.md'}")
    _say(f"  sha256 {report.sha256}")
    if receipt:
        _say(f"  anchored: chain {receipt['chain_id']}, block {receipt['block_number']}, entry {receipt['sequence']}, "
             f"tx {receipt['transaction']}")
    return 1 if failed else 0


# -- commands --------------------------------------------------------------------------------------

def cmd_apis(args: argparse.Namespace) -> int:
    from vigil.regulatory import apivault
    catalog = apivault.fetch_catalog()
    _say(f"ApiVault lists {len(catalog):,} APIs in {len({e['category'] for e in catalog})} categories. "
         "Vigil integrates these:\n")
    rows = apivault.check_integrations(catalog)
    _say(_table(["API", "on ApiVault", "category", "auth", "used for"],
                [[r["name"], "yes" if r["listed"] else "NO", r["category"], r["auth"], r["purpose"]] for r in rows]))
    if args.query:
        found = [e for e in catalog if args.query.lower() in f"{e['name']} {e['description']} {e['category']}".lower()]
        _say(f"\n{len(found)} entries match {args.query!r}:\n")
        _say(_table(["API", "category", "auth", "description"],
                    [[e["name"], e["category"], e["auth"] or "none", e["description"][:80]] for e in found[:40]]))
    else:
        _say("\nLooked at and left out:")
        for name, reason in apivault.CONSIDERED.items():
            _say(f"  {name}: {reason}")
    return 0 if all(r["listed"] for r in rows) else 1


def cmd_train(args: argparse.Namespace) -> int:
    from vigil.ml.train import RULES, train_alpha_model
    started = time.time()
    from vigil.strategies.ensemble import StrategyConfig
    bundle, report = train_alpha_model(all_market_data_files(), horizon=args.horizon, n_rounds=args.rounds,
                                       learning_rate=args.learning_rate, defer_ticks=StrategyConfig().defer_ticks)
    path = bundle.save()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "alpha_model.json").write_text(json.dumps(report, indent=1) + "\n")
    summary = report["summary"]
    _say(f"trained in {time.time() - started:.1f} s; model written to {path}\n")
    _say("Directional accuracy, leave-one-session-out (share of correct calls on updates where the price moved):\n")
    rows = [["AdaBoost", f"{summary['adaboost']['accuracy']:.1%}",
             f"{summary['adaboost']['min']:.1%} to {summary['adaboost']['max']:.1%}", "", ""]]
    for name in RULES:
        entry = summary[name]
        rows.append([name, f"{entry['accuracy']:.1%}", f"{entry['min']:.1%} to {entry['max']:.1%}",
                     f"{entry['adaboost_relative_gain']:+.1%}", f"{entry['adaboost_wins_sessions']}/4"])
    _say(_table(["signal", "accuracy", "range over sessions", "AdaBoost vs it", "sessions won"], rows))
    timing = report["hedge_timing"]
    _say(f"\nPutting a hedge off one update when the forecast favours it by {timing['defer_ticks']} ticks "
         "(ticks gained per hedge, were one due on every update):\n")
    _say(_table(["forecast", "gain per hedge", "range over sessions", "waits", "gain when it waits"],
                [[name, f"{entry['gain_per_hedge']:+.3f}", f"{entry['min']:+.3f} to {entry['max']:+.3f}",
                  f"{entry['waits']:.0%}", f"{entry['gain_when_waiting']:+.3f}"]
                 for name, entry in timing["signals"].items()]))
    return 0


def _reactions(args: argparse.Namespace) -> dict:
    """Reaction times to simulate with: those measured on the real exchange, unless told to do without."""
    from vigil.backtest import measured_reactions
    return {} if getattr(args, "instant", False) else measured_reactions()


def cmd_backtest(args: argparse.Namespace) -> int:
    from vigil.backtest import ablation, format_ablation, tournament
    from vigil.sim.engine import ExchangeConfig
    from vigil.strategies.ensemble import PRESETS
    sessions = [market_data_file(s) for s in args.sessions] if args.sessions else all_market_data_files()
    presets = {name: PRESETS[name] for name in (args.presets or PRESETS)}
    reactions = _reactions(args)
    seeds = tuple(range(args.seeds))
    started = time.time()
    report = ablation(sessions, presets=presets, seeds=seeds, reactions=reactions, workers=args.workers)
    _say(f"{len(presets)} strategies x {len(sessions)} sessions x {len(seeds)} seeds in {time.time() - started:.1f} s "
         "(each strategy alone against the recorded market; P&L in dollars)")
    _say("traders answer with the reaction times measured on the real exchange\n" if reactions else
         "no reaction times have been measured (scripts/record_live_match.py): every trader answers instantly\n")
    _say(format_ablation(report))

    # The same study with every trader's orders arriving later. How late they really arrive is not
    # known; how much the answer depends on it should be.
    report["sensitivity"] = []
    for latency in args.latency:
        slower = ablation(sessions, presets=presets, seeds=seeds, reactions=reactions, workers=args.workers,
                          exchange=ExchangeConfig(latency=latency))
        report["sensitivity"].append({"latency": latency, "rows": [
            {key: row[key] for key in ("strategy", "mean_pnl", "vs_baseline", "vs_previous", "breaches")}
            for row in slower["rows"]]})
        _say(f"\nwith every message {latency * 1000:g} ms later:\n")
        _say(format_ablation(slower))

    started = time.time()
    together = tournament(sessions, presets=presets, seeds=tuple(range(args.tournament_seeds)), reactions=reactions,
                          workers=args.workers)
    _say(f"\nAll of them in one match, with the example trader that ships with Ready Trader Go "
         f"({len(together['seeds'])} seeds, {time.time() - started:.1f} s):\n")
    _say(_table(["team", *together["sessions"], "mean P&L", "sd over seeds", "rank in each seed"],
                [[row["team"], *(f"{v:,.0f}" for v in row["pnl"]), f"{row['mean_pnl']:,.0f}",
                  "-" if row["std_over_seeds"] is None else f"{row['std_over_seeds']:,.0f}",
                  " ".join(str(rank) for rank in row["ranks"])] for row in together["rows"]]))
    if not args.sessions and not args.presets and not args.instant:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        (RESULTS_DIR / "ablation.json").write_text(json.dumps(report, indent=1) + "\n")
        (RESULTS_DIR / "tournament.json").write_text(json.dumps(together, indent=1) + "\n")
    return 0


def cmd_challenge(args: argparse.Namespace) -> int:
    """The challengers: fit what they fit, or measure them against base Vigil."""
    from vigil import challenge
    from vigil.strategies.challengers import CHALLENGERS, DEFAULT_PATH
    sessions = all_market_data_files()
    if args.action == "fit":
        started = time.time()
        params = challenge.fit(sessions, seeds=tuple(range(args.tuning_seeds)), rl_episodes=args.rl_episodes,
                               workers=args.workers, say=_say, only=args.only)
        path = params.save(DEFAULT_PATH)
        _say(f"\nfitted in {time.time() - started:.0f} s; written to {path.relative_to(REPO_ROOT)}\n")
        rows = []
        for key, name in params.meta["sessions"].items():
            fold = params.folds[key]
            rows.append([name, f"{fold['skew']['weight']:g}", f"{fold['glft']['gamma']:g}",
                         f"{fold['glft']['A']:.1f} / {fold['glft']['k']:.3f}", f"{fold['ou']['weight']:g}",
                         f"{fold['ou']['c']:+.2f} / {fold['ou']['half_life_updates']:.0f}",
                         f"{fold['hawkes']['weight']:g}", f"{fold['hawkes']['branching_ratio']:.2f}",
                         f"{fold['rl']['pnl_by_episode'][-1]:,.0f}",
                         f"{fold['skew_fast']['weight']:g}" if "weight" in fold.get("skew_fast", {}) else "-"])
        _say("fitted on the other three sessions, for trading the session named:\n")
        _say(_table(["held out", "skew weight", "glft gamma", "glft A / k", "ou weight", "ou c / half-life",
                     "hawkes weight", "hawkes branching", "rl last episode P&L", "skew_fast weight"], rows))
        return 0

    started = time.time()
    report = challenge.evaluate(sessions, seeds=tuple(range(args.seeds)), duel_seeds=tuple(range(args.duel_seeds)),
                                field_seeds=None if args.field_seeds is None else tuple(range(args.field_seeds)),
                                workers=args.workers)
    _say(f"measured in {time.time() - started:.0f} s\n")
    _say("Alone against the recorded market (P&L in dollars, mean over seeds):\n")
    _say(_table(["version", *report["sessions"], "mean", "vs base Vigil", "t", "runs ahead"],
                [[row["strategy"], *(f"{v:,.0f}" for v in row["pnl"]), f"{row['mean_pnl']:,.0f}",
                  "-" if row["vs_base"] is None else f"{row['vs_base']['relative']:+.1%}",
                  "-" if row["vs_base"] is None else f"{row['vs_base']['t']:+.1f}",
                  "-" if row["vs_base"] is None else f"{row['vs_base']['runs_ahead']}/{row['vs_base']['runs']}"]
                 for row in report["alone"]]))
    for speed, label in (("same_speed", "every challenger as quick as base Vigil"),
                         ("measured_speed", "at the speeds measured on the real exchange")):
        _say(f"\nHead to head with base Vigil, in the same match ({label}; each match paired with its mirror):\n")
        _say(_table(["challenger", "base Vigil", "challenger", "difference", "t (sessions)", "pairs ahead"],
                    [[row["challenger"], f"{row['base_mean']:,.0f}", f"{row['challenger_mean']:,.0f}",
                      f"{row['difference']:+,.0f}",
                      "-" if row["t_over_sessions"] is None else f"{row['t_over_sessions']:+.1f}",
                      f"{row['challenger_ahead']}/{row['matches_played']}"] for row in report["duels"][speed]]))
        _say(f"\nAll in one match, with the example trader and a second copy of base Vigil ({label}):\n")
        field = report["field"][speed]
        versus = field["versus_base"]

        def against(team: str, versus=versus) -> list[str]:
            if team not in versus:
                return ["-", "-"]
            row = versus[team]
            return [f"{row['difference']:+,.0f}", "-" if row["t_over_sessions"] is None else f"{row['t_over_sessions']:+.1f}"]

        _say(_table(["team", *field["sessions"], "mean", "less base Vigil", "t (sessions)", "rank in each seed"],
                    [[row["team"], *(f"{v:,.0f}" for v in row["pnl"]), f"{row['mean_pnl']:,.0f}", *against(row["team"]),
                      " ".join(str(rank) for rank in row["ranks"])] for row in field["rows"]]))
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "challengers.json").write_text(json.dumps(report, indent=1) + "\n")
    _say(f"\nwritten to {(RESULTS_DIR / 'challengers.json').relative_to(REPO_ROOT)}; {len(CHALLENGERS)} challengers")
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    from vigil.pipeline import field_of, reactions_of, simulate_and_audit
    session = market_data_file(args.session)
    traders = field_of(session, args.scenario)
    started = time.time()
    audit = simulate_and_audit(session, traders, match_id=args.match_id, seed=args.seed, end_time=args.seconds,
                               regulatory_watch=_regulatory_watch(args),
                               reactions=reactions_of(traders, _reactions(args)))
    _say(f"simulated {audit.result.market['duration']:.0f} s of {session.name} with {len(traders)} teams "
         f"({audit.result.market['events']:,} events) and audited it in {time.time() - started:.1f} s")
    if args.publish:
        from vigil.bus import connect, topics
        from vigil.bus.services import publish_match
        bus = connect(args.bus)
        if hasattr(bus, "ensure_topics"):
            bus.ensure_topics(topics.PARTITIONS)
        count, _ = publish_match(bus, audit.header, audit.simulation.records())
        bus.close()
        _say(f"published {count:,} events to {topics.EVENTS}")
        # A monitor reports on what it receives in artifacts/reports/<id>. The report made here, from
        # the match itself, is kept out of its way, with the match's files, as a live match's is.
        args.out = args.out or str(artifacts_dir() / "runs" / audit.report.report_id)
    return _finish(audit, args, simulation=audit.simulation)


def cmd_audit(args: argparse.Namespace) -> int:
    from vigil.pipeline import audit_csv
    started = time.time()
    audit = audit_csv(args.match_events, match_id=args.match_id, exchange_json=args.exchange_json,
                      score_board=args.score_board, regulatory_watch=_regulatory_watch(args))
    _say(f"audited {audit.result.market['events']:,} events in {time.time() - started:.1f} s")
    return _finish(audit, args)


def cmd_live(args: argparse.Namespace) -> int:
    from vigil.live import KINDS, LiveMatch, display_available
    from vigil.pipeline import audit_records
    from vigil.reports.builder import outcomes_from_score_board
    teams = dict(item.split("=", 1) for item in args.team)
    if not teams:
        raise SystemExit(f"give at least one --team Name=kind; kinds: {', '.join(KINDS)}, or a path to an autotrader.py")
    match_id = args.match_id or time.strftime("live-%Y%m%d-%H%M%S")
    session = market_data_file(args.session)
    publisher = None
    if args.hud and not display_available():
        raise SystemExit('the display needs PySide6, which is not installed: pip install -e ".[hud]"')
    with LiveMatch(session, teams, match_id=match_id, speed=args.speed, seconds=args.seconds, hud=args.hud) as match:
        header = match.header()
        length = (args.seconds or 900.0) / args.speed
        _say(f"match {match_id}: {len(teams)} teams on the real exchange, about {length + match.open_delay:.0f} s; "
             f"files in {match.run_dir}")
        if args.hud:
            _say("  the exchange's display is opening in a window of its own; it stays open after the match, "
                 "until you close it")
        if args.publish:
            from vigil.bus import connect, topics
            from vigil.bus.services import MatchPublisher
            bus = connect(args.bus)
            if hasattr(bus, "ensure_topics"):
                bus.ensure_topics(topics.PARTITIONS)
            publisher = MatchPublisher(bus, header)

        def stream():
            for record in match.events():
                if publisher:
                    publisher.event(record)
                yield record

        def announce(alert) -> None:
            _say(f"  {alert.time_end:7.1f} s  {alert.severity.name:8s} {alert.control} {alert.competitor}: {alert.title}")

        audit = audit_records(header, stream(), on_alert=announce, regulatory_watch=_regulatory_watch(args),
                              outcomes=lambda: outcomes_from_score_board(match.score_board_path))
        exchange_status = match.wait(60)
        departures = match.departures()
    if publisher:
        count, _ = publisher.end()
        publisher.bus.close()
        _say(f"published {count:,} events")
    args.out = args.out or str(match.run_dir)
    status = _finish(audit, args)
    # A trader that the exchange disconnected exits cleanly and shows as a breach above. One that
    # crashed, or never loaded, played no match at all, and the table above does not say so.
    for name, left in departures.items():
        _say(f"  {name} left the match {left['seconds_after_start']:.0f} s after it started"
             + (f", FAILING with exit status {left['exit_code']}" if left["failed"] else "")
             + (f": {left['last_words']}" if left["last_words"] else ""))
    if exchange_status != 0:
        _say(f"  the exchange exited with status {exchange_status}; see {match.run_dir / 'exchange.out'}")
    if exchange_status != 0 or any(left["failed"] for left in departures.values()):
        return 1
    return status


def cmd_produce(args: argparse.Namespace) -> int:
    from vigil.bus import connect, topics
    from vigil.bus.services import publish_match
    from vigil.rtg.events import read_match_events
    from vigil.rtg.marketdata import file_sha256
    from vigil.sim.engine import ExchangeConfig
    from vigil.surveillance.engine import header_for
    path = Path(args.match_events)
    config = ExchangeConfig.from_json(args.exchange_json) if args.exchange_json else ExchangeConfig()
    teams = sorted({record.competitor for record in read_match_events(path) if record.competitor})
    header = header_for(args.match_id or f"rtg-{file_sha256(path)[:12]}", "csv", teams=teams, limits=config.limits(),
                        fees=config.fees(), instrument=config.instrument(), speed=config.speed)
    bus = connect(args.bus)
    if hasattr(bus, "ensure_topics"):
        bus.ensure_topics(topics.PARTITIONS)
    started = time.time()
    count, digest = publish_match(bus, header, read_match_events(path))
    bus.close()
    _say(f"published match {header.match_id}: {count:,} events in {time.time() - started:.1f} s (sha256 {digest})")
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    from vigil.bus import connect, topics
    from vigil.bus.services import Monitor
    from vigil.reports.render import render_markdown
    bus = connect(args.bus)
    if hasattr(bus, "ensure_topics"):
        bus.ensure_topics(topics.PARTITIONS)

    def on_report(audit) -> dict[str, Any] | None:
        report = audit.report
        out = artifacts_dir() / "reports" / report.report_id
        out.mkdir(parents=True, exist_ok=True)
        path = report.save(out / "report.json")
        receipt = None
        if args.anchor:
            # A monitor must not die because the chain is away: the report is on disk either way.
            receipt, _ = _anchor(report, path, args.uri or "")
        (out / "report.md").write_text(render_markdown(report, receipt))
        summary = report.document["summary"]
        _say(f"match {report.report_id}: {summary['alerts']} alerts, flagged: "
             f"{', '.join(summary['participants_flagged']) or 'nobody'}; report {path}"
             + (f"; anchored as entry {receipt['sequence']}" if receipt else ""))
        return receipt

    monitor = Monitor(bus, group=args.group, on_report=on_report, regulatory_watch=lambda: _regulatory_watch(args))
    _say(f"monitoring {topics.EVENTS} as consumer group {args.group!r}" + (" (one match, then exit)" if args.once else ""))
    try:
        monitor.run(idle_timeout=args.idle, max_matches=1 if args.once else None)
    except KeyboardInterrupt:
        pass
    finally:
        bus.close()
    return 0


def cmd_regulatory(args: argparse.Namespace) -> int:
    from vigil.regulatory.store import Register
    from vigil.surveillance.rulebook import load_rulebook
    rulebook = load_rulebook()
    register = Register()
    if args.action == "scrape":
        from vigil.regulatory.scraper import scrape
        bus = None
        if args.publish:
            from vigil.bus import connect
            bus = connect(args.bus)
        while True:
            result = scrape(rulebook, register, years=args.years, bus=bus)
            summary = result.summary()
            _say(f"{summary['requests']} queries to the Federal Register: {summary['seen']} hits, "
                 f"{summary['new']} new documents, {summary['changed']} changed; the register now holds "
                 f"{register.count()}")
            for error in summary["errors"]:
                _say(f"  failed: {error}")
            for document in result.added[:15]:
                _say(f"  new  {document.publication_date}  {document.type:13s} {document.title[:90]}")
            if not args.every:
                break
            try:
                time.sleep(args.every)  # the Federal Register publishes once a day; hourly is plenty
            except KeyboardInterrupt:
                break
        if bus:
            bus.close()
    snapshot = register.snapshot(limit=args.limit)
    if snapshot is None:
        _say("the register is empty; run: vigil regulatory scrape")
        return 1
    _say(f"\nregister: {snapshot['documents']} documents, as of {snapshot['as_of']}, fingerprint {snapshot['sha256'][:16]}")
    _say("most recent documents that feature a watched term in their title or abstract:\n")
    rows = []
    for item in snapshot["recent"]:
        controls = sorted({c.key for r in item["regulations"] for c in rulebook.controls_citing(r)})
        rows.append([item["publication_date"], item["type"], item["title"][:64], ", ".join(item["regulations"]),
                     ", ".join(controls)])
    _say(_table(["published", "type", "title", "regulation", "controls"], rows))
    register.close()
    return 0


def _when(timestamp: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(timestamp))


def cmd_chain(args: argparse.Namespace) -> int:
    from vigil.chain.registry import Registry, connect, earlier_deployments
    from vigil.reports.builder import ComplianceReport
    if args.action == "build":
        from vigil.chain import artifact
        built = artifact.build()
        _say(f"compiled with solc {built['compiler']}: {len(built['deployed_bytecode']) // 2 - 1} bytes of code")
        return 0
    if args.action == "cost":
        from vigil.chain.cost import ANCHOR_GAS, cost_of, eth_usd
        price = int(args.gwei * 10 ** 9) if args.gwei else connect(args.rpc)[0].eth.gas_price
        _say("anchoring one report: " + cost_of(args.gas or ANCHOR_GAS, price, eth_usd()).describe()
             + " (ETH price from CoinGecko)")
        return 0
    if args.action == "deploy":
        w3, account = connect(args.rpc)
        if account is None:
            raise SystemExit("deploying needs a signing key: set VIGIL_ETH_PRIVATE_KEY")
        registry = Registry.deploy(w3)
        _say(f"registry deployed at {registry.address} on chain {w3.eth.chain_id}, owned by {account.address}")
        earlier = earlier_deployments(w3.eth.chain_id)
        if earlier:
            _say(f"it replaces {earlier[-1]} as the registry used on this chain by default. What was anchored "
                 f"there is still there: --registry {earlier[-1]}")
        return 0

    if args.action == "verify":
        # A file that cannot even be read as a report has failed verification, not broken the tool.
        try:
            report = ComplianceReport.load(args.report)
        except ValueError as error:
            _say(f"FAILED: {error}")
            return 1
    elif args.action in ("anchor", "proof"):
        report = ComplianceReport.load(args.report)
    registry = _registry(args.rpc, args.registry, signing=args.action == "anchor")
    chain_id = registry.w3.eth.chain_id

    if args.action == "history":
        rows = [[e["sequence"], _when(e["anchored_at"]), e["alert_count"], e["worst_severity"] or "-",
                 e["report_hash"][:16] + "...", e["reporter"]] for e in registry.history()]
        _say(f"registry {registry.address} on chain {chain_id}: {len(rows)} report{'' if len(rows) == 1 else 's'}, "
             f"chain {'verifies' if registry.verify_chain() else 'DOES NOT VERIFY'}\n")
        _say(_table(["entry", "anchored", "alerts", "worst", "report sha256", "reporter"], rows))
        return 0
    if args.action == "about":
        # Start from the evidence: given the exchange's record of a match, or a report on it, list
        # every report that was ever filed about that record.
        path = Path(args.report)
        try:
            events = ComplianceReport.load(path).document["match"]["events_sha256"]
        except ValueError:
            from vigil.rtg.marketdata import file_sha256
            events = file_sha256(path)
        filings = registry.reports_about(events)
        _say(f"event stream {events}\nregistry {registry.address} on chain {chain_id}: "
             f"{len(filings)} report{'' if len(filings) == 1 else 's'} filed about it\n")
        if filings:
            _say(_table(["entry", "anchored", "alerts", "worst", "report sha256", "rulebook", "reporter"],
                        [[e["sequence"], _when(e["anchored_at"]), e["alert_count"], e["worst_severity"] or "-",
                          e["report_hash"], e["rulebook_hash"][:16] + "...", e["reporter"]] for e in filings]))
        return 0
    if args.action == "anchor":
        receipt, failed = _anchor(report, Path(args.report), args.uri or "", registry)
        if receipt:
            _say(f"anchored {report.report_id} as entry {receipt['sequence']} in block {receipt['block_number']} "
                 f"(tx {receipt['transaction']}, {receipt['gas_used']:,} gas)")
        return 1 if failed else 0
    if args.action == "verify":
        verdict = registry.verify(report)
        _say(f"report {report.report_id}\n  file sha256   {report.sha256}")
        if verdict.filed:
            _say(f"  on chain      {verdict.filed['report_hash']}  (entry {verdict.filed['sequence']}, anchored "
                 f"{_when(verdict.filed['anchored_at'])})")
        _say(f"  registry      {registry.address} on chain {chain_id}")
        receipt_file = Path(args.report).with_name("anchor.json")
        if receipt_file.is_file():
            # The receipt travels with the report, so it is never what decides which registry to
            # trust; but if it names another one, whoever is checking should know.
            receipt = json.loads(receipt_file.read_text())
            if (str(receipt.get("contract", "")).lower(), receipt.get("chain_id")) != (registry.address.lower(), chain_id):
                _say(f"  note: the receipt beside this file names registry {receipt.get('contract')} on chain "
                     f"{receipt.get('chain_id')}, which is not the one consulted")
        if verdict.ok:
            _say("  VERIFIED: this file is the report that was anchored")
        for problem in verdict.problems():
            _say(f"  FAILED: {problem}")
        if verdict.related:
            same = verdict.filed["rulebook_hash"]
            listed = ", ".join(f"entry {e['sequence']} ({'the same' if e['rulebook_hash'] == same else 'another'} "
                               f"rulebook, sha256 {e['report_hash'][:16]}...)" for e in verdict.related)
            _say(f"  note: {len(verdict.related)} other report{'' if len(verdict.related) == 1 else 's'} on the same "
                 f"event stream {'is' if len(verdict.related) == 1 else 'are'} on file: {listed}")
        explorer_agrees = _explorer_check(report, registry.address, chain_id) if verdict.filed else True
        return 0 if verdict.ok and explorer_agrees else 1
    if args.action == "proof":
        proof = report.alert_proof(args.alert)
        accepted = registry.verify_alert(proof)
        _say(json.dumps({k: v for k, v in proof.items() if k != "alert"}, indent=2))
        _say(f"the contract {'accepts' if accepted else 'REJECTS'} this proof against the anchored root")
        return 0 if accepted else 1
    raise SystemExit(f"unknown chain action {args.action}")


def cmd_replay(args: argparse.Namespace) -> int:
    """Play a finished match back in Ready Trader Go's own display."""
    import subprocess

    from vigil.live import display_available
    from vigil.paths import rtg_home
    path = Path(args.match_events).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"no such file: {path}")
    if not display_available():
        raise SystemExit('the display needs PySide6, which is not installed: pip install -e ".[hud]"')
    # The display takes the tick size and price band from an exchange.json where it is started: the
    # match's own if it left one (a live run directory does), else Ready Trader Go's, which a
    # simulated match was played under.
    where = path.parent if (path.parent / "exchange.json").is_file() else rtg_home()
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(rtg_home()), os.environ.get("PYTHONPATH", "")]))
    _say(f"replaying {path.name} ({path.parent}) in the exchange's display; close its window to finish")
    return subprocess.call([sys.executable, "-c", "import pathlib, sys; from ready_trader_go.hud.__main__ "
                            "import replay; sys.exit(replay(pathlib.Path(sys.argv[1])))", str(path)],
                           cwd=where, env=env)


def cmd_report(args: argparse.Namespace) -> int:
    from vigil.reports.builder import ComplianceReport
    from vigil.reports.render import render_markdown
    path = Path(args.report)
    anchor = path.with_name("anchor.json")
    receipt = json.loads(anchor.read_text()) if anchor.is_file() else None
    _say(render_markdown(ComplianceReport.load(path), receipt))
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    """Every scenario on every session: what each control catches, and what it wrongly flags."""
    from concurrent.futures import ProcessPoolExecutor

    from vigil.pipeline import SCENARIOS
    sessions = all_market_data_files()
    scenarios = [s for s in SCENARIOS if s != "rogues"]
    jobs = [(scenario, str(session)) for scenario in scenarios for session in sessions]
    started = time.time()
    with ProcessPoolExecutor() as pool:
        outcomes = list(pool.map(_calibration_job, jobs))
    matrix: dict[str, Any] = {}
    for (scenario, session), outcome in zip(jobs, outcomes, strict=True):
        matrix.setdefault(scenario, {})[Path(session).name] = outcome
    rows = []
    for scenario, by_session in matrix.items():
        caught = sum(bool(o["culprits_flagged"]) for o in by_session.values()) if scenario != "clean" else "-"
        controls = sorted({c for o in by_session.values() for c in o["culprit_controls"]})
        false_alarms = sum(o["honest_alerts"] for o in by_session.values())
        discrepancies = sum(o["discrepancies"] for o in by_session.values())
        rows.append([scenario, f"{caught}/{len(by_session)}" if scenario != "clean" else "-",
                     ", ".join(controls) or "-", false_alarms, discrepancies])
    _say(f"{len(jobs)} matches in {time.time() - started:.1f} s\n")
    _say(_table(["scenario", "sessions in which the culprit was flagged", "by controls",
                 "alerts against honest teams", "reconciliation failures"], rows))
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "detection.json").write_text(json.dumps(matrix, indent=1) + "\n")
    return 0


def _calibration_job(job: tuple[str, str]) -> dict[str, Any]:
    from vigil.backtest import measured_reactions
    from vigil.pipeline import HONEST, field_of, reactions_of, simulate_and_audit
    scenario, session = job
    traders = field_of(session, scenario)
    audit = simulate_and_audit(session, traders, seed=7, reactions=reactions_of(traders, measured_reactions()))
    culprits = [name for name in audit.header.teams if name not in HONEST]
    alerts = Counter((alert.competitor, alert.control) for alert in audit.alerts)
    return {
        "culprits": culprits,
        "culprits_flagged": sorted({name for name, _ in alerts if name in culprits}),
        "culprit_controls": sorted({control for name, control in alerts if name in culprits}),
        "honest_alerts": sum(count for (name, _), count in alerts.items() if name not in culprits),
        "alerts": {f"{name}:{control}": count for (name, control), count in sorted(alerts.items())},
        "discrepancies": audit.result.integrity["discrepancies"],
        "trades_checked": audit.result.integrity["trade_reports_checked"],
        "exchange": {name: team.status for name, team in audit.simulation.teams.items() if team.status != "OK"},
    }


# -- argument parsing ------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vigil", description="Compliance monitoring for Ready Trader Go.")
    parser.add_argument("--verbose", "-v", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    def add(name: str, function, summary: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=summary, description=summary)
        sub.set_defaults(run=function)
        return sub

    def reporting(sub: argparse.ArgumentParser, where: str = "artifacts/reports/<id>") -> None:
        sub.add_argument("--out", help=f"directory for the report (default: {where})")
        sub.add_argument("--anchor", action="store_true", help="anchor the report's fingerprint on chain")
        sub.add_argument("--uri", help=uri_help)
        sub.add_argument("--no-regulatory", action="store_true", help="leave the regulatory watch out of the report")

    uri_help = ("with --anchor: where the report can be fetched, logged on chain with the filing and public for "
                "good (default: nothing)")

    def on_bus(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--bus", help="bus URL (default: $VIGIL_BUS or kafka://127.0.0.1:9092)")

    sub = add("apis", cmd_apis, "The ApiVault APIs this project uses, checked against the live catalogue.")
    sub.add_argument("query", nargs="?", help="search the catalogue instead")

    sub = add("train", cmd_train, "Fit the AdaBoost alpha model and report its out-of-sample accuracy.")
    sub.add_argument("--horizon", type=int, default=1, help="order book updates ahead to predict (default 1)")
    sub.add_argument("--rounds", type=int, default=200)
    sub.add_argument("--learning-rate", type=float, default=0.2)

    instant_help = "ignore the reaction times measured on the real exchange: every trader answers instantly"

    sub = add("backtest", cmd_backtest, "Trade each strategy preset on each session and compare.")
    sub.add_argument("--sessions", nargs="*", help="session numbers or CSV paths (default: all)")
    sub.add_argument("--presets", nargs="*", help="strategy presets (default: all)")
    sub.add_argument("--seeds", type=int, default=3, help="seeds to play each session under (default 3)")
    sub.add_argument("--tournament-seeds", type=int, default=8, help="seeds for the all-in-one match (default 8)")
    sub.add_argument("--latency", type=float, nargs="*", default=[0.025, 0.05], metavar="SECONDS",
                     help="also run with every message this much later (default: 0.025 0.05)")
    sub.add_argument("--instant", action="store_true", help=instant_help)
    sub.add_argument("--workers", type=int, default=max((os.cpu_count() or 2) // 2, 1),
                     help="processes to run matches in (default: half the cores)")

    sub = add("challenge", cmd_challenge, "Experimental versions of Vigil: fit them, or measure them against "
                                              "base Vigil.")
    sub.add_argument("action", choices=["fit", "run"])
    sub.add_argument("--tuning-seeds", type=int, default=2, help="fit: seeds to try each tuning value under (default 2)")
    sub.add_argument("--rl-episodes", type=int, default=300,
                     help="fit: simulated sessions to train each reinforcement-learning policy on (default 300)")
    sub.add_argument("--seeds", type=int, default=3, help="run: seeds for each version alone (default 3)")
    sub.add_argument("--duel-seeds", type=int, default=8, help="run: seeds for each head-to-head match (default 8)")
    sub.add_argument("--field-seeds", type=int,
                     help="run: seeds for the all-in-one match (default twice the number of teams: every team has "
                          "every slot's luck twice)")
    sub.add_argument("--only", nargs="+", metavar="CHALLENGER",
                     help="fit: fit just these challengers, keeping every other's parameters as they are")
    sub.add_argument("--workers", type=int, default=max((os.cpu_count() or 2) // 2, 1),
                     help="processes to run matches in (default: half the cores)")

    sub = add("simulate", cmd_simulate, "Play a match in the simulator, audit it and write the report.")
    sub.add_argument("--session", default="1", help="session number or market data CSV (default 1)")
    sub.add_argument("--scenario", default="rogues", help="who plays: clean, wash, rogues, or one misbehaving trader")
    sub.add_argument("--seconds", type=float, help="stop the match early")
    sub.add_argument("--seed", type=int, default=0)
    sub.add_argument("--instant", action="store_true", help=instant_help)
    sub.add_argument("--match-id")
    sub.add_argument("--publish", action="store_true", help="also publish the match to the bus")
    reporting(sub, where="artifacts/reports/<id>, or with --publish artifacts/runs/<id>")
    on_bus(sub)

    sub = add("audit", cmd_audit, "Audit a match the real exchange has played, from its match_events.csv.")
    sub.add_argument("match_events")
    sub.add_argument("--score-board", help="the exchange's score_board.csv, for its verdict on each team")
    sub.add_argument("--exchange-json", help="the exchange.json the match ran with, for its limits and fees")
    sub.add_argument("--match-id")
    reporting(sub)

    sub = add("live", cmd_live, "Run a match on the real exchange and audit it as it happens.")
    sub.add_argument("--session", default="1")
    sub.add_argument("--team", action="append", default=[], metavar="NAME=KIND",
                     help="a team: preset:full, actor:spoofer, example, or a path to an autotrader.py (repeatable)")
    sub.add_argument("--seconds", type=float, help="use only this much of the market data")
    sub.add_argument("--speed", type=float, default=1.0, help="exchange clock speed (default 1.0: real time)")
    sub.add_argument("--match-id")
    sub.add_argument("--publish", action="store_true", help="also publish the events to the bus as they occur")
    sub.add_argument("--hud", action="store_true",
                     help="watch the match in Ready Trader Go's own display (needs PySide6). It takes CPU from the "
                          "traders, so leave it off for a match whose result you mean to keep")
    reporting(sub, where="the run directory, artifacts/runs/<id>")
    on_bus(sub)

    sub = add("produce", cmd_produce, "Publish a recorded match to the bus.")
    sub.add_argument("match_events")
    sub.add_argument("--exchange-json")
    sub.add_argument("--match-id")
    on_bus(sub)

    sub = add("monitor", cmd_monitor, "The surveillance service: consume matches from the bus and report on them.")
    sub.add_argument("--group", default="vigil-surveillance", help="consumer group")
    sub.add_argument("--once", action="store_true", help="exit after one match")
    sub.add_argument("--idle", type=float, help="exit after this many seconds without a message")
    sub.add_argument("--anchor", action="store_true", help="anchor each report's fingerprint on chain")
    sub.add_argument("--uri", help=uri_help)
    sub.add_argument("--no-regulatory", action="store_true", help="leave the regulatory watch out of the reports")
    on_bus(sub)

    sub = add("regulatory", cmd_regulatory, "The regulatory register: scrape the Federal Register, or show what is held.")
    sub.add_argument("action", choices=["scrape", "show"])
    sub.add_argument("--years", type=int, default=5, help="how far back the first scrape reaches (default 5)")
    sub.add_argument("--every", type=float, metavar="SECONDS", help="keep running, scraping again at this interval")
    sub.add_argument("--limit", type=int, default=12)
    sub.add_argument("--publish", action="store_true", help="publish new documents to the bus")
    on_bus(sub)

    sub = add("chain", cmd_chain, "The on-chain registry: deploy, anchor, verify.")
    sub.add_argument("action", choices=["build", "deploy", "anchor", "verify", "proof", "about", "history", "cost"])
    sub.add_argument("report", nargs="?", help="a report.json (anchor, verify, proof), or for 'about' the "
                                               "match_events.csv or report.json of a match")
    sub.add_argument("alert", nargs="?", help="an alert id (proof)")
    sub.add_argument("--uri", help="anchor: where the report can be fetched, logged on chain with the filing and "
                                   "public for good (default: nothing)")
    sub.add_argument("--rpc", help="node URL (default: $VIGIL_ETH_RPC or http://127.0.0.1:8545)")
    sub.add_argument("--registry", help="registry address (default: $VIGIL_REGISTRY or the last one deployed)")
    sub.add_argument("--gas", type=int, help="gas used (cost)")
    sub.add_argument("--gwei", type=float, help="gas price in gwei (cost; default: the node's current price)")

    sub = add("replay", cmd_replay, "Play a finished match back in Ready Trader Go's own display.")
    sub.add_argument("match_events", help="a match_events.csv: from a run directory, a report directory, or the "
                                          "exchange's own")

    sub = add("report", cmd_report, "Print a report as Markdown.")
    sub.add_argument("report")

    add("calibrate", cmd_calibrate, "Run every scenario on every session and tabulate what the controls catch.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(name)s %(message)s")
    if args.command == "chain" and args.action in ("anchor", "verify", "proof") and not args.report:
        raise SystemExit(f"vigil chain {args.action} needs a report.json")
    if args.command == "chain" and args.action == "about" and not args.report:
        raise SystemExit("vigil chain about needs the match_events.csv of a match, or a report.json on it")
    if args.command == "chain" and args.action == "proof" and not args.alert:
        raise SystemExit("vigil chain proof needs a report.json and an alert id")
    import requests
    from web3.exceptions import Web3Exception
    try:
        return args.run(args)
    except (ApiError, ConnectionError, FileNotFoundError, LookupError, TimeoutError, ValueError,
            requests.RequestException, Web3Exception) as error:
        if args.verbose:
            raise
        # Never echo a node's full address: a hosted provider's carries the API key.
        print(f"vigil: {redact(str(error))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
