// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Fixture for test_rr_case.py: every way a case can end, in both forms of
// rr_case.h. RR_FIXTURE_FORM=registry runs the RR_CASE cases, =ids the
// requirement-id and flag edge cases, =hang one case that never ends
// (RR_FIXTURE_PIDFILE gets its pid; RR_FIXTURE_NO_PDEATHSIG clears its
// PR_SET_PDEATHSIG; RR_FIXTURE_USER_TERM installs a SIGTERM handler of the
// program's own that does _exit(42)), =signals the cases that check a case's
// signal mask and dispositions (the runner has a SIGINT handler of its own),
// and =locale the list under the LC_NUMERIC in RR_FIXTURE_LOCALE; otherwise
// the explicit list runs (and the registry is ignored).

#undef NDEBUG  // the assert() case must abort in optimized builds too
#include <cassert>
#include <clocale>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <string>

#include "rr_case.h"

#if !defined(_WIN32)
#include <unistd.h>
#endif
#if defined(__linux__)
#include <sys/prctl.h>
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

static void Hangs() {
#if defined(__linux__)
  // Act like a platform without PR_SET_PDEATHSIG, where only the runner's
  // signal handler can end the case.
  if (std::getenv("RR_FIXTURE_NO_PDEATHSIG") != nullptr) prctl(PR_SET_PDEATHSIG, 0);
#endif
#if !defined(_WIN32)
  if (const char* pidfile = std::getenv("RR_FIXTURE_PIDFILE")) {
    if (FILE* f = std::fopen(pidfile, "w")) {
      std::fprintf(f, "%ld\n", static_cast<long>(getpid()));
      std::fclose(f);
    }
  }
  for (;;) pause();
#endif
}

#if !defined(_WIN32)
extern "C" void UserTermHandler(int) { _exit(42); }
extern "C" void UserIntHandler(int) {}

static volatile sig_atomic_t g_hup_seen = 0;
extern "C" void CaseHupHandler(int) { g_hup_seen = 1; }

static void Install(int sig, void (*handler)(int)) {
  struct sigaction sa;
  std::memset(&sa, 0, sizeof sa);
  sa.sa_handler = handler;
  sigemptyset(&sa.sa_mask);
  sigaction(sig, &sa, nullptr);
}

static void HandlerOf(int sig, struct sigaction* sa) { sigaction(sig, nullptr, sa); }

// Inside a case none of the signals the runner blocks around fork is blocked.
static void MaskIsTheRunners() {
  sigset_t now;
  sigemptyset(&now);
  RR_CHECK(sigprocmask(SIG_SETMASK, nullptr, &now) == 0);
  RR_CHECK(!sigismember(&now, SIGTERM));
  RR_CHECK(!sigismember(&now, SIGINT));
  RR_CHECK(!sigismember(&now, SIGHUP));
}

// ... and each of them has what the runner had before rr::RunCases.
static void DispositionsAreTheRunners() {
  struct sigaction sa;
  HandlerOf(SIGTERM, &sa);
  RR_CHECK(sa.sa_handler == SIG_DFL);
  HandlerOf(SIGHUP, &sa);
  RR_CHECK(sa.sa_handler == SIG_DFL);
  HandlerOf(SIGINT, &sa);
  RR_CHECK(sa.sa_handler == &UserIntHandler);
}

static void RaisesSigterm() {
  raise(SIGTERM);
  std::printf("survived SIGTERM\n");
}

static void OwnSighupHandler() {
  Install(SIGHUP, &CaseHupHandler);
  raise(SIGHUP);
  RR_CHECK(g_hup_seen == 1);
}
#endif

int main(int argc, char** argv) {
  const std::string form = std::getenv("RR_FIXTURE_FORM") != nullptr ? std::getenv("RR_FIXTURE_FORM") : "";
  if (form == "registry") return rr::RunCases(argc, argv, "fixture_registry");
#if !defined(_WIN32)
  if (form == "hang" && std::getenv("RR_FIXTURE_USER_TERM") != nullptr) Install(SIGTERM, &UserTermHandler);
  if (form == "signals") {
    Install(SIGINT, &UserIntHandler);
    return rr::RunCases(argc, argv, "fixture_signals",
                        {
                            {"mask_is_the_runners", MaskIsTheRunners},
                            {"dispositions_are_the_runners", DispositionsAreTheRunners},
                            {"raises_sigterm", RaisesSigterm},
                            {"own_sighup_handler", OwnSighupHandler},
                        });
  }
#endif
  if (form == "hang") return rr::RunCases(argc, argv, "fixture_hang", {{"hangs", Hangs}});
  if (form == "ids") {
    return rr::RunCases(argc, argv, "fixture_ids",
                        {
                            {"dotted_id", Passes, "SRS-1.2_a"},
                            {"nbsp_id", MustNotRun, "REQ-1\xc2\xa0REQ-2"},
                            {"semicolon_id", MustNotRun, "REQ-1;REQ-2"},
                            {"slash_id", MustNotRun, "REQ-1/REQ-2"},
                            {nullptr, MustNotRun},
                            {"twice", Passes},
                            {"twice", MustNotRun},
                        });
  }
  if (form == "locale") {
    const char* wanted = std::getenv("RR_FIXTURE_LOCALE");
    if (wanted == nullptr || std::setlocale(LC_NUMERIC, wanted) == nullptr) return 77;  // locale not installed
    return rr::RunCases(argc, argv, "fixture_locale", {{"passes", Passes}, {"check_fails", CheckFails}});
  }
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
