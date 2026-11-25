"""Kafka client boundary cases that do not need a running broker."""
from __future__ import annotations

from unittest.mock import Mock

import pytest

kafka = pytest.importorskip("confluent_kafka")

from vigil.bus.kafka import KafkaBus  # noqa: E402


@pytest.mark.parametrize("failure", [False, True])
def test_empty_producer_queue_does_not_hide_failed_delivery(failure):
    bus = KafkaBus("unused:9092")
    producer = bus._producer = Mock()
    bus.publish("reports", {"report_id": "example"})
    callback = producer.produce.call_args.kwargs["on_delivery"]
    error = kafka.KafkaError(kafka.KafkaError._MSG_TIMED_OUT) if failure else None

    def flush(timeout):
        callback(error, Mock())
        return 0  # both successful and failed delivery empty the queue

    producer.flush.side_effect = flush
    if failure:
        with pytest.raises(kafka.KafkaException):
            bus.flush()
        with pytest.raises(kafka.KafkaException):
            bus.publish("reports", {"report_id": "later"})
        assert producer.produce.call_count == 1
    else:
        bus.flush()


def test_consumer_is_closed_even_if_delivery_failed():
    bus = KafkaBus("unused:9092")
    bus._producer = Mock()
    bus._producer.flush.return_value = 0
    bus._delivery_error = kafka.KafkaError(kafka.KafkaError._MSG_TIMED_OUT)
    consumer = bus._consumers["review"] = Mock()
    with pytest.raises(kafka.KafkaException):
        bus.close()
    consumer.close.assert_called_once()


def test_commits_stop_at_unfinished_headers_and_last_handed_offsets():
    bus = KafkaBus("unused:9092")
    consumer = bus._consumers["review"] = Mock()
    consumer.commit.return_value = []
    bus._handed["review"] = {("events", 0): 14, ("events", 1): 27}
    bus.commit("review", {("events", 0): 8})
    offsets = consumer.commit.call_args.kwargs["offsets"]
    assert {(p.topic, p.partition): p.offset for p in offsets} == {("events", 0): 8, ("events", 1): 28}
    assert consumer.commit.call_args.kwargs["asynchronous"] is False


def test_partition_commit_error_is_not_silent():
    bus = KafkaBus("unused:9092")
    consumer = bus._consumers["review"] = Mock()
    bus._handed["review"] = {("events", 0): 14}
    consumer.commit.return_value = [Mock(error=kafka.KafkaError(kafka.KafkaError._TIMED_OUT))]
    with pytest.raises(kafka.KafkaException):
        bus.commit("review")


@pytest.mark.parametrize("payload,key", [(b'{"type":"header","type":"end"}', b"k"),
                                         (b'{"x":NaN}', b"k"), (b"\xff", b"k"), (b"{}", b"\xff"), (b"[" * 2000, b"k")])
def test_bad_broker_bytes_reach_quarantine_without_decode_crash(payload, key):
    bus = KafkaBus("unused:9092")
    consumer = bus._consumers["review"] = Mock()
    raw = Mock()
    raw.error.return_value = None
    raw.value.return_value = payload
    raw.key.return_value = key
    raw.topic.return_value = "events"
    raw.partition.return_value = 0
    raw.offset.return_value = 7
    consumer.consume.return_value = [raw]
    received = next(bus.consume(["events"], "review"))
    assert received.raw == payload and received.raw_key == key and received.error
    assert bus._handed["review"] == {("events", 0): 7}
