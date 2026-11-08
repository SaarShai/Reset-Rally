"""Shared pytest fixtures for Reset Rally test suites."""

import datetime as dt
import os
from pathlib import Path
import threading
from zoneinfo import ZoneInfo
import pytest

SHIMS = Path(__file__).parent / "shims"
START = dt.datetime(2026, 11, 8, 17, 30, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()


class FakeClock:
    """Deterministic simulated clock with advance support."""

    def __init__(self, t: float = START) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> FakeClock:
    """Fixture providing an instance of FakeClock."""
    return FakeClock()


@pytest.fixture
def server(tmp_path: Path, clock: FakeClock):
    """Fixture running an HTTP Coordinator server in a background thread."""
    from rally.server import make_server

    srv = make_server(db=tmp_path / "rally.db", host="127.0.0.1", port=0, now=clock)
    th = threading.Thread(target=lambda: srv.serve_forever(poll_interval=0.01), daemon=True)
    th.start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def shims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Configure environment for deterministic CLI shims."""
    monkeypatch.setenv("PATH", f"{SHIMS / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHIM_STATE", str(tmp_path))
    return tmp_path
