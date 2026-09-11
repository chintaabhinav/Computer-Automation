"""Fixtures shared by the integration tests: the served mock app and a browser surface.

Lives here rather than in one test module because both the surface tests and the agent tests
drive the same application, and starting a second Flask process per module would only add
seconds and port contention.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_PATH = REPO_ROOT / "mock_app" / "app.py"
PORT = 5001
BASE_URL = f"http://127.0.0.1:{PORT}"


def _port_is_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="session")
def mock_app() -> Iterator[str]:
    """Serve the mock app for the whole session, reusing an already-running instance.

    Session-scoped because starting Flask per test would dominate the runtime, and the app is
    stateless -- there is nothing for one test to leak into another.
    """
    if _port_is_open(PORT):
        yield BASE_URL  # developer already has it running; leave it alone
        return

    process = subprocess.Popen(
        [sys.executable, str(APP_PATH)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(REPO_ROOT),
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = (process.stdout.read() or b"").decode(errors="replace")
                pytest.fail(f"mock app exited during startup:\n{output}")
            try:
                urllib.request.urlopen(BASE_URL, timeout=0.5).read()
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(0.1)
        else:
            pytest.fail("mock app did not become reachable within 15s")
        yield BASE_URL
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.fixture
def surface(mock_app: str) -> Iterator["object"]:
    """A headless browser surface, closed even when a test fails.

    Imports Playwright lazily so collecting the suite does not require it.
    """
    from src.surface.web import WebSurface

    web_surface = WebSurface(headless=True)
    try:
        yield web_surface
    finally:
        web_surface.close()
