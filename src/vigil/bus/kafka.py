"""The bus on Apache Kafka.

Delivery is at least once: the producer is idempotent and waits for the
in-sync replicas, the consumer commits only after the application says its
work is done. Anything that might therefore be seen twice is made harmless
downstream (events carry sequence numbers, alerts carry stable identifiers).
"""
from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic

from vigil.bus.base import Assignment, Idle, Message
from vigil.reports.canonical import parse_strict


class KafkaBus:
    def __init__(self, servers: str, *, client_id: str = "vigil", consumer_options: dict | None = None):
        self.servers = servers
        self._consumer_options = consumer_options or {}
        self._client_id = client_id
        self._producer: Producer | None = None
        self._delivery_error: KafkaError | None = None
        self._consumers: dict[str, Consumer] = {}
        self._assignments: dict[str, set[tuple[str, int]]] = {}
        self._changes: dict[str, deque[Assignment]] = {}
        # Per group, the offset of the last message handed to the application on each partition.
        self._handed: dict[str, dict[tuple[str, int], int]] = {}

    # -- administration ------------------------------------------------------------------------

    def ensure_topics(self, topics: dict[str, int], *, timeout: float = 15.0) -> list[str]:
        """Create the topics that do not exist yet (``{name: partitions}``); returns those created."""
        admin = AdminClient({"bootstrap.servers": self.servers})
        existing = admin.list_topics(timeout=timeout).topics
        wanted = [NewTopic(name, num_partitions=partitions, replication_factor=1)
                  for name, partitions in topics.items() if name not in existing]
        created: list[str] = []
        if not wanted:
            return created
        for name, future in admin.create_topics(wanted).items():
            try:
                future.result(timeout=timeout)
                created.append(name)
            except KafkaException as error:
                if error.args[0].code() != KafkaError.TOPIC_ALREADY_EXISTS:
                    raise
        return created

    def ping(self, timeout: float = 5.0) -> dict[str, Any]:
        """Broker and topic metadata; raises if no broker answers."""
        metadata = AdminClient({"bootstrap.servers": self.servers}).list_topics(timeout=timeout)
        return {"brokers": len(metadata.brokers),
                "topics": sorted(name for name in metadata.topics if not name.startswith("__"))}

    # -- producing -----------------------------------------------------------------------------

    def _get_producer(self) -> Producer:
        if self._producer is None:
            self._producer = Producer({
                "bootstrap.servers": self.servers,
                "client.id": self._client_id,
                "enable.idempotence": True,  # no duplicates or reordering on retry
                "acks": "all",
                "compression.type": "lz4",
                "linger.ms": 20,
            })
        return self._producer

    def publish(self, topic: str, value: dict[str, Any], key: str | None = None) -> None:
        self._check_delivery()
        producer = self._get_producer()
        payload = json.dumps(value, separators=(",", ":")).encode()
        while True:
            try:
                producer.produce(topic, payload, key=key.encode() if key is not None else None,
                                 on_delivery=self._delivered)
                break
            except BufferError:  # the local queue is full: let it drain
                producer.poll(0.5)
                self._check_delivery()
        producer.poll(0)
        self._check_delivery()

    def _delivered(self, error: KafkaError | None, message: Any) -> None:
        if error is not None and self._delivery_error is None:
            self._delivery_error = error

    def _check_delivery(self) -> None:
        # flush() returning zero means the queue is empty, including messages that FAILED.
        # Keep a terminal delivery error sticky so the application cannot commit past lost output.
        if self._delivery_error is not None:
            raise KafkaException(self._delivery_error)

    def flush(self) -> None:
        if self._producer is not None:
            remaining = self._producer.flush(30)
            self._check_delivery()
            if remaining:
                raise RuntimeError(f"{remaining} messages could not be delivered to {self.servers}")

    # -- consuming -----------------------------------------------------------------------------

    def consume(self, topics: Sequence[str], group: str, *, idle_timeout: float | None = None,
                poll_interval: float | None = None) -> Iterator[Message | Idle | Assignment]:
        consumer = self._consumers.get(group)
        changes = self._changes.setdefault(group, deque())
        owned = self._assignments.setdefault(group, set())
        handed = self._handed.setdefault(group, {})

        def changed(action, partitions):
            keys = tuple((p.topic, p.partition) for p in partitions)
            if action == "assign":
                owned.update(keys)
            else:
                owned.difference_update(keys)
            for key in keys:
                handed.pop(key, None)
            changes.append(Assignment(action, keys))

        if consumer is None:
            consumer = self._consumers[group] = Consumer({
                **self._consumer_options,
                "bootstrap.servers": self.servers,
                "group.id": group,
                "client.id": self._client_id,
                "auto.offset.reset": "earliest",
                "enable.auto.commit": False,
                "enable.auto.offset.store": False,
                "enable.partition.eof": False,
            })
        consumer.subscribe(list(topics), on_assign=lambda c, p: changed("assign", p),
                           on_revoke=lambda c, p: changed("revoke", p),
                           on_lost=lambda c, p: changed("lost", p))
        idle_since = time.monotonic()
        while True:
            batch = consumer.consume(num_messages=500, timeout=poll_interval or 0.5)
            while changes:
                yield changes.popleft()
            if not batch:
                if idle_timeout is not None and time.monotonic() - idle_since >= idle_timeout:
                    return
                if poll_interval is not None:
                    yield Idle()
                continue
            idle_since = time.monotonic()
            for raw in batch:
                if raw.error():
                    if raw.error().code() == KafkaError.UNKNOWN_TOPIC_OR_PART:
                        continue  # the topic has not been created yet; keep waiting for it
                    raise KafkaException(raw.error())
                payload = raw.value()
                key = None
                try:
                    key = raw.key().decode("utf-8") if raw.key() is not None else None
                    value = parse_strict(payload) if payload is not None else None
                    error = None if isinstance(value, dict) else "Kafka value is not a JSON object"
                except (ValueError, TypeError, UnicodeError, RecursionError):
                    value, error = None, "Kafka key or value is not valid UTF-8/strict JSON"
                handed[(raw.topic(), raw.partition())] = raw.offset()
                yield Message(raw.topic(), key, value, raw.partition(), raw.offset(), payload, error, raw.key())

    def lag(self, group: str) -> dict[str, int]:
        """Log-end minus last handed offset (not committed lag), per owned partition."""
        consumer = self._consumers.get(group)
        result = {}
        if consumer is not None:
            for (topic, partition), offset in self._handed.get(group, {}).items():
                _, high = consumer.get_watermark_offsets(TopicPartition(topic, partition), timeout=0.5)
                result[f"{topic}:{partition}"] = max(0, high - offset - 1)
        return result

    def commit(self, group: str, upto: Mapping[tuple[str, int], int] | None = None) -> None:
        # Commit what the application has been handed, not what the client has fetched: messages
        # are read from the broker in batches, and the rest of the current batch is still to come.
        consumer = self._consumers.get(group)
        handed = self._handed.get(group)
        if consumer is not None and handed:
            held_back = upto or {}
            committed = consumer.commit(offsets=[
                TopicPartition(topic, partition, min(offset + 1, held_back.get((topic, partition), offset + 1)))
                for (topic, partition), offset in handed.items()], asynchronous=False)
            for partition in committed or []:
                if partition.error:
                    raise KafkaException(partition.error)

    def close(self) -> None:
        try:
            self.flush()
        finally:
            for consumer in self._consumers.values():
                consumer.close()
            self._consumers.clear()
