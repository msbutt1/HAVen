"""Acceptance tests for the PROPOSED device telemetry contract v1.

These verify the contract and validator against fixtures only. They say
nothing about real sensor behaviour or link throughput.
"""

import copy
import json
import math
from pathlib import Path

import pytest

from haven_signals import contract
from haven_signals.validate_cli import main as validate_main

FIXTURES = Path(__file__).resolve().parents[1] / "contract" / "fixtures"
VALID = sorted((FIXTURES / "valid").glob("*.json"))
INVALID_MANIFEST = json.loads((FIXTURES / "invalid_manifest.json").read_text())
STREAM = FIXTURES / "valid" / "stream_simulated_boot.ndjson"


def load(path: Path):
    value, issues = contract.parse_document(path.read_text())
    assert not issues
    return value


def emg_record():
    return load(FIXTURES / "valid" / "emg_batch_uniform.json")


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.name)
def test_valid_fixture_validates(path):
    assert contract.validate_record(load(path)) == []


def test_every_invalid_fixture_is_listed_and_vice_versa():
    on_disk = {p.name for p in (FIXTURES / "invalid").glob("*.json")}
    assert on_disk == set(INVALID_MANIFEST)


@pytest.mark.parametrize("name", sorted(INVALID_MANIFEST))
def test_invalid_fixture_fails_with_expected_code(name):
    text = (FIXTURES / "invalid" / name).read_text()
    value, issues = contract.parse_document(text)
    if not issues:
        issues = contract.validate_record(value)
    codes = {i.code for i in issues}
    assert INVALID_MANIFEST[name]["code"] in codes, issues
    assert all(i.code in contract.ISSUE_CODES for i in issues)


def test_ndjson_stream_all_valid_and_sequenced():
    with STREAM.open("rb") as fh:
        results = list(contract.iter_ndjson(fh))
    assert results and all(not issues for _, _, issues in results)
    records = [rec for _, rec, _ in results]
    assert [r["seq"] for r in records] == list(range(len(records)))
    assert records[0]["type"] == "hello"
    assert {r["boot_id"] for r in records} == {"sim-boot-0001"}
    assert all(r["provenance"]["source"] == "simulated" for r in records)


def test_cli_exit_codes(capsys):
    assert validate_main([str(p) for p in VALID] + [str(STREAM)]) == 0
    assert validate_main([str(FIXTURES / "invalid" / "invalid_version.json")]) == 1
    out = capsys.readouterr().out
    assert "unsupported_version" in out


def test_serialize_roundtrip_is_compact_single_line():
    rec = emg_record()
    frame = contract.serialize_record(rec)
    assert frame.endswith("\n") and frame.count("\n") == 1
    parsed, issues = contract.parse_line(frame.encode())
    assert issues == [] and parsed == rec


def test_in_memory_nonfinite_rejected_before_serialization():
    rec = load(FIXTURES / "valid" / "imu_batch_physical_units.json")
    for bad in (math.nan, math.inf, -math.inf):
        r = copy.deepcopy(rec)
        r["samples"][0][0] = bad
        with pytest.raises(contract.ContractViolation):
            contract.serialize_record(r)


def test_framing_errors():
    assert contract.parse_line(b"")[1][0].code == "empty_line"
    assert contract.parse_line(b"\xff\xfe\n")[1][0].code == "not_utf8"
    assert contract.parse_line(b'{"a":\n1}')[1][0].code == "malformed_json"
    assert contract.parse_line(b"{not json}")[1][0].code == "malformed_json"
    huge = b'{"x":"' + b"a" * contract.MAX_LINE_BYTES + b'"}\n'
    assert contract.parse_line(huge)[1][0].code == "line_too_long"


def test_crlf_frame_accepted():
    frame = contract.serialize_record(emg_record()).rstrip("\n") + "\r\n"
    assert contract.parse_line(frame)[1] == []


def test_counts_must_stay_counts_without_conversion():
    rec = emg_record()
    rec["units"] = {"emg0": "mV"}
    codes = {i.code for i in contract.validate_record(rec)}
    assert "missing_conversion" in codes
    rec["conversion"] = {"emg0": {"scale": 0.0008, "source": "hypothetical"}}
    assert contract.validate_record(rec) == []  # integer mV values are still finite numbers


def test_conversion_on_count_channel_rejected():
    rec = emg_record()
    rec["conversion"] = {"emg0": {"scale": 1.0, "source": "x"}}
    assert {i.code for i in contract.validate_record(rec)} == {"unexpected_conversion"}


def test_timing_within_tolerance_accepted_and_beyond_rejected():
    rec = emg_record()
    rec["timing"]["interval_us"] = 1080  # 8% slow: plausible measured clock
    assert contract.validate_record(rec) == []
    rec["timing"]["interval_us"] = 1150  # 15%: metadata contradicts itself
    assert {i.code for i in contract.validate_record(rec)} == {"timing_inconsistent"}


def test_dropped_samples_require_overrun_flag():
    rec = emg_record()
    rec["quality"] = {"flags": [], "dropped_samples_before": 3}
    assert {i.code for i in contract.validate_record(rec)} == {"quality_inconsistent"}


def test_missing_flag_without_nulls_rejected():
    rec = emg_record()
    rec["quality"] = {"flags": ["missing_values"]}
    assert {i.code for i in contract.validate_record(rec)} == {"quality_inconsistent"}


def test_live_record_cannot_claim_original_source():
    rec = emg_record()
    rec["provenance"] = {"source": "live", "original_source": "simulated"}
    assert {i.code for i in contract.validate_record(rec)} == {"provenance_inconsistent"}


def test_boolean_seq_and_version_rejected():
    rec = emg_record()
    rec["seq"] = True
    assert {i.code for i in contract.validate_record(rec)} == {"invalid_value"}
    rec = emg_record()
    rec["version"] = True
    assert {i.code for i in contract.validate_record(rec)} == {"unsupported_version"}


def test_emg_channels_rejected_in_imu_batch():
    rec = emg_record()
    rec["type"] = "imu_batch"
    assert {i.code for i in contract.validate_record(rec)} == {"invalid_value"}


def test_max_size_batch_fits_line_limit():
    rec = emg_record()
    rec["type"] = "imu_batch"
    rec["channels"] = list(contract.IMU_CHANNEL_KIND)
    rec["units"] = {c: "raw_counts" for c in rec["channels"]}
    rec["samples"] = [[-32768] * 6 for _ in range(contract.MAX_SAMPLES_PER_BATCH)]
    rec["quality"] = {"flags": []}
    assert len(contract.serialize_record(rec).encode()) <= contract.MAX_LINE_BYTES
