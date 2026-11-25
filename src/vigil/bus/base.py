"""What the pipeline asks of a message bus, and nothing more.

Two implementations exist: Kafka, for the real thing, and an in-memory one
with the same ordering and offset semantics, so that the services can be
tested (and a whole pipeline run in one process) without a broker.
"""
from __future__ import annotations

import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class Message:
    topic: str
    key: str | None
    value: Any
    partition: int = 0
    offset: int = -1
    raw: bytes | None = None
    error: str | None = None
    raw_key: bytes | None = None


@dataclass(frozen=True, slots=True)
class Idle:
    """A polling heartbeat, so services can expire work even on an idle topic."""


@dataclass(frozen=True, slots=True)
class Assignment:
    """Partition ownership changed. In-flight state must not survive revocation."""

    action: str
    partitions: tuple[tuple[str, int], ...]


class Bus(Protocol):
    def publish(self, topic: str, value: dict[str, Any], key: str | None = None) -> None:
        """Queue a message. Messages with the same key keep their order."""

    def flush(self) -> None:
        """Block until everything published has been handed to the broker."""

    def consume(self, topics: Sequence[str], group: str, *, idle_timeout: float | None = None,
                poll_interval: float | None = None) -> Iterator[Message | Idle | Assignment]:
        """Yield messages for ``group``, from where it last committed (or the beginning).

        The iterator ends after ``idle_timeout`` seconds without a message;
        with ``None`` it never ends.
        """

    def commit(self, group: str, upto: Mapping[tuple[str, int], int] | None = None) -> None:
        """Record how far ``group`` has got: by default, through everything it has been handed.

        ``upto`` holds that back where work is unfinished. It maps a
        ``(topic, partition)`` to the offset of the earliest message the group
        would need to see again if it restarted; nothing at or after that
        offset is committed on that partition.
        """

    def close(self) -> None:
        ...


def connect(url: str | None = None) -> Bus:
    """Open a bus from a URL: ``memory://`` or ``kafka://host:port[,host:port]``.

    Defaults to ``$VIGIL_BUS``, then to a Kafka broker on localhost.
    """
    url = url or os.environ.get("VIGIL_BUS") or "kafka://127.0.0.1:9092"
    scheme, _, rest = url.partition("://")
    if scheme == "memory":
        from vigil.bus.memory import MemoryBus
        return MemoryBus()
    if scheme == "kafka":
        from vigil.bus.kafka import KafkaBus
        return KafkaBus(rest)
    raise ValueError(f"unsupported bus URL {url!r}: use memory:// or kafka://host:port")
