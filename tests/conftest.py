# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Shared fixtures and path setup for the Aprilaire 8800 tests.

This integration's tests come in two flavours:

* ``test_protocol.py`` and ``test_coordinator.py`` - pure-Python tests with no
  Home Assistant dependency. They only need ``pyserial`` and can be run
  standalone.

* HA-aware tests (``test_config_flow.py``, ``test_services.py``,
  ``test_text.py``) - require the ``pytest-homeassistant-custom-component``
  plugin. They use the ``hass`` fixture provided by that plugin.

For the protocol-only tests we add the integration's directory to
``sys.path`` so that ``from protocol import ...`` works without importing the
package ``__init__.py`` (which imports ``homeassistant``).
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTEGRATION_DIR = _REPO_ROOT / "custom_components" / "aprilaire_rs485"
sys.path.insert(0, str(_INTEGRATION_DIR))

# Activate the HA test plugin when it's available. If the plugin isn't
# installed (protocol-only test runs), tests that need it will skip via
# their own ``pytest.importorskip`` calls; importing it here would crash
# the whole session, hence the try/except.
try:
    import pytest_homeassistant_custom_component  # noqa: F401

    pytest_plugins = ["pytest_homeassistant_custom_component"]
except ImportError:
    pass
