// runner_options.h - whitney-runner command line, roles file and guards.

#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "uniquifier_layout.h"

namespace whitney {

// RoleAddr is one host role from roles.json: where its control_server
// listens and how it runs. The role's daemon serves /metrics on
// host:metrics_port.
struct RoleAddr {
  std::string host;
  uint16_t control_port;
  uint16_t metrics_port;
  std::string work_dir;
  std::string cpu_affinity;
};

// Roles is the parsed roles.json. indexer is nullopt when the file has
// "indexer": null.
struct Roles {
  RoleAddr event, db;
  std::optional<RoleAddr> indexer;
  std::vector<RoleAddr> execs;
  std::string bin_dir;
  std::string runner_bind_ip;
};

// Options is the whitney-runner command line. The defaults match today's
// exec-scaling job.
struct Options {
  std::string roles_path;
  uint32_t accounts = 100;
  std::string experiment = "erc20_payments";
  uint32_t txs_per_block = 1'000'000;
  std::vector<uint32_t> batch_sizes{1, 5, 10};
  uint32_t warmup_blocks = 0;
  uint32_t timed_blocks = 5;
  uint32_t connections_per_exec = 10;
  uint32_t coros_per_connection = 100;
  UniquifierLayout uniquifier_layout = UniquifierLayout::kPerSender;
  std::string json_out;
  bool help = false; // --help or -h: print usage_text and exit 0
};

// parse_roles_json parses a roles.json document. It throws
// std::invalid_argument, with the offending key in the message, on bad JSON,
// a missing or unknown key, or a value of the wrong type.
Roles parse_roles_json(std::string_view json);

// parse_options parses argv. Flags take a separate value:
//   --roles <path> (required) --accounts <n> --experiment <name>
//   --txs-per-block <n> --batch-sizes <n,n,...> --warmup-blocks <n>
//   --timed-blocks <n> --connections-per-exec <n>
//   --coros-per-connection <n> --uniquifier-layout per-sender|global
//   --json-out <path>
// --help or -h sets help and stops parsing; --roles is then not required.
// It throws std::invalid_argument on an unknown flag, a missing value or a
// bad number or layout.
Options parse_options(int argc, char **argv);

// usage_text returns the help text: a "Usage: <prog> ..." line, then one
// line per flag, with the default where it has one.
std::string usage_text(std::string_view prog);

// validate enforces the scenario guards (the same checks and messages as
// whitney/config.py): a supported experiment, block size, distinct batch
// sizes, accounts >= 2, nonzero
// connections_per_exec and coros_per_connection, at least one exec and an
// absolute bin_dir. It throws std::invalid_argument on the first failed
// guard. Every metrics_port must differ from the other metrics and control
// ports on its host. The replay-cache budget needs the generated work txs, so the runner
// checks it later, with check_replay_budget.
void validate(const Options &options, const Roles &roles);

// with_metrics_port returns args plus "--metrics-port <role.metrics_port>".
// The runner passes it to every daemon it starts; hogd binds the metrics
// server to the daemon's --bind-ip.
std::vector<std::string> with_metrics_port(std::vector<std::string> args,
                                           const RoleAddr &role);

} // namespace whitney
