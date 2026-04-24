# ESP32-CAM Setup

How to register battery-powered ESP32-CAM devices that push photos into
running race sessions for race-watcher viewing.

See issue [#660](https://github.com/weaties/helmlog/issues/660) for the
design motivation.

---

## How it works

Unlike the Insta360 control flow ([camera-setup.md](camera-setup.md)), which
is **pull**-mode, ESP32-CAMs are **push**-mode. The WiFi radio stays asleep
~100% of the time and only wakes on a timer. On each wake the firmware:

1. Associates with the boat's WiFi.
2. `GET /api/device-cameras/status` — returns `{active, session_id}`.
3. If `active`, takes a photo and `POST /api/device-cameras/{role}/photo`.
4. Deep-sleep until the next wake.

Polling the Pi from the firmware is not viable: keeping the radio associated
24/7 costs ~100 mA vs. ~10 μA in deep sleep, so a battery-powered camera
can't answer a `/capture` request without being permanently awake.

Photos land alongside the user's own photo notes under `data/notes/{race_id}/`
and are served by the existing `/notes/{path}` route.

---

## Endpoints

Both endpoints auth'd by device API key (`Authorization: Bearer <key>`). The
device must have `role=crew` and its `name` field must match the `{role}`
path param on the photo endpoint.

### `GET /api/device-cameras/status`

Cheap "should I bother capturing" probe. Fast path for sleepy cameras —
hit this first, skip the upload if inactive.

Response: `{"active": bool, "session_id": int | null}`

| Condition | Response |
|---|---|
| No bearer token | 401 |
| Valid token, no active race | `200 {"active": false, "session_id": null}` |
| Valid token, race in progress | `200 {"active": true, "session_id": N}` |

### `POST /api/device-cameras/{role}/photo`

Multipart JPEG ingest. The `{role}` path param must match the authenticated
device's `name` (a camera can only post as itself).

| Condition | Response |
|---|---|
| No bearer token | 401 |
| Role path param ≠ device name | 403 |
| No active race | 204 (file not written) |
| Active race | 201 `{"id": N, "ts": "...", "photo_path": "N/role_ts_uuid.jpg"}` |

The stored filename is prefixed with `{role}_` so the camera can be
queried after the fact. No schema migration: role survives as a filename
convention, not a column.

---

## Registering a camera

Each physical camera is a row in the `devices` table with `role=crew` and
`name=<camera-role>`. Use the admin device UI or the `gh`-equivalent CLI:

```bash
# Web UI path: /admin/devices → Add device
# Name: mainsail      (must match the URL path param used by the firmware)
# Role: crew
# Scope: (leave blank, or restrict to the two endpoints)

# The plaintext API key is shown **once**. Copy it into the firmware's NVS
# config — HelmLog only stores a SHA-256 hash.
```

If the key leaks, rotate it from the same admin UI; the firmware will need
a re-flash / re-provision to pick up the new key.

### Optional scope restriction

To lock the key down to just these two endpoints, set the device scope to:

```
GET /api/device-cameras/status, POST /api/device-cameras/*/photo
```

---

## Firmware contract

The firmware owns its own wake cadence via NVS — HelmLog has no setting
for "take a photo every N seconds." Suggested cycle:

1. Wake from deep sleep.
2. Associate with SSID in NVS.
3. `GET /api/device-cameras/status` with the bearer token.
4. If `active=false`: disconnect, deep-sleep for the next interval.
5. If `active=true`: capture JPEG, `POST .../photo` with `multipart/form-data`
   and the JPEG in the `file` field.
6. Disconnect, deep-sleep.

A 204 on `.../photo` means "no active race, ignore and back to sleep." A 403
means the camera was flashed with the wrong `{role}` or a key that belongs
to a different device — stop trying and surface the error on the setup UI.

See [`weaties/esp32-cam`](https://github.com/weaties/esp32-cam) for the
reference firmware.
