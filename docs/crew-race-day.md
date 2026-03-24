# Race Day Setup — Crew Quick Reference

_For when the usual operator isn't aboard. No technical knowledge needed._

---

## Before leaving the dock

### 1. Power up the Pi

The Raspberry Pi lives at the nav station. It should already be powered on
(it runs off the canbus power). Check that the small green LED is blinking —
that means it's running.

If it's off, Make sure the instruments are on. Give it about 60 seconds to boot.

### 2. Plug in the microphone

The **Gordik wireless mic** has two parts:

| Piece | What to do |
|---|---|
| **USB receiver** (small dongle) | Plug into any USB port on the Pi |
| **Transmitter clip** (the one with the mic) | Clip to whoever is calling tactics. Press the power button until the LED turns on. It pairs automatically. |

The mic is used for post-race debriefs — it records when you tap the Debrief
button in the app.

### 3. Turn on the Insta360 camera

1. **Power on** the Insta360 X4 (hold the power button ~2 seconds).
2. Wait for it to finish booting (~10 seconds).
3. The camera creates its own WiFi hotspot — the Pi connects to it
   automatically. You don't need to do anything.
4. Verify: on the HelmLog web page, the camera icon should show as connected.
   Or run `helmlog list-cameras` if you have terminal access.

**Important:** Make sure the camera is set to **video mode** (not photo or
timelapse). The Pi will start/stop recording automatically when you start/stop
races in the app.

### 4. Connect to Big Air WiFi (for internet at the dock) 

The Pi can sync data and fetch weather/tides when it has internet access.

**From your phone:**
1. Connect your phone to the **Big Air** WiFi on the boat (PWD: "graymanathena" ).

**The Pi itself** gets internet through its own connection — either the boat's
network or a phone hotspot. If the Pi needs internet and isn't connected, you
can share your phone's hotspot with it later at the dock. Data syncs
automatically when connectivity is available — no rush.

### 5. Open HelmLog on your phone

1. Open your browser and go to https://corvo105.helmlog.org
2. You should see the home page with instrument numbers updating.
3. If instruments show grey numbers, Signal K may need a moment — wait 30
   seconds and refresh.

---

## During racing

### Start a race
Tap **▶ START RACE N** when the sequence begins (or whenever you want to
start logging).

### Add notes (optional)
Tap **+ Note** during the race to log observations, settings, or photos.

### End a race
Tap **■ END RACE N** when you cross the finish line.

### Record results (optional)
Tap **Results ▶** on the completed race card. Search for boats by name or
sail number and tap to add them in finishing order.

---

## After racing

### Record a debrief
1. Tap **🎙 Debrief** on the completed race card.
2. Talk through what happened.
3. Tap **⏹ STOP DEBRIEF** when done.

### Record sails used
Tap **⛵ Sails ▶** on the race card, pick Main / Jib / Spinnaker from the
dropdowns, tap **Save Sails**.

---

## Packing up

1. **Camera** — hold the power button to turn it off. Stow it in its case.
2. **Mics** — Plug them into usb-c
3. **Pi** — leave it plugged in (it's fine to run 24/7). If you need to
   shut it down, don't just yank the power — ask someone to do a clean
   shutdown from the terminal.

---

## Troubleshooting

| Problem | What to do |
|---|---|
| **Page won't load** | Make sure Tailscale is on. Try refreshing. |
| **Instruments are grey/frozen** | Signal K may have stalled. Try refreshing. If still grey after a minute, the Pi may need a restart — ask for help. |
| **Camera shows "unreachable"** | Make sure the Insta360 is powered on and in video mode. Give it 30 seconds after boot. |
| **No Debrief button** | The Gordik USB receiver isn't plugged in, or a race session is still active (end it first). |
| **"Disk XX% full" warning** | Not urgent for today. Mention it to the navigator later. |

### Emergency note

The logger is **read-only** — it cannot affect any boat systems or instruments.
If the app breaks, just close the browser. The boat is fine.

---

_See also: [Full Operator's Guide](operators-guide.md) for more detail._
