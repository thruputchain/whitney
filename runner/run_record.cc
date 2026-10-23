#include "run_record.h"

#include <charconv>
#include <cmath>
#include <cstddef>
#include <format>
#include <span>
#include <string_view>
#include <system_error>

#include "hogd/base/json.h"
#include "hogd/base/string.h"
#include "hogd/crypto/crypto.h"

namespace whitney {
namespace {

// quote returns s as a JSON string literal, escaped by hogd's JSON writer.
std::string
quote(std::string_view s)
{
  std::string out;
  hogd::json::Writer(out).value(s);
  return out;
}

// number returns v in the shortest fixed-point form that reads back to the
// same double, or null when v is not finite (JSON has no inf or nan).
std::string
number(double v)
{
  if (!std::isfinite(v)) {
    return "null";
  }
  char buf[512]; // fixed form of DBL_MAX is 309 digits
  auto [end, ec] = std::to_chars(buf, buf + sizeof(buf), v,
                                 std::chars_format::fixed);
  if (ec != std::errc{}) {
    return "null";
  }
  std::string out(buf, end);
  // Keep a fractional part on whole numbers, so a JSON reader always sees
  // a float (4000.0, not the int 4000).
  if (out.find('.') == std::string::npos) {
    out += ".0";
  }
  return out;
}

} // namespace

std::string
trial_json(const TrialRecord &r)
{
  return std::format(
      R"({{"nexec":{},"batch_size":{},"block_index":{},"warmup":{},)"
      R"("txs":{},"elapsed_ns":{},"tps":{},"work_tx_hash":{},)"
      R"("accounts":{},"experiment":{},"open_ns":{},"exec_ns":{},)"
      R"("close_ns":{},"runner_cpu_ns":{}}})",
      r.nexec, r.batch_size, r.block_index, r.warmup ? "true" : "false",
      r.txs, r.elapsed_ns, number(r.tps), quote(r.work_tx_hash), r.accounts,
      quote(r.experiment), r.open_ns, r.exec_ns, r.close_ns, r.runner_cpu_ns);
}

std::string
hash_work_txs(const hogd::SignedTransactionBatch &txs)
{
  const hogd::SHA256Hash h = hogd::sha256(txs);
  return hogd::encode_hex(std::as_bytes(std::span(h)));
}

} // namespace whitney
