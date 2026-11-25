"""Subprocess fault-test worker. Fault injection lives here, never in production code."""
import argparse
import time
from pathlib import Path

from vigil.bus.context import ReplayContexts
from vigil.bus.kafka import KafkaBus
from vigil.bus.services import Monitor
from vigil.observability import Metrics
from vigil.reports.canonical import canonical_json
from vigil.storage import atomic_write

parser = argparse.ArgumentParser()
parser.add_argument("server")
parser.add_argument("group")
parser.add_argument("directory")
parser.add_argument("--fault", default="")
parser.add_argument("--name", default="worker")
args = parser.parse_args()
directory = Path(args.directory)
directory.mkdir(parents=True, exist_ok=True)


class FaultBus(KafkaBus):
    def commit(self, group, upto=None):
        if args.fault == "after-output" and (directory / "written").exists():
            atomic_write(directory / "before-commit", b"ready")
            while True:
                time.sleep(.1)
        return super().commit(group, upto)


bus = FaultBus(args.server, consumer_options={"session.timeout.ms": 6000, "heartbeat.interval.ms": 1000})
contexts = ReplayContexts(directory / "contexts.sqlite3")
metrics = Metrics()


def save(audit):
    audit.report.save(directory / "reports" / (audit.report.report_id + ".json"))
    atomic_write(directory / "written", b"ready")


def tick(current, active):
    atomic_write(directory / (args.name + ".json"), canonical_json(current.snapshot(active)))


monitor = Monitor(bus, group=args.group, on_report=save, metrics=metrics, on_tick=tick,
                  contexts=contexts, stale_after=120)
try:
    monitor.run(retain_reports=0, poll_interval=.1)
finally:
    contexts.close()
    bus.close()
