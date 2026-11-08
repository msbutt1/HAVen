#!/usr/bin/env bash
# Build one role for one board directory, in an isolated build dir.
#   tools/build.sh <wearable|station|diag> <board> [idf.py args...]
# Requires ESP-IDF v6.1 exported: . ~/esp/esp-idf-v6.1/export.sh
set -euo pipefail
role=${1:?role: wearable|station}; board=${2:?board: directory under firmware/boards}; shift 2
here=$(cd "$(dirname "$0")/.." && pwd)
[[ $role == wearable || $role == station || $role == diag ]] || { echo "bad role $role" >&2; exit 2; }
[[ -f $here/boards/$board/board.c ]] || { echo "no board $board" >&2; exit 2; }
[[ -n ${IDF_PATH:-} ]] || { echo "ESP-IDF not exported: . ~/esp/esp-idf-v6.1/export.sh" >&2; exit 2; }
want=$(cat "$here/IDF_VERSION")
have=$(cd "$IDF_PATH" && git describe --tags --exact-match 2>/dev/null || echo unknown)
[[ $have == "$want" ]] || { echo "ESP-IDF $have != pinned $want" >&2; exit 2; }
# Chip target: board W read as ESP32-D0WD-V3 by esptool; IDF target esp32.
target=${VG_IDF_TARGET:-esp32}
bdir=$here/build/$role-$board
# Role defaults first, then board facts (e.g. measured flash size) override them.
defaults="$here/$role/sdkconfig.defaults"
[[ -f $here/boards/$board/sdkconfig.defaults ]] && defaults="$defaults;$here/boards/$board/sdkconfig.defaults"
cd "$here/$role"
exec idf.py -B "$bdir" -DIDF_TARGET="$target" -DVG_BOARD="$board" \
     -DSDKCONFIG="$bdir/sdkconfig" -DSDKCONFIG_DEFAULTS="$defaults" "${@:-build}"
