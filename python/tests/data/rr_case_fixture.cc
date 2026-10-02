// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Fixture for test_rr_case.py: every way a case can end, in both forms of
// rr_case.h. RR_FIXTURE_FORM=registry runs the RR_CASE cases; otherwise the
// explicit list runs (and the registry is ignored).

#undef NDEBUG  // the assert() case must abort in optimized builds too
#include <cassert>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <stdexcept>
#include <string>

#include "rr_case.h"

#if !defined(_WIN32)
#include <unistd.h>
#endif

RR_CASE(registered_plain) { RR_CHECK(1 + 1 == 2); }

RR_CASE(registered_tagged, "REQ-9") { RR_CHECK(2 * 2 == 4); }

RR_CASE(registered_fails) { RR_CHECK(false); }

static void Passes() { RR_CHECK(3 * 3 == 9); }

static void PrintsThenPasses() {
  std::printf("hello from stdout\n");
  std::fprintf(stderr, "hello from stderr\n");
}

static void AssertFails() { assert(1 + 1 == 3); }

static void CheckFails() { RR_CHECK(2 + 2 == 5); }

static void Segfaults() {
  int* volatile p = nullptr;  // volatile: the store is not optimized into a trap
  *p = 1;
}

static void Throws() { throw std::runtime_error("boom"); }

static void ExitsNonzero() {
  std::printf("about to exit\n");
  std::exit(3);
}

static void XmlUnsafeOutput() {
  std::printf("bad <&\"> \x01 \xff bytes\n");
  std::exit(2);
}

static void MustNotRun() { std::printf("MUST NOT RUN\n"); }

static void KillsRunner() {
#if !defined(_WIN32)
  if (std::getenv("RR_FIXTURE_KILL") != nullptr) kill(getppid(), SIGKILL);
#endif
}

int main(int argc, char** argv) {
  const char* form = std::getenv("RR_FIXTURE_FORM");
  if (form != nullptr && std::string(form) == "registry") return rr::RunCases(argc, argv, "fixture_registry");
  return rr::RunCases(argc, argv, "fixture",
                      {
                          {"passes", Passes, "REQ-1"},
                          {"prints_then_passes", PrintsThenPasses},
                          {"assert_fails", AssertFails, "REQ-2"},
                          {"check_fails", CheckFails, "REQ-2"},
                          {"segfaults", Segfaults},
                          {"throws", Throws},
                          {"exits_nonzero", ExitsNonzero},
                          {"xml_unsafe_output", XmlUnsafeOutput},
                          {"comma_id", MustNotRun, "REQ-1,REQ-2"},
                          {"space_id", MustNotRun, "REQ-1 REQ-2"},
                          {"empty_id", MustNotRun, ""},
                          {"passes", MustNotRun},  // duplicate name
                          {"kills_runner", KillsRunner},
                      });
}
