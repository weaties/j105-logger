# Grafana — YouTube Video Deep Links

This document explains how to use the `/api/sessions/{race_id}/videos` endpoint
to surface clickable YouTube deep links inside Grafana dashboards.

---

## Endpoint

```
GET /api/sessions/{race_id}/videos?at=<UTC ISO 8601>
```

Returns a JSON array of video objects linked to the given race. Each object includes
a computed `deep_link` field that jumps directly to the correct moment in the video.

**Example response:**

```json
[
  {
    "id": 1,
    "race_id": 42,
    "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "video_id": "dQw4w9WgXcQ",
    "label": "Bow cam",
    "sync_utc": "2026-06-15T14:05:00+00:00",
    "sync_offset_s": 323.0,
    "duration_s": 3600.0,
    "title": "Race 1 — Bow Camera",
    "deep_link": "https://youtu.be/dQw4w9WgXcQ?t=353"
  }
]
```

If `?at=` is omitted, `deep_link` will be `null`.

---

## Deep-link formula

```
video_position_s = sync_offset_s + (at_utc_s - sync_utc_s)
deep_link = https://youtu.be/{video_id}?t={floor(video_position_s)}
```

`deep_link` is `null` when:
- `duration_s` is unknown (metadata fetch failed at add time), or
- The computed `video_position_s` falls outside `[0, duration_s]`.

---

## Grafana Text panel (HTML mode)

Create a **Text** panel set to **HTML** mode and paste the snippet below.
Replace `YOUR_BASE_URL` and `YOUR_RACE_ID` with the logger's address and the
race you want to link.

```html
<div id="video-links">Loading…</div>
<script>
(function () {
  var base = "http://YOUR_BASE_URL";
  var raceId = YOUR_RACE_ID;
  // __from and __to are Grafana's built-in time range macros (milliseconds UTC).
  var at = new Date(__from).toISOString();
  fetch(base + "/api/sessions/" + raceId + "/videos?at=" + encodeURIComponent(at))
    .then(function (r) { return r.json(); })
    .then(function (videos) {
      var el = document.getElementById("video-links");
      if (!videos.length) { el.textContent = "No videos linked."; return; }
      el.innerHTML = videos.map(function (v) {
        var label = v.label || v.title || "Video";
        var link  = v.deep_link || v.youtube_url;
        return '<a href="' + link + '" target="_blank" style="display:block;margin:4px 0">'
             + '🎬 ' + label + '</a>';
      }).join("");
    })
    .catch(function () {
      document.getElementById("video-links").textContent = "Could not load videos.";
    });
}());
</script>
```

> **Note:** Grafana's `__from` and `__to` variables are only available in
> panels with scripting enabled. If your Grafana version does not inject them,
> hard-code the UTC timestamp or use a Grafana variable instead:
> `var at = "${my_time_var}";`

---

## Grafana variable approach

1. Add a dashboard variable `video_race_id` (type: *Constant* or *Query*) set to the race ID.
2. Add a *Text* variable `video_at` that you set to the UTC instant you care about
   (e.g. from a time-picker annotation).
3. Use the panel link URL:
   ```
   http://YOUR_BASE_URL/api/sessions/${video_race_id}/videos?at=${video_at}
   ```

---

## Redirect endpoint (for Grafana Data Links)

```
GET /api/sessions/{race_id}/videos/redirect?at=<UTC ISO 8601>
```

Returns `HTTP 302` directly to the computed YouTube deep-link (`https://youtu.be/<id>?t=<seconds>`)
for the session's first video. This is designed for use with Grafana **Data Links**, which open
the redirect URL in a new browser tab.

| Status | Condition |
|--------|-----------|
| `302`  | Redirect to `https://youtu.be/<id>?t=<seconds>` (or plain YouTube URL if duration is unknown) |
| `404`  | Session not found, or no videos linked to it |
| `422`  | `at` param is missing or not a valid ISO 8601 timestamp |

If multiple videos are attached, the redirect goes to the first one (ordered by `created_at`).

---

## Grafana Data Links on time-series panels (recommended)

Data Links let you right-click any data point in a Grafana time-series panel and open the
corresponding video moment in a new tab.

### Setup

1. Open the panel editor for any time-series panel in your race dashboard.
2. Scroll to **Panel options → Data links** and click **Add link**.
3. Set **Title** to something like `▶ Watch video at this moment`.
4. Set **URL** to:
   ```
   http://YOUR_PI_HOST:3002/api/sessions/YOUR_RACE_ID/videos/redirect?at=${__value.time:date:iso}
   ```
   Replace `YOUR_PI_HOST` with the Pi's hostname or Tailscale address and `YOUR_RACE_ID` with
   the integer race ID (visible in the URL when you open a Grafana race dashboard).
5. Enable **Open in new tab**.
6. Click **Apply** and **Save dashboard**.

Now right-clicking a data point shows a context menu with your link. Clicking it opens YouTube at
exactly that moment.

### `${__value.time}` vs `${__from}`

| Variable | Value | Best use |
|----------|-------|----------|
| `${__value.time:date:iso}` | UTC timestamp of the clicked data point | Data Links on individual series points |
| `${__from:date:iso}` | Left edge of the current time range | Text panel thumbnail strip (see below) |

Use `${__value.time}` in Data Links so the video jumps to the exact moment you clicked, not just
the start of the visible window.

---

## Adding / editing videos

Videos are managed via the logger's web UI (History page → session card → **🎬 Videos ▶**)
or via the API directly:

```bash
# Add a video
curl -X POST http://YOUR_BASE_URL/api/sessions/42/videos \
  -H 'Content-Type: application/json' \
  -d '{
    "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "label": "Bow cam",
    "sync_utc": "2026-06-15T14:05:00Z",
    "sync_offset_s": 323.0
  }'

# Update the sync calibration
curl -X PATCH http://YOUR_BASE_URL/api/videos/1 \
  -H 'Content-Type: application/json' \
  -d '{"sync_offset_s": 350.0}'

# Delete
curl -X DELETE http://YOUR_BASE_URL/api/videos/1
```
