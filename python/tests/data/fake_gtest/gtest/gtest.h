// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Just enough of googletest's API for test_rr_gtest.py to compile rr_gtest.h
// without googletest: RecordProperty prints what it records.
#ifndef FAKE_GTEST_H_
#define FAKE_GTEST_H_

#include <cstdio>
#include <string>

namespace testing {

struct TestInfo {
  const char* suite;
  const char* test;
  const char* test_suite_name() const { return suite; }
  const char* name() const { return test; }
};

struct TestSuite {};

class UnitTest {
 public:
  static UnitTest* GetInstance() {
    static UnitTest unit;
    return &unit;
  }
  const TestInfo* current_test_info() const { return info; }
  const TestSuite* current_test_suite() const { return nullptr; }
  const TestInfo* info = nullptr;
};

class Test {
 public:
  static void RecordProperty(const std::string& key, const std::string& value) {
    std::printf("%s=%s\n", key.c_str(), value.c_str());
  }
};

}  // namespace testing

#endif  // FAKE_GTEST_H_
