# J105 Logger — Community & Fleet Platform Brief

*Draft: 2026-03-02, distilled from Dan's journal notes*

---

## The Core Insight

You're not building a data logger. You're building the foundation for a **fleet
performance network** — where every boat running the software contributes
anonymized sailing data back to the community, and everyone benefits from seeing
how the fleet performed in the same conditions.

The logger on the Pi is the edge node. The community is the network effect.

---

## What You Have Today

A single-boat system that does everything end-to-end:

- Instrument data capture (Signal K → SQLite)
- Audio recording + on-device transcription (faster-whisper + pyannote)
- Video sync (YouTube deep-links per second)
- Weather + tide overlay (Open-Meteo, NOAA)
- Race marking + export (CSV/GPX/JSON for regatta tools)
- Web UI for crew on race day
- Promotion pipeline (dev → test → stage → live on `saillog.io`)

This is already more than most racing programs have. The question is: how do
you turn this into something a fleet can run together?

---

## The Fleet Model

### What gets shared (the social contract)

| Data | Shared with fleet | Stays with the boat |
|---|---|---|
| GPS tracks (lat/lon/COG/SOG) | ✅ | ✅ |
| Instrument data (BSP, HDG, TWS, TWA, depth) | ✅ | ✅ |
| Weather + tide context | ✅ | ✅ |
| Race marks + timing | ✅ | ✅ |
| Audio recordings | ❌ | ✅ |
| Transcripts | ❌ | ✅ |
| Video links | ❌ | ✅ |
| Crew names / user accounts | ❌ | ✅ |

**The deal**: if you run the software, your sailing data (not your voice, not
your video, not your crew conversations) is available to everyone else in the
program. This is what makes fleet-wide analysis possible.

### What fleet sharing enables

- **Comparative performance**: see your VMG/polars against the fleet in the
  same wind
- **Wind field mapping**: multiple boats at different positions on the course
  give you a spatial wind picture — who had more pressure, where the shifts were
- **Tide + current correlation**: compare boatspeed vs SOG across the fleet to
  infer current effects at different positions
- **Start line analysis**: who won the start, what end was favored, how did
  boats separate
- **Regatta replay**: full fleet replay with instrument data overlay — like
  Sailmon but built from actual on-board data, not AIS

### What stays private and why

Audio, transcripts, and video are **tactical and personal**. They contain crew
conversations, strategy discussions, coaching notes. Sharing them would kill
adoption. Nobody wants their pre-start argument with the tactician available to
the fleet.

The same applies to crew identities. The boat participates; the people are private.

---

## Hardware Kit

What someone needs to join:

| Component | Cost (approx) | Notes |
|---|---|---|
| Raspberry Pi 5 8GB | $80 | Pi 4 8GB also works |
| CAN bus HAT (MCP2515) | $15–25 | SPI + 16 MHz crystal + GPIO 25 interrupt |
| NMEA 2000 cable + connector | $20–40 | Connects HAT to the boat's backbone |
| USB wireless mic (Gordik 2T1R or similar) | $30–50 | Optional, for audio recording |
| SD card (32GB+) | $10 | Or USB SSD for better durability |
| Waterproof case | $15–30 | The Pi lives on a boat |
| **Total** | **~$170–235** | Plus your existing B&G / NMEA 2000 instruments |

The software is free. The hardware is cheap. The barrier to entry is low.

---

## Networking & Connectivity

### On the boat (race day)

The Pi connects to the NMEA 2000 bus via the CAN HAT. No internet required for
core logging — everything writes to local SQLite. Audio records locally.

### Off the boat (sync & fleet data)

When the Pi gets internet (marina Wi-Fi, phone hotspot, home network), it:

1. Syncs shared data upstream to the fleet data store
2. Pulls down fleet data for races it participated in
3. Fetches weather/tide data for any gaps

### Tailscale as the fleet network

Each boat's Pi joins a shared Tailscale network (managed by the program). This
gives:

- **Private mesh networking** — every Pi reachable by hostname from anywhere
- **No port forwarding or static IPs** — works behind marina NAT, cellular, etc.
- **Encrypted transit** — data in motion is always encrypted
- **Admin visibility** — program maintainers can see which nodes are online
- **SSH access for support** — with owner permission, help debug remote Pis

The fleet Tailscale network is separate from individual owners' personal
Tailscale accounts. The Pi joins the fleet network; the owner's phone/laptop
joins their own.

Public access (for crew on race day) continues through Cloudflare Tunnel at
`{boat}.live.saillog.io`.

---

## Compute Architecture — What Runs Where

### On the Pi (edge)

- Signal K Server (owns the CAN bus)
- j105-logger (data capture, storage, web UI, export)
- Audio recording
- Race marking
- Local SQLite (single source of truth for that boat)

### Off the Pi (offloaded)

Some workloads are too heavy for a Pi, even the Pi 5:

| Workload | Why offload | Where it runs |
|---|---|---|
| Audio transcription | CPU-bound, heats up the Pi, takes 1–3× real-time | Owner's Mac/PC, or a shared fleet server |
| Speaker diarisation | Needs PyTorch + pyannote, memory-heavy | Owner's Mac/PC |
| Fleet data aggregation | Joins data across all boats | Central fleet server (or serverless) |
| Fleet replay rendering | Compute-intensive visualization | Client-side (browser) or server |

**Transcription offload** is the immediate priority. The Pi captures the audio;
a more powerful machine does the transcription. This could be:

- The owner's Mac (pull the WAV over Tailscale, transcribe locally, push result back)
- A fleet-level transcription service (owner opts in, audio goes up, transcript comes back)
- A CLI command: `j105-logger transcribe --remote` that handles the handoff

The key constraint: **audio never leaves the owner's control without explicit
consent**. The default path is local or the owner's own hardware.

---

## Open Source & Licensing

### The software

Open source. People should be able to fork it, hack on it, run it however they
want on their own boat with no obligations.

**Suggested license**: something permissive for the code (MIT or Apache 2.0)
with a separate **data sharing agreement** for fleet participation.

### The data sharing agreement

This is not a software license — it's a participation agreement for the fleet
network:

1. **By joining the fleet program, you agree to share your sailing data**
   (instrument, GPS, weather, race marks) with other participants
2. **Audio, transcripts, video, and crew identities are never shared** unless
   you explicitly publish them
3. **If you leave the program**, your historical shared data remains in the
   fleet dataset (it's already been used for analysis), but you stop
   contributing new data and lose access to other boats' data
4. **No commercial use of fleet data** without community governance approval
5. **The software is always free** — nobody pays to run the logger on their boat
6. **Data is used for sailing performance analysis only** — not sold to
   advertisers, insurance companies, or marina operators

### What this is NOT

- Not a SaaS product. Nobody pays a subscription.
- Not a data brokerage. The fleet data serves the fleet.
- Not a walled garden. The software runs on your hardware, your data lives on
  your Pi, and you can leave anytime.

---

## Community & Contribution Model

### The layers (revealed progressively)

Like Dan said — revealed in layers, like a secret society.

| Layer | Who | Access | Can contribute |
|---|---|---|---|
| **User** | Anyone who installs the software | Their own boat's data, local web UI | Bug reports, feature requests (issues) |
| **Fleet member** | Users who join the fleet network | Fleet-wide shared data + comparative analysis | Issues + data contributions |
| **Contributor** | Fleet members who submit code | Everything above + dev environment | PRs reviewed by agents + maintainers |
| **Maintainer** | Trusted contributors | Everything above + merge rights | Approve PRs, guide roadmap |

### Contribution workflow (agent-assisted)

This is the interesting part — agents participate in the development process:

1. **Issue creation** — anyone (user, fleet member, contributor) opens an issue
2. **Triage** — a configurable number of actual users + agents evaluate the
   issue, label it, assess feasibility, flag conflicts with existing work
3. **Development** — contributor works on their own Pi or Mac, creates a PR.
   Agents (Claude) can help write the code — contributors can literally pair
   with Claude on their own hardware
4. **Review** — PR is reviewed by:
   - Automated agents (lint, test, type check, security scan)
   - N participating contributors (code review)
   - At least one maintainer
5. **Merge** — maintainer merges to `main`, auto-deploys to test, promotes
   through stage → live

The agents aren't gatekeepers — they're accelerators. They help contributors
write better code, catch issues early, and reduce the burden on human reviewers.

### Development on contributor Pis

Contributors can run the full dev environment on their own Pi:

- Clone the repo, create a branch
- Use Claude Code on the Pi (or SSH from their Mac) to develop
- Run tests locally (`uv run pytest`)
- Push and create a PR
- PR preview deploys to `{boat}.test-pr{N}.saillog.io` (on the contributor's
  own test Pi, or a shared fleet test environment)

This means **the development environment IS the production environment** — same
hardware, same OS, same constraints. No "works on my Mac" surprises.

---

## Fleet Data Architecture (sketch)

This is future work, but worth thinking about now so we don't paint ourselves
into a corner.

### Option A: Centralized fleet server

- A single server (cloud VM or a dedicated Pi/NUC) runs the fleet data store
- Each boat's Pi syncs shared data to the central server over Tailscale
- Fleet analysis queries run against the central store
- Simple, easy to reason about, single point of failure

### Option B: Federated / peer-to-peer

- Each Pi holds its own data + cached copies of fleet data for races it
  participated in
- Sync happens peer-to-peer over Tailscale when boats are online
- No central server, more resilient, harder to build
- Eventual consistency — fleet view is only complete when all boats have synced

### Option C: Hybrid (probably the right answer)

- A lightweight central index that knows which boats have data for which races
- Actual data stays on each Pi (or the owner's storage)
- Fleet queries are federated — the index tells the client which boats to ask,
  the client pulls directly from each boat's Pi over Tailscale
- Central index is cheap to run (just metadata), actual data is distributed

### What to do now

**Nothing.** Don't build any of this yet. Just make sure the local SQLite
schema and export formats are clean enough that fleet aggregation is possible
later. The current schema is fine for this — UTC timestamps, standard units,
clean column names. When the time comes, the fleet layer can read from each
boat's SQLite (via API or direct sync) without schema changes.

---

## What's Next (prioritized)

### Now (things that are already in flight)

1. ~~Register `saillog.io`~~ ✅
2. ~~Document promotion strategy (issue #125)~~ ✅
3. Order test Pi 5 8GB ✅
4. Merge current feature branch (audit log, tags, triggers, headshots)
5. Move transcription off-Pi to Mac (issue needed)

### Soon (next few weeks)

6. Set up Cloudflare Tunnel + Access on `corvopi`
7. Provision test Pi, get stage + test environments running
8. Build `j105-logger promote` CLI subcommand
9. GitHub Actions for auto-deploy on branch push

### Later (when the fleet model matters)

10. Define the data sharing agreement
11. Choose a software license (MIT or Apache 2.0)
12. Design the fleet sync mechanism (start with Option A — central server)
13. Build fleet comparative analysis views
14. Write the "Buy a Pi, join the fleet" getting-started guide
15. Set up the contributor workflow with agent-assisted review
16. Community governance model (how decisions get made)

### Way later (blue sky)

- Fleet replay visualization (browser-based, multi-boat track replay)
- Wind field reconstruction from multi-boat data
- Predictive start line analysis
- Integration with Sailmon / other regatta tools as a data source
- Mobile app for race-day crew interaction
- Blue-green deployments for zero-downtime updates

---

## Open Questions

1. **Who hosts the fleet Tailscale network?** Dan for now, but governance
   should be community-driven eventually.
2. **What class of boat is this for?** Started as J/105 but nothing in the
   architecture is class-specific. Any boat with NMEA 2000 instruments works.
3. **How do you handle boats with different instrument setups?** Some boats
   have more sensors than others. The schema already handles NULL columns
   gracefully — boats export what they have.
4. **What about one-design vs handicap racing?** Fleet comparison is most
   meaningful in one-design (same boat, same sails, performance differences =
   skill). Handicap fleets could still benefit from wind field data.
5. **Transcription privacy for fleet data?** If audio stays on the boat, there's
   no issue. But if someone wants to share a transcript for coaching purposes,
   they should be able to — opt-in, never default.
6. **Revenue model?** Dan says no money. But servers cost money. Options:
   community-funded (each participant chips in for fleet infra), grant-funded
   (sailing associations), or sponsor-funded (marine electronics companies who
   want the data ecosystem to exist).
