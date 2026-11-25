"""Generate a self-contained evidence browser; all imported data is inert JSON/text."""
from __future__ import annotations

import json
from pathlib import Path

from vigil.provenance import file_hash
from vigil.reports.builder import ComplianceReport
from vigil.rtg.events import AMEND, CANCEL, INSERT, read_match_events
from vigil.storage import atomic_write
from vigil.surveillance.market import MarketReplica


def build_viewer(report_path: Path, events: Path | None, output: Path) -> None:
    report = ComplianceReport.load(report_path)
    if defects := report.defects():
        raise ValueError("invalid report: " + "; ".join(defects))
    doc, rows = report.document, []
    if events:
        if file_hash(events) != doc["match"]["events_sha256"]:
            raise ValueError("event file hash differs from report")
        wanted = set()
        for alert in doc["alerts"]:
            for seq in alert.get("evidence", [])[:100]:
                wanted.update(range(max(0, seq - 2), seq + 3))
        wanted = set(sorted(wanted)[:5000])
        fees = doc["match"]["fees"]
        replica = MarketReplica(fees.get("Maker", -.0001), fees.get("Taker", .0002))
        stopped = doc["summary"]["integrity"]["stopped_after_sequence"]
        def book():
            return {"future": replica.top_levels(0), "etf": replica.top_levels(1)}
        for record in read_match_events(events):
            usable = stopped is None or record.seq <= stopped
            before = book() if record.seq in wanted and usable else None
            if usable and record.op in (INSERT, AMEND, CANCEL):
                replica.apply(record)
                replica.expected.clear()  # viewer reconstructs books, not trade-report reconciliation
            if record.seq in wanted:
                rows.append({**record.to_dict(), "before": before,
                             "after": book() if usable else None})
    payload = json.dumps({"report": doc, "hash": report.sha256, "events_verified": bool(events), "rows": rows},
                         ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    template = Path(__file__).with_name("viewer.html").read_text()
    atomic_write(output, template.replace("__VIGIL_DATA__", payload).encode())
