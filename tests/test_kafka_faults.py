"""Real JVM broker and real consumer processes, on private ports and isolated topics.

Enable with VIGIL_TEST_KAFKA_FAULTS=1. No running development broker is stopped.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from vigil.bus import MemoryBus, topics
from vigil.bus.services import MatchPublisher, Monitor, publish_match
from vigil.rtg.events import read_match_events
from vigil.surveillance.engine import header_for

pytestmark = [pytest.mark.kafka_faults,
              pytest.mark.skipif(os.environ.get("VIGIL_TEST_KAFKA_FAULTS") != "1", reason="opt-in isolated broker faults")]
ROOT = Path(__file__).resolve().parents[1]


def eventually(check, seconds=40):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (FileNotFoundError, json.JSONDecodeError, ConnectionError):
            pass
        time.sleep(.1)
    raise AssertionError(f"condition did not become true in {seconds}s")


def free_port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


@pytest.fixture(scope="module")
def broker(tmp_path_factory):
    from confluent_kafka.admin import AdminClient
    directory = tmp_path_factory.mktemp("private-kafka")
    kafka = ROOT / ".tools/kafka/bin"
    if not (kafka / "kafka-server-start.sh").exists():
        pytest.fail("run scripts/bootstrap_tools.sh kafka first")
    port, controller = free_port(), free_port()
    while controller == port:
        controller = free_port()
    config = directory / "server.properties"
    config.write_text(f"""process.roles=broker,controller
node.id=1
controller.quorum.bootstrap.servers=127.0.0.1:{controller}
listeners=PLAINTEXT://127.0.0.1:{port},CONTROLLER://127.0.0.1:{controller}
advertised.listeners=PLAINTEXT://127.0.0.1:{port},CONTROLLER://127.0.0.1:{controller}
inter.broker.listener.name=PLAINTEXT
controller.listener.names=CONTROLLER
listener.security.protocol.map=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT
log.dirs={directory / 'logs'}
offsets.topic.replication.factor=1
transaction.state.log.replication.factor=1
transaction.state.log.min.isr=1
share.coordinator.state.topic.replication.factor=1
share.coordinator.state.topic.min.isr=1
group.initial.rebalance.delay.ms=0
""")
    env = {**os.environ, "KAFKA_HEAP_OPTS": "-Xms128m -Xmx512m", "LOG_DIR": str(directory / "server-logs")}
    cluster = subprocess.check_output([str(kafka / "kafka-storage.sh"), "random-uuid"], env=env, text=True).strip()
    subprocess.run([str(kafka / "kafka-storage.sh"), "format", "--standalone", "-t", cluster, "-c", str(config)],
                   env=env, check=True, capture_output=True)
    class Broker:
        server = f"127.0.0.1:{port}"
        process = None
        def start(self):
            self.log = (directory / "broker.log").open("ab")
            self.process = subprocess.Popen([str(kafka / "kafka-server-start.sh"), str(config)], env=env,
                                            stdout=self.log, stderr=subprocess.STDOUT)
            def ready():
                if self.process.poll() is not None:
                    raise RuntimeError((directory / "broker.log").read_text()[-4000:])
                try:
                    return AdminClient({"bootstrap.servers": self.server}).list_topics(timeout=.5).brokers
                except Exception:
                    return False
            eventually(ready, 60)
        def stop(self):
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(20)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(5)
            if hasattr(self, "log"):
                self.log.close()
    instance = Broker()
    instance.start()
    yield instance
    instance.stop()


@pytest.fixture
def setup(broker, tmp_path, monkeypatch):
    from vigil.bus.kafka import KafkaBus
    prefix = "fault-" + uuid.uuid4().hex[:10] + "."
    names = {name: prefix + getattr(topics, name) for name in ("EVENTS", "REPORTS", "ALERTS", "REGULATORY", "REJECTED")}
    for name, value in names.items():
        monkeypatch.setattr(topics, name, value)
    bus = KafkaBus(broker.server)
    bus.ensure_topics({names["EVENTS"]: 3, names["REPORTS"]: 1, names["ALERTS"]: 1,
                       names["REGULATORY"]: 1, names["REJECTED"]: 1})
    records = list(read_match_events(ROOT / "tests/fixtures/live_capture/match_events.csv"))
    teams = sorted({r.competitor for r in records if r.competitor})
    header = header_for("fault-match", "csv", teams=teams)
    processes, logs = [], []
    group = "fault-" + uuid.uuid4().hex
    def worker(fault="", name="worker"):
        log = (tmp_path / (name + ".log")).open("ab")
        process = subprocess.Popen([sys.executable, str(ROOT / "tests/support/monitor_worker.py"), broker.server,
                                    group, str(tmp_path), "--fault", fault, "--name", name],
                                   env={**os.environ, "VIGIL_TOPIC_PREFIX": prefix}, stdout=log, stderr=subprocess.STDOUT)
        processes.append(process)
        logs.append(log)
        return process
    def progress(name="worker"):
        return json.loads((tmp_path / (name + ".json")).read_text())
    def reference():
        memory = MemoryBus()
        publish_match(memory, header, records)
        return Monitor(memory).run(idle_timeout=0)[0].report
    yield bus, header, records, worker, progress, reference, tmp_path
    for process in processes:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(5)
    for log in logs:
        log.close()
    bus.close()


def assert_report(directory, expected):
    from vigil.reports.builder import ComplianceReport
    path = directory / "reports/fault-match.json"
    eventually(path.exists)
    actual = ComplianceReport.load(path)
    assert actual.sha256 == expected.sha256
    assert actual.alerts_root == expected.alerts_root
    assert actual.document["match"]["coverage"]["complete"] is True


def test_process_kill_replays_unfinished_header(setup):
    bus, header, records, worker, progress, reference, directory = setup
    process = worker()
    publisher = MatchPublisher(bus, header)
    for record in records[:100]:
        publisher.event(record)
    bus.flush()
    eventually(lambda: progress()["counters"].get("events", 0) >= 100)
    process.kill()
    process.wait(5)
    for record in records[100:]:
        publisher.event(record)
    publisher.end()
    worker(name="restart")
    assert_report(directory, reference())


def test_report_write_failure_replays_without_lost_input(setup):
    bus, header, records, worker, _, reference, directory = setup
    (directory / "reports").write_text("deliberate path collision")
    process = worker()
    publish_match(bus, header, records)
    eventually(lambda: process.poll() is not None)
    assert process.returncode != 0
    (directory / "reports").unlink()
    worker(name="retry")
    assert_report(directory, reference())


def test_kill_after_output_before_offset_commit(setup):
    bus, header, records, worker, _, reference, directory = setup
    process = worker("after-output")
    publish_match(bus, header, records)
    eventually((directory / "before-commit").exists)
    expected = reference()
    assert_report(directory, expected)
    process.kill()
    process.wait(5)
    worker(name="replayed")
    eventually(lambda: json.loads((directory / "replayed.json").read_text())["counters"].get("reports") == 1)
    assert_report(directory, expected)


def test_broker_restart_during_unfinished_match(broker, setup):
    bus, header, records, worker, progress, reference, directory = setup
    worker()
    publisher = MatchPublisher(bus, header)
    for record in records[:100]:
        publisher.event(record)
    bus.flush()
    eventually(lambda: progress()["counters"].get("events", 0) >= 100)
    broker.stop()
    for record in records[100:200]:
        publisher.event(record)  # queued while the broker is down
    broker.start()
    for record in records[200:]:
        publisher.event(record)
    publisher.end()
    assert_report(directory, reference())


def test_live_group_rebalance_replays_active_partition(setup):
    bus, header, records, worker, progress, reference, directory = setup
    first = worker(name="first")
    publisher = MatchPublisher(bus, header)
    for record in records[:100]:
        publisher.event(record)
    bus.flush()
    eventually(lambda: progress("first")["counters"].get("events", 0) >= 100)
    second = worker(name="second")
    eventually(lambda: progress("first")["counters"].get("partition_revoke", 0) > 0)
    eventually(lambda: progress("second")["counters"].get("partition_assign", 0) > 0)
    for record in records[100:]:
        publisher.event(record)
    publisher.end()
    assert_report(directory, reference())
    assert first.poll() is None and second.poll() is None
