"""The bus on Apache Kafka.

Delivery is at least once: the producer is idempotent and waits for the
in-sync replicas, the consumer commits only after the application says its
work is done. Anything that might therefore be seen twice is made harmless
downstream (events carry sequence numbers, alerts carry stable identifiers).
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic

from vigil.bus.base import Message


class KafkaBus:
    def __init__(self, servers: str, *, client_id: str = "vigil"):
        self.servers = servers
        self._client_id = client_id
        self._producer: Producer | None = None
        self._delivery_error: KafkaError | None = None
        self._consumers: dict[str, Consumer] = {}
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

    def consume(self, topics: Sequence[str], group: str, *, idle_timeout: float | None = None) -> Iterator[Message]:
        consumer = self._consumers.get(group)
        if consumer is None:
            consumer = self._consumers[group] = Consumer({
                "bootstrap.servers": self.servers,
                "group.id": group,
                "client.id": self._client_id,
                "auto.offset.reset": "earliest",
                "enable.auto.commit": False,
                "enable.partition.eof": False,
            })
        consumer.subscribe(list(topics))
        handed = self._handed.setdefault(group, {})
        idle_since = time.monotonic()
        while True:
            batch = consumer.consume(num_messages=500, timeout=0.5)
            if not batch:
                if idle_timeout is not None and time.monotonic() - idle_since >= idle_timeout:
                    return
                continue
            idle_since = time.monotonic()
            for raw in batch:
                if raw.error():
                    if raw.error().code() == KafkaError.UNKNOWN_TOPIC_OR_PART:
                        continue  # the topic has not been created yet; keep waiting for it
                    raise KafkaException(raw.error())
                key = raw.key().decode() if raw.key() is not None else None
                handed[(raw.topic(), raw.partition())] = raw.offset()
                yield Message(raw.topic(), key, json.loads(raw.value()), raw.partition(), raw.offset())

    def commit(self, group: str, upto: Mapping[tuple[str, int], int] | None = None) -> None:
        # Commit what the application has been handed, not what the client has fetched: messages
        # are read from the broker in batches, and the rest of the current batch is still to come.
        consumer = self._consumers.get(group)
        handed = self._handed.get(group)
        if consumer is not None and handed:
            held_back = upto or {}
            consumer.commit(offsets=[
                TopicPartition(topic, partition, min(offset + 1, held_back.get((topic, partition), offset + 1)))
                for (topic, partition), offset in handed.items()], asynchronous=False)

    def close(self) -> None:
        try:
            self.flush()
        finally:
            for consumer in self._consumers.values():
                consumer.close()
            self._consumers.clear()
