# VibeGuard bring-up diagnostic (ESP-IDF project). Build with: ../tools/build.sh diag <board>
cmake_minimum_required(VERSION 3.22)
set(VG_BOARD "unconfigured" CACHE STRING "board directory under firmware/boards")
set(EXTRA_COMPONENT_DIRS
    "${CMAKE_CURRENT_LIST_DIR}/../shared"
    "${CMAKE_CURRENT_LIST_DIR}/../boards"
    "${CMAKE_CURRENT_LIST_DIR}/../target")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(vibeguard_diag)
