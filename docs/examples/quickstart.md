# Compliance report: quickstart

**66 alerts, the most serious CRITICAL.** Participants flagged: Closer, Reckless, Spoofer, Stuffer, WashA, WashB.
The record reconciles: the 1,393 trades and 925 hedges reported by the exchange are those an independent replay of the order stream produces, and the positions it adds up to are those on the exchange's score board for all 9 teams.

## The match

|  |  |
|---|---|
| Source | sim |
| Market data | market_data2.csv |
| Duration | 900.0 s |
| Events | 652,491 |
| ETF lots traded | 58,348 |
| Teams | Baseline, Closer, MeanRev, Reckless, Spoofer, Stuffer, VigilFull, WashA, WashB |
| Position limit | 100 |
| Message limit | 50 per 1.0 s |

## Participants

| Team | Alerts | Worst | Exchange status | P&L ($) | Lots traded | Order-to-trade | Peak position | Longest un-hedged |
|---|---:|---|---|---:|---:|---:|---:|---:|
| Baseline | - | - | OK | 16,354.11 | 7,525 | 18.8 | 100 | 0 s |
| Closer | 1 | HIGH | OK | 157.35 | 57 | 16.9 | 57 | 0 s |
| MeanRev | - | - | OK | 15,037.97 | 8,949 | 13.5 | 100 | 0 s |
| Reckless | 2 | CRITICAL | BREACH | -4,695.00 | 30 | 0.0 | 30 | 890 s |
| Spoofer | 5 | HIGH | OK | -113.39 | 681 | 3.0 | 95 | 0 s |
| Stuffer | 44 | MEDIUM | OK | 47.15 | 8 | 2,655.4 | 8 | 0 s |
| VigilFull | - | - | OK | 20,574.79 | 9,871 | 11.5 | 100 | 30 s |
| WashA | 7 | HIGH | OK | -1,105.11 | 1,593 | 0.8 | 100 | 0 s |
| WashB | 7 | HIGH | OK | -595.15 | 1,570 | 0.9 | 100 | 0 s |

## Alerts

### CRITICAL: Un-hedged exposure (LIM-02), Reckless

Held more than the allowance of 10 lots un-hedged for the whole of the 60 s allowed (30 lots when last it traded).

- Alert `LIM-02-a8c414304fbd`, from 9.77 s to 69.77 s of the match.
- Measured: unhedged_lots = 30, peak_unhedged_lots = 30, allowance_lots = 10, seconds = 60.0, time_limit = 60.0.
- Evidence: 1 event of the match stream (sequence numbers 7527).
- Modelled on: SEC-15c3-5, MIFID2-RTS6, CFTC-38.251.

### HIGH: Spoofing and layering (MKT-01), Spoofer

3 times in 9 s, size was shown and withdrawn unexecuted (180 lots in all, resting 0.62 s on average) while 30 lots traded on the opposite side.

- Alert `MKT-01-7c623aa9dba6`, from 1.76 s to 10.30 s of the match.
- Measured: episodes = 3, shown_lots = 180, opposite_traded_lots = 30, mean_lifetime = 0.622, levels_pulled_together = 1, window = 60.0.
- Evidence: 6 events of the match stream (sequence numbers 1511, 1605, 2074, 2574, 7279, 8013).
- Modelled on: CEA-4c(a)(5), CME-575, FINRA-5210, MAR-12, SEC-10b-5.

### HIGH: Wash trading (MKT-02), WashA

Bought 30 and sold 30 lots against WashB in 6 executions over 20 s. Trading between the two made up at least 75% of each one's volume in that time, and 10 lots were handed straight back at a matching price.

- Alert `MKT-02-befe95783c96`, from 1.98 s to 22.05 s of the match.
- Counterparty: WashB.
- Measured: lots_bought = 30, lots_sold = 30, imbalance = 0.0, pair_share_of_volume = 0.75, returned_lots = 10, returned_share = 0.333, executions = 6, window = 120.0.
- Evidence: 6 events of the match stream (sequence numbers 1663, 7635, 10262, 14633, 20059, 22169).
- Modelled on: CEA-4c(a)(1), SEA-9(a), CME-534, FINRA-5210, MAR-12.

### HIGH: Wash trading (MKT-02), WashB

Bought 30 and sold 30 lots against WashA in 6 executions over 20 s. Trading between the two made up at least 75% of each one's volume in that time, and 10 lots were handed straight back at a matching price.

- Alert `MKT-02-7e12f8c10e63`, from 1.98 s to 22.05 s of the match.
- Counterparty: WashA.
- Measured: lots_bought = 30, lots_sold = 30, imbalance = 0.0, pair_share_of_volume = 0.75, returned_lots = 10, returned_share = 0.333, executions = 6, window = 120.0.
- Evidence: 6 events of the match stream (sequence numbers 1663, 7635, 10262, 14633, 20059, 22169).
- Modelled on: CEA-4c(a)(1), SEA-9(a), CME-534, FINRA-5210, MAR-12.

### HIGH: Spoofing and layering (MKT-01), Spoofer

25 times in 66 s, size was shown and withdrawn unexecuted (1500 lots in all, resting 0.87 s on average) while 221 lots traded on the opposite side.

- Alert `MKT-01-808332f17d69`, from 11.21 s to 77.04 s of the match.
- Measured: episodes = 25, shown_lots = 1500, opposite_traded_lots = 221, mean_lifetime = 0.87, levels_pulled_together = 1, window = 60.0.
- Evidence: 50 events of the match stream (sequence numbers 8584, 8925, 13276, 13579, 14638, 16107, 17656, 19367 ...).
- Modelled on: CEA-4c(a)(5), CME-575, FINRA-5210, MAR-12, SEC-10b-5.

### HIGH: Wash trading (MKT-02), WashA

Bought 86 and sold 60 lots against WashB in 15 executions over 118 s. Trading between the two made up at least 61% of each one's volume in that time, and 46 lots were handed straight back at a matching price.

- Alert `MKT-02-15ae2ea028da`, from 36.01 s to 154.04 s of the match.
- Counterparty: WashB.
- Measured: lots_bought = 86, lots_sold = 60, imbalance = 0.178, pair_share_of_volume = 0.608, returned_lots = 46, returned_share = 0.63, executions = 15, window = 120.0.
- Evidence: 15 events of the match stream (sequence numbers 32707, 43057, 44702, 51977, 55017, 56003, 60781, 62358 ...).
- Modelled on: CEA-4c(a)(1), SEA-9(a), CME-534, FINRA-5210, MAR-12.

### HIGH: Wash trading (MKT-02), WashB

Bought 60 and sold 86 lots against WashA in 15 executions over 118 s. Trading between the two made up at least 61% of each one's volume in that time, and 46 lots were handed straight back at a matching price.

- Alert `MKT-02-aebc16d16e00`, from 36.01 s to 154.04 s of the match.
- Counterparty: WashA.
- Measured: lots_bought = 60, lots_sold = 86, imbalance = 0.178, pair_share_of_volume = 0.608, returned_lots = 46, returned_share = 0.63, executions = 15, window = 120.0.
- Evidence: 15 events of the match stream (sequence numbers 32707, 43057, 44702, 51977, 55017, 56003, 60781, 62358 ...).
- Modelled on: CEA-4c(a)(1), SEA-9(a), CME-534, FINRA-5210, MAR-12.

### HIGH: Spoofing and layering (MKT-01), Spoofer

16 times in 73 s, size was shown and withdrawn unexecuted (930 lots in all, resting 0.73 s on average) while 151 lots traded on the opposite side.

- Alert `MKT-01-da4b2e335e89`, from 78.22 s to 151.25 s of the match.
- Measured: episodes = 16, shown_lots = 930, opposite_traded_lots = 151, mean_lifetime = 0.726, levels_pulled_together = 1, window = 60.0.
- Evidence: 32 events of the match stream (sequence numbers 58609, 59580, 60510, 60695, 61361, 61627, 62570, 62736 ...).
- Modelled on: CEA-4c(a)(5), CME-575, FINRA-5210, MAR-12, SEC-10b-5.

### HIGH: Spoofing and layering (MKT-01), Spoofer

10 times in 39 s, size was shown and withdrawn unexecuted (600 lots in all, resting 0.69 s on average) while 79 lots traded on the opposite side.

- Alert `MKT-01-d7d9052c577a`, from 174.03 s to 213.47 s of the match.
- Measured: episodes = 10, shown_lots = 600, opposite_traded_lots = 79, mean_lifetime = 0.689, levels_pulled_together = 1, window = 60.0.
- Evidence: 20 events of the match stream (sequence numbers 123503, 124879, 125327, 125403, 129232, 129249, 130152, 130254 ...).
- Modelled on: CEA-4c(a)(5), CME-575, FINRA-5210, MAR-12, SEC-10b-5.

### HIGH: Wash trading (MKT-02), WashA

Bought 40 and sold 70 lots against WashB in 11 executions over 118 s. Trading between the two made up at least 50% of each one's volume in that time, and 20 lots were handed straight back at a matching price.

- Alert `MKT-02-1b0d5acd51d7`, from 198.02 s to 315.96 s of the match.
- Counterparty: WashB.
- Measured: lots_bought = 40, lots_sold = 70, imbalance = 0.273, pair_share_of_volume = 0.5, returned_lots = 20, returned_share = 0.364, executions = 11, window = 120.0.
- Evidence: 11 events of the match stream (sequence numbers 139566, 142208, 144856, 148817, 150061, 152542, 179210, 190466 ...).
- Modelled on: CEA-4c(a)(1), SEA-9(a), CME-534, FINRA-5210, MAR-12.

### HIGH: Wash trading (MKT-02), WashB

Bought 70 and sold 40 lots against WashA in 11 executions over 118 s. Trading between the two made up at least 50% of each one's volume in that time, and 20 lots were handed straight back at a matching price.

- Alert `MKT-02-10aa60fce44f`, from 198.02 s to 315.96 s of the match.
- Counterparty: WashA.
- Measured: lots_bought = 70, lots_sold = 40, imbalance = 0.273, pair_share_of_volume = 0.5, returned_lots = 20, returned_share = 0.364, executions = 11, window = 120.0.
- Evidence: 11 events of the match stream (sequence numbers 139566, 142208, 144856, 148817, 150061, 152542, 179210, 190466 ...).
- Modelled on: CEA-4c(a)(1), SEA-9(a), CME-534, FINRA-5210, MAR-12.

### HIGH: Spoofing and layering (MKT-01), Spoofer

8 times in 48 s, size was shown and withdrawn unexecuted (480 lots in all, resting 0.90 s on average) while 64 lots traded on the opposite side.

- Alert `MKT-01-e54d601497cf`, from 219.46 s to 267.28 s of the match.
- Measured: episodes = 8, shown_lots = 480, opposite_traded_lots = 64, mean_lifetime = 0.901, levels_pulled_together = 1, window = 60.0.
- Evidence: 16 events of the match stream (sequence numbers 152330, 154111, 154653, 155815, 156294, 156445, 170708, 170910 ...).
- Modelled on: CEA-4c(a)(5), CME-575, FINRA-5210, MAR-12, SEC-10b-5.

### HIGH: Wash trading (MKT-02), WashA

Bought 59 and sold 40 lots against WashB in 10 executions over 98 s. Trading between the two made up at least 62% of each one's volume in that time, and 29 lots were handed straight back at a matching price.

- Alert `MKT-02-b8967b07b821`, from 359.99 s to 458.01 s of the match.
- Counterparty: WashB.
- Measured: lots_bought = 59, lots_sold = 40, imbalance = 0.192, pair_share_of_volume = 0.623, returned_lots = 29, returned_share = 0.586, executions = 10, window = 120.0.
- Evidence: 10 events of the match stream (sequence numbers 237132, 271106, 276037, 278593, 279397, 286987, 290367, 291716 ...).
- Modelled on: CEA-4c(a)(1), SEA-9(a), CME-534, FINRA-5210, MAR-12.

### HIGH: Wash trading (MKT-02), WashB

Bought 40 and sold 59 lots against WashA in 10 executions over 98 s. Trading between the two made up at least 62% of each one's volume in that time, and 29 lots were handed straight back at a matching price.

- Alert `MKT-02-16b35c8f9071`, from 359.99 s to 458.01 s of the match.
- Counterparty: WashA.
- Measured: lots_bought = 40, lots_sold = 59, imbalance = 0.192, pair_share_of_volume = 0.623, returned_lots = 29, returned_share = 0.586, executions = 10, window = 120.0.
- Evidence: 10 events of the match stream (sequence numbers 237132, 271106, 276037, 278593, 279397, 286987, 290367, 291716 ...).
- Modelled on: CEA-4c(a)(1), SEA-9(a), CME-534, FINRA-5210, MAR-12.

### HIGH: Wash trading (MKT-02), WashA

Bought 60 and sold 30 lots against WashB in 9 executions over 116 s. Trading between the two made up at least 60% of each one's volume in that time, and 30 lots were handed straight back at a matching price.

- Alert `MKT-02-34769151cdcf`, from 463.99 s to 580.02 s of the match.
- Counterparty: WashB.
- Measured: lots_bought = 60, lots_sold = 30, imbalance = 0.333, pair_share_of_volume = 0.6, returned_lots = 30, returned_share = 0.667, executions = 9, window = 120.0.
- Evidence: 9 events of the match stream (sequence numbers 317092, 318314, 320443, 321365, 323963, 325070, 326154, 358019 ...).
- Modelled on: CEA-4c(a)(1), SEA-9(a), CME-534, FINRA-5210, MAR-12.

### HIGH: Wash trading (MKT-02), WashB

Bought 30 and sold 60 lots against WashA in 9 executions over 116 s. Trading between the two made up at least 60% of each one's volume in that time, and 30 lots were handed straight back at a matching price.

- Alert `MKT-02-595f0a97d6f4`, from 463.99 s to 580.02 s of the match.
- Counterparty: WashA.
- Measured: lots_bought = 30, lots_sold = 60, imbalance = 0.333, pair_share_of_volume = 0.6, returned_lots = 30, returned_share = 0.667, executions = 9, window = 120.0.
- Evidence: 9 events of the match stream (sequence numbers 317092, 318314, 320443, 321365, 323963, 325070, 326154, 358019 ...).
- Modelled on: CEA-4c(a)(1), SEA-9(a), CME-534, FINRA-5210, MAR-12.

### HIGH: Wash trading (MKT-02), WashA

Bought 110 and sold 110 lots against WashB in 22 executions over 116 s. Trading between the two made up at least 76% of each one's volume in that time, and 60 lots were handed straight back at a matching price.

- Alert `MKT-02-418ca71ef649`, from 593.99 s to 710.00 s of the match.
- Counterparty: WashB.
- Measured: lots_bought = 110, lots_sold = 110, imbalance = 0.0, pair_share_of_volume = 0.759, returned_lots = 60, returned_share = 0.545, executions = 22, window = 120.0.
- Evidence: 22 events of the match stream (sequence numbers 398956, 403934, 405346, 406264, 407436, 411150, 413180, 415861 ...).
- Modelled on: CEA-4c(a)(1), SEA-9(a), CME-534, FINRA-5210, MAR-12.

### HIGH: Wash trading (MKT-02), WashB

Bought 110 and sold 110 lots against WashA in 22 executions over 116 s. Trading between the two made up at least 76% of each one's volume in that time, and 60 lots were handed straight back at a matching price.

- Alert `MKT-02-4c843c5dc770`, from 593.99 s to 710.00 s of the match.
- Counterparty: WashA.
- Measured: lots_bought = 110, lots_sold = 110, imbalance = 0.0, pair_share_of_volume = 0.759, returned_lots = 60, returned_share = 0.545, executions = 22, window = 120.0.
- Evidence: 22 events of the match stream (sequence numbers 398956, 403934, 405346, 406264, 407436, 411150, 413180, 415861 ...).
- Modelled on: CEA-4c(a)(1), SEA-9(a), CME-534, FINRA-5210, MAR-12.

### HIGH: Wash trading (MKT-02), WashA

Bought 60 and sold 70 lots against WashB in 13 executions over 98 s. Trading between the two made up at least 61% of each one's volume in that time, and 30 lots were handed straight back at a matching price.

- Alert `MKT-02-f487657ecfdd`, from 734.04 s to 832.02 s of the match.
- Counterparty: WashB.
- Measured: lots_bought = 60, lots_sold = 70, imbalance = 0.077, pair_share_of_volume = 0.61, returned_lots = 30, returned_share = 0.462, executions = 13, window = 120.0.
- Evidence: 13 events of the match stream (sequence numbers 503343, 515307, 529354, 530972, 540467, 542869, 544901, 546831 ...).
- Modelled on: CEA-4c(a)(1), SEA-9(a), CME-534, FINRA-5210, MAR-12.

### HIGH: Wash trading (MKT-02), WashB

Bought 70 and sold 60 lots against WashA in 13 executions over 98 s. Trading between the two made up at least 61% of each one's volume in that time, and 30 lots were handed straight back at a matching price.

- Alert `MKT-02-978ad864cd06`, from 734.04 s to 832.02 s of the match.
- Counterparty: WashA.
- Measured: lots_bought = 70, lots_sold = 60, imbalance = 0.077, pair_share_of_volume = 0.61, returned_lots = 30, returned_share = 0.462, executions = 13, window = 120.0.
- Evidence: 13 events of the match stream (sequence numbers 503343, 515307, 529354, 530972, 540467, 542869, 544901, 546831 ...).
- Modelled on: CEA-4c(a)(1), SEA-9(a), CME-534, FINRA-5210, MAR-12.

### HIGH: Marking the close (MKT-04), Closer

In the last 4.0 s, 17 aggressive orders traded up to 7.0 ticks through fair value, and the last of them set the closing price, which marks a position of +57 lots $114.00 in the participant's favour.

- Alert `MKT-04-71a549bdc3f4`, from 895.98 s to 900.00 s of the match.
- Measured: orders = 17, executions = 17, lots = 17, max_ticks_through_fair = 7.0, closing_position = 57, closing_mark = 134500, future_price = 134300, mark_benefit = 114.0, benefit_aimed_at = 114.0, set_the_closing_price = True, closing_window = 30.0.
- Evidence: 17 events of the match stream (sequence numbers 649305, 649674, 649911, 650213, 650480, 650671, 650808, 650969 ...).
- Modelled on: CEA-4c(a)(5), SEA-9(a), CFTC-180, MAR-12.

### MEDIUM: Quote stuffing (MKT-03), Stuffer

5 bursts of up to 32 order messages a second in 5 s, in which orders were cancelled within 0.5 s of entry and none traded.

- Alert `MKT-03-04d7aa7f04bb`, from 1.48 s to 6.75 s of the match.
- Measured: bursts = 5, peak_messages_per_second = 32, orders_flashed = 54, window = 60.0.
- Evidence: 5 events of the match stream (sequence numbers 1841, 2506, 3446, 4352, 5015).
- Modelled on: CME-575, MAR-12, MIFID2-RTS6, CEA-4c(a)(5).

### MEDIUM: Order-to-trade ratio (MKT-05), Stuffer

21251 order messages against 8 executed orders: a ratio of 2,655.4 by number (limit 150) and 2,655.4 by volume (limit 400).

- Alert `MKT-05-45e986428151`, from 1.76 s to 900.00 s of the match.
- Measured: order_messages = 21251, executed_orders = 8, order_message_lots = 21251, executed_lots = 8, ratio_by_number = 2655.38, ratio_by_volume = 2655.38, limit_by_number = 150.0, limit_by_volume = 400.0.
- Modelled on: MIFID2-RTS9, MIFID2-RTS6, CME-575.

### MEDIUM: Quote stuffing (MKT-03), Stuffer

52 bursts of up to 41 order messages a second in 61 s, in which orders were cancelled within 0.5 s of entry and none traded.

- Alert `MKT-03-7ba701cf448a`, from 6.79 s to 67.74 s of the match.
- Measured: bursts = 52, peak_messages_per_second = 41, orders_flashed = 620, window = 60.0.
- Evidence: 52 events of the match stream (sequence numbers 5720, 6460, 7897, 9130, 11393, 12828, 13879, 15503 ...).
- Modelled on: CME-575, MAR-12, MIFID2-RTS6, CEA-4c(a)(5).

### MEDIUM: Un-hedged exposure: time limit approaching (LIM-02), Reckless

Un-hedged for 45 s of the 60 s allowed (30 lots against an allowance of 10).

- Alert `LIM-02-6e6c619e8c17`, from 9.77 s to 54.84 s of the match.
- Measured: unhedged_lots = 30, peak_unhedged_lots = 30, allowance_lots = 10, seconds = 45.063, time_limit = 60.0.
- Evidence: 1 event of the match stream (sequence numbers 7527).
- Modelled on: SEC-15c3-5, MIFID2-RTS6, CFTC-38.251.

### MEDIUM: Quote stuffing (MKT-03), Stuffer

38 bursts of up to 41 order messages a second in 61 s, in which orders were cancelled within 0.5 s of entry and none traded.

- Alert `MKT-03-1fdff340005c`, from 67.79 s to 128.48 s of the match.
- Measured: bursts = 38, peak_messages_per_second = 41, orders_flashed = 439, window = 60.0.
- Evidence: 38 events of the match stream (sequence numbers 54242, 55008, 55609, 56050, 56578, 57114, 57559, 58126 ...).
- Modelled on: CME-575, MAR-12, MIFID2-RTS6, CEA-4c(a)(5).

### MEDIUM: Quote stuffing (MKT-03), Stuffer

24 bursts of up to 41 order messages a second in 60 s, in which orders were cancelled within 0.5 s of entry and none traded.

- Alert `MKT-03-3aadc6c865a6`, from 128.51 s to 188.74 s of the match.
- Measured: bursts = 24, peak_messages_per_second = 41, orders_flashed = 293, window = 60.0.
- Evidence: 24 events of the match stream (sequence numbers 92342, 93216, 94846, 95786, 96779, 98772, 100262, 111590 ...).
- Modelled on: CME-575, MAR-12, MIFID2-RTS6, CEA-4c(a)(5).

### MEDIUM: Quote stuffing (MKT-03), Stuffer

37 bursts of up to 41 order messages a second in 59 s, in which orders were cancelled within 0.5 s of entry and none traded.

- Alert `MKT-03-830c9a2da8ed`, from 189.52 s to 248.75 s of the match.
- Measured: bursts = 37, peak_messages_per_second = 41, orders_flashed = 444, window = 60.0.
- Evidence: 37 events of the match stream (sequence numbers 133486, 134441, 139342, 144371, 144830, 147567, 148806, 149549 ...).
- Modelled on: CME-575, MAR-12, MIFID2-RTS6, CEA-4c(a)(5).

### MEDIUM: Quote stuffing (MKT-03), Stuffer

50 bursts of up to 41 order messages a second in 60 s, in which orders were cancelled within 0.5 s of entry and none traded.

- Alert `MKT-03-8385b4dee345`, from 248.80 s to 308.78 s of the match.
- Measured: bursts = 50, peak_messages_per_second = 41, orders_flashed = 586, window = 60.0.
- Evidence: 50 events of the match stream (sequence numbers 170439, 171357, 172171, 173233, 174181, 175004, 175545, 176545 ...).
- Modelled on: CME-575, MAR-12, MIFID2-RTS6, CEA-4c(a)(5).

### MEDIUM: Quote stuffing (MKT-03), Stuffer

7 bursts of up to 41 order messages a second in 41 s, in which orders were cancelled within 0.5 s of entry and none traded.

- Alert `MKT-03-657a25f93c51`, from 350.77 s to 392.02 s of the match.
- Measured: bursts = 7, peak_messages_per_second = 41, orders_flashed = 80, window = 60.0.
- Evidence: 7 events of the match stream (sequence numbers 232083, 232570, 233364, 234150, 234830, 235628, 264182).
- Modelled on: CME-575, MAR-12, MIFID2-RTS6, CEA-4c(a)(5).

### MEDIUM: Quote stuffing (MKT-03), Stuffer

34 bursts of up to 41 order messages a second in 60 s, in which orders were cancelled within 0.5 s of entry and none traded.

- Alert `MKT-03-c231977fae9c`, from 396.79 s to 457.29 s of the match.
- Measured: bursts = 34, peak_messages_per_second = 41, orders_flashed = 408, window = 60.0.
- Evidence: 34 events of the match stream (sequence numbers 268431, 269495, 270015, 270861, 271577, 272923, 274279, 274936 ...).
- Modelled on: CME-575, MAR-12, MIFID2-RTS6, CEA-4c(a)(5).

### MEDIUM: Quote stuffing (MKT-03), Stuffer

45 bursts of up to 37 order messages a second in 61 s, in which orders were cancelled within 0.5 s of entry and none traded.

- Alert `MKT-03-ea7a9193b347`, from 457.52 s to 518.51 s of the match.
- Measured: bursts = 45, peak_messages_per_second = 37, orders_flashed = 508, window = 60.0.
- Evidence: 45 events of the match stream (sequence numbers 313877, 314416, 315030, 315786, 316408, 316930, 317592, 318305 ...).
- Modelled on: CME-575, MAR-12, MIFID2-RTS6, CEA-4c(a)(5).

### MEDIUM: Quote stuffing (MKT-03), Stuffer

23 bursts of up to 34 order messages a second in 60 s, in which orders were cancelled within 0.5 s of entry and none traded.

- Alert `MKT-03-ec84794ecbb7`, from 518.74 s to 579.22 s of the match.
- Measured: bursts = 23, peak_messages_per_second = 34, orders_flashed = 254, window = 60.0.
- Evidence: 23 events of the match stream (sequence numbers 353585, 354453, 355066, 355433, 355767, 356437, 357736, 358024 ...).
- Modelled on: CME-575, MAR-12, MIFID2-RTS6, CEA-4c(a)(5).

### MEDIUM: Quote stuffing (MKT-03), Stuffer

46 bursts of up to 41 order messages a second in 60 s, in which orders were cancelled within 0.5 s of entry and none traded.

- Alert `MKT-03-291b6984bd5e`, from 579.27 s to 639.49 s of the match.
- Measured: bursts = 46, peak_messages_per_second = 41, orders_flashed = 550, window = 60.0.
- Evidence: 46 events of the match stream (sequence numbers 390242, 391267, 391919, 393077, 393582, 394322, 394934, 395475 ...).
- Modelled on: CME-575, MAR-12, MIFID2-RTS6, CEA-4c(a)(5).

### MEDIUM: Quote stuffing (MKT-03), Stuffer

26 bursts of up to 34 order messages a second in 61 s, in which orders were cancelled within 0.5 s of entry and none traded.

- Alert `MKT-03-45adaa6c3540`, from 639.50 s to 700.46 s of the match.
- Measured: bursts = 26, peak_messages_per_second = 34, orders_flashed = 289, window = 60.0.
- Evidence: 26 events of the match stream (sequence numbers 426509, 426996, 427959, 428637, 432289, 433150, 437078, 437584 ...).
- Modelled on: CME-575, MAR-12, MIFID2-RTS6, CEA-4c(a)(5).

### MEDIUM: Quote stuffing (MKT-03), Stuffer

35 bursts of up to 41 order messages a second in 60 s, in which orders were cancelled within 0.5 s of entry and none traded.

- Alert `MKT-03-f8b0a7450807`, from 701.55 s to 761.55 s of the match.
- Measured: bursts = 35, peak_messages_per_second = 41, orders_flashed = 403, window = 60.0.
- Evidence: 35 events of the match stream (sequence numbers 479413, 480383, 481267, 482138, 482892, 483464, 484170, 484866 ...).
- Modelled on: CME-575, MAR-12, MIFID2-RTS6, CEA-4c(a)(5).

### MEDIUM: Quote stuffing (MKT-03), Stuffer

34 bursts of up to 37 order messages a second in 60 s, in which orders were cancelled within 0.5 s of entry and none traded.

- Alert `MKT-03-4792970ec43c`, from 761.75 s to 822.00 s of the match.
- Measured: bursts = 34, peak_messages_per_second = 37, orders_flashed = 382, window = 60.0.
- Evidence: 34 events of the match stream (sequence numbers 520392, 520992, 521397, 523970, 528783, 529854, 530653, 538650 ...).
- Modelled on: CME-575, MAR-12, MIFID2-RTS6, CEA-4c(a)(5).

### MEDIUM: Quote stuffing (MKT-03), Stuffer

47 bursts of up to 41 order messages a second in 61 s, in which orders were cancelled within 0.5 s of entry and none traded.

- Alert `MKT-03-e9ac20d34e52`, from 822.23 s to 883.00 s of the match.
- Measured: bursts = 47, peak_messages_per_second = 41, orders_flashed = 552, window = 60.0.
- Evidence: 47 events of the match stream (sequence numbers 574284, 575403, 576786, 578459, 579689, 580660, 582016, 583605 ...).
- Modelled on: CME-575, MAR-12, MIFID2-RTS6, CEA-4c(a)(5).

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-16476201ce2c`, from 1.98 s to 2.79 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 2084).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-ec7479757938`, from 32.50 s to 33.49 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 31292).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-7a91146133db`, from 62.53 s to 63.53 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 50441).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-5bb7d3c54156`, from 93.00 s to 93.95 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 67873).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-f6823cfebf6c`, from 125.27 s to 126.27 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 89949).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-5ebd2613451c`, from 170.51 s to 171.51 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 121541).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-635849443375`, from 204.20 s to 205.02 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 144502).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-9f928dbd104c`, from 234.20 s to 235.03 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 161403).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-18f2685f207b`, from 265.23 s to 266.05 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 182471).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-677951ffd0f5`, from 295.48 s to 296.23 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 199533).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-9dbc3b064e4e`, from 325.49 s to 326.47 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 218046).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-ca408911add3`, from 355.52 s to 356.50 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 234749).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-28bfdbe2c497`, from 391.49 s to 392.46 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 264311).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-b7115b90146e`, from 422.00 s to 422.76 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 286084).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-fff5177cb0a7`, from 456.79 s to 457.78 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 313445).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-bcd85179cf1c`, from 489.72 s to 490.55 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 335157).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-34552f20615b`, from 520.02 s to 521.02 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 354529).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-bfdf79e97d94`, from 575.53 s to 576.47 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 388271).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-c7094bf621c8`, from 605.99 s to 606.79 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 405806).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

41 order messages in 1 s, 82% of the limit of 50.

- Alert `LIM-03-52e3704fd569`, from 636.04 s to 637.02 s of the match.
- Measured: messages = 41, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 424108).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-0ac73f43b6f9`, from 667.03 s to 667.96 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 445749).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-8d7f1e25e6bf`, from 697.53 s to 698.51 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 475977).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-e3f7f6762bc3`, from 732.75 s to 733.51 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 502855).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-b636a9a56f0c`, from 763.51 s to 764.29 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 521181).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

41 order messages in 1 s, 82% of the limit of 50.

- Alert `LIM-03-7e6cd721609c`, from 793.50 s to 794.48 s of the match.
- Measured: messages = 41, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 547117).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-bc3166b4b85f`, from 823.71 s to 824.71 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 575753).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-145865519035`, from 853.80 s to 854.80 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 612470).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

### LOW: Message rate: limit approaching (LIM-03), Stuffer

40 order messages in 1 s, 80% of the limit of 50.

- Alert `LIM-03-21797e48a9d9`, from 884.26 s to 885.23 s of the match.
- Measured: messages = 40, limit = 50, interval = 1.0.
- Evidence: 1 event of the match stream (sequence numbers 640108).
- Modelled on: MIFID2-RTS6, CFTC-38.251, CME-575.

## Controls run

| Control | What it watches for | Alerts | Modelled on |
|---|---|---:|---|
| LIM-01 | Position limit | 0 | CFTC-150, SEC-15c3-5, MIFID2-RTS6, CFTC-38.251 |
| LIM-02 | Un-hedged exposure | 2 | SEC-15c3-5, MIFID2-RTS6, CFTC-38.251 |
| LIM-03 | Message rate | 28 | MIFID2-RTS6, CFTC-38.251, CME-575 |
| MKT-01 | Spoofing and layering | 5 | CEA-4c(a)(5), CME-575, FINRA-5210, MAR-12, SEC-10b-5 |
| MKT-02 | Wash trading | 14 | CEA-4c(a)(1), SEA-9(a), CME-534, FINRA-5210, MAR-12 |
| MKT-03 | Quote stuffing | 15 | CME-575, MAR-12, MIFID2-RTS6, CEA-4c(a)(5) |
| MKT-04 | Marking the close | 1 | CEA-4c(a)(5), SEA-9(a), CFTC-180, MAR-12 |
| MKT-05 | Order-to-trade ratio | 1 | MIFID2-RTS9, MIFID2-RTS6, CME-575 |
| MKT-06 | Off-market order | 0 | SEC-15c3-5, MIFID2-RTS6, CFTC-38.251 |
| REC-01 | Trade reconciliation | 0 | SEC-17a-4, CFTC-1.31, SEC-613, FINRA-3110 |

## Regulations cited

- **CEA-4c(a)(1)**, Commodity Exchange Act, wash sales and fictitious trades (7 U.S.C. 6c(a)(1)-(2); CFTC). Prohibits transactions that are, or are commonly known as, wash sales or accommodation trades, and fictitious sales. <https://www.law.cornell.edu/uscode/text/7/6c>
- **CEA-4c(a)(5)**, Commodity Exchange Act, disruptive practices (7 U.S.C. 6c(a)(5) (added by Dodd-Frank Act section 747); CFTC). Makes it unlawful to violate bids or offers, to show intentional or reckless disregard for the orderly execution of transactions during the closing period, or to spoof ("bidding or offering with the intent to cancel the bid or offer before execution"). <https://www.law.cornell.edu/uscode/text/7/6c>
- **CFTC-180**, Prohibition on manipulative and deceptive devices and price manipulation (17 CFR 180.1 and 180.2 (under 7 U.S.C. 9); CFTC). Prohibits the use of any manipulative device, scheme or artifice to defraud, and the manipulation or attempted manipulation of the price of any swap, commodity or future. <https://www.ecfr.gov/current/title-17/chapter-I/part-180>
- **CFTC-38.251**, Electronic trading risk principles for designated contract markets (17 CFR 38.251(e)-(g) and 38.255; CFTC). Requires a contract market to adopt rules and pre-trade risk controls that prevent, detect and mitigate market disruptions and system anomalies associated with electronic trading. <https://www.ecfr.gov/current/title-17/section-38.251>
- **CME-534**, Wash trades prohibited (CME Group Rule 534 (CME Rulebook, chapter 5); CME Group). Prohibits placing or accepting buy and sell orders in the same product for accounts with common beneficial ownership where the orders are expected to match each other. <https://www.cmegroup.com/rulebook/CME/I/5/5.pdf>
- **CME-575**, Disruptive practices prohibited (CME Group Rule 575 (CME Rulebook, chapter 5); CME Group). All orders must be entered for the purpose of executing bona fide transactions; prohibits orders entered to mislead, to overload or delay systems, or to disrupt orderly trading. <https://www.cmegroup.com/rulebook/CME/I/5/5.pdf>
- **FINRA-5210**, Publication of transactions and quotations (FINRA Rule 5210, Supplementary Material .02 (self-trades) and .03 (disruptive quoting and trading); FINRA). Prohibits publishing quotations or transactions that are not bona fide. The supplementary material covers self-trades from related algorithms and the entry of orders on one side to move the market before trading on the other. <https://www.finra.org/rules-guidance/rulebooks/finra-rules/5210>
- **MAR-12**, Market Abuse Regulation, market manipulation (Regulation (EU) No 596/2014, Articles 12 and 15, with Annex II of Delegated Regulation (EU) 2016/522; ESMA and national competent authorities). Defines and prohibits market manipulation. The delegated regulation lists the practices that indicate it, among them layering and spoofing, quote stuffing, wash trades, momentum ignition and marking the close. <https://eur-lex.europa.eu/eli/reg/2014/596/oj>
- **MIFID2-RTS6**, Organisational requirements for algorithmic trading (Directive 2014/65/EU Article 17, with Delegated Regulation (EU) 2017/589 (RTS 6); ESMA and national competent authorities). Requires pre-trade controls (price collars, maximum order value and volume, maximum message limits), real-time monitoring, post-trade controls and a kill function for algorithmic trading, plus automated surveillance for market manipulation. <https://eur-lex.europa.eu/eli/reg_del/2017/589/oj>
- **MIFID2-RTS9**, Ratio of unexecuted orders to transactions (Directive 2014/65/EU Article 48(6), with Delegated Regulation (EU) 2017/566 (RTS 9); ESMA and national competent authorities). Requires trading venues to calculate, for each member, the ratio of unexecuted orders to transactions both by number and by volume, and to set maximum ratios. <https://eur-lex.europa.eu/eli/reg_del/2017/566/oj>
- **SEA-9(a)**, Securities Exchange Act, manipulation of security prices (15 U.S.C. 78i(a)(1)-(2); SEC). Prohibits wash sales and matched orders entered to create a false or misleading appearance of active trading, and series of transactions that raise or depress a price to induce others to trade. <https://www.law.cornell.edu/uscode/text/15/78i>
- **SEC-10b-5**, Employment of manipulative and deceptive devices (17 CFR 240.10b-5 (under 15 U.S.C. 78j(b)); SEC). The general anti-fraud rule: prohibits any device, scheme or act that operates as a fraud or deceit in connection with the purchase or sale of a security. <https://www.ecfr.gov/current/title-17/section-240.10b-5>
- **SEC-15c3-5**, Market Access Rule (17 CFR 240.15c3-5; SEC). Requires a broker-dealer with market access to maintain risk management controls that prevent orders exceeding pre-set credit or capital thresholds and reject erroneous orders (by price or size), and to review their effectiveness regularly. <https://www.ecfr.gov/current/title-17/section-240.15c3-5>

Ready Trader Go is a simulator. The regulations above are the real-world obligations each control is modelled on; citing them is a cross-reference, not a legal finding.

## Provenance

|  |  |
|---|---|
| Report fingerprint (SHA-256 of `report.json`) | `899390c1b7bf7339b520018762da8fa415d161a6627f739108d52cd7a83734e8` |
| Merkle root of the alerts | `3988a684e5a519c623dee6c422f8b958a13311416d15bf4e44d8f940b025df72` |
| Event stream (SHA-256 of `match_events.csv`) | `720b4cb35b42b1dd27f5de9e3738a78c6fd70ef198bed06f3cec0a70ba309740` |
| Market data | `693f4af58c6fb85921746ee3d667c1f2826dc45838cd2020e83d395fcd4e74b8` |
| Rulebook | `b880315e61d29a9c77444d4de5762b66e528b81aab04d5f828eb73d65e272b4b` |

Not yet anchored.
