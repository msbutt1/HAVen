cmake_minimum_required(VERSION 3.22)
project(haven_firmware_host_tests C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)
add_compile_options(-Wall -Wextra -Werror -Wshadow -Wconversion -Wno-sign-conversion)

add_subdirectory(${CMAKE_CURRENT_LIST_DIR}/../../shared ${CMAKE_BINARY_DIR}/shared)

enable_testing()

add_executable(test_logic test_logic.c)
target_link_libraries(test_logic PRIVATE vg_shared)
target_include_directories(test_logic PRIVATE ${CMAKE_CURRENT_LIST_DIR})
add_test(NAME logic COMMAND test_logic)

add_executable(test_telemetry test_telemetry.c)
target_link_libraries(test_telemetry PRIVATE vg_shared)
target_include_directories(test_telemetry PRIVATE ${CMAKE_CURRENT_LIST_DIR})
add_test(NAME telemetry COMMAND test_telemetry)

add_executable(test_imu test_imu.c)
target_link_libraries(test_imu PRIVATE vg_shared)
target_include_directories(test_imu PRIVATE ${CMAKE_CURRENT_LIST_DIR})
add_test(NAME imu COMMAND test_imu)

add_executable(test_station test_station.c)
target_link_libraries(test_station PRIVATE vg_shared)
target_include_directories(test_station PRIVATE ${CMAKE_CURRENT_LIST_DIR})
add_test(NAME station COMMAND test_station)

# Host build of the SIMULATED fake device (same shared code as the firmware).
add_executable(vg-fake-device ${CMAKE_CURRENT_LIST_DIR}/../../tools/fake_device/fake_device.c)
target_link_libraries(vg-fake-device PRIVATE vg_shared)

add_executable(vg-fake-station ${CMAKE_CURRENT_LIST_DIR}/../../tools/fake_station/fake_station.c)
target_link_libraries(vg-fake-station PRIVATE vg_shared)

# Cross-check generated frames with the authoritative Python validator and
# processor (signals/). Skipped (not passed) if uv is unavailable.
find_program(UV_EXE uv)
if(UV_EXE)
    add_test(NAME contract_validation
             COMMAND ${UV_EXE} run --project ${CMAKE_CURRENT_LIST_DIR}/../../../signals
                     python ${CMAKE_CURRENT_LIST_DIR}/check_fake_streams.py $<TARGET_FILE:vg-fake-device>)
    add_test(NAME station_protocol
             COMMAND ${UV_EXE} run --project ${CMAKE_CURRENT_LIST_DIR}/../../../signals
                     python ${CMAKE_CURRENT_LIST_DIR}/check_station.py ${CMAKE_BINARY_DIR})
else()
    message(WARNING "uv not found: contract_validation test not registered")
endif()

# One test binary per board directory: every board file in the repository is
# compiled and checked, including the template.
file(GLOB _board_dirs LIST_DIRECTORIES true ${CMAKE_CURRENT_LIST_DIR}/../../boards/*)
foreach(_dir ${_board_dirs})
    if(IS_DIRECTORY ${_dir} AND EXISTS ${_dir}/board.c)
        get_filename_component(_name ${_dir} NAME)
        add_executable(test_board_${_name} test_board.c ${_dir}/board.c)
        target_link_libraries(test_board_${_name} PRIVATE vg_shared)
        target_include_directories(test_board_${_name} PRIVATE ${CMAKE_CURRENT_LIST_DIR})
        target_compile_definitions(test_board_${_name} PRIVATE VG_TEST_BOARD="${_name}")
        add_test(NAME board_${_name} COMMAND test_board_${_name})
    endif()
endforeach()
