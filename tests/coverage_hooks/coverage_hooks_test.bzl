"""Analysis tests: which linkopts @rules_requirements//cc:case adds, per setting.

Each test builds //cc:case under a fixed configuration (`bazel coverage` or
not, `--@rules_requirements//cc:coverage_hooks` on or off) and compares the
gcov link options it propagates with what is expected there.
"""

load("@rules_cc//cc/common:cc_info.bzl", "CcInfo")

_HOOKS = ["--coverage", "-Wl,-u,__gcov_dump", "-Wl,-u,__gcov_reset"]
_COVERAGE = "//command_line_option:collect_code_coverage"
_FLAG = str(Label("//cc:coverage_hooks"))

def _impl(ctx):
    # An attribute with a Starlark transition is a list (of one target here).
    lib = ctx.attr.lib[0]
    flags = []
    for linker_input in lib[CcInfo].linking_context.linker_inputs.to_list():
        flags.extend(linker_input.user_link_flags)
    got = [f for f in flags if f in _HOOKS]
    want = _HOOKS if ctx.attr.expect_hooks else []
    ok = got == want
    message = "" if ok else "linkopts of %s: expected %s, got %s (all: %s)" % (lib.label, want, got, flags)
    return [AnalysisTestResultInfo(success = ok, message = message)]

def _test_rule(settings):
    return rule(
        implementation = _impl,
        analysis_test = True,
        attrs = {
            "lib": attr.label(cfg = analysis_test_transition(settings = settings), providers = [CcInfo]),
            "expect_hooks": attr.bool(),
        },
    )

coverage_hooks_default_test = _test_rule({_COVERAGE: True})
coverage_hooks_on_test = _test_rule({_COVERAGE: True, _FLAG: True})
coverage_hooks_off_test = _test_rule({_COVERAGE: True, _FLAG: False})
no_coverage_test = _test_rule({_COVERAGE: False, _FLAG: True})
