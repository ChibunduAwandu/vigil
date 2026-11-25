"""An in-process bus with Kafka's semantics: an append-only log per topic, offsets per consumer group."""
from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from vigil.bus.base import Idle, Message


class MemoryBus:
    def __init__(self) -> None:
        self._logs: dict[str, list[Message]] = {}
        self._committed: dict[tuple[str, str], int] = {}  # (group, topic) -> next offset
        self._position: dict[tuple[str, str], int] = {}
        self._lock = threading.Condition()

    def publish(self, topic: str, value: dict[str, Any], key: str | None = None) -> None:
        with self._lock:
            log = self._logs.setdefault(topic, [])
            log.append(Message(topic, key, value, 0, len(log)))
            self._lock.notify_all()

    def flush(self) -> None:
        pass

    def consume(self, topics: Sequence[str], group: str, *, idle_timeout: float | None = None,
                poll_interval: float | None = None) -> Iterator[Message | Idle]:
        for topic in topics:
            self._position[(group, topic)] = self._committed.get((group, topic), 0)
        idle_since = time.monotonic()
        while True:
            with self._lock:
                message = self._next(topics, group)
                if message is None:
                    remaining = None if idle_timeout is None else idle_timeout - (time.monotonic() - idle_since)
                    if remaining is not None and remaining <= 0:
                        return
                    timeout = remaining if poll_interval is None else min(remaining or poll_interval, poll_interval)
                    self._lock.wait(timeout)
                    message = self._next(topics, group)
            if message is not None:
                idle_since = time.monotonic()
                yield message
            elif poll_interval is not None:
                yield Idle()

    def _next(self, topics: Sequence[str], group: str) -> Message | None:
        for topic in topics:
            log = self._logs.get(topic, [])
            position = self._position[(group, topic)]
            if position < len(log):
                self._position[(group, topic)] = position + 1
                return log[position]
        return None

    def commit(self, group: str, upto: Mapping[tuple[str, int], int] | None = None) -> None:
        with self._lock:
            for (owner, topic), position in self._position.items():
                if owner == group:
                    held_back = (upto or {}).get((topic, 0), position)  # every topic here is one partition
                    self._committed[(owner, topic)] = min(position, held_back)

    def close(self) -> None:
        pass

    # -- for tests -----------------------------------------------------------------------------

    def log(self, topic: str) -> list[Message]:
        return list(self._logs.get(topic, []))
