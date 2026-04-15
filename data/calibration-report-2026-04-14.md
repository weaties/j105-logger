# Corvo — Instrument Calibration Check After First Three Races

**Prepared:** 2026-04-14
**Races analyzed:** CYC Spring #1 & #2 (Apr 9), Ballard Cup #1 (Apr 14)

---

## Why this report

After the first three races of the season, the boat felt asymmetric port to
starboard, and the current readout on the plotter looked off between tacks. We
pulled data from the onboard logger and from the B&G system's own historical
recording to find out whether this is a calibration problem and, if so, what
to fix.

Short version: **the heel sensor has a confirmed +8° offset and should be
corrected at the dock. The compass has a smaller bias than we first thought
(~1°), and the rest of the port/starboard feel comes from something we can't
fully isolate from race data alone. A 10-minute motor-boat compass swing will
finish the job.**

---

## What we found

### 1. The heel sensor reads about +8° too high — high confidence

The B&G roll (heel) reading has a fixed offset of roughly **+8°**. When the
boat is actually dead flat, the instruments think we're heeled 8° to leeward.

Five independent lines of evidence all point to the same number:

| Condition                         | Heel reading  |
|-----------------------------------|---------------|
| Motoring out, 04-09 (14 min)      | **+8.7°**     |
| Motoring out, 04-14 (18 min)      | **+9.2°**     |
| Pre-race run-up, 04-09            | **+7.8°**     |
| Downwind mean, CYC-1 (both tacks) | **+7.3°**     |
| Downwind mean, Ballard Cup        | **+8.5°**     |

A boat motoring at 6 kn in flat water, and a boat sailing downwind nearly
flat, both cannot be physically heeled +8°. That's the sensor, not the boat.

**Impact on the boat today:** small — no live display number depends on heel
right now. But it throws off any polar-by-heel analysis and would mislead any
target-heel coaching we want to layer on later. Easy fix, worth doing.

### 2. The compass has a small bias — about +1°, not +4° — lower confidence

Our first pass looked at HDG−COG during upwind race legs and concluded the
compass was off by +4.3°. **That number was wrong.** When we added the
pre-race motoring segments — where the boat is going straight at 5–6 kn, dead
flat, with minimal leeway — the compass-vs-GPS difference is much smaller:

| Window                            | HDG−COG      |
|-----------------------------------|--------------|
| 04-09 practice, 14 min            | **+1.7°**    |
| 04-14 practice, 18 min            | **+1.0°**    |
| 04-09 pre-race motoring, 18 min   | **−0.2°**    |
| **Mean of clean motoring data**   | **+0.8°**    |

So the *fixed* compass alignment offset is small — probably close to 1°,
possibly zero within measurement noise.

### 3. Which means there's still unexplained asymmetry under racing conditions

If the true compass offset is only +1°, the ~4° asymmetry we see on upwind
race legs has to come from something that only shows up when the boat is
racing: heeled 18–20°, the keel fully loaded, hitting waves. Suspects, in
rough order of likelihood:

- **Non-linear heel-induced compass error.** Above some heel angle the
  fluxgate may swing in a way that a simple straight-line fit can't detect.
- **Asymmetric leeway — one tack makes more leeway than the other.** This
  would stem from rig tune, jib trim, or a helm bias. It looks like a compass
  problem but isn't.
- **Compass behaving differently under wave-induced pitch and roll** than it
  does motoring in flat water.

We cannot cleanly separate these three from race data alone. A short,
intentional calibration run in flat water will.

### What we ruled out

- **Masthead (wind) unit alignment** — fine. Apparent wind angle is symmetric
  within 0.5° port vs stbd across all three races.
- **Paddlewheel calibration** — no *consistent* error. One race showed a 13%
  port/stbd gap, two didn't. Most likely weed/kelp on the leg in question,
  not a sensor issue.
- **Linear heel-induced compass deviation** — the simple model doesn't fit.
  Either there's no heel-deviation at all, or it's non-linear.
- **Rig/trim asymmetry as the primary cause** — polar performance was 0 / 7 /
  17 points different port vs stbd across the three races. That's too
  inconsistent to be a persistent rig problem. Worth a routine check, not the
  first thing to chase.

### What we couldn't check yet

- **Rudder angle / weather helm.** The rudder sensor was not publishing during
  any of the three races. First job next time on the boat: confirm it's wired
  and streaming. A persistent helm bias on one tack would explain part of the
  upwind asymmetry without any compass error.

---

## The race data behind the findings

For the curious. "HDG−COG" is the difference between heading and GPS course —
should be symmetric around zero on port vs stbd if everything is calibrated
and leeway is symmetric. "Heel" should be symmetric (same magnitude, opposite
sign) on the two tacks, and near zero downwind.

| Race          | Leg     | HDG−COG  | Heel    | STW  | SOG  |
|---------------|---------|---------:|--------:|-----:|-----:|
| CYC-1 (R21)   | up-port |   −5.9°  | +18.1°  | 6.22 | 4.95 |
|               | up-stbd |  +12.4°  |  −1.9°  | 6.22 | 5.56 |
|               | dn-port |   −5.1°  |  +7.6°  | 6.54 | 6.37 |
|               | dn-stbd |   +3.1°  |  +6.9°  | 6.12 | 6.37 |
| CYC-2 (R22)   | up-port |   −5.3°  | +16.5°  | 5.84 | 4.59 |
|               | up-stbd |  +14.4°  |  +0.8°  | 5.36 | 4.70 |
|               | dn-port |   −4.4°  |  +8.9°  | 6.53 | 6.24 |
|               | dn-stbd |   +2.8°  |  +7.4°  | 6.63 | 6.90 |
| Ballard (R35) | up-port |   −5.1°  | +21.6°  | 6.91 | 5.82 |
|               | up-stbd |  +10.4°  |  −3.7°  | 5.70 | 5.13 |
|               | dn-port |   −0.4°  |  +9.9°  | 7.12 | 6.65 |
|               | dn-stbd |   +4.4°  |  +7.1°  | 6.98 | 6.74 |

Two things to notice without doing any math:

1. **Downwind heel is +7° to +10° on both tacks, every race.** Should be ~0°.
   That's the heel offset.
2. **HDG−COG on port is ~−5°, on stbd is ~+11° upwind.** Symmetric leeway
   should give equal magnitudes with opposite signs. The midpoint is shifted,
   but the motoring data (above) says only ~1° of that shift is a fixed
   compass offset — the rest is heel- or leeway-dependent.

---

## Action plan

### Before next racing — at the dock (~30 min)

| # | Task                                                                          | Owner     |
|---|-------------------------------------------------------------------------------|-----------|
| 1 | Apply a **−8.0° roll (heel) offset** at the B&G Precision-9 (or equivalent)   | Boat tech |
| 2 | **Do not** apply a heading offset yet — defer until a proper compass swing    | —         |
| 3 | Confirm the rudder-angle sensor is wired and publishing on the bus            | Boat tech |
| 4 | Screenshot current instrument settings **before** the change                  | Boat tech |

### A proper compass swing — 10 minutes of motoring, zero race-day risk

The best test for a compass offset is to motor a straight line on four
headings and compare each to GPS COG. This separates fixed offset from heel-
and wave-induced deviation, and it takes longer to explain than to do.

| # | Step                                                                          |
|---|-------------------------------------------------------------------------------|
| 5 | Pick a flat-water day with light, steady current (ideally slack)              |
| 6 | Motor North at 5 kn for 2 minutes. Record HDG and GPS COG means               |
| 7 | Repeat for East, South, West                                                  |
| 8 | The mean of `HDG − COG` across the four headings is the fixed compass offset  |
| 9 | Differences between headings reveal deviation (as opposed to a flat offset)   |

We can pull this from Signal K afterwards — no manual logging required, just
do it before or after a weeknight race.

### Pre-race verification of the heel fix

| #  | Check                                                                          |
|----|--------------------------------------------------------------------------------|
| 10 | At rest or motoring in flat water, the heel reading should sit within ±1° of zero after the correction |
| 11 | If it doesn't, we got the sign wrong — roll back                              |

### After the next race

| #  | Task                                                                          |
|----|-------------------------------------------------------------------------------|
| 12 | Re-pull the data. Expect downwind heel to land near zero on both tacks        |
| 13 | Check whether port/stbd polar still differs — if yes and the compass has been swung, look at rig tune and jib trim next |
| 14 | Do a proper STW calibration run on a calm day (known tide, four compass headings) to lock the paddlewheel down once |

### Longer term (next few weeks)

- Capture heel, rudder, and pitch in our own logger natively so we don't have
  to pull from two places.
- Compute and store leeway from heel and STW, so the derived current stops
  being biased on downwind legs.

---

## What success looks like

After the heel fix, on the next race we should see:

1. **Heel on a downwind run reads close to zero.** Primary verification of the
   heel-offset change.
2. **Polar-by-heel analysis starts making sense** (wasn't possible before
   because of the offset).

After the compass swing and any heading offset it recommends:

3. **Current on the plotter points in the same direction, same strength, on
   both tacks.**
4. **Target TWA close-hauled is symmetric port vs starboard.**
5. **Polar % on port and starboard converges** (within ~2–3 points in the same
   wind).

If the compass swing says the compass is clean (offset ≈ 0°) and the
port/stbd asymmetry *persists* after the heel fix, then the issue is real
performance — rig tune, jib trim, or helm — and we have clean data to address
it.

---

## Caveats worth stating out loud

- **Three races is enough to spot consistent offsets but not enough to rule
  out subtle second-order effects.** The compass behaviour under heel is
  exactly the kind of thing one more race and one intentional cal run would
  pin down.
- **The motoring segments used for the compass check were short** (14–18
  minutes each) and had some wind, which means the boat still had a little
  leeway even while motoring. The true fixed offset could plausibly be
  anywhere from 0° to +2°.
- **We do not yet have heel or rudder data flowing into our own logger.** The
  analysis above relies on the B&G's historical recording, which is not
  visible live on the displays during a race. Adding it to our logger is on
  the to-do list.
- **These recommendations are reversible.** If anything looks worse after a
  change, roll back and we're back where we started.

*Questions, sanity checks, and crew observations welcome before we make any
change on the boat.*
