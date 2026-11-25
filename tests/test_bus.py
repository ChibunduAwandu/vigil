from __future__ import annotations

import dataclasses
import itertools
import threading
import uuid

import pytest

from vigil.bus import MemoryBus, connect, topics
from vigil.bus.services import MatchPublisher, Monitor, publish_match

# -- the in-memory bus keeps Kafka's promises ---------------------------------------------------------

def test_memory_bus_orders_messages_and_tracks_groups():
    bus = MemoryBus()
    for i in range(5):
        bus.publish("t", {"i": i}, key="k")
    first = [m.value["i"] for m in bus.consume(["t"], "group-a", idle_timeout=0)]
    assert first == [0, 1, 2, 3, 4]
    # Nothing was committed: the same group starts again from the beginning.
    assert len(list(bus.consume(["t"], "group-a", idle_timeout=0))) == 5
    bus.commit("group-a")
    bus.publish("t", {"i": 5})
    assert [m.value["i"] for m in bus.consume(["t"], "group-a", idle_timeout=0)] == [5]
    # Another group has its own position.
    assert len(list(bus.consume(["t"], "group-b", idle_timeout=0))) == 6


def test_memory_bus_wakes_a_waiting_consumer():
    bus = MemoryBus()
    received = []

    def consume():
        for message in bus.consume(["t"], "g", idle_timeout=2.0):
            received.append(message.value)
            break

    thread = threading.Thread(target=consume)
    thread.start()
    bus.publish("t", {"hello": 1})
    thread.join(3)
    assert received == [{"hello": 1}]


def test_connect_parses_urls():
    assert isinstance(connect("memory://"), MemoryBus)
    with pytest.raises(ValueError):
        connect("amqp://localhost")


# -- publisher and monitor ---------------------------------------------------------------------------

def test_a_match_monitored_over_the_bus_matches_the_direct_audit(audits):
    direct = audits("wash", 200.0)
    bus = MemoryBus()
    count, digest = publish_match(bus, direct.header, direct.simulation.records())
    assert count == direct.result.market["events"]
    assert digest == direct.report.document["match"]["events_sha256"]

    seen = []
    monitor = Monitor(bus, on_report=lambda audit: seen.append(audit.report.report_id) or {"receipt": 1})
    (audit,) = monitor.run(idle_timeout=0)
    assert [a.alert_id for a in audit.alerts] == [a.alert_id for a in direct.alerts]
    assert audit.report.alerts_root == direct.report.alerts_root
    assert audit.report.document["match"]["transit"] == {
        "ended": True, "events_declared": count, "events_received": count, "sha256_declared": digest,
        "duplicates_discarded": 0, "late": 0, "missing": 0, "unreadable": 0, "conflicting_headers": 0,
        "complete": True}

    assert [m.value["alert_id"] for m in bus.log(topics.ALERTS)] == [a.alert_id for a in direct.alerts]
    assert {m.key for m in bus.log(topics.ALERTS)} == {"WashA", "WashB"}
    (receipt,) = bus.log(topics.REPORTS)
    assert receipt.key == direct.header.match_id and receipt.value["sha256"] == audit.report.sha256
    assert receipt.value["anchor"] == {"receipt": 1} and receipt.value["stream_complete"] is True
    assert seen == [direct.header.match_id]


def test_monitor_discards_redelivered_events_and_notices_lost_ones(audits):
    direct = audits("wash", 200.0)
    records = list(direct.simulation.records())
    bus = MemoryBus()
    publisher = MatchPublisher(bus, direct.header)
    for record in records[:1000]:
        publisher.event(record)
    for record in records[900:1000]:  # a retry delivers a hundred events twice
        bus.publish(topics.EVENTS, topics.event_message(direct.header.match_id, record), key=direct.header.match_id)
    for record in records[1000:]:
        publisher.event(record)
    publisher.end()
    (audit,) = Monitor(bus).run(idle_timeout=0)
    transit = audit.report.document["match"]["transit"]
    assert transit["duplicates_discarded"] == 100 and transit["complete"] is True
    assert audit.report.alerts_root == direct.report.alerts_root

    # Now lose a stretch of events in transit.
    bus = MemoryBus()
    bus.publish(topics.EVENTS, topics.header_message(direct.header), key="m")
    for record in records[:5000] + records[5050:]:
        bus.publish(topics.EVENTS, topics.event_message(direct.header.match_id, record), key="m")
    bus.publish(topics.EVENTS, topics.end_message(direct.header.match_id, len(records), "f" * 64), key="m")
    (audit,) = Monitor(bus).run(idle_timeout=0)
    transit = audit.report.document["match"]["transit"]
    assert transit == {**transit, "missing": 50, "complete": False, "events_received": len(records) - 50}
    assert (bus.log(topics.REPORTS)[0].value["stream_complete"]) is False


def test_monitor_handles_interleaved_matches_and_restarts(audits):
    one, two = audits("wash", 200.0), audits("clean", 60.0)
    bus = MemoryBus()
    first, second = MatchPublisher(bus, one.header), MatchPublisher(bus, two.header)
    a, b = list(one.simulation.records()), list(two.simulation.records())
    for i in range(max(len(a), len(b))):  # two matches in flight at once
        if i < len(a):
            first.event(a[i])
        if i < len(b):
            second.event(b[i])
    second.end()
    first.end()
    audits_out = Monitor(bus, group="g").run(idle_timeout=0)
    assert {x.report.report_id for x in audits_out} == {one.header.match_id, two.header.match_id}
    by_id = {x.report.report_id: x for x in audits_out}
    assert by_id[one.header.match_id].report.alerts_root == one.report.alerts_root
    assert by_id[two.header.match_id].alerts == []

    # A monitor that starts afresh in the same group has nothing left to do ...
    assert Monitor(bus, group="g").run(idle_timeout=0) == []
    # ... and one in a new group replays everything and reaches the same conclusions.
    replayed = Monitor(bus, group="other").run(idle_timeout=0)
    assert {x.report.sha256 for x in replayed} == {x.report.sha256 for x in audits_out}


def test_events_and_end_markers_without_a_header_are_counted_not_processed(audits):
    direct = audits("clean", 60.0)
    bus = MemoryBus()
    for record in list(direct.simulation.records())[:10]:
        bus.publish(topics.EVENTS, topics.event_message("unknown", record), key="unknown")
    bus.publish(topics.EVENTS, topics.end_message("unknown", 10, "0" * 64), key="unknown")
    monitor = Monitor(bus)
    assert monitor.run(idle_timeout=0) == [] and monitor.orphans == 11


def test_surveillance_over_the_bus_stops_at_a_hole_and_says_so(audits):
    direct = audits("wash", 200.0)
    records = list(direct.simulation.records())
    match_id = direct.header.match_id
    bus = MemoryBus()
    bus.publish(topics.EVENTS, topics.header_message(direct.header), key=match_id)
    for record in records[:5000] + records[5300:] + records[5000:5300]:  # 300 events overtaken by the rest
        bus.publish(topics.EVENTS, topics.event_message(match_id, record), key=match_id)
    bus.publish(topics.EVENTS, topics.end_message(match_id, len(records), "f" * 64), key=match_id)
    (audit,) = Monitor(bus).run(idle_timeout=0)

    transit = audit.report.document["match"]["transit"]
    assert (transit["missing"], transit["late"], transit["duplicates_discarded"]) == (300, 300, 0)
    assert transit["complete"] is False and transit["events_received"] == len(records) - 300
    integrity = audit.report.document["summary"]["integrity"]
    assert integrity["stopped_after_sequence"] == records[4999].seq
    # One alert says the record has a hole. Nothing is concluded from events after it: with the
    # books out of step every one of them would look like a discrepancy or a breach.
    gaps = [alert for alert in audit.alerts if alert.metrics.get("kind") == "gap"]
    assert len(gaps) == 1 and gaps[0].severity.name == "CRITICAL" and gaps[0].competitor == "(exchange)"
    assert all(alert.time_end <= records[5000].time for alert in audit.alerts)
    published = [message.value["alert_id"] for message in bus.log(topics.ALERTS)]
    assert sorted(published) == sorted(alert.alert_id for alert in audit.alerts)


def test_events_lost_off_the_end_of_a_stream_are_counted(audits):
    direct = audits("wash", 200.0)
    records = list(direct.simulation.records())
    match_id = direct.header.match_id
    bus = MemoryBus()
    bus.publish(topics.EVENTS, topics.header_message(direct.header), key=match_id)
    for record in records[:-500]:
        bus.publish(topics.EVENTS, topics.event_message(match_id, record), key=match_id)
    bus.publish(topics.EVENTS, topics.end_message(match_id, len(records), "f" * 64), key=match_id)
    (audit,) = Monitor(bus).run(idle_timeout=0)
    transit = audit.report.document["match"]["transit"]
    assert transit["missing"] == 500 and transit["complete"] is False
    assert audit.report.document["summary"]["integrity"]["stopped_after_sequence"] == records[-501].seq
    assert [m.value["metrics"]["kind"] for m in bus.log(topics.ALERTS) if m.value["control"] == "REC-01"] == ["gap"]


def test_a_header_delivered_twice_does_not_restart_the_match(audits):
    direct = audits("wash", 200.0)
    records = list(direct.simulation.records())
    match_id = direct.header.match_id
    bus = MemoryBus()
    publisher = MatchPublisher(bus, direct.header)
    for record in records[:5000]:
        publisher.event(record)
    bus.publish(topics.EVENTS, topics.header_message(direct.header), key=match_id)  # a retry, from the top
    for record in records[4900:5000]:
        bus.publish(topics.EVENTS, topics.event_message(match_id, record), key=match_id)
    # Someone else announces a match under the same name. The first keeps it.
    impostor = dataclasses.replace(direct.header, teams=["Nobody"])
    bus.publish(topics.EVENTS, topics.header_message(impostor), key=match_id)
    for record in records[5000:]:
        publisher.event(record)
    publisher.end()
    (audit,) = Monitor(bus).run(idle_timeout=0)
    transit = audit.report.document["match"]["transit"]
    assert (transit["duplicates_discarded"], transit["conflicting_headers"], transit["complete"]) == (100, 1, True)
    assert audit.report.alerts_root == direct.report.alerts_root
    assert audit.report.document["match"]["teams"] == sorted(direct.header.teams)


def test_an_unfinished_match_holds_back_only_what_a_restart_would_need(audits):
    one, two, three = audits("clean", 60.0), audits("wash", 200.0), audits("spoofer", 150.0)
    bus = MemoryBus()
    publish_match(bus, one.header, one.simulation.records())
    stuck_at = len(bus.log(topics.EVENTS))
    stuck = dataclasses.replace(one.header, match_id="stuck")
    bus.publish(topics.EVENTS, topics.header_message(stuck), key="stuck")  # a match that never ends
    publish_match(bus, two.header, two.simulation.records())
    publish_match(bus, three.header, three.simulation.records())

    first = Monitor(bus, group="g", stale_after=None)
    reported = first.run(idle_timeout=0)
    assert [audit.report.report_id for audit in reported] == [x.header.match_id for x in (one, two, three)]
    assert first.restart_points() == {(topics.EVENTS, 0): stuck_at}

    # A monitor that starts again resumes at the unfinished match, not at the beginning: the match
    # before it is done with, and the two after it are read and reported again, to the same effect.
    again = Monitor(bus, group="g", stale_after=None).run(idle_timeout=0)
    assert [audit.report.report_id for audit in again] == [two.header.match_id, three.header.match_id]
    assert [audit.report.sha256 for audit in again] == [audit.report.sha256 for audit in reported[1:]]


def test_a_match_that_goes_quiet_is_reported_as_it_stands(audits):
    one = audits("wash", 200.0)
    records = list(one.simulation.records())
    bus = MemoryBus()
    stuck = dataclasses.replace(one.header, match_id="stuck")
    bus.publish(topics.EVENTS, topics.header_message(stuck), key="stuck")
    for record in records[:10]:
        bus.publish(topics.EVENTS, topics.event_message("stuck", record), key="stuck")
    publish_match(bus, one.header, records)

    seconds = itertools.count()  # a clock that moves on a second every time it is read
    monitor = Monitor(bus, group="g", stale_after=300.0, clock=lambda: float(next(seconds)))
    reported = monitor.run(idle_timeout=0)
    assert [audit.report.report_id for audit in reported] == ["stuck", one.header.match_id]
    transit = reported[0].report.document["match"]["transit"]
    assert transit["ended"] is False and transit["complete"] is False and transit["events_received"] == 10
    assert reported[1].report.document["match"]["transit"]["complete"] is True
    # With nothing left in flight, nothing is held back: a restart has nothing to do.
    assert monitor.restart_points() == {} and Monitor(bus, group="g").run(idle_timeout=0) == []


def test_messages_that_are_not_match_messages_do_not_stop_the_monitor(audits):
    direct = audits("clean", 60.0)
    records = list(direct.simulation.records())
    match_id = direct.header.match_id
    bus = MemoryBus()
    bus.publish(topics.EVENTS, {"nonsense": True})
    bus.publish(topics.EVENTS, {"type": "header", "match": "broken", "header": {"no_such_field": 1}}, key="broken")
    bus.publish(topics.EVENTS, {"type": "gossip", "match": match_id}, key=match_id)
    publisher = MatchPublisher(bus, direct.header)
    for record in records[:100]:
        publisher.event(record)
    bus.publish(topics.EVENTS, {"type": "event", "match": match_id, "record": [1, 2]}, key=match_id)
    bus.publish(topics.EVENTS, {"type": "event", "match": match_id}, key=match_id)
    for record in records[100:]:
        publisher.event(record)
    publisher.end()
    monitor = Monitor(bus)
    (audit,) = monitor.run(idle_timeout=0)
    assert monitor.unreadable == 3
    transit = audit.report.document["match"]["transit"]
    assert transit["unreadable"] == 2 and transit["complete"] is True
    assert audit.report.alerts_root == direct.report.alerts_root
    assert audit.result.integrity["discrepancies"] == 0


# -- the real broker ---------------------------------------------------------------------------------

def test_failed_output_delivery_leaves_match_available_for_restart(audits, monkeypatch):
    direct = audits("wash", 200.0)
    bus = MemoryBus()
    publish_match(bus, direct.header, direct.simulation.records())

    def failed_flush():
        raise RuntimeError("report delivery failed")

    monkeypatch.setattr(bus, "flush", failed_flush)
    with pytest.raises(RuntimeError, match="report delivery failed"):
        Monitor(bus, group="restart").run(idle_timeout=0)
    # No commit: the same group gets its original match header on restart.
    first = next(bus.consume([topics.EVENTS], "restart", idle_timeout=0))
    assert first.value["type"] == "header"
    assert first.value["match"] == direct.header.match_id

@pytest.mark.kafka
def test_kafka_round_trip(audits, kafka_address):
    direct = audits("wash", 200.0)
    header = type(direct.header).from_dict({**direct.header.to_dict(), "match_id": f"pytest-{uuid.uuid4().hex[:8]}"})
    bus = connect(f"kafka://{kafka_address}")
    try:
        assert bus.ping()["brokers"] >= 1
        bus.ensure_topics(topics.PARTITIONS)
        count, digest = publish_match(bus, header, direct.simulation.records())
        group = f"pytest-{uuid.uuid4().hex[:8]}"
        found = None
        monitor = Monitor(bus, group=group)
        for message in bus.consume([topics.EVENTS], group, idle_timeout=15.0):
            if message.value["match"] != header.match_id:
                continue  # other matches left on the topic by earlier runs
            audit = monitor.handle(message.value)
            if audit is not None:
                found = audit
                break
        assert found is not None
        assert found.report.document["match"]["transit"]["complete"] is True
        assert found.report.document["match"]["events_sha256"] == digest
        assert [a.control for a in found.alerts] == [a.control for a in direct.alerts]
        bus.commit(group)
    finally:
        bus.close()
