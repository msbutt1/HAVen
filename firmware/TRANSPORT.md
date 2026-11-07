# Wearable telemetry transport (Prompt 2)

The firmware emits **device telemetry contract v1** exactly as specified in
`signals/contract/TELEMETRY_CONTRACT.md`. The same `vg-validate` and `Processor` code
that checks the simulator checks the firmware's output.

## Start here, Abduljabbar: what this is and where it plugs in

**What it is.** This is the code on the wearable ESP32 that turns sensor samples into the
telemetry stream your backend receives over USB. It packs samples into the same
`hello` / `status` / `emg_batch` / `imu_batch` JSON lines that `vg-sim` already produces,
one record per line, and sends them over the board's USB-serial port at **921600 baud**.

**What it's for.** It lets you replace the simulator with the real device without
changing your decoding or processing code. The serial stream and `vg-sim` output are the
same format, validated by the same `contract.parse_line`. The only new work on your side
is the **serial adapter**: open the port, read lines, and hand them to the code you
already use for the simulator.

**Where it goes.**

```
wearable ESP32 ──USB (CP2102, /dev/ttyUSB*)──► your serial adapter ──► contract.parse_line
                                                                      └► Processor (signals/) ──► your API / storage
```

The firmware lives in `firmware/` (Salaar). Your adapter lives in your backend code, next
to your existing mock adapter, behind the same interface.

**What you can do today, without hardware.** `tools/fake_device` builds `vg-fake-device`,
which runs the firmware's own encoding pipeline on your laptop and prints the exact byte
stream the board will send. Feed it to your adapter through a pipe, or a pty if you want
to exercise the serial path:

```sh
cd firmware
cmake -S tests/host -B build/host -G Ninja && cmake --build build/host
build/host/vg-fake-device --seconds 30 > fake.ndjson                  # clean stream
build/host/vg-fake-device --seconds 30 --baud 921600 --stall 3:5 > lossy.ndjson   # with visible loss
```

Everything it emits is labelled `"source": "simulated"`.

**Things to handle in the adapter:**
- Some lines won't decode: the boot banner at reset, or noise. Count and skip them with
  `Processor.feed_invalid_frame`.
- A new `boot_id` means the board restarted. Opening the port may itself restart it.
- Sequence gaps mean lost records. Report them; never fill them in.
- Both boards show the same USB serial number. Tell them apart by `device_id` in the
  hello record.

Details are in [Adapter boundary](#adapter-boundary-for-abduljabbar) below. Real-board
capture at 921600 is still pending, so treat the baud rate as provisional until it is
measured.

## Pipeline: three stages, none waits on the next

```
acquisition                 serialization                    transmission
(esp_timer callbacks)       (task, every 10 ms)              (task)
sensor/fake ─► sample ring ─► batcher ─► encoder ─► frame queue ─► UART0 ─► CP2102 ─► host
               bounded,       contract     NDJSON    bounded,           921600 8N1
               drop+count     batches      line      drop whole frame
```

| Stage | Code | Bound | When full |
|---|---|---|---|
| Sample ring, one per stream | `shared/src/sample_ring.c` | 1024 EMG / 512 IMU samples | The new sample is dropped and its index consumed. The next batch reports `dropped_samples_before` with the `overrun` flag, and `emg_dropped_samples` / `imu_dropped_samples` count it |
| Batch | `shared/src/batcher.c` | ≤ 256 rows; 50 ms target; 100 ms max latency | — |
| Encoded line | `shared/src/telemetry.c` | 8192-byte buffer, never truncated | `encode_errors` is counted; no seq is consumed |
| Frame queue | `shared/src/frame_queue.c` | 32 KiB; 2 KiB reserved for hello/status | The whole frame is dropped. Its seq was already assigned, so the host sees a **seq gap**; `tx_dropped_records` / `tx_dropped_bytes` count it |
| UART TX | `target/vg_uart_tx.c` | IDF driver TX ring of 8 KiB | Only the TX task blocks |

Acquisition never waits for the host. A stalled or slow sink causes bounded, counted
loss that is visible on the host. No sample is ever repeated, interpolated or invented.
This is tested by the `stall` and `pause` scenarios in `tests/host/check_fake_streams.py`.

## Identity, sequence and time

- **`device_id`** = `vg-w-<base MAC>`. Board W is `vg-w-2805a5075554`.
- **`boot_id`** = `b-` + 16 hex characters from the hardware RNG, generated on every boot
  with the bootloader entropy source enabled (ESP-IDF v6.1 *Random Number Generation*).
  A new boot or stream restart always gets a new `boot_id`.
- **`seq`** is one counter per (`device_id`, `boot_id`) shared by every record type.
  It starts at 0 (the hello) and is assigned when a record is successfully encoded.
  It never repeats or wraps within a boot. At 2^53−1 the encoder refuses further records
  (`VG_TM_ERR_EXHAUSTED`), and the stream must restart with a new `boot_id`; this is tested.
- **Time** comes from `esp_timer_get_time()`: 64-bit µs since boot, which never wraps in
  practice. `shared/include/vg/time_ext.h` extends any 32-bit source; its wrap is tested.
  Values above 2^53−1 are refused.

### When sample indices advance

`first_sample_index` counts **acquisition attempts** per stream since boot. The index
advances by exactly 1 for every scheduled sample slot:

| Slot outcome | Index | Appears as |
|---|---|---|
| Sample delivered | +1 | A row in a batch |
| Ring full (consumer behind) | +1 | Missing rows; the next batch has `dropped_samples_before` += 1 and `overrun` |
| Sensor read failed (`vg_ring_skip`) | +1 | The same as above (the contract has a single device-loss flag) |
| Sensor disconnected / value unavailable | +1 | A row of `null`s, or a `null` cell, with `sensor_disconnected` / `missing_values` |
| Frame dropped in the queue (transport) | was +1 for each sample in it | An index jump **without** `dropped_samples_before`, plus a seq gap |

So the host can tell device-side loss (reported) from transport loss (unreported jump).
`signals`' Processor already classifies these as `device_overrun` versus
`transport_or_unreported`.

## Batching and timing

- Batches close at about 50 ms, on an index gap, on a sensor-state change, or on a time
  step outside 0.5–1.5× the nominal interval.
- Only the longest prefix whose mean interval is within the contract's ±10 % is sent. The
  remaining rows start the next batch, so every batch validates and no sample is
  discarded for timing reasons.
- **Uniform timing** (`t0_us` + measured mean `interval_us`) is used when every timestamp
  is within 25 % of a nominal interval of that line. Otherwise the batch uses **explicit**
  per-sample timestamps with the `timing_irregular` flag.

## Wire load and link choice

Measured on real encoder output, including the `\n` framing (`check_fake_streams.py`,
20 s runs, synthetic values):

| EMG Hz | IMU (3 axes) Hz | Bytes/s | UART load @115200 | @460800 | @921600 |
|---|---|---|---|---|---|
| 1000 | 100 | 25,995 | 226 % | 56 % | 28 % |
| 1000 | 200 | 27,178 | 236 % | 59 % | 29 % |
| 1000 | 500 | 30,770 | 267 % | 67 % | 33 % |
| 1000 | 1000 | 36,788 | 319 % | 80 % | 40 % |
| 500 | 500 | 27,239 | 236 % | 59 % | 30 % |

Load is bytes × 10 bits (8N1) ÷ baud. Real values with more digits add a few percent.

**Decision: batched NDJSON at 921600 baud, provisional until measured on the board.**
- Both boards use a **CP2102** USB-UART bridge (measured, H2). The CP2102 datasheet
  maximum is 921600 baud, and the ESP32 UART supports up to 5 Mbaud (`SOC_UART_BITRATE_MAX`).
- At 921600, the target load (EMG 1 kHz + IMU ≤ 1 kHz) is 28–40 %, which leaves more than
  2× headroom. The simulated 921600 link dropped nothing (tested).
- **115200 cannot carry it.** The simulated run at 115200 dropped 471 records, all of
  them visible, and the stream stayed valid.
- **No binary format is needed** while 921600 holds. If bring-up shows that 921600 is
  unreliable on these boards or cables, the fallback order is:
  1. 460800, which works up to about 500 Hz IMU with roughly 1.5× headroom;
  2. then a minimal binary framing, with a tested decoder into these same logical records.
- **Pending (hardware):** a sustained throughput and error-rate test at 921600 on board W.

## Diagnostics and the telemetry port

- In the wearable build, UART0 carries **only telemetry**:
  `CONFIG_ESP_CONSOLE_NONE`, `CONFIG_LOG_DEFAULT_LEVEL_NONE` and
  `CONFIG_BOOTLOADER_LOG_LEVEL_NONE` are set in `wearable/sdkconfig.defaults`.
- **Diagnostics travel in-band** as valid records:
  - status `counters`;
  - hello `hardware` facts (board, acquisition mode, `config_problems`, link).
- **The ESP32 boot ROM still prints a short banner at reset.** This cannot be disabled
  through Kconfig on ESP32, and it is printed at 115200 while the host listens at 921600,
  so it arrives as garbage. Panic output also uses UART0. The host must count such lines
  as invalid frames and skip them. The first valid record after a reset is a hello with
  `seq` 0 and a new `boot_id`.

## Reconnect behaviour

- The device does not know whether a host is listening: the UART always transmits. A host
  that (re)connects mid-boot simply starts receiving at some seq > 0.
- **hello is re-sent every 5 s** (status every 1 s and on every sensor-state change), with
  the next seq. A late host learns the stream declaration within 5 s, and seq never
  repeats.
- **Opening the serial port may reset the board.** These dev boards wire DTR/RTS to
  EN/IO0 for auto-reset. A reset is always visible: a new `boot_id` and a `boot_change`
  event. Bring-up will check which open settings avoid it.

## Adapter boundary (for Abduljabbar)

Device → backend is unchanged contract v1 NDJSON, so the existing mock/simulator path and
the serial path share one decoder (`contract.parse_line`). For the serial adapter:
1. Open the CP2102 port at **921600 8N1**, no flow control. Find the right port by the
   `device_id` in hello (MAC-based); both boards report the same USB serial number.
2. Read lines. Pass undecodable lines to `Processor.feed_invalid_frame` and count them
   (boot ROM banner, line noise). Never repair them.
3. Feed records in arrival order. Use the host clock with `poll()`, as with the simulator.
4. A new `boot_id` is a reset. The device never repeats a seq within a boot.

## Fake (simulated) target

- **`vg-fake-device`** (`tools/fake_device/`, built by the host tests) is the firmware's
  own ring/batcher/encoder/queue code on a simulated clock. It is deterministic, always
  `simulated`, and can inject a stalled sink, a paused serializer, a sensor disconnect,
  per-channel nulls, IMU jitter and a limited baud rate.
- **The ESP32 wearable build** currently runs the same pipeline with fake sources. Its
  hello says `acquisition: FAKE … (SIMULATED)`, and every record is
  `provenance.source = "simulated"`.
- **Live provenance** is only possible from a link whose batchers are fed by a real
  adapter. `vg_link_init` refuses to attach a simulated-source batcher to a live encoder,
  and the encoder refuses a batch whose source differs. Both are tested.
- **Sensor states start `unknown`** and change only on evidence from the acquisition layer.
