set(FMT_INSTALL OFF CACHE INTERNAL "" FORCE)
set(FMT_BUILD_TESTS OFF CACHE INTERNAL "" FORCE)
set(FMT_BUILD_EXAMPLES OFF CACHE INTERNAL "" FORCE)

if(DOWNLOAD_DEPENDENCE)
    set(DEP_FMT_NAME fmt)
    set(DEP_FMT_TAG 40626af88bd7df9a5fb80be7b25ac85b122d6c21)
    set(DEP_FMT_GIT_URLS
        https://github.com/fmtlib/fmt.git
        https://gitcode.com/GitHub_Trending/fm/fmt.git
    )
    include(helper.cmake)
    find_reachable_git_url(REACHABLE_URL DEP_FMT_GIT_URLS)
    include(FetchContent)
    message(STATUS "Fetching ${DEP_FMT_NAME}(${DEP_FMT_TAG}) from ${REACHABLE_URL}")
    FetchContent_Declare(${DEP_FMT_NAME} GIT_REPOSITORY ${REACHABLE_URL} GIT_TAG ${DEP_FMT_TAG})
    FetchContent_MakeAvailable(${DEP_FMT_NAME})
else()
    add_subdirectory(fmt)
endif()
