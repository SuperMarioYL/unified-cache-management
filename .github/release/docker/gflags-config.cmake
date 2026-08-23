# Compatibility package for the gflags 2.1.x RPM shipped by Ascend manylinux.
find_library(GFLAGS_LIBRARY NAMES gflags REQUIRED)
find_path(GFLAGS_INCLUDE_DIR NAMES gflags/gflags.h REQUIRED)

if(NOT TARGET gflags::gflags)
  add_library(gflags::gflags UNKNOWN IMPORTED)
  set_target_properties(gflags::gflags PROPERTIES
    IMPORTED_LOCATION "${GFLAGS_LIBRARY}"
    INTERFACE_INCLUDE_DIRECTORIES "${GFLAGS_INCLUDE_DIR}"
  )
endif()

set(gflags_FOUND TRUE)
