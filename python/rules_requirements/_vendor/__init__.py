# SPDX-License-Identifier: AGPL-3.0-or-later
"""Vendored third-party code, so the core has zero runtime dependencies.

* ``yaml`` — PyYAML 6.0.2 (MIT, see ``yaml/LICENSE``), pure-Python part only.
  ``cyaml.py`` was removed so the vendored copy can never import a system
  ``yaml._yaml`` C extension belonging to a different PyYAML install.
  Otherwise unmodified.
"""
