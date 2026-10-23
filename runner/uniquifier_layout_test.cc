#include "uniquifier_layout.h"

#include <gtest/gtest.h>

#include <cstddef>
#include <cstdint>
#include <functional>
#include <map>
#include <random>
#include <set>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace whitney {
namespace {

constexpr uint64_t kBase = 1000;

// skewed_senders returns n sender ids drawn from [0, senders) with a fixed
// seed. Sender 0 gets about half of all txs, so it sets S.
std::vector<uint32_t>
skewed_senders(size_t n, uint32_t senders, uint32_t seed)
{
  std::mt19937 gen(seed);
  std::uniform_int_distribution<uint32_t> pick(0, senders - 1);
  std::bernoulli_distribution hot(0.5);
  std::vector<uint32_t> ids(n);
  for (uint32_t &id : ids) {
    id = hot(gen) ? 0 : pick(gen);
  }
  return ids;
}

std::vector<Uniq>
all_uniquifiers(const UniquifierPlan &plan, size_t n, uint64_t blocks)
{
  std::vector<Uniq> out;
  for (uint64_t i = 0; i < blocks; i++) {
    for (size_t j = 0; j < n; j++) {
      out.push_back(plan.at(kBase, i, j));
    }
  }
  return out;
}

TEST(UniquifierLayout, ParseAndName)
{
  EXPECT_TRUE(parse_uniquifier_layout("per-sender") ==
              UniquifierLayout::kPerSender);
  EXPECT_TRUE(parse_uniquifier_layout("global") == UniquifierLayout::kGlobal);
  EXPECT_FALSE(parse_uniquifier_layout("per_sender").has_value());
  EXPECT_FALSE(parse_uniquifier_layout("").has_value());
  EXPECT_EQ(layout_name(UniquifierLayout::kPerSender), "per-sender");
  EXPECT_EQ(layout_name(UniquifierLayout::kGlobal), "global");
}

TEST(SenderIds, DenseInFirstSeenOrder)
{
  SenderIds<std::string> ids;
  EXPECT_EQ(ids.id("b"), 0u);
  EXPECT_EQ(ids.id("a"), 1u);
  EXPECT_EQ(ids.id("b"), 0u);
  EXPECT_EQ(ids.id("c"), 2u);
  EXPECT_EQ(ids.size(), 3u);
}

TEST(PerSender, HeightsPerBlockIsCeilOfBusiestSender)
{
  constexpr uint64_t B = kSenderNoncesPerHeight;
  EXPECT_EQ(B, 7000u);
  // One tx: S is at least 1.
  EXPECT_EQ(UniquifierPlan(UniquifierLayout::kPerSender, std::vector<uint32_t>{0}).heights_per_block(),
            1u);
  std::vector<uint32_t> ids(B, 3);
  ids.push_back(1);
  EXPECT_EQ(UniquifierPlan(UniquifierLayout::kPerSender, ids).heights_per_block(),
            1u);
  ids.push_back(3); // sender 3 now has B + 1 txs
  EXPECT_EQ(UniquifierPlan(UniquifierLayout::kPerSender, ids).heights_per_block(),
            2u);
  std::vector<uint32_t> big(2 * B + 1, 0);
  EXPECT_EQ(UniquifierPlan(UniquifierLayout::kPerSender, big).heights_per_block(),
            3u);
}

TEST(PerSender, ExpiryIsBasePlusBlockTimesSPlusBucket)
{
  constexpr uint64_t B = kSenderNoncesPerHeight;
  // Sender 0: 2B + 1 txs (S = 3), interleaved with sender 1 (2 txs).
  std::vector<uint32_t> ids(2 * B + 1, 0);
  ids.insert(ids.begin() + 1, 1);
  ids.push_back(1);
  const UniquifierPlan plan(UniquifierLayout::kPerSender, ids);
  ASSERT_EQ(plan.heights_per_block(), 3u);
  ASSERT_EQ(plan.size(), ids.size());
  const size_t n = ids.size();
  for (uint64_t i : {0u, 1u, 4u}) {
    // j = 0 is sender 0's tx k = 0; j = 1 is sender 1's tx k = 0.
    EXPECT_EQ(plan.at(kBase, i, 0).expiry_height, kBase + (i * 3));
    EXPECT_EQ(plan.at(kBase, i, 1).expiry_height, kBase + (i * 3));
    // j = B is sender 0's tx k = B - 1, the last of bucket 0.
    EXPECT_EQ(plan.at(kBase, i, B).expiry_height, kBase + (i * 3));
    // j = B + 1 is sender 0's k = B: bucket 1.
    EXPECT_EQ(plan.at(kBase, i, B + 1).expiry_height, kBase + (i * 3) + 1);
    // j = n - 2 is sender 0's k = 2B: bucket 2.
    EXPECT_EQ(plan.at(kBase, i, n - 2).expiry_height, kBase + (i * 3) + 2);
    // j = n - 1 is sender 1's k = 1: bucket 0.
    EXPECT_EQ(plan.at(kBase, i, n - 1).expiry_height, kBase + (i * 3));
    for (size_t j : {size_t{0}, size_t{1}, size_t{B + 1}, n - 1}) {
      EXPECT_EQ(plan.at(kBase, i, j).nonce, (i * n) + j);
    }
  }
}

// The replay-cache properties hog-db needs, on a skewed random batch.
TEST(PerSender, ReplayCacheProperties)
{
  for (uint32_t senders : {2u, 100u, 10'000u}) {
    const size_t n = 40'000;
    const uint64_t blocks = 6;
    const std::vector<uint32_t> ids = skewed_senders(n, senders, senders);
    const UniquifierPlan plan(UniquifierLayout::kPerSender, ids);
    const uint64_t S = plan.heights_per_block();
    std::map<uint32_t, uint64_t> counts;
    for (uint32_t id : ids) {
      counts[id]++;
    }
    // Sender 0 is the busiest: 75% of txs with 2 senders, about 50% else.
    const uint64_t busiest = counts[0];
    EXPECT_EQ(S, (busiest + kSenderNoncesPerHeight - 1) /
                     kSenderNoncesPerHeight)
        << senders;
    EXPECT_TRUE(S >= 3) << senders;

    std::set<uint64_t> nonces;
    std::map<std::pair<uint32_t, uint64_t>, uint64_t> per_height;
    std::map<uint32_t, std::set<uint64_t>> heights;
    for (uint64_t i = 0; i < blocks; i++) {
      for (size_t j = 0; j < n; j++) {
        const Uniq u = plan.at(kBase, i, j);
        EXPECT_TRUE(nonces.insert(u.nonce).second) << "repeated nonce";
        // Not expired when block i commits at height kBase + i.
        EXPECT_TRUE(u.expiry_height >= kBase + i);
        // Block i owns heights [kBase + i * S, kBase + (i + 1) * S).
        EXPECT_TRUE(u.expiry_height >= kBase + (i * S));
        EXPECT_TRUE(u.expiry_height < kBase + ((i + 1) * S));
        per_height[{ids[j], u.expiry_height}]++;
        heights[ids[j]].insert(u.expiry_height);
      }
    }
    for (const auto &[key, count] : per_height) {
      EXPECT_TRUE(count <= kSenderNoncesPerHeight) << count;
    }
    EXPECT_EQ(heights[0].size(), blocks * S); // the busiest sender
    for (const auto &[sender, hs] : heights) {
      EXPECT_TRUE(hs.size() <= blocks * S);
    }
  }
}

TEST(PerSender, Deterministic)
{
  const std::vector<uint32_t> ids = skewed_senders(20'000, 50, 7);
  const UniquifierPlan a(UniquifierLayout::kPerSender, ids);
  const UniquifierPlan b(UniquifierLayout::kPerSender, ids);
  EXPECT_TRUE(all_uniquifiers(a, ids.size(), 3) ==
              all_uniquifiers(b, ids.size(), 3));
}

// The global layout is the original runner's: CommonBenchState::
// bench_uniquifier(work_base, i * n + j), 4,000 nonces per height.
TEST(Global, MatchesBenchUniquifier)
{
  constexpr uint64_t G = kGlobalNoncesPerHeight;
  EXPECT_EQ(G, 4000u);
  const size_t n = 10'001;
  const std::vector<uint32_t> ids = skewed_senders(n, 10, 1);
  const UniquifierPlan plan(UniquifierLayout::kGlobal, ids);
  EXPECT_EQ(plan.heights_per_block(), 3u); // ceil(10001 / 4000)
  for (uint64_t i : {0u, 1u, 9u}) {
    for (size_t j : {size_t{0}, size_t{3999}, size_t{4000}, n - 1}) {
      const uint64_t index = (i * n) + j;
      const Uniq u = plan.at(kBase, i, j);
      EXPECT_EQ(u.expiry_height, kBase + (index / G));
      EXPECT_EQ(u.nonce, index);
      EXPECT_TRUE(u.expiry_height >= kBase + i);
    }
  }
  EXPECT_EQ(UniquifierPlan(UniquifierLayout::kGlobal,
                           std::vector<uint32_t>(1'000'000, 0))
                .heights_per_block(),
            250u);
}

TEST(ReplayBudget, RejectsAtTheCap)
{
  EXPECT_EQ(kMaxLiveHeights, 4000u);
  EXPECT_NO_THROW(
      check_replay_budget(UniquifierLayout::kPerSender, 2, 1999)); // 3998
  try {
    check_replay_budget(UniquifierLayout::kPerSender, 2, 2000);
    ADD_FAILURE() << "no throw";
  } catch (const ReplayBudgetError &e) {
    EXPECT_EQ(std::string(e.what()),
              "replay-cache budget: 2 expiry heights per sender per block × "
              "2000 blocks = 4000, must be < 4000 (uniquifier_layout "
              "per-sender)");
  }
  EXPECT_NO_THROW(check_replay_budget(UniquifierLayout::kGlobal, 250, 15));
  try {
    check_replay_budget(UniquifierLayout::kGlobal, 250, 16);
    ADD_FAILURE() << "no throw";
  } catch (const ReplayBudgetError &e) {
    EXPECT_EQ(std::string(e.what()),
              "replay-cache budget: 250 expiry heights per sender per block × "
              "16 blocks = 4000, must be < 4000 (uniquifier_layout global)");
  }
  // uint64_t math: the largest counts do not wrap to a small product.
  EXPECT_THROW(check_replay_budget(UniquifierLayout::kGlobal, 1'073'742,
                                   uint64_t{4'294'967'295u} * 2),
               ReplayBudgetError);
}

} // namespace
} // namespace whitney
