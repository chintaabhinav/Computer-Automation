"""Marks the repository root for pytest so tests can import the `src` package.

Present largely for its location: under pytest's default import mode the directory containing a
conftest.py is prepended to sys.path, which is what makes `from src.artifact...` resolve when
tests run from anywhere in the tree.
"""

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Register the `integration` marker.

    Integration tests drive a real browser against a real server, so they are slower and need
    Chromium installed. Marking them keeps `pytest -m "not integration"` a fast, dependency-free
    check while the full suite stays the default.
    """
    config.addinivalue_line(
        "markers",
        "integration: exercises a real browser against the locally served mock app",
    )
