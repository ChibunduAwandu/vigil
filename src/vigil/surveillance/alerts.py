"""Alerts: what the surveillance engine has to say, in a form that can be audited."""
from __future__ import annotations

import enum
import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any


class Severity(enum.IntEnum):
    INFO = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @classmethod
    def parse(cls, value: str | int | Severity) -> Severity:
        if isinstance(value, Severity):
            return value
        if isinstance(value, int):
            return cls(value)
        return cls[value.upper()]


@dataclass(frozen=True)
class Alert:
    """One finding against one participant.

    ``alert_id`` is derived from what the alert is about rather than from when
    it happened to be raised, so replaying the same events produces the same
    identifiers. ``evidence`` lists the sequence numbers of the match events
    that support the finding: enough to walk from the alert back to the tape.
    """

    alert_id: str
    match_id: str
    control: str  # e.g. "MKT-01"
    title: str
    severity: Severity
    competitor: str
    time_start: float
    time_end: float
    description: str
    metrics: dict[str, Any] = field(default_factory=dict)
    evidence: tuple[int, ...] = ()
    orders: tuple[int, ...] = ()
    counterparties: tuple[str, ...] = ()
    regulations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = self.severity.name
        for key in ("evidence", "orders", "counterparties", "regulations"):
            data[key] = list(data[key])
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Alert:
        data = dict(data)
        data["severity"] = Severity.parse(data["severity"])
        for key in ("evidence", "orders", "counterparties", "regulations"):
            data[key] = tuple(data.get(key, ()))
        return cls(**data)


def alert_id(match_id: str, control: str, competitor: str, key: str) -> str:
    """A stable identifier: the same finding on the same match always gets the same one."""
    digest = hashlib.sha256(f"{match_id}|{control}|{competitor}|{key}".encode()).hexdigest()
    return f"{control}-{digest[:12]}"
