"""vg-validate: check NDJSON streams or single-record .json fixtures against the
PROPOSED device telemetry contract v1.

Exit status: 0 all records valid, 1 at least one invalid record, 2 usage/IO error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import contract


def _validate_json_file(path: Path) -> list[tuple[int, list[contract.Issue]]]:
    value, issues = contract.parse_document(path.read_text(encoding="utf-8"))
    if not issues:
        if not isinstance(value, dict):
            issues = [contract.Issue("not_object", "", "top-level value must be a JSON object")]
        else:
            issues = contract.validate_record(value)
            if not issues:
                frame = json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n"
                if len(frame.encode("utf-8")) > contract.MAX_LINE_BYTES:
                    issues = [contract.Issue("line_too_long", "",
                                             "compact frame exceeds the line limit")]
    return [(1, issues)]


def _validate_ndjson(stream) -> list[tuple[int, list[contract.Issue]]]:
    return [(lineno, issues) for lineno, _rec, issues in contract.iter_ndjson(stream)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vg-validate", description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+",
                        help="NDJSON files (one record per line), single-record .json files, "
                             "or '-' for NDJSON on stdin")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print the summary")
    args = parser.parse_args(argv)

    n_records = n_invalid = 0
    for name in args.paths:
        try:
            if name == "-":
                results = _validate_ndjson(sys.stdin.buffer)
            else:
                path = Path(name)
                if path.suffix == ".json":
                    results = _validate_json_file(path)
                else:
                    with path.open("rb") as fh:
                        results = _validate_ndjson(fh)
        except OSError as exc:
            print(f"vg-validate: {exc}", file=sys.stderr)
            return 2
        for lineno, issues in results:
            n_records += 1
            if issues:
                n_invalid += 1
                if not args.quiet:
                    for issue in issues:
                        print(f"{name}:{lineno}: INVALID {issue}")
    print(f"vg-validate: {n_records - n_invalid}/{n_records} records valid "
          f"(contract v1, {contract.CONTRACT_STATUS})", file=sys.stderr)
    return 1 if n_invalid else 0


if __name__ == "__main__":
    sys.exit(main())
