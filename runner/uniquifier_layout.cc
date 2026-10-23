#include "uniquifier_layout.h"

#include <algorithm>
#include <cassert>
#include <format>
#include <limits>

namespace whitney {

std::optional<UniquifierLayout>
parse_uniquifier_layout(std::string_view s)
{
  if (s == "per-sender") {
    return UniquifierLayout::kPerSender;
  }
  if (s == "global") {
    return UniquifierLayout::kGlobal;
  }
  return std::nullopt;
}

std::string_view
layout_name(UniquifierLayout layout)
{
  return layout == UniquifierLayout::kPerSender ? "per-sender" : "global";
}

UniquifierPlan::UniquifierPlan(UniquifierLayout layout,
                               std::span<const uint32_t> sender_ids)
  : layout_(layout), n_(sender_ids.size())
{
  assert(n_ > 0);
  if (layout == UniquifierLayout::kGlobal) {
    heights_per_block_ =
        (n_ + kGlobalNoncesPerHeight - 1) / kGlobalNoncesPerHeight;
    return;
  }
  const uint32_t max_id = *std::ranges::max_element(sender_ids);
  std::vector<uint64_t> seen(size_t{max_id} + 1, 0);
  bucket_.reserve(n_);
  uint64_t most = 0;
  for (uint32_t id : sender_ids) {
    const uint64_t k = seen[id]++;
    bucket_.push_back(static_cast<uint32_t>(k / kSenderNoncesPerHeight));
    most = std::max(most, seen[id]);
  }
  heights_per_block_ =
      (most + kSenderNoncesPerHeight - 1) / kSenderNoncesPerHeight;
}

Uniq
UniquifierPlan::at(uint64_t work_base, uint64_t block, size_t j) const
{
  assert(j < n_);
  const uint64_t index = (block * n_) + j;
  if (layout_ == UniquifierLayout::kGlobal) {
    return {.expiry_height = work_base + (index / kGlobalNoncesPerHeight),
            .nonce = index};
  }
  return {.expiry_height =
              work_base + (block * heights_per_block_) + bucket_[j],
          .nonce = index};
}

void
check_replay_budget(UniquifierLayout layout, uint64_t heights_per_block,
                    uint64_t blocks)
{
  uint64_t total = 0;
  if (__builtin_mul_overflow(heights_per_block, blocks, &total)) {
    total = std::numeric_limits<uint64_t>::max();
  }
  if (total >= kMaxLiveHeights) {
    throw ReplayBudgetError(std::format(
        "replay-cache budget: {} expiry heights per sender per block × {} "
        "blocks = {}, must be < {} (uniquifier_layout {})",
        heights_per_block, blocks, total, kMaxLiveHeights,
        layout_name(layout)));
  }
}

} // namespace whitney
