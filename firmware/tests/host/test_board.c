/* Built once per board directory (see CMakeLists.txt). Invariants every board
 * file must satisfy, plus per-board expectations. */
#include <stdio.h>

#include "vg/pin_check.h"
#include "vg_test.h"

#ifndef VG_TEST_BOARD
#error "VG_TEST_BOARD not defined"
#endif

static void print_line(void *ctx, const char *line)
{
    (void)ctx;
    printf("    %s\n", line);
}

static void test_roles(void)
{
    CHECK(vg_board_wearable.role == VG_ROLE_WEARABLE);
    CHECK(vg_board_station.role == VG_ROLE_STATION);
    CHECK(strcmp(vg_board_wearable.board_name, VG_TEST_BOARD) == 0);
    CHECK(strcmp(vg_board_station.board_name, VG_TEST_BOARD) == 0);
}

static void test_no_errors_and_motor_off(void)
{
    vg_issue_t is[32];
    int nw = vg_pin_check(&vg_board_wearable, is, 32);
    CHECK(nw <= 32 && vg_issue_error_count(is, nw) == 0);
    int ns = vg_pin_check(&vg_board_station, is, 32);
    CHECK(ns <= 32 && vg_issue_error_count(is, ns) == 0);

    /* No board in the repository may permit motor output until the motor,
     * driver and wiring are confirmed. Update this deliberately when they are. */
    CHECK(!vg_board_station.motor.enabled);
    CHECK(!vg_motor_output_permitted(&vg_board_station));
    CHECK(!vg_motor_output_permitted(&vg_board_wearable));
}

static void test_board_specific(void)
{
    if (strcmp(VG_TEST_BOARD, "unconfigured") == 0 || strcmp(VG_TEST_BOARD, "TEMPLATE") == 0) {
        CHECK(vg_board_wearable.chip == VG_CHIP_UNKNOWN);
        CHECK(!vg_board_wearable.emg.enabled && !vg_board_wearable.imu.enabled);
        CHECK(!vg_board_station.imu.enabled && !vg_board_station.velostat.enabled);
    }
    if (strcmp(VG_TEST_BOARD, "esp32_devkit30_provisional") == 0) {
        /* photo-based: never presented as confirmed */
        CHECK(vg_board_wearable.chip_status == VG_FACT_PROVISIONAL);
        CHECK(vg_board_station.chip_status == VG_FACT_PROVISIONAL);
        /* everything depending on unresolved facts stays disabled */
        CHECK(!vg_board_wearable.emg.enabled);
        CHECK(!vg_board_wearable.imu.enabled && !vg_board_station.imu.enabled);
        CHECK(!vg_board_station.velostat.enabled);
        CHECK(!vg_board_station.oled.enabled && !vg_board_station.dht11.enabled);
    }
    if (strcmp(VG_TEST_BOARD, "esp32_d0wd_v3_devkit30") == 0) {
        /* chip read with esptool on board W; peripherals still unverified */
        CHECK(vg_board_wearable.chip == VG_CHIP_ESP32);
        CHECK(vg_board_wearable.chip_status == VG_FACT_CONFIRMED);
        CHECK(!vg_board_wearable.emg.enabled);
        CHECK(!vg_board_wearable.imu.enabled && !vg_board_station.imu.enabled);
        CHECK(!vg_board_station.velostat.enabled);
        CHECK(!vg_board_station.oled.enabled && !vg_board_station.dht11.enabled);
        /* GPIO12 strapping: flash voltage 3.3 V; nothing assigned there */
        CHECK(vg_board_wearable.emg.gpio != 12 && vg_board_station.motor.gpio != 12);
    }
}

int main(void)
{
    printf("board %s\n", VG_TEST_BOARD);
    RUN(test_roles);
    RUN(test_no_errors_and_motor_off);
    RUN(test_board_specific);
    vg_config_report(&vg_board_wearable, print_line, NULL);
    vg_config_report(&vg_board_station, print_line, NULL);
    return DONE();
}
