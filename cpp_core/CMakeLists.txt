cmake_minimum_required(VERSION 3.16)
project(nervlynx_cpp_core LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_library(nervlynx_core INTERFACE)
target_include_directories(nervlynx_core INTERFACE ${CMAKE_CURRENT_SOURCE_DIR}/include)

add_executable(smoke_surveillance src/smoke_surveillance.cpp)
target_link_libraries(smoke_surveillance PRIVATE nervlynx_core)

enable_testing()
add_test(NAME smoke_surveillance_test COMMAND smoke_surveillance)
