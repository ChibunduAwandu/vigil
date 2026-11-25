# Controls

Generated from `config/rulebook.yaml` by `scripts/render_docs.py`. The rulebook is the single place where thresholds live; a compliance report records its SHA-256, so a finding can be traced to the thresholds that produced it.

Rulebook version 1, SHA-256 `b880315e61d29a9c77444d4de5762b66e528b81aab04d5f828eb73d65e272b4b` (what `shasum -a 256 config/rulebook.yaml` prints).

Ready Trader Go is a simulator: nothing traded on it is legally regulated. Each control is modelled on an obligation that a firm trading a real ETF against a real future would carry, and the mapping below records which. It is an engineering cross-reference, not legal advice.

## Risk limits

### LIM-01: Position limit

The net position in either instrument went beyond the exchange's position limit.

Severity CRITICAL. Modelled on: CFTC-150, SEC-15c3-5, MIFID2-RTS6, CFTC-38.251.

### LIM-02: Un-hedged exposure

The combined ETF and future position stayed outside the un-hedged allowance for longer than permitted. A warning is raised when most of the permitted time has been used.

Severity CRITICAL. Modelled on: SEC-15c3-5, MIFID2-RTS6, CFTC-38.251.

| parameter | value |
|---|---|
| `allowance_lots` | `10` |
| `time_limit` | `60.0` |
| `warn_fraction` | `0.75` |

### LIM-03: Message rate

Order messages were sent faster than the exchange's throttle allows: the participant reached the limit and the exchange disconnected it. A warning is raised as the rate approaches the limit.

Severity CRITICAL. Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

| parameter | value |
|---|---|
| `warn_fraction` | `0.8` |
| `warn_cooldown` | `30.0` |
| `silence_seconds` | `5.0` |

## Market conduct

### MKT-01: Spoofing and layering

Large orders were repeatedly shown in the visible part of the book on one side while the participant traded on the other, then withdrawn unexecuted, by cancelling them or amending them down, without being replaced.

Severity HIGH. Modelled on: CEA-4c(a)(5), CME-575, FINRA-5210, MAR-12, SEC-10b-5.

| parameter | value |
|---|---|
| `min_lots` | `40` |
| `min_order_lots` | `10` |
| `max_lifetime` | `5.0` |
| `max_fill_ratio` | `0.1` |
| `visible_levels` | `5` |
| `min_opposite_lots` | `1` |
| `pull_gap` | `0.5` |
| `requote_window` | `0.3` |
| `requote_ratio` | `0.5` |
| `requote_ticks` | `15` |
| `window` | `60.0` |
| `min_episodes` | `3` |
| `layering_levels` | `3` |

### MKT-02: Wash trading

Two participants traded with each other in both directions in matching size, and either did most of their business with each other or passed the same lots straight back at a matching price, so that nothing changed hands and no risk was taken.

Severity HIGH. Modelled on: CEA-4c(a)(1), SEA-9(a), CME-534, FINRA-5210, MAR-12.

| parameter | value |
|---|---|
| `window` | `120.0` |
| `min_round_trip_lots` | `30` |
| `min_trades_each_way` | `2` |
| `max_imbalance` | `0.35` |
| `min_pair_share` | `0.5` |
| `min_returned_lots` | `60` |
| `min_returned_share` | `0.75` |
| `max_return_seconds` | `10.0` |
| `max_price_gap_ticks` | `1.5` |

### MKT-03: Quote stuffing

Bursts of orders were entered and cancelled almost at once with nothing executed, a pattern that loads the exchange and obscures the book without any intent to trade.

Severity MEDIUM. Modelled on: CME-575, MAR-12, MIFID2-RTS6, CEA-4c(a)(5).

| parameter | value |
|---|---|
| `burst_messages` | `24` |
| `burst_cancel_ratio` | `0.8` |
| `max_order_life` | `0.5` |
| `window` | `60.0` |
| `min_bursts` | `5` |

### MKT-04: Marking the close

In the final seconds of the session the participant paid through fair value in a way that moved the closing price in favour of the position it was holding.

Severity HIGH. Modelled on: CEA-4c(a)(5), SEA-9(a), CFTC-180, MAR-12.

| parameter | value |
|---|---|
| `closing_window` | `30.0` |
| `min_through_ticks` | `1.5` |
| `min_benefit` | `25.0` |
| `min_attempt_orders` | `3` |

### MKT-05: Order-to-trade ratio

The ratio of order messages to executions, counted as MiFID II RTS 9 counts them, was far above what a participant trading in good faith produces.

Severity MEDIUM. Modelled on: MIFID2-RTS9, MIFID2-RTS6, CME-575.

| parameter | value |
|---|---|
| `min_orders` | `200` |
| `max_ratio_by_number` | `150.0` |
| `max_ratio_by_volume` | `400.0` |

### MKT-06: Off-market order

An order was priced so far from the market that it is either an error or an attempt to print an unrepresentative price.

Severity MEDIUM. Modelled on: SEC-15c3-5, MIFID2-RTS6, CFTC-38.251.

| parameter | value |
|---|---|
| `collar` | `0.03` |
| `executed_severity` | `HIGH` |

## Books and records

### REC-01: Trade reconciliation

A trade or hedge reported by the exchange is not the one obtained by replaying the order stream through the matching rules, the positions the stream adds up to are not those on the exchange's score board, or the stream is incomplete, out of order or unreadable.

Severity HIGH. Modelled on: SEC-17a-4, CFTC-1.31, SEC-613, FINRA-3110.

| parameter | value |
|---|---|
| `max_alerts` | `20` |

## Regulations

### CEA-4c(a)(5)

**Commodity Exchange Act, disruptive practices.** 7 U.S.C. 6c(a)(5) (added by Dodd-Frank Act section 747). CFTC, US. <https://www.law.cornell.edu/uscode/text/7/6c>

Makes it unlawful to violate bids or offers, to show intentional or reckless disregard for the orderly execution of transactions during the closing period, or to spoof ("bidding or offering with the intent to cancel the bid or offer before execution").

Controls: MKT-01, MKT-03, MKT-04. Watched in the Federal Register for: spoofing, disruptive trading practices.

### CEA-4c(a)(1)

**Commodity Exchange Act, wash sales and fictitious trades.** 7 U.S.C. 6c(a)(1)-(2). CFTC, US. <https://www.law.cornell.edu/uscode/text/7/6c>

Prohibits transactions that are, or are commonly known as, wash sales or accommodation trades, and fictitious sales.

Controls: MKT-02. Watched in the Federal Register for: wash trading, wash sale.

### CFTC-180

**Prohibition on manipulative and deceptive devices and price manipulation.** 17 CFR 180.1 and 180.2 (under 7 U.S.C. 9). CFTC, US. <https://www.ecfr.gov/current/title-17/chapter-I/part-180>

Prohibits the use of any manipulative device, scheme or artifice to defraud, and the manipulation or attempted manipulation of the price of any swap, commodity or future.

Controls: MKT-04. Watched in the Federal Register for: price manipulation, manipulative device.

### CFTC-150

**Limits on positions.** 17 CFR Part 150 (under 7 U.S.C. 6a). CFTC, US. <https://www.ecfr.gov/current/title-17/chapter-I/part-150>

Sets speculative position limits and the aggregation and exemption rules that go with them.

Controls: LIM-01. Watched in the Federal Register for: position limits.

### CFTC-38.251

**Electronic trading risk principles for designated contract markets.** 17 CFR 38.251(e)-(g) and 38.255. CFTC, US. <https://www.ecfr.gov/current/title-17/section-38.251>

Requires a contract market to adopt rules and pre-trade risk controls that prevent, detect and mitigate market disruptions and system anomalies associated with electronic trading.

Controls: LIM-01, LIM-02, LIM-03, MKT-06. Watched in the Federal Register for: electronic trading risk principles, pre-trade risk controls.

### CFTC-1.31

**Regulatory records; retention and production.** 17 CFR 1.31. CFTC, US. <https://www.ecfr.gov/current/title-17/section-1.31>

Requires regulatory records to be kept so that their authenticity and reliability are ensured, and to be produced promptly on request.

Controls: REC-01. Watched in the Federal Register for: regulatory records, records retention.

### SEA-9(a)

**Securities Exchange Act, manipulation of security prices.** 15 U.S.C. 78i(a)(1)-(2). SEC, US. <https://www.law.cornell.edu/uscode/text/15/78i>

Prohibits wash sales and matched orders entered to create a false or misleading appearance of active trading, and series of transactions that raise or depress a price to induce others to trade.

Controls: MKT-02, MKT-04. Watched in the Federal Register for: market manipulation, wash sales, marking the close.

### SEC-10b-5

**Employment of manipulative and deceptive devices.** 17 CFR 240.10b-5 (under 15 U.S.C. 78j(b)). SEC, US. <https://www.ecfr.gov/current/title-17/section-240.10b-5>

The general anti-fraud rule: prohibits any device, scheme or act that operates as a fraud or deceit in connection with the purchase or sale of a security.

Controls: MKT-01. Watched in the Federal Register for: manipulative trading.

### SEC-15c3-5

**Market Access Rule.** 17 CFR 240.15c3-5. SEC, US. <https://www.ecfr.gov/current/title-17/section-240.15c3-5>

Requires a broker-dealer with market access to maintain risk management controls that prevent orders exceeding pre-set credit or capital thresholds and reject erroneous orders (by price or size), and to review their effectiveness regularly.

Controls: LIM-01, LIM-02, MKT-06. Watched in the Federal Register for: market access rule, 15c3-5.

### SEC-17a-4

**Records to be preserved by brokers and dealers.** 17 CFR 240.17a-4, in particular paragraph (f). SEC, US. <https://www.ecfr.gov/current/title-17/section-240.17a-4>

Sets retention periods for broker-dealer records. Electronic records must either be kept in a non-rewriteable, non-erasable format or carry a complete time-stamped audit trail that lets the original be recreated if it is modified or deleted.

Controls: REC-01. Watched in the Federal Register for: electronic recordkeeping, 17a-4.

### SEC-613

**Consolidated audit trail.** 17 CFR 242.613. SEC, US. <https://www.ecfr.gov/current/title-17/section-242.613>

Requires every order event (origination, modification, cancellation, routing, execution) to be recorded and reported to a central repository with synchronised timestamps.

Controls: REC-01. Watched in the Federal Register for: consolidated audit trail.

### FINRA-5210

**Publication of transactions and quotations.** FINRA Rule 5210, Supplementary Material .02 (self-trades) and .03 (disruptive quoting and trading). FINRA, US. <https://www.finra.org/rules-guidance/rulebooks/finra-rules/5210>

Prohibits publishing quotations or transactions that are not bona fide. The supplementary material covers self-trades from related algorithms and the entry of orders on one side to move the market before trading on the other.

Controls: MKT-01, MKT-02.

### FINRA-3110

**Supervision.** FINRA Rule 3110. FINRA, US. <https://www.finra.org/rules-guidance/rulebooks/finra-rules/3110>

Requires a supervisory system, including written procedures and reviews, that is reasonably designed to achieve compliance with securities laws and FINRA rules.

Controls: REC-01.

### CME-575

**Disruptive practices prohibited.** CME Group Rule 575 (CME Rulebook, chapter 5). CME Group, US (exchange rule). <https://www.cmegroup.com/rulebook/CME/I/5/5.pdf>

All orders must be entered for the purpose of executing bona fide transactions; prohibits orders entered to mislead, to overload or delay systems, or to disrupt orderly trading.

Controls: LIM-03, MKT-01, MKT-03, MKT-05.

### CME-534

**Wash trades prohibited.** CME Group Rule 534 (CME Rulebook, chapter 5). CME Group, US (exchange rule). <https://www.cmegroup.com/rulebook/CME/I/5/5.pdf>

Prohibits placing or accepting buy and sell orders in the same product for accounts with common beneficial ownership where the orders are expected to match each other.

Controls: MKT-02.

### MAR-12

**Market Abuse Regulation, market manipulation.** Regulation (EU) No 596/2014, Articles 12 and 15, with Annex II of Delegated Regulation (EU) 2016/522. ESMA and national competent authorities, EU. <https://eur-lex.europa.eu/eli/reg/2014/596/oj>

Defines and prohibits market manipulation. The delegated regulation lists the practices that indicate it, among them layering and spoofing, quote stuffing, wash trades, momentum ignition and marking the close.

Controls: MKT-01, MKT-02, MKT-03, MKT-04.

### MAR-16

**Market Abuse Regulation, detection and reporting.** Regulation (EU) No 596/2014, Article 16, with Delegated Regulation (EU) 2016/957. ESMA and national competent authorities, EU. <https://eur-lex.europa.eu/eli/reg_del/2016/957/oj>

Requires anyone professionally arranging or executing transactions to maintain effective systems for detecting suspicious orders and transactions and to report them without delay.

Controls: none directly.

### MIFID2-RTS6

**Organisational requirements for algorithmic trading.** Directive 2014/65/EU Article 17, with Delegated Regulation (EU) 2017/589 (RTS 6). ESMA and national competent authorities, EU. <https://eur-lex.europa.eu/eli/reg_del/2017/589/oj>

Requires pre-trade controls (price collars, maximum order value and volume, maximum message limits), real-time monitoring, post-trade controls and a kill function for algorithmic trading, plus automated surveillance for market manipulation.

Controls: LIM-01, LIM-02, LIM-03, MKT-03, MKT-05, MKT-06.

### MIFID2-RTS9

**Ratio of unexecuted orders to transactions.** Directive 2014/65/EU Article 48(6), with Delegated Regulation (EU) 2017/566 (RTS 9). ESMA and national competent authorities, EU. <https://eur-lex.europa.eu/eli/reg_del/2017/566/oj>

Requires trading venues to calculate, for each member, the ratio of unexecuted orders to transactions both by number and by volume, and to set maximum ratios.

Controls: MKT-05.
