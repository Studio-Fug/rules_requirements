"""rr_node_test: node:test files with one JUnit test case per test.

The macro wraps the consumer's own rules_js `js_test` (passed as `rule`, so
rules_requirements does not depend on rules_js). `_rr_node_files` expands the
runner (`//js:node_test_main.cjs.tpl`) and copies the reporter into the
caller's package — rules_js runs entry points from the bin tree — with every
path baked in, so the test also runs unchanged under `rr_evidence`.
"""

def _runfiles_path(ctx, f):
    """`f`'s path below the runfiles root (`_main/pkg/x.js`, `repo+/x.js`)."""
    short = f.short_path
    return short[3:] if short.startswith("../") else ctx.workspace_name + "/" + short

def relpath(path, start):
    """`path` relative to the directory `start` (both `/`-separated, relative).

    Args:
      path: the destination.
      start: the directory to go from.

    Returns:
      The relative path, `..` segments first.
    """
    to = [p for p in path.split("/") if p and p != "."]
    frm = [p for p in start.split("/") if p and p != "."]
    common = 0
    for i in range(min(len(to), len(frm))):
        if to[i] != frm[i]:
            break
        common = i + 1
    return "/".join([".."] * (len(frm) - common) + to[common:]) or "."

def _dirname(path):
    return path.rpartition("/")[0]

def _rr_node_files_impl(ctx):
    main = ctx.outputs.main_out
    reporter = ctx.outputs.reporter_out
    main_dir = _dirname(_runfiles_path(ctx, main))
    test = ctx.file.test
    verifies = ctx.file._verifies
    args = [ctx.expand_location(a, ctx.attr.data) for a in ctx.attr.args]
    values = {
        "ARGS": json.encode(args),
        "LEVEL": ctx.attr.level,
        "REPORTER_REL": relpath(_runfiles_path(ctx, reporter), main_dir),
        "TARGET": ctx.attr.target,
        "TEST_REL": relpath(_runfiles_path(ctx, test), main_dir),
        "TEST_RUNFILES": _runfiles_path(ctx, test),
        # Workspace-relative for the main repository's files (`rr.file`).
        "TEST_SHORT": test.short_path if not test.short_path.startswith("../") else "external/" + test.short_path[3:],
        "VERIFIES_RUNFILES": _runfiles_path(ctx, verifies),
    }
    ctx.actions.expand_template(
        template = ctx.file._main_template,
        output = main,
        # The template's placeholders are string literals: `"{{NAME}}"`.
        substitutions = {'"{{%s}}"' % k: json.encode(v) for k, v in values.items()},
    )
    ctx.actions.expand_template(template = ctx.file._reporter, output = reporter, substitutions = {})
    return [DefaultInfo(
        files = depset([main, reporter]),
        runfiles = ctx.runfiles(files = [main, reporter, test, verifies]),
    )]

rr_node_files = rule(
    implementation = _rr_node_files_impl,
    doc = "Writes rr_node_test's entry point and reporter into the caller's package.",
    attrs = {
        "args": attr.string_list(doc = "Arguments for the test file (`$(location)` is expanded)."),
        "data": attr.label_list(allow_files = True, doc = "Targets `args` may reference."),
        "level": attr.string(doc = "Default level for cases that do not declare one."),
        "main_out": attr.output(mandatory = True),
        "reporter_out": attr.output(mandatory = True),
        "target": attr.string(mandatory = True, doc = "The test's label; names the JUnit suite."),
        "test": attr.label(allow_single_file = True, mandatory = True, doc = "The node:test file to run."),
        "_main_template": attr.label(default = "//js:node_test_main.cjs.tpl", allow_single_file = True),
        "_reporter": attr.label(default = "//js:rr_node_reporter.mjs", allow_single_file = True),
        "_verifies": attr.label(default = "//js:verifies.cjs", allow_single_file = True),
    },
)

def rr_node_test(name, rule, test, data = [], args = [], level = "", **kwargs):
    """A node:test file run with one JUnit test case per `test()` / `it()`.

    Pass `rule = js_test` (from `@aspect_rules_js//js:defs.bzl`) and the test
    file — usually compiled JavaScript such as `:dist-test/tests/x.test.js`.
    The test runs exactly as a plain `js_test` would run it (`node <file>`,
    the same exit code); the entry point adds a reporter and writes JUnit:

    - one case per leaf test: classname `<file stem>[ > describe...]`, the
      test's name, passed / failed / skipped (`skip` and `todo`);
    - `rr.requirement=<id>` / `rr.level=<level>` / `rr.artifact.<key>=<value>`
      diagnostics (`t.diagnostic(...)`, or `verifies(t, id)` from
      `@rules_requirements//js:verifies.cjs`) as that case's properties;
    - target-scope error cases (`rr.scope=target`) for failures outside any
      test: `<load>`, `<exit-status>`, a root `after()` hook (`<file>`), a describe's or
      parent test's own failure (`<hooks>`).

    Args:
      name: test name.
      rule: the `js_test` rule from rules_js.
      test: the node:test file to run.
      data: runtime data (e.g. the compiled sources and their package.json).
      args: arguments for the test file; baked into the entry point so they
        also apply under rr_evidence (`$(location)` of `data` is expanded).
      level: default verification level for the cases.
      **kwargs: forwarded to `rule` (size, tags, env, ...).
    """
    main = name + ".rr_node_main.cjs"
    reporter = name + ".rr_node_reporter.mjs"
    rr_node_files(
        name = name + ".rr_node",
        test = test,
        data = data,
        args = args,
        level = level,
        target = "//%s:%s" % (native.package_name(), name),
        main_out = main,
        reporter_out = reporter,
        testonly = True,
        tags = ["manual"],
        visibility = ["//visibility:private"],
    )
    rule(
        name = name,
        entry_point = main,
        data = data + [test, ":" + name + ".rr_node"],
        **kwargs
    )
