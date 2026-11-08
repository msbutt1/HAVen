# Shared portable layer. Builds as an ESP-IDF component (vg_shared) on target,
# or as a plain static library for host tests.
set(VG_SHARED_SRCS
    src/chip_caps.c
    src/pin_check.c
    src/names.c
    src/fake_source.c
    src/sample_ring.c
    src/frame_queue.c
    src/telemetry.c
    src/batcher.c
    src/link.c
    src/imu_mpu.c
    src/jsonflat.c
    src/haptic_fake.c
    src/station.c)

if(ESP_PLATFORM)
    idf_component_register(SRCS ${VG_SHARED_SRCS} INCLUDE_DIRS include)
else()
    add_library(vg_shared STATIC ${VG_SHARED_SRCS})
    target_include_directories(vg_shared PUBLIC include)
    target_link_libraries(vg_shared PUBLIC m)
    # Embedded task stacks are small (a few KB): forbid large frames in shared code.
    target_compile_options(vg_shared PRIVATE -Wframe-larger-than=1024)
endif()
