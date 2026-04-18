---
date: 2026-04-18
---

# Speed Calibration Report — 2026-04-18

## Setup

- Near-zero wind, building flood tide
- Constant RPM throughout
- Four legs run sequentially: West, East, North, South
- Current instrument speed correction: **96.3%**

## Data (1-minute averages, from SQLite)

| Leg | Window (UTC) | Heading | COG | STW | SOG |
|---|---|---|---|---|---|
| West | 20:00–20:04 | 279° | 274° | 6.84 | 6.77 |
| East | 20:08–20:16 | 104° | 105° | 6.93 | 6.05 |
| North | 20:20–20:22 | 15° | 11° | 6.73 | 5.85 |
| South | 20:24–20:27 | 196° | 194° | 6.81 | 6.61 |

All legs had COG within 2–5° of heading (real current set; projection error from
the offset is <0.4%, ignored).

## Method

On reciprocal headings at constant boat RPM, the current component along-track
cancels, so true through-water speed is `(SOG₁ + SOG₂) / 2`. The calibration
factor applied on top of the current 96.3% is
`true_STW / displayed_STW_avg`, and the new correction is
`96.3% × factor`.

## Result

| Pair | True STW | Displayed STW | Factor | New correction |
|---|---|---|---|---|
| W/E | 6.41 | 6.885 | 0.9307 | **89.6%** |
| N/S | 6.23 | 6.770 | 0.9202 | **88.6%** |
| Combined | 6.32 | 6.828 | 0.9256 | **89.1%** |

**Recommendation: set correction to ~89%** (89.1% if using the decimal). Paddle
wheel is reading roughly 8% high.

## Caveats

- W/E and N/S pairs agree within 1% — good internal consistency given the
  building flood (W/E ran earlier in weaker current, N/S later in stronger).
  The small spread is consistent with slight leeway/RPM drift, not a bad result.
- N leg had the shortest steady window (3 min) and is the noisiest single
  data point; the W/E pair (89.6%) is the more trustworthy single number if
  you want to be conservative.
- Method assumes constant boat through-water speed across each reciprocal
  pair. With no wind and constant RPM, that holds well.
- For an even better calibration: run longer steady-state legs (5+ min each),
  on more precisely reciprocal headings, ideally at a single tide state
  rather than a building one.
