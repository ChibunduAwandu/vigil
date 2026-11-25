"""Running a match on the real Ready Trader Go exchange, and watching it as it happens.

The simulator is for research; this is the real thing. ``LiveMatch`` starts
the unmodified exchange and one process per auto-trader, exactly as
``rtg.py run`` does, in a directory of its own. The exchange writes every
event to ``match_events.csv`` as the match proceeds, and ``events()`` follows
that file, so the surveillance engine sees the official record, byte for byte,
within a moment of each event.

Matches run in real time: a full session is fifteen minutes. For a quicker
look, ``seconds`` cuts the market data short and ``speed`` runs the clock fast.
With ``hud`` the exchange's own display (Ready Trader Go's heads-up display)
shows the match as it is played.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from pathlib import Path

from vigil.ml.model import DEFAULT_MODEL_PATH
from vigil.ml.train import session_key
from vigil.paths import REPO_ROOT, artifacts_dir, rtg_home
from vigil.rtg.events import CSV_HEADER, MatchHeader, MatchRecord
from vigil.rtg.marketdata import file_sha256
from vigil.strategies.badactors import BAD_ACTORS
from vigil.strategies.challengers import STRATEGIES
from vigil.surveillance.engine import header_for

SECRET = "secret"

# What can be entered for a team: a strategy preset, a misbehaving trader, the stock example,
# or a path to any Ready Trader Go autotrader.py.
KINDS = (*(f"preset:{name}" for name in STRATEGIES), *(f"actor:{name}" for name in BAD_ACTORS), "actor:wash_first",
         "actor:wash_second", "example")

FINGERPRINT_FILE = "traders.sha256"  # left in a run directory: which traders played the match


def traders_fingerprint() -> str:
    """One SHA-256 over what decides how the presets, the challengers and the example trader behave: their
    code and the parameters fitted for them.

    A match is played by the traders as they are on the day. ``LiveMatch``
    leaves this in the run directory, so that a simulation of the match made
    later can say whether it played the same traders.
    """
    from vigil.strategies.challengers import DEFAULT_PATH as CHALLENGER_PATH
    package = Path(__file__).resolve().parent
    files = [*(package / "strategies" / name for name in ("ensemble.py", "inventory.py", "kelly.py")),
             *(package / "ml" / name for name in ("adaboost.py", "features.py", "model.py")),
             Path(os.environ.get("VIGIL_MODEL") or DEFAULT_MODEL_PATH), rtg_home() / "autotrader.py",
             *sorted((package / "strategies" / "challengers").glob("*.py")),
             Path(os.environ.get("VIGIL_CHALLENGERS") or CHALLENGER_PATH)]
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest() if path.is_file() else b"absent")
    return digest.hexdigest()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _module_source(kind: str, settings_file: str | None) -> str:
    """The ``autotrader.py`` the exchange's launcher will import for a team."""
    if kind.startswith("preset:"):
        return ("import os\n"
                f"os.environ['VIGIL_STRATEGY'] = {settings_file!r}\n"
                "from vigil.strategies.ensemble import AutoTrader  # noqa: E402,F401\n")
    if kind in ("actor:wash_first", "actor:wash_second"):
        first = kind.endswith("first")
        return ("from vigil.strategies.badactors import WashTrader\n\n\n"
                "class AutoTrader(WashTrader):\n"
                "    def __init__(self, loop, team_name, secret):\n"
                f"        super().__init__(loop, team_name, secret, first={first})\n")
    if kind.startswith("actor:"):
        name = BAD_ACTORS[kind.split(":", 1)[1]].__name__
        return f"from vigil.strategies.badactors import {name} as AutoTrader  # noqa: F401\n"
    raise ValueError(f"unknown trader kind {kind!r}; choose from {', '.join(KINDS)} or a path to an autotrader.py")


def display_available() -> bool:
    """Is Ready Trader Go's display installed? It needs PySide6 (pip install -e ".[hud]")."""
    import importlib.util
    return importlib.util.find_spec("PySide6") is not None


def exchange_rules(config: dict) -> dict:
    """The strategy settings that restate an ``exchange.json``: the limits, fees and clock a trader must respect."""
    limits, engine = config["Limits"], config["Engine"]
    return {
        "position_limit": limits["PositionLimit"],
        "message_limit": limits["MessageFrequencyLimit"],
        "message_interval": limits["MessageFrequencyInterval"],
        "tick": int(config["Instrument"]["TickSize"] * 100.0),
        "maker_fee": config["Fees"]["Maker"],
        "taker_fee": config["Fees"]["Taker"],
        "book_interval": engine["TickInterval"],
        "speed": float(engine.get("Speed", 1.0)),
    }


def reaction_times(run_dir: str | os.PathLike, within: float = 0.02) -> dict[str, dict[str, float]]:
    """How long each team took to answer an order book update, measured from a finished match.

    The exchange publishes its books at each score tick. A team's reaction
    time is the delay from a tick to the first order message from that team
    the exchange acted on (an insert, a cancel or an amend: a hedge answers a
    fill, not the books), where that came within ``within`` seconds. Returned
    per team: its ``median``, its ``spread`` (half the distance between the
    16th and 84th percentiles) and the number of ``ticks`` it answered.

    Those are times at the exchange, which acts on one message at a time: a
    team that arrives second is timed after the first has been dealt with. So
    the medians of two teams can be close while one of them is ahead nearly
    every time, and it is who is ahead that decides who trades. ``typical``
    and ``jitter`` are a reaction time and its spread that reproduce how often
    each team was ahead of each other one: a location per team fitted to the
    pairwise frequencies (a Thurstone scale), in the units of the measured
    times. They are what a simulation should be given. ``ahead_of`` is those
    frequencies: for each other team, the share of the updates both answered
    on which this team's first order reached the exchange first.
    """
    import bisect
    from statistics import NormalDist

    import numpy as np

    from vigil.rtg.events import read_match_events
    run_dir = Path(run_dir)
    with open(run_dir / "score_board.csv", newline="") as handle:
        ticks = sorted({float(row["Time"]) for row in csv.DictReader(handle) if row["Operation"] == "Tick"})
    first: dict[str, dict[int, float]] = {}
    for record in read_match_events(run_dir / "match_events.csv"):
        if not record.competitor or record.op not in ("Insert", "Cancel", "Amend"):
            continue
        tick = bisect.bisect_right(ticks, record.time) - 1
        if tick >= 0 and record.time - ticks[tick] < within:
            first.setdefault(record.competitor, {}).setdefault(tick, record.time - ticks[tick])
    out = {}
    for team, delays in first.items():
        values = np.array(list(delays.values()))
        low, median, high = np.percentile(values, [16, 50, 84])
        out[team] = {"median": float(median), "spread": float((high - low) / 2.0), "ticks": len(values)}

    # How far apart two teams sit, in units of the noise, from how often one was ahead of the other:
    # if each answers after location + noise, P(a ahead of b) = Phi((location_b - location_a) / (sigma * sqrt 2)).
    teams = sorted(out)
    rows, targets, weights = [], [], []
    ahead_of: dict[str, dict[str, float]] = {team: {} for team in teams}
    for i, a in enumerate(teams):
        for j in range(i + 1, len(teams)):
            b = teams[j]
            shared = [tick for tick in first[a] if tick in first[b]]
            if len(shared) < 30:
                continue
            ahead = sum(first[a][tick] < first[b][tick] for tick in shared) / len(shared)
            ahead_of[a][b] = round(ahead, 4)
            ahead_of[b][a] = round(sum(first[b][tick] < first[a][tick] for tick in shared) / len(shared), 4)
            row = [0.0] * len(teams)
            row[i], row[j] = -1.0, 1.0
            rows.append(row)
            targets.append(NormalDist().inv_cdf(min(max(ahead, 0.01), 0.99)))
            weights.append(len(shared) ** 0.5)
    jitter = float(np.mean([entry["spread"] for entry in out.values()])) if out else 0.0
    centre = float(np.mean([entry["median"] for entry in out.values()])) if out else 0.0
    location = dict.fromkeys(teams, 0.0)
    if rows:
        design = np.array(rows) * np.array(weights)[:, None]
        design = np.vstack([design, np.ones(len(teams))])  # locations are relative: make them sum to zero
        solution = np.linalg.lstsq(design, np.append(np.array(targets) * np.array(weights), 0.0), rcond=None)[0]
        location = dict(zip(teams, solution.tolist(), strict=True))
    for team, entry in out.items():
        # One unit of location is sigma * sqrt 2; sigma is what the spread of the measured times says it is.
        entry["typical"] = max(centre + location[team] * jitter * 2 ** 0.5, 0.0001)
        entry["jitter"] = jitter
        entry["ahead_of"] = ahead_of[team]
    return out


def entries(run_dir: str | os.PathLike) -> dict[str, str]:
    """What each team of a match was entered as, read back from its run directory.

    The inverse of what ``LiveMatch`` writes there. A trader it cannot place,
    an autotrader.py of someone's own, comes back as ``"custom"``.
    """
    run_dir = Path(run_dir)
    example = (rtg_home() / "autotrader.py").read_text()
    actors = {_module_source(kind, None): kind for kind in KINDS if kind.startswith("actor:")}
    kinds = {}
    for name in json.loads((run_dir / "exchange.json").read_text())["Traders"]:
        module = LiveMatch.module_name(name)
        settings = run_dir / f"{module}.strategy.json"
        source = (run_dir / f"{module}.py").read_text()
        if settings.exists():
            kinds[name] = "preset:" + json.loads(settings.read_text())["preset"]
        elif source == example:
            kinds[name] = "example"
        else:
            kinds[name] = actors.get(source, "custom")
    return kinds


class LiveMatch:
    def __init__(self, session: str | os.PathLike, traders: Mapping[str, str], *, match_id: str,
                 run_dir: str | os.PathLike | None = None, speed: float = 1.0, seconds: float | None = None,
                 open_delay: float = 5.0, hud: bool = False):
        """``traders`` maps team names to kinds (see ``KINDS``); ``hud`` opens the exchange's display."""
        self.session = Path(session).resolve()
        self.traders = dict(traders)
        for name, kind in self.traders.items():
            if kind not in KINDS and not Path(kind).expanduser().is_file():
                raise ValueError(f"team {name}: {kind!r} is neither one of {', '.join(KINDS)} nor an autotrader.py")
        if len({self.module_name(name) for name in self.traders}) != len(self.traders):
            raise ValueError("team names must differ in more than case and punctuation")
        self.match_id = match_id
        self.speed = float(speed)
        self.seconds = seconds
        self.open_delay = open_delay
        self.hud = hud
        self._display: subprocess.Popen | None = None
        # Absolute: the exchange and the traders run with this directory as their working directory.
        self.run_dir = (Path(run_dir) if run_dir else artifacts_dir() / "runs" / match_id).resolve()
        self.port = _free_port()
        self.deadline: float | None = None  # monotonic time by which the match must be over
        self._exchange: subprocess.Popen | None = None
        self._processes: list[subprocess.Popen] = []
        self._left: dict[str, tuple[float, int]] = {}  # team -> (seconds after start, exit code), if it left early
        self._started = 0.0
        self._config: dict = {}

    # -- files ---------------------------------------------------------------------------------

    @property
    def match_events_path(self) -> Path:
        return self.run_dir / "match_events.csv"

    @property
    def score_board_path(self) -> Path:
        return self.run_dir / "score_board.csv"

    @property
    def exchange_json(self) -> Path:
        return self.run_dir / "exchange.json"

    def _prepare(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        for stale in ("match_events.csv", "score_board.csv", "info.dat"):
            (self.run_dir / stale).unlink(missing_ok=True)

        market_data = self.session
        if self.seconds is not None:
            market_data = self.run_dir / "market_data.csv"
            with open(self.session, newline="") as source, open(market_data, "w", newline="") as target:
                reader, writer = csv.reader(source), csv.writer(target)
                writer.writerow(next(reader))
                for row in reader:
                    if float(row[0]) >= self.seconds:
                        break
                    writer.writerow(row)
            self.duration = float(self.seconds)
        else:
            with open(self.session, "rb") as source:  # the time of the last event, from the tail of the file
                source.seek(max(source.seek(0, os.SEEK_END) - 4096, 0))
                self.duration = float(source.read().split(b"\n")[-2].split(b",")[0])
        self.market_data = market_data

        config = json.loads((rtg_home() / "exchange.json").read_text())
        config["Engine"].update(MarketDataFile=str(market_data), Speed=float(self.speed),
                                MarketOpenDelay=float(self.open_delay))  # the exchange insists on floats
        config["Execution"] = {"Host": "127.0.0.1", "Port": self.port}
        if self.hud:
            # The display connects to a port of its own. While it is connected the exchange counts it
            # among its clients, so the match then runs to the end of the market data even if every
            # trader has gone.
            config["Hud"] = {"Host": "127.0.0.1", "Port": _free_port()}
        else:
            config.pop("Hud", None)  # no display attached: the match ends when the last trader leaves or the data does
        config["Traders"] = {name: SECRET for name in self.traders}
        self.exchange_json.write_text(json.dumps(config, indent=2))
        self._config = config
        (self.run_dir / FINGERPRINT_FILE).write_text(traders_fingerprint() + "\n")

        key = session_key(self.session)
        for name, kind in self.traders.items():
            module = self.module_name(name)
            if kind == "example":
                shutil.copy(rtg_home() / "autotrader.py", self.run_dir / f"{module}.py")
            elif kind.startswith(("preset:", "actor:")):
                settings_file = None
                if kind.startswith("preset:"):
                    # The fold model that never saw this session, and the rules this exchange is run with.
                    settings_file = str(self.run_dir / f"{module}.strategy.json")
                    Path(settings_file).write_text(json.dumps(
                        {"preset": kind.split(":", 1)[1], "holdout": key, **exchange_rules(config)}))
                (self.run_dir / f"{module}.py").write_text(_module_source(kind, settings_file))
            else:
                shutil.copy(Path(kind).expanduser(), self.run_dir / f"{module}.py")
            (self.run_dir / f"{module}.json").write_text(json.dumps({
                "Execution": {"Host": "127.0.0.1", "Port": self.port},
                "Information": config["Information"],
                "TeamName": name,
                "Secret": SECRET,
            }, indent=2))

    @staticmethod
    def module_name(team: str) -> str:
        return "trader_" + re.sub(r"\W", "_", team).lower()

    def header(self) -> MatchHeader:
        config = self._config
        # The fingerprint is of what the exchange replayed, which for a shortened match is a copy
        # of the head of the session and not the session file itself. Say so in the name.
        played = self.session.name if self.seconds is None else f"{self.session.name}, first {self.seconds:g} s"
        return header_for(
            self.match_id, "live", teams=sorted(self.traders), limits=config["Limits"], fees=config["Fees"],
            instrument=config["Instrument"], market_data=played,
            market_data_sha256=file_sha256(self.market_data), speed=self.speed,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    # -- processes -----------------------------------------------------------------------------

    def start(self) -> LiveMatch:
        self._prepare()
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(rtg_home()), str(REPO_ROOT / "src"), env.get("PYTHONPATH", "")])
        env["PYTHONWARNINGS"] = "ignore"

        def launch(code: str, log: str) -> subprocess.Popen:
            return subprocess.Popen([sys.executable, "-c", code], cwd=self.run_dir, env=env,
                                    stdout=open(self.run_dir / log, "w"), stderr=subprocess.STDOUT)

        self._exchange = launch("import ready_trader_go.exchange as e; e.main()", "exchange.out")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:  # wait for the exchange to listen before the traders dial in
            if self._exchange.poll() is not None:
                raise RuntimeError(f"the exchange exited at start-up; see {self.run_dir / 'exchange.out'}")
            with socket.socket() as sock:
                sock.settimeout(0.2)
                if sock.connect_ex(("127.0.0.1", self.port)) == 0:
                    break
            time.sleep(0.05)
        else:
            self.stop()
            raise RuntimeError("the exchange did not start listening within 10 s")
        for name in self.traders:
            module = self.module_name(name)
            self._processes.append(launch(f"import ready_trader_go.trader as t; t.main({module!r})", f"{module}.out"))
        if self.hud:
            # Started from the run directory, the display reads the exchange.json the match runs with.
            hud = self._config["Hud"]
            self._display = launch("from ready_trader_go.hud.__main__ import main; "
                                   f"main({hud['Host']!r}, {hud['Port']})", "hud.out")
        self._started = time.monotonic()
        # A healthy match lasts the opening delay plus the market data. Far past that, something
        # is wrong (the exchange waits for ever on market data it cannot read, for one).
        self.deadline = time.monotonic() + self.open_delay + self.duration / self.speed * 1.25 + 30.0
        return self

    def running(self) -> bool:
        """Is the exchange still going? Also notes any trader that has gone before it."""
        alive = self._exchange is not None and self._exchange.poll() is None
        if alive:
            for name, process in zip(self.traders, self._processes, strict=False):
                if name not in self._left and process.poll() is not None:
                    self._left[name] = (time.monotonic() - self._started, process.returncode)
        return alive

    def departures(self) -> dict[str, dict]:
        """Traders whose process ended while the exchange was still running.

        The exchange disconnects a trader that breaches a limit, and the trader
        then exits cleanly; one that crashed, or could not even be loaded,
        exits with an error. ``failed`` tells the two apart, and the last line
        of the trader's log says what happened.
        """
        out = {}
        for name, (after, code) in self._left.items():
            log = self.run_dir / f"{self.module_name(name)}.out"
            lines = log.read_text(errors="replace").strip().splitlines() if log.is_file() else []
            out[name] = {"seconds_after_start": round(after, 1), "exit_code": code, "failed": code != 0,
                         "last_words": lines[-1] if lines else ""}
        return out

    def events(self, poll: float = 0.05) -> Iterator[MatchRecord]:
        """Follow ``match_events.csv`` until the exchange has exited and the file is exhausted."""
        path = self.match_events_path
        while not path.exists():
            if not self.running():
                return
            if time.monotonic() > self.deadline:
                self.stop()
                raise TimeoutError(f"the exchange never opened its event file; see the logs in {self.run_dir}")
            time.sleep(poll)
        seq = 0
        pending = b""
        header_seen = False
        # Read as bytes and decode whole lines: a read can end anywhere, in the middle of a line
        # or of a character.
        with open(path, "rb") as handle:
            finishing = False
            while True:
                chunk = handle.read(1 << 16)
                if chunk:
                    pending += chunk
                    *lines, pending = pending.split(b"\r\n")
                    for row in csv.reader(line.decode("utf-8") for line in lines):
                        if not header_seen:
                            if row[:4] != CSV_HEADER[:4]:
                                raise ValueError(f"{path}: unexpected header {row!r}")
                            header_seen = True
                            continue
                        yield MatchRecord.from_row(seq, row)
                        seq += 1
                elif finishing:
                    return
                elif not self.running():
                    # The exchange flushes its last rows as it exits, which may be after the read
                    # above came back empty. Look once more now that it is known to have gone.
                    finishing = True
                elif time.monotonic() > self.deadline:
                    self.stop()
                    raise TimeoutError(f"the match is overdue; see the logs in {self.run_dir}")
                else:
                    time.sleep(poll)

    def wait(self, timeout: float | None = None) -> int:
        code = self._exchange.wait(timeout)
        for process in self._processes:
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()
        return code

    def stop(self) -> None:
        for process in [*self._processes, self._exchange]:
            if process is not None and process.poll() is None:
                process.terminate()
        for process in [*self._processes, self._exchange]:
            if process is not None:
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    process.kill()

    def __enter__(self) -> LiveMatch:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
