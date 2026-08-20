"""Shared fixtures.

Everything here drives tests/fake_squeezelite.py, so the suite needs no
PipeWire, no DAC and no root -- it runs on a stock CI runner.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL = os.path.join(ROOT, "panel")
FAKE = os.path.join(ROOT, "tests", "fake_squeezelite.py")
sys.path.insert(0, PANEL)

import players  # noqa: E402  (needs the path above)


@pytest.fixture
def fast(monkeypatch):
    """Collapse the real-world timings so the suite runs in seconds."""
    monkeypatch.setattr(players, "SQUEEZELITE", FAKE)
    monkeypatch.setattr(players, "RETRY_START", 0.05)
    monkeypatch.setattr(players, "RETRY_MAX", 0.2)
    monkeypatch.setattr(players, "HEALTHY_AFTER", 0.5)
    monkeypatch.setattr(players, "HEALTH_INTERVAL", 0.05)
    monkeypatch.setattr(players, "SINK_GRACE", 0.3)
    monkeypatch.setattr(players, "NODE_WAIT_SECONDS", 0.5)
    # sink_present is stubbed, but _prepare falls back to "cannot check, let
    # squeezelite try" when pw-dump is absent -- which it is in CI. Point at a
    # binary that exists so the readiness check is exercised, not skipped.
    monkeypatch.setattr(players, "PW_DUMP", "sh")
    return monkeypatch


@pytest.fixture
def supervisor(tmp_path, fast):
    # No PipeWire in CI: every node is "present" unless a test says otherwise.
    fast.setattr(players, "sink_present", lambda node: True)
    fast.setattr(players, "list_sinks", lambda: [])
    fast.setattr(players, "set_sink_volume", lambda node, volume: None)
    sup = players.Supervisor(config_path=str(tmp_path / "players.json"))
    yield sup
    sup.stop_all()
