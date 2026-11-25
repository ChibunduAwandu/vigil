"""Shared fixtures, and the rules for which tests run where.

Most of the suite needs nothing but Python and the Ready Trader Go sample
data. A few tests need more and skip themselves when it is absent:

* ``kafka``: a broker on 127.0.0.1:9092 (``scripts/kafka_local.sh start``)
* ``chain``: the anvil binary (``scripts/bootstrap_tools.sh foundry``)
* ``network``: the public internet; opt in with ``VIGIL_TEST_NETWORK=1``
* ``live``: starts the real exchange for half a minute; opt in with ``VIGIL_TEST_LIVE=1``
"""
from __future__ import annotations

import csv
import os
import shutil
import socket
from pathlib import Path

import pytest

from vigil.paths import market_data_file

FIXTURES = Path(__file__).parent / "fixtures"
KAFKA = os.environ.get("VIGIL_TEST_KAFKA", "127.0.0.1:9092")


def _listening(address: str) -> bool:
    host, _, port = address.partition(":")
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, int(port))) == 0


def pytest_collection_modifyitems(config, items):
    from vigil.chain.artifact import tool
    reasons = {
        "kafka": None if _listening(KAFKA) else f"no Kafka broker at {KAFKA}",
        "chain": None if tool("anvil") else "anvil is not installed",
        "network": None if os.environ.get("VIGIL_TEST_NETWORK") else "set VIGIL_TEST_NETWORK=1 to run",
        "live": None if os.environ.get("VIGIL_TEST_LIVE") else "set VIGIL_TEST_LIVE=1 to run",
    }
    for item in items:
        for marker, reason in reasons.items():
            if reason and marker in item.keywords:
                item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture(autouse=True)
def no_public_chain(monkeypatch):
    """Nothing in the suite may reach a public network's chain or spend from a real wallet, even when the
    shell it runs in has .env loaded. A test that needs a node starts its own and says so."""
    for name in ("VIGIL_ETH_RPC", "VIGIL_ETH_PRIVATE_KEY", "VIGIL_REGISTRY", "ETHERSCAN_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def session_one() -> Path:
    return market_data_file(1)


@pytest.fixture(scope="session")
def capture(tmp_path_factory, session_one) -> Path:
    """Files written by the real exchange (see tests/fixtures/live_capture/README.md)."""
    capture = tmp_path_factory.mktemp("live_capture")
    for name in ("exchange.json", "match_events.csv", "score_board.csv"):
        shutil.copyfile(FIXTURES / "live_capture" / name, capture / name)
    # Recreate the capture's exact 20-second input from the separately installed data,
    # keeping the upstream market-data excerpt out of the published repository.
    with session_one.open(newline="") as source, (capture / "market_data.csv").open("w", newline="") as output:
        reader, writer = csv.reader(source), csv.writer(output)
        writer.writerow(next(reader))
        for row in reader:
            if float(row[0]) > 20.0:
                break
            writer.writerow(row)
    return capture


@pytest.fixture(scope="session")
def rulebook():
    from vigil.surveillance.rulebook import load_rulebook
    return load_rulebook()


@pytest.fixture(scope="session")
def audits(session_one):
    """Simulated and audited matches, shared across tests: ``audits(scenario, seconds)``."""
    from vigil.pipeline import field_of, simulate_and_audit
    cache: dict = {}

    def get(scenario: str, seconds: float, *, traders=None, seed: int = 7):
        key = (scenario, seconds, seed)
        if key not in cache:
            cache[key] = simulate_and_audit(session_one, traders or field_of(session_one, scenario), seed=seed,
                                            end_time=seconds, match_id=f"test-{scenario}-{int(seconds)}")
        return cache[key]
    return get


@pytest.fixture()
def kafka_address() -> str:
    return KAFKA
