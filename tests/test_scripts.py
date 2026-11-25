"""The scripts that write the results data, and the documents rendered from it."""
from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

from vigil.live import FINGERPRINT_FILE, LiveMatch, traders_fingerprint

ROOT = Path(__file__).resolve().parents[1]


def script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def run_dir(capture, session_one, tmp_path) -> Path:
    """The captured match, laid out as the run directory ``vigil live`` left when it was played."""
    match = LiveMatch(session_one, {"VigilFull": "preset:full", "Example": "example"}, match_id="capture",
                      seconds=20.0, run_dir=tmp_path / "capture")
    match._prepare()
    for name in ("match_events.csv", "score_board.csv"):
        shutil.copy(capture / name, match.run_dir / name)
    return match.run_dir


def test_a_run_directory_says_which_traders_played(run_dir, monkeypatch, tmp_path):
    assert (run_dir / FINGERPRINT_FILE).read_text().strip() == traders_fingerprint()
    # Another model is another trader, whatever the code.
    other = tmp_path / "model.json"
    other.write_text("{}")
    monkeypatch.setenv("VIGIL_MODEL", str(other))
    assert traders_fingerprint() != (run_dir / FINGERPRINT_FILE).read_text().strip()


def test_a_match_is_simulated_with_the_traders_that_played_it(run_dir):
    recorder = script("record_live_match")
    first = recorder.record(run_dir, "as it was played")
    assert first["discrepancies"] == 0 and first["scores_checked"] == 2
    assert first["played_with"] == first["simulated_with"] == traders_fingerprint()
    assert first["simulated_as_played"] is True and first["simulator_seeds"] == list(recorder.SEEDS)
    for team in first["teams"].values():
        assert team["exchange_pnl"] == pytest.approx(team["audit_pnl"], abs=0.005)
        assert len(team["simulated_pnl"]) == len(team["simulated_pnl_without_reaction_times"]) == len(recorder.SEEDS)

    # Later the traders are changed. Simulating the match again would play the new ones against a
    # match the old ones played, so what is on record is kept, and keeps saying whose it is.
    (run_dir / FINGERPRINT_FILE).write_text("0" * 64 + "\n")
    kept = recorder.record(run_dir, previous={**first, "played_with": "0" * 64, "simulated_with": "0" * 64})
    assert kept["simulated_with"] == "0" * 64 and kept["simulated_as_played"] is True
    assert kept["note"] == "as it was played"
    assert all(kept["teams"][name]["simulated_pnl"] == team["simulated_pnl"] for name, team in first["teams"].items())

    # With nothing on record there is only the new traders to simulate, and the entry says that is what they are.
    fresh = recorder.record(run_dir)
    assert fresh["simulated_with"] == traders_fingerprint() and fresh["simulated_as_played"] is False

    # A run directory from before the fingerprint was kept cannot say; whoever records it can.
    (run_dir / FINGERPRINT_FILE).unlink()
    assert recorder.record(run_dir)["simulated_as_played"] is None
    assert recorder.record(run_dir, as_played=True)["simulated_as_played"] is True
    held = recorder.record(run_dir, previous=first, keep_simulation=True, as_played=False)
    assert held["simulated_as_played"] is False and held["simulated_with"] == first["simulated_with"]


def test_the_results_page_says_whose_traders_a_simulation_used():
    caveat = script("render_docs").simulation_caveat
    today = traders_fingerprint()
    assert caveat({"simulated_as_played": True, "simulated_with": today}, today) == ""
    assert "cannot be regenerated" in caveat({"simulated_as_played": True, "simulated_with": "earlier"}, today)
    assert "do not compare like with like." in caveat({"simulated_as_played": False, "simulated_with": today}, today)
    assert "changed again" in caveat({"simulated_as_played": False, "simulated_with": None}, today)
    assert "not recorded" in caveat({"simulator_seeds": [0]}, today)


def test_the_generated_pages_are_what_the_data_renders_to():
    """docs/RESULTS.md and docs/CONTROLS.md are never edited by hand; if this fails, run scripts/render_docs.py."""
    render = script("render_docs")
    assert render.controls() == (ROOT / "docs" / "CONTROLS.md").read_text()
    assert render.results() == (ROOT / "docs" / "RESULTS.md").read_text()
