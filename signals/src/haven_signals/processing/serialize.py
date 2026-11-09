"""Output serialization (separate from algorithms and transport)."""

from __future__ import annotations

import json
from typing import Iterable, TextIO


def to_ndjson_line(record: dict) -> str:
    """Compact, deterministic single-line JSON; non-finite numbers are refused."""
    return json.dumps(record, separators=(",", ":"), allow_nan=False, ensure_ascii=False) + "\n"


def write_ndjson(records: Iterable[dict], out: TextIO) -> int:
    n = 0
    for rec in records:
        out.write(to_ndjson_line(rec))
        n += 1
    return n
