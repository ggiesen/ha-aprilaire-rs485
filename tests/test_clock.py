# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for the clock-sync TIME/DATE formatting."""

from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")
pytest.importorskip("homeassistant")

from custom_components.aprilaire_rs485.coordinator import _format_clock


@pytest.mark.parametrize(
    ("value", "expected_time", "expected_date"),
    [
        # Zero-padding of hour/minute and month/day, and 24-hour time.
        (datetime(2026, 3, 7, 9, 5), "0905", "030726"),
        (datetime(2026, 12, 25, 23, 59), "2359", "122526"),
        # Midnight and a year ending in 00.
        (datetime(2026, 1, 1, 0, 0), "0000", "010126"),
        (datetime(2000, 6, 15, 13, 7), "1307", "061500"),
    ],
)
def test_format_clock(value: datetime, expected_time: str, expected_date: str) -> None:
    """_format_clock renders hhmm / mmddyy with zero-padding and 2-digit year."""
    assert _format_clock(value) == (expected_time, expected_date)
