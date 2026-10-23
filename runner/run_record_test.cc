#include "run_record.h"

#include <gtest/gtest.h>

#include <limits>
#include <string>
#include <utility>

#include "transaction.hh"

namespace whitney {
namespace {

TEST(TrialJson, ExactLine)
{
  TrialRecord r{
      .nexec = 3,
      .batch_size = 5,
      .block_index = 2,
      .warmup = false,
      .txs = 1'000'000,
      .elapsed_ns = 2'000'000'123,
      .tps = 499999.96925,
      .work_tx_hash = "df3f619804a92fdb",
      .accounts = 100,
      .experiment = "erc20_payments",
      .open_ns = 1'500'000,
      .exec_ns = 1'950'000'000,
      .close_ns = 45'000'000,
      .runner_cpu_ns = 20'000'000'000,
  };
  EXPECT_EQ(trial_json(r),
            R"({"nexec":3,"batch_size":5,"block_index":2,"warmup":false,)"
            R"("txs":1000000,"elapsed_ns":2000000123,"tps":499999.96925,)"
            R"("work_tx_hash":"df3f619804a92fdb","accounts":100,)"
            R"("experiment":"erc20_payments","open_ns":1500000,)"
            R"("exec_ns":1950000000,"close_ns":45000000,)"
            R"("runner_cpu_ns":20000000000})");
}

TEST(TrialJson, WarmupWholeTpsAndEscapes)
{
  TrialRecord r{
      .nexec = 1,
      .batch_size = 1,
      .block_index = 0,
      .warmup = true,
      .txs = 4000,
      .elapsed_ns = 1'000'000'000,
      .tps = 4000.0,
      .work_tx_hash = "",
      .accounts = 1,
      .experiment = "a\"b",
      .open_ns = 0,
      .exec_ns = 0,
      .close_ns = 0,
      .runner_cpu_ns = 0,
  };
  EXPECT_EQ(trial_json(r),
            R"({"nexec":1,"batch_size":1,"block_index":0,"warmup":true,)"
            R"("txs":4000,"elapsed_ns":1000000000,"tps":4000.0,)"
            R"("work_tx_hash":"","accounts":1,"experiment":"a\"b",)"
            R"("open_ns":0,"exec_ns":0,"close_ns":0,"runner_cpu_ns":0})");
}

TEST(TrialJson, TpsAlwaysHasFraction)
{
  TrialRecord r{};
  r.tps = 0.0;
  EXPECT_NE(trial_json(r).find(R"("tps":0.0,)"), std::string::npos);
  r.tps = 1e20;
  EXPECT_NE(trial_json(r).find(R"("tps":100000000000000000000.0,)"),
            std::string::npos);
  r.tps = 12.5;
  EXPECT_NE(trial_json(r).find(R"("tps":12.5,)"), std::string::npos);
}

TEST(TrialJson, NonFiniteTpsIsNull)
{
  TrialRecord r{};
  r.tps = std::numeric_limits<double>::infinity();
  EXPECT_NE(trial_json(r).find(R"("tps":null,)"), std::string::npos);
}

hogd::SignedTransactionBatch
batch_of_two()
{
  hogd::SignedTransactionBatch txs;
  txs.resize(2);
  txs[0].tx.uniquifier.nonce = 1;
  txs[0].tx.uniquifier.block_expiry_height = 10;
  txs[1].tx.uniquifier.nonce = 2;
  txs[1].tx.uniquifier.block_expiry_height = 10;
  return txs;
}

TEST(HashWorkTxs, StableAndOrderSensitive)
{
  hogd::SignedTransactionBatch a = batch_of_two();
  hogd::SignedTransactionBatch b = batch_of_two();
  const std::string ha = hash_work_txs(a);
  EXPECT_EQ(ha.size(), 64u);
  EXPECT_EQ(ha.find_first_not_of("0123456789abcdef"), std::string::npos);
  EXPECT_EQ(ha, hash_work_txs(a));
  EXPECT_EQ(ha, hash_work_txs(b));

  std::swap(b[0], b[1]);
  EXPECT_NE(ha, hash_work_txs(b));
}

TEST(HashWorkTxs, EmptyBatchIsSha256OfXdrLength)
{
  // XDR of an empty variable-length array is a 4-byte zero length.
  // sha256(00 00 00 00):
  EXPECT_EQ(hash_work_txs(hogd::SignedTransactionBatch{}),
            "df3f619804a92fdb4057192dc43dd748ea778adc52bc498ce80524c014b81119");
}

} // namespace
} // namespace whitney
