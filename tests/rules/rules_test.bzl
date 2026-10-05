"""Analysis tests of the 0.3 rule surface: rr_model(lock), rr_report(check,
lane, on_attribution_error) and rr_sets_lock_test.

An aspect hands each test the actions of the target under test (as
bazel_skylib's analysistest does), so the tests read the `rr` command line the
report action runs and the arguments baked into the generated mains (`json.encode`: no blanks).
"""

load("//rr:defs.bzl", "RrModelInfo", "RrReportInfo")

_ActionsInfo = provider(doc = "The actions of a target.", fields = ["actions"])

def _actions_aspect_impl(target, _ctx):
    return [_ActionsInfo(actions = target.actions)]

_actions_aspect = aspect(implementation = _actions_aspect_impl)

def _result(problems):
    return [AnalysisTestResultInfo(success = not problems, message = "\n".join(problems))]

def _after(argv, flag):
    """The argument following `flag` in `argv`, or None."""
    for i in range(len(argv) - 1):
        if argv[i] == flag:
            return argv[i + 1]
    return None

def _written(target):
    """The text of the file a FileWrite action of `target` writes (a generated main)."""
    for action in target[_ActionsInfo].actions:
        if action.mnemonic == "FileWrite":
            return action.content
    return ""

def _model_lock_test_impl(ctx):
    problems = []
    model = ctx.attr.model
    info = model[RrModelInfo]
    if info.lock == None or info.lock.basename != "verification.rrlock":
        problems.append("RrModelInfo.lock is %s, expected verification.rrlock" % info.lock)
    files = [f.basename for f in model[DefaultInfo].files.to_list()]
    if "verification.rrlock" in files:
        problems.append("the lock is in the model's files %s; it must ride in the runfiles only" % files)
    runfiles = [f.basename for f in model[DefaultInfo].default_runfiles.files.to_list()]
    if "verification.rrlock" not in runfiles:
        problems.append("the lock is not in the model's runfiles %s" % runfiles)
    main = _written(ctx.attr.validate_main)
    if '"validate"' not in main or '"--sets-lock","tests/rules/verification.rrlock"' not in main:
        problems.append("<model>_test does not validate with --sets-lock: %s" % main)
    return _result(problems)

model_lock_test = rule(
    implementation = _model_lock_test_impl,
    analysis_test = True,
    attrs = {
        "model": attr.label(providers = [RrModelInfo]),
        "validate_main": attr.label(aspects = [_actions_aspect]),
    },
)

def _report_test_impl(ctx):
    problems = []
    report = ctx.attr.report
    runs = [a for a in report[_ActionsInfo].actions if a.mnemonic == "RrReport"]
    if len(runs) != 1:
        return _result(["expected one RrReport action, got %d" % len(runs)])
    argv = runs[0].argv
    for flag, want in (
        ("--on-attribution-error", ctx.attr.on_attribution_error),
        ("--lane", ctx.attr.lane or None),
    ):
        got = _after(argv, flag)
        if got != want:
            problems.append("%s: expected %s, got %s (argv %s)" % (flag, want, got, argv))
    lock = _after(argv, "--sets-lock")
    if (lock or "").endswith("tests/rules/verification.rrlock") != ctx.attr.expect_lock:
        problems.append("--sets-lock: got %s (expect lock: %s)" % (lock, ctx.attr.expect_lock))
    if ctx.attr.lane and not (_after(argv, "--lane-targets") or "").endswith("lane.txt"):
        problems.append("--lane-targets: got %s" % _after(argv, "--lane-targets"))
    info = report[RrReportInfo]
    if info.on_attribution_error != ctx.attr.on_attribution_error or info.lane != ctx.attr.lane:
        problems.append("RrReportInfo: %s" % info)
    if info.json == None or not info.json.basename.endswith(".json"):
        problems.append("RrReportInfo.json: %s" % info.json)
    if ctx.attr.check_main:
        main = _written(ctx.attr.check_main)
        if '"check-report"' not in main or ".json" not in main:
            problems.append("<report>_check_test does not run rr check-report on the JSON: %s" % main)
    return _result(problems)

report_test = rule(
    implementation = _report_test_impl,
    analysis_test = True,
    attrs = {
        "report": attr.label(providers = [RrReportInfo], aspects = [_actions_aspect]),
        "check_main": attr.label(aspects = [_actions_aspect]),
        "lane": attr.string(),
        "on_attribution_error": attr.string(),
        "expect_lock": attr.bool(),
    },
)

def _sets_lock_test_test_impl(ctx):
    problems = []
    check = _written(ctx.attr.check_main)
    update = _written(ctx.attr.update_main)
    if '"sets","check"' not in check or '"--sets-lock","tests/rules/verification.rrlock"' not in check:
        problems.append("the test does not run rr sets check on the lock: %s" % check)
    if '"sets","lock","--write"' not in update or '"--out","tests/rules/verification.rrlock"' not in update:
        problems.append("the .update binary does not rewrite the source lock: %s" % update)
    return _result(problems)

sets_lock_test_test = rule(
    implementation = _sets_lock_test_test_impl,
    analysis_test = True,
    attrs = {
        "check_main": attr.label(aspects = [_actions_aspect]),
        "update_main": attr.label(aspects = [_actions_aspect]),
    },
)
