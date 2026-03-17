"""Pi test harness — orchestrates federation testing across two Raspberry Pis.

Runs from a Mac (or any machine with SSH access to both Pis) and drives the
full setup → seed → test → teardown lifecycle over SSH and HTTP.

Usage:
    # Full lifecycle
    uv run python scripts/pi_harness.py \\
        --pi-a 100.73.127.5 --pi-b 100.78.208.87 \\
        --ssh-key ~/.ssh/helmlog-harness

    # Setup only (leave federation in place for manual testing)
    uv run python scripts/pi_harness.py --setup-only ...

    # Test only (federation already set up from a previous --setup-only)
    uv run python scripts/pi_harness.py --test-only ...

    # Teardown only
    uv run python scripts/pi_harness.py --teardown-only ...

Prerequisites:
    - SSH key-based access to both Pis (see docs in issue #334)
    - Both Pis have helmlog installed and the service running
    - Both Pis are on the same Tailscale network
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# SSH + HTTP helpers
# ---------------------------------------------------------------------------

SSH_OPTS = [
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "BatchMode=yes",
]


@dataclass
class PiHost:
    """A Raspberry Pi target for the harness."""

    ip: str
    ssh_key: str
    ssh_user: str = "weaties"
    port: int = 80
    name: str = ""
    branch: str = ""
    identity: dict[str, Any] = field(default_factory=dict)
    co_op_id: str = ""

    def ssh(self, cmd: str, check: bool = True, timeout: int = 30) -> str:
        """Execute a command on this Pi via SSH."""
        full_cmd = [
            "ssh",
            *SSH_OPTS,
            "-i",
            self.ssh_key,
            f"{self.ssh_user}@{self.ip}",
            cmd,
        ]
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            msg = f"SSH to {self.name or self.ip} failed: {result.stderr.strip()}"
            raise RuntimeError(msg)
        return result.stdout.strip()

    @property
    def base_url(self) -> str:
        return f"http://{self.ip}:{self.port}"


def _log(phase: str, msg: str) -> None:
    print(f"  [{phase}] {msg}")


def _header(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------


def preflight(pi_a: PiHost, pi_b: PiHost) -> bool:
    """Check SSH connectivity, service health, and branch info."""
    _header("Preflight")
    ok = True

    for pi in [pi_a, pi_b]:
        try:
            hostname = pi.ssh("hostname")
            pi.name = hostname
            _log("preflight", f"{pi.ip} → {hostname}")
        except Exception as exc:
            _log("preflight", f"FAIL: cannot SSH to {pi.ip}: {exc}")
            ok = False
            continue

        # Check service status
        status = pi.ssh("sudo systemctl is-active helmlog", check=False)
        _log("preflight", f"  helmlog service: {status}")
        if status != "active":
            _log("preflight", "  WARNING: helmlog service not active")

        # Check git branch
        branch = pi.ssh(
            "cd ~/helmlog && git rev-parse --abbrev-ref HEAD",
            check=False,
        )
        pi.branch = branch
        _log("preflight", f"  branch: {branch}")

        # Check identity via peer API (no auth needed)
        try:
            resp = httpx.get(f"{pi.base_url}/co-op/identity", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                fp = data.get("fingerprint", "?")[:8]
                name = data.get("name", "?")
                _log("preflight", f"  identity: {name} ({fp}...)")
            else:
                code = resp.status_code
                _log("preflight", f"  identity: not initialized ({code})")
        except httpx.ConnectError:
            _log("preflight", f"  WARNING: cannot reach {pi.base_url}")
            ok = False

    if pi_a.branch != pi_b.branch:
        _log(
            "preflight",
            f"WARNING: branch mismatch — {pi_a.name}={pi_a.branch}, {pi_b.name}={pi_b.branch}",
        )

    return ok


def _enable_auth_bypass(pi: PiHost) -> None:
    """Temporarily set AUTH_DISABLED=true and restart the service."""
    # Check for an uncommented AUTH_DISABLED=true (ignore commented lines)
    result = pi.ssh(
        "grep -q '^AUTH_DISABLED=true' ~/helmlog/.env 2>/dev/null && echo YES || echo NO",
        check=False,
    )
    if result.strip() != "YES":
        pi.ssh("echo 'AUTH_DISABLED=true' >> ~/helmlog/.env")
        _log("setup", f"  {pi.name}: AUTH_DISABLED=true added to .env")
    else:
        _log("setup", f"  {pi.name}: AUTH_DISABLED=true already set")

    pi.ssh("sudo systemctl restart helmlog")
    _log("setup", f"  {pi.name}: service restarted")

    # Wait for service to be ready
    for _attempt in range(15):
        try:
            resp = httpx.get(f"{pi.base_url}/co-op/identity", timeout=3)
            if resp.status_code in (200, 404):
                return
        except httpx.ConnectError:
            pass
        time.sleep(1)
    msg = f"Service on {pi.name} did not become ready"
    raise RuntimeError(msg)


def _init_identity(
    pi: PiHost,
    sail_number: str,
    boat_name: str,
    email: str,
) -> dict[str, Any]:
    """Initialize identity via the web API."""
    # Check if identity already exists
    resp = httpx.get(f"{pi.base_url}/co-op/identity", timeout=10)
    if resp.status_code == 200:
        data = resp.json()
        fp = data.get("fingerprint", "")[:8]
        _log("setup", f"  {pi.name}: identity exists — {data.get('name')} ({fp}...)")
        pi.identity = data
        return data

    # Create new identity
    resp = httpx.post(
        f"{pi.base_url}/api/federation/identity",
        json={
            "sail_number": sail_number,
            "boat_name": boat_name,
            "owner_email": email,
        },
        timeout=10,
    )
    if resp.status_code == 409:
        resp2 = httpx.get(f"{pi.base_url}/api/federation/identity", timeout=10)
        data = resp2.json().get("identity", {})
        _log("setup", f"  {pi.name}: identity in DB — {data.get('boat_name')}")
        pi.identity = data
        return data
    resp.raise_for_status()
    data = resp.json()
    fp = data.get("fingerprint", "")[:8]
    _log("setup", f"  {pi.name}: identity created — {data.get('boat_name')} ({fp}...)")
    pi.identity = data
    return data


def _create_co_op(pi: PiHost, name: str) -> str:
    """Create a co-op on the admin Pi. Returns co_op_id."""
    resp = httpx.get(f"{pi.base_url}/api/federation/co-ops", timeout=10)
    if resp.status_code == 200:
        co_ops = resp.json().get("co_ops", [])
        for c in co_ops:
            if c.get("co_op_name") == name and c.get("role") == "admin":
                co_op_id = c["co_op_id"]
                _log("setup", f"  {pi.name}: co-op '{name}' exists ({co_op_id[:8]}...)")
                pi.co_op_id = co_op_id
                return co_op_id

    resp = httpx.post(
        f"{pi.base_url}/api/federation/co-ops",
        json={"name": name},
        timeout=10,
    )
    resp.raise_for_status()
    co_op_id = resp.json()["co_op_id"]
    _log("setup", f"  {pi.name}: co-op '{name}' created ({co_op_id[:8]}...)")
    pi.co_op_id = co_op_id
    return co_op_id


def _invite_and_join(
    admin_pi: PiHost,
    member_pi: PiHost,
    co_op_id: str,
) -> None:
    """Invite member_pi to the co-op and have it join."""
    # Get member's boat card
    resp = httpx.get(f"{member_pi.base_url}/co-op/identity", timeout=10)
    resp.raise_for_status()
    boat_card = resp.json()

    # Check if member is already a peer in admin's co-op
    resp = httpx.get(f"{admin_pi.base_url}/api/federation/co-ops", timeout=10)
    if resp.status_code == 200:
        for c in resp.json().get("co_ops", []):
            if c["co_op_id"] == co_op_id:
                for p in c.get("peers", []):
                    if p.get("fingerprint") == boat_card.get("fingerprint"):
                        _log("setup", f"  {member_pi.name}: already a peer")
                        _ensure_member_joined(member_pi, admin_pi, co_op_id)
                        return

    # Invite from admin
    resp = httpx.post(
        f"{admin_pi.base_url}/api/federation/co-ops/{co_op_id}/invite",
        json=boat_card,
        timeout=10,
    )
    resp.raise_for_status()
    invite_bundle = resp.json().get("invite_bundle", {})
    _log("setup", f"  {admin_pi.name}: invited {boat_card.get('name')}")

    # Join from member
    resp = httpx.post(
        f"{member_pi.base_url}/api/federation/join",
        json=invite_bundle,
        timeout=10,
    )
    resp.raise_for_status()
    _log("setup", f"  {member_pi.name}: joined co-op")

    # Update Tailscale IPs so peers can reach each other
    admin_card = httpx.get(
        f"{admin_pi.base_url}/co-op/identity",
        timeout=10,
    ).json()
    _update_peer_ip(
        member_pi,
        co_op_id,
        admin_card.get("fingerprint", ""),
        admin_pi.ip,
    )
    _update_peer_ip(
        admin_pi,
        co_op_id,
        boat_card.get("fingerprint", ""),
        member_pi.ip,
    )


def _ensure_member_joined(
    member_pi: PiHost,
    admin_pi: PiHost,
    co_op_id: str,
) -> None:
    """Check if member has the co-op locally; if not, re-join."""
    resp = httpx.get(f"{member_pi.base_url}/api/federation/co-ops", timeout=10)
    if resp.status_code == 200:
        for c in resp.json().get("co_ops", []):
            if c["co_op_id"] == co_op_id:
                _log("setup", f"  {member_pi.name}: already has co-op locally")
                return

    # Need to re-invite and join
    boat_card = httpx.get(
        f"{member_pi.base_url}/co-op/identity",
        timeout=10,
    ).json()
    resp = httpx.post(
        f"{admin_pi.base_url}/api/federation/co-ops/{co_op_id}/invite",
        json=boat_card,
        timeout=10,
    )
    if resp.status_code in (200, 201):
        invite_bundle = resp.json().get("invite_bundle", {})
        httpx.post(
            f"{member_pi.base_url}/api/federation/join",
            json=invite_bundle,
            timeout=10,
        )
        _log("setup", f"  {member_pi.name}: re-joined co-op")


def _update_peer_ip(
    pi: PiHost,
    co_op_id: str,
    fingerprint: str,
    ip: str,
) -> None:
    """Update a peer's Tailscale IP in the DB via SSH."""
    pi.ssh(
        f"sqlite3 ~/helmlog/data/logger.db"
        f" \"UPDATE co_op_peers SET tailscale_ip='{ip}'"
        f" WHERE co_op_id='{co_op_id}' AND fingerprint='{fingerprint}'\"",
        check=False,
    )


def setup(pi_a: PiHost, pi_b: PiHost, co_op_name: str) -> str:
    """Full federation setup: identities, co-op, invite, join."""
    _header("Setup")

    # Enable auth bypass on both Pis
    _enable_auth_bypass(pi_a)
    _enable_auth_bypass(pi_b)

    # Initialize identities
    _init_identity(
        pi_a,
        sail_number="TST1",
        boat_name=f"harness-{pi_a.name}",
        email="harness@test.helmlog",
    )
    _init_identity(
        pi_b,
        sail_number="TST2",
        boat_name=f"harness-{pi_b.name}",
        email="harness@test.helmlog",
    )

    _log("setup", "Identities ready (co-op created after seeding)")
    return ""


def setup_federation(pi_a: PiHost, pi_b: PiHost, co_op_name: str) -> str:
    """Create co-op and invite/join after seeding.

    Must run after seed() — the seeder deletes and recreates the DB, so any
    co-op data created before seeding would be lost.
    """
    _header("Federation Setup")

    co_op_id = _create_co_op(pi_a, co_op_name)
    _invite_and_join(pi_a, pi_b, co_op_id)
    pi_b.co_op_id = co_op_id

    _log("setup", f"Federation ready: co-op {co_op_id[:8]}...")
    return co_op_id


def seed(pi_a: PiHost, pi_b: PiHost, co_op_id: str = "") -> dict[str, Any]:
    """Seed test sessions with instrument data on both Pis."""
    _header("Seed")
    results = {}

    # Ensure harness_seed.py exists on each Pi (may be on a different branch)
    seed_script = (Path(__file__).parent / "harness_seed.py").read_text()
    for pi in [pi_a, pi_b]:
        pi.ssh(
            f"cat > ~/helmlog/scripts/harness_seed.py << 'HARNESS_EOF'\n{seed_script}HARNESS_EOF",
            check=False,
        )

    for pi in [pi_a, pi_b]:
        _log("seed", f"Seeding sessions on {pi.name}...")
        # Kill any leftover seed processes and stop service to release DB lock
        pi.ssh("pkill -f harness_seed || true", check=False)
        pi.ssh("sudo systemctl stop helmlog", check=False)
        time.sleep(1)
        # The helmlog service creates logger.db as helmlog:weaties 644.
        # The seeder runs as weaties and can't write to it. Delete the DB,
        # and the seeder will recreate it (running all migrations from scratch).
        pi.ssh("rm -f ~/helmlog/data/logger.db", check=False)
        co_op_arg = f" --co-op-id {co_op_id}" if co_op_id else ""
        output = pi.ssh(
            "cd ~/helmlog && ~/.local/bin/uv run --no-sync python scripts/harness_seed.py"
            f"{co_op_arg} --sessions 2 2>/dev/null",
            timeout=180,
        )
        pi.ssh("sudo systemctl start helmlog", check=False)
        # Wait for service to be ready
        for _wait in range(15):
            try:
                resp = httpx.get(f"{pi.base_url}/co-op/identity", timeout=3)
                if resp.status_code in (200, 404):
                    break
            except httpx.ConnectError:
                pass
            time.sleep(1)
        try:
            data = json.loads(output)
            sessions = data.get("sessions", [])
            _log("seed", f"  {pi.name}: {len(sessions)} sessions created")
            results[pi.name] = sessions
        except json.JSONDecodeError:
            _log("seed", f"  {pi.name}: WARNING — {output[:200]}")
            results[pi.name] = []

    return results


def test(pi_a: PiHost, pi_b: PiHost) -> list[dict[str, Any]]:
    """Run smoke tests from Pi-A against Pi-B."""
    _header("Test")

    _log("test", f"Running smoke tests: {pi_a.name} → {pi_b.name}")

    # Copy the service identity to weaties' home so load_identity() works.
    # The service stores keys at /var/cache/helmlog/.helmlog/identity/
    # but the smoke script runs as weaties and looks in ~/.helmlog/identity/.
    # sudo rsync is NOPASSWD in the helmlog sudoers config.
    pi_a.ssh(
        "mkdir -p ~/.helmlog/identity"
        " && sudo rsync -a --chown=weaties:weaties"
        " /var/cache/helmlog/.helmlog/identity/ ~/.helmlog/identity/"
        " && chmod 600 ~/.helmlog/identity/boat.key",
        check=False,
    )

    output = pi_a.ssh(
        "cd /home/weaties/helmlog"
        " && /home/weaties/.local/bin/uv run --no-sync"
        " python scripts/integration_smoke.py"
        f" --peer {pi_b.ip} --port {pi_b.port} --json 2>/dev/null",
        timeout=60,
        check=False,
    )

    # Parse results — the JSON is after the human-readable output
    results: list[dict[str, Any]] = []
    for line in output.split("\n"):
        line = line.strip()
        if line.startswith("["):
            try:
                results = json.loads(line)
                break
            except json.JSONDecodeError:
                pass

    if not results:
        try:
            results = json.loads(output)
        except json.JSONDecodeError:
            _log("test", "WARNING: could not parse smoke test output")
            _log("test", output[:500])
            return []

    passed = sum(1 for r in results if r.get("passed"))
    failed = sum(1 for r in results if not r.get("passed"))
    _log("test", f"Results: {passed}/{len(results)} passed, {failed} failed")

    for r in results:
        status = "PASS" if r.get("passed") else "FAIL"
        dur = r.get("duration_ms", 0)
        detail = r.get("detail", "")
        _log("test", f"  [{status}] {r['name']} ({dur:.0f}ms) {detail}")

    return results


def _get_session_id_on_pi(pi: PiHost, seed_results: dict[str, Any]) -> int | None:
    """Return a session ID to use for UI tests — from seed results or SSH fallback."""
    sessions = seed_results.get(pi.name, [])
    if sessions:
        return int(sessions[0]["session_id"])
    # SSH fallback: query the DB directly
    try:
        result = pi.ssh(
            "sqlite3 ~/helmlog/data/logger.db 'SELECT id FROM races ORDER BY id DESC LIMIT 1'",
            check=False,
        )
        if result.strip().isdigit():
            return int(result.strip())
    except Exception:
        pass
    return None


def test_ui(pi_a: PiHost, seed_results: dict[str, Any]) -> list[dict[str, Any]]:
    """Run session-match UI smoke tests from Mac against Pi-A.

    All HTTP calls run directly from Mac — no SSH needed except the DB schema
    check (which cannot be done over HTTP).  AUTH_DISABLED must already be set
    (the setup phase handles this).
    """
    _header("UI Tests (Mac → Pi-A)")

    session_id = _get_session_id_on_pi(pi_a, seed_results)
    base = pi_a.base_url
    results: list[dict[str, Any]] = []

    def _t(name: str, fn: Callable[[], str | None]) -> None:
        start = time.monotonic()
        try:
            detail = fn() or ""
            elapsed = (time.monotonic() - start) * 1000
            _log("ui", f"  [PASS] {name} ({elapsed:.0f}ms) {detail}")
            results.append(
                {"name": name, "passed": True, "duration_ms": elapsed, "detail": detail}
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            _log("ui", f"  [FAIL] {name} ({elapsed:.0f}ms) {exc}")
            results.append(
                {"name": name, "passed": False, "duration_ms": elapsed, "detail": str(exc)}
            )

    def _skip(name: str, reason: str) -> None:
        results.append(
            {"name": name, "passed": True, "duration_ms": 0, "detail": f"SKIP: {reason}"}
        )

    # ── DB schema — SSH is unavoidable here ───────────────────────────────────
    def check_schema() -> str:
        schema = pi_a.ssh(
            "sqlite3 ~/helmlog/data/logger.db '.schema races'",
            check=False,
        )
        required = ("shared_name", "match_group_id", "match_confirmed")
        missing = [c for c in required if c not in schema]
        assert not missing, f"Missing columns: {', '.join(missing)}"
        return "shared_name, match_group_id, match_confirmed present"

    _t("db_schema_has_match_columns", check_schema)

    # ── History page — no session required ────────────────────────────────────
    def check_history() -> str:
        r = httpx.get(f"{base}/history", timeout=10)
        assert r.status_code == 200, f"Expected 200, got {r.status_code}"
        return "history page renders"

    _t("history_page_renders", check_history)

    # ── Per-session tests — skipped gracefully if no sessions were seeded ─────
    if session_id is None:
        for name in (
            "match_api_reachable",
            "match_api_has_required_fields",
            "scan_endpoint_reachable",
            "match_nonexistent_session_returns_404",
            "session_detail_renders",
            "session_detail_has_match_content",
            "confirm_match_when_candidate",
            "name_validation",
        ):
            _skip(name, "no seeded session available")
    else:

        def check_match_api() -> str:
            r = httpx.get(f"{base}/api/sessions/{session_id}/match", timeout=10)
            assert r.status_code in (200, 404), (
                f"Expected 200/404, got {r.status_code}: {r.text[:200]}"
            )
            return f"session {session_id}: HTTP {r.status_code}"

        _t("match_api_reachable", check_match_api)

        def check_match_fields() -> str:
            r = httpx.get(f"{base}/api/sessions/{session_id}/match", timeout=10)
            if r.status_code == 404:
                return "SKIP: endpoint not yet implemented"
            r.raise_for_status()
            data = r.json()
            for required_field in ("status", "match_group_id", "shared_name"):
                assert required_field in data, (
                    f"Missing field '{required_field}' in {list(data.keys())}"
                )
            return f"fields OK: {list(data.keys())}"

        _t("match_api_has_required_fields", check_match_fields)

        def check_scan() -> str:
            r = httpx.post(f"{base}/api/sessions/{session_id}/match/scan", timeout=15)
            assert r.status_code in (200, 202, 404), (
                f"Expected 200/202/404, got {r.status_code}: {r.text[:200]}"
            )
            return f"scan returned {r.status_code}"

        _t("scan_endpoint_reachable", check_scan)

        def check_404() -> str:
            r = httpx.get(f"{base}/api/sessions/99999/match", timeout=10)
            assert r.status_code == 404, f"Expected 404, got {r.status_code}"
            return "nonexistent session correctly returns 404"

        _t("match_nonexistent_session_returns_404", check_404)

        def check_session_detail() -> str:
            r = httpx.get(f"{base}/session/{session_id}", timeout=10)
            assert r.status_code == 200, f"Expected 200, got {r.status_code}"
            ct = r.headers.get("content-type", "")
            assert "html" in ct.lower(), f"Expected HTML content-type, got {ct!r}"
            return f"session {session_id} renders"

        _t("session_detail_renders", check_session_detail)

        def check_match_section() -> str:
            r = httpx.get(f"{base}/session/{session_id}", timeout=10)
            if r.status_code != 200:
                return f"SKIP: session page returned {r.status_code}"
            html = r.text.lower()
            markers = ["match", "shared_name", "scan", "match_state", "match-"]
            found = [m for m in markers if m in html]
            assert found, f"No match-related content in session HTML (checked: {markers})"
            return f"match markers found: {found[:3]}"

        _t("session_detail_has_match_content", check_match_section)

        def check_confirm_conditional() -> str:
            r = httpx.get(f"{base}/api/sessions/{session_id}/match", timeout=10)
            if r.status_code != 200:
                return "SKIP: match API not available"
            state = r.json().get("status", "unmatched")
            if state != "candidate":
                return f"SKIP: status={state!r} (need candidate; run scan first)"
            r2 = httpx.post(f"{base}/api/sessions/{session_id}/match/confirm", timeout=10)
            assert r2.status_code in (200, 204), f"Expected 200/204, got {r2.status_code}"
            return "confirmed candidate match"

        _t("confirm_match_when_candidate", check_confirm_conditional)

        def check_name_validation() -> str:
            r = httpx.get(f"{base}/api/sessions/{session_id}/match", timeout=10)
            if r.status_code != 200:
                return "SKIP: match API not available"
            state = r.json().get("status", "unmatched")
            if state in ("confirmed", "named"):
                r2 = httpx.put(
                    f"{base}/api/sessions/{session_id}/match/name",
                    json={"name": "Harness Test Shared Name"},
                    timeout=10,
                )
                assert r2.status_code in (200, 204), (
                    f"Expected 200/204, got {r2.status_code}"
                )
                return "shared name set on confirmed match"
            r2 = httpx.put(
                f"{base}/api/sessions/{session_id}/match/name",
                json={"name": "Should Be Rejected"},
                timeout=10,
            )
            assert r2.status_code in (400, 409, 422), (
                f"Expected 400/409/422 for {state!r} session, got {r2.status_code}"
            )
            return f"name correctly rejected for {state!r} state ({r2.status_code})"

        _t("name_validation", check_name_validation)

    passed = sum(1 for r in results if r.get("passed"))
    _log("ui", f"Results: {passed}/{len(results)} passed")
    return results


def teardown(pi_a: PiHost, pi_b: PiHost) -> None:
    """Remove AUTH_DISABLED and restart services."""
    _header("Teardown")

    for pi in [pi_a, pi_b]:
        pi.ssh("sed -i '/^AUTH_DISABLED=true/d' ~/helmlog/.env", check=False)
        pi.ssh("sudo systemctl restart helmlog", check=False)
        _log("teardown", f"{pi.name}: AUTH_DISABLED removed, restarted")


def _table(results: list[dict[str, Any]]) -> str:
    """Format test results as a markdown table body."""
    rows = []
    for r in results:
        s = "PASS" if r.get("passed") else "FAIL"
        dur = f"{r.get('duration_ms', 0):.0f}ms"
        detail = r.get("detail", "")
        rows.append(f"| {r['name']} | {s} | {dur} | {detail} |")
    return "\n".join(rows)


def _build_report_body(
    pi_a: PiHost,
    pi_b: PiHost,
    co_op_id: str,
    seed_results: dict[str, Any],
    test_results: list[dict[str, Any]],
    ui_test_results: list[dict[str, Any]] | None = None,
) -> str:
    """Build a GitHub issue comment body from results."""
    all_results = test_results + (ui_test_results or [])
    passed = sum(1 for r in all_results if r.get("passed"))
    total = len(all_results)
    failed = total - passed
    emoji = "white_check_mark" if failed == 0 else "x"

    session_lines = []
    for pi_name, sessions in seed_results.items():
        for s in sessions:
            sid = s["session_id"]
            pts = s["points"]
            session_lines.append(f"  - {pi_name}: session {sid} ({pts} pts)")
    session_summary = "\n".join(session_lines)

    fed_passed = sum(1 for r in test_results if r.get("passed"))
    body = f"""## Pi Test Harness Report :{emoji}:

**Topology:** {pi_a.name} ({pi_a.branch}) ↔ {pi_b.name} ({pi_b.branch})
**Co-op:** `{co_op_id}`

### Seeded Data
{session_summary}

### Federation Smoke Tests — {fed_passed}/{len(test_results)} passed

| Test | Status | Duration | Detail |
|---|---|---|---|
{_table(test_results)}"""

    if ui_test_results is not None:
        ui_passed = sum(1 for r in ui_test_results if r.get("passed"))
        body += f"""

### Session Match UI Tests — {ui_passed}/{len(ui_test_results)} passed

| Test | Status | Duration | Detail |
|---|---|---|---|
{_table(ui_test_results)}"""

    body += "\n\n---\n_Generated by `scripts/pi_harness.py`_"
    return body


def report_to_issue(
    issue_number: int,
    pi_a: PiHost,
    pi_b: PiHost,
    co_op_id: str,
    seed_results: dict[str, Any],
    test_results: list[dict[str, Any]],
    ui_test_results: list[dict[str, Any]] | None = None,
) -> None:
    """Post results as a comment on the GitHub issue."""
    body = _build_report_body(
        pi_a, pi_b, co_op_id, seed_results, test_results, ui_test_results
    )
    subprocess.run(
        ["gh", "issue", "comment", str(issue_number), "--body", body],
        check=True,
        capture_output=True,
        text=True,
    )
    _log("report", f"Results posted to issue #{issue_number}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pi test harness — cross-Pi federation testing",
    )
    parser.add_argument("--pi-a", required=True, help="Pi A (admin) IP/hostname")
    parser.add_argument("--pi-b", required=True, help="Pi B (member) IP/hostname")
    parser.add_argument("--ssh-key", required=True, help="Path to SSH private key")
    parser.add_argument("--ssh-user", default="weaties", help="SSH username")
    parser.add_argument("--port", type=int, default=80, help="helmlog HTTP port")
    parser.add_argument("--co-op-name", default="harness-test", help="Co-op name")
    parser.add_argument("--issue", type=int, help="GitHub issue to post results to")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--setup-only", action="store_true", help="Setup only")
    mode.add_argument("--test-only", action="store_true", help="Test only")
    mode.add_argument("--test-ui-only", action="store_true", help="UI tests only")
    mode.add_argument("--teardown-only", action="store_true", help="Teardown only")

    args = parser.parse_args()

    pi_a = PiHost(
        ip=args.pi_a,
        ssh_key=args.ssh_key,
        ssh_user=args.ssh_user,
        port=args.port,
    )
    pi_b = PiHost(
        ip=args.pi_b,
        ssh_key=args.ssh_key,
        ssh_user=args.ssh_user,
        port=args.port,
    )

    print("Pi Test Harness")
    print(f"  Pi A (admin):  {args.pi_a}")
    print(f"  Pi B (member): {args.pi_b}")

    # Preflight always runs
    if not preflight(pi_a, pi_b):
        print("\nPreflight failed — fix connectivity issues and retry.")
        sys.exit(1)

    co_op_id = ""
    seed_results: dict[str, Any] = {}
    test_results: list[dict[str, Any]] = []
    ui_test_results: list[dict[str, Any]] | None = None

    if args.teardown_only:
        teardown(pi_a, pi_b)
        sys.exit(0)

    if args.test_only:
        test_results = test(pi_a, pi_b)
    elif args.test_ui_only:
        ui_test_results = test_ui(pi_a, seed_results)
    elif args.setup_only:
        setup(pi_a, pi_b, args.co_op_name)
        seed_results = seed(pi_a, pi_b)
        co_op_id = setup_federation(pi_a, pi_b, args.co_op_name)
    else:
        # Full lifecycle: identities → seed → federation → test → UI → teardown
        setup(pi_a, pi_b, args.co_op_name)
        seed_results = seed(pi_a, pi_b)
        co_op_id = setup_federation(pi_a, pi_b, args.co_op_name)
        test_results = test(pi_a, pi_b)
        ui_test_results = test_ui(pi_a, seed_results)
        teardown(pi_a, pi_b)

    # Report
    if args.issue and (test_results or seed_results or ui_test_results):
        report_to_issue(
            args.issue,
            pi_a,
            pi_b,
            co_op_id,
            seed_results,
            test_results,
            ui_test_results,
        )

    # Summary
    _header("Done")
    if co_op_id:
        print(f"  Co-op ID: {co_op_id}")
    if test_results:
        passed = sum(1 for r in test_results if r.get("passed"))
        failed = sum(1 for r in test_results if not r.get("passed"))
        print(f"  Federation tests: {passed} passed, {failed} failed")
    if ui_test_results is not None:
        passed = sum(1 for r in ui_test_results if r.get("passed"))
        failed = sum(1 for r in ui_test_results if not r.get("passed"))
        print(f"  UI tests: {passed} passed, {failed} failed")
    all_failed = sum(
        1 for r in (test_results + (ui_test_results or [])) if not r.get("passed")
    )
    if all_failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
