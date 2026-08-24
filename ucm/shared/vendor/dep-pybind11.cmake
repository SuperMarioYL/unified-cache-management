set(PYBIND11_INSTALL OFF CACHE INTERNAL "" FORCE)
set(PYBIND11_BUILD_TESTS OFF CACHE INTERNAL "" FORCE)
set(PYBIND11_BUILD_EXAMPLES OFF CACHE INTERNAL "" FORCE)

if(DOWNLOAD_DEPENDENCE)
    set(DEP_PYBIND11_NAME pybind11)
    set(DEP_PYBIND11_TAG f5fbe867d2d26e4a0a9177a51f6e568868ad3dc8)
    set(DEP_PYBIND11_GIT_URLS
        https://github.com/pybind/pybind11.git
        https://gitcode.com/GitHub_Trending/py/pybind11.git
    )
    include(helper.cmake)
    find_reachable_git_url(REACHABLE_URL DEP_PYBIND11_GIT_URLS)
    include(FetchContent)
    message(STATUS "Fetching ${DEP_PYBIND11_NAME}(${DEP_PYBIND11_TAG}) from ${REACHABLE_URL}")
    FetchContent_Declare(${DEP_PYBIND11_NAME} GIT_REPOSITORY ${REACHABLE_URL} GIT_TAG ${DEP_PYBIND11_TAG})
    FetchContent_MakeAvailable(${DEP_PYBIND11_NAME})
else()
    add_subdirectory(pybind11)
endif()
