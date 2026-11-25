"""The two ends of the pipeline: publishing a match, and monitoring what is published."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from vigil.bus import topics, validation
from vigil.bus.base import Assignment, Bus, Idle, Message
from vigil.bus.context import ReplayContexts
from vigil.observability import Metrics
from vigil.pipeline import MatchAudit
from vigil.provenance import environment, software
from vigil.reports.builder import build_report
from vigil.reports.canonical import EventStreamHasher, canonical_json
from vigil.rtg.events import MatchHeader, MatchRecord
from vigil.surveillance.engine import SurveillanceEngine
from vigil.surveillance.rulebook import Rulebook, load_rulebook

log = logging.getLogger("vigil.bus")


class MatchPublisher:
    """Writes one match to the bus: a header, the events in order, and an end marker."""

    def __init__(self, bus: Bus, header: MatchHeader):
        self.bus = bus
        self.header = header
        self._hasher = EventStreamHasher()
        bus.publish(topics.EVENTS, topics.header_message(header), key=header.match_id)

    def event(self, record: MatchRecord) -> None:
        self._hasher.update(record)
        self.bus.publish(topics.EVENTS, topics.event_message(self.header.match_id, record), key=self.header.match_id)

    def end(self) -> tuple[int, str]:
        count, digest = self._hasher.count, self._hasher.hexdigest()
        self.bus.publish(topics.EVENTS, topics.end_message(self.header.match_id, count, digest),
                         key=self.header.match_id)
        self.bus.flush()
        return count, digest


def publish_match(bus: Bus, header: MatchHeader, records: Iterable[MatchRecord]) -> tuple[int, str]:
    """Publish a whole match; returns the number of events and the fingerprint of the stream."""
    publisher = MatchPublisher(bus, header)
    for record in records:
        publisher.event(record)
    return publisher.end()


@dataclass
class _Match:
    engine: SurveillanceEngine
    header: MatchHeader
    origin: tuple[str, int, int] | None  # topic, partition and offset of its header: where a restart resumes
    heard: float  # when its last message arrived, on the monitor's clock
    hasher: EventStreamHasher = field(default_factory=EventStreamHasher)
    regulatory_watch: dict[str, Any] | None = None
    last_seq: int = -1
    duplicates: int = 0  # events delivered again
    missing: int = 0  # events that never came
    late: int = 0  # events that came after the stream had moved past them, too late to be of use
    unreadable: int = 0
    other_headers: int = 0  # a second, different header under the same identifier
    holes: list[tuple[int, int]] = field(default_factory=list)


class Monitor:
    """The surveillance service: consumes matches from the bus, publishes alerts and reports.

    It keeps one engine per match in flight, so matches may interleave.
    Delivery is at least once. The monitor's place on each partition is
    committed when a match has been reported, and never past the header of a
    match still in flight, so a monitor that dies mid-match starts that match
    again from its header when it comes back. The price is that matches which
    ended after the oldest unfinished one began are read, and reported, a
    second time. They come out the same: a replayed match raises the same
    alerts under the same identifiers and gives the same report fingerprint,
    which the registry will not accept twice. Whoever consumes the alerts
    should key on the identifier.

    Within one run, an event delivered twice is recognised by its sequence
    number and dropped. An event that never arrives leaves a hole the order
    books cannot be rebuilt across: the engine says so in an alert and stops
    following that match, and the report states that the stream was incomplete.

    A match that goes quiet without an end marker for ``stale_after`` seconds
    is reported as it stands, as incomplete, so that it cannot hold up the
    commits of everything that comes after it.
    """

    def __init__(self, bus: Bus, *, rulebook: Rulebook | None = None, group: str = "vigil-surveillance",
                 on_report: Callable[[MatchAudit], dict[str, Any] | None] | None = None,
                 regulatory_watch: Callable[[], dict[str, Any] | None] | None = None,
                 stale_after: float | None = 300.0, clock: Callable[[], float] = time.monotonic,
                 on_reject: Callable[[dict], None] | None = None, contexts: ReplayContexts | None = None,
                 metrics: Metrics | None = None, on_tick: Callable[[Metrics, int], None] | None = None,
                 max_active: int = 16, max_events: int = 1_000_000):
        if max_active < 1 or max_events < 1 or (stale_after is not None and stale_after <= 0):
            raise ValueError("monitor limits must be positive")
        self.on_reject, self.contexts = on_reject, contexts
        runtime = environment() if contexts else {}
        self._runtime = {k: runtime.get(k) for k in ("python", "dependencies_sha256")}
        self.metrics, self.on_tick = metrics or Metrics(), on_tick
        self.max_active, self.max_events = max_active, max_events
        self.bus = bus
        self.rulebook = rulebook or load_rulebook()
        self.group = group
        self.on_report = on_report
        self.regulatory_watch = regulatory_watch
        self.stale_after = stale_after
        self._clock = clock
        self._matches: dict[str, _Match] = {}
        self.orphans = 0  # events and end markers for a match whose header this monitor never saw
        self.unreadable = 0  # messages that are not match messages at all

    def run(self, *, idle_timeout: float | None = None, max_matches: int | None = None,
            retain_reports: int = 256, poll_interval: float = 0.5) -> list[MatchAudit]:
        """Run with bounded retention. Services use retain_reports=0 and the report callback."""
        audits: deque[MatchAudit] = deque(maxlen=max(0, retain_reports))
        completed, tick = 0, time.monotonic()
        for message in self.bus.consume([topics.EVENTS], self.group, idle_timeout=idle_timeout,
                                        poll_interval=poll_interval):
            started = time.perf_counter()
            if isinstance(message, Assignment):
                self.metrics.counts["partition_" + message.action] += len(message.partitions)
                if message.action != "assign":
                    revoked = set(message.partitions)
                    for name, match in list(self._matches.items()):
                        if match.origin and match.origin[:2] in revoked:
                            del self._matches[name]  # no report/commit: next owner replays the retained header
                audit = None
            elif isinstance(message, Idle):
                audit = None
            else:
                audit = self.handle(message)
                self.metrics.observe(time.perf_counter() - started)
            reported = ([audit] if audit is not None else []) + self.abandon_stale()
            if reported:
                audits.extend(reported)
                completed += len(reported)
            if reported or isinstance(message, Idle) or (isinstance(message, Message) and message.error):
                # Rejected records have been durably quarantined and output flushed before committing.
                self.bus.flush()
                self.bus.commit(self.group, self.restart_points())
            if self.on_tick and (time.monotonic() - tick >= 1 or reported):
                self.on_tick(self.metrics, len(self._matches))
                tick = time.monotonic()
            if max_matches is not None and completed >= max_matches:
                break
        if self.on_tick:
            self.on_tick(self.metrics, len(self._matches))
        return list(audits)

    def restart_points(self) -> dict[tuple[str, int], int]:
        """Per partition, the earliest message a restarted monitor would need: the header of its oldest match in flight."""
        points: dict[tuple[str, int], int] = {}
        for match in self._matches.values():
            if match.origin is not None:
                topic, partition, offset = match.origin
                points[(topic, partition)] = min(offset, points.get((topic, partition), offset))
        return points

    def abandon_stale(self) -> list[MatchAudit]:
        """Report, as incomplete, the matches that have gone quiet without ending."""
        if self.stale_after is None:
            return []
        now = self._clock()
        stale = [name for name, match in self._matches.items() if now - match.heard > self.stale_after]
        for name in stale:
            log.warning("match %s has been silent for %.0f s without an end marker; reporting it as it stands",
                        name, now - self._matches[name].heard)
        reports = []
        for name in stale:
            reports.append(self._report(self._matches[name], None))
            del self._matches[name]
        return reports

    # -- one message ---------------------------------------------------------------------------

    def handle(self, message: Message | dict[str, Any]) -> MatchAudit | None:
        """Process one message; returns the audit when it completes a match."""
        if isinstance(message, Message):
            value, origin = message.value, (message.topic, message.partition, message.offset)
        else:
            value, origin = message, None
        try:
            if isinstance(message, Message) and message.error:
                raise ValueError(message.error)
            kind, match_id = validation.envelope(value)
            if isinstance(message, Message) and message.key is not None and message.key != match_id:
                raise ValueError("message key and envelope identifier differ")
        except ValueError as error:
            self._reject(message, str(error))
            return None
        if kind == "event":
            self._event(match_id, value, message)
            return None
        if kind == "header":
            self._header(match_id, value, origin, message)
            return None
        if kind == "end":
            try:
                validation.end(value)
            except ValueError as error:
                self._reject(message, str(error))
                return None
            match = self._matches.get(match_id)
            if match is None:
                self.orphans += 1
                self.metrics.counts["orphan_messages"] += 1
                log.warning("end marker for match %s, which this monitor never saw begin", match_id)
                return None
            audit = self._report(match, value)
            del self._matches[match_id]
            return audit
        self.unreadable += 1
        log.warning("ignoring message of unknown type %r", kind)
        return None

    def _reject(self, message: Message | dict, reason: str) -> None:
        self.unreadable += 1
        self.metrics.counts["rejected_messages"] += 1
        value = message.value if isinstance(message, Message) else message
        raw = message.raw if isinstance(message, Message) else None
        if raw is None:
            raw = json.dumps(value, ensure_ascii=True, default=repr).encode()
        location = {"topic": message.topic, "partition": message.partition, "offset": message.offset} if isinstance(
            message, Message) else {"topic": "direct", "partition": 0, "offset": -1}
        rejection = {**location, "reason": reason, "sha256": hashlib.sha256(raw).hexdigest(),
                     "raw_base64": base64.b64encode(raw).decode()}
        if isinstance(message, Message) and message.raw_key is not None:
            rejection["key_base64"] = base64.b64encode(message.raw_key).decode()
        if self.on_reject:
            self.on_reject(rejection)  # failure must prevent committing this input
        self.bus.publish(topics.REJECTED, {k: v for k, v in rejection.items() if k != "raw_base64"},
                         key=rejection["sha256"])
        match_id = value.get("match") if isinstance(value, dict) else None
        if not isinstance(match_id, str) and isinstance(message, Message):
            match_id = message.key
        candidates = {match_id} if isinstance(match_id, str) else set()
        if isinstance(message, Message) and isinstance(message.key, str):
            candidates.add(message.key)
        known = candidates.intersection(self._matches)
        for name, match in self._matches.items():
            affected = name in known or (not known and (not isinstance(message, Message) or
                        (match.origin and match.origin[:2] == (message.topic, message.partition))))
            if affected:
                match.unreadable += 1
                for alert in match.engine.gap(match.last_seq, 1):
                    self.bus.publish(topics.ALERTS, alert.to_dict(), key=alert.competitor)
        self.bus.flush()
        log.warning("rejected message at %s:%s:%s: %s", location["topic"], location["partition"], location["offset"], reason)

    def _header(self, match_id: str, value: dict[str, Any], origin: tuple[str, int, int] | None,
                message: Message | dict) -> None:
        try:
            header = validation.header(value.get("header"), match_id)
        except ValueError as error:
            self._reject(message, str(error))
            return
        match = self._matches.get(match_id)
        if match is None:
            if len(self._matches) >= self.max_active:
                raise RuntimeError("maximum active matches reached; input remains uncommitted for replay")
            watch = copy.deepcopy(self.regulatory_watch()) if self.regulatory_watch else None
            context = {"header_sha256": hashlib.sha256(canonical_json(header.to_dict())).hexdigest(),
                       "rulebook_sha256": self.rulebook.sha256, "software": software(), "regulatory_watch": watch,
                       "runtime": self._runtime}
            if self.contexts:
                key = json.dumps([self.group, origin, match_id])
                context = self.contexts.freeze(key, context)
            self._matches[match_id] = _Match(SurveillanceEngine(header, self.rulebook), header, origin, self._clock(),
                                            regulatory_watch=context["regulatory_watch"])
            log.info("match %s started: teams %s", match_id, ", ".join(header.teams))
        elif header != match.header:
            # Two publishers using one identifier. The first keeps it; what the second sends will
            # show up as duplicates and as a stream that does not hash to what either declared.
            match.other_headers += 1
            self._reject(message, "conflicting header for an active match")
            log.warning("a second, different header arrived for match %s, which is in flight; keeping the first",
                        match_id)
        # else: the header delivered again. So will its events be, and their sequence numbers say so.

    def _event(self, match_id: str, value: dict[str, Any], message: Message | dict) -> None:
        match = self._matches.get(match_id)
        if match is None:
            self.orphans += 1
            return
        match.heard = self._clock()
        try:
            record = validation.record(value.get("record"))
            seq = record.seq
        except ValueError as error:
            self._reject(message, str(error))
            return
        if seq <= match.last_seq:
            if any(start <= seq < stop for start, stop in match.holes):
                match.late += 1
                self.metrics.counts["late_events"] += 1
            else:
                match.duplicates += 1
                self.metrics.counts["duplicate_events"] += 1
            return
        alerts = []
        if seq != match.last_seq + 1:
            lost = seq - match.last_seq - 1
            match.missing += lost
            self.metrics.counts["missing_events"] += lost
            if len(match.holes) < 1024:
                match.holes.append((match.last_seq + 1, seq))
            alerts += match.engine.gap(match.last_seq, lost)
        match.last_seq = seq
        match.hasher.update(record)
        if match.hasher.count > self.max_events:
            alerts += match.engine.gap(match.last_seq - 1, 1)
            self.metrics.counts["events_over_limit"] += 1
        else:
            alerts += match.engine.process(record)
        self.metrics.counts["events"] += 1
        for alert in alerts:
            self.bus.publish(topics.ALERTS, alert.to_dict(), key=alert.competitor)

    # -- the end of a match --------------------------------------------------------------------

    def _report(self, match: _Match, end: dict[str, Any] | None) -> MatchAudit:
        """Close a match and publish its report. ``end`` is its end marker, or ``None`` if none ever came."""
        declared = end.get("events") if end else None
        digest = end.get("sha256") if end else None
        missing = match.missing
        already = {alert.alert_id for alert in match.engine.alerts}
        if isinstance(declared, int) and declared > match.last_seq + 1:
            lost = declared - (match.last_seq + 1)  # the tail of the stream never arrived
            missing += lost
            match.engine.gap(match.last_seq, lost)
        result = match.engine.finalize()
        for alert in result.alerts:
            if alert.alert_id not in already:
                self.bus.publish(topics.ALERTS, alert.to_dict(), key=alert.competitor)

        received = match.hasher.hexdigest()
        transit = {
            "ended": end is not None,
            "events_declared": declared,
            "events_received": match.hasher.count,
            "sha256_declared": digest,
            "duplicates_discarded": match.duplicates,
            "late": match.late,
            "missing": missing,
            "unreadable": match.unreadable,
            "conflicting_headers": match.other_headers,
            "complete": (end is not None and match.hasher.count == declared and received == digest
                         and not match.unreadable and not match.other_headers and match.engine.stopped_at is None),
        }
        watch = match.regulatory_watch
        report = build_report(result, self.rulebook, events_sha256=received, transit=transit,
                              regulatory_watch=watch)
        audit = MatchAudit(result.header, result, report)
        receipt = self.on_report(audit) if self.on_report else None
        self.bus.publish(topics.REPORTS, {
            "report_id": report.report_id,
            "sha256": report.sha256,
            "alerts_merkle_root": report.alerts_root,
            "alerts": len(result.alerts),
            "worst_severity": report.document["summary"]["worst_severity"],
            "participants_flagged": report.document["summary"]["participants_flagged"],
            "stream_complete": transit["complete"],
            "anchor": receipt,
        }, key=report.report_id)
        self.bus.flush()
        log.info("match %s reported: %d alerts, stream %s", report.report_id, len(result.alerts),
                 "complete" if transit["complete"] else "INCOMPLETE")
        self.metrics.counts["reports"] += 1
        self.metrics.counts["alerts"] += len(result.alerts)
        self.metrics.counts["incomplete_matches"] += int(not transit["complete"])
        return audit
