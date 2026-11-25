"""Validate transport shapes before constructing surveillance state."""
from __future__ import annotations

import math
from typing import Any

from vigil.rtg.events import OPERATIONS, MatchHeader, MatchRecord
from vigil.storage import identifier


def number(value: Any, name: str, *, positive: bool = False) -> None:
    if type(value) not in (int, float) or (abs(value) > 1e15 or not math.isfinite(value)) or (positive and value <= 0):
        raise ValueError(f"{name} must be a finite {'positive ' if positive else ''}number")


def envelope(value: Any) -> tuple[str, str]:
    if not isinstance(value, dict):
        raise ValueError("message must be an object")
    kind = value.get("type")
    if kind not in ("header", "event", "end"):
        raise ValueError("unknown message type")
    return kind, identifier(value.get("match"))


def header(value: Any, match_id: str) -> MatchHeader:
    if not isinstance(value, dict):
        raise ValueError("header must be an object")
    try:
        result = MatchHeader.from_dict(value)
    except TypeError as error:
        raise ValueError("invalid header fields") from error
    if result.match_id != match_id:
        raise ValueError("envelope and header match identifiers differ")
    if result.source not in ("sim", "live", "csv") or not isinstance(result.started_at, str):
        raise ValueError("invalid match source or timestamp")
    if not isinstance(result.teams, list) or len(result.teams) > 256:
        raise ValueError("teams must be a list of at most 256 identifiers")
    teams = [identifier(team) for team in result.teams]
    if len(set(teams)) != len(teams):
        raise ValueError("duplicate team names")
    number(result.speed, "speed", positive=True)
    for name in ("market_data", "market_data_sha256"):
        if getattr(result, name) is not None and not isinstance(getattr(result, name), str):
            raise ValueError(f"invalid {name}")
    for name in ("limits", "fees", "instrument"):
        mapping = getattr(result, name)
        if not isinstance(mapping, dict) or len(mapping) > 32:
            raise ValueError(f"invalid {name}")
        for key, item in mapping.items():
            if not isinstance(key, str):
                raise ValueError(f"invalid {name} key")
            number(item, key, positive=name == "limits")
    if result.instrument.get("TickSize", 1.0) < 0.01 or result.instrument.get("EtfClamp", 0.002) < 0:
        raise ValueError("invalid instrument tick size or clamp")
    return result


def record(value: Any) -> MatchRecord:
    if not isinstance(value, list) or len(value) != 11:
        raise ValueError("record must have 11 positional fields")
    result = MatchRecord(*value)
    for name in ("seq", "order_id", "volume"):
        if type(getattr(result, name)) is not int or abs(getattr(result, name)) >= 2**63:
            raise ValueError(f"{name} must be an integer")
    if not 0 <= result.seq < 2**63 or result.order_id < 0:
        raise ValueError("invalid sequence number or order ID")
    number(result.time, "event time")
    if result.time < 0:
        raise ValueError("negative event time")
    if not isinstance(result.competitor, str) or len(result.competitor) > 256:
        raise ValueError("invalid participant")
    if result.competitor:
        identifier(result.competitor)
    if not isinstance(result.op, str) or result.op not in OPERATIONS or result.side not in (None, "A", "B") or result.lifespan not in (None, "F", "G"):
        raise ValueError("invalid event operation, side or lifespan")
    if result.instrument is not None and (type(result.instrument) is not int or result.instrument not in (0, 1)):
        raise ValueError("invalid instrument")
    if result.fee is not None and (type(result.fee) is not int or abs(result.fee) >= 2**63):
        raise ValueError("invalid fee")
    if result.price is not None:
        number(result.price, "price")
    if result.op in ("Amend", "Cancel"):
        if result.volume > 0:
            raise ValueError("cancel/amend must remove volume")
    else:
        if result.volume <= 0 or result.price is None or result.price <= 0 or result.side is None or result.instrument is None:
            raise ValueError("order/trade must name instrument, side, positive volume and price")
        if result.op == "Insert" and (result.lifespan is None or result.price != int(result.price)):
            raise ValueError("insert must name a lifespan and integer-cent price")
        if result.op == "Hedge" and result.instrument != 0:
            raise ValueError("hedges must use the future")
    return result


def end(value: dict) -> None:
    count, digest = value.get("events"), value.get("sha256")
    if type(count) is not int or not 0 <= count < 2**63:
        raise ValueError("invalid declared event count")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("invalid declared stream SHA-256")
