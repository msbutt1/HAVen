# Board facts component (vg_board). The board is selected with the cache
# variable VG_BOARD (default: unconfigured). Each board directory holds data only.
if(NOT DEFINED VG_BOARD OR VG_BOARD STREQUAL "")
    set(VG_BOARD "unconfigured")
endif()

set(_vg_board_src "${CMAKE_CURRENT_LIST_DIR}/${VG_BOARD}/board.c")
if(NOT EXISTS "${_vg_board_src}")
    message(FATAL_ERROR "VG_BOARD='${VG_BOARD}': no ${_vg_board_src}")
endif()

if(ESP_PLATFORM)
    idf_component_register(SRCS "${_vg_board_src}" REQUIRES shared)
    target_compile_definitions(${COMPONENT_LIB} PUBLIC VG_BOARD_NAME="${VG_BOARD}")
else()
    add_library(vg_board_${VG_BOARD} STATIC "${_vg_board_src}")
    target_link_libraries(vg_board_${VG_BOARD} PUBLIC vg_shared)
endif()
