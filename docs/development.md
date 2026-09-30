# Development

Contributions are welcome through pull requests on
[GitHub](https://github.com/Studio-Fug/rules_requirements).

## Layout

| Path | What |
| ---- | ---- |
| `python/rules_requirements/` | The toolkit: model, validation, ingestion, tracing, reports, hooks, CLI. |
| `python/rules_requirements/_vendor/yaml/` | Vendored PyYAML (pure-Python part, MIT). |
| `python/tests/` | Unit tests (pytest). |
| `rr/defs.bzl`, `rr/private/` | Bazel rules and macros. |
| `cc/rr_gtest.h` | googletest hook. |
| `rust/` | Rust hook crate (`rr`). |
| `schema/` | JSON Schema for model files. |
| `tests/integration/` | Every hook → evidence → report, pinned by goldens. |
| `examples/thermostat/` | The {doc}`tutorial` project (a separate Bazel module). |
| `docs/` | This site. |

## Python tests and coverage

```console
$ pip install -e ".[test]"
$ pytest
$ coverage run -m pytest && coverage report
```

Measure coverage with `coverage run` rather than `pytest --cov`: the installed
package registers a `pytest11` plugin, which pytest imports before pytest-cov
starts measuring. CI requires at least 90 % line and branch coverage.

## Bazel

```console
$ bazel test //...                       # unit tests + hook integration goldens
$ (cd examples/thermostat && bazel test //...)
```

The root module and the example are separate Bazel modules (`.bazelignore`
keeps `examples/` out of the root). Local settings — an output base, a disk
cache — belong in an untracked `user.bazelrc`, which `.bazelrc` imports if it
exists.

The unit tests run under Bazel with their own pip hub, `rr_dev_pip`, locked in
`python/requirements_dev.lock`; regenerate it with
`bazel run //python:requirements_dev.update`.

When a change intentionally alters a report, regenerate the goldens and review
the diff:

```console
$ bazel run //tests/integration:report_json_golden_test.update
$ bazel run //tests/integration:report_md_golden_test.update
```

(and the same targets in `examples/thermostat`).

## Presubmit checks

```console
$ pip install pre-commit && pre-commit run --all-files
```

| Hook | Checks |
| ---- | ------ |
| pre-commit-hooks | trailing whitespace, final newlines, YAML/JSON/TOML syntax, merge conflicts, large files, LF line endings |
| ruff | lint and formatting (`pyproject.toml`) |
| mypy | strict type checking of `python/rules_requirements` |
| buildifier | formatting and lint of Starlark |
| codespell | spelling |
| SPDX header | every first-party source file starts with `SPDX-License-Identifier: AGPL-3.0-or-later` |

## Continuous integration

`ci.yaml` runs on every push and pull request:

- **Lint** — the presubmit checks above.
- **Python 3.9–3.13** — the unit tests, and the coverage gate.
- **Bazel** — `bazel test //...` with Bazel 7.7.1 and 8.8.1 on Linux, and 8.8.1
  on macOS.
- **Example** — the thermostat's tests and report with Bazel 7.7.1 and 8.8.1;
  the rendered report is uploaded as an artifact.
- **Python package** — builds the wheel and smoke-tests it in a clean
  environment.

`docs.yaml` builds this site on every push and pull request; on `main` it also
measures coverage for the badge and publishes the site to GitHub Pages.

## Documentation

```console
$ pip install -r docs/requirements.txt -e .
$ python -m sphinx -W -n -b html docs docs/_build/html
```

The pages are MyST Markdown. The CLI reference is generated from the argument
parser and the API reference from docstrings; the trace graphs are drawn at
build time by the toolkit itself.

## License

rules_requirements is licensed under the
[GNU Affero General Public License v3.0 or later](https://github.com/Studio-Fug/rules_requirements/blob/main/LICENSE).
The vendored copy of PyYAML is distributed under its MIT license
(`python/rules_requirements/_vendor/yaml/LICENSE`).
