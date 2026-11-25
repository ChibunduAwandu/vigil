"""The report for people: Markdown generated from the canonical document."""
from __future__ import annotations

from typing import Any

from vigil.reports.builder import ComplianceReport

_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")


def _money(value: float | None) -> str:
    return "-" if value is None else f"{value:,.2f}"


def _table(header: list[str], rows: list[list[Any]], align: str | None = None) -> list[str]:
    align = align or "l" * len(header)
    rule = ["---:" if a == "r" else "---" for a in align]
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(rule) + "|"]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return lines


def render_markdown(report: ComplianceReport, anchor: dict[str, Any] | None = None) -> str:
    """Render the report. ``anchor`` is the receipt of its on-chain anchoring, if there is one.

    A receipt is a file that was written when the report was filed. It is
    quoted only if it is for this report's fingerprint, and what it says is
    presented as the receipt's word: the chain itself is asked by
    ``vigil chain verify``.
    """
    doc = report.document
    match, summary = doc["match"], doc["summary"]
    lines = [f"# Compliance report: {doc['report_id']}", ""]

    worst = summary["worst_severity"]
    if summary["alerts"]:
        flagged = ", ".join(summary["participants_flagged"])
        lines.append(f"**{summary['alerts']} alerts, the most serious {worst}.** Participants flagged: {flagged}.")
    else:
        lines.append("**No alerts.** Every control ran and none was triggered.")
    integrity = summary["integrity"]
    checked = f"{integrity['trade_reports_checked']:,} trades and {integrity.get('hedges_checked', 0):,} hedges"
    if integrity["discrepancies"]:
        lines.append(f"**The record did not reconcile:** {integrity['discrepancies']} discrepancies between what "
                     f"the exchange reported ({checked}) and an independent replay of the order stream. Findings "
                     "below rest on a record that cannot be fully relied on.")
    else:
        lines.append(f"The record reconciles: the {checked} reported by the exchange are those an independent "
                     "replay of the order stream produces"
                     + (f", and the positions it adds up to are those on the exchange's score board for all "
                        f"{integrity['scores_checked']} teams." if integrity.get("scores_checked") else "."))
    if integrity.get("stopped_after_sequence") is not None:
        lines.append(f"**Surveillance stopped after event {integrity['stopped_after_sequence']:,}**, where the "
                     "stream has a hole. Nothing after that point has been examined.")
    transit = match.get("transit")
    if transit and not transit["complete"]:
        declared = transit["events_declared"]
        lines.append(f"**The event stream arrived incomplete:** {transit['events_received']:,} events"
                     + (f" of {declared:,}" if declared is not None else ", and no end marker")
                     + f"; {transit['missing']:,} missing.")
    lines.append("")

    lines += ["## The match", ""]
    lines += _table(["", ""], [
        ["Source", match["source"]],
        ["Market data", f"{match['market_data'] or '-'}"],
        ["Duration", f"{match['duration_seconds']:,.1f} s"],
        ["Events", f"{match['events']:,}"],
        ["ETF lots traded", f"{match['market']['etf_volume']:,}"],
        ["Teams", ", ".join(match["teams"])],
        ["Position limit", match["limits"].get("PositionLimit")],
        ["Message limit", f"{match['limits'].get('MessageFrequencyLimit')} per "
                          f"{match['limits'].get('MessageFrequencyInterval')} s"],
    ])
    lines.append("")

    lines += ["## Participants", ""]
    rows = []
    for name, entry in doc["participants"].items():
        metrics, exchange = entry["metrics"], entry["exchange"] or {}
        rows.append([
            name, entry["alerts"] or "-", entry["worst_severity"] or "-", exchange.get("status", "-"),
            _money(metrics.get("profit_or_loss")), f"{metrics['executed_lots']:,}",
            f"{metrics['order_to_trade_by_number']:,.1f}", metrics["max_abs_etf_position"],
            f"{metrics['longest_unhedged_seconds']:.0f} s",
        ])
    lines += _table(["Team", "Alerts", "Worst", "Exchange status", "P&L ($)", "Lots traded", "Order-to-trade",
                     "Peak position", "Longest un-hedged"], rows, "lrllrrrrr")
    lines.append("")

    lines += ["## Alerts", ""]
    if not doc["alerts"]:
        lines += ["None.", ""]
    alerts = sorted(doc["alerts"], key=lambda a: (_ORDER.index(a["severity"]), a["time_start"]))
    for alert in alerts:
        cited = ", ".join(alert["regulations"])
        lines += [
            f"### {alert['severity']}: {alert['title']} ({alert['control']}), {alert['competitor']}",
            "",
            f"{alert['description']}",
            "",
            f"- Alert `{alert['alert_id']}`, from {alert['time_start']:.2f} s to {alert['time_end']:.2f} s "
            "of the match.",
        ]
        if alert["counterparties"]:
            lines.append(f"- Counterparty: {', '.join(alert['counterparties'])}.")
        if alert["metrics"]:
            shown = ", ".join(f"{key} = {value}" for key, value in alert["metrics"].items()
                              if not isinstance(value, dict))
            lines.append(f"- Measured: {shown}.")
        if alert["evidence"]:
            events = alert["evidence"]
            sample = ", ".join(str(seq) for seq in events[:8]) + (" ..." if len(events) > 8 else "")
            noun = "event" if len(events) == 1 else "events"
            lines.append(f"- Evidence: {len(events)} {noun} of the match stream (sequence numbers {sample}).")
        lines += [f"- Modelled on: {cited}.", ""]

    controls = doc["rulebook"]["controls_run"]
    lines += ["## Controls run", ""]
    counts = summary["by_control"]
    lines += _table(["Control", "What it watches for", "Alerts", "Modelled on"], [
        [key, control["title"], counts.get(key, 0), ", ".join(control["regulations"])]
        for key, control in controls.items()], "llrl")
    lines.append("")

    if doc["regulations_cited"]:
        lines += ["## Regulations cited", ""]
        for key, regulation in doc["regulations_cited"].items():
            lines.append(f"- **{key}**, {regulation['title']} ({regulation['citation']}; {regulation['authority']}). "
                         f"{regulation['summary']} <{regulation['url']}>")
        lines += ["", "Ready Trader Go is a simulator. The regulations above are the real-world obligations each "
                      "control is modelled on; citing them is a cross-reference, not a legal finding.", ""]

    watch = doc.get("regulatory_watch")
    if watch:
        lines += ["## Regulatory watch", ""]
        lines.append(f"{watch['documents']} documents in the register as of {watch['as_of']}; the most recent "
                     "that bear on the controls above:")
        lines.append("")
        for item in watch["recent"]:
            lines.append(f"- {item['publication_date']}, {item['type']}, {item['agency']}: "
                         f"[{item['title']}]({item['url']}) (touches {', '.join(item['regulations'])})")
        lines.append("")

    lines += ["## Provenance", ""]
    rows = [
        ["Report fingerprint (SHA-256 of `report.json`)", f"`{report.sha256}`"],
        ["Merkle root of the alerts", f"`{report.alerts_root}`"],
        ["Event stream (SHA-256 of `match_events.csv`)", f"`{match['events_sha256']}`"],
        ["Market data", f"`{match['market_data_sha256'] or '-'}`"],
        ["Rulebook", f"`{doc['rulebook']['sha256']}`"],
    ]
    if watch:
        rows.append(["Regulatory register snapshot", f"`{watch['sha256']}`"])
    lines += _table(["", ""], rows)
    lines.append("")
    if anchor and anchor.get("report_sha256") != report.sha256:
        lines += [
            f"**The anchoring receipt kept with this file is for another fingerprint** "
            f"(`{anchor.get('report_sha256')}`). This is not the report that receipt was issued for.",
            "",
        ]
    elif anchor:
        lines += [
            f"Its receipt records it as anchored on chain {anchor['chain_id']} in block {anchor['block_number']} "
            f"(transaction `{anchor['transaction']}`), registry `{anchor['contract']}`, "
            f"as entry {anchor['sequence']}. The registry refuses to store a second report under the same "
            "identifier, so this fingerprint cannot be replaced. To check the file against the chain itself: "
            "`vigil chain verify report.json`.",
            "",
        ]
    else:
        lines += ["Not yet anchored.", ""]
    return "\n".join(lines)
