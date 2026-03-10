"""Tests for the system health monitor module."""

from __future__ import annotations

from typing import TYPE_CHECKING

from helmlog.monitor import _DEFAULT_INTERVAL_S, _get_interval

if TYPE_CHECKING:
    import pytest


class TestGetInterval:
    """_get_interval reads MONITOR_INTERVAL_S from env with clamping."""

    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MONITOR_INTERVAL_S", raising=False)
        assert _get_interval() == _DEFAULT_INTERVAL_S

    def test_reads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MONITOR_INTERVAL_S", "10")
        assert _get_interval() == 10

    def test_clamps_low(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MONITOR_INTERVAL_S", "0")
        assert _get_interval() == 1

    def test_clamps_high(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MONITOR_INTERVAL_S", "999")
        assert _get_interval() == 300

    def test_invalid_value_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MONITOR_INTERVAL_S", "abc")
        assert _get_interval() == _DEFAULT_INTERVAL_S

    def test_empty_string_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MONITOR_INTERVAL_S", "")
        assert _get_interval() == _DEFAULT_INTERVAL_S
