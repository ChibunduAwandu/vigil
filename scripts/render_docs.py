"""Regenerate the two documents whose content is data: docs/RESULTS.md and docs/CONTROLS.md.

The numbers in RESULTS.md are read from docs/results/*.json, which the commands
`vigil train`, `vigil backtest` and `vigil calibrate` write, along with
scripts/record_live_match.py for matches played on the real exchange. Nothing in it is typed by
hand, so it cannot drift from what the code measures. CONTROLS.md is read off config/rulebook.yaml.

    vigil train && vigil backtest && vigil calibrate && python scripts/render_docs.py
"""
from __future__ import annotations

import json
from pathlib import Path
from statistics import mean, stdev

from vigil.live import traders_fingerprint
from vigil.ml.train import ALWAYS_WAIT, RULES
from vigil.surveillance.rulebook import load_rulebook

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "docs" / "results"


def table(header: list[str], rows: list[list], align: str) -> str:
    rule = ["---:" if a == "r" else "---" for a in align]
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(rule) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def pct(x: float, digits: int = 1) -> str:
    return f"{x * 100:.{digits}f}%"


def signed(x: float) -> str:
    return f"{x * 100:+.1f}%"


def session_name(file_name: str) -> str:
    return file_name.replace("market_data", "session ").replace(".csv", "")


def listed(names: list[str]) -> str:
    """`a`, `b` and `c`."""
    quoted = [f"`{name}`" for name in names]
    return quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + " and " + quoted[-1]


def load(name: str) -> dict | None:
    path = RESULTS / name
    return json.loads(path.read_text()) if path.exists() else None


def results() -> str:
    model = load("alpha_model.json")
    ablation = load("ablation.json")
    tournament = load("tournament.json")
    detection = load("detection.json")
    live = load("live_matches.json") or {}
    reactions = load("reaction_times.json") or {}
    challenge = load("challengers.json")

    out = ["# Results", "",
           "Everything on this page is generated from the JSON files in `docs/results/`, which the commands "
           "below write. To reproduce it:", "",
           "```shell", "vigil train && vigil backtest && vigil calibrate && python scripts/render_docs.py", "```", "",
           "The data is the four sample sessions that ship with Ready Trader Go: fifteen minutes each, about "
           "half a million market events per session. Four sessions is a small sample. The figures below are "
           "measurements on that sample, not estimates of what would happen on other data.", ""]
    out += strategies(ablation, model)
    out += competition(tournament, reactions)
    out += the_model(model)
    out += challengers(challenge, live)
    out += surveillance(detection)
    out += agreement(live)
    return "\n".join(out)


# -- strategies -----------------------------------------------------------------------------------------

def strategies(ablation: dict, model: dict) -> list[str]:
    sessions = ablation["sessions"]
    rows = {row["strategy"]: row for row in ablation["rows"]}
    base, full, kelly = rows["baseline"], rows["full"], rows["kelly"]
    lift = full["vs_baseline"]
    timing = model["hedge_timing"]
    timed = timing["signals"]["adaboost"]
    hedged = full["lots_per_session"]["hedge_lots"]
    timing_dollars = timed["gain_per_hedge"] * hedged  # a tick is a dollar a lot
    seeds = len(ablation["seeds"])

    out = ["## Trading strategies", "",
           "Each row adds one component to the row above it. Every strategy trades each session alone against "
           "the recorded market, in the simulator, and the boosted model used on a session was fitted without "
           f"that session. Each session is played under {seeds} seeds, which fix the instants at which the "
           "exchange publishes its books (they jitter, as on the real exchange), and every strategy is shown "
           "the same ones. "
           + ("The trader's orders reach the exchange after the reaction time measured for it in a real match "
              "(see [below](#how-fast-each-trader-is))." if ablation["reaction_times"] else
              "No reaction times had been measured when this was run, so orders reach the exchange the instant "
              "they are sent: the best case.")
           + " P&L is in dollars, as scored by the exchange's own accounting, averaged over the seeds.", ""]
    body = []
    for name, row in rows.items():
        def versus(key: str, row=row) -> str:
            v = row[key]
            return "-" if v is None else f"{signed(v['relative'])} (t = {v['t']:+.1f})"
        body.append([f"`{name}`", *(f"{v:,.0f}" for v in row["pnl"]), f"**{row['mean_pnl']:,.0f}**",
                     versus("vs_baseline"), versus("vs_previous"), f"{row['lots_per_session'].get('hedge_lots', 0):,}"])
    out += [table(["strategy", *map(session_name, sessions), "mean", "vs baseline", "vs row above",
                   "lots hedged per session"], body, "l" + "r" * (len(sessions) + 4)), "",
            "What the rows are:", "",
            "- `baseline`: a market maker that quotes one price a side, chosen for expected profit, and hedges "
            "every fill in the future at once.",
            "- `inventory`: adds a price on inventory, from a dynamic programme over the position limit "
            "(`strategies/inventory.py`).",
            "- `mean_reversion`: adds taking of resting ETF orders that are rich or cheap to the future by "
            "more than costs plus a volatility margin.",
            "- `kelly`: replaces hedge-at-once with a Kelly-sized band of un-hedged inventory "
            "(`strategies/kelly.py`), capped by the exchange's un-hedged-lots rule.",
            "- `momentum`: adds a one-update forecast from book pressure (microprice), used to time hedges.",
            "- `full`: replaces that forecast with the boosted model's.", "",
            f"**The full strategy earned {signed(lift['relative'])} more than the baseline on average "
            f"({full['mean_pnl']:,.0f} against {base['mean_pnl']:,.0f} dollars a session), and more in "
            f"{lift['runs_ahead']} of {lift['runs']} runs.** "
            f"The t-statistics compare two strategies minute by minute over the same market "
            f"({len(sessions) * 15} paired session-minutes, each averaged over the seeds). Taking whole sessions "
            f"as the unit instead, which assumes less and has only {len(sessions) - 1} degrees of freedom, the full "
            f"strategy against the baseline gives t = {lift['t_over_sessions']:+.1f} and each step over the row "
            "above it: " + ", ".join(f"`{name}` {row['vs_previous']['t_over_sessions']:+.1f}"
                                     for name, row in list(rows.items())[1:]) + ".", "",
            "How to read it:", "",
            f"- Most of the gain is the Kelly band ({signed(kelly['vs_previous']['relative'])} over the row above "
            f"it). It comes from not hedging: lots hedged per session fall from "
            f"{rows['mean_reversion']['lots_per_session']['hedge_lots']:,} to "
            f"{kelly['lots_per_session']['hedge_lots']:,}, and each hedge costs half the future's spread.",
            f"- Inventory pricing and mean reversion together add "
            f"{signed(rows['mean_reversion']['vs_baseline']['relative'])}.",
            f"- The forecast adds little that can be told from noise: "
            f"{signed(rows['momentum']['vs_previous']['relative'])} for the book-pressure rule "
            f"(t = {rows['momentum']['vs_previous']['t']:+.1f}) and "
            f"{signed(full['mean_pnl'] / kelly['mean_pnl'] - 1)} for the boosted model relative to no forecast. "
            "It could not add much. It looks one update (a quarter of a second) ahead, and when it is "
            f"confident enough to act on, the price then moves its way by {timed['gain_when_waiting']:.2f} ticks on "
            f"average, while trading on it would mean crossing a spread of {2 * timing['half_spread']:.1f} ticks. So "
            "it is used only where acting is free, to time hedges that were due anyway, and the Kelly band leaves "
            f"few of those: {hedged:,} lots a session. Even the whole of the timing gain measured "
            f"[below](#what-the-forecast-is-worth), {timed['gain_per_hedge']:.2f} ticks a lot, would come to about "
            f"{timing_dollars:,.0f} dollars a session, {pct(timing_dollars / full['mean_pnl'])} of the P&L.", ""]

    out += ["Risk, pooled over all runs:", ""]
    body = [[f"`{name}`", f"{row['risk']['pnl_std_per_minute']:,.0f}", f"{row['risk']['sharpe_per_minute']:.2f}",
             f"{row['risk']['worst_minute']:,.0f}", f"{row['risk']['max_drawdown']:,.0f}", row["breaches"],
             row["errors"]] for name, row in rows.items()]
    out += [table(["strategy", "std of P&L per minute", "mean / std per minute", "worst minute", "max drawdown",
                   "limit breaches", "orders the exchange rejected"], body, "lrrrrrr"), "",
            f"The extra return is not free. Carrying inventory raises the minute-to-minute variability of P&L "
            f"from {base['risk']['pnl_std_per_minute']:,.0f} to {full['risk']['pnl_std_per_minute']:,.0f} dollars, "
            f"and the ratio of mean to standard deviation goes from {base['risk']['sharpe_per_minute']:.2f} for "
            f"the baseline to {full['risk']['sharpe_per_minute']:.2f} for the full strategy. "
            + ("No strategy breached a limit in any run." if not any(r["breaches"] for r in rows.values())
               else "**Some runs ended in a limit breach; see the table.**"), ""]

    if ablation.get("sensitivity"):
        out += ["### If orders arrive later", "",
                "How long a trader's order takes to reach the exchange is not something a simulator knows. "
                "The table repeats the study with every message delayed by a fixed time on top of the above. "
                "A result that holds across it can be believed; one that does not is an artefact of the "
                "timing assumed.", ""]
        cases = [("as above", rows)] + [(f"+{case['latency'] * 1000:g} ms", {r["strategy"]: r for r in case["rows"]})
                                        for case in ablation["sensitivity"]]
        body = [[f"`{name}`", *(f"{case[name]['mean_pnl']:,.0f}" for _, case in cases)] for name in rows]
        body.append(["**full vs baseline**", *(f"**{signed(case['full']['vs_baseline']['relative'])}**"
                                               for _, case in cases)])
        out += [table(["mean P&L", *(label for label, _ in cases)], body, "l" + "r" * len(cases)), "",
                "And the t-statistic of each step over the row above it:", ""]
        body = [[f"`{name}`", *(f"{case[name]['vs_previous']['t']:+.1f}" for _, case in cases)]
                for name in list(rows)[1:]]
        out += [table(["step", *(label for label, _ in cases)], body, "l" + "r" * len(cases)), "",
                steps_that_survive(cases), ""]
    return out


def steps_that_survive(cases: list[tuple[str, dict]]) -> str:
    """Which steps of the study are significant under every timing assumption, in words."""
    names = list(cases[0][1])[1:]
    firm = [name for name in names if all(case[name]["vs_previous"]["t"] >= 2.0 for _, case in cases)]
    shaky = [name for name in names if name not in firm]
    sentence = ("The steps that stand up under every assumption (t of 2 or more each time) are "
                + ", ".join(f"`{name}`" for name in firm) + "." if firm else
                "No step is significant under every assumption.")
    if shaky:
        sentence += (" " + ", ".join(f"`{name}`" for name in shaky)
                     + (" does" if len(shaky) == 1 else " do") + " not, and should be read as unproven.")
    return sentence


# -- competition ----------------------------------------------------------------------------------------

def competition(tournament: dict, reactions: dict) -> list[str]:
    seeds = len(tournament["seeds"])
    out = ["### Under competition", "",
           "All six presets and the example trader that ships with Ready Trader Go, in the same match, "
           f"competing for the same customer orders. Each session is played under {seeds} seeds; with several "
           "teams in a match the seed also settles who reaches the exchange first where their reaction times "
           "do not.", ""]
    body = []
    for row in tournament["rows"]:
        ranks = row["ranks"]
        body.append([f"`{row['team']}`", *(f"{v:,.0f}" for v in row["pnl"]), f"**{row['mean_pnl']:,.0f}**",
                     "-" if row["std_over_seeds"] is None else f"{row['std_over_seeds']:,.0f}",
                     f"{min(ranks)} to {max(ranks)}" if min(ranks) != max(ranks) else f"{ranks[0]}"])
    out += [table(["team", *map(session_name, tournament["sessions"]), "mean", "sd over seeds",
                   "rank, best to worst seed"], body, "l" + "r" * (len(tournament["sessions"]) + 3)), ""]
    fixed = [row["team"] for row in tournament["rows"] if min(row["ranks"]) == max(row["ranks"])]
    out += ["With seven teams sharing the flow every P&L is smaller. "
            + (f"Only {', '.join(f'`{team}`' for team in fixed)} finished in the same place under every seed; "
               if fixed else "No team finished in the same place under every seed; ")
            + "where two teams' ranks overlap, the order between them in any one match is luck.", ""]

    if reactions:
        out += ["### How fast each trader is", "",
                "On the real exchange several traders answer the same order book update, and the exchange acts "
                "on their orders in the order they arrive: the first to arrive gets the customer. The time from "
                "an update being published to a team's first order being acted on was measured in matches "
                "played on the real exchange (`vigil.live.reaction_times`), and the simulations above give each "
                "trader its measured time.", ""]
        body = [[f"`{kind}`", f"{entry['median'] * 1000:.2f}", f"{entry['spread'] * 1000:.2f}", f"{entry['ticks']:,}",
                 f"`{entry['match']}`"] for kind, entry in sorted(reactions.items(), key=lambda kv: kv[1]["median"])]
        out += [table(["trader", "median (ms)", "spread (ms)", "updates answered", "measured in"], body, "lrrrl"), "",
                "These are times at the exchange, so a trader that arrives second also waits for the first "
                "one's orders to be processed. The differences between traders are small next to the update "
                "interval and decide who trades all the same.", ""]
    return out


# -- the model ------------------------------------------------------------------------------------------

def the_model(model: dict) -> list[str]:
    summary = model["summary"]
    ada = summary["adaboost"]
    timing = model["hedge_timing"]
    timed = timing["signals"]["adaboost"]
    out = ["## The AdaBoost model", "",
           f"The model predicts whether the future's mid price will be higher or lower "
           f"{model['horizon_updates']} order book update ahead (about 0.25 s), from "
           f"{len(model['full_model']['feature_importance'])} "
           f"features of the book and the trade flow that it found useful (of 25 offered). It is "
           f"{model['n_rounds']} decision stumps boosted with learning rate {model['learning_rate']}, written in "
           "numpy (`ml/adaboost.py`) and checked against scikit-learn in the tests.", "",
           "Accuracy is measured leave-one-session-out: to score a session the model is fitted on the other "
           "three, including its probability calibration. \"Accuracy\" is the share of correct directional "
           "calls among updates on which the signal made a call and the price then moved; the same definition "
           "is applied to every signal. The books are those a trader is shown, at the instants it is shown "
           "them.", ""]
    body = [["**AdaBoost**", f"**{pct(ada['accuracy'])}**", f"{pct(ada['min'])} to {pct(ada['max'])}", "", ""]]
    for name in RULES:
        entry = summary[name]
        body.append([name, pct(entry["accuracy"]), f"{pct(entry['min'])} to {pct(entry['max'])}",
                     f"{signed(entry['adaboost_relative_gain'])} ({entry['adaboost_point_gain'] * 100:+.1f} points)",
                     f"{entry['adaboost_wins_sessions']} of {len(model['sessions'])}"])
    momentum = summary["momentum: last 4 updates"]
    best_name = max(RULES, key=lambda name: summary[name]["accuracy"])
    best = summary[best_name]
    out += [table(["signal", "accuracy", "range over sessions", "AdaBoost relative to it", "sessions AdaBoost wins"],
                  body, "lrrrr"), "",
            "How to read it:", "",
            f"- Against a price-momentum rule the model is {signed(momentum['adaboost_relative_gain'])} better in "
            f"relative terms ({pct(ada['accuracy'])} against {pct(momentum['accuracy'])}), ahead in "
            f"{momentum['adaboost_wins_sessions']} of {len(model['sessions'])} sessions."
            + (" Price momentum is worse than a coin here: at this horizon the future mean-reverts."
               if momentum["accuracy"] < 0.5 else ""),
            f"- Against the best single hand-written signal ({best_name}), the model is "
            f"{signed(best['adaboost_relative_gain'])}: {pct(ada['accuracy'])} against {pct(best['accuracy'])}, "
            f"ahead in {best['adaboost_wins_sessions']} sessions of {len(model['sessions'])}. "
            "Whatever the model has found beyond that one signal, it is this much and no more.",
            "- What the model adds over a raw signal is a calibrated probability, which is what the hedge "
            "timing uses, and a call on every update (a signal such as imbalance abstains when the book is "
            "balanced).", ""]
    out += ["When the model is confident it is right more often:", ""]
    body = []
    for threshold in ("0.55", "0.60", "0.65"):
        accuracies = [s["adaboost"]["when_confident"][threshold]["accuracy"] for s in model["per_session"].values()]
        coverage = [s["adaboost"]["when_confident"][threshold]["coverage"] for s in model["per_session"].values()]
        body.append([f"at least {threshold} either way", pct(sum(accuracies) / len(accuracies)),
                     pct(sum(coverage) / len(coverage))])
    out += [table(["predicted probability", "accuracy", "share of moving updates"], body, "lrr"), "",
            "Feature weights in the model fitted on all four sessions (share of the total vote):", "",
            ", ".join(f"`{f['feature']}` {pct(f['weight'], 0)}" for f in model["full_model"]["feature_importance"][:10])
            + ".", ""]

    rule = "book pressure (microprice)"
    per_session = [scores["hedge_timing"] for scores in model["per_session"].values()]
    ahead = sum(s["adaboost"]["gain_per_hedge"] > s[rule]["gain_per_hedge"] for s in per_session)
    rule_gain = timing["signals"][rule]["gain_per_hedge"]
    out += ["### What the forecast is worth", "",
            "Accuracy is not what the strategy is paid for. It uses the forecast for one decision: a hedge that is "
            f"due is put off for one update when the forecast says the future is about to move its way by at least "
            f"{timing['defer_ticks']} ticks. The table prices that decision on the quotes shown at each update, "
            "leave-one-session-out, as if a hedge fell due on every update and as often one way as the other. "
            "Which updates hedges do fall due on is the strategy's business, so this is what each forecast "
            "offers, not what the strategy collects; the backtest above measures that.", ""]
    body = [[name, f"{entry['gain_per_hedge']:+.3f}", f"{entry['min']:+.3f} to {entry['max']:+.3f}",
             pct(entry["waits"], 0), f"{entry['gain_when_waiting']:+.3f}"] for name, entry in timing["signals"].items()]
    out += [table(["forecast", "ticks gained per hedge", "range over sessions", "share of hedges put off",
                   "ticks gained when put off"], body, "lrrrr"), "",
            f"A hedge pays half the future's spread, {timing['half_spread']:.2f} ticks on average. Timing it on the "
            f"boosted model's forecast gets {timed['gain_per_hedge']:.3f} of a tick back "
            f"({pct(timed['gain_per_hedge'] / timing['half_spread'])} of the cost, standard error "
            f"{timed['standard_error']:.3f}). The one-line book-pressure rule gets {rule_gain:.3f}, and the boosted "
            f"model is ahead of it in {ahead} of {len(per_session)} sessions"
            + (": for the decision it is used for, the model is no better than the rule. "
               if timed["gain_per_hedge"] <= rule_gain or ahead * 2 <= len(per_session) else ". ")
            + f"Putting every hedge off regardless gets {timing['signals'][ALWAYS_WAIT]['gain_per_hedge']:+.3f}, which "
            "is the check that the gain comes from the forecast and not from the accounting.", ""]
    return out


# -- challengers ----------------------------------------------------------------------------------------

CHALLENGER_LINES = {
    "skew": "forecast-skewed quoting. Quotes are priced around where the boosted model says the future is heading, "
            "not where it is (Cartea, Jaimungal & Penalva, *Algorithmic and High-Frequency Trading*, ch. 10)",
    "glft": "optimal quoting with fitted fill rates. Quote distances come from the closed-form solution of Guéant, "
            "Lehalle & Fernandez-Tapia (2013) for a market maker with a position limit, with the rate at which "
            "customer orders reach each price fitted to the sessions",
    "ou": "mean-reversion carrying. The future's deviation from its recent average is modelled as an "
          "Ornstein-Uhlenbeck process, and the inventory Vigil carries leans the way the fitted model expects the "
          "price to go: back towards the average if it reverts, onwards if it runs on",
    "hawkes": "a self-exciting (Hawkes) model of the future's aggressive order flow, which comes in bursts. Expected "
              "bursts put the quotes further out and make them smaller, and shrink the inventory carried",
    "rl": "reinforcement learning. A tabular Q-learning policy, trained in the simulator, moves base Vigil's "
          "quotes a tick either way or withdraws them",
    "skew_fast": "forecast-skewed quoting at base Vigil's speed. Quotes are skewed by book pressure, how far the "
                 "future's touch price weighted by size sits from its mid, which is worked out from the future's book "
                 "before the ETF's arrives; `skew` waits for the boosted model instead",
    "rl_fast": "`rl`'s policy with its deciding done before the update arrives: the choice for the state it is "
               "in is worked out whenever that state changes, and on the update only looked up. It trades as `rl` "
               "does, event for event",
}
NUMBERS = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six", 7: "Seven"}
FAST = {"skew_fast": "skew", "rl_fast": "rl"}  # the versions rebuilt to send as quickly as base Vigil, and their originals


SENDS_LATER = {"skew": "`skew` waits for the forecast before it quotes", "rl": "`rl` consults its policy"}


def control_match(live: dict, prefer: str | None = None) -> tuple[str, dict[str, float]] | None:
    """A live match with two copies of base Vigil in it (``prefer`` if it is one, else the latest), and each
    team's typical reaction there, in ms."""
    controls = [match_id for match_id, match in live.items()
                if sum(team["entered_as"] == "preset:full" for team in match["teams"].values()) >= 2]
    if not controls:
        return None
    match_id = prefer if prefer in controls else controls[-1]
    return match_id, {name: team["reaction"]["typical"] * 1000 for name, team in live[match_id]["teams"].items()
                      if team.get("reaction")}


def challengers(report: dict | None, live: dict | None = None) -> list[str]:
    if not report:
        return []
    sessions = report["sessions"]
    names = [row["challenger"] for row in report["duels"]["same_speed"]]
    techniques = [name for name in names if name not in FAST]
    fast = [name for name in names if name in FAST]
    out = ["## Challengers: experimental versions of Vigil", "",
           f"{NUMBERS.get(len(techniques), len(techniques))} versions of the trader each replace one part of base "
           "Vigil (the `full` strategy) with a technique from the market-making literature, so that a difference in "
           "result can be put down to that part."
           + (f" {NUMBERS.get(len(fast), len(fast))} more, {listed(fast)}, are {listed([FAST[n] for n in fast])} "
              "rebuilt to send their orders as quickly as base Vigil does." if fast else "")
           + " Each can be entered as a team, `preset:<name>`, like any preset (`strategies/challengers/`).", ""]
    out += [f"- `{name}`: {CHALLENGER_LINES[name]}." for name in names if name in CHALLENGER_LINES]
    timed = {kind.split(":", 1)[-1] for kind in report["timed_on_the_exchange"]}
    untimed = [name for name in names if name not in timed]
    out += ["", "Whatever a challenger fits from data was fitted leave-one-session-out (`vigil challenge fit`): to "
            "trade a session it uses parameters fitted to the other three, and the one tuning knob each has was "
            "chosen by what it earned trading those three. The boosted model it trades with never saw the session "
            "either. "
            + ("Their reaction times were measured on the real exchange, as base Vigil's were." if not untimed else
               "Their reaction times were measured on the real exchange, in one match with base Vigil; "
               + listed(untimed) + (" were" if len(untimed) > 1 else " was") + " not in it and "
               + ("are" if len(untimed) > 1 else "is") + " given base Vigil's, from whose handler "
               + ("they send their" if len(untimed) > 1 else "it sends its") + " first orders."), ""]

    out += ["What was fitted, for trading each session:", ""]
    body = []
    for name, fits in report["fits"].items():
        skew, glft, ou, hawkes, rl = (fits[key] for key in ("skew", "glft", "ou", "hawkes", "rl"))
        episodes = rl.get("pnl_by_episode") or [0.0]
        tail = episodes[-max(len(episodes) // 10, 1):]
        body.append([session_name(name), f"{skew['weight']:g}",
                     f"{glft['gamma']:g} (A {glft['A']:.1f}, k {glft['k']:.3f}, R² {glft['r2']:.3f})",
                     f"{ou['weight']:g} (" + (f"reverting, half-life {ou['half_life_updates']:.0f} updates"
                                              if ou["c"] > 0 else "runs on") + f", c {ou['c']:+.2f})",
                     f"{hawkes['weight']:g} (branching {hawkes['branching_ratio']:.2f}, "
                     f"corr. with 10 s variance {hawkes['horizon_correlation']:.2f})",
                     f"{mean(tail):,.0f}",
                     f"{fits['skew_fast']['weight']:g}" if "weight" in fits.get("skew_fast", {}) else "-"])
    out += [table(["held out", "skew: weight", "glft: risk aversion", "ou: weight", "hawkes: weight",
                   "rl: P&L in the last tenth of training", "skew_fast: weight"], body, "lllllll"),
            "", "`rl_fast` plays the policy fitted for `rl`.", ""]
    own = report.get("reversion_by_session") or {}
    if own:
        runs_on = [name for name, fit in own.items() if fit["c"] <= 0]
        wrong = [name for name, fits in report["fits"].items() if name in own and (fits["ou"]["c"] > 0) != (own[name]["c"] > 0)]
        out += ["Fitted to each session on its own, the future " + (
                    "reverts towards its average in every session" if not runs_on else
                    f"runs on in {', '.join(map(session_name, runs_on))} and reverts in the "
                    + ("other three" if len(runs_on) == 1 and len(own) == 4 else "others"))
                + (". Where it runs on it does so strongly enough to decide the sign of every fit that includes it, "
                   if runs_on and len(runs_on) == 1 else ". ")
                + (f"so the model trading each of the {len(wrong)} sessions expects the opposite of what that session "
                   "does: four sessions are too few for this model to be judged on." if len(wrong) == len(own) else
                   f"The model trading {' and '.join(map(session_name, wrong))} expects the opposite of what "
                   "it does." if wrong else "Every fitted model expects what its own session does."), ""]

    rows = {row["strategy"]: row for row in report["alone"]}
    base = rows["full"]
    body = []
    for name, row in rows.items():
        versus = row["vs_base"]
        body.append([f"`{name}`" + (" (base Vigil)" if name == "full" else ""),
                     *(f"{v:,.0f}" for v in row["pnl"]), f"**{row['mean_pnl']:,.0f}**",
                     "-" if versus is None else f"{signed(versus['relative'])} (t = {versus['t']:+.1f})",
                     "-" if versus is None or versus["t_over_sessions"] is None else f"{versus['t_over_sessions']:+.1f}",
                     "-" if versus is None else f"{versus['runs_ahead']} of {versus['runs']}", row["breaches"]])
    out += ["### Alone", "",
            f"Each version trades each session by itself against the recorded market, under {len(report['seeds'])} "
            "seeds, every version under the same ones; compared with base Vigil minute by minute (the t-statistic) "
            "and session by session.", "",
            table(["version", *map(session_name, sessions), "mean", "vs base Vigil", "t over sessions",
                   "runs ahead", "limit breaches"], body, "l" + "r" * (len(sessions) + 5)), ""]
    departures = rows.get("rl", {}).get("lots_per_session", {}).get("departures")
    if departures is not None:
        out += [f"`rl`'s policy quotes otherwise than base Vigil on {departures:,.0f} updates a session, of some "
                "3,600 (four a second for fifteen minutes): safe policy improvement keeps base Vigil's choice "
                "wherever the training gave too little evidence against it, and four sessions give little.", ""]

    times = report["reaction_times"]

    def ms(kind: str) -> str:
        if kind not in times:
            return "-"
        return f"{times[kind][0] * 1000:.2f}" + ("" if kind.split(":", 1)[-1] in timed or kind == "preset:full"
                                                 else " (base Vigil's)")

    def duel_cells(row: dict) -> list[str]:
        differences = [match["challenger"] - match["base"] for match in row["matches"]]
        level = sum(abs(d) < 0.005 for d in differences)
        won = sum(d >= 0.005 for d in differences)
        return [f"{row['difference']:+,.0f}",
                "-" if row["t_over_sessions"] is None else f"{row['t_over_sessions']:+.1f}",
                f"{won}–{level}–{len(differences) - won - level}"]

    same = {row["challenger"]: row for row in report["duels"]["same_speed"]}
    measured = {row["challenger"]: row for row in report["duels"]["measured_speed"]}
    body = [[f"`{name}`", ms(f"preset:{name}"), *duel_cells(same[name]), *duel_cells(measured[name])] for name in same]
    out += ["### Head to head", "",
            f"Base Vigil and one challenger in the same match, competing for the same customers: every session "
            f"under {len(report['duel_seeds'])} seeds. In a match between two teams, who reaches the exchange first "
            "moves either side's result by a few thousand dollars, so each match is played twice, the challenger "
            "in each team slot once, and the pair averaged: luck then falls to each side equally, and two copies "
            "of base Vigil come out exactly level. The t-statistic is the mean difference over its standard error "
            "with the session as the unit; four sessions are few to estimate that from, so it is never taken as "
            "smaller than the luck between seeds within a session implies.", "",
            "Each comparison is made twice. With the challenger as quick to answer the exchange as base Vigil "
            "(the strategy alone), and at the speeds measured for each in a match on the real exchange "
            f"(base Vigil's reaction there: {ms('preset:full')} ms), which adds what the design costs in speed. "
            "Differences are the challenger's P&L less base Vigil's, in dollars, averaged over the pairs.", "",
            table(["challenger", "reaction (ms)", "equal speed: difference", "t over sessions", "pairs won–level–lost",
                   "measured speed: difference", "t over sessions", "pairs won–level–lost"], body, "lrrrrrrr"), ""]

    fields = report["field"]
    control = "full-copy"

    def versus(speed: str, team: str) -> list[str]:
        row = fields[speed]["versus_base"].get(team)
        if row is None:
            return ["-", "-"]
        return [f"{row['difference']:+,.0f}", "-" if row["t_over_sessions"] is None else f"{row['t_over_sessions']:+.1f}"]

    def label(team: str) -> str:
        return {"full": "`full` (base Vigil)", control: f"`{control}` (base Vigil again: the control)"}.get(
            team, f"`{team}`")

    at_measured = {row["team"]: row for row in fields["measured_speed"]["rows"]}
    body = [[label(row["team"]), f"**{row['mean_pnl']:,.0f}**", *versus("same_speed", row["team"]),
             f"{at_measured[row['team']]['mean_pnl']:,.0f}", *versus("measured_speed", row["team"])]
            for row in fields["same_speed"]["rows"]]
    out += ["### All in one match", "",
            f"Base Vigil, every challenger, the example trader and a second copy of base Vigil in the same match, "
            f"every session under {len(fields['same_speed']['seeds'])} seeds; the teams move round the slots from "
            "seed to seed, so each has each slot's luck the same number of times. Each team's P&L is compared with "
            "base Vigil's in the same match. The second copy is the control: it trades exactly as base Vigil does, "
            "so what separates the two is luck, and a challenger's difference from base Vigil means something only "
            "when it is clearly larger. Mean P&L per match in dollars.", "",
            table(["team", "equal speed: mean", "less base Vigil", "t over sessions", "measured speed: mean",
                   "less base Vigil", "t over sessions"], body, "lrrrrrr"), ""]

    alone_winners = [name for name, row in rows.items() if row["vs_base"] and row["vs_base"]["t"] >= 2.0
                     and (row["vs_base"]["t_over_sessions"] or 0) >= 2.0]
    alone_losers = [name for name, row in rows.items() if row["vs_base"] and row["vs_base"]["t"] <= -2.0]
    winners = [name for name, row in same.items() if (row["t_over_sessions"] or 0) >= 2.0]
    losers = [name for name, row in same.items() if (row["t_over_sessions"] or 0) <= -2.0]
    in_field = fields["same_speed"]["versus_base"]
    luck = in_field[control]
    field_level = [name for name in same if abs(in_field[name]["t_over_sessions"] or 0) < 2.0]
    field_behind = [name for name in same if (in_field[name]["t_over_sessions"] or 0) <= -2.0]
    field_ahead = [name for name in same if (in_field[name]["t_over_sessions"] or 0) >= 2.0]
    gaps = [in_field[name]["difference"] for name in field_level]
    base_ms = times["preset:full"][0]
    slower = [name for name in measured if name in timed and times[f"preset:{name}"][0] > 1.2 * base_ms]
    close = [name for name in measured if name in timed and name not in slower]
    given = [name for name in measured if name not in timed]  # not timed: given base Vigil's reaction time
    timing = load("reaction_times_challengers.json") or {}
    on_exchange = control_match(live or {}, prefer=next((entry.get("match") for entry in timing.values()), None))
    control_sentence = fast_sentence = ""
    if on_exchange:
        match_id, typical = on_exchange
        teams = (live or {})[match_id]["teams"]
        copies = [name for name, team in teams.items() if team["entered_as"] == "preset:full"]
        kind_of = {name: team["entered_as"].split(":", 1)[-1] for name, team in teams.items()}

        slow_teams = [name for name in teams if kind_of[name] in slower]
        fast_teams = [name for name in teams if kind_of[name] in fast]
        reference, twin = copies[0], copies[1]

        def ahead(names: list[str]) -> str:
            """``a`` on 25 and ``b`` on 23 (per cent of the updates each answered with base Vigil)."""
            shares = [f"{100 * teams[name]['reaction']['ahead_of'].get(reference, float('nan')):.0f}" for name in names]
            if len(names) == 1:
                return f"`{kind_of[names[0]]}` on {shares[0]}"
            return (", ".join(f"`{kind_of[name]}`" for name in names[:-1]) + f" and `{kind_of[names[-1]]}` on "
                    + ", ".join(shares[:-1]) + " and " + shares[-1])

        copy_share = f"{100 * teams[twin]['reaction']['ahead_of'].get(reference, float('nan')):.0f}"
        control_sentence = (
            f"In the match the reaction times come from (`{match_id}`, below), base Vigil's copy reached the "
            f"exchange ahead of it on {copy_share} per cent of the updates both answered"
            + (f", {ahead(slow_teams)}" if slow_teams else "") + ": the delay is the code's, not the machine's. ")
        if fast_teams:
            fast_sentence = (
                f"- {listed([kind_of[name] for name in fast_teams])} were rebuilt to send as quickly as base Vigil, "
                f"and on the real exchange they did: in `{match_id}` they reached the exchange ahead of base Vigil on "
                f"{ahead(fast_teams).split(' on ', 1)[1]} per cent of the updates both answered, as its copy did on "
                f"{copy_share}. As quick, they are level with base Vigil head to head: "
                + "; ".join(f"`{name}` {same[name]['difference']:+,.0f} dollars a match (t = "
                            f"{same[name]['t_over_sessions']:+.1f})" for name in fast if name in same)
                + (". `rl_fast` makes exactly the decisions `rl` makes, and `rl` departs from base Vigil's so seldom "
                   "that the two trade alike." if "rl_fast" in fast else "."))
    skew_gaps = [team["exchange_pnl"] - mean(team["simulated_pnl"]) for match in (live or {}).values()
                 for team in match["teams"].values()
                 if team["entered_as"] == "preset:skew" and team.get("simulated_pnl")]
    skew_sentence = ""
    if "skew" in slower and skew_gaps and min(skew_gaps) > 0:
        skew_sentence = (
            "The simulator gives a trader one reaction time an update, so at measured speed it delays `skew`'s "
            "takes, which leave before the forecast is worked out, with its quotes, and overstates what its speed "
            f"costs: in each of the {len(skew_gaps)} matches `skew` played on the real exchange it earned more than "
            f"the simulator gave it, by {min(skew_gaps):,.0f} to {max(skew_gaps):,.0f} dollars. ")
    # What the measured speeds alone change: each difference at measured speed less the same at equal speed.
    speed_effect = {name: measured[name]["difference"] - same[name]["difference"] for name in measured}

    def behind_or_ahead(dollars: float) -> str:
        return f"{abs(dollars):,.0f} dollars a match {'behind' if dollars < 0 else 'ahead of'}"

    out += ["How to read it:", "",
            "- Alone, " + (listed(alone_winners) + " earned more than base Vigil by a margin that holds both minute "
                           "by minute and session by session" if alone_winners else
                           "no challenger earned more than base Vigil by a margin that holds both minute by minute "
                           "and session by session")
            + ("; " + listed(alone_losers) + " earned clearly less (t of -2 or below)." if alone_losers else "."),
            "- Head to head at equal speed, " + (listed(winners) + " beat base Vigil consistently across sessions"
                                                 if winners else "no challenger beat base Vigil consistently across "
                                                 "sessions")
            + ("; base Vigil beat " + listed(losers) + " consistently." if losers else "."),
            f"- In a field at equal speed, the second copy of base Vigil ended {behind_or_ahead(luck['difference'])} "
            f"the first (t = {luck['t_over_sessions']:+.1f}), which is luck. "
            + (f"{listed(field_level)} ended between {min(gaps):+,.0f} and {max(gaps):+,.0f} dollars a match against "
               "base Vigil, none with a t of 2 or more: no more distinguishable from it than its own copy"
               if field_level else "")
            + (("; " if field_level else "") + f"{listed(field_ahead)} ended ahead of it with a t of 2 or more ("
               + ", ".join(f"{in_field[name]['difference']:+,.0f}" for name in field_ahead) + "), "
               + ("though not head to head: what a trader earns in a field depends on who else is in it"
                  if not set(field_ahead) & set(winners) else "as head to head") if field_ahead else "")
            + (("; " if field_level or field_ahead else "") + f"{listed(field_behind)} clearly behind it"
               if field_behind else "") + ".",
            ("- At measured speeds, " + listed(slower) + (" reach" if len(slower) > 1 else " reaches")
             + " the exchange later than base Vigil, and against base Vigil that alone decides most matches. "
             + "They do more before they send (" + "; ".join(SENDS_LATER.get(name, f"`{name}`: more work")
                                                     for name in slower)
             + "). That is microseconds, but between traders that see the same update at the same moment it decides "
             "who is served first (ARCHITECTURE.md, \"Getting there first\"). " + control_sentence + skew_sentence
             if slower else "- At measured speeds, ")
            + (listed(close) + (" were" if len(close) > 1 else " was") + " ahead of base Vigil about as often as "
               "behind it, so " + ("their" if len(close) > 1 else "its") + " results at measured speeds say nothing "
               "about " + ("their" if len(close) > 1 else "its") + " speed; that the hundredths of a millisecond "
               "between the times measured for them and base Vigil's change a simulated duel's result by as much as "
               f"{max(abs(speed_effect[name]) for name in close):,.0f} dollars shows how much these results hang on "
               "who is first." if close else "")
            + (f" {listed(given)} {'were' if len(given) > 1 else 'was'} not in that match and "
               f"{'are' if len(given) > 1 else 'is'} given base Vigil's time, so at measured speed "
               f"{'they come out' if len(given) > 1 else 'it comes out'} as at equal speed." if given else ""),
            *([fast_sentence] if fast_sentence else []),
            f"- A gain of a few per cent is within what four sessions can tell apart only when its t-statistic is "
            f"large; base Vigil earns {base['mean_pnl']:,.0f} dollars a session alone, and the differences above "
            "should be read against that.", ""]
    return out


# -- surveillance ---------------------------------------------------------------------------------------

def surveillance(detection: dict) -> list[str]:
    out = ["## Surveillance", "",
           "Each scenario is a full match with a field of honest traders (three Vigil presets and the example "
           "trader) plus one trader, or pair, written to commit one specific abuse (`strategies/badactors.py`). "
           "`clean` is the honest field alone. Every scenario is played on all four sessions.", ""]
    body = []
    total_honest = total_bad = total_checked = missed = 0
    for scenario, by_session in detection.items():
        flagged = sum(bool(o["culprits_flagged"]) for o in by_session.values())
        found = sorted({c for o in by_session.values() for c in o["culprit_controls"]})
        honest = sum(o["honest_alerts"] for o in by_session.values())
        total_honest += honest
        total_bad += sum(o["discrepancies"] for o in by_session.values())
        total_checked += sum(o["trades_checked"] for o in by_session.values())
        missed += 0 if scenario == "clean" else len(by_session) - flagged
        exchange = sorted({status for o in by_session.values() for status in o["exchange"].values()})
        body.append([f"`{scenario}`", "-" if scenario == "clean" else f"{flagged} of {len(by_session)}",
                     ", ".join(found) or "-", honest, ", ".join(exchange) or "-"])
    matches = sum(len(v) for v in detection.values())
    stopped = [scenario for scenario, by_session in detection.items()
               if any(status != "OK" for o in by_session.values() for status in o["exchange"].values())]
    out += [table(["scenario", "sessions in which the culprit was flagged", "by", "alerts against honest teams",
                   "exchange's own verdict on the culprit"], body, "lrlrl"), "",
            f"Across {matches} matches: "
            + ("every culprit was flagged in every session, " if not missed else
               f"a culprit went unflagged in {missed} matches, ")
            + f"{total_honest} alerts were raised against honest teams, and all {total_checked:,} trades the "
            f"exchange reported were reproduced by replaying the order stream ({total_bad} discrepancies).", "",
            "What this does and does not show:", "",
            "- The thresholds in `config/rulebook.yaml` were tuned on these scenarios, and the culprits were "
            "written by the same hand as the detectors. The table shows the controls work as designed on the "
            "behaviour they were designed for; it is not an estimate of detection rates against an adversary "
            "who is trying not to be caught.",
            "- The tests add variations on the culprits: a spoofer that withdraws by amending instead of "
            "cancelling, one that follows each pull with a token order that never rests or rests far away, a "
            "layerer that pulls its layers a quarter of a second apart. Those are caught too. A spoofer that "
            "spaces its withdrawals more than half a second apart, or keeps each under forty lots, is not: "
            "that is where the thresholds are.",
            ("- Only " + listed(stopped) + (" are" if len(stopped) > 1 else " is") + " stopped by the exchange "
             "itself. The other abuses are invisible to it: " if stopped else
             "- The exchange itself stops none of these. They are invisible to it: ")
            + "it has rules about positions and message rates, not about conduct.",
            "- The wash-trading scenario leaves the example trader out of the field. It quotes at fair value, "
            "which is where the colluding pair meet, and it trades with them so often that they rarely "
            "complete a round trip with each other.", ""]
    return out


# -- agreement with the real exchange ---------------------------------------------------------------

def agreement(live: dict) -> list[str]:
    out = ["## Agreement with the real exchange", "",
           "The simulator and the surveillance engine are both checked against files written by the real "
           "exchange (`tests/fixtures/live_capture/`, and any match run with `vigil live`):", "",
           "- Replaying, in the simulator, the order messages two traders sent during a real match reproduces "
           "the exchange's event stream exactly and its positions and fees to the unit "
           "(`tests/test_sim.py::test_replaying_a_real_match_reproduces_it_exactly`). That pins down the "
           "matching and the risk checks. It says nothing about timing, which is the subject of the table "
           "below.",
           "- The fingerprint surveillance computes for an event stream equals the SHA-256 of the "
           "`match_events.csv` the exchange wrote.",
           "- Every trade the real exchange reported is reproduced by the replica's replay of the order stream, "
           "every hedge is at a price the replica's future book gives, and the positions and profit and loss "
           "surveillance derives for each team equal the exchange's score board "
           "(`tests/test_live.py`, `tests/test_exchange_rules.py`).", ""]
    if not live:
        return out
    out += ["### Matches on the real exchange", "",
            "`vigil live` plays a match on the unmodified exchange in real time, one process per trader, and "
            "audits the event file as the exchange writes it. `scripts/record_live_match.py` then reads the "
            "result back from the files the exchange left behind, and plays the same field on the same market "
            "data in the simulator, twice over: once with every trader answering instantly, and once with each "
            "trader given the reaction time measured for it in that match.", ""]
    today = traders_fingerprint()
    for match in live.values():
        teams = match["teams"]
        agree = sum(abs(t["exchange_pnl"] - t["audit_pnl"]) < 0.005 for t in teams.values())
        out += [f"**`{match['match_id']}`**: {match['market_data']}, {match['seconds']:,.0f} seconds, "
                f"{match['events']:,} events. The replica reproduced all {match['trade_reports_checked']:,} trade "
                f"reports and the prices of all {match['hedges_checked']:,} hedges "
                f"({match['discrepancies']} discrepancies), surveillance raised {match['alerts']} alerts, and "
                f"its profit and loss equals the exchange's score board to the cent for {agree} of {len(teams)} teams."
                + (f" {match['note']}" if match.get("note") else ""), ""]
        body = []
        for name, team in sorted(teams.items(), key=lambda item: -item[1]["exchange_pnl"]):
            timed, instant = team["simulated_pnl"], team["simulated_pnl_without_reaction_times"]
            reaction = team["reaction"]
            body.append([name, f"`{team['entered_as']}`", team["status"], f"{team['exchange_pnl']:,.2f}",
                         f"{team['audit_pnl']:,.2f}",
                         f"{mean(timed):,.0f} ± {stdev(timed):,.0f}" if timed else "-",
                         f"{mean(instant):,.0f} ± {stdev(instant):,.0f}" if instant else "-",
                         f"{reaction['median'] * 1000:.2f}" if reaction else "-"])
        out += [table(["team", "entered as", "exchange's verdict", "P&L on the score board", "P&L from the audit",
                       "simulator, measured reaction times", "simulator, instant", "reaction (ms)"],
                      body, "lllrrrrr"), ""]
        if match["simulator_seeds"]:
            def off(key: str, teams=teams) -> float:
                return mean(abs(mean(t[key]) - t["exchange_pnl"]) for t in teams.values() if t[key])
            out += [f"(Simulator columns: mean and standard deviation over {len(match['simulator_seeds'])} seeds.) "
                    f"On average the simulator is {off('simulated_pnl'):,.0f} dollars a team from the real result "
                    f"with the measured reaction times and {off('simulated_pnl_without_reaction_times'):,.0f} "
                    "without them." + simulation_caveat(match, today), ""]
    out += how_close(live, today)
    return out


def spearman(a: list[float], b: list[float]) -> float:
    """Rank correlation of two lists without ties."""
    def ranks(values: list[float]) -> list[int]:
        order = sorted(range(len(values)), key=values.__getitem__)
        return [order.index(i) for i in range(len(values))]
    n, ra, rb = len(a), ranks(a), ranks(b)
    return 1.0 - 6.0 * sum((x - y) ** 2 for x, y in zip(ra, rb, strict=True)) / (n * (n * n - 1))


def how_close(live: dict, today: str) -> list[str]:
    """The simulator against the real exchange, match by match, and what that adds up to."""
    simulated = {key: match for key, match in live.items() if match["simulator_seeds"]}
    if not simulated:
        return []
    columns = {"with": "simulated_pnl", "without": "simulated_pnl_without_reaction_times"}
    body, scores = [], {}
    for key, match in simulated.items():
        teams = list(match["teams"].values())
        real = [team["exchange_pnl"] for team in teams]
        score = scores[key] = {}
        for label, column in columns.items():
            means = [mean(team[column]) for team in teams]
            score[label] = {
                "off": mean(abs(m - r) for m, r in zip(means, real, strict=True)),
                "rank": spearman(means, real),
                "inside": sum(min(team[column]) <= team["exchange_pnl"] <= max(team[column]) for team in teams),
            }
        a, b = score["with"], score["without"]
        body.append([f"`{key}`", len(teams), "yes" if match.get("simulated_as_played") else "no",
                     f"{a['off']:,.0f} / {b['off']:,.0f}", f"{a['rank']:+.2f} / {b['rank']:+.2f}",
                     f"{a['inside']} / {b['inside']}"])
    out = ["### How close the simulator comes", "",
           "Each pair of figures is with the measured reaction times, then without them. \"Inside its range\" "
           "counts the teams whose real result lies between the lowest and the highest the simulator gave them "
           "over its seeds.", "",
           table(["match", "teams", "simulated with the traders that played it",
                  "dollars a team between simulator and exchange", "rank correlation with the real order of finish",
                  "teams whose real result is inside its range"], body, "lrlrrr"), ""]

    outside = [key for key, match in simulated.items() if scores[key]["with"]["inside"] < len(match["teams"])]
    fair = {key: match for key, match in simulated.items() if match.get("simulated_as_played")}
    out += ["What that adds up to:", "",
            "- The simulator is exact about matching and about the exchange's rules. About who reaches the exchange "
            "first it is a model, and not a close one. "
            + (f"In {len(outside)} of {len(simulated)} matches at least one team's real result lies outside "
               "everything the simulator produced for it, so the spread over seeds understates what one real match "
               "can do." if outside else
               "Every team's real result lies within what the simulator produced for it over its seeds.")]
    if fair:
        # Where speeds differ most is where reaction times should matter most.
        def spread(match: dict) -> float:
            times = [team["reaction"]["typical"] for team in match["teams"].values()
                     if team["reaction"] and team["entered_as"] != "example"]
            return max(times) / min(times) if len(times) > 1 else 1.0
        key = max(fair, key=lambda k: spread(fair[k]))
        a, b = scores[key]["with"], scores[key]["without"]
        out += [f"- What the reaction times buy is the order of finish when the traders' speeds really differ. In "
                f"`{key}` the slowest preset took {spread(fair[key]):.1f} times as long to answer as the fastest, and "
                + (f"the simulator ranks the teams as they finished only when it is told so (rank correlation "
                   f"{a['rank']:+.2f} with the reaction times, {b['rank']:+.2f} without)."
                   if a["rank"] > b["rank"] + 0.2 else
                   f"even there they did not help (rank correlation {a['rank']:+.2f} with them, {b['rank']:+.2f} "
                   "without).")]
        even = [k for k in fair if k != key and spread(fair[k]) < 1.3]
        if even:
            out += ["- Where the presets are about equally fast ("
                    + ", ".join(f"`{k}`: rank correlation {scores[k]['with']['rank']:+.2f} with the reaction times, "
                                f"{scores[k]['without']['rank']:+.2f} without" for k in even)
                    + "), the order in which they finish one match is not something the simulator predicts, with "
                    "reaction times or without. The simulated tournament above says as much about itself: the "
                    "ranks of neighbouring teams overlap from seed to seed."]
    one_draw = ("- A live match is one draw, on a desktop machine that was doing other things. Three matches on one "
                "session are enough to show the size of the simulator's error and not enough to correct for it."
                if len(simulated) == 3 and len({m["market_data"] for m in simulated.values()}) == 1 else
                "- A live match is one draw, on a machine that was doing other things.")
    control = control_match(live)
    if control:
        match_id = control[0]
        copies = [team["exchange_pnl"] for team in live[match_id]["teams"].values() if team["entered_as"] == "preset:full"]
        one_draw += (f" How much one draw can say: in `{match_id}` two identical copies of base Vigil, equally fast, "
                     f"ended {abs(copies[0] - copies[1]):,.0f} dollars apart.")
    out += [one_draw, ""]
    return out


def simulation_caveat(match: dict, today: str) -> str:
    """What has to be said about a match's simulator columns: whose traders they were made with."""
    current = match.get("simulated_with") == today
    as_played = match.get("simulated_as_played")
    if as_played is False:
        return (" These columns were computed with a later version of the traders than the one that played the "
                "match, so they do not compare like with like"
                + ("." if current else ", and the traders have changed again since."))
    if as_played is None:
        return " It was not recorded whether the traders changed between this match and its simulation."
    return ("" if current else
            " These columns were computed with the traders that played the match. The traders have changed since, "
            "so the columns cannot be regenerated from the code as it stands.")


def controls() -> str:
    rulebook = load_rulebook()
    out = ["# Controls", "",
           "Generated from `config/rulebook.yaml` by `scripts/render_docs.py`. The rulebook is the single place "
           "where thresholds live; a compliance report records its SHA-256, so a finding can be traced to the "
           "thresholds that produced it.", "",
           f"Rulebook version {rulebook.version}, SHA-256 `{rulebook.sha256}` "
           "(what `shasum -a 256 config/rulebook.yaml` prints).", "",
           "Ready Trader Go is a simulator: nothing traded on it is legally regulated. Each control is modelled "
           "on an obligation that a firm trading a real ETF against a real future would carry, and the mapping "
           "below records which. It is an engineering cross-reference, not legal advice.", ""]
    category = None
    for control in rulebook.controls.values():
        if control.category != category:
            category = control.category
            out += [f"## {category.capitalize()}", ""]
        out += [f"### {control.key}: {control.title}", "", control.description, "",
                f"Severity {control.severity.name}."
                + (" Modelled on: " + ", ".join(control.regulations) + "." if control.regulations else ""), ""]
        if control.parameters:
            out += [table(["parameter", "value"], [[f"`{k}`", f"`{v}`"] for k, v in control.parameters.items()], "ll"),
                    ""]
    out += ["## Regulations", ""]
    for regulation in rulebook.regulations.values():
        used = ", ".join(c.key for c in rulebook.controls_citing(regulation.key)) or "none directly"
        watch = regulation.watch.get("terms")
        out += [f"### {regulation.key}", "",
                f"**{regulation.title}.** {regulation.citation}. {regulation.authority}, {regulation.jurisdiction}. "
                f"<{regulation.url}>", "", regulation.summary, "",
                f"Controls: {used}." + (f" Watched in the Federal Register for: {', '.join(watch)}." if watch else ""),
                ""]
    return "\n".join(out)


if __name__ == "__main__":
    (ROOT / "docs" / "RESULTS.md").write_text(results())
    (ROOT / "docs" / "CONTROLS.md").write_text(controls())
    print("wrote docs/RESULTS.md and docs/CONTROLS.md")
