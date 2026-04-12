"""USB audio device detection for multi-channel recording (#462 pt.2).

Hardware isolation: this module is the only place that touches ``pyudev`` or
``sounddevice`` for the purpose of *discovering* a multi-channel device. The
recording layer in ``audio.py`` consumes the ``DetectedDevice`` value object.

Two detection paths:

* **Linux** — ``pyudev`` walks the USB tree to extract a stable device
  identity ``(vendor_id, product_id, serial, usb_port_path)`` that the
  ``channel_map`` table is keyed on. We then cross-reference ``sounddevice``
  to learn the channel count and host index.
* **darwin / dev** — ``sounddevice`` only. macOS does not expose USB
  vendor/product/serial via PortAudio, so the identity tuple is filled with
  zeros/empty strings. This is a *dev convenience* path; production runs on
  Linux where the real identity is available.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from loguru import logger

# ---------------------------------------------------------------------------
# Value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DetectedDevice:
    """A USB audio input device discovered for multi-channel recording.

    The ``identity()`` tuple matches the composite key on the ``channel_map``
    table introduced in #493 / schema v63 — call it to look up the channel
    map for this physical device.
    """

    vendor_id: int
    product_id: int
    serial: str
    usb_port_path: str
    max_channels: int
    sounddevice_index: int
    name: str

    def identity(self) -> tuple[int, int, str, str]:
        return (self.vendor_id, self.product_id, self.serial, self.usb_port_path)


# ---------------------------------------------------------------------------
# Platform gate
# ---------------------------------------------------------------------------


def _is_linux() -> bool:
    """Indirected for tests so the Linux path can be exercised on darwin."""
    return sys.platform.startswith("linux")


# ---------------------------------------------------------------------------
# darwin / dev fallback — sounddevice enumeration only
# ---------------------------------------------------------------------------


def detect_via_sounddevice(*, min_channels: int) -> DetectedDevice | None:
    """Pick the highest-channel input device visible to PortAudio.

    Returns ``None`` if no device meets ``min_channels``. Vendor/product/serial
    are zero/empty because PortAudio does not expose them on macOS.
    """
    import sounddevice as sd

    devices = sd.query_devices()
    best: DetectedDevice | None = None
    for idx, dev in enumerate(devices):
        max_in = int(dev["max_input_channels"])
        if max_in < min_channels:
            continue
        if best is None or max_in > best.max_channels:
            best = DetectedDevice(
                vendor_id=0,
                product_id=0,
                serial="",
                usb_port_path="",
                max_channels=max_in,
                sounddevice_index=idx,
                name=str(dev["name"]),
            )
    return best


# ---------------------------------------------------------------------------
# Linux — pyudev USB walk + sounddevice cross-reference
# ---------------------------------------------------------------------------


def _parse_hex(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(value, 16)
    except ValueError:
        return 0


def detect_via_pyudev(*, min_channels: int) -> DetectedDevice | None:
    """Walk the USB tree via ``pyudev`` and match to a sounddevice entry.

    Only callable on Linux. Returns ``None`` if no device is found that meets
    ``min_channels``. Failure to import ``pyudev`` (e.g. missing libudev) is
    treated as "no device" rather than raising — the caller can fall back to
    the mono recording path.
    """
    try:
        import pyudev  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised only on broken Linux
        logger.warning("pyudev unavailable, multi-channel detection skipped: {}", exc)
        return None

    try:
        context = pyudev.Context()
    except Exception as exc:  # pragma: no cover - libudev missing
        logger.warning("pyudev Context() failed: {}", exc)
        return None

    import sounddevice as sd

    sd_devices = sd.query_devices()
    # Find the first sounddevice entry that has >= min_channels inputs.
    sd_match: tuple[int, dict[str, object]] | None = None
    for idx, dev in enumerate(sd_devices):
        if int(dev["max_input_channels"]) >= min_channels:
            sd_match = (idx, dev)
            break
    if sd_match is None:
        return None
    sd_idx, sd_dev = sd_match

    # Walk USB sound devices for vendor/product/serial.
    try:
        usb_iter = context.list_devices(subsystem="sound", ID_BUS="usb")
    except TypeError:
        usb_iter = context.list_devices(subsystem="sound")

    for udev in usb_iter:
        vendor = udev.get("ID_VENDOR_ID", None)
        product = udev.get("ID_MODEL_ID", None)
        if not vendor or not product:
            continue
        serial = udev.get("ID_SERIAL_SHORT", "") or ""
        usb_port_path = str(getattr(udev, "sys_name", "") or "")
        return DetectedDevice(
            vendor_id=_parse_hex(vendor),
            product_id=_parse_hex(product),
            serial=serial,
            usb_port_path=usb_port_path,
            max_channels=int(sd_dev["max_input_channels"]),  # type: ignore[call-overload]
            sounddevice_index=sd_idx,
            name=str(sd_dev["name"]),
        )
    return None


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------


def detect_multi_channel_device(*, min_channels: int = 4) -> DetectedDevice | None:
    """Detect a multi-channel USB audio input device.

    Linux uses ``pyudev`` for stable identity; darwin falls back to
    ``sounddevice`` enumeration with empty identity fields. Returns ``None``
    if no device meets ``min_channels``, in which case the caller should fall
    back to mono recording.
    """
    if _is_linux():
        return detect_via_pyudev(min_channels=min_channels)
    return detect_via_sounddevice(min_channels=min_channels)
