cmake_minimum_required(VERSION 3.15)
project(whisper_addon C CXX)

# The RealTimeX Electron addon around upstream whisper.cpp. whisper and ggml are built by
# upstream's own CMake (static, linked into the addon), so new or moved upstream sources
# (e.g. ggml-metal's kernels/ in v1.9.5) need no change here.

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(WHISPER_ROOT ${CMAKE_CURRENT_SOURCE_DIR}/../..)

if(MSVC)
    # Static runtime everywhere (addon, whisper, ggml) for portability.
    set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
endif()

# Upstream build options.
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(WHISPER_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(WHISPER_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(WHISPER_BUILD_SERVER OFF CACHE BOOL "" FORCE)
set(GGML_NATIVE OFF CACHE BOOL "" FORCE)        # portable binaries, not tuned to the build machine
set(GGML_BACKEND_DL OFF CACHE BOOL "" FORCE)    # backends linked in, not loaded at runtime
if(CMAKE_SYSTEM_PROCESSOR MATCHES "x86_64|AMD64|x64")
    set(GGML_AVX ON CACHE BOOL "" FORCE)
    set(GGML_AVX2 ON CACHE BOOL "" FORCE)
    set(GGML_F16C ON CACHE BOOL "" FORCE)
    set(GGML_FMA ON CACHE BOOL "" FORCE)
endif()
if(APPLE)
    set(GGML_METAL ON CACHE BOOL "" FORCE)
    set(GGML_METAL_EMBED_LIBRARY ON CACHE BOOL "" FORCE)
    set(GGML_ACCELERATE ON CACHE BOOL "" FORCE)
    set(GGML_VULKAN OFF CACHE BOOL "" FORCE)    # Vulkan is Windows/Linux only (opt-in: -DGGML_VULKAN=ON)
endif()

# Vulkan (Windows/Linux) stays optional, as before: without the Vulkan SDK and glslc on the
# build machine, build the CPU backend instead of failing.
if(GGML_VULKAN AND NOT APPLE)
    find_package(Vulkan COMPONENTS glslc QUIET)
    if(NOT (Vulkan_FOUND AND Vulkan_GLSLC_EXECUTABLE))
        message(WARNING "GGML_VULKAN=ON but Vulkan SDK or glslc not found — skipping Vulkan backend")
        set(GGML_VULKAN OFF CACHE BOOL "" FORCE)
    endif()
endif()

add_subdirectory(${WHISPER_ROOT} whisper-build EXCLUDE_FROM_ALL)

add_library(whisper_addon SHARED
    native/whisper_addon.cc
    native/audio_pipeline.cc
    ${CMAKE_JS_SRC}
)
set_target_properties(whisper_addon PROPERTIES PREFIX "" SUFFIX ".node")
target_include_directories(whisper_addon PRIVATE
    ${CMAKE_JS_INC}
    ${CMAKE_CURRENT_SOURCE_DIR}/node_modules/node-addon-api
)
target_compile_definitions(whisper_addon PRIVATE NAPI_DISABLE_CPP_EXCEPTIONS)
target_link_libraries(whisper_addon PRIVATE whisper ${CMAKE_JS_LIB})

if(APPLE)
    set_target_properties(whisper_addon PROPERTIES LINK_FLAGS "-undefined dynamic_lookup")
endif()

if(WIN32)
    target_link_libraries(whisper_addon PRIVATE delayimp)
endif()

if(UNIX AND NOT APPLE)
    target_compile_definitions(whisper_addon PRIVATE _GNU_SOURCE)
    target_link_libraries(whisper_addon PRIVATE m pthread)
    set_target_properties(whisper_addon PROPERTIES
        BUILD_WITH_INSTALL_RPATH TRUE
        INSTALL_RPATH "$ORIGIN"
    )
endif()
