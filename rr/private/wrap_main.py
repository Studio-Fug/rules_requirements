# SPDX-License-Identifier: AGPL-3.0-or-later
"""Entry point for ``rr_wrapped_test`` (see rules_requirements.hooks.wrap)."""

from rules_requirements.hooks.wrap import main

if __name__ == "__main__":
    raise SystemExit(main())
