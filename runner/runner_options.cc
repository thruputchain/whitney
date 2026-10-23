#include "runner_options.h"

#include <algorithm>
#include <charconv>
#include <cstddef>
#include <format>
#include <initializer_list>
#include <limits>
#include <stdexcept>
#include <string>
#include <string_view>
#include <system_error>
#include <utility>
#include <vector>

#include "hogd/base/json.h"

namespace whitney {
namespace {

using hogd::json::Value;

// Guard limits, the same as whitney/config.py.
// The global uniquifier layout needs a block of at least 4,000 txs, or block 1
// would get an already-expired height.
constexpr uint32_t kMinTxsPerBlock = kGlobalNoncesPerHeight;
constexpr uint32_t kMinAccounts = 2; // hogd's loadgen needs two addresses
constexpr std::string_view kExperiments[] = {"erc20_payments"};
// Known to hogd, but not supported: its signatures cover the whole tx, so
// rewriting the uniquifier per block invalidates them.
constexpr std::string_view kBasicAuthExperiment = "erc20_basic_auth_payments";

[[noreturn]] void
fail(const std::string &msg)
{
  throw std::invalid_argument(msg);
}

// check_keys rejects keys of obj that are not in allowed. path names obj in
// the message ("" for the top level).
void
check_keys(const Value &obj, std::string_view path,
           std::initializer_list<std::string_view> allowed)
{
  for (const auto &[key, value] : obj.as_object()) {
    bool known = false;
    for (std::string_view a : allowed) {
      known = known || key == a;
    }
    if (!known) {
      fail(std::format("roles: unknown key: {}{}", path, key));
    }
  }
}

const Value &
need(const Value &obj, std::string_view path, std::string_view key)
{
  const Value *v = obj.find(key);
  if (v == nullptr) {
    fail(std::format("roles: missing key: {}{}", path, key));
  }
  return *v;
}

std::string
need_string(const Value &obj, std::string_view path, std::string_view key)
{
  std::optional<std::string_view> s = need(obj, path, key).as_string();
  if (!s) {
    fail(std::format("roles: {}{} must be a string", path, key));
  }
  return std::string(*s);
}

uint16_t
need_port(const Value &obj, std::string_view path, std::string_view key)
{
  std::optional<int64_t> port = need(obj, path, key).as_integer();
  if (!port || *port < 1 || *port > std::numeric_limits<uint16_t>::max()) {
    fail(std::format("roles: {}{} must be an integer in 1..65535", path, key));
  }
  return static_cast<uint16_t>(*port);
}

// check_metrics_ports enforces that each role's metrics_port is free on its
// host: no other role's metrics_port or control_port there uses it.
void
check_metrics_ports(const Roles &r)
{
  std::vector<std::pair<std::string, const RoleAddr *>> roles = {
      {"event", &r.event}, {"db", &r.db}};
  if (r.indexer) {
    roles.emplace_back("indexer", &*r.indexer);
  }
  for (std::size_t i = 0; i < r.execs.size(); ++i) {
    roles.emplace_back(std::format("exec{}", i), &r.execs[i]);
  }
  for (std::size_t i = 0; i < roles.size(); ++i) {
    const auto &[name, role] = roles[i];
    for (std::size_t j = 0; j < roles.size(); ++j) {
      const auto &[other_name, other] = roles[j];
      if (i == j || other->host != role->host) {
        continue;
      }
      if (j < i && other->metrics_port == role->metrics_port) {
        fail(std::format("metrics_port {} is used by both {} and {} on host {}",
                         role->metrics_port, other_name, name, role->host));
      }
      if (other->control_port == role->metrics_port) {
        fail(std::format("metrics_port {} is used by both {} (control_port) "
                         "and {} on host {}",
                         role->metrics_port, other_name, name, role->host));
      }
    }
  }
}

RoleAddr
parse_role(const Value &v, const std::string &name)
{
  if (v.type() != Value::Type::kObject) {
    fail(std::format("roles: {} must be an object", name));
  }
  const std::string path = name + ".";
  check_keys(v, path,
             {"host", "control_port", "metrics_port", "work_dir",
              "cpu_affinity"});
  RoleAddr r;
  r.host = need_string(v, path, "host");
  r.control_port = need_port(v, path, "control_port");
  r.metrics_port = need_port(v, path, "metrics_port");
  r.work_dir = need_string(v, path, "work_dir");
  r.cpu_affinity = need_string(v, path, "cpu_affinity");
  return r;
}

uint32_t
parse_u32(std::string_view flag, std::string_view s)
{
  uint32_t n = 0;
  const char *end = s.data() + s.size();
  auto [ptr, ec] = std::from_chars(s.data(), end, n);
  if (s.empty() || ec != std::errc{} || ptr != end) {
    fail(std::format("{} needs an unsigned 32-bit integer, got: {}", flag, s));
  }
  return n;
}

std::vector<uint32_t>
parse_batch_sizes(std::string_view flag, std::string_view s)
{
  std::vector<uint32_t> out;
  for (;;) {
    std::size_t comma = s.find(',');
    uint32_t n = parse_u32(flag, s.substr(0, comma));
    if (n == 0) {
      fail(std::format("{} values must be >= 1", flag));
    }
    out.push_back(n);
    if (comma == std::string_view::npos) {
      return out;
    }
    s.remove_prefix(comma + 1);
  }
}

} // namespace

Roles
parse_roles_json(std::string_view json)
{
  auto parsed = hogd::json::parse(json);
  if (!parsed) {
    fail(std::format("roles: invalid JSON at byte {}: {}",
                     parsed.error().offset, parsed.error().message));
  }
  const Value &top = *parsed;
  if (top.type() != Value::Type::kObject) {
    fail("roles: top level must be an object");
  }
  check_keys(top, "",
             {"bin_dir", "runner_bind_ip", "event", "db", "indexer", "execs"});

  Roles r;
  r.bin_dir = need_string(top, "", "bin_dir");
  r.runner_bind_ip = need_string(top, "", "runner_bind_ip");
  r.event = parse_role(need(top, "", "event"), "event");
  r.db = parse_role(need(top, "", "db"), "db");
  if (const Value &ix = need(top, "", "indexer"); !ix.is_null()) {
    r.indexer = parse_role(ix, "indexer");
  }
  const Value &execs = need(top, "", "execs");
  if (execs.type() != Value::Type::kArray) {
    fail("roles: execs must be an array");
  }
  std::size_t i = 0;
  for (const Value &e : execs.as_array()) {
    r.execs.push_back(parse_role(e, std::format("execs[{}]", i++)));
  }
  return r;
}

Options
parse_options(int argc, char **argv)
{
  Options o;
  for (int i = 1; i < argc; ++i) {
    const std::string_view flag = argv[i];
    auto value = [&]() -> std::string_view {
      if (i + 1 >= argc) {
        fail(std::format("missing value for {}", flag));
      }
      return argv[++i];
    };
    if (flag == "--help" || flag == "-h") {
      o.help = true;
      return o;
    }
    if (flag == "--roles") {
      o.roles_path = value();
    } else if (flag == "--accounts") {
      o.accounts = parse_u32(flag, value());
    } else if (flag == "--experiment") {
      o.experiment = value();
    } else if (flag == "--txs-per-block") {
      o.txs_per_block = parse_u32(flag, value());
    } else if (flag == "--batch-sizes") {
      o.batch_sizes = parse_batch_sizes(flag, value());
    } else if (flag == "--warmup-blocks") {
      o.warmup_blocks = parse_u32(flag, value());
    } else if (flag == "--timed-blocks") {
      o.timed_blocks = parse_u32(flag, value());
    } else if (flag == "--connections-per-exec") {
      o.connections_per_exec = parse_u32(flag, value());
    } else if (flag == "--coros-per-connection") {
      o.coros_per_connection = parse_u32(flag, value());
    } else if (flag == "--uniquifier-layout") {
      const std::string_view v = value();
      std::optional<UniquifierLayout> layout = parse_uniquifier_layout(v);
      if (!layout) {
        fail(std::format("{} must be per-sender or global, got: {}", flag, v));
      }
      o.uniquifier_layout = *layout;
    } else if (flag == "--json-out") {
      o.json_out = value();
    } else {
      fail(std::format("unknown flag: {}", flag));
    }
  }
  if (o.roles_path.empty()) {
    fail("--roles is required");
  }
  return o;
}

std::string
usage_text(std::string_view prog)
{
  const Options d;
  std::string sizes;
  for (uint32_t b : d.batch_sizes) {
    sizes += std::format("{}{}", sizes.empty() ? "" : ",", b);
  }
  return std::format(
      "Usage: {} --roles <path> [flags]\n"
      "  --roles <path>                roles.json: hosts, ports, work dirs\n"
      "  --accounts <n>                loadgen accounts (default {})\n"
      "  --experiment <name>           erc20_payments, the only supported "
      "one (default {})\n"
      "  --txs-per-block <n>           work txs per block, >= 4000 "
      "(default {})\n"
      "  --batch-sizes <n,n,...>       one fresh-genesis run per batch size "
      "(default {})\n"
      "  --warmup-blocks <n>           warm-up blocks per run (default {})\n"
      "  --timed-blocks <n>            timed blocks per run (default {})\n"
      "  --connections-per-exec <n>    (default {})\n"
      "  --coros-per-connection <n>    (default {})\n"
      "  --uniquifier-layout <name>    per-sender or global, the original "
      "runner's (default {})\n"
      "  --json-out <path>             write one JSON line per block\n"
      "  --help                        print this text\n",
      prog, d.accounts, d.experiment, d.txs_per_block, sizes, d.warmup_blocks,
      d.timed_blocks, d.connections_per_exec, d.coros_per_connection,
      layout_name(d.uniquifier_layout));
}

void
validate(const Options &o, const Roles &r)
{
  if (o.experiment == kBasicAuthExperiment) {
    fail(std::format("experiment {} is not supported: its signatures cover "
                     "the uniquifier the runner rewrites",
                     kBasicAuthExperiment));
  }
  bool known = false;
  std::string names;
  for (std::string_view e : kExperiments) {
    known = known || o.experiment == e;
    names += names.empty() ? "" : ", ";
    names += e;
  }
  if (!known) {
    fail(std::format("experiment must be one of {}", names));
  }
  if (o.txs_per_block < kMinTxsPerBlock) {
    fail(std::format("txs_per_block must be >= {}", kMinTxsPerBlock));
  }
  // Each batch size is one fresh-genesis run with its own trial rows.
  std::vector<uint32_t> sorted = o.batch_sizes;
  std::ranges::sort(sorted);
  std::string repeated;
  for (std::size_t i = 1; i < sorted.size(); ++i) {
    if (sorted[i] == sorted[i - 1] &&
        (i < 2 || sorted[i - 2] != sorted[i])) {
      repeated += std::format("{}{}", repeated.empty() ? "" : ", ", sorted[i]);
    }
  }
  if (!repeated.empty()) {
    fail(std::format("batch_sizes must not repeat: {}", repeated));
  }
  if (o.accounts < kMinAccounts) {
    fail(std::format("accounts must be >= {}", kMinAccounts));
  }
  if (o.connections_per_exec == 0) {
    fail("connections_per_exec must be >= 1");
  }
  if (o.coros_per_connection == 0) {
    fail("coros_per_connection must be >= 1");
  }
  if (r.execs.empty()) {
    fail("execs must have at least one entry");
  }
  if (!r.bin_dir.starts_with('/')) {
    fail(std::format("bin_dir must be an absolute path, got: {}", r.bin_dir));
  }
  check_metrics_ports(r);
}

std::vector<std::string>
with_metrics_port(std::vector<std::string> args, const RoleAddr &role)
{
  args.emplace_back("--metrics-port");
  args.push_back(std::to_string(role.metrics_port));
  return args;
}

} // namespace whitney
