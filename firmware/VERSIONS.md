# Toolchain and dependency record

| Item | Pinned / used | How it is enforced |
|---|---|---|
| ESP-IDF | **v6.1**, commit `fff9895c82d744c7237be8847347bdd1b07c6643` (released 2026-08-27) | `IDF_VERSION` file; `tools/build.sh` refuses any other checked-out tag |
| Xtensa GCC | `xtensa-esp-elf-gcc` esp-15.2.0_20251204 (GCC 15.2.0) | Installed by ESP-IDF v6.1 `install.sh esp32` |
| esptool | 5.6.0 | ESP-IDF v6.1 Python environment (constraints file `~/.espressif/espidf.constraints.v6.1.txt`) |
| Python (IDF env) | 3.13.13 | `~/.espressif/python_env/idf6.1_py3.13_env` |
| CMake / Ninja (this machine) | 4.3.2 / 1.10.1 | System tools; IDF requires CMake ≥ 3.22 |
| Host compiler (tests) | gcc 14.3.0 (conda-forge) | Any C11 compiler; built with `-Wall -Wextra -Werror -Wconversion` |
| Third-party components | **none** | No `idf_component.yml`, so no `dependencies.lock` is generated |

## Why ESP-IDF

There was no existing team toolchain. ESP-IDF is Espressif's own framework, so its
documentation is the authority used for every chip fact here. It also provides:
- 64-bit `esp_timer_get_time()` µs timestamps, so no 32-bit `micros()` wrap handling;
- the oneshot and continuous ADC drivers with calibration;
- FreeRTOS tasks, to keep acquisition separate from serial output;
- reproducible CMake builds.

Arduino-ESP32 sits on top of ESP-IDF and would add a second version to pin.

## Install (once per machine)

```sh
mkdir -p ~/esp && cd ~/esp
git clone --depth 1 -b v6.1 --recursive --shallow-submodules \
    https://github.com/espressif/esp-idf.git esp-idf-v6.1
cd esp-idf-v6.1 && ./install.sh esp32
git rev-parse HEAD   # must print fff9895c82d744c7237be8847347bdd1b07c6643
```

## Documentation consulted (2026-11-07)

- ESP-IDF v6.1 Programming Guide, ESP32 target:
  - *GPIO & RTC GPIO*: input-only, strapping, flash/PSRAM, ADC1/ADC2 and UART0 pins;
  - *ADC Oneshot Mode Driver*: `adc_oneshot_*`, `esp_adc` component;
  - *Build System*: `EXTRA_COMPONENT_DIRS`, `idf_component_register`, `sdkconfig.defaults`.
- ESP-IDF v6.1 headers (installed tree):
  - `adc_oneshot_io_to_channel()`;
  - `esp_chip_info()`;
  - `esp_timer_get_time()`;
  - `ADC_ATTEN_DB_{0,2_5,6,12}`;
  - ESP32 `SOC_ADC_RTC_{MIN,MAX}_BITWIDTH` = 9/12;
  - `SOC_GPIO_VALID[_OUTPUT]_GPIO_MASK`.
- Upside Down Labs, BioAmp EXG Pill documentation (v1.0 page).
