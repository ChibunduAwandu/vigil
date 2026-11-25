"""One byte sequence per document.

A hash is only useful as a fingerprint if everyone who holds the document
computes the same one. JSON allows the same data to be written in many ways
(key order, spacing, escapes), so a report is always serialised in exactly one:
keys sorted, no insignificant whitespace, UTF-8, non-ASCII characters written
literally, and no NaN or infinity. The report file on disk *is* those bytes,
which means its fingerprint can be checked with nothing more than
``shasum -a 256 report.json``.

Reading is as strict as writing. JSON parsers disagree about documents that
say something twice or say something that is not JSON, and a fingerprint is
worth nothing if two readers of the same bytes see different reports. So a
file is read with ``parse_strict``, which refuses what is ambiguous rather
than picking a reading.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from typing import Any

from vigil.rtg.events import CSV_HEADER, MatchRecord


def canonical_json(document: Any) -> bytes:
    """The one serialisation of ``document``.

    Only string keys are accepted. A number used as a key would be written as
    a string and sorted as a number, and the same document read back would
    sort it as a string: two byte sequences for one document.
    """
    _require_string_keys(document, "")
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _require_string_keys(value: Any, where: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"a canonical document has string keys only; {key!r} at "
                                f"{where or 'the top level'} is {type(key).__name__}")
            _require_string_keys(item, f"{where}.{key}" if where else key)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _require_string_keys(item, f"{where}[{index}]")


def parse_strict(raw: bytes) -> Any:
    """Parse JSON that has one meaning, and refuse JSON that has several.

    Refused: a key given twice in one object (some parsers keep the first
    value and some the last, so the file would show one thing to a person's
    tools and another to ours); ``NaN``, ``Infinity`` and numbers too large to
    hold, which are not JSON; and anything that is not UTF-8 without a byte
    order mark. Raises ``ValueError`` saying which.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"it is not UTF-8 ({error.reason} at byte {error.start})") from None

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"the key {key!r} appears twice in one object")
            result[key] = value
        return result

    def constant(name: str) -> Any:
        raise ValueError(f"{name} is not a JSON value")

    def number(literal: str) -> float:
        value = float(literal)
        if not math.isfinite(value):
            raise ValueError(f"the number {literal} is out of range")
        return value

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
    except json.JSONDecodeError as error:
        raise ValueError(f"it is not valid JSON ({error.msg} at character {error.pos})") from None


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class EventStreamHasher:
    """Fingerprint of a match's event stream.

    The digest is that of the events written out in the exchange's own
    ``match_events.csv`` layout, header included. Whoever holds that file can
    therefore check it against a report with ``shasum``; whoever consumed the
    events from a topic arrives at the same digest without ever writing a file.
    """

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self._buffer = io.StringIO(newline="")
        self._writer = csv.writer(self._buffer)
        self.count = 0
        self._write(CSV_HEADER)

    def _write(self, row: list) -> None:
        self._writer.writerow(row)
        self._digest.update(self._buffer.getvalue().encode("utf-8"))
        self._buffer.seek(0)
        self._buffer.truncate(0)

    def update(self, record: MatchRecord) -> None:
        self._write(record.to_row())
        self.count += 1

    def hexdigest(self) -> str:
        return self._digest.hexdigest()
