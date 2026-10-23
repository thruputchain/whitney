// uniquifier_layout.h - the uniquifiers whitney-runner gives the work txs.
//
// The runner replays the same work txs in every block, so each block gives
// each tx a fresh uniquifier (expiry height, nonce). hog-db keeps a replay
// cache per address: the root invoked address of the tx, which for
// erc20_payments is the sender's wallet. Per address it allows at most
// kReplayCacheHeightsMaxLen (4,000) live expiry heights and
// kReplayCacheNoncesMaxLen (8,000) nonces per height. A height is live while
// it is >= the block height; a tx whose expiry height is below the block
// height is rejected.
//
// Two layouts:
//   - per-sender (the default): block i gives the k-th tx of a sender expiry
//     height work_base + i * S + floor(k / 7000). S is the busiest sender's
//     ceil(txs / 7000), so each sender adds at most S heights per block.
//   - global: the original runner's layout, CommonBenchState::
//     bench_uniquifier: expiry height work_base + floor(index / 4000) over the
//     global index i * n + j. With many senders, each sender touches about
//     n / 4000 heights per block.
// Both use the nonce i * n + j, so no nonce repeats across blocks.

#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <optional>
#include <span>
#include <stdexcept>
#include <string_view>
#include <unordered_map>
#include <vector>

namespace whitney {

enum class UniquifierLayout { kPerSender, kGlobal };

// Nonces per (sender, expiry height) in the per-sender layout, under hog-db's
// 8,000 with a margin.
inline constexpr uint64_t kSenderNoncesPerHeight = 7000;
// Nonces per expiry height, over all senders, in the global layout
// (CommonBenchState::kBenchUniquifiersPerHeight).
inline constexpr uint64_t kGlobalNoncesPerHeight = 4000;
// hog-db's live expiry heights per address (kReplayCacheHeightsMaxLen).
inline constexpr uint64_t kMaxLiveHeights = 4000;

// parse_uniquifier_layout maps "per-sender" and "global" to a layout, and
// anything else to nullopt.
std::optional<UniquifierLayout> parse_uniquifier_layout(std::string_view s);

// layout_name is the inverse of parse_uniquifier_layout.
std::string_view layout_name(UniquifierLayout layout);

struct Uniq {
  uint64_t expiry_height;
  uint64_t nonce;
  bool operator==(const Uniq &) const = default;
};

// SenderIds gives each distinct key a dense id, 0, 1, ..., in first-seen
// order.
template <class Key, class Hash = std::hash<Key>>
class SenderIds {
public:
  uint32_t id(const Key &key)
  {
    return ids_.try_emplace(key, static_cast<uint32_t>(ids_.size()))
        .first->second;
  }
  size_t size() const { return ids_.size(); }

private:
  std::unordered_map<Key, uint32_t, Hash> ids_;
};

// UniquifierPlan holds the uniquifier of every work tx in every block.
class UniquifierPlan {
public:
  // sender_ids[j] is the dense id of tx j's sender (see SenderIds). It must
  // not be empty.
  UniquifierPlan(UniquifierLayout layout, std::span<const uint32_t> sender_ids);

  // heights_per_block is S, the most expiry heights one sender gets per block:
  // exact for per-sender, the upper bound ceil(n / 4000) for global. After T
  // blocks a sender has at most T * S live heights.
  uint64_t heights_per_block() const { return heights_per_block_; }

  size_t size() const { return n_; }

  // at is tx j's uniquifier in block i (0-based, warm-up included), when the
  // first work block is work_base.
  Uniq at(uint64_t work_base, uint64_t block, size_t j) const;

private:
  UniquifierLayout layout_;
  size_t n_;
  uint64_t heights_per_block_;
  // Per-sender only: floor(k / 7000) for tx j, the k-th tx of its sender.
  std::vector<uint32_t> bucket_;
};

// ReplayBudgetError: the run would go over hog-db's live height cap.
class ReplayBudgetError : public std::runtime_error {
public:
  using std::runtime_error::runtime_error;
};

// check_replay_budget throws ReplayBudgetError when heights_per_block x
// blocks reaches kMaxLiveHeights.
void check_replay_budget(UniquifierLayout layout, uint64_t heights_per_block,
                         uint64_t blocks);

} // namespace whitney
