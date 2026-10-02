// SPDX-License-Identifier: AGPL-3.0-or-later
//
// rules_requirements per-case JUnit for plain-assert C/C++ tests, without
// googletest.
//
//   #include "rr_case.h"   // Bazel: deps = ["@rules_requirements//cc:case"]
//
//   RR_CASE(wifi_settings_vector) {              // one test case
//     RR_CHECK(Encode(kSettings) == kVector);    // assert() that NDEBUG keeps
//   }
//
//   RR_CASE(rejects_truncated_frame, "REQ-7") {  // optional: the ONE id it verifies
//     assert(!Decode(kTruncated));
//   }
//
//   int main(int argc, char** argv) { return rr::RunCases(argc, argv, "improv_codec"); }
//
// An existing main that calls plain test functions converts without moving
// them:
//
//   int main(int argc, char** argv) {
//     return rr::RunCases(argc, argv, "improv_codec", {
//         {"wifi_settings_vector", test_wifi_settings_vector},
//         {"rejects_truncated_frame", test_rejects_truncated_frame, "REQ-7"},
//     });
//   }
//
// On POSIX each case runs in its own forked child with core dumps suppressed,
// so a failing assert() or RR_CHECK, a crash, an uncaught exception or a
// non-zero exit() fails that case only and the next case still runs. The
// parent relays the child's output to the test log and writes one JUnit
// <testcase classname="<suite>" name="<case>"> per case to $XML_OUTPUT_FILE
// (set by `bazel test`) or to --rr_junit=PATH; the binary exits 1 if any case
// failed. The JUnit is rewritten before each case with that case recorded as
// an error, so a run killed mid-case (a Bazel timeout) still reports the
// cases that finished and names the one that did not. Without fork (Windows)
// cases run in-process: a failing assert ends the binary there, and the case
// it was running is the one reported as an error.
//
// A case verifies at most one requirement. The optional id is a single
// string recorded as the case's `requirement` property: RR_CASE with two ids
// does not compile [RR-E101], an id that is empty or contains a comma or
// whitespace is reported as an error case without running it [RR-E104], and
// nothing inside a case can add another.
//
// Flags (others are ignored, so the binary still accepts its own):
//   --rr_list         print every case key (`suite::case [id]`) and exit
//   --rr_case=NAME    run one case in-process (no fork, no JUnit): debuggers
//   --rr_junit=PATH   write JUnit here instead of $XML_OUTPUT_FILE
// `bazel test --test_filter=GLOB[,GLOB...]` selects cases by name or by
// `suite::case` (`*` and `?` wildcards), and `shard_count` is honoured.

#ifndef RULES_REQUIREMENTS_RR_CASE_H_
#define RULES_REQUIREMENTS_RR_CASE_H_

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <initializer_list>
#include <string>
#include <vector>
#if defined(__cpp_exceptions) || defined(__EXCEPTIONS) || defined(_CPPUNWIND)
#include <exception>
#define RR_INTERNAL_EXCEPTIONS 1
#endif
#if !defined(_WIN32)
#include <cerrno>
#include <csignal>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>
#define RR_INTERNAL_FORK 1
#endif

// Like assert(), but never compiled out by NDEBUG: prints the location and
// the expression, then aborts the case.
#define RR_CHECK(expr)                                                                         \
  ((expr) ? (void)0                                                                            \
          : (std::fprintf(stderr, "%s:%d: RR_CHECK(%s) failed\n", __FILE__, __LINE__, #expr), \
             std::fflush(stderr), std::abort()))

// RR_CASE(name) { body }  or  RR_CASE(name, "REQ-1") { body }: defines a case
// and registers it for rr::RunCases(argc, argv, suite). `name` must be an
// identifier; it is the case's name in the report.
#define RR_CASE(...)                                                                                  \
  RR_INTERNAL_EXPAND(RR_INTERNAL_CASE_PICK(__VA_ARGS__, RR_INTERNAL_CASE_TOO_MANY_IDS,                 \
                                           RR_INTERNAL_CASE_TOO_MANY_IDS, RR_INTERNAL_CASE_TOO_MANY_IDS, \
                                           RR_INTERNAL_CASE_VERIFIES, RR_INTERNAL_CASE_PLAIN, unused))  \
  (__VA_ARGS__)

namespace rr {

// One test case: a name, the function that runs it and optionally the one
// requirement id it verifies.
struct Case {
  Case(const char* case_name, void (*case_fn)(), const char* requirement_id = nullptr)
      : name(case_name), fn(case_fn), requirement(requirement_id) {}
  const char* name;
  void (*fn)();
  const char* requirement;
};

namespace internal {

inline std::vector<Case>& Registry() {
  static std::vector<Case> cases;
  return cases;
}

struct Registrar {
  explicit Registrar(const Case& c) { Registry().push_back(c); }
};

// Empty if `id` is absent or one well-formed id, else why it is not.
inline std::string CheckRequirement(const char* id) {
  if (id == nullptr) return "";
  if (*id == '\0') return "empty requirement id [RR-E104]";
  for (const char* p = id; *p != '\0'; ++p) {
    if (*p == ',') {
      return std::string("requirement \"") + id +
             "\" names more than one id; a test case verifies at most one requirement [RR-E104]";
    }
    if (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r' || *p == '\f' || *p == '\v') {
      return std::string("malformed requirement id \"") + id + "\" [RR-E104]";
    }
  }
  return "";
}

// `*` and `?` wildcards.
inline bool Glob(const char* pat, const char* s) {
  const char* star = nullptr;
  const char* resume = nullptr;
  while (*s != '\0') {
    if (*pat == '*') {
      star = pat++;
      resume = s;
    } else if (*pat == '?' || *pat == *s) {
      ++pat;
      ++s;
    } else if (star != nullptr) {
      pat = star + 1;
      s = ++resume;
    } else {
      return false;
    }
  }
  while (*pat == '*') ++pat;
  return *pat == '\0';
}

// A comma-separated list of globs, each matched against the case name and
// its `suite::case` key.
inline bool Selected(const std::string& filter, const std::string& name, const std::string& key) {
  size_t start = 0;
  while (start <= filter.size()) {
    size_t end = filter.find(',', start);
    if (end == std::string::npos) end = filter.size();
    std::string pat = filter.substr(start, end - start);
    if (!pat.empty() && (Glob(pat.c_str(), name.c_str()) || Glob(pat.c_str(), key.c_str()))) return true;
    start = end + 1;
  }
  return false;
}

// The length of the UTF-8 sequence at `s[i]` if it encodes a character XML
// 1.0 allows, else 0.
inline size_t XmlChar(const std::string& s, size_t i) {
  const unsigned char c = static_cast<unsigned char>(s[i]);
  size_t len = c < 0x80 ? 1 : (c & 0xE0) == 0xC0 ? 2 : (c & 0xF0) == 0xE0 ? 3 : (c & 0xF8) == 0xF0 ? 4 : 0;
  if (len == 0 || i + len > s.size()) return 0;
  unsigned long cp = len == 1 ? c : len == 2 ? (c & 0x1Fu) : len == 3 ? (c & 0x0Fu) : (c & 0x07u);
  for (size_t k = 1; k < len; ++k) {
    const unsigned char cc = static_cast<unsigned char>(s[i + k]);
    if ((cc & 0xC0) != 0x80) return 0;
    cp = (cp << 6) | (cc & 0x3Fu);
  }
  static const unsigned long kMin[] = {0, 0, 0x80, 0x800, 0x10000};
  if (cp < kMin[len]) return 0;  // overlong
  const bool allowed = cp == 0x9 || cp == 0xA || cp == 0xD || (cp >= 0x20 && cp <= 0xD7FF) ||
                       (cp >= 0xE000 && cp <= 0xFFFD) || (cp >= 0x10000 && cp <= 0x10FFFF);
  return allowed ? len : 0;
}

// XML attribute text: escapes markup and replaces anything XML 1.0 cannot
// carry (control characters, bytes that are not UTF-8) with '?', since test
// output is arbitrary.
inline std::string XmlAttr(const std::string& s) {
  std::string out;
  size_t i = 0;
  while (i < s.size()) {
    const size_t len = XmlChar(s, i);
    if (len == 0) {
      out += '?';
      ++i;
      continue;
    }
    switch (s[i]) {
      case '<': out += "&lt;"; break;
      case '>': out += "&gt;"; break;
      case '&': out += "&amp;"; break;
      case '"': out += "&quot;"; break;
      case '\t': out += "&#9;"; break;
      case '\n': out += "&#10;"; break;
      case '\r': out += "&#13;"; break;
      default: out.append(s, i, len);
    }
    i += len;
  }
  return out;
}

struct Result {
  std::string name;
  std::string requirement;  // empty: none (or not a valid one)
  std::string status;       // passed | failed | error
  std::string message;
  double seconds;
};

// Writes the JUnit atomically (temporary file, then rename).
inline void WriteJUnit(const std::string& path, const std::string& suite, const std::vector<Result>& results) {
  if (path.empty()) return;
  int failures = 0, errors = 0;
  double total = 0;
  for (const Result& r : results) {
    if (r.status == "failed") ++failures;
    if (r.status == "error") ++errors;
    total += r.seconds;
  }
  const std::string tmp = path + ".rr_case.tmp";
  FILE* f = std::fopen(tmp.c_str(), "w");
  if (f == nullptr) {
    std::fprintf(stderr, "rr_case: cannot write %s\n", tmp.c_str());
    return;
  }
  const std::string esc_suite = XmlAttr(suite);
  std::fprintf(f, "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n");
  std::fprintf(f, "<testsuites tests=\"%u\" failures=\"%d\" errors=\"%d\" time=\"%.3f\">\n",
               static_cast<unsigned>(results.size()), failures, errors, total);
  std::fprintf(f, "  <testsuite name=\"%s\" tests=\"%u\" failures=\"%d\" errors=\"%d\" skipped=\"0\" time=\"%.3f\">\n",
               esc_suite.c_str(), static_cast<unsigned>(results.size()), failures, errors, total);
  for (const Result& r : results) {
    std::fprintf(f, "    <testcase classname=\"%s\" name=\"%s\" time=\"%.3f\">\n", esc_suite.c_str(),
                 XmlAttr(r.name).c_str(), r.seconds);
    if (!r.requirement.empty()) {
      std::fprintf(f, "      <properties><property name=\"requirement\" value=\"%s\"/></properties>\n",
                   XmlAttr(r.requirement).c_str());
    }
    if (r.status == "failed" || r.status == "error") {
      std::fprintf(f, "      <%s message=\"%s\"/>\n", r.status == "failed" ? "failure" : "error",
                   XmlAttr(r.message).c_str());
    }
    std::fprintf(f, "    </testcase>\n");
  }
  std::fprintf(f, "  </testsuite>\n</testsuites>\n");
  const bool ok = std::fclose(f) == 0;
  if (!ok || std::rename(tmp.c_str(), path.c_str()) != 0) {
    std::fprintf(stderr, "rr_case: cannot write %s\n", path.c_str());
    std::remove(tmp.c_str());
  }
}

// Runs the case body, turning an escaping exception into a message.
inline bool Invoke(const Case& c, std::string* message) {
#if defined(RR_INTERNAL_EXCEPTIONS)
  try {
    c.fn();
  } catch (const std::exception& e) {
    *message = std::string("uncaught exception: ") + e.what();
    return false;
  } catch (...) {
    *message = "uncaught exception";
    return false;
  }
#else
  c.fn();
#endif
  (void)message;
  return true;
}

#if defined(RR_INTERNAL_FORK)
inline std::string SignalName(int sig) {
  switch (sig) {
    case SIGABRT: return "SIGABRT";
    case SIGSEGV: return "SIGSEGV";
    case SIGBUS: return "SIGBUS";
    case SIGFPE: return "SIGFPE";
    case SIGILL: return "SIGILL";
    case SIGKILL: return "SIGKILL";
    case SIGTERM: return "SIGTERM";
    case SIGTRAP: return "SIGTRAP";
    default: return "signal " + std::to_string(sig);
  }
}

// Runs one case in a forked child; fills status/message.
inline void RunForked(const Case& c, Result* r) {
  int out[2];
  if (pipe(out) != 0) {
    r->status = "error";
    r->message = std::string("pipe failed: ") + std::strerror(errno);
    return;
  }
  std::fflush(nullptr);
  const pid_t pid = fork();
  if (pid < 0) {
    r->status = "error";
    r->message = std::string("fork failed: ") + std::strerror(errno);
    close(out[0]);
    close(out[1]);
    return;
  }
  if (pid == 0) {
    // The child: its output goes through the parent (which keeps the last
    // line for the failure message), line-buffered so nothing printed before
    // an abort is lost; a failing case must not litter the sandbox with cores.
    close(out[0]);
    dup2(out[1], 1);
    dup2(out[1], 2);
    close(out[1]);
    static char stdout_buffer[BUFSIZ];  // glibc ignores _IOLBF without a fresh buffer
    std::setvbuf(stdout, stdout_buffer, _IOLBF, sizeof stdout_buffer);
    struct rlimit no_core;
    no_core.rlim_cur = 0;
    no_core.rlim_max = 0;
    setrlimit(RLIMIT_CORE, &no_core);
    std::string message;
    const bool ok = Invoke(c, &message);
    if (!ok) std::fprintf(stderr, "%s\n", message.c_str());
    std::fflush(nullptr);
    _exit(ok ? 0 : 1);
  }
  close(out[1]);
  std::string last, line;
  char buf[4096];
  for (;;) {
    const ssize_t n = read(out[0], buf, sizeof buf);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) break;
    std::fwrite(buf, 1, static_cast<size_t>(n), stdout);
    for (ssize_t i = 0; i < n; ++i) {
      if (buf[i] == '\n') {
        if (line.find_first_not_of(" \t\r") != std::string::npos) last = line;
        line.clear();
      } else if (line.size() < 1024) {
        line += buf[i];
      }
    }
  }
  if (line.find_first_not_of(" \t\r") != std::string::npos) last = line;
  std::fflush(stdout);
  close(out[0]);
  int st = 0;
  pid_t waited;
  do {
    waited = waitpid(pid, &st, 0);
  } while (waited < 0 && errno == EINTR);
  const size_t trim = last.find_last_not_of(" \t\r");
  last = trim == std::string::npos ? "" : last.substr(last.find_first_not_of(" \t\r"), trim + 1);
  if (waited < 0) {
    r->status = "error";
    r->message = std::string("waitpid failed: ") + std::strerror(errno);
  } else if (WIFSIGNALED(st)) {
    r->status = "failed";
    r->message = "terminated by " + SignalName(WTERMSIG(st));
    // assert(), RR_CHECK and std::terminate say why on the line before abort.
    if (WTERMSIG(st) == SIGABRT && !last.empty()) r->message += ": " + last;
  } else if (WIFEXITED(st) && WEXITSTATUS(st) != 0) {
    r->status = "failed";
    r->message = "exited with status " + std::to_string(WEXITSTATUS(st));
    if (!last.empty()) r->message += ": " + last;
  }
}
#endif

inline int Run(int argc, char** argv, const char* suite_name, const std::vector<Case>& cases) {
  const std::string suite = suite_name != nullptr ? suite_name : "";
  std::string junit;
  if (const char* env = std::getenv("XML_OUTPUT_FILE")) junit = env;
  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    if (arg == "--rr_list") {
      for (const Case& c : cases) {
        std::printf("%s::%s%s%s%s\n", suite.c_str(), c.name, c.requirement != nullptr ? " [" : "",
                    c.requirement != nullptr ? c.requirement : "", c.requirement != nullptr ? "]" : "");
      }
      return 0;
    }
    if (arg.compare(0, 10, "--rr_case=") == 0) {
      for (const Case& c : cases) {
        if (arg.compare(10, std::string::npos, c.name) == 0) {
          std::printf("[ RUN      ] %s::%s (in-process)\n", suite.c_str(), c.name);
          std::fflush(stdout);
          c.fn();
          std::printf("[       OK ] %s::%s\n", suite.c_str(), c.name);
          return 0;
        }
      }
      std::fprintf(stderr, "rr_case: no case named %s (see --rr_list)\n", arg.c_str() + 10);
      return 2;
    }
    if (arg.compare(0, 11, "--rr_junit=") == 0) {
      junit = arg.substr(11);
    } else if (arg.compare(0, 5, "--rr_") == 0) {
      std::fprintf(stderr, "rr_case: unknown flag %s (--rr_list, --rr_case=NAME, --rr_junit=PATH)\n", arg.c_str());
      return 2;
    }
  }

  std::string filter;
  if (const char* env = std::getenv("TESTBRIDGE_TEST_ONLY")) filter = env;
  long shards = 1, shard = 0;
  if (const char* env = std::getenv("TEST_TOTAL_SHARDS")) shards = std::strtol(env, nullptr, 10);
  if (const char* env = std::getenv("TEST_SHARD_INDEX")) shard = std::strtol(env, nullptr, 10);
  if (shards < 1 || shard < 0 || shard >= shards) {
    shards = 1;
    shard = 0;
  }
  if (const char* env = std::getenv("TEST_SHARD_STATUS_FILE")) {
    if (FILE* f = std::fopen(env, "w")) std::fclose(f);  // tells Bazel sharding is supported
  }

  if (cases.empty()) {
    std::fprintf(stderr, "rr_case: %s has no test cases (define them with RR_CASE or pass them to rr::RunCases)\n",
                 suite.c_str());
    WriteJUnit(junit, suite, {});
    return 1;
  }

  std::vector<Result> results;
  std::vector<std::string> seen;
  int failed = 0, selected = 0;
  for (const Case& c : cases) {
    const std::string name = c.name != nullptr ? c.name : "";
    bool duplicate = false;
    for (const std::string& s : seen) duplicate = duplicate || s == name;
    seen.push_back(name);
    if (!filter.empty() && !Selected(filter, name, suite + "::" + name)) continue;
    if (selected++ % shards != shard) continue;

    Result r;
    r.name = name;
    r.status = "error";
    r.message = "did not finish: the test binary ended while running this case (a crash in-process, or killed by a timeout)";
    r.seconds = 0;
    const std::string bad_id = CheckRequirement(c.requirement);
    if (bad_id.empty() && c.requirement != nullptr) r.requirement = c.requirement;
    results.push_back(r);
    WriteJUnit(junit, suite, results);  // a killed run still names this case

    std::printf("[ RUN      ] %s::%s\n", suite.c_str(), name.c_str());
    std::fflush(stdout);
    const auto start = std::chrono::steady_clock::now();
    r.status = "passed";
    r.message.clear();
    if (name.empty() || c.fn == nullptr) {
      r.status = "error";
      r.message = "a case needs a name and a function";
    } else if (duplicate) {
      r.status = "error";
      r.message = "duplicate case name: each case of a suite needs its own";
    } else if (!bad_id.empty()) {
      r.status = "error";
      r.message = bad_id;
    } else {
#if defined(RR_INTERNAL_FORK)
      RunForked(c, &r);
#else
      if (!Invoke(c, &r.message)) r.status = "failed";
#endif
    }
    r.seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();
    if (r.status == "passed") {
      std::printf("[       OK ] %s::%s (%.0f ms)\n", suite.c_str(), name.c_str(), r.seconds * 1000);
    } else {
      ++failed;
      std::printf("[  FAILED  ] %s::%s: %s\n", suite.c_str(), name.c_str(), r.message.c_str());
    }
    std::fflush(stdout);
    results.back() = r;
  }
  WriteJUnit(junit, suite, results);
  std::printf("[==========] %s: %u case(s), %d failed\n", suite.c_str(), static_cast<unsigned>(results.size()),
              failed);
  std::fflush(stdout);
  return failed != 0 ? 1 : 0;
}

}  // namespace internal

// Runs the cases defined with RR_CASE, as suite `suite`. Returns the exit
// status for main(): 0 if every selected case passed, 1 if any failed, 2 on
// a bad flag.
inline int RunCases(int argc, char** argv, const char* suite) {
  return internal::Run(argc, argv, suite, internal::Registry());
}

// Runs `cases` (and not the RR_CASE registry), as suite `suite`.
inline int RunCases(int argc, char** argv, const char* suite, std::initializer_list<Case> cases) {
  return internal::Run(argc, argv, suite, std::vector<Case>(cases));
}

}  // namespace rr

#define RR_INTERNAL_EXPAND(x) x
#define RR_INTERNAL_CASE_PICK(a1, a2, a3, a4, a5, macro, ...) macro
#define RR_INTERNAL_CASE_PLAIN(name) RR_INTERNAL_CASE_DEFINE(name, nullptr)
#define RR_INTERNAL_CASE_VERIFIES(name, id) RR_INTERNAL_CASE_DEFINE(name, id)
#define RR_INTERNAL_CASE_TOO_MANY_IDS(name, ...)                                                       \
  static_assert(false, "RR_CASE(" #name ", ...): a test case verifies at most one requirement [RR-E101]"); \
  static void rr_case_fn_##name()
#if defined(__GNUC__) || defined(__clang__)
#define RR_INTERNAL_UNUSED __attribute__((unused))
#else
#define RR_INTERNAL_UNUSED
#endif
#define RR_INTERNAL_CASE_DEFINE(name, id)                                         \
  static void rr_case_fn_##name();                                                \
  RR_INTERNAL_UNUSED static const ::rr::internal::Registrar rr_case_registrar_##name( \
      ::rr::Case(#name, &rr_case_fn_##name, id));                                 \
  static void rr_case_fn_##name()

#endif  // RULES_REQUIREMENTS_RR_CASE_H_
