"""The rulebook: controls, their thresholds, and the regulations they answer to."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from vigil.paths import config_dir
from vigil.surveillance.alerts import Severity


@dataclass(frozen=True)
class Regulation:
    key: str
    title: str
    citation: str
    authority: str
    jurisdiction: str
    url: str
    summary: str
    watch: dict[str, Any] = field(default_factory=dict)  # what the regulatory scraper follows for it

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "title": self.title, "citation": self.citation, "authority": self.authority,
                "jurisdiction": self.jurisdiction, "url": self.url, "summary": self.summary}


@dataclass(frozen=True)
class Control:
    key: str
    title: str
    category: str
    severity: Severity
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    regulations: tuple[str, ...] = ()

    def param(self, name: str, default: Any = None) -> Any:
        return self.parameters.get(name, default)

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "title": self.title, "category": self.category,
                "severity": self.severity.name, "description": self.description,
                "parameters": dict(self.parameters), "regulations": list(self.regulations)}


@dataclass(frozen=True)
class Rulebook:
    version: int
    controls: dict[str, Control]
    regulations: dict[str, Regulation]
    sha256: str  # of the file it was loaded from: what ``shasum -a 256 rulebook.yaml`` prints

    def control(self, key: str) -> Control:
        return self.controls[key]

    def regulations_for(self, control: str) -> list[Regulation]:
        return [self.regulations[key] for key in self.controls[control].regulations]

    def controls_citing(self, regulation: str) -> list[Control]:
        return [control for control in self.controls.values() if regulation in control.regulations]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "sha256": self.sha256,
            "controls": {key: control.to_dict() for key, control in self.controls.items()},
            "regulations": {key: regulation.to_dict() for key, regulation in self.regulations.items()},
        }


def default_rulebook_path() -> Path:
    return config_dir() / "rulebook.yaml"


def load_rulebook(path: str | os.PathLike | None = None) -> Rulebook:
    path = Path(path) if path is not None else default_rulebook_path()
    data = path.read_bytes()
    return parse_rulebook(yaml.safe_load(data), sha256=hashlib.sha256(data).hexdigest())


def parse_rulebook(raw: dict[str, Any], sha256: str | None = None) -> Rulebook:
    """Build a rulebook from parsed YAML.

    ``sha256`` is the fingerprint of the file the YAML came from. A rulebook
    that did not come from a file is identified by the hash of its content
    written as canonical JSON instead.
    """
    regulations = {
        key: Regulation(key=key, title=body["title"], citation=body["citation"], authority=body["authority"],
                        jurisdiction=body["jurisdiction"], url=body["url"], summary=body["summary"].strip(),
                        watch=body.get("watch", {}))
        for key, body in raw.get("regulations", {}).items()
    }
    controls = {}
    for key, body in raw.get("controls", {}).items():
        cited = tuple(body.get("regulations", ()))
        unknown = [name for name in cited if name not in regulations]
        if unknown:
            raise ValueError(f"control {key} cites regulations that are not defined: {unknown}")
        controls[key] = Control(key=key, title=body["title"], category=body["category"],
                                severity=Severity.parse(body["severity"]),
                                description=body["description"].strip(),
                                parameters=dict(body.get("parameters") or {}), regulations=cited)
    if sha256 is None:
        canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        sha256 = hashlib.sha256(canonical.encode()).hexdigest()
    return Rulebook(version=int(raw.get("version", 1)), controls=controls, regulations=regulations, sha256=sha256)
