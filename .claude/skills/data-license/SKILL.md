---
name: data-license
description: Review code changes against the data licensing policy (docs/data-licensing.md) for compliance. TRIGGER when modifying code that handles user data, PII (audio, photos, emails, biometrics, diarized transcripts), co-op/federation data sharing, export endpoints, deletion/anonymization, or audit logging. Key files — storage.py, export.py, peer_api.py, peer_client.py, federation.py, transcribe.py, audio.py, web.py (data endpoints). DO NOT trigger for UI-only changes, instrument decoding, polar analysis, config, docs, or CSS/JS/templates.
---

# Data License Compliance Review

Review the current changes against `docs/data-licensing.md` to ensure
compliance with the Helm Log data licensing policy.

## 1. Identify Data-Touching Changes

Check `git diff` (staged + unstaged) and any new files for code that:

- Stores, reads, exports, or deletes user data
- Serves data via API endpoints
- Handles PII (video, audio, photos, emails, biometrics, transcripts)
- Implements co-op membership, sharing, or governance
- Adds new data types or collection mechanisms

If no data-touching changes are found, report "No data licensing impact" and stop.

## 1.5 Automated Violation Scan

Before proceeding to manual review, run automated scans for common violation
patterns. Use the diff of the current changes (or `HEAD~1..HEAD` for committed
work) to detect likely issues early:

```bash
# Check for PII fields leaking into co-op/peer API responses
git diff HEAD~1..HEAD -- src/helmlog/peer_api.py src/helmlog/export.py | grep -i "audio\|transcript\|email\|photo\|biometric\|crew_name\|face"

# Check for missing audit logging in peer API endpoints
git diff HEAD~1..HEAD -- src/helmlog/peer_api.py | grep -c "audit_log\|log_access"

# Check for bulk export patterns in co-op endpoints
git diff HEAD~1..HEAD -- src/helmlog/peer_api.py src/helmlog/web.py | grep -i "bulk\|export_all\|dump\|download.*all"

# Check for missing embargo checks near session data queries
git diff HEAD~1..HEAD -- src/helmlog/ | grep -B5 -A5 "session.*data\|track.*fetch\|shared_session" | grep -c "embargo"

# Check for gambling/betting language
git diff HEAD~1..HEAD -- src/helmlog/ | grep -i "bet\|wager\|gambl\|odds\|spread"
```

Report scan results before proceeding to manual review:
- **PII leak scan:** list any hits with file and matched term — these get priority in step 2
- **Audit logging scan:** if peer API code changed but audit log call count is 0, flag as a gap
- **Bulk export scan:** any hits are automatic findings — co-op data must not be bulk-exportable
- **Embargo scan:** if session/track data code changed but no embargo references found nearby, flag for manual verification
- **Gambling scan:** any hits require immediate review — gambling use is unconditionally prohibited

If all scans are clean, note "Automated scan: no violations detected" and proceed.
If any scan produces hits, flag them for closer inspection in step 2.

## 2. Check Against Policy Sections

For each data-touching change, verify compliance with the relevant sections.

**Test coverage cross-reference:** For any data-handling code that is changed,
verify that there are integration tests covering the data licensing constraints.
If a new data endpoint is added, or an existing endpoint's data shape changes,
without corresponding tests in `tests/integration/test_data_license_e2e.py`,
flag this as a test coverage gap. Similarly, check for coverage in
`test_embargo_e2e.py` for embargo-related changes and `test_auth_e2e.py` for
auth/signing changes. Missing test coverage for data licensing constraints is
a finding, not just a suggestion.

### Section 1 — Data Ownership
- [ ] Boat owner retains full export/delete/restrict rights over their data
- [ ] AIS and proximity data from other vessels is excluded
- [ ] Audio/photo/email deletion and anonymization rights are supported
- [ ] Video PII: crew can request face-blur or removal from video recordings
- [ ] Camera consent: crew are informed when cameras are recording
- [ ] YouTube uploads default to unlisted; video links are boat-private
- [ ] Processing offload: offload hosts do not retain data after task completion
- [ ] Biometric data (if any) has per-person consent separate from instrument sharing
- [ ] Biometric data cannot be used in personnel decisions
- [ ] Crew emails are deleted when access is revoked

### Section 2 — Data Sharing
- [ ] Default shared data is limited to: instrument data, session metadata,
      derived metrics, race results
- [ ] Private data (audio, notes, sails, currents, photos, YouTube, crew roster)
      is not exposed to co-op endpoints
- [ ] No bulk export of other boats' co-op data
- [ ] Temporal sharing embargo timestamps are respected before serving track data.
      When an embargo check exists, verify it is **semantically correct** — the
      comparison operator must match the meaning of `embargo_until` (is it the
      first shareable moment or the last blocked moment?). Presence of a check
      is not sufficient; the boundary condition must be right.
- [ ] Temporal sharing policy is enforced at the co-op level, not per-boat
- [ ] Fleet benchmarks contain no boat identities or per-boat data points
- [ ] Benchmarks enforce minimum 4-boat threshold per condition bin
- [ ] Benchmarks exclude embargoed session data until embargo lifts
- [ ] Coach access grants are time-limited with an expiry date (seasonal renewal
      required — no permanent grants). Coach access requires authorization from
      the boat owner per session, not blanket access.
- [ ] Coaches may not bulk-export or aggregate multiple boats' data into a
      derived dataset retained independently of the platform

### Section 5 — Retention and Deletion
- [ ] Data portability: own-boat data exportable in CSV, GPX, JSON, WAV
- [ ] Suppression (soft delete) works during 30-day grace periods
- [ ] Hard delete is irreversible after grace period

### Section 8 — AI/ML
- [ ] Co-op data is not used for ML without governance approval
- [ ] ML opt-out flag is respected

### Section 9 — Commercial Use
- [ ] Co-op data is not used for gambling, betting, or wagering purposes
- [ ] No commercial use without co-op vote

### Section 11 — Liability
- [ ] Co-op data from other boats is not exportable in protest-ready formats

### Section 12 — Technical Requirements
- [ ] Audit logging for co-op data access
- [ ] Rate limiting with auto-freeze on anomalous patterns

## 3. Report Findings

For each issue found, anchor the finding to a specific location in the diff:

```
### Finding <N>: <short description>
- **File:** src/helmlog/<module>.py:<line number>
- **Policy:** Section <N> — "<quoted policy language>"
- **Code:** `<the offending line or expression>`
- **Severity:** CRITICAL / HIGH / LOW
  - CRITICAL: PII leak, auth bypass, missing embargo check, gambling facilitation
  - HIGH: missing audit log, bulk export possible, missing test coverage
  - LOW: style issue, missing comment, minor gap in deletion flow
- **Fix:** <concrete suggested fix>
```

Every finding must reference the exact file and line number (or line range) where
the issue occurs, quote the policy language being violated, and include the
offending code snippet. Do not report vague findings — if it cannot be anchored
to a specific line, it is not a finding.

If all checks pass, report "Data licensing review: compliant."

## 4. Compliance Scorecard

After completing the review, produce a summary scorecard covering all policy
sections that were evaluated. Use N/A for sections not relevant to the changes:

```
## Data License Compliance: <branch or description>

| Policy Section | Status | Notes |
|---|---|---|
| 1. Data Ownership | PASS/FAIL/N/A | |
| 2. Data Sharing | PASS/FAIL/N/A | |
| 5. Retention & Deletion | PASS/FAIL/N/A | |
| 8. AI/ML | PASS/FAIL/N/A | |
| 9. Commercial Use | PASS/FAIL/N/A | |
| 11. Liability | PASS/FAIL/N/A | |
| 12. Technical Requirements | PASS/FAIL/N/A | |

**Automated scan:** <clean / N hits flagged>
**Test coverage:** <adequate / gaps identified>
**Result: COMPLIANT / NON-COMPLIANT** (<N> sections reviewed, <M> issues found)
```

If NON-COMPLIANT, list the blocking findings by number. The PR must not merge
until all CRITICAL and HIGH findings are resolved.
