"""Markers for the release tests."""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        'build: runs a real `uv build` (a few seconds); included in CI\'s `-m "not live"` run',
    )
