"""Following the exchange's event file as it is written. No exchange is started here: the tests write
the file themselves, in the awkward ways a real one can."""
from __future__ import annotations

import math

from vigil.live import LiveMatch
from vigil.rtg.events import CSV_HEADER

HEADER = ",".join(CSV_HEADER).encode() + b"\r\n"


def row(number: int, team: str = "Team") -> bytes:
    return f"{number / 100:.6f},{team},Insert,{number},1,B,10,100000,G,\r\n".encode()


class Exchange:
    """A stand-in for the exchange's process: alive until told otherwise, and doing something as it goes."""

    def __init__(self, on_exit=None):
        self.alive = True
        self.on_exit = on_exit
        self.returncode = None

    def poll(self):
        if self.alive and self.on_exit is not None:
            self.on_exit()  # its last act, which the caller sees only as the process having gone
            self.alive = False
            self.returncode = 0
        return None if self.alive else 0


def tailing(tmp_path, exchange) -> LiveMatch:
    match = LiveMatch("unused.csv", {}, match_id="tail", run_dir=tmp_path)
    match._exchange = exchange
    match.deadline = math.inf
    return match


def test_rows_written_as_the_exchange_exits_are_not_lost(tmp_path):
    path = tmp_path / "match_events.csv"
    path.write_bytes(HEADER + b"".join(row(n) for n in range(100)))

    def last_flush():
        with open(path, "ab") as handle:
            handle.write(b"".join(row(n) for n in range(100, 150)))

    # The exchange flushes its final buffer and exits between the tailer finding nothing more
    # to read and asking whether the exchange is still there.
    match = tailing(tmp_path, Exchange(on_exit=last_flush))
    records = list(match.events(poll=0.0))
    assert [record.order_id for record in records] == list(range(150))
    assert [record.seq for record in records] == list(range(150))


def test_a_line_or_a_character_split_across_two_reads_is_put_back_together(tmp_path):
    path = tmp_path / "match_events.csv"
    name = "Zürich-東京"  # a team name of several bytes a character
    whole = HEADER + b"".join(row(n, name) for n in range(3))
    # Stop in the middle of the second row, halfway through a character of the team name.
    cut = whole.index(name.encode(), len(HEADER) + len(row(0, name))) + len("Zü".encode()) - 1
    path.write_bytes(whole[:cut])

    def the_rest():
        with open(path, "ab") as handle:
            handle.write(whole[cut:])

    match = tailing(tmp_path, Exchange(on_exit=the_rest))
    records = list(match.events(poll=0.0))
    assert [(record.order_id, record.competitor) for record in records] == [(0, name), (1, name), (2, name)]


def test_the_exchanges_display_is_given_a_port_only_when_it_is_wanted(session_one, tmp_path):
    import json
    for hud in (False, True):
        match = LiveMatch(session_one, {"Example": "example"}, match_id="hud", seconds=5.0, run_dir=tmp_path / str(hud),
                          hud=hud)
        match._prepare()
        config = json.loads((match.run_dir / "exchange.json").read_text())
        assert ("Hud" in config) is hud
        if hud:
            assert config["Hud"]["Host"] == "127.0.0.1" and config["Hud"]["Port"] != config["Execution"]["Port"]


def test_a_last_line_the_exchange_never_finished_is_left_out(tmp_path):
    path = tmp_path / "match_events.csv"
    path.write_bytes(HEADER + row(0) + row(1)[:-10])
    exchange = Exchange()
    exchange.alive = False
    records = list(tailing(tmp_path, exchange).events(poll=0.0))
    assert [record.order_id for record in records] == [0]
