"""Tests for src/helmlog/usb_audio.py — multi-channel USB device detection.

Both the Linux (pyudev) and darwin (sounddevice) paths are mocked so the suite
runs cleanly on Mac dev machines per CLAUDE.md.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from helmlog.usb_audio import (
    DetectedDevice,
    detect_multi_channel_device,
    detect_via_sounddevice,
)

# ---------------------------------------------------------------------------
# DetectedDevice
# ---------------------------------------------------------------------------


def test_detected_device_identity_tuple() -> None:
    d = DetectedDevice(
        vendor_id=0x1234,
        product_id=0x5678,
        serial="ABC",
        usb_port_path="1-1.2",
        max_channels=4,
        sounddevice_index=2,
        name="Lavalier4",
    )
    assert d.identity() == (0x1234, 0x5678, "ABC", "1-1.2")


# ---------------------------------------------------------------------------
# darwin / sounddevice fallback
# ---------------------------------------------------------------------------


_FAKE_SD_DEVICES_4CH = [
    {"name": "Built-in", "max_input_channels": 2, "max_output_channels": 0},
    {"name": "Lavalier 4-ch USB", "max_input_channels": 4, "max_output_channels": 0},
    {"name": "HDMI", "max_input_channels": 0, "max_output_channels": 2},
]

_FAKE_SD_DEVICES_MONO_ONLY = [
    {"name": "Built-in", "max_input_channels": 2, "max_output_channels": 0},
    {"name": "Gordik USB", "max_input_channels": 1, "max_output_channels": 0},
]


def test_detect_via_sounddevice_picks_highest_channel_input() -> None:
    with patch("sounddevice.query_devices", return_value=_FAKE_SD_DEVICES_4CH):
        result = detect_via_sounddevice(min_channels=4)
    assert result is not None
    assert result.max_channels == 4
    assert result.sounddevice_index == 1
    assert "Lavalier" in result.name
    # darwin can't see vendor/product/serial via sounddevice — use empty/zero
    assert result.vendor_id == 0
    assert result.product_id == 0
    assert result.serial == ""
    assert result.usb_port_path == ""


def test_detect_via_sounddevice_returns_none_when_below_threshold() -> None:
    with patch("sounddevice.query_devices", return_value=_FAKE_SD_DEVICES_MONO_ONLY):
        result = detect_via_sounddevice(min_channels=4)
    assert result is None


def test_detect_via_sounddevice_min_one_finds_anything() -> None:
    with patch("sounddevice.query_devices", return_value=_FAKE_SD_DEVICES_MONO_ONLY):
        result = detect_via_sounddevice(min_channels=1)
    assert result is not None
    assert result.max_channels == 2


# ---------------------------------------------------------------------------
# Linux / pyudev path
# ---------------------------------------------------------------------------


def _fake_udev_device(
    vendor: str = "1234",
    product: str = "5678",
    serial: str = "ABC123",
    devpath: str = "1-1.2",
) -> SimpleNamespace:
    """A fake pyudev.Device with the attributes our code reads."""
    attrs = {
        "ID_VENDOR_ID": vendor,
        "ID_MODEL_ID": product,
        "ID_SERIAL_SHORT": serial,
    }
    return SimpleNamespace(
        properties=attrs,
        get=lambda k, default=None: attrs.get(k, default),
        sys_name=devpath,
    )


def test_detect_via_pyudev_matches_4ch_device() -> None:
    """Linux path: pyudev enumerates a 4-ch USB audio device."""
    fake_device = _fake_udev_device()
    fake_context = MagicMock()
    fake_context.list_devices.return_value = [fake_device]

    fake_pyudev = MagicMock()
    fake_pyudev.Context.return_value = fake_context

    fake_sd_devices = [
        {"name": "Lavalier 4-ch USB", "max_input_channels": 4, "max_output_channels": 0},
    ]

    with (
        patch.dict(sys.modules, {"pyudev": fake_pyudev}),
        patch("sounddevice.query_devices", return_value=fake_sd_devices),
        patch("helmlog.usb_audio._is_linux", return_value=True),
    ):
        from helmlog.usb_audio import detect_via_pyudev

        result = detect_via_pyudev(min_channels=4)

    assert result is not None
    assert result.vendor_id == 0x1234
    assert result.product_id == 0x5678
    assert result.serial == "ABC123"
    assert result.usb_port_path == "1-1.2"
    assert result.max_channels == 4


def test_detect_via_pyudev_returns_none_when_no_4ch_device() -> None:
    fake_context = MagicMock()
    fake_context.list_devices.return_value = []
    fake_pyudev = MagicMock()
    fake_pyudev.Context.return_value = fake_context

    with (
        patch.dict(sys.modules, {"pyudev": fake_pyudev}),
        patch("sounddevice.query_devices", return_value=[]),
        patch("helmlog.usb_audio._is_linux", return_value=True),
    ):
        from helmlog.usb_audio import detect_via_pyudev

        result = detect_via_pyudev(min_channels=4)

    assert result is None


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------


def test_detect_multi_channel_device_uses_sounddevice_on_darwin() -> None:
    """On darwin the dispatcher should fall through to the sounddevice stub."""
    with (
        patch("helmlog.usb_audio._is_linux", return_value=False),
        patch("sounddevice.query_devices", return_value=_FAKE_SD_DEVICES_4CH),
    ):
        result = detect_multi_channel_device(min_channels=4)
    assert result is not None
    assert result.max_channels == 4


def test_detect_multi_channel_device_returns_none_when_nothing_found() -> None:
    with (
        patch("helmlog.usb_audio._is_linux", return_value=False),
        patch("sounddevice.query_devices", return_value=_FAKE_SD_DEVICES_MONO_ONLY),
    ):
        result = detect_multi_channel_device(min_channels=4)
    assert result is None


@pytest.mark.parametrize(
    ("vendor_hex", "expected"),
    [("1234", 0x1234), ("0abc", 0x0ABC), ("FFFF", 0xFFFF)],
)
def test_pyudev_vendor_hex_parsing(vendor_hex: str, expected: int) -> None:
    fake_device = _fake_udev_device(vendor=vendor_hex)
    fake_context = MagicMock()
    fake_context.list_devices.return_value = [fake_device]
    fake_pyudev = MagicMock()
    fake_pyudev.Context.return_value = fake_context

    with (
        patch.dict(sys.modules, {"pyudev": fake_pyudev}),
        patch(
            "sounddevice.query_devices",
            return_value=[{"name": "x", "max_input_channels": 4, "max_output_channels": 0}],
        ),
        patch("helmlog.usb_audio._is_linux", return_value=True),
    ):
        from helmlog.usb_audio import detect_via_pyudev

        result = detect_via_pyudev(min_channels=4)
    assert result is not None
    assert result.vendor_id == expected
