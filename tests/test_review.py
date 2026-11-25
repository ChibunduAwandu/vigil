from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from vigil.bus import MemoryBus
from vigil.bus.services import Monitor, publish_match
from vigil.evaluation import evaluate, scenario
from vigil.observability import Metrics
from vigil.pipeline import audit_records
from vigil.reports.viewer import build_viewer
from vigil.review import benchmark
from vigil.rtg.events import write_match_events
from vigil.surveillance.engine import header_for

ROOT = Path(__file__).resolve().parents[1]


def test_challenge_sources_reconcile_and_include_a_difficult_negative():
    protocol = json.loads((ROOT / "config/evaluation.json").read_text())
    for case in protocol["cases"]:
        records, labels, _ = scenario(case, 11, protocol["duration_seconds"])
        assert [r.seq for r in records] == list(range(len(records)))
        assert [r.time for r in records] == sorted(r.time for r in records)
        result = audit_records(header_for(case, "sim", teams=["Subject", "Peer"]), records)
        assert result.result.integrity["discrepancies"] == 0
        if case == "reciprocal":
            assert not labels and any(a.control == "MKT-02" for a in result.alerts)


def test_evaluation_refuses_threshold_drift(tmp_path):
    protocol = json.loads((ROOT / "config/evaluation.json").read_text())
    protocol["rulebook_sha256"] = "0" * 64
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(protocol))
    with pytest.raises(ValueError, match="rulebook changed"):
        evaluate(path)


def test_viewer_matches_evidence_and_escapes_embedded_input(tmp_path):
    records, _, _ = scenario("off_market", 11, 180)
    malicious = "</script><script>alert(1)</script>"
    records = [dataclasses.replace(r, competitor=malicious if r.competitor else "") for r in records]
    report = audit_records(header_for("viewer", "sim", teams=[malicious]), records).report
    report.save(tmp_path / "report.json")
    write_match_events(tmp_path / "events.csv", records)
    build_viewer(tmp_path / "report.json", tmp_path / "events.csv", tmp_path / "view.html")
    html = (tmp_path / "view.html").read_text()
    assert malicious not in html and "\\u003c/script\\u003e" in html
    data = json.loads(html.split('id="data">')[1].split('</script>')[0])
    assert data["events_verified"] is True
    row = next(r for r in data["rows"] if r["seq"] == report.alerts[0].evidence[0])
    assert row["before"] != row["after"]
    (tmp_path / "events.csv").write_text("altered")
    with pytest.raises(ValueError, match="hash differs"):
        build_viewer(tmp_path / "report.json", tmp_path / "events.csv", tmp_path / "bad.html")


def test_event_and_active_budgets_fail_closed():
    records, _, _ = scenario("off_market", 11, 180)
    bus = MemoryBus()
    publish_match(bus, header_for("bounded", "sim"), records)
    report = Monitor(bus, max_events=2).run(idle_timeout=0)[0].report
    assert report.document["match"]["coverage"]["complete"] is False
    assert report.document["summary"]["integrity"]["stopped_after_sequence"] == 1
    from vigil.bus import topics
    bus = MemoryBus()
    for name in ("a", "b"):
        bus.publish(topics.EVENTS, topics.header_message(header_for(name, "sim")), key=name)
    with pytest.raises(RuntimeError, match="maximum active"):
        Monitor(bus, max_active=1).run(idle_timeout=0)


def test_metrics_cumulative_histogram_and_counts(tmp_path):
    metrics = Metrics()
    metrics.observe(.00001)
    metrics.observe(20)
    metrics.counts["rejected_messages"] += 1
    metrics.save(tmp_path / "metrics.json", 2)
    snapshot = json.loads((tmp_path / "metrics.json").read_text())
    assert sum(snapshot["processing_seconds"]["counts"]) == 2
    text = (tmp_path / "metrics.prom").read_text()
    assert 'vigil_processing_seconds_bucket{le="+Inf"} 2' in text
    assert "vigil_active_matches 2" in text and "vigil_rejected_messages_total 1" in text


def test_benchmark_measures_actual_replay(tmp_path):
    records, _, _ = scenario("off_market", 11, 180)
    write_match_events(tmp_path / "events.csv", records)
    result = benchmark(tmp_path / "events.csv", 1)
    assert result["events"] == len(records) and result["median_events_per_second"] > 0
    latency = result["runs"][0]["handler_microseconds"]
    assert latency["p50"] <= latency["p95"] <= latency["p99"]
