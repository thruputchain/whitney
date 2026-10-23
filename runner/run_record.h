// run_record.h - one JSON line per timed or warm-up block, and the work-tx
// hash that identifies the generated workload.

#pragma once

#include <cstdint>
#include <string>

#include "transaction.hh"

namespace whitney {

// TrialRecord is the result of one block of one batch-size run.
struct TrialRecord {
  uint32_t nexec;
  uint32_t batch_size;
  uint32_t block_index;
  bool warmup;
  uint64_t txs;
  uint64_t elapsed_ns;
  double tps;
  std::string work_tx_hash;
  uint32_t accounts;
  std::string experiment;
  // Wall time of the block's open, propose and close phases; their sum is
  // at most elapsed_ns.
  uint64_t open_ns;
  uint64_t exec_ns;
  uint64_t close_ns;
  // CPU time of the runner process (all threads) during elapsed_ns.
  uint64_t runner_cpu_ns;
};

// trial_json returns r as one JSON object on one line, keys in struct order,
// with no trailing newline. tps uses the shortest decimal form that reads
// back to the same double; a non-finite tps is written as null.
std::string trial_json(const TrialRecord &r);

// hash_work_txs returns the lowercase hex SHA-256 of the XDR encoding of txs.
std::string hash_work_txs(const hogd::SignedTransactionBatch &txs);

} // namespace whitney
