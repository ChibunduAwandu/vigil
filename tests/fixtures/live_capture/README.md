# A real Ready Trader Go match

These files record the output and configuration of the real exchange (`ready_trader_go.exchange`)
playing the first 20 seconds of `market_data1.csv` with two auto-traders: Vigil's full strategy
and the example trader that ships with Ready Trader Go. They were recorded with
`vigil.live.LiveMatch` and are what the tests use to hold the simulator and the surveillance
engine to the behaviour of the real thing.

- `exchange.json`: its configuration (the port and data path are placeholders)
- `match_events.csv`: every event of the match, as the exchange wrote it
- `score_board.csv`: each team's score over time, as the exchange wrote it

The input `market_data.csv` is not distributed here. The session-scoped `capture`
fixture in `tests/conftest.py` recreates its exact contents in a temporary directory
from the first 20 seconds of the separately installed `market_data1.csv`.
