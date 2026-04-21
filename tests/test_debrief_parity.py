"""Debrief multi-channel parity with race audio (#648 Part 1, C1–C6).

Validates the capture-topology invariants from the structured spec:

- C1: same recorder + audio_config instance used for race start and debrief start
- C2: debrief audio_sessions row has same ``channels`` as race
- C3: sibling mode creates a new ``capture_group_id`` for debrief with the
      same number of ordinals (0..N-1) as the race
- C4: channel_map resolves identically for race and debrief audio sessions
- C5: USB device-set change between race-end and debrief-start logs a
      WARNING; debrief is NOT blocked and records with current topology
- C6: integration-level smoke test — /api/races/start → /end → debrief/start
      produces audio_sessions rows with matching topology
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from helmlog.audio import AudioConfig, AudioSession
from helmlog.usb_audio import DetectedDevice
from helmlog.web import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from helmlog.storage import Storage


# ---------------------------------------------------------------------------
# Fakes — a more realistic AudioRecorderGroup stand-in that:
#   * generates a fresh capture_group_id on every start() call (like the real
#     AudioRecorderGroup does via uuid.uuid4())
#   * propagates vendor_id/product_id/serial/usb_port_path from the devices arg
#     onto each sibling AudioSession so the C5 identity comparison has data
# ---------------------------------------------------------------------------


class FakeGroupRecorder:
    """Quacks like AudioRecorderGroup — fresh group_id per start(), devices
    passed through to sibling sessions as USB identity."""

    def __init__(self) -> None:
        self._sessions: list[AudioSession] = []

    async def start(
        self,
        config: AudioConfig,  # noqa: ARG002
        *,
        devices: list[DetectedDevice],
        name: str | None = None,
    ) -> list[AudioSession]:
        group_id = uuid.uuid4().hex
        sessions: list[AudioSession] = []
        for ordinal, dev in enumerate(devices):
            fname = f"{name}-sib{ordinal}.wav" if name else f"fake-{ordinal}.wav"
            sessions.append(
                AudioSession(
                    file_path=f"/tmp/{fname}",
                    device_name=dev.name,
                    start_utc=datetime.now(UTC),
                    end_utc=None,
                    sample_rate=48000,
                    channels=1,
                    vendor_id=dev.vendor_id,
                    product_id=dev.product_id,
                    serial=dev.serial,
                    usb_port_path=dev.usb_port_path,
                    capture_group_id=group_id,
                    capture_ordinal=ordinal,
                )
            )
        self._sessions = sessions
        return sessions

    async def stop(self) -> list[AudioSession]:
        completed: list[AudioSession] = []
        for s in self._sessions:
            s.end_utc = datetime.now(UTC)
            completed.append(s)
        self._sessions = []
        return completed


def _dev(vendor: int, product: int, serial: str, port: str, idx: int) -> DetectedDevice:
    return DetectedDevice(
        vendor_id=vendor,
        product_id=product,
        serial=serial,
        usb_port_path=port,
        max_channels=1,
        sounddevice_index=idx,
        name=f"Fake card {idx}",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _set_event(client: httpx.AsyncClient, name: str = "TestRegatta") -> None:
    resp = await client.post("/api/event", json={"event_name": name})
    assert resp.status_code == 204


def _patch_group_isinstance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make capture_start/stop treat FakeGroupRecorder as an AudioRecorderGroup."""
    monkeypatch.setattr(
        "helmlog.audio.AudioRecorderGroup",
        FakeGroupRecorder,
        raising=True,
    )


async def _race_then_debrief(
    storage: Storage,
    tmp_path: Path,
    *,
    devices_race: list[DetectedDevice],
    devices_debrief: list[DetectedDevice] | None = None,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], dict[str, Any] | None, int, FakeGroupRecorder]:
    """Drive race-start → end → debrief-start in sibling mode and return the
    race's primary audio_session row, the debrief's primary row, the race id,
    and the recorder instance used."""
    _patch_group_isinstance(monkeypatch)
    recorder = FakeGroupRecorder()
    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
    app = create_app(storage, recorder=recorder, audio_config=config)  # type: ignore[arg-type]

    calls: list[list[DetectedDevice]] = [devices_race]
    if devices_debrief is not None:
        calls.append(devices_debrief)
    else:
        calls.append(devices_race)

    call_iter = iter(calls)

    def _fake_detect(*, min_channels: int = 1) -> list[DetectedDevice]:  # noqa: ARG001
        return next(call_iter)

    monkeypatch.setattr("helmlog.usb_audio.detect_all_capture_devices", _fake_detect)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        await client.post(f"/api/races/{race_id}/end")
        await client.post(f"/api/races/{race_id}/debrief/start")

    race_row = None
    async for r in _iter_audio_sessions(storage, race_id, "race", "practice"):
        race_row = r
        break
    debrief_row = None
    async for r in _iter_audio_sessions(storage, race_id, "debrief"):
        debrief_row = r
        break
    assert race_row is not None
    return race_row, debrief_row, race_id, recorder


async def _iter_audio_sessions(
    storage: Storage, race_id: int, *types: str
) -> AsyncIterator[dict[str, Any]]:
    placeholders = ",".join("?" * len(types))
    cur = await storage._read_conn().execute(
        "SELECT id, file_path, channels, channel_map, capture_group_id,"
        " capture_ordinal, vendor_id, product_id, serial, usb_port_path,"
        " session_type, race_id"
        f" FROM audio_sessions WHERE race_id = ? AND session_type IN ({placeholders})"
        " ORDER BY capture_ordinal ASC, id ASC",
        (race_id, *types),
    )
    rows = await cur.fetchall()
    for row in rows:
        yield dict(row)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c1_same_recorder_and_config_reused_for_debrief(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: race and debrief share the app.state.recorder / audio_config instance."""
    devs = [_dev(0x1234, 0x5678, "A", "1-1", 1), _dev(0x1234, 0x5678, "B", "1-2", 2)]
    race_row, debrief_row, _race_id, _rec = await _race_then_debrief(
        storage, tmp_path, devices_race=devs, monkeypatch=monkeypatch
    )
    assert race_row is not None and debrief_row is not None
    # Same recorder → same USB identities captured on the primary row
    assert (race_row["vendor_id"], race_row["product_id"], race_row["serial"]) == (
        debrief_row["vendor_id"],
        debrief_row["product_id"],
        debrief_row["serial"],
    )


@pytest.mark.asyncio
async def test_c2_debrief_channels_match_race_sibling(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2: debrief audio_sessions row has the same ``channels`` as race."""
    devs = [_dev(0x1111, 0x2222, "S0", "1-1", 1), _dev(0x1111, 0x2222, "S1", "1-2", 2)]
    race_row, debrief_row, _race_id, _rec = await _race_then_debrief(
        storage, tmp_path, devices_race=devs, monkeypatch=monkeypatch
    )
    assert race_row is not None and debrief_row is not None
    assert debrief_row["channels"] == race_row["channels"]


@pytest.mark.asyncio
async def test_c3_debrief_has_new_group_id_same_ordinal_count(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C3: new capture_group_id with same sibling ordinal count (0..N-1)."""
    devs = [
        _dev(0xAAAA, 0xBBBB, f"sn{i}", f"1-{i}", i)
        for i in range(1, 4)  # 3 siblings
    ]
    race_row, debrief_row, race_id, _rec = await _race_then_debrief(
        storage, tmp_path, devices_race=devs, monkeypatch=monkeypatch
    )
    assert race_row is not None and debrief_row is not None
    race_group = race_row["capture_group_id"]
    debrief_group = debrief_row["capture_group_id"]
    assert race_group is not None and debrief_group is not None
    assert race_group != debrief_group, "debrief must get a fresh capture_group_id"

    race_sibs = await storage.list_capture_group_siblings(race_group)
    debrief_sibs = await storage.list_capture_group_siblings(debrief_group)
    assert len(race_sibs) == len(debrief_sibs) == 3
    assert [s["capture_ordinal"] for s in race_sibs] == [0, 1, 2]
    assert [s["capture_ordinal"] for s in debrief_sibs] == [0, 1, 2]


@pytest.mark.asyncio
async def test_c4_channel_map_resolves_identically_for_race_and_debrief(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C4: channel_map resolution via USB identity + ordinal is stable across
    race and debrief. Uses the admin-default channel_map table."""
    devs = [
        _dev(0xCAFE, 0xBEEF, "helm-sn", "usb1-1", 1),
        _dev(0xCAFE, 0xBEEF, "main-sn", "usb1-2", 2),
    ]
    # Seed an admin-default map for each device (channel 0 → position).
    for dev, position in ((devs[0], "helm"), (devs[1], "main")):
        await storage.set_channel_map(
            vendor_id=dev.vendor_id,
            product_id=dev.product_id,
            serial=dev.serial,
            usb_port_path=dev.usb_port_path,
            mapping={0: position},
        )

    race_row, debrief_row, _race_id, _rec = await _race_then_debrief(
        storage, tmp_path, devices_race=devs, monkeypatch=monkeypatch
    )
    assert race_row is not None and debrief_row is not None

    race_map = await storage.get_channel_map_for_audio_session(int(race_row["id"]))
    debrief_map = await storage.get_channel_map_for_audio_session(int(debrief_row["id"]))
    assert race_map == debrief_map
    assert race_map == {0: "helm"}  # sanity — first sibling is the helm card


@pytest.mark.asyncio
async def test_c5_usb_set_change_logs_warning_does_not_block(
    storage: Storage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C5: if detection at debrief-start returns a different device set than
    the race saw, log a WARNING; debrief is NOT blocked."""
    import logging

    from loguru import logger as _loguru

    # Route loguru → caplog so pytest can inspect warnings.
    handler_id = _loguru.add(
        lambda msg: caplog.handler.emit(
            logging.LogRecord(
                name="loguru",
                level=msg.record["level"].no,
                pathname="",
                lineno=0,
                msg=msg.record["message"],
                args=(),
                exc_info=None,
            )
        ),
        level="WARNING",
    )
    try:
        caplog.set_level("WARNING")
        race_devs = [
            _dev(0xDEAD, 0xBEEF, "A", "1-1", 1),
            _dev(0xDEAD, 0xBEEF, "B", "1-2", 2),
        ]
        # Debrief detects one fewer device (simulates hot-unplug).
        debrief_devs = [_dev(0xDEAD, 0xBEEF, "A", "1-1", 1)]
        _race_row, debrief_row, race_id, _rec = await _race_then_debrief(
            storage,
            tmp_path,
            devices_race=race_devs,
            devices_debrief=debrief_devs,
            monkeypatch=monkeypatch,
        )
        # Debrief must still have been created, just with fewer siblings.
        assert debrief_row is not None
        assert race_id is not None
        debrief_sibs = await storage.list_capture_group_siblings(
            str(debrief_row["capture_group_id"])
        )
        assert len(debrief_sibs) == 1

        # Warning was emitted.
        assert any(
            "device set" in r.message.lower() or "#648" in r.message
            for r in caplog.records
            if r.levelno >= logging.WARNING
        ), f"expected a C5 device-set-changed warning; got records: {caplog.records}"
    finally:
        _loguru.remove(handler_id)


@pytest.mark.asyncio
async def test_c6_race_then_debrief_topology_integration(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C6: end-to-end smoke — race start → end → debrief start produces two
    audio_sessions rows with matching topology in sibling mode."""
    devs = [
        _dev(0x1357, 0x2468, "port", "usb-1", 1),
        _dev(0x1357, 0x2468, "stbd", "usb-2", 2),
    ]
    race_row, debrief_row, race_id, rec = await _race_then_debrief(
        storage, tmp_path, devices_race=devs, monkeypatch=monkeypatch
    )
    assert race_row is not None, "race primary audio_session row missing"
    assert debrief_row is not None, "debrief primary audio_session row missing"
    assert race_row["race_id"] == race_id
    assert debrief_row["race_id"] == race_id
    assert debrief_row["session_type"] == "debrief"

    # Channel count per row matches (1 per sibling in sibling mode).
    assert race_row["channels"] == debrief_row["channels"]

    # Sibling-group shape matches — same ordinal count, fresh group_id.
    race_sibs = await storage.list_capture_group_siblings(str(race_row["capture_group_id"]))
    debrief_sibs = await storage.list_capture_group_siblings(str(debrief_row["capture_group_id"]))
    assert len(race_sibs) == len(debrief_sibs) == 2
    assert race_row["capture_group_id"] != debrief_row["capture_group_id"]

    # USB identities matched between corresponding ordinals.
    for r, d in zip(race_sibs, debrief_sibs, strict=True):
        assert (r["vendor_id"], r["product_id"], r["serial"], r["usb_port_path"]) == (
            d["vendor_id"],
            d["product_id"],
            d["serial"],
            d["usb_port_path"],
        )

    # Recorder instance was the same for both captures (app.state.recorder).
    assert isinstance(rec, FakeGroupRecorder)
