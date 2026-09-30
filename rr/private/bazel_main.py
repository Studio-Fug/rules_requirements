# SPDX-License-Identifier: AGPL-3.0-or-later
"""Entry point for the build-time helpers behind the Bazel rules."""

from rules_requirements.bazel import main

if __name__ == "__main__":
    raise SystemExit(main())
