// SPDX-License-Identifier: AGPL-3.0-or-later
//
// rules_requirements traceability for googletest.
//
//   #include "rr_gtest.h"   // Bazel: deps = ["@rules_requirements//cc:gtest"]
//
//   TEST(Interlock, CutsHeaterAtLimit) {
//     RR_VERIFIES("REQ-4", "REQ-5");   // entity ids this test verifies
//     RR_LEVEL("sil");                  // optional: rigor this test provides
//     RR_ARTIFACT("firmware_build_id", kBuildId);  // optional: for staleness
//     ...
//   }
//
// googletest writes JUnit XML to $XML_OUTPUT_FILE under `bazel test` (or to
// --gtest_output=xml:PATH), and RecordProperty() values land on the test
// case — as <property> elements in current releases, as attributes in older
// ones; the rules_requirements JUnit ingestor reads both. RecordProperty keeps
// one value per key, so the ids are recorded as a comma-separated
// "requirements" property. `RR_VERIFIES` may be called more than once; ids
// accumulate.

#ifndef RULES_REQUIREMENTS_RR_GTEST_H_
#define RULES_REQUIREMENTS_RR_GTEST_H_

#include <initializer_list>
#include <string>

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

// Records `ids` as verified by the currently running test.
inline void Verifies(std::initializer_list<const char*> ids) {
  std::string& all = internal::CurrentIds();
  for (const char* id : ids) {
    if (id == nullptr || *id == '\0') continue;
    if (!all.empty()) all += ",";
    all += id;
  }
  ::testing::Test::RecordProperty("requirements", all);
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
