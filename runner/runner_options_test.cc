#include "runner_options.h"

#include <gtest/gtest.h>

#include <initializer_list>
#include <stdexcept>
#include <string>
#include <vector>

namespace whitney {
namespace {

constexpr char kRolesJson[] = R"({"bin_dir":"/opt/whitney/bin","runner_bind_ip":"10.0.1.10",
 "event":{"host":"10.0.1.11","control_port":11111,"metrics_port":9101,"work_dir":"/mnt/whitney/work/event","cpu_affinity":""},
 "db":{"host":"10.0.1.12","control_port":11112,"metrics_port":9102,"work_dir":"/mnt/whitney/work/db","cpu_affinity":"0-15"},
 "indexer":{"host":"10.0.1.13","control_port":11113,"metrics_port":9103,"work_dir":"/mnt/whitney/work/indexer","cpu_affinity":""},
 "execs":[{"host":"10.0.1.14","control_port":11114,"metrics_port":9104,"work_dir":"/mnt/whitney/work/exec0","cpu_affinity":""},
          {"host":"10.0.1.15","control_port":11115,"metrics_port":9104,"work_dir":"/mnt/whitney/work/exec1","cpu_affinity":"16-31"}]})";

// args builds an argv for parse_options. The strings must outlive the call.
class Args {
public:
  Args(std::initializer_list<const char *> args) : strs_{"whitney-runner"}
  {
    strs_.insert(strs_.end(), args.begin(), args.end());
    for (std::string &s : strs_) {
      ptrs_.push_back(s.data());
    }
    ptrs_.push_back(nullptr);
  }
  int argc() const { return static_cast<int>(strs_.size()); }
  char **argv() { return ptrs_.data(); }

private:
  std::vector<std::string> strs_;
  std::vector<char *> ptrs_;
};

Options
parse(std::initializer_list<const char *> args)
{
  Args a(args);
  return parse_options(a.argc(), a.argv());
}

// expect_throw_with checks that f throws std::invalid_argument whose message
// contains want.
template<typename F>
void
expect_throw_with(F &&f, const std::string &want)
{
  try {
    f();
    ADD_FAILURE() << "no exception, want one containing: " << want;
  } catch (const std::invalid_argument &e) {
    EXPECT_NE(std::string(e.what()).find(want), std::string::npos)
        << "message: " << e.what() << "\nwant: " << want;
  }
}

Roles
good_roles()
{
  return parse_roles_json(kRolesJson);
}

Options
good_options()
{
  return parse({"--roles", "/etc/whitney/roles.json"});
}

TEST(ParseRoles, FullFile)
{
  Roles r = parse_roles_json(kRolesJson);
  EXPECT_EQ(r.bin_dir, "/opt/whitney/bin");
  EXPECT_EQ(r.runner_bind_ip, "10.0.1.10");

  EXPECT_EQ(r.event.host, "10.0.1.11");
  EXPECT_EQ(r.event.control_port, 11111);
  EXPECT_EQ(r.event.work_dir, "/mnt/whitney/work/event");
  EXPECT_EQ(r.event.cpu_affinity, "");

  EXPECT_EQ(r.db.host, "10.0.1.12");
  EXPECT_EQ(r.db.control_port, 11112);
  EXPECT_EQ(r.db.work_dir, "/mnt/whitney/work/db");
  EXPECT_EQ(r.db.cpu_affinity, "0-15");

  ASSERT_TRUE(r.indexer.has_value());
  EXPECT_EQ(r.indexer->host, "10.0.1.13");
  EXPECT_EQ(r.indexer->control_port, 11113);
  EXPECT_EQ(r.indexer->work_dir, "/mnt/whitney/work/indexer");

  ASSERT_EQ(r.execs.size(), 2u);
  EXPECT_EQ(r.execs[0].host, "10.0.1.14");
  EXPECT_EQ(r.execs[0].control_port, 11114);
  EXPECT_EQ(r.execs[0].work_dir, "/mnt/whitney/work/exec0");
  EXPECT_EQ(r.execs[1].host, "10.0.1.15");
  EXPECT_EQ(r.execs[1].control_port, 11115);
  EXPECT_EQ(r.execs[1].cpu_affinity, "16-31");

  EXPECT_EQ(r.event.metrics_port, 9101);
  EXPECT_EQ(r.db.metrics_port, 9102);
  EXPECT_EQ(r.indexer->metrics_port, 9103);
  EXPECT_EQ(r.execs[0].metrics_port, 9104);
  EXPECT_EQ(r.execs[1].metrics_port, 9104); // each exec on its own host
}

TEST(ParseRoles, MetricsPortIsRequired)
{
  expect_throw_with(
      [] {
        parse_roles_json(
            R"({"bin_dir":"/b","runner_bind_ip":"1.2.3.4",
                "event":{"host":"h","control_port":1,"work_dir":"/w","cpu_affinity":""},
                "db":{"host":"h","control_port":2,"metrics_port":9102,"work_dir":"/w","cpu_affinity":""},
                "indexer":null,
                "execs":[{"host":"h","control_port":3,"metrics_port":9110,"work_dir":"/w","cpu_affinity":""}]})");
      },
      "missing key: event.metrics_port");
  for (const char *bad : {"0", "65536", "\"9101\"", "-1"}) {
    const std::string json = std::string(R"({"bin_dir":"/b","runner_bind_ip":"1.2.3.4",
        "event":{"host":"h","control_port":1,"metrics_port":9101,"work_dir":"/w","cpu_affinity":""},
        "db":{"host":"h","control_port":2,"metrics_port":9102,"work_dir":"/w","cpu_affinity":""},
        "indexer":null,
        "execs":[{"host":"h","control_port":3,"metrics_port":)") +
                             bad + R"(,"work_dir":"/w","cpu_affinity":""}]})";
    expect_throw_with([&] { parse_roles_json(json); },
                      "execs[0].metrics_port must be an integer in 1..65535");
  }
}

TEST(ParseRoles, MissingDbThrows)
{
  expect_throw_with(
      [] {
        parse_roles_json(
            R"({"bin_dir":"/b","runner_bind_ip":"1.2.3.4",
                "event":{"host":"h","control_port":1,"metrics_port":9101,"work_dir":"/w","cpu_affinity":""},
                "indexer":null,
                "execs":[{"host":"h","control_port":2,"metrics_port":9102,"work_dir":"/w","cpu_affinity":""}]})");
      },
      "db");
}

TEST(ParseRoles, IndexerNullIsNullopt)
{
  Roles r = parse_roles_json(
      R"({"bin_dir":"/b","runner_bind_ip":"1.2.3.4",
          "event":{"host":"h","control_port":1,"metrics_port":9101,"work_dir":"/w","cpu_affinity":""},
          "db":{"host":"h","control_port":2,"metrics_port":9102,"work_dir":"/w","cpu_affinity":""},
          "indexer":null,
          "execs":[{"host":"h","control_port":3,"metrics_port":9103,"work_dir":"/w","cpu_affinity":""}]})");
  EXPECT_FALSE(r.indexer.has_value());
}

TEST(ParseRoles, BadValuesNameTheKey)
{
  expect_throw_with([] { parse_roles_json("{"); }, "roles");
  expect_throw_with([] { parse_roles_json("[]"); }, "object");
  expect_throw_with(
      [] {
        parse_roles_json(
            R"({"bin_dir":"/b","runner_bind_ip":"1.2.3.4",
                "event":{"host":"h","control_port":70000,"metrics_port":9100,"work_dir":"/w","cpu_affinity":""},
                "db":{"host":"h","control_port":2,"metrics_port":9102,"work_dir":"/w","cpu_affinity":""},
                "indexer":null,
                "execs":[{"host":"h","control_port":3,"metrics_port":9103,"work_dir":"/w","cpu_affinity":""}]})");
      },
      "event.control_port");
  expect_throw_with(
      [] {
        parse_roles_json(
            R"({"bin_dir":"/b","runner_bind_ip":"1.2.3.4",
                "event":{"host":"h","control_port":1,"metrics_port":9101,"work_dir":"/w","cpu_affinity":""},
                "db":{"host":"h","control_port":2,"metrics_port":9102,"work_dir":"/w","cpu_affinity":""},
                "indexer":null,
                "execs":[{"host":"h","control_port":3,"metrics_port":9103,"work_dir":"/w","cpu_affinity":""},
                         {"host":7,"control_port":4,"metrics_port":9104,"work_dir":"/w","cpu_affinity":""}]})");
      },
      "execs[1].host");
  expect_throw_with(
      [] {
        parse_roles_json(
            R"({"bin_dir":"/b","runner_bind_ip":"1.2.3.4","extra":1,
                "event":{"host":"h","control_port":1,"metrics_port":9101,"work_dir":"/w","cpu_affinity":""},
                "db":{"host":"h","control_port":2,"metrics_port":9102,"work_dir":"/w","cpu_affinity":""},
                "indexer":null,
                "execs":[{"host":"h","control_port":3,"metrics_port":9103,"work_dir":"/w","cpu_affinity":""}]})");
      },
      "extra");
}

TEST(ParseOptions, DefaultsMatchTodaysJob)
{
  Options o = parse({"--roles", "/etc/whitney/roles.json"});
  EXPECT_EQ(o.roles_path, "/etc/whitney/roles.json");
  EXPECT_EQ(o.accounts, 100u);
  EXPECT_EQ(o.experiment, "erc20_payments");
  EXPECT_EQ(o.txs_per_block, 1'000'000u);
  EXPECT_EQ(o.batch_sizes, (std::vector<uint32_t>{1, 5, 10}));
  EXPECT_EQ(o.warmup_blocks, 0u);
  EXPECT_EQ(o.timed_blocks, 5u);
  EXPECT_EQ(o.connections_per_exec, 10u);
  EXPECT_EQ(o.coros_per_connection, 100u);
  EXPECT_TRUE(o.uniquifier_layout == UniquifierLayout::kPerSender);
  EXPECT_EQ(o.json_out, "");

  // A default-constructed Options carries the same values.
  Options d;
  EXPECT_EQ(d.accounts, 100u);
  EXPECT_EQ(d.experiment, "erc20_payments");
  EXPECT_EQ(d.txs_per_block, 1'000'000u);
  EXPECT_EQ(d.batch_sizes, (std::vector<uint32_t>{1, 5, 10}));
  EXPECT_EQ(d.warmup_blocks, 0u);
  EXPECT_EQ(d.timed_blocks, 5u);
  EXPECT_EQ(d.connections_per_exec, 10u);
  EXPECT_EQ(d.coros_per_connection, 100u);
  EXPECT_TRUE(d.uniquifier_layout == UniquifierLayout::kPerSender);
}

TEST(ParseOptions, BatchSizesList)
{
  Options o = parse({"--roles", "r.json", "--batch-sizes", "5"});
  EXPECT_EQ(o.batch_sizes, (std::vector<uint32_t>{5}));
  o = parse({"--roles", "r.json", "--batch-sizes", "1,5,10"});
  EXPECT_EQ(o.batch_sizes, (std::vector<uint32_t>{1, 5, 10}));
}

TEST(ParseOptions, EveryFlag)
{
  Options o = parse({"--roles", "r.json", "--accounts", "7", "--experiment",
                     "erc20_basic_auth_payments", "--txs-per-block", "4000",
                     "--batch-sizes", "2,3", "--warmup-blocks", "1",
                     "--timed-blocks", "14", "--connections-per-exec", "4",
                     "--coros-per-connection", "8", "--uniquifier-layout",
                     "global", "--json-out", "/tmp/t.jsonl"});
  EXPECT_EQ(o.roles_path, "r.json");
  EXPECT_EQ(o.accounts, 7u);
  EXPECT_EQ(o.experiment, "erc20_basic_auth_payments");
  EXPECT_EQ(o.txs_per_block, 4000u);
  EXPECT_EQ(o.batch_sizes, (std::vector<uint32_t>{2, 3}));
  EXPECT_EQ(o.warmup_blocks, 1u);
  EXPECT_EQ(o.timed_blocks, 14u);
  EXPECT_EQ(o.connections_per_exec, 4u);
  EXPECT_EQ(o.coros_per_connection, 8u);
  EXPECT_TRUE(o.uniquifier_layout == UniquifierLayout::kGlobal);
  EXPECT_EQ(o.json_out, "/tmp/t.jsonl");
}

TEST(ParseOptions, BadFlagsThrow)
{
  expect_throw_with([] { parse({"--roles", "r.json", "--nope", "1"}); },
                    "--nope");
  expect_throw_with([] { parse({"--roles", "r.json", "--accounts"}); },
                    "--accounts");
  expect_throw_with([] { parse({"--roles", "r.json", "--accounts", "x"}); },
                    "--accounts");
  expect_throw_with(
      [] { parse({"--roles", "r.json", "--accounts", "99999999999"}); },
      "--accounts");
  expect_throw_with(
      [] { parse({"--roles", "r.json", "--batch-sizes", "1,,5"}); },
      "--batch-sizes");
  expect_throw_with(
      [] { parse({"--roles", "r.json", "--batch-sizes", "0"}); },
      "--batch-sizes");
  expect_throw_with(
      [] { parse({"--roles", "r.json", "--uniquifier-layout", "sender"}); },
      "--uniquifier-layout must be per-sender or global, got: sender");
  expect_throw_with([] { parse({}); }, "--roles");
}

TEST(ParseOptions, Help)
{
  EXPECT_TRUE(parse({"--help"}).help);
  EXPECT_TRUE(parse({"-h"}).help);
  EXPECT_TRUE(parse({"--roles", "r.json", "--help"}).help);
  EXPECT_FALSE(parse({"--roles", "r.json"}).help);
}

TEST(Usage, NamesEveryFlag)
{
  const std::string u = usage_text("whitney-runner");
  EXPECT_TRUE(u.starts_with("Usage: whitney-runner --roles <path>")) << u;
  for (const char *flag :
       {"--roles", "--accounts", "--experiment", "--txs-per-block",
        "--batch-sizes", "--warmup-blocks", "--timed-blocks",
        "--connections-per-exec", "--coros-per-connection",
        "--uniquifier-layout", "--json-out", "--help"}) {
    EXPECT_NE(u.find(flag), std::string::npos) << flag;
  }
}

TEST(Validate, AcceptsDefaults)
{
  EXPECT_NO_THROW(validate(good_options(), good_roles()));
}

// The replay-cache budget depends on the generated work txs, so the runner
// checks it before the first work block (check_replay_budget), not here.
// The old 15-block cap was that budget for 1M-tx blocks in the global layout.
TEST(Validate, NoBlockCapOrReplayBudget)
{
  Options o = good_options();
  o.warmup_blocks = 1;
  o.timed_blocks = 99;
  EXPECT_NO_THROW(validate(o, good_roles()));
  o.txs_per_block = 4'294'967'295u;
  EXPECT_NO_THROW(validate(o, good_roles()));
}

TEST(Validate, RejectsRepeatedBatchSizes)
{
  Options o = good_options();
  o.batch_sizes = {5, 1, 5};
  expect_throw_with([&] { validate(o, good_roles()); },
                    "batch_sizes must not repeat: 5");
  o.batch_sizes = {10, 1, 10, 1};
  expect_throw_with([&] { validate(o, good_roles()); },
                    "batch_sizes must not repeat: 1, 10");
  o.batch_sizes = {1, 5, 10};
  EXPECT_NO_THROW(validate(o, good_roles()));
}

TEST(Validate, RejectsSmallBlocks)
{
  Options o = good_options();
  o.txs_per_block = 1000;
  expect_throw_with([&] { validate(o, good_roles()); },
                    "txs_per_block must be >= 4000");
}

TEST(Validate, RejectsRelativeBinDir)
{
  Roles r = good_roles();
  r.bin_dir = "opt/whitney/bin";
  expect_throw_with([&] { validate(good_options(), r); }, "bin_dir");
}

TEST(Validate, RejectsNoExecs)
{
  Roles r = good_roles();
  r.execs.clear();
  expect_throw_with([&] { validate(good_options(), r); }, "execs");
}

TEST(Validate, RejectsUnknownExperiment)
{
  Options o = good_options();
  o.experiment = "erc721";
  expect_throw_with([&] { validate(o, good_roles()); },
                    "experiment must be one of erc20_payments");
}

TEST(Validate, RejectsBasicAuthExperiment)
{
  Options o = good_options();
  o.experiment = "erc20_basic_auth_payments";
  expect_throw_with([&] { validate(o, good_roles()); },
                    "experiment erc20_basic_auth_payments is not supported: its "
                    "signatures cover the uniquifier the runner rewrites");
}

TEST(Validate, MetricsPortsDistinctPerHost)
{
  // Different hosts may share a metrics port (each exec host uses 9104).
  Roles r = good_roles();
  EXPECT_NO_THROW(validate(good_options(), r));

  // One host: every metrics port must differ from the others on it.
  r.db.host = r.event.host;
  r.db.metrics_port = r.event.metrics_port;
  expect_throw_with([&] { validate(good_options(), r); },
                    "metrics_port 9101 is used by both event and db on host "
                    "10.0.1.11");

  // And from the control ports on it.
  r = good_roles();
  r.execs[1].host = r.execs[0].host;
  r.execs[1].metrics_port = r.execs[0].control_port;
  expect_throw_with([&] { validate(good_options(), r); },
                    "metrics_port 11114 is used by both exec0 (control_port) "
                    "and exec1 on host 10.0.1.14");
}

TEST(MetricsPort, AppendedToDaemonArgs)
{
  RoleAddr role = good_roles().db;
  std::vector<std::string> args = {"--bind-ip", "10.0.1.12"};
  EXPECT_EQ(with_metrics_port(args, role),
            (std::vector<std::string>{"--bind-ip", "10.0.1.12",
                                      "--metrics-port", "9102"}));
}

TEST(Validate, RejectsZeroCounts)
{
  Options o = good_options();
  o.accounts = 0;
  expect_throw_with([&] { validate(o, good_roles()); },
                    "accounts must be >= 2");
  o.accounts = 1;
  expect_throw_with([&] { validate(o, good_roles()); },
                    "accounts must be >= 2");
  o.accounts = 2;
  EXPECT_NO_THROW(validate(o, good_roles()));
  o = good_options();
  o.connections_per_exec = 0;
  expect_throw_with([&] { validate(o, good_roles()); },
                    "connections_per_exec must be >= 1");
  o = good_options();
  o.coros_per_connection = 0;
  expect_throw_with([&] { validate(o, good_roles()); },
                    "coros_per_connection must be >= 1");
}

} // namespace
} // namespace whitney
