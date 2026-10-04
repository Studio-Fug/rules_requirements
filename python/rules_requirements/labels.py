# SPDX-License-Identifier: AGPL-3.0-or-later
"""One spelling per build target.

The model names targets (``verified_by``, ``config.variants``, the lock) and
evidence files results under them (``bazel-testlogs`` paths, ``rr wrap
--target``, records). The same target has many spellings — ``//p``,
``//p:p``, ``@@//p:p``, ``@splanc//p:p`` from inside splanc, and the
canonical ``@@rules_requirements+//p:n`` (Bazel 8) or
``@@rules_requirements~//p:n`` (Bazel 7) of a module repo — and a claim must
match its evidence whatever spelling either side used, or two spellings of
one target would read as two targets (and could be claimed by two
requirements). :func:`normalize_label` maps every spelling to one:

* the main repository is ``//p:n`` (``@@//``, ``@//`` and ``@<main_repo>//``
  are dropped);
* another module repository is ``@<apparent name>//p:n`` (the canonical
  ``~``/``+`` decoration is dropped, so ``bazel-testlogs/external/<repo>~/``
  paths match a model written with apparent names);
* ``//p`` is ``//p:p`` and ``@r`` is ``@r//:r``;
* the pseudo-targets ``suite:<testsuite name>`` (JUnit outside a testlogs
  tree) and ``record:<stem>`` (records without a target) pass through.

Anything else (``:n``, ``p:n``, a pattern such as ``//p/...``, an empty
string) raises :class:`BadTarget`, reported as ``bad-target``.

Repositories created by module extensions keep their canonical name, always
written ``@@`` (``@@rules_python~~pip~pypi//...``): a test target there needs
an alias in a module repository to be claimed under a stable name.
"""

from __future__ import annotations

import re

PSEUDO_PREFIXES = ("suite:", "record:")

# A module's canonical repo name: ``name+`` (Bazel 8), ``name~`` (Bazel 7.1+)
# or ``name~<version>`` (Bazel 7.0). Extension repos (``a~ext~b``,
# ``a++ext+b``) carry more than one decoration and are left alone.
_MODULE_CANONICAL = re.compile(r"^([A-Za-z][A-Za-z0-9._-]*)(?:\+|~|~[0-9][0-9A-Za-z._-]*)$")
_REPO = re.compile(r"^[A-Za-z0-9_.~+-]*$")
_APPARENT_REPO = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")
# Characters Bazel rejects in package and target names, plus '#' (it separates
# a case key's target from its path) and the pattern wildcard '*'.
_BAD_CHARS = re.compile(r"[\s#*\"'\\\x00-\x1f\x7f]")


class BadTarget(ValueError):  # noqa: N818 - named after the bad-target rule
    """``label`` is not a build label or pseudo-target (rule ``bad-target``)."""


def is_pseudo(label: str) -> bool:
    """Whether ``label`` is a ``suite:`` / ``record:`` pseudo-target."""
    return label.startswith(PSEUDO_PREFIXES)


def is_repo_name(name: str) -> bool:
    """Whether ``name`` can be a ``config.main_repo`` (an apparent repo name)."""
    return bool(_APPARENT_REPO.match(name))


def normalize_label(label: str, main_repo: str = "") -> str:
    """The canonical spelling of ``label``; raises :class:`BadTarget`.

    ``main_repo`` is the apparent name the main repository has in other
    modules (``config.main_repo``), so ``@<main_repo>//p:n`` is ``//p:n``.
    """
    if not isinstance(label, str):
        raise BadTarget(f"{label!r} is not a label")
    text = label.strip()
    if not text:
        raise BadTarget("empty label")
    if is_pseudo(text):
        prefix, _, name = text.partition(":")
        if not name.strip() or "#" in name or "\n" in name:
            raise BadTarget(f"{label!r}: a {prefix}: pseudo-target needs a name without '#' or newlines")
        return text
    repo = ""
    rest = text
    if text.startswith("@"):
        at = 2 if text.startswith("@@") else 1
        repo, sep, tail = text[at:].partition("//")
        if not sep:  # "@r" is "@r//:r"
            if not repo:
                raise BadTarget(f"{label!r}: a label needs '//'")
            tail = ":" + repo
        rest = "//" + tail
        if not _REPO.match(repo):
            raise BadTarget(f"{label!r}: bad repository name {repo!r}")
        m = _MODULE_CANONICAL.match(repo)
        if m:
            repo = m.group(1)
        if repo == main_repo:
            repo = ""
    if not rest.startswith("//"):
        raise BadTarget(f"{label!r}: not an absolute label (write //package:name)")
    body = rest[2:]
    pkg, colon, name = body.partition(":")
    if not colon:
        if not pkg:
            raise BadTarget(f"{label!r}: names no target")
        name = pkg.rsplit("/", 1)[-1]
    if not name:
        raise BadTarget(f"{label!r}: empty target name")
    if pkg.startswith("/") or pkg.endswith("/") or "//" in pkg:
        raise BadTarget(f"{label!r}: malformed package {pkg!r}")
    for part in pkg.split("/") if pkg else ():
        if part in (".", "..", "...") or _BAD_CHARS.search(part):
            raise BadTarget(f"{label!r}: {part!r} is not a package name (a label names one target, not a pattern)")
    if ":" in name or _BAD_CHARS.search(name) or name.startswith("/") or name.endswith("/") or "//" in name:
        raise BadTarget(f"{label!r}: {name!r} is not a target name")
    if any(part in (".", "..") for part in name.split("/")):
        raise BadTarget(f"{label!r}: {name!r} is not a target name")
    # A name still decorated is canonical (apparent names cannot hold ~ or +).
    sigil = "@@" if ("~" in repo or "+" in repo) else "@"
    return f"{sigil + repo if repo else ''}//{pkg}:{name}"


def try_normalize(label: str, main_repo: str = "") -> str | None:
    """:func:`normalize_label`, or ``None`` for a bad label."""
    try:
        return normalize_label(label, main_repo)
    except BadTarget:
        return None


def read_known_targets(text: str) -> tuple[list[str], list[str]]:
    """The labels listed one per line (``bazel query 'tests(//...)'``
    output), as written, and the lines that are not labels. Blank lines and
    ``#`` comments are skipped."""
    known: list[str] = []
    bad: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        (known if try_normalize(line) is not None else bad).append(line)
    return known, bad
