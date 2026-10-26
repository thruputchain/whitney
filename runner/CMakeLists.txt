# whitney-runner: Whitney's fork of hogd's multiexec_runner.
#
# hogd adds this directory from hogd/hog_exec/bench through
# -DHOGD_EXTRA_BENCH_DIRS=<abs path to runner/>, so it gets the same scope,
# include dirs and link targets as multiexec_runner.

add_executable(
  whitney-runner
  whitney_runner.cc
)
target_link_libraries(
  whitney-runner
  hogd
  protocol
  runtime
  exec_interface
  hog_db
  runtime_test_util
  contract_loader
  loadgen
)

target_include_directories(
  whitney-runner
  PRIVATE
  ${CMAKE_BINARY_DIR}
)

# The archive ships built_contracts/*.rv32a next to the runner, so build them
# with it instead of relying on loadgen's own dependency.
add_dependencies(
  whitney-runner
  all_riscvs_built
)

set_property(TARGET whitney-runner PROPERTY INTERPROCEDURAL_OPTIMIZATION TRUE)

# Pure runner logic (options, roles.json, guards, trial record, uniquifier
# layout), kept apart from whitney_runner.cc so it can be unit tested.
add_library(
  whitney_runner_lib STATIC
  runner_options.cc
  run_record.cc
  uniquifier_layout.cc
)
target_link_libraries(
  whitney_runner_lib
  PUBLIC
  hogd
  protocol
)
target_include_directories(
  whitney_runner_lib
  PUBLIC
  ${CMAKE_CURRENT_SOURCE_DIR}
  ${CMAKE_BINARY_DIR}
)

# Plain signature, like the target_link_libraries call above: CMake does not
# allow mixing it with the keyword form on one target.
target_link_libraries(
  whitney-runner
  whitney_runner_lib
)

# Unit tests. Not registered with hogd_discover_tests: discovery runs the
# binary at build time. build/build.sh --test runs it instead.
add_executable(
  whitney_runner_test
  runner_options_test.cc
  run_record_test.cc
  uniquifier_layout_test.cc
)
target_link_libraries(
  whitney_runner_test
  whitney_runner_lib
  GTest::gtest
  GTest::gtest_main
)
