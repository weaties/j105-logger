# J105 Logger — Data Licensing Policy

This document defines data ownership, access, sharing, and retention rules for all
data collected, stored, and shared through the J105 Logger platform. These rules
govern both single-boat usage and fleet-wide data sharing through the co-op model.

All participants must agree to this policy before joining the data co-op. Single-boat
users who do not participate in the co-op are bound only by the ownership and audio/PII
sections.

---

## 1. Data Ownership

### Instrument data

All instrument data — positions, wind, speed, heading, depth, heel, pitch, and
derived metrics — is owned by the **boat owner or instance operator** (the person
who administers the J105 Logger instance on that boat's hardware).

The boat owner has full rights to export, share, delete, or restrict access to
their instrument data.

### Audio and voice data

Audio recordings are personally identifiable information (PII). Under GDPR, CCPA,
and equivalent privacy laws, **speakers retain personal rights over their voice data**
regardless of who operates the recording hardware.

This means:

- Any crew member can request deletion of audio recordings containing their voice.
- Transcripts derived from audio inherit the same status — a speaker can request
  deletion of transcript segments attributed to them.
- Diarized (speaker-labeled) transcripts carry stronger PII obligations than
  unlabeled transcripts.

The boat owner controls access to audio and transcripts, but cannot override a
speaker's deletion request for their own voice data.

### Notes and annotations

Session notes, race comments, and annotations are owned by the boat and are
boat-private by default.

### Coach and combined datasets

When a coach imports multiple boats' data for analysis, the coach holds a
**delegated access license**, not ownership. The coach may view and analyze the
data but does not own it. If a boat revokes the coach's access, the coach must
delete that boat's data from their systems.

---

## 2. Data Sharing — The Co-op Model

### Overview

The J105 Logger data co-op is a **reciprocal sharing arrangement** for instrument
data. Members share their race data with the fleet and gain access to other
members' data in return.

### What is shared by default

When a boat joins the co-op, the following data is shared with all co-op members:

- **Instrument data**: positions, wind (TWS, TWA, TWD, AWS, AWA), boat speed (BSP,
  SOG, COG), heading, depth, heel, pitch
- **Session metadata**: date, duration, race name, venue
- **Derived metrics**: VMG, polar performance percentage, tacking angles

### What is NOT shared by default

The following data remains **boat-private** and is never shared unless the boat
owner explicitly opts in:

- Audio recordings
- Transcripts (full text and segments)
- Session notes and annotations
- Race comments and threaded discussions
- Crew roster and role assignments
- Sail selections and tuning notes

### Explicit sharing with coaches and tuning partners

Boat owners can explicitly share private data (notes, transcripts, audio) with:

- **Designated coaches**: identified by user account, granted access to specific
  sessions or all sessions
- **Tuning partners**: another boat in the co-op granted reciprocal access to
  private data for collaborative tuning work

Explicit sharing is per-boat, revocable at any time, and does not extend to the
broader co-op.

### Contribution threshold

To join the co-op and access fleet data, a boat must share **at least one race
session**. There is no ongoing minimum contribution requirement.

This low threshold maximizes fleet adoption. In a one-design fleet where everyone
races together, social dynamics are a more effective incentive than technical
enforcement.

---

## 3. Co-op Membership and Governance

### Joining

Any boat running a J105 Logger instance can request to join the co-op. Joining
requires:

1. Agreeing to this data licensing policy
2. Sharing at least one race session
3. Acceptance by an existing co-op admin (for the initial rollout) or automatic
   admission once the co-op is established

### Voluntary departure

A boat may leave the co-op at any time. On departure:

- The boat's server-side co-op access is revoked
- The departing boat's historical data is **anonymized** in fleet comparisons
  (displayed as "Boat X" rather than the actual boat name)
- Anonymized data remains available to the co-op for historical fleet analysis
- A **30-day grace period** applies before full deletion of identifiable data from
  co-op systems

### Expulsion

The co-op may vote to remove a member. The process is:

1. **Initiation**: any co-op member may propose expulsion with a stated reason
2. **Vote**: a **supermajority (2/3) of co-op members** must vote in favor of
   removal
3. **30-day notice**: the member is notified and retains full co-op access during
   the notice period
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

### Crew departure

When a crew member leaves the boat, the **boat owner decides** what happens to
that crew member's access:

- **Retain read-only access**: the former crew member can view sessions they
  participated in but cannot modify or export data
- **Retain full access**: the former crew member keeps the same access to sessions
  they were part of, including export
- **Revoke access**: all access to the boat's data is removed

The boat owner can change this decision at any time.

### Audio deletion rights

Regardless of the boat owner's departure policy, a former crew member always
retains the right to request deletion of audio recordings containing their voice,
per Section 1.

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

- The boat's data remains available in the co-op
- The request can be cancelled at any time
- After 30 days, all identifiable data is permanently deleted from co-op systems

### What deletion covers

- All instrument data, session metadata, and derived metrics associated with the
  boat
- Any audio, transcripts, notes, or annotations that were explicitly shared
- The boat's identity in fleet comparisons (replaced with anonymized placeholder
  if historical comparisons are retained)

### What deletion does not cover

- Other boats' data that happened to include the deleted boat in fleet comparisons
  (those comparisons are anonymized, not removed)
- Aggregated or statistical data that cannot be attributed to a specific boat

---

## 6. Technical Requirements

This policy requires the following technical capabilities in the J105 Logger
codebase:

| Requirement | Purpose |
|---|---|
| `data_sharing_consent` table | Record each boat's agreement to this policy and co-op membership status |
| Per-session sharing flags | Mark individual sessions as co-op-shared, coach-shared, or private |
| Boat-level identity in auth | Boats are first-class entities with owners, crew, and sharing posture (not just user-level auth) |
| Coach/tuning-partner ACLs | Explicit access grants for private data to designated coaches and tuning partners |
| Anonymization capability | Replace boat identity with "Boat X" in fleet comparisons on departure/expulsion |
| Deletion pipeline | 30-day delayed deletion with cancellation support |
| Audio PII deletion | Ability to delete specific audio segments by speaker (requires diarization) |
| Expulsion vote tracking | Record votes, notice periods, and appeal outcomes |

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
