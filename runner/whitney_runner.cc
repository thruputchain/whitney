// Forked from hogd: hogd/hog_exec/bench/multiexec_runner.cc at tag
// v0.1.31-rc.13 (commit 0deea30c7cbf70de72308840466b61966ec424f3).
// Whitney builds it as whitney-runner through hogd's HOGD_EXTRA_BENCH_DIRS.
//
// The block execution code (BlockValidationQueue, connection_coro,
// run_block) is the fork's. What differs from multiexec_runner:
//   - the work txs come from loadgen calls of at most kLoadgenChunkTxs
//     each (one call of 8M txs crashes the loadgen);
//   - blocks open the event block first, then the executors, and check
//     every executor's open result (open_block_event_first);
//   - run_block takes the nominations buffer from its caller. The work
//     blocks share one buffer, allocated before the first work block, so
//     the timed blocks do not allocate, zero and free one entry per tx;
//   - role addresses, work dirs and CPU affinity come from roles.json, not
//     from host names (RoleHandle, WhitneyCluster);
//   - the mock scheduler servers bind to roles.runner_bind_ip;
//   - daemon paths are absolute, under roles.bin_dir;
//   - each batch-size run first stops, waits for and clears every role;
//   - the indexer and its latency probe run only when roles.indexer is set;
//   - every daemon gets --metrics-port from roles.json;
//   - every knob is a flag, and each work block (warm-up included) is
//     written as one JSON line to --json-out, with its phase times and the
//     runner's own CPU time;
//   - the work txs get per-sender uniquifiers by default
//     (uniquifier_layout.h); --uniquifier-layout global keeps the original
//     layout.

#include <algorithm>
#include <atomic>
#include <cassert>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <exception>
#include <filesystem>
#include <format>
#include <fstream>
#include <iterator>
#include <memory>
#include <print>
#include <span>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include "ci_server.hh"
#include "consensus.hh"
#include "exec_result.hh"
#include "exec_sched.hh"
#include "hogd/base/coropoll.h"
#include "hogd/base/log.h"
#include "hogd/base/rpc_server.h"
#include "hogd/base/types.h"
#include "hogd/base/xdr_util.h"
#include "hogd/crypto/crypto.h"
#include "hogd/hog_exec/bench/common_bench_state.h"
#include "hogd/hog_exec/bench/control_client.h"
#include "hogd/hog_exec/bench/indexer_latency_probe.h"
#include "hogd/hog_exec/bench/mock_scheduler.h"
#include "hogd/hog_exec/bench/multiexec_cluster.h"
#include "hogd/init.h"
#include "hogd/loadgen/loadgen_server.h"
#include "loadgen.hh"
#include "rpc_types.hh"
#include "run_record.h"
#include "runner_options.h"
#include "transaction.hh"
#include "uniquifier_layout.h"
#include "x23/async.h"
#include "x23/fastclock.h"
#include "x23/vec.h"

namespace hogd {

namespace {

// Work txs per loadgen extend_experiment call.
constexpr size_t kLoadgenChunkTxs = 2'000'000;

struct BlockValidationQueue {
  struct Batch {
    size_t start;
    x23::span<const SignedTransaction> txs;
  };

  BlockValidationQueue(const x23::XdrVec<SignedTransaction> *txs,
                       size_t batch_size)
    : txs_(txs->data(), txs->size()), batch_size(batch_size)
  {}

  void reset_offset() { offset.store(0); }

  const std::span<const SignedTransaction> txs_;
  const size_t batch_size;
  std::atomic<uint64_t> offset{0};

  Batch next_batch()
  {
    auto start = offset.fetch_add(batch_size);
    const size_t bounded_start = std::min<size_t>(start, txs_.size());
    return {.start = bounded_start,
            .txs = {txs_.begin() + bounded_start,
                    txs_.begin() +
                        std::min<size_t>(start + batch_size, txs_.size())}};
  }
};

x23::Detached
connection_coro(exec_client_t &exec_client, BlockValidationQueue *queue,
                x23::CoroCB<void> &cb, int ncoro, BlockID block_number,
                std::atomic<size_t> &n_total_coros,
                x23::XdrVec<NondeterministicProposalData> *nominations)
{
  auto coro = [](exec_client_t &exec_client, x23::CoroCB<void> &cb,
                 BlockValidationQueue *queue, BlockID block_number,
                 std::atomic<size_t> &n_total_coros,
                 x23::XdrVec<NondeterministicProposalData> *nominations)
      -> x23::Detached {
    co_await default_threadpool().bg();
    while (true) {
      auto batch = queue->next_batch();
      if (batch.txs.empty()) {
        if (n_total_coros.fetch_sub(1, std::memory_order_relaxed) == 1) {
          cb();
        }
        co_return;
      }

      auto r = co_await exec_client.executorsched_propose_batch(block_number,
                                                                batch.txs);
      assert(r.size() == batch.txs.size());
      for (size_t i = 0; i < r.size(); ++i) {
        const ExecProposeResult &result = r[i];
        if (result.result() != ExecResult::ExecResultOK) {
          panic("Failed to propose batch");
        }
        (*nominations)[batch.start + i] = result.proposal_data();
      }
    }
  };

  for (int i = 0; i < ncoro; i++) {
    coro(exec_client, cb, queue, block_number, n_total_coros, nominations);
  }
  co_return;
};

// Wall time of one block's phases: open_block, the propose batches, and
// close_block (with the event commit).
struct BlockPhases {
  uint64_t open_ns;
  uint64_t exec_ns;
  uint64_t close_ns;
};

// CPU time of this process (all threads, user and system) so far.
uint64_t
process_cpu_ns()
{
  timespec ts{};
  if (clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &ts) != 0) {
    panic("clock_gettime(CLOCK_PROCESS_CPUTIME_ID) failed");
  }
  return uint64_t(ts.tv_sec) * 1'000'000'000 + uint64_t(ts.tv_nsec);
}

// Opens a proposal block in the order hog_mempool's scheduler uses: the event
// block and the db snapshot first, then every executor, checking each
// result. hogd's bench open_block (multiexec_cluster.h) sends the executor
// opens before the event open and drops their results. An executor whose
// notify reaches hog-event first fails its open (kRPCBadBlockID) and then
// rejects the block's batches; with 64 executors that happens within a few
// blocks.
void
open_block_event_first(bench_db_sched_client_t &db_sched_client,
                       bench_event_sched_client_t &event_sched_client,
                       std::vector<exec_client_t> &exec_clients,
                       uint64_t parent_block_number, uint64_t block_number,
                       BlockHeight block_height)
{
  info("opening block {}", block_number);
  auto start_time = x23::fastclock::now();

  auto event_async = event_sched_client.eventsched_open_block(block_number);
  auto issue_time = x23::fastclock::now();
  auto res = db_sched_client.dbsched_create_snapshot(
      parent_block_number, block_number,
      BlockExecutionMode::kBlockExecutionModeProposal);
  if (res != DbSchedResult::DbSchedResultOK) {
    panic("Failed to create snapshot");
  }
  auto create_time = x23::fastclock::now();
  if (event_async() != RPCError::kRPCNone) {
    panic("Failed to open event block {}", block_number);
  }

  auto [async, cb] = x23::make_async<void>();
  std::atomic<size_t> n_remaining_execs = exec_clients.size();
  std::atomic<size_t> n_failed_execs = 0;
  for (auto &exec_client : exec_clients) {
    [](auto &exec_client, uint64_t block_number, BlockHeight block_height,
       std::atomic<size_t> &n_remaining_execs,
       std::atomic<size_t> &n_failed_execs, auto &cb) -> x23::Detached {
      const ExecResult r =
          co_await exec_client.executorsched_open_proposal_block(
              block_number, test_block_execution(block_height));
      if (r != ExecResult::ExecResultOK) {
        n_failed_execs.fetch_add(1, std::memory_order_relaxed);
      }
      if (n_remaining_execs.fetch_sub(1, std::memory_order_relaxed) == 1) {
        cb();
      }
    }(exec_client, block_number, block_height, n_remaining_execs,
      n_failed_execs, cb);
  }
  async();
  if (const size_t failed = n_failed_execs.load(); failed != 0) {
    panic("{} of {} executors failed to open block {}", failed,
          exec_clients.size(), block_number);
  }
  auto done_time = x23::fastclock::now();

  std::println("opening: issue_time {} create_time {} done_time {}",
               (issue_time - start_time).count(),
               (create_time - issue_time).count(),
               (done_time - create_time).count());
}

BlockPhases
run_block(x23::XdrVec<SignedTransaction> const *txs,
          bench_db_sched_client_t &db_sched_client,
          bench_event_sched_client_t &event_sched_client,
          std::vector<exec_client_t> &exec_clients,
          size_t n_coros_per_connection, size_t n_connections,
          size_t batch_size, uint64_t parent_block_number,
          uint64_t current_block_number, ConsensusValue &prev_value,
          IndexerLatencyProbe *indexer_probe,
          x23::XdrVec<NondeterministicProposalData> *nominations)
{
  CHECK_EQ(nominations->size(), txs->size());
  auto start_time = x23::fastclock::now();

  std::atomic<size_t> n_total_coros = n_coros_per_connection * n_connections;
  // Height comes off the previous committed value, never off a block number.
  open_block_event_first(db_sched_client, event_sched_client, exec_clients,
                         parent_block_number, current_block_number,
                         prev_value.proposal_header.block_height + 1);

  BlockValidationQueue block_validation_queue(txs, batch_size);

  auto start_coros_time = x23::fastclock::now();

  // make sure any destructors are run during the measured inner loop
  {
    auto [async, cb] = x23::make_async<void>();
    for (auto &exec_client : exec_clients) {
      connection_coro(exec_client, &block_validation_queue, cb,
                      n_coros_per_connection, current_block_number,
                      n_total_coros, nominations);
    }
    async();
  }

  auto end_coros_time = x23::fastclock::now();
  double elapsed_time = (end_coros_time - start_coros_time).count();
  auto inner_tps = txs->size() * 1'000'000'000.0 / elapsed_time;
  close_block(db_sched_client, event_sched_client, exec_clients,
              current_block_number, prev_value, indexer_probe);

  auto end_block_time = x23::fastclock::now();
  const BlockPhases phases{
      .open_ns = uint64_t((start_coros_time - start_time).count()),
      .exec_ns = uint64_t((end_coros_time - start_coros_time).count()),
      .close_ns = uint64_t((end_block_time - end_coros_time).count()),
  };
  std::println("open_time {} middle_time {} close_time {} inner tps: {}",
               phases.open_ns, phases.exec_ns, phases.close_ns, inner_tps);
  return phases;
}

// RoleHandle is one role's control_server, reached at the role's host and
// control port. Like hogd's ControlServerHandle, destruction stops whatever
// the server runs. Each step prints "role=<name> <step>" on stdout, so a log
// shows the order of stop, wait, clear and start.
class RoleHandle {
public:
  RoleHandle(std::string name, const whitney::RoleAddr &addr)
    : name_(std::move(name)), addr_(addr),
      ci_client_(connect_to_control_server(addr.host,
                                           std::to_string(addr.control_port)))
  {}

  RoleHandle(const RoleHandle &) = delete;
  RoleHandle &operator=(const RoleHandle &) = delete;
  RoleHandle(RoleHandle &&) = delete;
  RoleHandle &operator=(RoleHandle &&) = delete;

  ~RoleHandle()
  {
    std::println("role={} teardown", name_);
    try {
      ci_client_.stop_server();
    } catch (...) {
      panic("Failed to stop control server");
    }
  }

  // reset leaves the role with no running child and an empty work dir. A
  // runner that panicked skipped its destructors, so its daemons may still
  // be up; they are stopped here before their work dir is cleared.
  void reset()
  {
    check_work_dir();

    std::println("role={} stop", name_);
    ci_client_.stop_server();

    // The same poll as `control_cli <host> <port> wait`: query_proc_status
    // until it stops reporting a running child. After stop_server it reports
    // Internal (no child at all); OK means the last child has exited.
    std::println("role={} wait", name_);
    while (true) {
      CIProcStatus status = ci_client_.query_proc_status();
      CIServerErrorCode code = status.error_code();
      if (code == CIServerErrorCode::kCIServerErrorCodeAlreadyRunning) {
        std::this_thread::sleep_for(std::chrono::milliseconds(250));
        continue;
      }
      if (code == CIServerErrorCode::kCIServerErrorCodeOK ||
          code == CIServerErrorCode::kCIServerErrorCodeInternal) {
        break;
      }
      panic("role {}: query_proc_status failed: {}", name_, code);
    }

    std::println("role={} clear", name_);
    if (ci_client_.clear_work_dir() !=
        CIServerErrorCode::kCIServerErrorCodeOK) {
      panic("role {}: failed to clear work dir", name_);
    }
  }

  // start runs program with args plus --metrics-port from roles.json, so
  // every daemon serves /metrics on its --bind-ip at a fixed port.
  void start(const std::string &program, const std::vector<std::string> &args)
  {
    std::println("role={} start", name_);
    ci_client_.start_server(program, whitney::with_metrics_port(args, addr_),
                            addr_.cpu_affinity);
  }

  const whitney::RoleAddr &addr() const { return addr_; }

private:
  // clear_work_dir clears the control_server's own -w dir, so roles.json
  // must name that same dir, or the daemon's data would survive the clear.
  void check_work_dir()
  {
    CIWorkDirStatus status = ci_client_.query_work_dir();
    if (status.error_code() != CIServerErrorCode::kCIServerErrorCodeOK) {
      panic("role {}: failed to query work dir: {}", name_,
            status.error_code());
    }
    const std::string server_dir(status.work_dir().begin(),
                                 status.work_dir().end());
    if (dir_key(server_dir) != dir_key(addr_.work_dir)) {
      panic("role {}: control_server work dir {} != roles.json work_dir {}",
            name_, server_dir, addr_.work_dir);
    }
  }

  // dir_key normalizes a dir path for comparison: "/a/./b/" is "/a/b".
  static std::filesystem::path dir_key(const std::string &dir)
  {
    std::filesystem::path p = std::filesystem::path(dir).lexically_normal();
    if (!p.has_filename() && p != p.root_path()) {
      p = p.parent_path();
    }
    return p;
  }

  std::string name_;
  whitney::RoleAddr addr_;
  ControlClientWrapper ci_client_;
};

// WhitneyCluster is hogd's MultiexecCluster with role addresses from
// roles.json. It resets every role, then starts hog-event, hog-db,
// hog-indexer (when roles.indexer is set) and one hog-exec per exec role,
// and returns once every executor reports ready. Destruction stops them.
class WhitneyCluster {
public:
  explicit WhitneyCluster(const whitney::Roles &roles)
  {
    const auto add = [this](std::string name, const whitney::RoleAddr &addr) {
      handles_.push_back(std::make_unique<RoleHandle>(std::move(name), addr));
      return handles_.back().get();
    };
    RoleHandle *event = add("event", roles.event);
    RoleHandle *db = add("db", roles.db);
    RoleHandle *indexer =
        roles.indexer ? add("indexer", *roles.indexer) : nullptr;
    std::vector<RoleHandle *> execs;
    for (size_t i = 0; i < roles.execs.size(); i++) {
      execs.push_back(add(std::format("exec{}", i), roles.execs[i]));
    }

    for (auto &handle : handles_) {
      handle->reset();
    }

    // Port 0: each mock scheduler server gets an ephemeral port.
    const x23::HostPort sched_bind{roles.runner_bind_ip, uint16_t{0}};
    const uint16_t sched_exec_port = sched_exec_test_.start_server(sched_bind);
    const uint16_t sched_db_port = sched_db_test_.start_server(sched_bind);
    const uint16_t sched_event_port =
        sched_event_test_.start_server(sched_bind);
    const auto sched_addr = [&](uint16_t port) {
      return std::format("{}:{}", roles.runner_bind_ip, port);
    };
    const auto program = [&](std::string_view name) {
      return std::format("{}/{}", roles.bin_dir, name);
    };

    event->start(program("hog-event"),
                 {"--bind-ip", roles.event.host, "--mempool-addr",
                  sched_addr(sched_event_port), "--work-dir",
                  roles.event.work_dir, "--indexer-port",
                  std::to_string(kBenchEventIndexerPort)});

    EventCard event_card = sched_event_test_->impl()->await_event_card();
    sched_exec_test_->impl()->register_event_card(event_card);

    db->start(program("hog-db"),
              {"--db-path", roles.db.work_dir, "--ram-bytes", "10000000000",
               "--bind-ip", roles.db.host, "--coordinator-addr",
               sched_addr(sched_db_port)});

    // Started before the executors so it is already draining the outbox by
    // the time the first block commits, which is the steady state the latency
    // number is supposed to describe.
    if (indexer != nullptr) {
      indexer_block_explorer_addr_ =
          std::format("{}:{}", indexer->addr().host,
                      kBenchIndexerBlockExplorerPort);
      indexer->start(
          program("hog-indexer"),
          {"--bind-ip", indexer->addr().host, "--event-addr",
           std::format("{}:{}", roles.event.host, kBenchEventIndexerPort),
           "--db-path", indexer->addr().work_dir, "--block-explorer-port",
           std::to_string(kBenchIndexerBlockExplorerPort)});
    }

    for (RoleHandle *exec : execs) {
      exec->start(program("hog-exec"),
                  {"--bind-ip", exec->addr().host, "--coordinator-addr",
                   sched_addr(sched_exec_port)});
    }

    auto db_cards = sched_db_test_->impl()->await_shard_cards(1);
    sched_exec_test_->impl()->register_shard_cards(db_cards);
    exec_cards_ = sched_exec_test_->impl()->await_executor_cards(execs.size());

    event_sched_client_ = std::make_unique<bench_event_sched_client_t>(
        connect_to_event_sched_server(event_card.address_for_sched));
    assert(db_cards.size() == 1);
    db_sched_client_ = std::make_unique<bench_db_sched_client_t>(
        connect_to_db_server(db_cards[0]));

    auto await_ready = [&](const ExecCard &exec_card) {
      auto exec_client = connect_to_exec_server(exec_card);
      while (true) {
        auto res = exec_client.executorsched_ready()();
        if (res != ExecResult::ExecResultOK) {
          warn_every_n_sec(1.0, "Executor is not ready, retrying...");
          std::this_thread::sleep_for(std::chrono::milliseconds(100));
          continue;
        }
        break;
      }
    };
    for (auto &exec_card : exec_cards_) {
      await_ready(exec_card);
    }
  }

  WhitneyCluster(const WhitneyCluster &) = delete;
  WhitneyCluster &operator=(const WhitneyCluster &) = delete;
  WhitneyCluster(WhitneyCluster &&) = delete;
  WhitneyCluster &operator=(WhitneyCluster &&) = delete;
  ~WhitneyCluster() = default;

  void
  write_genesis_configuration(const GenesisConfiguration &genesis_configuration)
  {
    ConsensusValue genesis_value{};
    genesis_value.proposal_header.block_height = 0;
    genesis_value.proposal_header.config_hash =
        compute_config_hash(genesis_configuration);
    auto res = db_sched_client_->dbsched_load_genesis_configuration(
        genesis_configuration, genesis_value);
    if (res != DbSchedResult::DbSchedResultOK) {
      panic("Failed to write genesis block");
    }
  }

  // [n_connections_per_executor] connections to each executor, interleaved
  // so that consecutive connections go to different executors.
  std::vector<exec_client_t>
  connect_exec_clients(size_t n_connections_per_executor) const
  {
    const size_t n_connections =
        n_connections_per_executor * exec_cards_.size();
    std::vector<exec_client_t> exec_clients;
    exec_clients.reserve(n_connections);
    for (size_t i = 0; i < n_connections; i++) {
      exec_clients.push_back(
          connect_to_exec_server(exec_cards_[i % exec_cards_.size()]));
    }
    return exec_clients;
  }

  size_t num_executors() const { return exec_cards_.size(); }

  // Empty when roles.json has no indexer.
  const std::string &indexer_block_explorer_addr() const
  {
    return indexer_block_explorer_addr_;
  }

  bench_db_sched_client_t &db_sched_client() { return *db_sched_client_; }
  bench_event_sched_client_t &event_sched_client()
  {
    return *event_sched_client_;
  }

private:
  // Declared first so it is destroyed last: stopping the servers comes after
  // everything that talks to them has gone away.
  std::vector<std::unique_ptr<RoleHandle>> handles_;

  RpcServer<SchedExecTest> sched_exec_test_;
  RpcServer<MockSchedDB> sched_db_test_;
  RpcServer<MockSchedEvent> sched_event_test_;

  std::string indexer_block_explorer_addr_;
  std::vector<ExecCard> exec_cards_;

  std::unique_ptr<bench_event_sched_client_t> event_sched_client_;
  std::unique_ptr<bench_db_sched_client_t> db_sched_client_;
};

ExperimentType
experiment_type(const std::string &name)
{
  if (name != "erc20_payments") {
    panic("unknown experiment: {}", name); // validate() rejects it first
  }
  return ExperimentType::kExperimentTypeERC20Payments;
}

// One batch-size run: fresh daemons and genesis, then warmup_blocks +
// timed_blocks work blocks. Each block is written to [json_out] (when not
// null) as one trial_json line.
void
run_multiexec_bench(const whitney::Roles &roles, const whitney::Options &opts,
                    unsigned int batch_size, std::ofstream *json_out)
{
  const unsigned int num_accounts = opts.accounts;
  const unsigned int n_connections_per_executor = opts.connections_per_exec;
  const unsigned int n_coros_per_connection = opts.coros_per_connection;
  const unsigned int num_txs_per_block = opts.txs_per_block;

  WhitneyCluster cluster(roles);

  std::unique_ptr<IndexerLatencyProbe> indexer_probe;
  if (roles.indexer) {
    indexer_probe = std::make_unique<IndexerLatencyProbe>(
        cluster.indexer_block_explorer_addr());
  }

  auto &event_sched_client = cluster.event_sched_client();
  auto &db_sched_client = cluster.db_sched_client();

  uint64_t parent_block_number = 0;
  uint64_t current_block_number = 1;
  ConsensusValue prev_value = MockSchedEvent::bench_genesis_tip();

  ExperimentConfig config(experiment_type(opts.experiment));
  config.erc20_payments() = {
      .num_addresses = num_accounts,
      .network_hash = {},
  };

  LoadgenRpc loadgen;
  auto loadgen_result = loadgen.generate_experiment(config)();
  if (loadgen_result.error() != LoadgenRPCError::kLoadgenRPCErrorOK) {
    panic("Failed to generate loadgen experiment");
  }
  auto genesis_configuration = loadgen_result.success().genesis_configuration;
  cluster.write_genesis_configuration(genesis_configuration);

  const size_t n_connections =
      n_connections_per_executor * cluster.num_executors();
  std::vector<exec_client_t> coro_exec_clients =
      cluster.connect_exec_clients(n_connections_per_executor);

  {
    const auto &genesis_txs = loadgen_result.success().genesis_ceremony_txs;
    x23::XdrVec<NondeterministicProposalData> nominations(genesis_txs.size());
    run_block(&genesis_txs, db_sched_client, event_sched_client,
              coro_exec_clients, n_coros_per_connection, n_connections,
              batch_size, parent_block_number, current_block_number,
              prev_value, indexer_probe.get(), &nominations);
  }
  parent_block_number = current_block_number;
  current_block_number++;

  for (auto const &init_block : loadgen_result.success().initialization_txs) {
    x23::XdrVec<NondeterministicProposalData> nominations(init_block.size());
    run_block(&init_block, db_sched_client, event_sched_client,
              coro_exec_clients, n_coros_per_connection, n_connections,
              batch_size, parent_block_number, current_block_number, prev_value,
              indexer_probe.get(), &nominations);
    parent_block_number = current_block_number;
    current_block_number++;
  }

  info("done initialization txs");

  // The loadgen returns one call's txs as one ParallelBatch, and a call of
  // 8M txs crashes while it builds that batch (4M works). Big blocks get
  // their work txs from several calls of at most kLoadgenChunkTxs each.
  SignedTransactionBatch work_txs;
  work_txs.reserve(num_txs_per_block);
  for (size_t done = 0; done < num_txs_per_block;) {
    const size_t n =
        std::min<size_t>(kLoadgenChunkTxs, num_txs_per_block - done);
    auto work_txs_raw = loadgen.extend_experiment(
        loadgen_result.success().context_id, n, 1000000)();
    if (work_txs_raw.error() != LoadgenRPCError::kLoadgenRPCErrorOK) {
      panic("Failed to extend loadgen experiment");
    }
    SignedTransactionBatch chunk;
    parallel_batch_to_t(work_txs_raw.success().work_txs, chunk,
                        kNoBatchSizeLimit)();
    CHECK_EQ(chunk.size(), n);
    work_txs.append(std::make_move_iterator(chunk.begin()),
                    std::make_move_iterator(chunk.end()));
    done += n;
  }
  CHECK_EQ(work_txs.size(), num_txs_per_block);
  // Identifies the generated workload; taken before the uniquifiers below
  // are rewritten per block.
  const std::string work_tx_hash = whitney::hash_work_txs(work_txs);

  // The same work txs are replayed across every trial, so each must carry a
  // fresh uniquifier (HogDb now rejects a re-consumed one). The signature
  // covers only the inner calldata, not the uniquifier, so we can rewrite it in
  // place. hog-db keys its replay cache by the tx's root invoked address, the
  // sender's wallet, so the per-sender layout groups by that address.
  // uniquifier_layout.h has both layouts and the caps they keep under.
  whitney::SenderIds<ContractAddress, SipHash24Hasher> senders;
  std::vector<uint32_t> sender_ids;
  sender_ids.reserve(work_txs.size());
  for (const SignedTransaction &stx : work_txs) {
    sender_ids.push_back(senders.id(stx.tx.invocation.invoked_address));
  }
  const whitney::UniquifierPlan plan(opts.uniquifier_layout, sender_ids);
  // Global: block i's lowest expiry height is work_base + floor(i * n / 4000)
  // and it commits at work_base + i, so n >= 4000 (validate() checks it).
  if (opts.uniquifier_layout == whitney::UniquifierLayout::kGlobal) {
    CHECK_GE(work_txs.size(), whitney::kGlobalNoncesPerHeight);
  }
  const BlockID work_base = current_block_number;
  const size_t n_blocks = size_t{opts.warmup_blocks} + opts.timed_blocks;
  // Exact for this batch, before any work block: a sender over hog-db's live
  // height cap would fail its txs mid-run.
  whitney::check_replay_budget(opts.uniquifier_layout,
                               plan.heights_per_block(), n_blocks);
  std::println("uniquifier_layout={} senders={} heights_per_block={}",
               whitney::layout_name(opts.uniquifier_layout), senders.size(),
               plan.heights_per_block());
  // One buffer for all work blocks: each block overwrites every entry.
  x23::XdrVec<NondeterministicProposalData> work_nominations(work_txs.size());
  for (size_t i = 0; i < n_blocks; i++) {
    for (size_t j = 0; j < work_txs.size(); j++) {
      const whitney::Uniq u = plan.at(work_base, i, j);
      work_txs[j].tx.uniquifier = Uniquifier{
          .block_expiry_height = static_cast<BlockHeight>(u.expiry_height),
          .nonce = u.nonce};
    }
    std::println("Start trial {} with txs={} nexecutors={} nconnections={} "
                 "ncoros_per_connection={} nacc={} "
                 "batch_size={}",
                 i, num_txs_per_block, cluster.num_executors(), n_connections,
                 n_coros_per_connection, num_accounts, batch_size);
    const uint64_t cpu_start = process_cpu_ns();
    auto ts = x23::fastclock::now();
    const BlockPhases phases = run_block(
        &work_txs, db_sched_client, event_sched_client, coro_exec_clients,
        n_coros_per_connection, n_connections, batch_size, parent_block_number,
        current_block_number, prev_value, indexer_probe.get(),
        &work_nominations);
    auto end_time = x23::fastclock::now();
    const uint64_t runner_cpu_ns = process_cpu_ns() - cpu_start;
    double elapsed_time = (end_time - ts).count();
    auto tps = num_txs_per_block * 1'000'000'000.0 / elapsed_time;
    std::println("time taken: {} ns for txs={} nexecutors={} nconnections={} "
                 "ncoros_per_connection={} nacc={} "
                 "batch_size={} tps={}",
                 elapsed_time, num_txs_per_block, cluster.num_executors(),
                 n_connections, n_coros_per_connection, num_accounts,
                 batch_size, tps);
    info("time taken: {} ns for txs={} nexecutors={} nconnections={} "
         "ncoros_per_connection={} nacc={} "
         "batch_size={} tps={}",
         elapsed_time, num_txs_per_block, cluster.num_executors(),
         n_connections, n_coros_per_connection, num_accounts, batch_size, tps);
    if (json_out != nullptr) {
      const whitney::TrialRecord record{
          .nexec = static_cast<uint32_t>(cluster.num_executors()),
          .batch_size = batch_size,
          .block_index = static_cast<uint32_t>(i),
          .warmup = i < opts.warmup_blocks,
          .txs = num_txs_per_block,
          .elapsed_ns = static_cast<uint64_t>(elapsed_time),
          .tps = tps,
          .work_tx_hash = work_tx_hash,
          .accounts = num_accounts,
          .experiment = opts.experiment,
          .open_ns = phases.open_ns,
          .exec_ns = phases.exec_ns,
          .close_ns = phases.close_ns,
          .runner_cpu_ns = runner_cpu_ns,
      };
      *json_out << whitney::trial_json(record) << '\n';
      json_out->flush();
      if (!*json_out) {
        panic("failed to write --json-out {}", opts.json_out);
      }
    }
    parent_block_number = current_block_number;
    current_block_number++;
  }

  if (indexer_probe) {
    // Every block of this run has committed; let the indexer finish draining
    // before reporting. The trials commit far faster than the indexer
    // ingests, so the backlog standing here outlives the run by many seconds
    // and the drain bounds the indexer going quiet rather than the total
    // wait. A stall is reported as [unobserved] rather than silently
    // shrinking the sample.
    if (!indexer_probe->drain()) {
      warn("indexer latency probe: indexer stopped draining committed heights");
    }
    indexer_probe->stop();
    indexer_probe->report(
        std::format("nexec={},batch={}", cluster.num_executors(), batch_size));
  }
}

} // namespace

} // namespace hogd

int
main(int argc, char *argv[]) // NOLINT(bugprone-exception-escape)
{
  // Line-buffer stdout before any output. Over ssh or into a file, stdout is
  // not a tty and would be fully buffered, so a panic() or a timeout kill
  // would lose the role=... and trial lines that show how far the run got.
  std::setvbuf(stdout, nullptr, _IOLBF, 0);

  whitney::Options opts;
  try {
    opts = whitney::parse_options(argc, argv);
  } catch (const std::invalid_argument &e) {
    std::println(stderr, "{}: {}", argv[0], e.what());
    std::print(stderr, "{}", whitney::usage_text(argv[0]));
    return EXIT_FAILURE;
  }
  if (opts.help) {
    std::print("{}", whitney::usage_text(argv[0]));
    return EXIT_SUCCESS;
  }

  auto ret = hogd::hogd_init(argc, argv);
  if (ret)
    return ret;

  whitney::Roles roles;
  try {
    std::ifstream in(opts.roles_path);
    if (!in) {
      throw std::invalid_argument(
          std::format("cannot read roles file: {}", opts.roles_path));
    }
    std::ostringstream text;
    text << in.rdbuf();
    roles = whitney::parse_roles_json(text.str());
    whitney::validate(opts, roles);
  } catch (const std::invalid_argument &e) {
    std::println(stderr, "{}: {}", argv[0], e.what());
    return EXIT_FAILURE;
  }

  std::ofstream json_out;
  if (!opts.json_out.empty()) {
    json_out.open(opts.json_out, std::ios::out | std::ios::trunc);
    if (!json_out) {
      std::println(stderr, "{}: cannot write --json-out {}", argv[0],
                   opts.json_out);
      return EXIT_FAILURE;
    }
  }

  try {
    for (uint32_t batch_size : opts.batch_sizes) {
      hogd::run_multiexec_bench(roles, opts, batch_size,
                                opts.json_out.empty() ? nullptr : &json_out);
    }
  } catch (const whitney::ReplayBudgetError &e) {
    // From check_replay_budget, before the first work block. The cluster has
    // been stopped on the way out.
    std::println(stderr, "{}: {}", argv[0], e.what());
    return EXIT_FAILURE;
  }

  std::fflush(stdout);
  return 0;
}
