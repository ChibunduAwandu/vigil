"""The compliance report.

A report is a pure function of its inputs: the match (its header and event
stream), the rulebook it was judged against and, optionally, the regulatory
register as it stood. It records nothing about when or where it was produced,
so regenerating it from the same inputs gives the same bytes and therefore the
same fingerprint. *When* it was produced and by whom is recorded by the anchor
on chain, not by the report.
"""
from __future__ import annotations

import csv
import hashlib
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import vigil
from vigil.chain.merkle import inclusion_proof, leaf_hash, root_of
from vigil.reports.canonical import canonical_json, parse_strict, sha256_hex
from vigil.sim.result import MatchResult
from vigil.surveillance.alerts import Alert, Severity
from vigil.surveillance.engine import SurveillanceResult
from vigil.surveillance.rulebook import Rulebook

SCHEMA = "vigil.compliance-report/1"


@dataclass(frozen=True)
class ComplianceReport:
    document: dict[str, Any]
    raw: bytes | None = None  # the bytes of the file it was read from, when it was read from one

    # -- identity ------------------------------------------------------------------------------

    @property
    def report_id(self) -> str:
        return self.document["report_id"]

    @property
    def canonical(self) -> bytes:
        return canonical_json(self.document)

    @property
    def sha256(self) -> str:
        """The report's fingerprint: what gets anchored, and what ``shasum -a 256`` prints for the file.

        For a report read from a file this is the hash of that file's bytes,
        whatever they are. A copy saved in another layout is a different file
        with a different fingerprint, even if it says the same thing.
        """
        return sha256_hex(self.raw if self.raw is not None else self.canonical)

    @property
    def in_canonical_form(self) -> bool:
        return self.raw is None or self.raw == self.canonical

    @property
    def key(self) -> str:
        """The 32-byte key under which the report is anchored: the hash of its identifier."""
        return hashlib.sha256(self.report_id.encode()).hexdigest()

    # -- contents ------------------------------------------------------------------------------

    @property
    def alerts(self) -> list[Alert]:
        return [Alert.from_dict(item) for item in self.document["alerts"]]

    @property
    def alerts_root(self) -> str:
        return self.document["alerts_merkle_root"]

    @property
    def worst_severity(self) -> Severity | None:
        """The most serious severity among the alerts themselves, not the one the summary states."""
        return max((Severity.parse(item["severity"]) for item in self.document["alerts"]), default=None)

    def defects(self) -> list[str]:
        """Ways in which the report is not what ``build_report`` writes: empty for a sound one.

        The summary, the Merkle root and the fingerprints are all stated in the
        document, and what is filed on chain must be what the alerts themselves
        say. A document whose statements about itself are untrue, or a file
        that is not in the canonical layout, has been altered since it was
        built.
        """
        document, found = self.document, []
        if not self.in_canonical_form:
            found.append("the file is not in the canonical layout a report is written in")
        if document.get("schema") != SCHEMA:
            found.append(f"its schema is {document.get('schema')!r}, not {SCHEMA!r}")
        alerts = document["alerts"]
        if document["alerts_merkle_root"] != alerts_merkle_root(alerts):
            found.append("the Merkle root it states is not the root of its alerts")
        if document["summary"]["alerts"] != len(alerts):
            found.append(f"its summary counts {document['summary']['alerts']} alerts and it holds {len(alerts)}")
        worst = self.worst_severity
        if document["summary"]["worst_severity"] != (worst.name if worst is not None else None):
            found.append(f"its summary gives the worst severity as {document['summary']['worst_severity']} and "
                         f"its alerts make it {worst.name if worst is not None else None}")
        identifiers = [item["alert_id"] for item in alerts]
        if len(set(identifiers)) != len(identifiers):
            found.append("two of its alerts share an identifier")
        for name, value in (("event stream", document["match"]["events_sha256"]),
                            ("rulebook", document["rulebook"]["sha256"])):
            if not _is_sha256(value):
                found.append(f"its {name} fingerprint is not a SHA-256 digest")
        return found

    def alert_proof(self, alert_id: str) -> dict[str, Any]:
        """Evidence that one alert belongs to this report, checkable against the anchored root alone."""
        items = self.document["alerts"]
        index = next((i for i, item in enumerate(items) if item["alert_id"] == alert_id), None)
        if index is None:
            raise KeyError(f"no alert {alert_id!r} in report {self.report_id}")
        leaves = [leaf_hash(canonical_json(item)) for item in items]
        return {
            "report_id": self.report_id,
            "alert": items[index],
            "leaf": leaves[index].hex(),
            "index": index,
            "size": len(leaves),
            "proof": [sibling.hex() for sibling in inclusion_proof(leaves, index)],
            "root": self.alerts_root,
        }

    # -- files ---------------------------------------------------------------------------------

    def save(self, path: str | os.PathLike) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.canonical)
        return path

    @classmethod
    def load(cls, path: str | os.PathLike) -> ComplianceReport:
        """Read a report file, keeping its bytes. Raises ``ValueError`` if it is not a report at all."""
        raw = Path(path).read_bytes()
        try:
            document = parse_strict(raw)
            _require_shape(document)
        except ValueError as error:
            raise ValueError(f"{path} cannot be read as a report: {error}") from None
        return cls(document, raw)


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _require_shape(document: Any) -> None:
    """The parts of a report that everything else reads, present and of the right kind."""
    if not isinstance(document, dict):
        raise ValueError("it is not a JSON object")
    kinds = {"report_id": str, "alerts": list, "alerts_merkle_root": str, "summary": dict, "match": dict,
             "rulebook": dict, "participants": dict}
    for key, kind in kinds.items():
        if not isinstance(document.get(key), kind):
            raise ValueError(f"it has no {key!r}" if key not in document else f"its {key!r} is not the right kind")
    for item in document["alerts"]:
        if not isinstance(item, dict) or not isinstance(item.get("alert_id"), str) or "severity" not in item:
            raise ValueError("one of its alerts is not an alert")
        try:
            Severity.parse(item["severity"])
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ValueError(f"alert {item['alert_id']} has no recognisable severity") from None
    for key, within in (("alerts", "summary"), ("worst_severity", "summary"), ("events_sha256", "match"),
                        ("sha256", "rulebook")):
        if key not in document[within]:
            raise ValueError(f"its {within} has no {key!r}")


def alerts_merkle_root(alerts: list[dict[str, Any]]) -> str:
    return root_of([leaf_hash(canonical_json(item)) for item in alerts]).hex()


def build_report(result: SurveillanceResult, rulebook: Rulebook, *, events_sha256: str,
                 outcomes: dict[str, dict[str, Any]] | None = None,
                 regulatory_watch: dict[str, Any] | None = None,
                 transit: dict[str, Any] | None = None) -> ComplianceReport:
    """Assemble the report for one match.

    ``outcomes`` is what the exchange itself decided about each team (status,
    score), where that is known; ``regulatory_watch`` is the scraper's view of
    recent rule-making relevant to the controls that ran; ``transit`` says
    whether the event stream arrived over the bus as its publisher sent it.
    """
    if result.rulebook_sha256 != rulebook.sha256:
        raise ValueError("the surveillance result was produced with a different rulebook")
    alerts = [alert.to_dict() for alert in result.alerts]
    by_competitor: dict[str, list[Alert]] = {}
    for alert in result.alerts:
        by_competitor.setdefault(alert.competitor, []).append(alert)

    participants = {}
    for name, metrics in result.participants.items():
        own = by_competitor.get(name, [])
        participants[name] = {
            "metrics": metrics,
            "alerts": len(own),
            "worst_severity": max((a.severity for a in own), default=None),
            "controls_triggered": sorted({a.control for a in own}),
            "exchange": (outcomes or {}).get(name),
        }
        worst = participants[name]["worst_severity"]
        participants[name]["worst_severity"] = worst.name if worst is not None else None

    cited = sorted({key for alert in result.alerts for key in alert.regulations})
    severities = Counter(alert.severity.name for alert in result.alerts)
    worst = result.worst()
    header = result.header
    document = {
        "schema": SCHEMA,
        "report_id": header.match_id,
        "match": {
            "source": header.source,
            "started_at": header.started_at or None,
            "teams": sorted(header.teams),
            "market_data": header.market_data,
            "market_data_sha256": header.market_data_sha256,
            "events": result.market["events"],
            "events_sha256": events_sha256,
            "transit": transit,
            "duration_seconds": result.market["duration"],
            "limits": header.limits,
            "fees": header.fees,
            "instrument": header.instrument,
            "speed": header.speed,
            "market": result.market,
        },
        "rulebook": {
            "version": rulebook.version,
            "sha256": rulebook.sha256,
            "controls_run": {key: rulebook.control(key).to_dict() for key in result.controls_run},
        },
        "summary": {
            "alerts": len(alerts),
            "by_severity": {level.name: severities.get(level.name, 0) for level in reversed(Severity)},
            "by_control": dict(sorted(Counter(alert.control for alert in result.alerts).items())),
            "participants_flagged": sorted(name for name, own in by_competitor.items() if own),
            "worst_severity": worst.name if worst is not None else None,
            "integrity": result.integrity,
        },
        "participants": participants,
        "alerts": alerts,
        "alerts_merkle_root": alerts_merkle_root(alerts),
        "regulations_cited": {key: rulebook.regulations[key].to_dict() for key in cited},
        "regulatory_watch": regulatory_watch,
        "software": {"vigil": vigil.__version__},
    }
    return ComplianceReport(document)


def outcomes_from_simulation(result: MatchResult) -> dict[str, dict[str, Any]]:
    """What the (simulated) exchange decided about each team."""
    return {
        name: {
            "status": team.status,
            "breach_time": team.breach_time,
            "breach_reason": team.breach_reason,
            "profit_or_loss": round(team.pnl, 2),
            "etf_position": team.etf_position,
            "future_position": team.future_position,
        }
        for name, team in result.teams.items()
    }


def outcomes_from_score_board(path: str | os.PathLike) -> dict[str, dict[str, Any]]:
    """The same, read from a ``score_board.csv`` written by the real exchange."""
    outcomes: dict[str, dict[str, Any]] = {}
    with open(path, newline="") as handle:
        for row in csv.DictReader(handle):
            team = outcomes.setdefault(row["Team"], {"status": "OK", "breach_time": None, "breach_reason": None,
                                                     "profit_or_loss": 0.0, "etf_position": None,
                                                     "future_position": None})
            if row["Operation"] == "Breach":
                team["status"] = "BREACH"
                if team["breach_time"] is None:
                    team["breach_time"] = float(row["Time"])
            elif row["Operation"] == "Tick":
                team["profit_or_loss"] = round(float(row["ProfitOrLoss"]) / 100.0, 2)
                team["etf_position"], team["future_position"] = int(row["EtfPosition"]), int(row["FuturePosition"])
                if row.get("Status") == "BREACH":
                    team["status"] = "BREACH"
    return outcomes
