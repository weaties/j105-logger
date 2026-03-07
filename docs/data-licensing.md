# J105 Logger — Data Licensing Policy

This document defines data ownership, access, sharing, and retention rules for all
data collected, stored, and shared through the J105 Logger platform. These rules
govern both single-boat usage and co-op data sharing.

A **data co-op** is a group of boats that agree to share instrument data under this
policy. Multiple independent co-ops can exist — for example, a J/105 co-op in
Seattle, a J/105 co-op in San Francisco, and a J/80 co-op in Seattle are all
separate co-ops with independent membership, governance, and data pools. A boat may
belong to more than one co-op.

All participants must agree to this policy before joining a data co-op. Single-boat
users who do not participate in any co-op are bound only by the ownership and
audio/PII sections.

---

## 1. Data Ownership

### Instrument data

All instrument data — positions, wind, speed, heading, depth, heel, pitch, and
derived metrics — is owned by the **boat owner or instance operator** (the person
who administers the J105 Logger instance on that boat's hardware).

The boat owner has full rights to export, share, delete, or restrict access to
their instrument data. The boat owner controls who their data is shared with and
can revoke any person's access at any time.

### Audio and voice data

Audio recordings are personally identifiable information (PII). Under GDPR, CCPA,
and equivalent privacy laws, **speakers retain personal rights over their voice data**
regardless of who operates the recording hardware.

This means:

- Any crew member — active or former — can request deletion or anonymization of
  audio recordings containing their voice, without needing to leave the crew.
  A crew member may simply not want a specific conversation on the record.
- **Anonymization** (voice scrambling, redaction, or speaker removal from the
  audio) is an acceptable alternative to full deletion, provided the speaker's
  voice is no longer identifiable in the resulting recording.
- Transcripts derived from audio inherit the same status — a speaker can request
  deletion or anonymization of transcript segments attributed to them.
- Diarized (speaker-labeled) transcripts carry stronger PII obligations than
  unlabeled transcripts. Anonymizing a diarized transcript means removing or
  replacing the speaker label and redacting identifiable content.

The boat owner controls access to audio and transcripts, but cannot override a
speaker's deletion or anonymization request for their own voice data.

### Photos and images

Photos captured as part of session notes, on-board cameras, or image-based
detection (e.g., sail shape analysis) are owned by the boat and are **boat-private
by default**. Photos that contain identifiable people are PII — the same deletion
and anonymization rights that apply to audio (above) apply to identifiable photos.

### Notes and annotations

Session notes, race comments, and annotations are owned by the boat and are
boat-private by default.

### YouTube and external video

YouTube videos linked to sessions are hosted on YouTube and governed by YouTube's
terms of service. The J105 Logger stores only **metadata** (video ID, title, sync
points) — not the video content itself. The boat owner controls which videos are
linked and can unlink a video at any time. Unlinking removes the metadata from the
logger but does not affect the video on YouTube.

If a boat departs the co-op or requests data deletion, YouTube video metadata
linked to that boat's sessions is included in the deletion scope (see Section 5).
The actual YouTube video remains on YouTube under the uploader's control.

### Coach and combined datasets

When a coach imports multiple boats' data for analysis, the coach holds a
**delegated access license**, not ownership. The coach may view and analyze the
data but does not own it. The boat owner can revoke the coach's access at any
time, at which point the coach must delete that boat's data from their systems.

---

## 2. Data Sharing — The Co-op Model

### Overview

A J105 Logger data co-op is a **reciprocal sharing arrangement** for instrument
data. Members share their race data with the co-op and gain access to other
members' data in return.

### What is shared by default

When a boat joins a co-op, the following data is shared with all co-op members:

- **Instrument data**: positions, wind (TWS, TWA, TWD, AWS, AWA), boat speed (BSP,
  SOG, COG), heading, depth, heel, pitch
- **Session metadata**: date, duration, race name, venue
- **Derived metrics**: VMG, polar performance percentage, tacking angles

### What is NOT shared by default

The following data remains **boat-private** and is never shared unless the boat
owner explicitly opts in:

- Audio recordings
- Transcripts (full text and segments)
- Photos and images
- Session notes and annotations
- Race comments and threaded discussions
- Crew roster and role assignments
- Sail selections and tuning notes
- YouTube video links and metadata

### Explicit sharing with coaches and tuning partners

Boat owners can explicitly share private data (notes, transcripts, audio, photos)
with:

- **Designated coaches**: identified by user account, granted access to specific
  sessions or all sessions. The boat owner can revoke a coach's access at any time.
- **Tuning partners**: another boat in the co-op granted reciprocal access to
  private data for collaborative tuning work

Explicit sharing is per-boat, revocable at any time, and does not extend to the
broader co-op.

### Contribution threshold

To join the co-op and access co-op data, a boat must share **at least one race
session**. There is no ongoing minimum contribution requirement.

This low threshold maximizes adoption. In a one-design fleet where everyone races
together, social dynamics are a more effective incentive than technical enforcement.

---

## 3. Co-op Membership and Governance

### Co-op identity

Each co-op is an independent group defined by its membership. There is no global
co-op — each co-op has its own members, governance, and data pool.

### Boat representatives

Each boat in the co-op has a **designated representative** — typically the boat
owner — who is the voting member for governance decisions. One person, one vote per
boat, regardless of crew size.

#### Club and institutional ownership

When a single entity (e.g., a sailing club, corporate team, or charter company)
owns multiple boats, the following rules apply:

- Each boat still gets **one vote** in co-op governance
- The owning entity designates a representative for each boat (this may be the same
  person for multiple boats, or different people)
- To prevent bloc voting from dominating a small co-op, a single entity's boats
  collectively hold **no more than 1/3 of total votes** in any governance decision.
  If one entity's boats exceed 1/3 of co-op membership, their excess votes are
  excluded from the count

### Joining

Any boat running a J105 Logger instance can request to join a co-op. Joining
requires:

1. Agreeing to this data licensing policy
2. Sharing at least one race session
3. Acceptance by a co-op admin

#### Bootstrap phase

When a co-op has **fewer than 5 member boats**, it operates in bootstrap phase:

- Admission is by admin approval only (no vote required)
- Expulsion requires **unanimous vote** of all other members (since 2/3 of a tiny
  group is effectively everyone)
- Once the co-op reaches 5 members, standard governance rules apply

### Voluntary departure

A boat may leave the co-op at any time. On departure:

- The boat's server-side co-op access is revoked immediately
- The departing boat's data is **suppressed** from co-op views during a **30-day
  grace period** (not visible to other members, but not yet deleted)
- The departure can be reversed during the grace period
- After 30 days, the departing boat's historical data is **anonymized** in co-op
  comparisons (displayed as "Boat X" rather than the actual boat name) and all
  identifiable data is permanently deleted from co-op systems

### Expulsion

The co-op may vote to remove a member. The process is:

1. **Initiation**: any co-op member's representative may propose expulsion with a
   stated reason
2. **Vote**: a **supermajority (2/3) of boat representatives** must vote in favor
   of removal
3. **30-day notice**: the member is notified. During this period, their data is
   **suppressed** from co-op views (not visible to other members) but they retain
   read access to co-op data
4. **Appeal**: during the 30-day notice period, the member may make their case to
   the co-op. A new supermajority vote can reverse the expulsion decision
5. **Expulsion takes effect**: if the vote stands after 30 days:
   - Server-side co-op access is revoked
   - The expelled member's **license to use co-op data is revoked** — they must
     delete any co-op data in their possession (this includes local copies)
   - The expelled boat's historical data is **anonymized** in the co-op (same
     treatment as voluntary departure)

### Re-entry after expulsion

An expelled member may re-apply to the co-op at any time. Re-admission requires
the same **supermajority (2/3) vote** as expulsion.

---

## 4. Crew Access and Departure

### Active crew

The boat owner controls which crew members have access to the boat's J105 Logger
instance and what level of access they hold (view, edit, admin).

### Per-recording deletion

A crew member does **not** need to leave the crew to request deletion or
anonymization of a specific recording. If a crew member was recorded discussing
something they don't want on the record, they can request removal of that specific
audio and its associated transcript at any time, per Section 1.

### Crew departure

When a crew member leaves the boat, the **boat owner decides** what happens to
that crew member's access:

- **Retain read-only access**: the former crew member can view sessions they
  participated in but cannot modify or export data
- **Retain full access**: the former crew member keeps the same access to sessions
  they were part of, including export
- **Revoke access**: all access to the boat's data is removed

The boat owner can change this decision at any time.

### Audio and photo deletion rights

Regardless of the boat owner's departure policy, a former crew member always
retains the right to request deletion or anonymization of audio recordings and
identifiable photos containing them, per Section 1.

---

## 5. Data Retention and Deletion

### Retention

Data is retained indefinitely by default on the boat's local J105 Logger instance.
There is no automatic expiration.

### Deletion requests

Any boat owner may request **full deletion** of all their data from the co-op and
any instances that synced it. Deletion requests are subject to a **30-day grace
period** before execution. This prevents impulsive data loss and allows time to
reconsider.

During the grace period:

- The boat's data is **suppressed** — hidden from co-op views but not yet deleted
- The request can be cancelled at any time, which restores visibility
- After 30 days, all identifiable data is permanently deleted from co-op systems

### What deletion covers

- All instrument data, session metadata, and derived metrics associated with the
  boat
- Any audio, transcripts, photos, notes, or annotations that were explicitly shared
- YouTube video metadata linked to the boat's sessions (the actual YouTube videos
  are unaffected — they remain on YouTube under the uploader's control)
- The boat's identity in co-op comparisons (replaced with anonymized placeholder
  if historical comparisons are retained)

### What deletion does not cover

- Other boats' data that happened to include the deleted boat in co-op comparisons
  (those comparisons are anonymized, not removed)
- Aggregated or statistical data that cannot be attributed to a specific boat
- YouTube videos themselves (hosted on YouTube, not controlled by the logger)

---

## 6. Technical Requirements

This policy requires the following technical capabilities in the J105 Logger
codebase:

| Requirement | Purpose |
|---|---|
| `data_sharing_consent` table | Record each boat's agreement to this policy and co-op membership status |
| Multi-co-op support | A boat can belong to multiple co-ops; each co-op has independent membership and data pools |
| Per-session sharing flags | Mark individual sessions as co-op-shared, coach-shared, or private |
| Boat-level identity in auth | Boats are first-class entities with owners, designated representatives, crew, and sharing posture |
| Club/multi-boat entity support | Track which boats belong to the same owning entity for vote-capping rules |
| Coach/tuning-partner ACLs | Explicit, revocable access grants for private data to designated coaches and tuning partners |
| Anonymization capability | Replace boat identity with "Boat X" in co-op comparisons on departure/expulsion |
| Audio anonymization | Voice scrambling / redaction as an alternative to full deletion |
| Photo PII handling | Deletion or anonymization of identifiable photos on request |
| Data suppression | Hide (but preserve) a boat's data during 30-day grace periods |
| Deletion pipeline | 30-day delayed deletion with cancellation support |
| Audio PII deletion | Ability to delete or anonymize specific audio segments by speaker (requires diarization) |
| Expulsion vote tracking | Record votes (by boat representative), notice periods, and appeal outcomes |
| YouTube metadata cleanup | Remove linked video metadata on boat departure/deletion |

---

## 7. Software License

The J105 Logger source code is licensed under the **GNU Affero General Public
License v3.0 (AGPLv3)**. See the `LICENSE` file in the repository root.

This means:

- Anyone can use, study, modify, and distribute the software
- Anyone who runs a modified version as a network service (e.g., hosting their own
  instance) must make their modified source code available under the same license
- This protects the project from proprietary forks while encouraging community
  contribution

The AGPLv3 governs the **software**. This data licensing policy governs the
**data** collected and shared through the software. They are complementary but
independent — using the software does not grant rights to other boats' data, and
participating in the data co-op does not grant rights to modify the software
beyond what the AGPLv3 allows.

---

## Document History

| Date | Change |
|---|---|
| 2026-03-07 | Initial version — ownership, co-op model, expulsion, crew departure, retention |
| 2026-03-07 | Rev 2 — multi-co-op model, photos, YouTube metadata, audio anonymization, club ownership, boat representatives, bootstrap phase, data suppression during grace periods, per-recording crew deletion rights |
