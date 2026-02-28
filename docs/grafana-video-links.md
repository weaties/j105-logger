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
