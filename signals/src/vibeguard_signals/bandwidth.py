"""vg-bandwidth: estimate serial throughput of NDJSON contract-v1 telemetry at
PROVISIONAL sample rates, versus a hypothetical compact binary framing.

All rates here are planning assumptions; real rates come from hardware
measurement. The estimate serializes real contract records (validated by
``contract.serialize_record``) filled with worst-case-width integer values.
"""

from __future__ import annotations

import argparse
import sys

from . import contract

UART_BAUDS = (115_200, 460_800, 921_600)
BITS_PER_UART_BYTE = 10  # 8N1 framing

# Hypothetical binary framing, for comparison only (not specified yet):
# header ~24 B (type, ids hash, seq, first_sample_index, t0, interval, flags)
# + CRC16 (2 B) + COBS overhead (~1 B per 254) + delimiter (1 B).
BINARY_HEADER_BYTES = 24
BINARY_TRAILER_BYTES = 3


def _batch(rtype: str, channels: list[str], unit: str, rate: float, n: int,
           worst_value: int) -> dict:
    return {
        "schema": contract.SCHEMA,
        "version": 1,
        "type": rtype,
        "device_id": "wearable-01",
        "boot_id": "b-7f3a9c21",
        "seq": 1_234_567,
        "provenance": {"source": "live"},
        "sample_rate_hz": rate,
        "first_sample_index": 123_456_789,
        "timing": {"mode": "uniform", "t0_us": 3_600_000_000, "interval_us": 1e6 / rate},
        "channels": channels,
        "units": {c: unit for c in channels},
        "samples": [[worst_value] * len(channels) for _ in range(n)],
        "quality": {"flags": []},
    }


def estimate(name: str, rtype: str, channels: list[str], unit: str, rate: float,
             worst_value: int, bytes_per_value: int, batch_s: float) -> dict:
    n = max(1, min(contract.MAX_SAMPLES_PER_BATCH, round(rate * batch_s)))
    frame = contract.serialize_record(_batch(rtype, channels, unit, rate, n, worst_value))
    batches_per_s = rate / n
    json_bps = len(frame.encode()) * batches_per_s
    bin_frame = BINARY_HEADER_BYTES + n * len(channels) * bytes_per_value
    bin_frame += BINARY_TRAILER_BYTES + bin_frame // 254
    return {
        "name": name, "rate": rate, "n": n, "frame": len(frame.encode()),
        "json_Bps": json_bps, "bin_Bps": bin_frame * batches_per_s,
    }


SCENARIOS = (
    # name, type, channels, unit, rate, worst value, binary bytes/value
    ("EMG 1ch @500 Hz", "emg_batch", ["emg0"], "adc_counts", 500.0, 4095, 2),
    ("EMG 1ch @1000 Hz", "emg_batch", ["emg0"], "adc_counts", 1000.0, 4095, 2),
    ("IMU accel 3ax @100 Hz", "imu_batch", ["ax", "ay", "az"], "raw_counts", 100.0, -32768, 2),
    ("IMU accel 3ax @400 Hz", "imu_batch", ["ax", "ay", "az"], "raw_counts", 400.0, -32768, 2),
    ("IMU accel 3ax @1000 Hz", "imu_batch", ["ax", "ay", "az"], "raw_counts", 1000.0, -32768, 2),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vg-bandwidth", description=__doc__.splitlines()[0])
    parser.add_argument("--batch-ms", type=float, default=50.0,
                        help="target batch duration in ms (default 50)")
    args = parser.parse_args(argv)
    batch_s = args.batch_ms / 1000.0

    rows = {s[0]: estimate(*s, batch_s=batch_s) for s in SCENARIOS}
    print(f"Provisional estimate, target batch {args.batch_ms:g} ms, worst-case value widths "
          "(EMG '4095', IMU '-32768').")
    print(f"{'stream':<24}{'n/batch':>8}{'JSON B/frame':>14}{'JSON B/s':>11}{'binary B/s':>12}")
    for r in rows.values():
        print(f"{r['name']:<24}{r['n']:>8}{r['frame']:>14}{r['json_Bps']:>11.0f}{r['bin_Bps']:>12.0f}")

    print("\nCombined configurations (UART utilisation at 8N1, 10 bits/byte):")
    combos = (
        ("EMG@500 + IMU@100", "EMG 1ch @500 Hz", "IMU accel 3ax @100 Hz"),
        ("EMG@1000 + IMU@400", "EMG 1ch @1000 Hz", "IMU accel 3ax @400 Hz"),
        ("EMG@1000 + IMU@1000", "EMG 1ch @1000 Hz", "IMU accel 3ax @1000 Hz"),
    )
    header = f"{'config':<22}{'format':<8}{'B/s':>8}" + "".join(f"{b:>10}" for b in UART_BAUDS)
    print(header)
    for label, a, b in combos:
        for fmt, key in (("json", "json_Bps"), ("binary", "bin_Bps")):
            bps = rows[a][key] + rows[b][key]
            util = "".join(f"{bps * BITS_PER_UART_BYTE / baud:>10.0%}" for baud in UART_BAUDS)
            print(f"{label:<22}{fmt:<8}{bps:>8.0f}{util}")
    print("\n>100% means the link cannot carry the stream. Keep utilisation well below ~50% "
          "for headroom. Native USB CDC links are not limited by a UART baud rate but still need "
          "measurement on the confirmed board.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
