// SPDX-License-Identifier: AGPL-3.0-or-later
//
// rules_requirements traceability for googletest.
//
//   #include "rr_gtest.h"   // Bazel: deps = ["@rules_requirements//cc:gtest"]
//
//   TEST(Interlock, CutsHeaterAtLimit) {
//     RR_VERIFIES("REQ-4");            // the ONE requirement this test verifies
//     RR_LEVEL("sil");                  // optional: rigor this test provides
//     RR_ARTIFACT("firmware_build_id", kBuildId);  // optional: for staleness
//     ...
//   }
//
// googletest writes JUnit XML to $XML_OUTPUT_FILE under `bazel test` (or to
// --gtest_output=xml:PATH), and RecordProperty() values land on the test
// case — as <property> elements in current releases, as attributes in older
// ones; the rules_requirements JUnit ingestor reads both. The id is recorded
// as the "requirement" property (0.2 wrote "requirements"; ingest reads both
// names). It is a declared tag: which requirement the case verifies is decided
// by attribution.
//
// A test case verifies at most one requirement. Several ids on one test (in
// one call, or over several calls) are deprecated: they warn on stderr
// [RR-E101] and are all recorded, as a comma list (RecordProperty keeps one
// value per key), so attribution quarantines the case and it counts for none
// of them. RR_VERIFIES in SetUpTestSuite, an Environment or main is still
// recorded on the suite, but no test case inherits a suite-level requirement.

#ifndef RULES_REQUIREMENTS_RR_GTEST_H_
#define RULES_REQUIREMENTS_RR_GTEST_H_

#include <cstdio>
#include <initializer_list>
#include <string>
#include <vector>

#include "gtest/gtest.h"

namespace rules_requirements {

namespace internal {
// Ids accumulate per recording scope: the running test, else the running
// suite (SetUpTestSuite/TearDownTestSuite), else the whole program (main, an
// Environment). Entering a new scope forgets the previous scope's ids.
inline std::string& CurrentIds() {
  static std::string ids;
  static const void* owner = nullptr;
  const ::testing::UnitTest* unit = ::testing::UnitTest::GetInstance();
  const void* now = unit->current_test_info();
  if (now == nullptr) now = unit->current_test_suite();
  if (now == nullptr) now = unit;
  if (now != owner) {
    owner = now;
    ids.clear();
  }
  return ids;
}
}  // namespace internal

namespace internal {
// The distinct ids of a comma list, in first-seen order.
inline std::vector<std::string> Distinct(const std::string& list) {
  std::vector<std::string> out;
  std::string::size_type start = 0;
  while (!list.empty() && start <= list.size()) {
    std::string::size_type end = list.find(',', start);
    if (end == std::string::npos) end = list.size();
    const std::string id = list.substr(start, end - start);
    bool seen = id.empty();
    for (const std::string& o : out) seen = seen || o == id;
    if (!seen) out.push_back(id);
    start = end + 1;
  }
  return out;
}
}  // namespace internal

// Declares `ids` (ONE id; several are deprecated) for the currently running test.
inline void Verifies(std::initializer_list<const char*> ids) {
  std::string& all = internal::CurrentIds();
  const std::vector<std::string>::size_type before = internal::Distinct(all).size();
  for (const char* id : ids) {
    if (id == nullptr || *id == '\0') continue;
    if (!all.empty()) all += ",";
    all += id;
  }
  const std::vector<std::string> named = internal::Distinct(all);
  if (before < 2 && named.size() >= 2) {
    const ::testing::TestInfo* info = ::testing::UnitTest::GetInstance()->current_test_info();
    const std::string test =
        info != nullptr ? std::string(info->test_suite_name()) + "." + info->name() : std::string("(no test)");
    std::string names;
    for (const std::string& id : named) names += (names.empty() ? "" : ", ") + id;
    std::fprintf(stderr,
                 "rr_gtest: warning: %s names %s; a test case verifies at most one requirement [RR-E101]. "
                 "Multi-id declarations are deprecated: every id is still recorded, so the case is quarantined "
                 "and counts for none of them (each reads INVALID), and 0.4 rejects it. Split the test, or keep "
                 "one id.\n",
                 test.c_str(), names.c_str());
  }
  std::string distinct;
  for (const std::string& id : named) distinct += (distinct.empty() ? "" : ",") + id;
  ::testing::Test::RecordProperty("requirement", distinct);
}

// Records the verification level (rigor) the current test provides.
inline void Level(const char* level) {
  ::testing::Test::RecordProperty("level", level);
}

// Records one key of the identity of the artifact under test.
inline void Artifact(const std::string& key, const std::string& value) {
  ::testing::Test::RecordProperty("artifact." + key, value);
}

}  // namespace rules_requirements

#define RR_VERIFIES(...) ::rules_requirements::Verifies({__VA_ARGS__})
#define RR_LEVEL(level) ::rules_requirements::Level(level)
#define RR_ARTIFACT(key, value) ::rules_requirements::Artifact(key, value)

#endif  // RULES_REQUIREMENTS_RR_GTEST_H_
