"""Topics and the messages that travel on them."""
from __future__ import annotations

import os
from typing import Any

from vigil.rtg.events import MatchHeader, MatchRecord

EVENTS = "rtg.match-events"  # header, events and end marker of every match, keyed by match
ALERTS = "rtg.alerts"  # one message per alert, keyed by participant
REPORTS = "compliance.reports"  # one message per finished report: its fingerprint and where it is anchored
REGULATORY = "regulatory.updates"  # one message per new or changed regulatory document
REJECTED = "rtg.rejected"  # source offsets, reasons and hashes; raw payloads are stored locally

PREFIX = os.environ.get("VIGIL_TOPIC_PREFIX", "")
EVENTS, ALERTS, REPORTS, REGULATORY, REJECTED = (PREFIX + name for name in
                                              (EVENTS, ALERTS, REPORTS, REGULATORY, REJECTED))

# Partitions per topic. A match's messages share a key, so they stay in order on one partition
# while different matches spread across partitions (and across the consumers of a group).
PARTITIONS = {EVENTS: 3, ALERTS: 1, REPORTS: 1, REGULATORY: 1, REJECTED: 1}

_FIELDS = ("seq", "time", "competitor", "op", "order_id", "instrument", "side", "volume", "price", "lifespan",
           "fee")


def header_message(header: MatchHeader) -> dict[str, Any]:
    return {"type": "header", "match": header.match_id, "header": header.to_dict()}


def event_message(match_id: str, record: MatchRecord) -> dict[str, Any]:
    # Positional rather than keyed: half a million of these make a match.
    return {"type": "event", "match": match_id, "record": [getattr(record, name) for name in _FIELDS]}


def end_message(match_id: str, events: int, sha256: str) -> dict[str, Any]:
    """Closes a match and states what was sent, so the consumer can tell whether it received all of it."""
    return {"type": "end", "match": match_id, "events": events, "sha256": sha256}


def record_from(message: dict[str, Any]) -> MatchRecord:
    return MatchRecord(*message["record"])
