"""Public Bazel API of rules_requirements.

```starlark
load("@rules_requirements//rr:defs.bzl", "rr_model", "rr_py_test", "rr_report")
```

Model:
  * `rr_model`            — declare the model files; also creates `<name>_test`
                            (validation) unless `validate = False`.
  * `rr_annotations_test` — fail on source annotations referencing unknown ids.
  * `rr_annotations_check`— the same over the whole workspace, via `bazel run`.
  * `rr_editor`           — `bazel run` target for the web editor (`rr serve`).

Test hooks:
  * `rr_py_test`          — pytest with `@pytest.mark.rr(...)` traceability JUnit.
  * `rr_wrapped_test`     — run a test binary and convert its output (libtest).
  * `rr_rust_test`        — `rust_test` + wrapper, for `rr::verifies!(...)`.
  (googletest needs no wrapper: depend on `@rules_requirements//cc:gtest`.)

Reports:
  * `rr_evidence`         — run tests inside a build action, collecting JUnit.
  * `rr_report`           — model + evidence -> HTML / JSON / Markdown report.
  * `rr_golden_test`      — compare a generated file with a checked-in golden.
"""

load("@rules_python//python:py_binary.bzl", "py_binary")
load("@rules_python//python:py_test.bzl", "py_test")
load(
    "//rr/private:rules.bzl",
    _RrEvidenceInfo = "RrEvidenceInfo",
    _RrModelInfo = "RrModelInfo",
    _rr_evidence = "rr_evidence",
    _rr_main = "rr_main",
    _rr_model = "rr_model_rule",
    _rr_report = "rr_report",
)

RrModelInfo = _RrModelInfo
RrEvidenceInfo = _RrEvidenceInfo
rr_evidence = _rr_evidence
rr_report = _rr_report

_LIB = Label("//python")

def _py(kind, name, entry, baked_args = [], data = [], deps = [], srcs = [], **kwargs):
    """A py_test/py_binary whose generated main bakes in `baked_args`."""
    main = name + ".rr_main.py"
    _rr_main(
        name = name + ".rr_main",
        entry = entry,
        baked_args = baked_args,
        data = data,
        out = main,
        testonly = kwargs.get("testonly", kind == "test"),
        tags = ["manual"],
        visibility = ["//visibility:private"],
    )
    kind_fn = py_test if kind == "test" else py_binary
    kind_fn(
        name = name,
        srcs = srcs + [main],
        main = main,
        data = data,
        deps = deps + [_LIB],
        **kwargs
    )

def rr_model(name, srcs, strict = False, validate = True, visibility = None, **kwargs):
    """Declare requirements model files.

    Args:
      name: target name; other rules take it as `model`.
      srcs: YAML/JSON model files (any layout; they are merged).
      strict: treat validation warnings as errors in `<name>_test`.
      validate: create the `<name>_test` validation test.
      visibility: visibility of the model target.
      **kwargs: forwarded to the validation test (e.g. `tags`).
    """
    _rr_model(name = name, srcs = srcs, visibility = visibility)
    if validate:
        _py(
            "test",
            name + "_test",
            "cli",
            baked_args = ["validate"] + (["--strict"] if strict else []) + ["$(rootpaths :%s)" % name],
            data = [":" + name],
            **kwargs
        )

def rr_annotations_test(name, model, srcs, **kwargs):
    """Fail if any `@rr(...)`-style annotation in `srcs` names an unknown id.

    Args:
      name: test name.
      model: an `rr_model` target.
      srcs: source files to scan (e.g. `glob(["**/*.py"])`).
      **kwargs: forwarded to the underlying `py_test`.
    """
    _py(
        "test",
        name,
        "cli",
        baked_args = ["scan", "--list", "--root", ".", "--model", "$(rootpaths %s)" % model, "--files"] +
                     ["$(rootpaths %s)" % s for s in srcs],
        data = [model] + srcs,
        **kwargs
    )

def rr_annotations_check(name, model, **kwargs):
    """`bazel run` target that scans the whole workspace for annotations.

    Unlike `rr_annotations_test` (hermetic, explicit `srcs`), this walks the
    source tree `bazel run` was invoked in (git-aware) and fails on any
    annotation naming an id the model does not define. Extra `rr scan` flags
    (`--list`, `--exclude GLOB`, ...) can be passed after `--`.

    Args:
      name: target name.
      model: an `rr_model` target.
      **kwargs: forwarded to the underlying `py_binary`.
    """
    _py("binary", name, "cli", baked_args = ["scan", "--model", "$(rootpaths %s)" % model], data = [model], **kwargs)

def rr_editor(name, model, paths = [], evidence = ["bazel-testlogs"], args = [], **kwargs):
    """`bazel run :<name>` opens the web editor (`rr serve`) on the model.

    The editor edits the real files in the workspace (not runfiles copies):
    model paths are taken relative to the workspace root.

    Args:
      name: target name.
      model: an `rr_model` target (its files are what the editor opens).
      paths: workspace-relative directories or files to open instead of the
        model's files — use the model's directory for one-object-per-file
        layouts, so new entities can be created there.
      evidence: workspace-relative evidence paths (default `bazel-testlogs`).
      args: extra `rr serve` arguments (e.g. `--port=9000`, `--no-llm`).
      **kwargs: forwarded to the underlying `py_binary`.
    """
    model_args = ["--model"] + (paths if paths else ["$(rootpaths %s)" % model])
    _py(
        "binary",
        name,
        "cli",
        baked_args = ["serve"] + model_args + ["--evidence"] + evidence + args,
        data = [model],
        **kwargs
    )

def rr_py_test(name, srcs, deps = [], args = [], data = [], **kwargs):
    """A pytest `py_test` whose JUnit carries `@pytest.mark.rr(...)` traces.

    pytest itself is not bundled — add your workspace's pytest to `deps`.
    Files named `test_*.py` / `*_test.py` are collected; other `srcs`
    (e.g. `conftest.py`, helpers) are importable support files.

    Args:
      name: test name.
      srcs: test files (pytest collects them all).
      deps: dependencies, including pytest.
      args: extra pytest arguments (baked in, so they also apply under rr_evidence).
      data: runtime data.
      **kwargs: forwarded to `py_test`.
    """
    tests = [s for s in srcs if s.split("/")[-1].split(":")[-1].startswith("test_") or s.endswith("_test.py")]
    _py(
        "test",
        name,
        "pytest",
        baked_args = ["$(rootpath %s)" % s for s in (tests or srcs)] + args,
        srcs = srcs,
        data = data + srcs,
        deps = deps,
        **kwargs
    )

def rr_wrapped_test(name, test, format = "libtest", level = "", args = [], **kwargs):
    """Run test binary `test` and convert its output to traceability JUnit.

    Args:
      name: test name.
      test: the test executable target (usually tagged `manual`).
      format: output format of the binary (`libtest`).
      level: default verification level for cases without one.
      args: extra arguments for the binary.
      **kwargs: forwarded to `py_test`.
    """
    wrap_args = ["--format", format, "--target", "//%s:%s" % (native.package_name(), name)]
    if level:
        wrap_args += ["--level", level]
    _py(
        "test",
        name,
        "wrap",
        baked_args = wrap_args + ["--", "$(rootpath %s)" % test] + args,
        data = [test],
        **kwargs
    )

def rr_rust_test(name, rule, level = "", tags = [], size = None, timeout = None, flaky = None, args = [], env = {}, env_inherit = [], visibility = None, **kwargs):
    """A Rust test whose `rr::verifies!(...)` calls become JUnit traces.

    Pass `rule = rust_test` (from `@rules_rust//rust:defs.bzl`) and the usual
    `rust_test` attributes; add `@rules_requirements//rust:rr` to `deps`.
    Creates `<name>_bin` (the real `rust_test`, tagged manual) and `<name>`.

    Args:
      name: test name.
      rule: the `rust_test` rule.
      level: default verification level for the cases.
      tags: tags for the wrapper test.
      size: test size.
      timeout: test timeout.
      flaky: flaky flag.
      args: arguments for the test binary (e.g. `--test-threads=1`).
      env: environment for the test run.
      env_inherit: environment variables to inherit from the invoking shell.
      visibility: visibility of the wrapper test.
      **kwargs: forwarded to `rust_test`.
    """
    rule(name = name + "_bin", tags = ["manual"], visibility = ["//visibility:private"], **kwargs)
    extra = {"args": args, "env": env, "env_inherit": env_inherit, "visibility": visibility}
    if size:
        extra["size"] = size
    if timeout:
        extra["timeout"] = timeout
    if flaky != None:
        extra["flaky"] = flaky
    if kwargs.get("testonly") != None:
        extra["testonly"] = kwargs["testonly"]
    rr_wrapped_test(name = name, test = ":%s_bin" % name, level = level, tags = tags, **extra)

def rr_golden_test(name, src, golden, **kwargs):
    """Test that `src` (a generated file) equals the checked-in `golden`.

    `bazel run :<name>.update` rewrites the golden from the current output.

    Args:
      name: test name.
      src: generated file (e.g. `:report.json` from `rr_report`).
      golden: checked-in expected file in this package.
      **kwargs: forwarded to `py_test`.
    """
    golden_path = "%s/%s" % (native.package_name(), golden) if native.package_name() else golden
    common = ["--actual", "$(rootpath %s)" % src, "--golden", golden_path]
    _py("test", name, "bazel", baked_args = ["golden"] + common, data = [src, golden], **kwargs)
    _py(
        "binary",
        name + ".update",
        "bazel",
        baked_args = ["golden", "--update"] + common,
        data = [src, golden],
        tags = ["manual"],
        testonly = True,
    )
