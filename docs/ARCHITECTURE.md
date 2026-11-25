# Architecture

How Vigil is put together, and why. Start with the [README](../README.md) for a runnable example.
For the surveillance pipeline, jump to [surveillance](#surveillance-surveillance),
[the bus](#the-bus-bus), and [reports and anchoring](#reports-and-anchoring-reports-chain-contracts).
The simulator and strategy sections describe the research tools that supply events to that pipeline.
Ready Trader Go is a separately installed dependency; see [attribution](../NOTICE.md).

## The one data shape

A Ready Trader Go match is a stream of events: an order was inserted, amended or cancelled; a
competitor's order traded; a competitor hedged in the future. The exchange writes them to
`match_events.csv` in the order it processed them, the replayed market's own orders included (under an
empty team name). `rtg/events.py` defines `MatchRecord`, a faithful copy of one such event plus its
sequence number, and every stage of the pipeline consumes or produces a stream of them.

The stream is lossless in a stronger sense than it looks. Matching is deterministic, so the order
events alone determine the state of both order books at every instant and every trade with both of
its counterparties. The exchange only reports a competitor's own fills; the rest is recoverable by
replay, and the surveillance engine recovers it.

Where a stream comes from does not matter to what consumes it:

| Source | How |
|---|---|
| the simulator | `Simulator.run()` yields the events of a simulated match |
| a finished match | `read_match_events()` reads the exchange's CSV |
| a running match | `LiveMatch.events()` follows the CSV as the exchange writes it |
| Kafka | the monitor consumes `rtg.match-events` |

## The simulator (`sim/`)

The real exchange runs on the wall clock: fifteen minutes per match, and never quite the same match
twice. Research needs the opposite, so the simulator replaces the clock and keeps everything else.

Everything else is literal. `Simulator` constructs the exchange's own `OrderBook`, `CompetitorManager`,
`Competitor`, `CompetitorAccount`, `MarketEventsReader`, `FrequencyLimiter` and `UnhedgedLots` objects
and drives them from a priority queue of simulated instants instead of from asyncio timers. Traders
are ordinary `BaseAutoTrader` subclasses. What they send is decoded from the wire format they would
have put on a socket, and what the exchange sends back reaches them through the same callbacks with
the same arguments. Three details make this work without touching the vendored code:

- `MarketEventsReader` takes its events from a queue filled by a reader thread. The simulator swaps
  the queue for an iterator over pre-parsed events.
- `UnhedgedLots` schedules its sixty-second timer on `asyncio.get_running_loop()`. For the duration
  of a run the simulator registers its own loop object (simulated `time`, `call_later`) as the running
  loop, so the exchange's timer code runs unmodified on simulated time.
- Messages from the exchange to a trader are queued and delivered when the exchange has finished the
  step it is in, as they would be over a socket. A trader cannot react in the middle of a match.

**Timing** is the part that has to be modelled, and it matters more than it looks.

The exchange runs two timers. One applies market data, every 0.05 s; the other publishes the order
books and scores, every 0.25 s; each fires within a fifth of its interval of when it is due. The
publishing timer does not bring the market data up to date first. So the books a trader is shown are
whatever the other timer, or the last message from any trader, left them: up to a twentieth of a
second old. The simulator has both timers, with the same jitter drawn from a seeded generator, and
publishes stale books as the exchange does. An earlier version applied market data up to the instant
of each tick, which showed traders a fresher market than they ever really see.

What a trader does with what it is shown takes time too. Several traders answer the same update, the
exchange acts on their orders in the order they arrive, and the first to arrive gets the customer.
Ten microseconds between two handlers decide that nine times in ten: in the first tournament played
on the real exchange, the two quickest presets were ahead of the slowest on more than nine in ten of
the updates both answered. `add_trader(..., reaction=(typical, spread))` gives a
simulated trader a reaction time; `vigil.live.reaction_times` measures them from a match played on
the real exchange, and the backtests use what was measured (`docs/results/reaction_times.json`).
Without them every trader answers instantly and the order among them is a coin flip, which flatters
whoever is really slowest.

That luck is large. Two copies of base Vigil in one simulated match typically end it two or three
thousand dollars apart (the standard deviation of the difference), by who happened to reach the
exchange first; on the real exchange, in a control match, two copies ended 2,546 dollars apart. Each
team's draws come from a stream of its own, seeded by the seed, the session and the team's slot;
seeding by the seed alone, as an earlier version did, gave a team the same luck in every session and
made a lucky name look like a better trader. Comparisons between teams therefore pair matches: a
duel is played twice, each team in each slot once, and in a field the teams move round the slots
from seed to seed.

`ExchangeConfig(latency=...)` adds a fixed delay to every trader on top, for asking how much a result
depends on all this. The answer is in [RESULTS.md](RESULTS.md).

**How do we know it is faithful?** About matching and the exchange's rules, exactly:
`tests/test_sim.py` takes a match played on the real exchange by two traders, extracts the order
messages they sent and the instants at which the exchange processed them, and has scripted traders
re-send exactly those in the simulator. The resulting event stream is identical to the real one,
event for event, and positions and fees agree to the unit.

About timing it is a model, and the evidence for the model is mixed. Fields played on the real
exchange are played again in the simulator, with their measured reaction times and without, and the
results are set side by side in RESULTS.md. Where the traders' speeds really differed, the reaction
times are what make the simulator rank the teams as they finished. In dollars it stays one to two
thousand a team from the real result, in matches where a team makes five to seventeen thousand,
with them or without; and in every match some team's real result lies outside everything the
simulator produced for it. A simulated match between several teams is a guide to what kind of
strategy does well. It is not a forecast of a result.

## The trading strategy (`strategies/`)

### How money is made on this exchange

The ETF's book holds a standing market of 10,000 lots a side about six ticks either side of the
future, refreshed several times a second, and a stream of customer orders (about two a second, 46 lots
on average) whose limit prices are scattered up to thirty ticks either side of fair value. Half of
them are therefore marketable. A customer order trades with whoever shows the best price; whatever is
not filled rests in the book at its limit, often at a price that is wrong.

So there are two ways to earn: show a better price than the standing market and be hit (market
making), and take resting orders that are rich or cheap (mean reversion). Either way the fill is laid
off in the future, which costs half the future's spread: about three quarters of a tick on average.

The binding constraint is not capital but the position limit of 100 lots. With customer orders
averaging 46 lots the position is at a limit much of the time, and a side at its limit earns nothing.
Most of what follows is about using that scarce capacity well.

### The components

`ensemble.py` builds the strategy from parts that can each be switched off, which is how their
contributions are measured (`PRESETS`, `vigil backtest`).

**Quoting.** One quote a side. Its price is chosen each update by scoring every price from the
tightest acceptable to just inside the standing market: the probability that a customer order reaches
that price, times the share of it left over by the volume queued ahead, times the edge over the hedge
price. With nobody ahead, the widest price wins; when resting orders or competitors are in the way,
the score moves the quote in front of them.

**Inventory** (`inventory.py`). Capacity has a price, and it is computed rather than guessed. Customer
orders arrive, equally often on each side, and fill the quote for as many lots as the limit allows;
the long-run value of holding `q` lots relative to none is the fixed point of the average-reward
Bellman equation, found by relative value iteration. The solution is close to a parabola, worth about
2.7 ticks per lot at the limit, which is the quadratic inventory penalty of Avellaneda and Stoikov
reached from a different premise: there it is risk aversion, here the opportunity cost of a limit.
A trade that moves the position towards zero is credited that much and one that moves it away is
charged it, in both the quote prices and the decision of how much of a resting order to take.

**Mean reversion.** A resting bid above the future's offer (or an offer below its bid) by more than
the taker fee plus a margin proportional to recent volatility is taken with a fill-and-kill order,
for as many lots as are worth it given the inventory value. For lots that merely cancel inventory
already carried un-hedged no hedge is needed, so the yardstick for those is the mid.

**Kelly hedging** (`kelly.py`). Hedging every fill at once is safe and expensive: the next customer
often takes the inventory away again for nothing. Leaving a lot un-hedged is a bet with an edge (the
half-spread saved, times the chance that flow removes the lot first) and a variance (the future's,
over the time the lot is carried). Maximising expected log wealth under a cost of trading gives not a
target position but a band, inside which nothing is done and outside which inventory is hedged back
to the nearer edge; this is the no-trade region of Davis and Norman with a fractional-Kelly investor.
The band is sized against a bankroll that starts small and grows with realised profit, and is capped
by the exchange's own rule: more than ten lots may be un-hedged only briefly, so inventory beyond ten
lots is carried for at most thirty seconds.

**Forecast.** A prediction of the future's next move, from book pressure (the `momentum` preset) or
from the boosted model (`full`). It is used for one thing: when a hedge is due and the forecast says
the price is about to move in the inventory's favour, the hedge waits an update. When the forecast is
confident enough to act on, the price then moves its way by about a sixth of a tick, and the future's
spread averages a tick and a half, so it cannot pay for a trade that would not otherwise happen; it
can only improve the timing of one that would. Measured, that is worth about 0.06 ticks a hedge, and
the Kelly band leaves few hedges to time, so it comes to a fraction of a percent of the P&L (see
[RESULTS.md](RESULTS.md)).

### Staying inside the limits

A breach ends the match for the trader, so limits are enforced by arithmetic. An order is sent only
if the position limit would hold even if every other live order on that side filled first. Capacity
held by an order being cancelled is not reused until the exchange confirms the cancel: between the
decision and the exchange acting on it, someone else may have filled the order. The trader keeps its
own count of messages in the last second and stops short of the exchange's limit, reserving the last
few for hedges.

### Getting there first

Several traders answer each order book update, and the exchange serves their orders in the order
they arrive. Where two of them want the same customer, the one that arrives first gets it, and the
margin is microseconds: the time a handler spends thinking before it sends anything.

The first full match between the presets on the real exchange made the point. The richer the
strategy, the longer its handler took, the later its orders arrived, and the less it earned: the
presets finished almost exactly in order of speed, with the full strategy, which ran the model on
every update before doing anything else, last. None of that shows in a simulation where every
trader answers instantly.

So the handler is ordered by urgency. The orders that race (what to take, where to quote) depend
only on the two books, and go out first. The features, the forecast and the hedging decision come
after: the forecast is only used to time hedges, and nothing races for a hedge, which trades against
the future's book without taking anything from it. The search for what to take stops at the first
lot not worth taking, which on most updates is the first lot. Before the change a preset's first
order left when its whole handler had run, which then took thirty to forty microseconds. Now it
leaves thirteen to fifteen microseconds after the update arrives, the full strategy's included
(`scripts/handler_times.py` measures it; splitting the handler into steps the challengers can
override, below, cost about a microsecond of that).

The stock example trader shows how little it takes. Its pricing is a subtraction and takes about a
microsecond, but it writes a line to its log first, and that takes fifteen. Against the presets as
they were, it reached the exchange ahead of four of the six. Against the presets as they are, it was
behind every one of them on nine updates in ten. RESULTS.md has both matches.

Microseconds decide it because of how traders hear about an update. Each polls the shared memory
the exchange publishes to without pause (Ready Trader Go's subscriber spins on it), so traders with
a processor to themselves can see an update within microseconds of one another, and whichever does
less before it sends reaches the exchange first. The exchange deals with messages one at a time, so
the other then waits while the first one's are dealt with: a lead of microseconds shows at the
exchange as a fraction of a millisecond. Two of the challengers below did more before they sent
than base Vigil does (`rl` consults its policy, about five microseconds; `skew` waits for the
forecast, about thirteen), and on the real exchange each was behind base Vigil on three updates in
four or more, in matches where two copies of base Vigil were each ahead of the other half the time.
Rebuilt to do that work before the update arrives, `rl_fast` and `skew_fast` were ahead of base
Vigil on 48 per cent of the updates both answered, as its copy was on 49 (RESULTS.md,
`fast-live-s1`).

### Challengers (`strategies/challengers/`)

Five experimental versions of the trader, each base Vigil with one part replaced, so that each can be
pitted against base Vigil and the difference put down to that part, and two of them rebuilt to be as
quick as base Vigil (below). Base Vigil's handler is split into steps for this (`_trade`,
`_decide_takes`, `_decide_quotes`, `_carry_drift`, `_carry_risk`, `_quote_margin`, `_quote_size`,
`_future_book`), each of which a challenger may override; with none overridden it
does exactly what it did before the split, event for event, which a fingerprint of every preset's
event stream confirmed. A setting, `engine`, names the trader that runs, so a challenger is entered
like a preset (`preset:glft`) in the simulator, the backtests and on the real exchange.

The data decided two of the five designs. The ETF's customer orders arrive as a Poisson process, so
there are no bursts in them for a self-exciting model to find; the future's aggressive orders do
burst, so that is what the Hawkes version models. The gap between the ETF and the future closes
within two seconds, and taking the orders that open it is already base Vigil's mean reversion; what
reverts slowly enough to hold a position on is the future itself, so that is what the
Ornstein-Uhlenbeck version models. It does so in three sessions of the four; the fourth trends, and
strongly enough to flip every fit that includes it, so each leave-one-session-out model expects the
opposite of what its own session does. Two findings shaped the other designs. Customer orders are
priced so widely that the textbook optimal quotes (Guéant, Lehalle & Fernandez-Tapia) sit beyond the
standing market's walls, so the model's quotes are capped just inside them, the best price that can
trade. And four sessions are too little data to learn quoting from nothing, so the reinforcement
learner learns corrections to base Vigil's quotes instead.

Everything is fitted leave-one-session-out (`vigil challenge fit`), as the boosted model is: data
models from the other three sessions, the one tuning knob each challenger has by what it earns
trading them, the reinforcement learner by training on them. `vigil challenge run` then measures
each against base Vigil alone, head to head in the same match (paired, as above), and all together
in one field, where a second copy of base Vigil plays as a control. A field of near-identical
traders is noisy in a way a paired duel is not, and the control is there because of it: in the
first field measured, without one, the forecast-skewed version finished a thousand dollars a match
ahead of base Vigil, every session agreeing; with the control added and two more seeds its lead was
two hundred, and the two copies of base Vigil ended four hundred apart. For the same reason a
t-statistic over sessions never takes its standard error as smaller than the luck within a session
implies: four sessions' means can lie close together by chance. The comparison leans, if anything,
towards base Vigil: its own settings were chosen while looking at all four sessions, the
challengers' without the session they trade.

Two of the five sent their orders later than base Vigil, and on the real exchange they lost the race
for customers for it (above, "Getting there first"), so each has a second version that sends as
quickly. `rl_fast` makes exactly `rl`'s decisions, event for event (a test checks it, with a policy
that departs from base Vigil's choice in every state): it works out the policy's choice whenever the
state it depends on changes, on a fill, a hedge or a forecast, for either width of the future's
spread, so that on the update it only reads the spread and looks the choice up. `skew_fast` cannot
have the forecast without waiting for the model, so it skews by book pressure instead, the future's
microprice less its mid, whose sign calls the next move about as often as the model does; it works
that out in `_future_book`, a hook base Vigil calls on the future's book, which the exchange
publishes just before the ETF's and which base Vigil leaves alone. As quick as base Vigil, neither
beats it head to head. `rl` quotes otherwise than base Vigil on about ten updates a session of 3,600,
so `rl_fast` is base Vigil in all but name. The skewed versions come out ahead of base Vigil in the
all-in-one field at equal speed, but by how much depends on who else is in it: in the three fields
measured as versions were added, `skew` led base Vigil by 1,146, 206 and 1,050 dollars a match.
The simulator also does `skew` an injustice at measured speed: it gives a trader one reaction time
an update for everything it sends, while `skew`'s takes leave before its forecast is worked out and
only its quotes wait. On the real exchange `skew` earned more than the simulator gave it in each of
the three matches it played.

The reinforcement learner took three attempts, each judged on its training sessions only. Plain
Q-learning learnt a policy that earned less than base Vigil on the very sessions it was trained
on; double Q-learning with a longer horizon narrowed the gap; safe policy improvement with baseline
bootstrapping (Laroche et al., 2019), which keeps base Vigil's action wherever an alternative has
been tried too seldom to judge, closed it. Trained and judged on the same session for 300 sessions'
worth of experience, no setting that let it depart further from base Vigil earned more.

## The model (`ml/`)

`features.py` has one implementation, `FeatureState`, that turns order book updates and trade ticks
into twenty-five features one update at a time. The training set is made by pushing a recorded tape
through it; the live trader pushes the feed through it. A feature cannot mean one thing offline and
another online.

`adaboost.py` is discrete AdaBoost over decision stumps in numpy: each round picks the single
threshold on a single feature with the lowest weighted error, found by sorting every feature once and
taking cumulative sums. It is written out because an auto-trader submitted to the competition may
import numpy but not scikit-learn, and a fitted model is five small arrays that score a vector in
microseconds. Margins are mapped to probabilities by Platt scaling.

`train.py` evaluates leave-one-session-out. To score a session, the model is fitted on the other
three, and its calibration is itself cross-fitted inside those three. The bundle that is saved holds
one model per held-out session and one fitted on everything; a backtest on a session is handed the
model that never saw it.

Accuracy is not what the strategy is paid for, so `train.py` also prices the one decision the forecast
is used for (`hedge_timing`): what putting a hedge off for an update gains, on the quotes the hedge
would really have paid, for the boosted model, for the book-pressure rule it replaces, and for
waiting regardless, which should gain nothing and does.

## Surveillance (`surveillance/`)

### The replica

`MarketReplica` holds a shadow order book per instrument, built with the exchange's own `OrderBook`,
and applies every insert, amend and cancel in stream order. A listener on every order records each
execution with the resting order and the incoming order that caused it, so every trade has both
counterparties attached, including trades between the market's own orders that the exchange never
reports.

The replica also knows which trades the exchange ought to report: every fill of a competitor's order,
in the order the matching loop produces them, for whom, on which side of which instrument, at what
price and with what fee. The engine checks each reported trade against that list, and lays a report
that is missing at the door of the order it was for, not of whichever happened to be reported next.
A hedge does not pass through a book, but its price is determined by one: it must be a price the
replica's future book gives for that volume at that moment. A report or a hedge that fails means the
record has been altered or events were lost (control REC-01). On real exchange output the checks
have never failed; the tests make them fail by editing, deleting, reordering and inventing records.

One thing the stream cannot show about itself is a hedge that was removed, added or resized: a hedge
leaves no mark on any book. That shows only against a second record. Where the exchange's score
board is to hand, the positions the stream adds up to are held against the positions on it.

The engine never raises on what it is fed. An event that cannot be read is a finding, and the engine
carries on. A hole in the stream is a finding too, and there the engine stops: the books cannot be
rebuilt across a hole, and alerts raised from books that are wrong accuse people of things that did
not happen. It says what is missing, once, and the report says that nothing after that point was
examined.

### Participants

`Participant` rebuilds each team's positions, cash and fees from reported trades and hedges alone.
At the end the positions are marked the way the exchange marks them, and the result equals the
exchange's score board. That is a second, independent check that the stream is complete.

It also rebuilds enough of the exchange's own view of each team to know when the exchange would have
thrown it out: a position past the limit, the un-hedged clock run down, the message allowance used
up. That matters because of what the exchange does next. It cancels everything the team has resting,
and those cancellations sit in the stream looking like any others. Counted as the team's own they
produce a message-rate breach that never happened and a cancel count for a team that sent none. The
engine recognises them and books them to the exchange.

The message limit has a twist. The exchange never acts on the message that breaks it, so that
message is never in the stream: a genuine stream holds at most the limit in any window. A breach
shows as the limit reached and then the exchange cancelling the team's orders, or, if it had none
resting, as the limit reached and then silence. Those two are what control LIM-03 looks for.

### The controls

Each control is a `Detector` subclass that overrides the hooks it needs (an order arrived, was
cancelled, traded; a position changed; time passed; the match ended). Thresholds come from
`config/rulebook.yaml` through `self.param(...)`; none is a constant in the code, and a test confirms
that loosening one in the rulebook silences the alert.

The conduct controls had to be written so that honest market making does not set them off, and the
distinctions they draw are the substance of the design:

- **Spoofing (MKT-01).** A market maker also shows size, trades on the other side, and cancels. What
  it does not do is leave: it replaces the quote it pulled, because the quote was there to trade. An
  episode is a large, visible, unexecuted order that sat in the book while the participant traded on
  the opposite side and was then withdrawn *without being replaced*. Withdrawn means cancelled or
  amended down; replaced means by an order that rests, at about the same price, and large enough to
  answer for what was pulled (an order that never rests, or sits twenty ticks away, replaces
  nothing). Orders pulled within half a second of each other at several prices are counted as one
  withdrawal and reported as layering.
- **Wash trading (MKT-02).** Two competitors trading with each other in both directions is ordinary:
  one picks off the other's stale quotes each way. A wash is reciprocal trading that is either
  concentrated (each does most of its business with the other) or circular (lots come straight back
  at the same price relative to fair value *with the roles reversed*: whoever rested the order on the
  way out hits the other's order on the way back). A trader picking off a market maker is the
  aggressor in both directions.
- **Marking the close (MKT-04).** The exchange values a position at the last traded price, so a
  high last print flatters a long position. Selling high when long is just a good trade. The
  offence is an aggressive trade in the closing seconds that is uneconomic in itself (priced through
  fair value) and *adds to* the position whose valuation it improves. Whose trade closed the session
  is read off the replica's book: if two of the market's own orders traded last, nobody marked it.
- **Quote stuffing (MKT-03)** and **order-to-trade ratio (MKT-05)** look for orders with no intent to
  trade: bursts cancelled within half a second with nothing executed, and, over the session, the ratio
  of unexecuted orders to transactions by number and by volume as MiFID II RTS 9 defines it, with
  messages counted as its annex counts them: an entry or a cancellation once, an amendment twice, and
  a fill-and-kill order that the book cancels unfilled twice.

Alerts carry the sequence numbers of the events that support them, and an identifier derived from
what the alert is about rather than when it was raised.

## The bus (`bus/`)

`Bus` is four methods: publish, consume, commit, flush. `KafkaBus` implements it on confluent-kafka;
`MemoryBus` implements it in-process with the same ordering and offset semantics, which is what lets
the services be tested without a broker.

| Topic | Key | Carries |
|---|---|---|
| `rtg.match-events` | match | a header, the events in order, an end marker |
| `rtg.alerts` | participant | one message per alert |
| `compliance.reports` | match | the report's fingerprint and where it is anchored |
| `regulatory.updates` | document number | new and changed regulatory documents |

A match's messages share a key, so Kafka keeps them in order on one partition while different
matches spread across partitions and consumers.

Delivery is at least once, and everything downstream is arranged so that once more is harmless:

- The producer is idempotent and waits for all in-sync replicas. Delivery callbacks surface failed
  sends even if the producer queue has drained; a failure aborts reporting before input offsets commit.
- The end marker states how many events were sent and their SHA-256. The monitor computes the same
  over what it received; a mismatch is recorded in the report rather than glossed over.
- Events carry sequence numbers. One seen twice is discarded. One that never comes leaves a hole,
  and the engine stops following that match there (see above) rather than guess.
- The monitor commits its place on a partition when a match has been reported, and never past the
  header of a match still in flight, nor past the last message it was *handed* (as opposed to
  fetched). If it dies mid-match it starts that match again from its header.
- The price of that is repetition: matches that ended after the oldest unfinished one began are read
  and reported a second time. Alert identifiers depend only on the alert's content. Report hashes
  also depend on the rulebook, frozen regulatory snapshot, coverage, and software/exchange fingerprints;
  these must stay fixed for byte-identical replay. The registry refuses a second filing under the
  same identifier. Whoever consumes alerts should key on the identifier.
- Idle polls drive expiry even without incoming messages. A match silent for five minutes without an
  end marker is reported as incomplete; the limit is configurable.
- Malformed records are quarantined with their bytes, key, offsets and reason. Affected coverage is
  invalidated before input commits. Delivery failures and persistence failures stop the monitor for replay.
- Revoked partitions lose their in-memory state without a partial report. The new owner replays the
  retained header. SQLite freezes regulatory context and verifies software, dependency runtime and rules.
- The CLI retains no completed reports in memory. Active-match and per-match event budgets bound state
  growth; exceeded event budgets stop reconstruction and mark the report incomplete.

This is a single-host prototype with real rebalance tests. Workers must share durable context/artifact
storage and the same environment. There are no distributed checkpoints or multi-host coordination.
Recovery requires enough topic retention to replay from the oldest unfinished header. Disk history has
an explicit archival/retention requirement; see [OPERATIONS.md](OPERATIONS.md).

## Reports and anchoring (`reports/`, `chain/`, `contracts/`)

A report is a JSON document: the match, the rulebook it was judged against (with its hash), each
participant's figures, the alerts, the regulations they cite and the state of the regulatory
register. It records nothing about when or where it was produced, so the same inputs give the same
document.

It is serialised in exactly one way (sorted keys, no insignificant whitespace, UTF-8), and that is
what is written to disk. The fingerprint of the report is therefore the SHA-256 of the file, and
that is meant literally: a report read back from a file is fingerprinted by the file's bytes, not
by what they parse to. A copy that has been indented or re-saved by another tool says the same thing
and is a different file; verification says so, rather than passing it. Reading is as strict as
writing. JSON parsers disagree about a document that gives a key twice (some keep the first value,
some the last), so such a file could show one report to a person's tools and another to ours; it is
refused, as are `NaN`, numbers out of range and anything that is not plain UTF-8.

The alerts are committed to separately, through the root of a Merkle tree built as RFC 6962 specifies
for Certificate Transparency (leaves and nodes hashed with different prefixes, so an interior node
cannot be passed off as a leaf). That allows one alert to be disclosed and proved to belong to a
report without disclosing the others.

`ComplianceRegistry.sol` stores, per report identifier: the report's fingerprint, the alerts' Merkle
root and count, the fingerprints of the event stream and the rulebook, the block time, the reporter,
and a running hash over all entries so far. Its properties:

- **Write-once.** `anchor` reverts if the identifier has been used. Nothing can be updated or deleted.
- **Permissioned.** Only addresses the owner has added as reporters may file. Owning and reporting
  are separate roles: handing over ownership neither lets the new owner file nor stops the old one.
- **Verifiable on chain.** `verifyReport` compares a fingerprint; `verifyAlert` checks a Merkle audit
  path (RFC 9162, section 2.1.3.2) against the stored root, taking the tree size from the stored
  alert count rather than from the caller.
- **Chained.** Each entry stores the SHA-256 of the previous head and every field of the entry, the
  alert count, severity, reporter and time included, so one 32-byte value commits to the whole
  history in order.
- **Searchable by evidence.** An identifier is the filer's to choose, so write-once alone would not
  stop a second, more convenient report on the same match being filed under another name. Filings
  are therefore indexed by the event stream they are about. Whoever holds the exchange's record of a
  match can ask for every report ever filed about it (`reportsAbout`, `vigil chain about`), and
  `vigil chain verify` says when a report is not the only one.

Every hash is SHA-256, not Keccak. The EVM has a precompile for it, and it means an auditor can check
every link with `shasum`: the report file, the exchange's event file and the rulebook file each hash
to what is on chain.

Filing is one-way, so `Registry.anchor` refuses a report that contradicts itself: a stated Merkle
root that is not the root of its alerts, a summary that does not match them, a fingerprint that is
not a fingerprint. What goes on chain is computed from the alerts, not copied from what the document
says about itself.

Verification (`Registry.verify`) recomputes everything from the file, its fingerprint from its bytes
and the Merkle root, count and severity from its alerts, and compares them with what was filed. It
reports which check failed: an edited alert changes both the fingerprint and the root; an edit
elsewhere changes only the fingerprint; a re-saved copy is told apart from an altered one. The
registry consulted is the one the verifier names or has deployed, recognised by its code, and never
the one named in a receipt that arrived with the report.

On a public network, through a hosted provider, three things differ from a local chain, and the
client allows for each. The provider answers from many nodes, so the one asked for a block just
after a transaction may not have it yet: block reads are retried. A free plan turns away bursts of
requests: calls back off and try again for up to half a minute, and a node that still refuses is
reported as rate-limited, not as absent. And a program can be cut off after its filing has gone
through but before it has kept the receipt: filing the same report again files nothing, and rebuilds
the receipt from the contract's log of the filing, found by the time the registry recorded (providers
search logs only a few thousand blocks at a time). A provider's address carries its API key, so the
client never prints more of it than the host. `vigil chain verify` also asks Etherscan, which reads
the chain from nodes of its own, whether it holds the same filing; a disagreement fails the check.

The contract is developed with Foundry: thirty Solidity tests, four of them fuzzed. They include a
reference Merkle tree written the obvious recursive way, pinned to the published Certificate
Transparency test vectors, against which the iterative verifier is checked exhaustively: every node
of every tree up to seventeen leaves, offered at every index with its true sibling path and with
shorter and longer ones, and only a leaf at its own index passes. Mutation testing of the contract
(sixty-three single changes of behaviour) leaves two survivors, both of which change nothing that
can be observed. The ABI and bytecode are shipped in the Python package so that anchoring needs only
Python, and a test recompiles the source and fails if they differ.

## Regulatory register (`regulatory/`)

Each regulation in the rulebook may carry a `watch`: the agencies and phrases to search the Federal
Register for. The scraper runs every watch, stores each document once with a hash of its content
(so a later correction is noticed), and records which regulation each bears on and how prominently:
in the title, in the abstract, or only somewhere in the text. The first run reaches back five years;
later runs start a month before the newest document held. A report records the register's
fingerprint and the most recent documents that are prominently about a regulation its controls cite.

The Federal Register was chosen from ApiVault because it is where SEC and CFTC rule-making is
actually published, and its API needs no key. `regulatory/apivault.py` records every ApiVault API
the project uses and, as deliberately, the ones it looked at and did not.

## Running the real exchange (`live.py`)

`LiveMatch` writes an `exchange.json` and one small module per team into a directory of its own,
starts the exchange and the traders as the stock launcher does, and follows `match_events.csv`.
Following the file rather than the exchange's heads-up-display socket is deliberate: the file is the
official record, so the fingerprint computed from the stream is the fingerprint of the file, and a
display client would change the exchange's behaviour (it counts as a connected competitor). The cost
is a delay of a fraction of a second while the exchange's writer buffers.

The display can still be had for watching (`vigil live --hud`, and `vigil replay` for a finished
match). It is a viewer only; surveillance follows the file either way. While it is connected the
exchange counts it among its clients, so a match with the display open runs to the end of its market
data even if every trader has left.

A match that cannot make progress (the exchange waits for ever on market data it cannot open) is
detected by deadline and stopped, not waited for. A trader that crashes or cannot be loaded is
reported by name with the last line of its log, and the command fails: otherwise it would simply be
a team with no trades in an otherwise normal-looking report.

Presets are started with the rules of the exchange they are started on (its limits, fees and clock
speed) and with the model that never saw the session being played.

The directory also gets a fingerprint of the traders' code and fitted model. A match is played by
the traders as they are on the day, and `scripts/record_live_match.py`, which plays the same field
again in the simulator for comparison, uses the fingerprint to tell whether it would be playing the
same traders. If they have changed since, the simulation already on record is kept, and RESULTS.md
says of each match which traders its simulation used.
