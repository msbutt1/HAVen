"""Cross-check firmware-generated telemetry with the authoritative Python
validator and processor in signals/ (vibeguard_signals).

Run via ctest (contract_validation) or directly:
    uv run --project ../../../signals python check_fake_streams.py PATH/TO/vg-fake-device

Every scenario's output must be 100 % valid contract v1. Loss must be visible
and accounted for, never filled in. Also prints the measured wire load table.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from vibeguard_signals import contract
from vibeguard_signals.processing import ProcessingConfig, Processor

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)
        print(f"  FAIL {msg}")


def run(dev: str, *args: str) -> tuple[bytes, str]:
    p = subprocess.run([dev, *args], capture_output=True, check=False)
    if p.returncode != 0:
        FAILURES.append(f"{args}: exit {p.returncode}: {p.stderr.decode()}")
    return p.stdout, p.stderr.decode()


def parse(raw: bytes, name: str) -> list[dict]:
    recs = []
    lines = raw.split(b"\n")
    check(lines[-1] == b"", f"{name}: output must end with a newline")
    for i, line in enumerate(lines[:-1]):
        rec, issues = contract.parse_line(line)
        if issues:
            check(False, f"{name}: line {i + 1} invalid: {issues[0]}")
            continue
        recs.append(rec)
    return recs


def last_status(recs: list[dict]) -> dict:
    return [r for r in recs if r["type"] == "status"][-1]


def seq_gaps(recs: list[dict]) -> int:
    missing = 0
    by_boot: dict[str, int] = {}
    for r in recs:
        prev = by_boot.get(r["boot_id"])
        if prev is not None:
            check(r["seq"] > prev, f"seq not increasing within boot {r['boot_id']}")
            missing += r["seq"] - prev - 1
        else:
            check(r["seq"] == 0, f"boot {r['boot_id']} does not start at seq 0")
        by_boot[r["boot_id"]] = r["seq"]
    return missing


def index_accounting(recs: list[dict], stream: str) -> dict:
    """Walk one stream's batches: indices never overlap; every jump is either
    device-reported (dropped_samples_before) or unreported (transport loss)."""
    typ = f"{stream}_batch"
    nxt = 0
    rows = device_dropped = transport_lost = 0
    for r in recs:
        if r["type"] != typ:
            continue
        first, n = r["first_sample_index"], len(r["samples"])
        dev = r["quality"].get("dropped_samples_before", 0)
        check(first >= nxt, f"{typ}: index overlap at {first} (expected >= {nxt})")
        jump = first - nxt
        check(dev <= jump, f"{typ}: dropped_before {dev} exceeds index jump {jump}")
        device_dropped += dev
        transport_lost += jump - dev
        rows += n
        nxt = first + n
    return {"rows": rows, "device_dropped": device_dropped, "transport_lost": transport_lost,
            "end_index": nxt}


def process(recs: list[dict]) -> dict[str, int]:
    proc = Processor(ProcessingConfig())
    events: dict[str, int] = {}
    outs = []
    for r in recs:
        outs += proc.feed(r)
    outs += proc.finish()
    for o in outs:
        if o["type"] == "stream_event":
            events[o["event"]] = events.get(o["event"], 0) + 1
    check(proc.summary.records_accepted == len(recs),
          f"processor accepted {proc.summary.records_accepted}/{len(recs)}")
    check("invalid_record" not in events, "processor reported invalid records")
    return events


def provenance_simulated(recs: list[dict], name: str) -> None:
    check(all(r["provenance"]["source"] == "simulated" for r in recs),
          f"{name}: a fake-device record is not labelled simulated")


def scenario(dev: str, name: str, *args: str) -> tuple[list[dict], dict, str]:
    raw, err = run(dev, "--boot-id", f"fake-{name}", *args)
    recs = parse(raw, name)
    provenance_simulated(recs, name)
    events = process(recs)
    print(f"[{name}] {len(recs)} records valid; events={events}")
    return recs, events, err


def main() -> int:
    dev = sys.argv[1]
    here = Path(__file__).resolve().parent

    # 1. clean stream: no loss of any kind
    recs, ev, _ = scenario(dev, "clean", "--seconds", "10")
    check(seq_gaps(recs) == 0, "clean: seq gap")
    for s in ("emg", "imu"):
        a = index_accounting(recs, s)
        check(a["device_dropped"] == 0 and a["transport_lost"] == 0, f"clean: {s} loss {a}")
    check(not ({"seq_gap", "sample_gap", "timing_discontinuity", "rate_mismatch"} & set(ev)),
          f"clean: unexpected events {ev}")
    st = last_status(recs)
    check(st["sensors"] == {"emg": "ok", "imu": "ok"}, "clean: sensors")
    a = index_accounting(recs, "emg")
    # device time 1000..10_001_000 us, first EMG sample at 5000 us, 1 kHz -> 9997 samples
    check(a["rows"] == 9997, f"clean: EMG rows {a['rows']} (expected 9997)")

    # 2. stalled output sink: bounded, visible transport loss; acquisition never blocked
    recs, ev, _ = scenario(dev, "stall", "--seconds", "10", "--baud", "921600", "--stall", "3:5")
    st = last_status(recs)
    gaps = seq_gaps(recs)
    check(gaps > 0, "stall: expected seq gaps")
    check(gaps == st["counters"]["tx_dropped_records"],
          f"stall: seq gaps {gaps} != tx_dropped_records {st['counters']['tx_dropped_records']}")
    check(st["counters"]["emg_dropped_samples"] == 0 and st["counters"]["imu_dropped_samples"] == 0,
          "stall: acquisition dropped samples (must not be blocked by the sink)")
    for s in ("emg", "imu"):
        a = index_accounting(recs, s)
        check(a["transport_lost"] > 0 and a["device_dropped"] == 0, f"stall: {s} {a}")
    check(ev.get("seq_gap", 0) > 0 and ev.get("sample_gap", 0) > 0, f"stall: host events {ev}")

    # 3. serializer paused: acquisition ring overflows -> device-reported overrun
    recs, ev, _ = scenario(dev, "pause", "--seconds", "10", "--pause", "3:5", "--ring", "512")
    st = last_status(recs)
    check(seq_gaps(recs) == 0, "pause: no transport loss expected")
    a = index_accounting(recs, "emg")
    check(a["device_dropped"] > 0 and a["transport_lost"] == 0, f"pause: emg {a}")
    check(a["device_dropped"] == st["counters"]["emg_dropped_samples"],
          f"pause: dropped_before total {a['device_dropped']} != counter "
          f"{st['counters']['emg_dropped_samples']}")
    check(any("overrun" in r["quality"]["flags"] for r in recs if r["type"] == "emg_batch"),
          "pause: no overrun flag")

    # 4. sensor faults: disconnect (all-null) and per-channel nulls, never zeros
    recs, ev, _ = scenario(dev, "faults", "--seconds", "8", "--disconnect", "2:3", "--nulls", "4:5")
    disc = [r for r in recs if r["type"] == "emg_batch"
            and "sensor_disconnected" in r["quality"]["flags"]]
    check(len(disc) > 0 and all(v is None for r in disc for row in r["samples"] for v in row),
          "faults: disconnected batches must be all-null")
    check(any(r["type"] == "status" and r["sensors"]["emg"] == "disconnected" for r in recs),
          "faults: status must report emg disconnected")
    nul = [r for r in recs if r["type"] == "imu_batch" and "missing_values" in r["quality"]["flags"]]
    nulls_at = {c for r in nul for row in r["samples"] for c, v in enumerate(row) if v is None}
    check(len(nul) > 0 and nulls_at == {1}, f"faults: imu nulls only on channel ay, got {nulls_at}")

    # 5. timing jitter: explicit timestamps where uniform would misstate time
    recs, ev, _ = scenario(dev, "jitter", "--seconds", "5", "--imu-rate", "500", "--imu-jitter", "700")
    imu = [r for r in recs if r["type"] == "imu_batch"]
    check(any(r["timing"]["mode"] == "explicit" and "timing_irregular" in r["quality"]["flags"]
              for r in imu), "jitter: expected explicit timing batches")
    a = index_accounting(recs, "imu")
    check(a["device_dropped"] == 0 and a["transport_lost"] == 0, f"jitter: no sample lost {a}")

    # 6. restart: a new boot is a new boot_id with seq and indices from 0
    r1, _ = run(dev, "--seconds", "2", "--boot-id", "fake-boot-a")
    r2, _ = run(dev, "--seconds", "2", "--boot-id", "fake-boot-b")
    recs = parse(r1 + r2, "restart")
    seq_gaps(recs)
    ev = process(recs)
    check(ev.get("boot_change", 0) >= 1, f"restart: boot_change not detected {ev}")

    # 7. encoder edge cases from the C unit tests (2^53-1 limits, nulls, explicit timing)
    with tempfile.TemporaryDirectory() as d:
        edge = Path(d) / "edge.ndjson"
        t = subprocess.run([str(Path(dev).parent / "test_telemetry"), "--emit", str(edge)],
                           capture_output=True, check=False)
        check(t.returncode == 0, "test_telemetry --emit failed")
        recs = parse(edge.read_bytes(), "edge")
        check(len(recs) >= 9, f"edge: only {len(recs)} records")
        print(f"[edge] {len(recs)} encoder edge-case records valid")

    # 8. wire load: measured bytes of real encoder output, incl. '\n' framing
    print("\nMeasured NDJSON wire load (fake device, 50 ms batches, worst-case-free synthetic values):")
    print(f"{'EMG Hz':>7} {'IMU Hz':>7} {'B/s':>8} {'@115200':>9} {'@460800':>9} {'@921600':>9}")
    for emg_hz, imu_hz in ((1000, 100), (1000, 200), (1000, 500), (1000, 1000), (500, 500)):
        raw, _ = run(dev, "--seconds", "20", "--emg-rate", str(emg_hz), "--imu-rate", str(imu_hz))
        bps = len(raw) / 20.0
        loads = [bps * 10 / b for b in (115_200, 460_800, 921_600)]
        print(f"{emg_hz:>7} {imu_hz:>7} {bps:>8.0f} " + " ".join(f"{x:>8.0%}" for x in loads))
    # throughput-limited run at the CP2102 maximum: must not drop with headroom
    raw, err = run(dev, "--seconds", "20", "--imu-rate", "500", "--baud", "921600")
    recs = parse(raw, "baud921600")
    st = last_status(recs)
    check(st["counters"]["tx_dropped_records"] == 0, "EMG 1000 + IMU 500 dropped records at 921600")
    print(f"[921600 baud, EMG 1000 + IMU 500] tx_dropped_records="
          f"{st['counters']['tx_dropped_records']}")
    raw, err = run(dev, "--seconds", "20", "--imu-rate", "500", "--baud", "115200")
    recs = parse(raw, "baud115200")
    st = last_status(recs)
    check(st["counters"]["tx_dropped_records"] > 0, "115200 should be overloaded")
    print(f"[115200 baud, EMG 1000 + IMU 500] tx_dropped_records="
          f"{st['counters']['tx_dropped_records']} (overloaded: visible loss, still valid)")

    print(f"\n{'FAILED' if FAILURES else 'OK'}: {len(FAILURES)} failures")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
