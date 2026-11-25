from __future__ import annotations

import base64
import copy
import dataclasses
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from vigil.bus import MemoryBus, topics
from vigil.bus.base import Assignment, Message
from vigil.bus.context import ReplayContexts
from vigil.bus.services import Monitor, publish_match
from vigil.bus.validation import header, record
from vigil.pipeline import audit_records
from vigil.reports.builder import ComplianceReport
from vigil.reports.signing import keygen, sign
from vigil.rtg.events import MatchRecord
from vigil.storage import artifact_path, atomic_write
from vigil.surveillance.engine import header_for

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("independent", ROOT / "tools/verify_report.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def empty(name="case"):
    return header_for(name, "sim", teams=["Alice"])


@pytest.mark.parametrize("value", ["../outside", "/tmp/outside", "a/b", "..", "雪"])
def test_external_identifier_is_not_a_path(tmp_path, value):
    target = artifact_path(tmp_path, value)
    assert target.parent == tmp_path and target.name.startswith("match-")


def test_path_and_report_symlinks_are_rejected(tmp_path):
    (tmp_path / "escape").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError):
        artifact_path(tmp_path, "escape")
    report = audit_records(empty(), []).report
    actual = tmp_path / "actual.json"
    actual.write_text("untouched")
    (tmp_path / "report.json").symlink_to(actual)
    with pytest.raises(ValueError):
        report.save(tmp_path / "report.json")
    assert actual.read_text() == "untouched"


def test_atomic_write_keeps_previous_file_on_replace_failure(tmp_path, monkeypatch):
    path = tmp_path / "report"
    atomic_write(path, b"previous")
    def fail(*args):
        raise OSError("disk error")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        atomic_write(path, b"new")
    assert path.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("field,value", [("speed", 10**400), ("speed", True), ("teams", {}),
                                         ("limits", {"x": float("nan")}), ("instrument", {"TickSize": 0})])
def test_header_validation_is_fail_closed(field, value):
    data = empty().to_dict()
    data[field] = value
    with pytest.raises(ValueError):
        header(data, "case")


@pytest.mark.parametrize("index,value", [(0, True), (1, float("inf")), (3, []), (4, -1), (8, 10**400)])
def test_bad_event_types_do_not_escape_validator(index, value):
    values = [0, 1., "Alice", "Insert", 1, 1, "B", 10, 99900, "G", None]
    values[index] = value
    with pytest.raises(ValueError):
        record(values)


def test_quarantine_preserves_bytes_and_stops_affected_partition():
    bus, rejected = MemoryBus(), []
    monitor = Monitor(bus, on_reject=rejected.append)
    monitor.handle(Message(topics.EVENTS, "case", topics.header_message(empty()), 0, 0))
    monitor.handle(Message(topics.EVENTS, None, None, 0, 1, b"\xff", "invalid UTF-8", b"\xfe"))
    assert base64.b64decode(rejected[0]["raw_base64"]) == b"\xff"
    assert base64.b64decode(rejected[0]["key_base64"]) == b"\xfe"
    assert rejected[0]["offset"] == 1
    assert monitor._matches["case"].engine.stopped_at is not None
    assert len(bus.log(topics.REJECTED)) == 1


def test_quarantine_failure_prevents_commit():
    bus = MemoryBus()
    bus.publish(topics.EVENTS, {"type": "unexpected"})
    def fail(_):
        raise OSError("disk full")
    with pytest.raises(OSError):
        Monitor(bus, on_reject=fail).run(idle_timeout=0)
    assert len(list(bus.consume([topics.EVENTS], "vigil-surveillance", idle_timeout=0))) == 1


def test_idle_poll_expires_a_match_without_another_message():
    bus = MemoryBus()
    bus.publish(topics.EVENTS, topics.header_message(empty()), key="case")
    reports = Monitor(bus, stale_after=.02).run(idle_timeout=.15, poll_interval=.01)
    assert len(reports) == 1 and reports[0].report.delivery["ended"] is False
    assert reports[0].report.document["match"]["coverage"]["complete"] is False


@pytest.mark.parametrize("retained", [0, 2])
def test_many_completed_matches_do_not_accumulate(retained):
    bus, saved = MemoryBus(), []
    for i in range(100):
        publish_match(bus, empty(str(i)), [])
    monitor = Monitor(bus, on_report=lambda a: saved.append(a.report.sha256))
    reports = monitor.run(idle_timeout=0, retain_reports=retained)
    assert len(saved) == 100 and len(reports) == retained and not monitor._matches


def test_rebalance_discards_state_without_emitting_partial_report():
    class Rebalancing(MemoryBus):
        def consume(self, *args, **kwargs):
            yield Message(topics.EVENTS, "case", topics.header_message(empty()), 2, 12)
            yield Assignment("revoke", ((topics.EVENTS, 2),))
    bus = Rebalancing()
    monitor = Monitor(bus)
    assert monitor.run() == [] and monitor.restart_points() == {}
    assert not bus.log(topics.REPORTS)


def test_context_persists_snapshot_and_refuses_rulebook_changes(tmp_path):
    first = {"header_sha256": "h", "rulebook_sha256": "r", "software": {"v": "1"},
             "regulatory_watch": {"snapshot": "first"}}
    store = ReplayContexts(tmp_path / "context.sqlite3")
    store.freeze("match", first)
    store.close()
    store = ReplayContexts(tmp_path / "context.sqlite3")
    assert store.freeze("match", {**first, "regulatory_watch": {"snapshot": "second"}}) == first
    with pytest.raises(ValueError, match="rulebook"):
        store.freeze("match", {**first, "rulebook_sha256": "changed"})
    store.close()


def test_duplicates_do_not_change_report_hash(tmp_path):
    one = MatchRecord(0, 1., "Alice", "Insert", 1, 1, "B", 1, 99900, "G")
    hashes = []
    for duplicate in (False, True):
        bus = MemoryBus()
        publish_match(bus, empty(), [one])
        monitor = Monitor(bus)
        messages = bus.log(topics.EVENTS)
        monitor.handle(messages[0])
        monitor.handle(messages[1])
        if duplicate:
            monitor.handle(messages[1])
        result = monitor.handle(messages[2]).report
        hashes.append(result.sha256)
        result.save(tmp_path / "report.json")
        assert ComplianceReport.load(tmp_path / "report.json").delivery["duplicates_discarded"] == int(duplicate)
    assert hashes[0] == hashes[1]


def test_report_versions_preserve_replaced_bytes(tmp_path):
    first = audit_records(empty(), []).report
    first.save(tmp_path / "report.json")
    second = audit_records(dataclasses.replace(empty(), teams=["Bob"]), []).report
    second.save(tmp_path / "report.json")
    assert (tmp_path / "versions" / (first.sha256 + ".json")).read_bytes() == first.canonical


def test_independent_verifier_legacy_v2_tamper_and_signatures(tmp_path):
    pytest.importorskip("cryptography")
    subprocess.run([sys.executable, "-I", str(ROOT / "tools/verify_report.py"),
                    str(ROOT / "docs/examples/quickstart.json")], check=True, capture_output=True)
    report = audit_records(empty(), []).report
    target = report.save(tmp_path / "report.json")
    verifier.verify(target, report.sha256)
    private, public = keygen(tmp_path / "keys")
    envelope = tmp_path / "signature.json"
    sign(target, private, envelope)
    verifier.verify_signature(report.sha256, envelope, public)
    assert private.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="already exist"):
        keygen(tmp_path / "keys")
    wrong = keygen(tmp_path / "wrong")[1]
    with pytest.raises(ValueError):
        verifier.verify_signature(report.sha256, envelope, wrong)
    changed = copy.deepcopy(report.document)
    changed["match"]["teams"] = ["Mallory"]
    target.write_text(json.dumps(changed, sort_keys=True, separators=(",", ":")))
    with pytest.raises(ValueError, match="trusted hash"):
        verifier.verify(target, report.sha256)
    with pytest.raises(ValueError, match="different report"):
        verifier.verify_signature(hashlib.sha256(target.read_bytes()).hexdigest(), envelope, public)


def test_independent_proofs_cover_unbalanced_trees():
    from vigil.chain.merkle import inclusion_proof, leaf_hash, root_of
    from vigil.reports.canonical import canonical_json
    for size in range(1, 26):
        items = [{"n": n, "text": "café"} for n in range(size)]
        leaves = [leaf_hash(canonical_json(i)) for i in items]
        expected = root_of(leaves).hex()
        assert verifier.root(items).hex() == expected
        for index in range(size):
            proof = {"size": size, "index": index, "alert": items[index],
                     "proof": [s.hex() for s in inclusion_proof(leaves, index)]}
            assert verifier.check_proof(proof, expected) == expected
            proof["alert"] = {"n": -1}
            with pytest.raises(ValueError):
                verifier.check_proof(proof, expected)
