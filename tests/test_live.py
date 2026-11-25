"""The real exchange, end to end. Takes about half a minute; opt in with VIGIL_TEST_LIVE=1."""
from __future__ import annotations

import hashlib
import json

import pytest

from vigil.live import LiveMatch, entries, reaction_times
from vigil.pipeline import audit_records
from vigil.reports.builder import outcomes_from_score_board

pytestmark = pytest.mark.live


def test_a_match_on_the_real_exchange_is_audited_as_it_runs(session_one, tmp_path):
    traders = {"VigilFull": "preset:full", "Example": "example", "Spoofer": "actor:spoofer"}
    with LiveMatch(session_one, traders, match_id="pytest-live", seconds=25.0, run_dir=tmp_path / "run") as match:
        audit = audit_records(match.header(), match.events(),
                              outcomes=lambda: outcomes_from_score_board(match.score_board_path))
        assert match.wait(30) == 0
    document = audit.report.document
    # What was audited is, byte for byte, the record the exchange wrote.
    assert document["match"]["events_sha256"] == hashlib.sha256(match.match_events_path.read_bytes()).hexdigest()
    assert document["summary"]["integrity"]["discrepancies"] == 0
    assert document["summary"]["integrity"]["trade_reports_checked"] > 20
    for name, entry in document["participants"].items():
        assert entry["exchange"]["status"] == "OK"
        # Our independent P&L against the exchange's own score board.
        assert entry["metrics"]["profit_or_loss"] == pytest.approx(entry["exchange"]["profit_or_loss"], abs=0.01), name
    assert document["participants"]["VigilFull"]["alerts"] == 0
    assert entries(match.run_dir) == traders  # the run directory says what each team was
    # Every hedge was at a price the replica's future book gives, and the positions the stream
    # adds up to are the score board's.
    integrity = document["summary"]["integrity"]
    assert integrity["hedges_checked"] > 0 and integrity["scores_checked"] == len(traders)
    assert match.departures() == {}  # nobody left before the end
    # Each team answered order book updates, in something under a few milliseconds.
    timing = reaction_times(match.run_dir)
    assert {"VigilFull", "Example"} <= set(timing)
    assert all(0 < entry["median"] < 0.01 and entry["ticks"] > 5 for entry in timing.values())
    # The settings a preset was started with name the rules of the exchange it was started on.
    settings = json.loads((match.run_dir / "trader_vigilfull.strategy.json").read_text())
    assert settings["position_limit"] == 100 and settings["speed"] == 1.0 and len(settings["holdout"]) == 16


def test_a_trader_that_cannot_be_loaded_is_reported_as_gone(session_one, tmp_path):
    broken = tmp_path / "broken.py"
    broken.write_text('raise RuntimeError("this trader cannot even be loaded")\n')
    traders = {"Example": "example", "Broken": str(broken)}
    with LiveMatch(session_one, traders, match_id="pytest-broken", seconds=6.0, run_dir=tmp_path / "run",
                   open_delay=2.0) as match:
        records = list(match.events())
        assert match.wait(30) == 0
    assert records and {record.competitor for record in records} <= {"", "Example"}
    departures = match.departures()
    assert set(departures) == {"Broken"} and departures["Broken"]["failed"]
    assert "cannot even be loaded" in departures["Broken"]["last_words"]


def test_a_match_that_cannot_start_is_reported_not_waited_for(session_one, tmp_path):
    """Market data the exchange cannot read leaves it waiting for ever; the runner must not wait with it."""
    match = LiveMatch(session_one, {"Example": "example"}, match_id="pytest-stuck", seconds=5.0,
                      run_dir=tmp_path / "run", open_delay=1.0)
    with match:
        match.market_data.unlink()
        match.deadline = 0.0  # as if the match were long overdue
        with pytest.raises(TimeoutError):
            list(match.events())
    assert not match.running()
