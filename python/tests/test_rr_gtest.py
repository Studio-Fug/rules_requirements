# SPDX-License-Identifier: AGPL-3.0-or-later
"""rr_gtest.h's RR_VERIFIES, compiled against a stand-in for googletest's API
(data/fake_gtest): several ids on one test are recorded, with RR-E101 on stderr."""

import os
import shutil
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
_INCLUDE = os.path.join(_REPO, "cc")
_FAKE = os.path.join(_HERE, "data", "fake_gtest")

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX compiler invocation")

_PROGRAM = r"""
#include "rr_gtest.h"

int main() {
  testing::TestInfo two{"Interlock", "CutsHeaterAtLimit"}, again{"Interlock", "SameIdTwice"},
      calls{"Interlock", "TwoCalls"};
  testing::UnitTest* unit = testing::UnitTest::GetInstance();
  unit->info = &two;
  RR_VERIFIES("REQ-4", "REQ-5");
  unit->info = &again;
  RR_VERIFIES("REQ-1");
  RR_VERIFIES("REQ-1");
  unit->info = &calls;
  RR_VERIFIES("REQ-2");
  RR_VERIFIES("REQ-3");
  RR_VERIFIES("REQ-6");
  return 0;
}
"""


def test_several_ids_on_one_test_warn_once_and_are_all_recorded(tmp_path):
    cxx = os.environ.get("CXX") or shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
    if not cxx or not os.path.exists(os.path.join(_INCLUDE, "rr_gtest.h")):
        pytest.skip("no C++ compiler (or cc/rr_gtest.h) available")
    src = tmp_path / "t.cc"
    src.write_text(_PROGRAM, encoding="utf-8")
    flags = ["-std=c++14", "-Wall", "-Wextra", "-Wconversion", "-Wsign-conversion", "-Werror"]
    build = subprocess.run(
        [cxx, *flags, "-I", _INCLUDE, "-I", _FAKE, str(src), "-o", str(tmp_path / "t")],
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run([str(tmp_path / "t")], capture_output=True, text=True)
    assert run.returncode == 0
    assert run.stdout.splitlines() == [
        "requirements=REQ-4,REQ-5",
        "requirements=REQ-1",
        "requirements=REQ-1,REQ-1",
        "requirements=REQ-2",
        "requirements=REQ-2,REQ-3",
        "requirements=REQ-2,REQ-3,REQ-6",
    ]
    warnings = run.stderr.splitlines()
    assert len(warnings) == 2, run.stderr  # once per test, when it gains a second id
    assert warnings[0].startswith("rr_gtest: warning: Interlock.CutsHeaterAtLimit names REQ-4, REQ-5;")
    assert warnings[1].startswith("rr_gtest: warning: Interlock.TwoCalls names REQ-2, REQ-3;")
    assert all("[RR-E101]" in w for w in warnings)
