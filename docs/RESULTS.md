# Results

Everything on this page is generated from the JSON files in `docs/results/`, which the commands below write. To reproduce it:

```shell
vigil train && vigil backtest && vigil calibrate && python scripts/render_docs.py
```

The data is the four sample sessions that ship with Ready Trader Go: fifteen minutes each, about half a million market events per session. Four sessions is a small sample. The figures below are measurements on that sample, not estimates of what would happen on other data.

## Trading strategies

Each row adds one component to the row above it. Every strategy trades each session alone against the recorded market, in the simulator, and the boosted model used on a session was fitted without that session. Each session is played under 3 seeds, which fix the instants at which the exchange publishes its books (they jitter, as on the real exchange), and every strategy is shown the same ones. The trader's orders reach the exchange after the reaction time measured for it in a real match (see [below](#how-fast-each-trader-is)). P&L is in dollars, as scored by the exchange's own accounting, averaged over the seeds.

| strategy | session 1 | session 2 | session 3 | session 4 | mean | vs baseline | vs row above | lots hedged per session |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `baseline` | 31,589 | 37,042 | 38,410 | 33,119 | **35,040** | - | - | 10,904 |
| `inventory` | 32,596 | 38,949 | 37,712 | 33,685 | **35,736** | +2.0% (t = +1.0) | +2.0% (t = +1.0) | 12,067 |
| `mean_reversion` | 33,927 | 42,356 | 41,262 | 35,451 | **38,249** | +9.2% (t = +3.6) | +7.0% (t = +3.4) | 15,316 |
| `kelly` | 46,927 | 52,046 | 45,730 | 38,973 | **45,919** | +31.0% (t = +5.6) | +20.1% (t = +4.6) | 3,512 |
| `momentum` | 47,549 | 53,639 | 45,747 | 39,716 | **46,663** | +33.2% (t = +5.8) | +1.6% (t = +2.8) | 3,309 |
| `full` | 47,488 | 53,329 | 45,894 | 39,675 | **46,597** | +33.0% (t = +5.9) | -0.1% (t = -0.4) | 3,297 |

What the rows are:

- `baseline`: a market maker that quotes one price a side, chosen for expected profit, and hedges every fill in the future at once.
- `inventory`: adds a price on inventory, from a dynamic programme over the position limit (`strategies/inventory.py`).
- `mean_reversion`: adds taking of resting ETF orders that are rich or cheap to the future by more than costs plus a volatility margin.
- `kelly`: replaces hedge-at-once with a Kelly-sized band of un-hedged inventory (`strategies/kelly.py`), capped by the exchange's un-hedged-lots rule.
- `momentum`: adds a one-update forecast from book pressure (microprice), used to time hedges.
- `full`: replaces that forecast with the boosted model's.

**The full strategy earned +33.0% more than the baseline on average (46,597 against 35,040 dollars a session), and more in 12 of 12 runs.** The t-statistics compare two strategies minute by minute over the same market (60 paired session-minutes, each averaged over the seeds). Taking whole sessions as the unit instead, which assumes less and has only 3 degrees of freedom, the full strategy against the baseline gives t = +4.4 and each step over the row above it: `inventory` +1.3, `mean_reversion` +4.5, `kelly` +3.4, `momentum` +2.3, `full` -0.7.

How to read it:

- Most of the gain is the Kelly band (+20.1% over the row above it). It comes from not hedging: lots hedged per session fall from 15,316 to 3,512, and each hedge costs half the future's spread.
- Inventory pricing and mean reversion together add +9.2%.
- The forecast adds little that can be told from noise: +1.6% for the book-pressure rule (t = +2.8) and +1.5% for the boosted model relative to no forecast. It could not add much. It looks one update (a quarter of a second) ahead, and when it is confident enough to act on, the price then moves its way by 0.16 ticks on average, while trading on it would mean crossing a spread of 1.6 ticks. So it is used only where acting is free, to time hedges that were due anyway, and the Kelly band leaves few of those: 3,297 lots a session. Even the whole of the timing gain measured [below](#what-the-forecast-is-worth), 0.06 ticks a lot, would come to about 200 dollars a session, 0.4% of the P&L.

Risk, pooled over all runs:

| strategy | std of P&L per minute | mean / std per minute | worst minute | max drawdown | limit breaches | orders the exchange rejected |
|---|---:|---:|---:|---:|---:|---:|
| `baseline` | 1,031 | 2.27 | -200 | 1,758 | 0 | 0 |
| `inventory` | 941 | 2.53 | 406 | 1,631 | 0 | 0 |
| `mean_reversion` | 1,033 | 2.47 | 316 | 1,577 | 0 | 0 |
| `kelly` | 1,300 | 2.36 | -66 | 1,308 | 0 | 0 |
| `momentum` | 1,310 | 2.37 | -66 | 1,308 | 0 | 0 |
| `full` | 1,306 | 2.38 | -66 | 1,308 | 0 | 0 |

The extra return is not free. Carrying inventory raises the minute-to-minute variability of P&L from 1,031 to 1,306 dollars, and the ratio of mean to standard deviation goes from 2.27 for the baseline to 2.38 for the full strategy. No strategy breached a limit in any run.

### If orders arrive later

How long a trader's order takes to reach the exchange is not something a simulator knows. The table repeats the study with every message delayed by a fixed time on top of the above. A result that holds across it can be believed; one that does not is an artefact of the timing assumed.

| mean P&L | as above | +25 ms | +50 ms |
|---|---:|---:|---:|
| `baseline` | 35,040 | 34,154 | 32,738 |
| `inventory` | 35,736 | 34,759 | 33,322 |
| `mean_reversion` | 38,249 | 36,776 | 35,160 |
| `kelly` | 45,919 | 44,475 | 42,749 |
| `momentum` | 46,663 | 44,989 | 43,139 |
| `full` | 46,597 | 44,859 | 43,124 |
| **full vs baseline** | **+33.0%** | **+31.3%** | **+31.7%** |

And the t-statistic of each step over the row above it:

| step | as above | +25 ms | +50 ms |
|---|---:|---:|---:|
| `inventory` | +1.0 | +0.9 | +0.8 |
| `mean_reversion` | +3.4 | +2.8 | +2.6 |
| `kelly` | +4.6 | +4.8 | +4.8 |
| `momentum` | +2.8 | +2.7 | +2.4 |
| `full` | -0.4 | -0.8 | -0.1 |

The steps that stand up under every assumption (t of 2 or more each time) are `mean_reversion`, `kelly`, `momentum`. `inventory`, `full` do not, and should be read as unproven.

### Under competition

All six presets and the example trader that ships with Ready Trader Go, in the same match, competing for the same customer orders. Each session is played under 8 seeds; with several teams in a match the seed also settles who reaches the exchange first where their reaction times do not.

| team | session 1 | session 2 | session 3 | session 4 | mean | sd over seeds | rank, best to worst seed |
|---|---:|---:|---:|---:|---:|---:|---:|
| `inventory` | 10,322 | 9,373 | 10,376 | 9,765 | **9,959** | 470 | 1 to 2 |
| `momentum` | 11,880 | 9,272 | 8,052 | 8,019 | **9,306** | 408 | 1 to 3 |
| `full` | 11,564 | 8,601 | 9,225 | 7,598 | **9,247** | 534 | 1 to 3 |
| `kelly` | 10,846 | 8,055 | 7,461 | 6,673 | **8,259** | 562 | 4 to 6 |
| `mean_reversion` | 7,021 | 6,935 | 7,593 | 6,348 | **6,974** | 448 | 4 to 6 |
| `baseline` | 6,787 | 6,117 | 7,985 | 6,040 | **6,732** | 734 | 4 to 6 |
| `example (Optiver)` | -9,348 | -12,187 | -10,127 | -10,840 | **-10,625** | 134 | 7 |

With seven teams sharing the flow every P&L is smaller. Only `example (Optiver)` finished in the same place under every seed; where two teams' ranks overlap, the order between them in any one match is luck.

### How fast each trader is

On the real exchange several traders answer the same order book update, and the exchange acts on their orders in the order they arrive: the first to arrive gets the customer. The time from an update being published to a team's first order being acted on was measured in matches played on the real exchange (`vigil.live.reaction_times`), and the simulations above give each trader its measured time.

| trader | median (ms) | spread (ms) | updates answered | measured in |
|---|---:|---:|---:|---|
| `preset:inventory` | 0.70 | 0.27 | 1,789 | `live-tournament-s3-b` |
| `preset:baseline` | 0.75 | 0.29 | 1,343 | `live-tournament-s3-b` |
| `preset:full` | 0.77 | 0.28 | 1,891 | `live-tournament-s3-b` |
| `preset:momentum` | 0.78 | 0.28 | 1,889 | `live-tournament-s3-b` |
| `preset:mean_reversion` | 0.78 | 0.29 | 1,816 | `live-tournament-s3-b` |
| `preset:kelly` | 0.80 | 0.28 | 1,904 | `live-tournament-s3-b` |
| `example` | 0.91 | 0.31 | 2,716 | `live-tournament-s3-b` |

These are times at the exchange, so a trader that arrives second also waits for the first one's orders to be processed. The differences between traders are small next to the update interval and decide who trades all the same.

## The AdaBoost model

The model predicts whether the future's mid price will be higher or lower 1 order book update ahead (about 0.25 s), from 22 features of the book and the trade flow that it found useful (of 25 offered). It is 200 decision stumps boosted with learning rate 0.2, written in numpy (`ml/adaboost.py`) and checked against scikit-learn in the tests.

Accuracy is measured leave-one-session-out: to score a session the model is fitted on the other three, including its probability calibration. "Accuracy" is the share of correct directional calls among updates on which the signal made a call and the price then moved; the same definition is applied to every signal. The books are those a trader is shown, at the instants it is shown them.

| signal | accuracy | range over sessions | AdaBoost relative to it | sessions AdaBoost wins |
|---|---:|---:|---:|---:|
| **AdaBoost** | **58.8%** | 56.9% to 60.3% |  |  |
| momentum: last 4 updates | 48.3% | 47.4% to 48.9% | +21.7% (+10.5 points) | 4 of 4 |
| momentum: above/below EMA16 | 47.1% | 45.9% to 48.1% | +24.9% (+11.7 points) | 4 of 4 |
| mean reversion: fade last move | 54.6% | 54.3% to 55.2% | +7.6% (+4.1 points) | 4 of 4 |
| mean reversion: fade EMA16 gap | 52.9% | 51.9% to 54.1% | +11.0% (+5.8 points) | 4 of 4 |
| order-flow momentum: OFI EMA | 50.6% | 49.5% to 51.4% | +16.1% (+8.2 points) | 4 of 4 |
| book imbalance: top level | 57.9% | 56.1% to 59.3% | +1.5% (+0.9 points) | 4 of 4 |

How to read it:

- Against a price-momentum rule the model is +21.7% better in relative terms (58.8% against 48.3%), ahead in 4 of 4 sessions. Price momentum is worse than a coin here: at this horizon the future mean-reverts.
- Against the best single hand-written signal (book imbalance: top level), the model is +1.5%: 58.8% against 57.9%, ahead in 4 sessions of 4. Whatever the model has found beyond that one signal, it is this much and no more.
- What the model adds over a raw signal is a calibrated probability, which is what the hedge timing uses, and a call on every update (a signal such as imbalance abstains when the book is balanced).

When the model is confident it is right more often:

| predicted probability | accuracy | share of moving updates |
|---|---:|---:|
| at least 0.55 either way | 62.4% | 65.4% |
| at least 0.60 either way | 65.1% | 34.7% |
| at least 0.65 either way | 71.1% | 11.3% |

Feature weights in the model fitted on all four sessions (share of the total vote):

`micro` 13%, `dimb1` 10%, `lask1` 8%, `mr16` 7%, `lbid1` 7%, `ofi` 7%, `wmid5` 6%, `ofi_ema` 6%, `imb1` 6%, `mr64` 5%.

### What the forecast is worth

Accuracy is not what the strategy is paid for. It uses the forecast for one decision: a hedge that is due is put off for one update when the forecast says the future is about to move its way by at least 0.05 ticks. The table prices that decision on the quotes shown at each update, leave-one-session-out, as if a hedge fell due on every update and as often one way as the other. Which updates hedges do fall due on is the strategy's business, so this is what each forecast offers, not what the strategy collects; the backtest above measures that.

| forecast | ticks gained per hedge | range over sessions | share of hedges put off | ticks gained when put off |
|---|---:|---:|---:|---:|
| adaboost | +0.061 | +0.049 to +0.069 | 39% | +0.157 |
| book pressure (microprice) | +0.060 | +0.049 to +0.069 | 39% | +0.152 |
| always wait | -0.000 | -0.000 to +0.000 | 100% | -0.000 |

A hedge pays half the future's spread, 0.78 ticks on average. Timing it on the boosted model's forecast gets 0.061 of a tick back (7.8% of the cost, standard error 0.004). The one-line book-pressure rule gets 0.060, and the boosted model is ahead of it in 2 of 4 sessions: for the decision it is used for, the model is no better than the rule. Putting every hedge off regardless gets -0.000, which is the check that the gain comes from the forecast and not from the accounting.

## Challengers: experimental versions of Vigil

Five versions of the trader each replace one part of base Vigil (the `full` strategy) with a technique from the market-making literature, so that a difference in result can be put down to that part. Two more, `skew_fast` and `rl_fast`, are `skew` and `rl` rebuilt to send their orders as quickly as base Vigil does. Each can be entered as a team, `preset:<name>`, like any preset (`strategies/challengers/`).

- `skew`: forecast-skewed quoting. Quotes are priced around where the boosted model says the future is heading, not where it is (Cartea, Jaimungal & Penalva, *Algorithmic and High-Frequency Trading*, ch. 10).
- `glft`: optimal quoting with fitted fill rates. Quote distances come from the closed-form solution of Guéant, Lehalle & Fernandez-Tapia (2013) for a market maker with a position limit, with the rate at which customer orders reach each price fitted to the sessions.
- `ou`: mean-reversion carrying. The future's deviation from its recent average is modelled as an Ornstein-Uhlenbeck process, and the inventory Vigil carries leans the way the fitted model expects the price to go: back towards the average if it reverts, onwards if it runs on.
- `hawkes`: a self-exciting (Hawkes) model of the future's aggressive order flow, which comes in bursts. Expected bursts put the quotes further out and make them smaller, and shrink the inventory carried.
- `rl`: reinforcement learning. A tabular Q-learning policy, trained in the simulator, moves base Vigil's quotes a tick either way or withdraws them.
- `skew_fast`: forecast-skewed quoting at base Vigil's speed. Quotes are skewed by book pressure, how far the future's touch price weighted by size sits from its mid, which is worked out from the future's book before the ETF's arrives; `skew` waits for the boosted model instead.
- `rl_fast`: `rl`'s policy with its deciding done before the update arrives: the choice for the state it is in is worked out whenever that state changes, and on the update only looked up. It trades as `rl` does, event for event.

Whatever a challenger fits from data was fitted leave-one-session-out (`vigil challenge fit`): to trade a session it uses parameters fitted to the other three, and the one tuning knob each has was chosen by what it earned trading those three. The boosted model it trades with never saw the session either. Their reaction times were measured on the real exchange, in one match with base Vigil; `glft`, `ou` and `hawkes` were not in it and are given base Vigil's, from whose handler they send their first orders.

What was fitted, for trading each session:

| held out | skew: weight | glft: risk aversion | ou: weight | hawkes: weight | rl: P&L in the last tenth of training | skew_fast: weight |
|---|---|---|---|---|---|---|
| session 1 | 4 | 0.0005 (A 23.5, k 0.073, R² 0.997) | 0.5 (reverting, half-life 116 updates, c +0.52) | 0.5 (branching 0.52, corr. with 10 s variance 0.21) | 45,891 | 1 |
| session 2 | 2 | 0.0005 (A 23.0, k 0.069, R² 0.998) | 0.25 (runs on, c -0.26) | 2 (branching 0.37, corr. with 10 s variance 0.17) | 44,474 | 0.5 |
| session 3 | 0.5 | 0.0001 (A 23.5, k 0.072, R² 0.995) | 0.5 (runs on, c -0.58) | 1 (branching 0.51, corr. with 10 s variance 0.20) | 46,504 | 0.5 |
| session 4 | 2 | 0.0005 (A 23.4, k 0.069, R² 0.996) | 0.5 (runs on, c -0.77) | 0.5 (branching 0.49, corr. with 10 s variance 0.25) | 48,385 | 0.5 |

`rl_fast` plays the policy fitted for `rl`.

Fitted to each session on its own, the future runs on in session 1 and reverts in the other three. Where it runs on it does so strongly enough to decide the sign of every fit that includes it, so the model trading each of the 4 sessions expects the opposite of what that session does: four sessions are too few for this model to be judged on.

### Alone

Each version trades each session by itself against the recorded market, under 3 seeds, every version under the same ones; compared with base Vigil minute by minute (the t-statistic) and session by session.

| version | session 1 | session 2 | session 3 | session 4 | mean | vs base Vigil | t over sessions | runs ahead | limit breaches |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `full` (base Vigil) | 47,630 | 53,329 | 45,827 | 39,666 | **46,613** | - | - | - | 0 |
| `skew` | 47,029 | 53,867 | 45,051 | 41,321 | **46,817** | +0.4% (t = +0.4) | +0.4 | 9 of 12 | 0 |
| `glft` | 35,185 | 44,260 | 36,987 | 37,581 | **38,503** | -17.4% (t = -7.1) | -3.7 | 0 of 12 | 0 |
| `ou` | 42,893 | 53,403 | 45,151 | 39,028 | **45,119** | -3.2% (t = -2.0) | -1.4 | 2 of 12 | 0 |
| `hawkes` | 48,791 | 50,690 | 45,105 | 39,677 | **46,066** | -1.2% (t = -1.4) | -0.7 | 5 of 12 | 0 |
| `rl` | 47,583 | 53,334 | 45,738 | 39,647 | **46,576** | -0.1% (t = -0.8) | -1.9 | 3 of 12 | 0 |
| `skew_fast` | 45,205 | 53,899 | 45,789 | 39,981 | **46,219** | -0.8% (t = -1.0) | -0.6 | 5 of 12 | 0 |
| `rl_fast` | 47,252 | 53,332 | 45,827 | 39,673 | **46,521** | -0.2% (t = -1.5) | -1.0 | 3 of 12 | 0 |

`rl`'s policy quotes otherwise than base Vigil on 10 updates a session, of some 3,600 (four a second for fifteen minutes): safe policy improvement keeps base Vigil's choice wherever the training gave too little evidence against it, and four sessions give little.

### Head to head

Base Vigil and one challenger in the same match, competing for the same customers: every session under 8 seeds. In a match between two teams, who reaches the exchange first moves either side's result by a few thousand dollars, so each match is played twice, the challenger in each team slot once, and the pair averaged: luck then falls to each side equally, and two copies of base Vigil come out exactly level. The t-statistic is the mean difference over its standard error with the session as the unit; four sessions are few to estimate that from, so it is never taken as smaller than the luck between seeds within a session implies.

Each comparison is made twice. With the challenger as quick to answer the exchange as base Vigil (the strategy alone), and at the speeds measured for each in a match on the real exchange (base Vigil's reaction there: 0.80 ms), which adds what the design costs in speed. Differences are the challenger's P&L less base Vigil's, in dollars, averaged over the pairs.

| challenger | reaction (ms) | equal speed: difference | t over sessions | pairs won–level–lost | measured speed: difference | t over sessions | pairs won–level–lost |
|---|---:|---:|---:|---:|---:|---:|---:|
| `skew` | 1.20 | -114 | -0.1 | 16–0–16 | -13,884 | -15.6 | 0–0–32 |
| `glft` | 0.80 (base Vigil's) | -24,378 | -17.9 | 0–0–32 | -24,378 | -17.9 | 0–0–32 |
| `ou` | 0.80 (base Vigil's) | -1,442 | -1.2 | 13–0–19 | -1,442 | -1.2 | 13–0–19 |
| `hawkes` | 0.80 (base Vigil's) | -883 | -1.3 | 14–0–18 | -883 | -1.3 | 14–0–18 |
| `rl` | 1.04 | -207 | -0.8 | 3–24–5 | -11,191 | -23.3 | 0–0–32 |
| `skew_fast` | 0.81 | +121 | +0.3 | 15–0–17 | -199 | -0.4 | 14–0–18 |
| `rl_fast` | 0.83 | -207 | -0.8 | 3–24–5 | -1,052 | -3.2 | 11–0–21 |

### All in one match

Base Vigil, every challenger, the example trader and a second copy of base Vigil in the same match, every session under 20 seeds; the teams move round the slots from seed to seed, so each has each slot's luck the same number of times. Each team's P&L is compared with base Vigil's in the same match. The second copy is the control: it trades exactly as base Vigil does, so what separates the two is luck, and a challenger's difference from base Vigil means something only when it is clearly larger. Mean P&L per match in dollars.

| team | equal speed: mean | less base Vigil | t over sessions | measured speed: mean | less base Vigil | t over sessions |
|---|---:|---:|---:|---:|---:|---:|
| `skew` | **7,878** | +1,050 | +4.0 | 2,875 | -5,410 | -7.9 |
| `skew_fast` | **7,278** | +449 | +2.0 | 8,037 | -248 | -0.9 |
| `hawkes` | **6,903** | +75 | +0.3 | 8,310 | +25 | +0.1 |
| `rl` | **6,868** | +40 | +0.1 | 2,957 | -5,328 | -13.1 |
| `full` (base Vigil) | **6,828** | - | - | 8,285 | - | - |
| `full-copy` (base Vigil again: the control) | **6,656** | -172 | -0.8 | 8,105 | -179 | -0.8 |
| `rl_fast` | **6,627** | -202 | -0.8 | 7,341 | -944 | -3.0 |
| `ou` | **6,033** | -795 | -1.7 | 7,769 | -516 | -0.7 |
| `glft` | **3,562** | -3,266 | -6.8 | 4,174 | -4,111 | -7.7 |
| `example (Optiver)` | **-12,723** | -19,551 | -36.3 | -12,217 | -20,502 | -26.0 |

How to read it:

- Alone, no challenger earned more than base Vigil by a margin that holds both minute by minute and session by session; `glft` earned clearly less (t of -2 or below).
- Head to head at equal speed, no challenger beat base Vigil consistently across sessions; base Vigil beat `glft` consistently.
- In a field at equal speed, the second copy of base Vigil ended 172 dollars a match behind the first (t = -0.8), which is luck. `ou`, `hawkes`, `rl` and `rl_fast` ended between -795 and +75 dollars a match against base Vigil, none with a t of 2 or more: no more distinguishable from it than its own copy; `skew` and `skew_fast` ended ahead of it with a t of 2 or more (+1,050, +449), though not head to head: what a trader earns in a field depends on who else is in it; `glft` clearly behind it.
- At measured speeds, `skew` and `rl` reach the exchange later than base Vigil, and against base Vigil that alone decides most matches. They do more before they send (`skew` waits for the forecast before it quotes; `rl` consults its policy). That is microseconds, but between traders that see the same update at the same moment it decides who is served first (ARCHITECTURE.md, "Getting there first"). In the match the reaction times come from (`fast-live-s1`, below), base Vigil's copy reached the exchange ahead of it on 49 per cent of the updates both answered, `rl` and `skew` on 25 and 23: the delay is the code's, not the machine's. The simulator gives a trader one reaction time an update, so at measured speed it delays `skew`'s takes, which leave before the forecast is worked out, with its quotes, and overstates what its speed costs: in each of the 3 matches `skew` played on the real exchange it earned more than the simulator gave it, by 2,518 to 6,069 dollars. `skew_fast` and `rl_fast` were ahead of base Vigil about as often as behind it, so their results at measured speeds say nothing about their speed; that the hundredths of a millisecond between the times measured for them and base Vigil's change a simulated duel's result by as much as 845 dollars shows how much these results hang on who is first. `glft`, `ou` and `hawkes` were not in that match and are given base Vigil's time, so at measured speed they come out as at equal speed.
- `rl_fast` and `skew_fast` were rebuilt to send as quickly as base Vigil, and on the real exchange they did: in `fast-live-s1` they reached the exchange ahead of base Vigil on 48 and 48 per cent of the updates both answered, as its copy did on 49. As quick, they are level with base Vigil head to head: `skew_fast` +121 dollars a match (t = +0.3); `rl_fast` -207 dollars a match (t = -0.8). `rl_fast` makes exactly the decisions `rl` makes, and `rl` departs from base Vigil's so seldom that the two trade alike.
- A gain of a few per cent is within what four sessions can tell apart only when its t-statistic is large; base Vigil earns 46,613 dollars a session alone, and the differences above should be read against that.

## Surveillance

Each scenario is a full match with a field of honest traders (three Vigil presets and the example trader) plus one trader, or pair, written to commit one specific abuse (`strategies/badactors.py`). `clean` is the honest field alone. Every scenario is played on all four sessions.

| scenario | sessions in which the culprit was flagged | by | alerts against honest teams | exchange's own verdict on the culprit |
|---|---:|---|---:|---|
| `clean` | - | - | 0 | - |
| `wash` | 4 of 4 | MKT-02, MKT-04 | 0 | - |
| `spoofer` | 4 of 4 | MKT-01 | 0 | - |
| `layerer` | 4 of 4 | MKT-01 | 0 | - |
| `stuffer` | 4 of 4 | LIM-03, MKT-03, MKT-05 | 0 | - |
| `close_marker` | 4 of 4 | MKT-04 | 0 | - |
| `reckless` | 4 of 4 | LIM-02 | 0 | BREACH |
| `limit_breaker` | 4 of 4 | LIM-01, LIM-02 | 0 | BREACH |
| `fat_finger` | 4 of 4 | MKT-06 | 0 | - |

Across 36 matches: every culprit was flagged in every session, 0 alerts were raised against honest teams, and all 85,497 trades the exchange reported were reproduced by replaying the order stream (0 discrepancies).

What this does and does not show:

- The thresholds in `config/rulebook.yaml` were tuned on these scenarios, and the culprits were written by the same hand as the detectors. The table shows the controls work as designed on the behaviour they were designed for; it is not an estimate of detection rates against an adversary who is trying not to be caught.
- The tests add variations on the culprits: a spoofer that withdraws by amending instead of cancelling, one that follows each pull with a token order that never rests or rests far away, a layerer that pulls its layers a quarter of a second apart. Those are caught too. A spoofer that spaces its withdrawals more than half a second apart, or keeps each under forty lots, is not: that is where the thresholds are.
- Only `reckless` and `limit_breaker` are stopped by the exchange itself. The other abuses are invisible to it: it has rules about positions and message rates, not about conduct.
- The wash-trading scenario leaves the example trader out of the field. It quotes at fair value, which is where the colluding pair meet, and it trades with them so often that they rarely complete a round trip with each other.

## Agreement with the real exchange

The simulator and the surveillance engine are both checked against files written by the real exchange (`tests/fixtures/live_capture/`, and any match run with `vigil live`):

- Replaying, in the simulator, the order messages two traders sent during a real match reproduces the exchange's event stream exactly and its positions and fees to the unit (`tests/test_sim.py::test_replaying_a_real_match_reproduces_it_exactly`). That pins down the matching and the risk checks. It says nothing about timing, which is the subject of the table below.
- The fingerprint surveillance computes for an event stream equals the SHA-256 of the `match_events.csv` the exchange wrote.
- Every trade the real exchange reported is reproduced by the replica's replay of the order stream, every hedge is at a price the replica's future book gives, and the positions and profit and loss surveillance derives for each team equal the exchange's score board (`tests/test_live.py`, `tests/test_exchange_rules.py`).

### Matches on the real exchange

`vigil live` plays a match on the unmodified exchange in real time, one process per trader, and audits the event file as the exchange writes it. `scripts/record_live_match.py` then reads the result back from the files the exchange left behind, and plays the same field on the same market data in the simulator, twice over: once with every trader answering instantly, and once with each trader given the reaction time measured for it in that match.

**`full-live-s3`**: market_data3.csv, 900 seconds, 467,410 events. The replica reproduced all 2,397 trade reports and the prices of all 2,034 hedges (0 discrepancies), surveillance raised 0 alerts, and its profit and loss equals the exchange's score board to the cent for 4 of 4 teams. Played on a desktop machine with other applications running (load average 15 to 20 on its 12 cores).

| team | entered as | exchange's verdict | P&L on the score board | P&L from the audit | simulator, measured reaction times | simulator, instant | reaction (ms) |
|---|---|---|---:|---:|---:|---:|---:|
| VigilKelly | `preset:kelly` | OK | 17,325.52 | 17,325.52 | 16,633 ± 1,065 | 15,558 ± 1,290 | 0.80 |
| MeanRev | `preset:mean_reversion` | OK | 15,857.40 | 15,857.40 | 16,872 ± 1,065 | 14,715 ± 1,202 | 0.79 |
| VigilFull | `preset:full` | OK | 12,088.78 | 12,088.78 | 10,008 ± 1,228 | 15,034 ± 756 | 0.84 |
| Example | `example` | OK | -3,894.73 | -3,894.73 | -4,133 ± 268 | -6,416 ± 232 | 0.53 |

(Simulator columns: mean and standard deviation over 8 seeds.) On average the simulator is 1,007 dollars a team from the real result with the measured reaction times and 2,094 without them. These columns were computed with a later version of the traders than the one that played the match, so they do not compare like with like, and the traders have changed again since.

**`live-tournament-s3`**: market_data3.csv, 900 seconds, 481,605 events. The replica reproduced all 2,800 trade reports and the prices of all 2,522 hedges (0 discrepancies), surveillance raised 0 alerts, and its profit and loss equals the exchange's score board to the cent for 7 of 7 teams. Played on a desktop machine with other applications running (load average 30 to 38 on its 12 cores, the seven traders included: each polls the exchange's shared memory without pause). Audited as it ran, under the rulebook as it then stood, this match raised one alert against an honest team: the full strategy's order-to-trade ratio was 107 by volume against a limit of 80. The limit was moved afterwards (see control MKT-05), and the figures here are the audit under the rulebook as it stands now.

| team | entered as | exchange's verdict | P&L on the score board | P&L from the audit | simulator, measured reaction times | simulator, instant | reaction (ms) |
|---|---|---|---:|---:|---:|---:|---:|
| baseline | `preset:baseline` | OK | 10,571.08 | 10,571.08 | 10,709 ± 1,102 | 7,280 ± 872 | 0.66 |
| inventory | `preset:inventory` | OK | 9,612.99 | 9,612.99 | 14,184 ± 1,008 | 9,351 ± 1,353 | 0.61 |
| momentum | `preset:momentum` | OK | 8,945.33 | 8,945.33 | 6,437 ± 867 | 8,198 ± 1,569 | 0.94 |
| kelly | `preset:kelly` | OK | 8,438.48 | 8,438.48 | 6,173 ± 996 | 7,635 ± 1,396 | 0.96 |
| mean_reversion | `preset:mean_reversion` | OK | 8,347.05 | 8,347.05 | 6,224 ± 933 | 8,192 ± 950 | 0.94 |
| full | `preset:full` | OK | 5,621.27 | 5,621.27 | 3,862 ± 881 | 8,470 ± 886 | 1.04 |
| example | `example` | OK | -5,099.04 | -5,099.04 | -5,106 ± 303 | -6,364 ± 353 | 0.69 |

(Simulator columns: mean and standard deviation over 8 seeds.) On average the simulator is 1,910 dollars a team from the real result with the measured reaction times and 1,339 without them. These columns were computed with the traders that played the match. The traders have changed since, so the columns cannot be regenerated from the code as it stands.

**`live-tournament-s3-b`**: market_data3.csv, 900 seconds, 480,934 events. The replica reproduced all 3,099 trade reports and the prices of all 2,576 hedges (0 discrepancies), surveillance raised 0 alerts, and its profit and loss equals the exchange's score board to the cent for 7 of 7 teams. The same field on the same session and machine (load average 21 to 27) after the traders' handlers were reordered to send the orders that race before working out anything else.

| team | entered as | exchange's verdict | P&L on the score board | P&L from the audit | simulator, measured reaction times | simulator, instant | reaction (ms) |
|---|---|---|---:|---:|---:|---:|---:|
| kelly | `preset:kelly` | OK | 11,008.40 | 11,008.40 | 7,012 ± 958 | 8,182 ± 1,515 | 0.80 |
| momentum | `preset:momentum` | OK | 10,276.22 | 10,276.22 | 9,206 ± 1,295 | 8,588 ± 2,216 | 0.78 |
| inventory | `preset:inventory` | OK | 10,091.85 | 10,091.85 | 11,284 ± 2,086 | 9,786 ± 1,095 | 0.70 |
| full | `preset:full` | OK | 8,737.71 | 8,737.71 | 7,725 ± 1,564 | 9,086 ± 817 | 0.77 |
| mean_reversion | `preset:mean_reversion` | OK | 7,786.87 | 7,786.87 | 7,547 ± 628 | 7,995 ± 1,118 | 0.78 |
| baseline | `preset:baseline` | OK | 6,270.09 | 6,270.09 | 7,716 ± 971 | 6,925 ± 626 | 0.75 |
| example | `example` | OK | -9,033.33 | -9,033.33 | -10,080 ± 367 | -6,412 ± 391 | 0.91 |

(Simulator columns: mean and standard deviation over 8 seeds.) On average the simulator is 1,429 dollars a team from the real result with the measured reaction times and 1,236 without them. These columns were computed with the traders that played the match. The traders have changed since, so the columns cannot be regenerated from the code as it stands.

**`challengers-live-s4`**: market_data4.csv, 900 seconds, 522,054 events. The replica reproduced all 3,984 trade reports and the prices of all 3,007 hedges (0 discrepancies), surveillance raised 0 alerts, and its profit and loss equals the exchange's score board to the cent for 7 of 7 teams. Base Vigil, the five challengers and the example trader, on session 4, which no live match had used before; the same desktop machine, with other applications running (load average 8 to 14).

| team | entered as | exchange's verdict | P&L on the score board | P&L from the audit | simulator, measured reaction times | simulator, instant | reaction (ms) |
|---|---|---|---:|---:|---:|---:|---:|
| hawkes | `preset:hawkes` | OK | 10,764.33 | 10,764.33 | 10,057 ± 821 | 8,687 ± 1,362 | 0.83 |
| ou | `preset:ou` | OK | 9,375.23 | 9,375.23 | 12,028 ± 1,482 | 8,303 ± 1,496 | 0.79 |
| base | `preset:full` | OK | 7,991.99 | 7,991.99 | 11,056 ± 1,521 | 7,941 ± 1,069 | 0.81 |
| skew | `preset:skew` | OK | 6,983.32 | 6,983.32 | 4,466 ± 898 | 9,230 ± 803 | 1.04 |
| rl | `preset:rl` | OK | 6,214.71 | 6,214.71 | 4,740 ± 1,523 | 7,933 ± 881 | 1.01 |
| glft | `preset:glft` | OK | 3,297.11 | 3,297.11 | 4,683 ± 832 | 3,380 ± 674 | 0.71 |
| example | `example` | OK | -11,934.90 | -11,934.90 | -12,009 ± 306 | -8,484 ± 333 | 0.98 |

(Simulator columns: mean and standard deviation over 8 seeds.) On average the simulator is 1,697 dollars a team from the real result with the measured reaction times and 1,529 without them. These columns were computed with the traders that played the match. The traders have changed since, so the columns cannot be regenerated from the code as it stands.

**`control-live-s2`**: market_data2.csv, 900 seconds, 650,641 events. The replica reproduced all 3,550 trade reports and the prices of all 2,690 hedges (0 discrepancies), surveillance raised 0 alerts, and its profit and loss equals the exchange's score board to the cent for 5 of 5 teams. A control: two identical copies of base Vigil (base and copy) beside rl, skew and the example trader, on session 2, which no live match had used before. The copies answered the exchange equally fast, each ahead of the other on half the updates both answered, while rl and skew were behind base Vigil on four updates in five; the same desktop machine (load average 4 to 8).

| team | entered as | exchange's verdict | P&L on the score board | P&L from the audit | simulator, measured reaction times | simulator, instant | reaction (ms) |
|---|---|---|---:|---:|---:|---:|---:|
| base | `preset:full` | OK | 17,873.91 | 17,873.91 | 19,415 ± 2,419 | 13,053 ± 899 | 0.62 |
| copy | `preset:full` | OK | 15,328.35 | 15,328.35 | 20,306 ± 1,476 | 13,695 ± 2,101 | 0.62 |
| skew | `preset:skew` | OK | 12,502.27 | 12,502.27 | 6,433 ± 800 | 11,573 ± 1,817 | 0.89 |
| rl | `preset:rl` | OK | 10,256.00 | 10,256.00 | 8,035 ± 1,282 | 12,388 ± 1,044 | 0.79 |
| example | `example` | OK | -12,697.54 | -12,697.54 | -12,462 ± 364 | -8,117 ± 278 | 0.82 |

(Simulator columns: mean and standard deviation over 8 seeds.) On average the simulator is 3,009 dollars a team from the real result with the measured reaction times and 2,819 without them. These columns were computed with the traders that played the match. The traders have changed since, so the columns cannot be regenerated from the code as it stands.

**`fast-live-s1`**: market_data1.csv, 900 seconds, 504,825 events. The replica reproduced all 3,224 trade reports and the prices of all 2,597 hedges (0 discrepancies), surveillance raised 0 alerts, and its profit and loss equals the exchange's score board to the cent for 7 of 7 teams. rl_fast and skew_fast, rebuilt to send as quickly as base Vigil, beside the versions they replace, base Vigil, a second copy of base Vigil as a control and the example trader, on session 1, which no live match had used before. Both fast versions were ahead of base Vigil on about half the updates both answered, as the copy was; rl and skew on a quarter. The same desktop machine, busier than before (load average 15 to 26).

| team | entered as | exchange's verdict | P&L on the score board | P&L from the audit | simulator, measured reaction times | simulator, instant | reaction (ms) |
|---|---|---|---:|---:|---:|---:|---:|
| skew | `preset:skew` | OK | 13,312.27 | 13,312.27 | 7,288 ± 1,685 | 12,428 ± 1,060 | 1.07 |
| rl_fast | `preset:rl_fast` | OK | 13,281.51 | 13,281.51 | 13,764 ± 1,130 | 11,498 ± 2,084 | 0.90 |
| skew_fast | `preset:skew_fast` | OK | 12,905.59 | 12,905.59 | 15,359 ± 1,577 | 11,850 ± 1,757 | 0.89 |
| copy | `preset:full` | OK | 12,240.08 | 12,240.08 | 13,410 ± 1,417 | 11,645 ± 1,309 | 0.90 |
| base | `preset:full` | OK | 10,619.43 | 10,619.43 | 15,058 ± 2,170 | 11,250 ± 1,426 | 0.89 |
| rl | `preset:rl` | OK | 7,391.74 | 7,391.74 | 7,810 ± 1,135 | 11,549 ± 1,038 | 1.06 |
| example | `example` | OK | -10,416.29 | -10,416.29 | -9,878 ± 192 | -7,083 ± 209 | 1.05 |

(Simulator columns: mean and standard deviation over 8 seeds.) On average the simulator is 2,218 dollars a team from the real result with the measured reaction times and 1,777 without them.

### How close the simulator comes

Each pair of figures is with the measured reaction times, then without them. "Inside its range" counts the teams whose real result lies between the lowest and the highest the simulator gave them over its seeds.

| match | teams | simulated with the traders that played it | dollars a team between simulator and exchange | rank correlation with the real order of finish | teams whose real result is inside its range |
|---|---:|---|---:|---:|---:|
| `full-live-s3` | 4 | no | 1,007 / 2,094 | +0.80 / +0.80 | 3 / 2 |
| `live-tournament-s3` | 7 | yes | 1,910 / 1,339 | +0.93 / +0.21 | 2 / 4 |
| `live-tournament-s3-b` | 7 | yes | 1,429 / 1,236 | +0.39 / +0.68 | 4 / 5 |
| `challengers-live-s4` | 7 | yes | 1,697 / 1,529 | +0.79 / +0.79 | 3 / 3 |
| `control-live-s2` | 5 | yes | 3,009 / 2,819 | +0.80 / +0.80 | 2 / 2 |
| `fast-live-s1` | 7 | yes | 2,218 / 1,777 | +0.29 / +0.71 | 4 / 5 |

What that adds up to:

- The simulator is exact about matching and about the exchange's rules. About who reaches the exchange first it is a model, and not a close one. In 6 of 6 matches at least one team's real result lies outside everything the simulator produced for it, so the spread over seeds understates what one real match can do.
- What the reaction times buy is the order of finish when the traders' speeds really differ. In `live-tournament-s3` the slowest preset took 2.0 times as long to answer as the fastest, and the simulator ranks the teams as they finished only when it is told so (rank correlation +0.93 with the reaction times, +0.21 without).
- Where the presets are about equally fast (`live-tournament-s3-b`: rank correlation +0.39 with the reaction times, +0.68 without), the order in which they finish one match is not something the simulator predicts, with reaction times or without. The simulated tournament above says as much about itself: the ranks of neighbouring teams overlap from seed to seed.
- A live match is one draw, on a machine that was doing other things. How much one draw can say: in `fast-live-s1` two identical copies of base Vigil, equally fast, ended 1,621 dollars apart.
