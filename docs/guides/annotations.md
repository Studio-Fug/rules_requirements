# Source annotations

Annotations tie model entities to the code that implements and verifies them.
They make the report say not only *that* a requirement is verified but *where*
it lives — and they fail the build when code refers to an id that no longer
exists.

## Syntax

The universal form is a tag in a comment or docstring:

```python
# @rr(REQ-0001): Implements isolated access to secure data
class SecureStore: ...
```

```cpp
// @rr.verifies(REQ-0002, REQ-0003)
TEST(Parser, RejectsEmpty) { ... }
```

| Form | Relation |
| ---- | -------- |
| `@rr(ID, ...)` | *implements* in production code, *verifies* in test files |
| `@rr.implements(ID, ...)` | implements |
| `@rr.verifies(ID, ...)` | verifies |
| `@pytest.mark.rr(...)`, `@pytest.mark.requirements(...)` | verifies |
| `rr::verifies!(...)` (Rust) | verifies |
| `RR_VERIFIES(...)` (googletest) | verifies |

The language hooks ({doc}`hooks`) double as annotations, so a tagged test is
both evidence (at run time) and a verification link (in the source).
`@rr.implements(...)` / `@rr.verifies(...)` are also real Python decorators
(`from rules_requirements import rr`), so the same text works as a comment or
as code.

Details:

- Ids are recognised by the configured prefixes and id pattern; anything else
  inside the parentheses (quotes, `level="hil"`, prose) is ignored. A tag
  without a recognisable id is ignored.
- Text after `):` up to the end of the line is the annotation's
  **description** (a trailing `*/`, `-->`, `"""` or `'''` is dropped).
- Any entity kind can be annotated — `# @rr(MIT-2)` on the function that
  realises a risk control is as useful as a requirement link.

### Which files are tests

`@rr(...)` means *verifies* when the file's path looks like a test: a
`test`, `tests`, `testing`, `spec` or `__tests__` directory component, or a file
name like `test_*`, `*_test.*`, `*_tests.*`, `*.test.*` or `*.spec.*`.
Otherwise it means *implements*. Use the explicit forms where the heuristic is
wrong.

### Symbol binding

Each annotation records the definition it annotates, which the reports show
next to the location (`thermostat/controller.py:27 (class Controller)`):

- a tag on a line of its own binds to the next definition, looking past
  decorators, attributes and comment lines — a blank line or any other code
  ends the search;
- a tag trailing a definition line binds to that definition;
- the in-body hooks (`rr::verifies!`, `RR_VERIFIES`) bind to the nearest
  enclosing definition above them.

Recognised definitions are `def`, `class`, `fn`, `struct`, `enum`, `trait`,
`impl`, `func`, `function`, `interface`, `type`, `module`, `mod` and
`namespace` (with common modifiers such as `pub`, `async`, `export`, `static`),
and googletest's `TEST`, `TEST_F`, `TEST_P` and `TYPED_TEST`, which bind as
`Suite.Name`.

## Legacy conventions

Projects often already have a convention, such as a docstring line
`Requirements: PR-1, PR-2`. Keep it by adding a pattern to the model's
`config:`; capture group 1 holds the id list:

```yaml
config:
  annotation_patterns:
    - 'Requirements:\s*([A-Z0-9,\s-]+)'
```

Matches of extra patterns use the path-based default relation.

## Checking annotations

`rr scan` (alias `check-annotations`) scans the workspace and fails if any
annotation names an undefined id:

```console
$ rr scan --model requirements/ --list        # in examples/thermostat
interlock/interlock.h:7: implements REQ-5 [class Interlock] — Over-temperature interlock, independent of the controller.
interlock/interlock_test.cc:13: verifies REQ-5 [Interlock.TripsAtLimit]
...
setpoint/src/lib.rs:24: implements REQ-3, REQ-4 [fn parse] — Explicit unit required; the result is range-checked in °C.
setpoint/src/lib.rs:46: verifies REQ-3 [fn parses_celsius_and_fahrenheit]
...
thermostat/controller.py:27: implements REQ-1, REQ-2 [class Controller] — Bang-bang control with a ±0.5 °C hysteresis band.
thermostat/display.py:5: implements REQ-7 [def format_setpoint] — The unit is always shown next to the number.
scanned: 18 annotation(s), 21 reference(s)
all references resolve.
```

Which files are scanned:

- In a git checkout, the tracked files plus untracked files that are not
  ignored; otherwise every file under `--root` (skipping dot-directories,
  `bazel-*` and `node_modules`).
- By default only source files: `.py`, `.pyi`, `.rs`, `.c`, `.cc`, `.cpp`,
  `.cxx`, `.h`, `.hh`, `.hpp`, `.hxx`, `.go`, `.java`, `.kt`, `.swift`, `.m`,
  `.mm`, `.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`, `.cs`, `.rb`, `.sh`, `.bzl`,
  `.proto`, `.v`, `.sv`, `.vhd`, `.vue`, `.svelte`, and files named `BUILD`,
  `BUILD.bazel` or `MODULE.bazel`. `--include GLOB` replaces this filter.
- Always skipped: `.git/`, `node_modules/`, `bazel-*`, `third_party/`,
  `_vendor/` and `.venv/`; add more with `--exclude GLOB`.
- `--files F ...` scans exactly the given files (relative to `--root`).
- Files that are not valid UTF-8 are skipped.

`--json FILE` writes every annotation as `{ids, relation, path, line, text,
symbol}`.

In Bazel, {ref}`rr_annotations_test <rr-annotations-test>` runs the same check
hermetically over declared sources, and `rr_report(srcs = ...)` (or
`rr report --scan`) adds the links to the report together with a
`no-implementation` gap for every requirement that no annotation implements.
