#!/usr/bin/env python3
"""
Focused Verification Suite for Backend–Frontend Recovery Authentication Contract

Tests:
  1. Authentication & Authorization Binding:
     - Cross-origin recovery-authorization blocked (HTTP 403)
     - Missing token rejected (HTTP 401: "Recovery authorization required")
     - Invalid token rejected (HTTP 401: "Invalid recovery authorization")
     - Token from another run rejected (HTTP 403: "Authorization does not match this workload")
     - Expired authorization rejected (HTTP 401: "Recovery authorization expired")
     - Obsolete snapshot_version rejected (HTTP 409: "Deadlock state changed; refresh analysis")
     - Changed deadlock_cycle rejected (HTTP 409: "Deadlock state changed; refresh analysis")
     - JS-rounded float create_time_100ns rejected (confirming exact 64-bit binding!)
     - Reused single-use authorization rejected (HTTP 401: "Recovery authorization has already been used")
  2. Process Protection:
     - approved=False performs no termination (HTTP 400)
     - Wrong PID, wrong creation time, wrong sandbox rejected (HTTP 403)
     - Supervisor PID cannot be authorized or terminated (HTTP 403)
     - Critical OS process PID 4 cannot be authorized or terminated (HTTP 403)
     - Unrelated process outside the Job Object cannot be authorized or terminated (HTTP 403)
  3. Complete Selective Recovery via Dashboard Authorization Flow:
     - POST /recovery-authorization -> POST /recover with X-Recovery-Token & create_time_100ns_str
     - Only Worker A terminates; Supervisor & Job Object remain active
     - Worker B receives real WAIT_ABANDONED, validates state, releases mutexes, exits 0
     - Post-recovery WFG has 0 cycles and Banker's is SAFE
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
from backend.isolation_engine import get_process_creation_time_100ns


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http_json(method: str, url: str, payload=None, headers=None, timeout=8.0):
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"raw": body}
        return e.code, parsed


def _wait_deadlocked_run(base_url: str, seen: set, proc: subprocess.Popen, timeout_s: float = 20.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        code, data = _http_json("GET", f"{base_url}/api/deadlock/live/runs")
        assert code == 200
        fresh = [r for r in data.get("runs", []) if r["run_id"] not in seen]
        if fresh:
            rid = fresh[0]["run_id"]
            c2, snap = _http_json("GET", f"{base_url}/api/deadlock/live/runs/{rid}")
            assert c2 == 200
            if snap["deadlock_detection"]["has_deadlock"] and all(
                sum(w["requesting"].values()) >= 1 for w in snap["workers"]
            ):
                return rid, snap
        assert proc.poll() is None, f"Workload exited early (rc={proc.returncode})"
        time.sleep(0.2)
    raise AssertionError("Timed out waiting for deadlocked run")


def run_tests():
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    print("=" * 80)
    print(f"  RECOVERY AUTHENTICATION CONTRACT & SECURITY SUITE ({base_url})")
    print("=" * 80)

    server = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "backend.main:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Also spawn an unrelated external Python process outside any Job Object to verify process protection
    outside_proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    outside_ct = get_process_creation_time_100ns(outside_proc.pid)

    p1 = None
    p2 = None
    try:
        for _ in range(50):
            try:
                c, _ = _http_json("GET", f"{base_url}/api/system/mode", timeout=1.0)
                if c == 200:
                    break
            except Exception:
                time.sleep(0.2)

        seen = set()
        # Launch Run 1 (Circular Deadlock)
        p1 = subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "workloads" / "circular_deadlock.py"), "--server", base_url, "--timeout", "60", "--delay", "0.2"],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        run1_id, snap1 = _wait_deadlocked_run(base_url, seen, p1)
        seen.add(run1_id)

        # Launch Run 2 (second independent run for cross-run token rejection testing)
        p2 = subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "workloads" / "circular_deadlock.py"), "--server", base_url, "--timeout", "60", "--delay", "0.2"],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        run2_id, snap2 = _wait_deadlocked_run(base_url, seen, p2)
        seen.add(run2_id)

        victim1 = snap1["deadlock_detection"]["recommended_victim_details"]
        sb1 = snap1["sandbox"]["sandbox_name"]
        ver1 = snap1["snapshot_version"]
        cycle1 = snap1["deadlock_detection"]["cycles"][0]
        sup1_pid = snap1["supervisor"]["pid"]

        # ── 1. Cross-origin authorization request blocked ─────────────────
        c, d = _http_json(
            "POST",
            f"{base_url}/api/deadlock/live/runs/{run1_id}/recovery-authorization",
            headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
            payload={"victim_pid": victim1["pid"], "snapshot_version": ver1},
        )
        assert c == 403, f"Expected 403 for cross-origin auth, got {c}: {d}"
        print("  [PASS] Cross-origin recovery-authorization rejected (HTTP 403)")

        # ── 2. Missing token rejected ─────────────────────────────────────
        c, d = _http_json(
            "POST",
            f"{base_url}/api/deadlock/live/runs/{run1_id}/recover",
            payload={
                "sandbox_name": sb1,
                "pid": victim1["pid"],
                "create_time_100ns": victim1["create_time_100ns_str"],
                "approved": True,
                "snapshot_version": ver1,
                "deadlock_cycle": cycle1,
            },
        )
        assert c == 401 and d.get("detail") == "Recovery authorization required", f"Got {c}: {d}"
        print("  [PASS] Missing token rejected (HTTP 401: 'Recovery authorization required')")

        # ── 3. Invalid token rejected ─────────────────────────────────────
        c, d = _http_json(
            "POST",
            f"{base_url}/api/deadlock/live/runs/{run1_id}/recover",
            headers={"X-Recovery-Token": "forged_fake_token_12345"},
            payload={
                "sandbox_name": sb1,
                "pid": victim1["pid"],
                "create_time_100ns": victim1["create_time_100ns_str"],
                "approved": True,
                "snapshot_version": ver1,
                "deadlock_cycle": cycle1,
            },
        )
        assert c == 401 and d.get("detail") == "Invalid recovery authorization", f"Got {c}: {d}"
        print("  [PASS] Invalid token rejected (HTTP 401: 'Invalid recovery authorization')")

        # ── 4. Token from Run 2 rejected on Run 1 ─────────────────────────
        c_a2, auth2 = _http_json(
            "POST",
            f"{base_url}/api/deadlock/live/runs/{run2_id}/recovery-authorization",
            headers={"Origin": base_url, "Sec-Fetch-Site": "same-origin"},
            payload={"snapshot_version": snap2["snapshot_version"]},
        )
        assert c_a2 == 200 and auth2.get("token")
        c, d = _http_json(
            "POST",
            f"{base_url}/api/deadlock/live/runs/{run1_id}/recover",
            headers={"X-Recovery-Token": auth2["token"]},
            payload={
                "sandbox_name": sb1,
                "pid": victim1["pid"],
                "create_time_100ns": victim1["create_time_100ns_str"],
                "approved": True,
                "snapshot_version": ver1,
                "deadlock_cycle": cycle1,
            },
        )
        assert c == 403 and d.get("detail") == "Authorization does not match this workload", f"Got {c}: {d}"
        print("  [PASS] Cross-run token rejected (HTTP 403: 'Authorization does not match this workload')")

        # ── 5. Expired token rejected ─────────────────────────────────────
        c_exp, auth_exp = _http_json(
            "POST",
            f"{base_url}/api/deadlock/live/runs/{run1_id}/recovery-authorization",
            headers={"Origin": base_url, "Sec-Fetch-Site": "same-origin"},
            payload={"victim_pid": victim1["pid"], "snapshot_version": ver1, "ttl_seconds": 1.0},
        )
        assert c_exp == 200
        time.sleep(1.2)
        c, d = _http_json(
            "POST",
            f"{base_url}/api/deadlock/live/runs/{run1_id}/recover",
            headers={"X-Recovery-Token": auth_exp["token"]},
            payload={
                "sandbox_name": auth_exp["sandbox_name"],
                "pid": auth_exp["pid"],
                "create_time_100ns": auth_exp["create_time_100ns_str"],
                "approved": True,
                "snapshot_version": auth_exp["snapshot_version"],
                "deadlock_cycle": auth_exp["deadlock_cycle"],
            },
        )
        assert c == 401 and d.get("detail") == "Recovery authorization expired", f"Got {c}: {d}"
        print("  [PASS] Expired recovery token rejected (HTTP 401: 'Recovery authorization expired')")

        # ── 6. Issue valid fresh authorization for Run 1 ──────────────────
        c_a1, auth1 = _http_json(
            "POST",
            f"{base_url}/api/deadlock/live/runs/{run1_id}/recovery-authorization",
            headers={"Origin": base_url, "Sec-Fetch-Site": "same-origin"},
            payload={"victim_pid": victim1["pid"], "snapshot_version": ver1},
        )
        assert c_a1 == 200
        assert auth1["create_time_100ns_str"] == str(victim1["create_time_100ns"])
        assert auth1["snapshot_version"] == ver1
        assert auth1["deadlock_cycle"] == cycle1

        # ── 7. Obsolete snapshot_version & changed deadlock_cycle rejected ─
        c, d = _http_json(
            "POST",
            f"{base_url}/api/deadlock/live/runs/{run1_id}/recover",
            headers={"X-Recovery-Token": auth1["token"]},
            payload={
                "sandbox_name": auth1["sandbox_name"],
                "pid": auth1["pid"],
                "create_time_100ns": auth1["create_time_100ns_str"],
                "approved": True,
                "snapshot_version": ver1 - 1,
                "deadlock_cycle": auth1["deadlock_cycle"],
            },
        )
        assert c == 409 and d.get("detail") == "Deadlock state changed; refresh analysis", f"Got {c}: {d}"

        c, d = _http_json(
            "POST",
            f"{base_url}/api/deadlock/live/runs/{run1_id}/recover",
            headers={"X-Recovery-Token": auth1["token"]},
            payload={
                "sandbox_name": auth1["sandbox_name"],
                "pid": auth1["pid"],
                "create_time_100ns": auth1["create_time_100ns_str"],
                "approved": True,
                "snapshot_version": auth1["snapshot_version"],
                "deadlock_cycle": ["worker_b", "worker_a"],
            },
        )
        assert c == 409 and d.get("detail") == "Deadlock state changed; refresh analysis", f"Got {c}: {d}"
        print("  [PASS] Obsolete snapshot_version & changed deadlock_cycle rejected (HTTP 409)")

        # ── 8. Process protection checks (approval=False, wrong PID/CT/sandbox, supervisor, critical, outside job) ──
        # approval=False
        c, d = _http_json(
            "POST",
            f"{base_url}/api/deadlock/live/runs/{run1_id}/recover",
            headers={"X-Recovery-Token": auth1["token"]},
            payload={
                "sandbox_name": auth1["sandbox_name"],
                "pid": auth1["pid"],
                "create_time_100ns": auth1["create_time_100ns_str"],
                "approved": False,
                "snapshot_version": auth1["snapshot_version"],
                "deadlock_cycle": auth1["deadlock_cycle"],
            },
        )
        assert c == 400

        # JS-rounded float create_time_100ns (exact regression check for the 64-bit bug!)
        rounded_ct = int(float(victim1["create_time_100ns"]))
        if rounded_ct == victim1["create_time_100ns"]:
            rounded_ct += 1
        c, d = _http_json(
            "POST",
            f"{base_url}/api/deadlock/live/runs/{run1_id}/recover",
            headers={"X-Recovery-Token": auth1["token"]},
            payload={
                "sandbox_name": auth1["sandbox_name"],
                "pid": auth1["pid"],
                "create_time_100ns": rounded_ct,
                "approved": True,
                "snapshot_version": auth1["snapshot_version"],
                "deadlock_cycle": auth1["deadlock_cycle"],
            },
        )
        assert c == 403 and d.get("detail") == "Selected process is no longer eligible", f"Got {c}: {d}"

        # Wrong sandbox
        c, d = _http_json(
            "POST",
            f"{base_url}/api/deadlock/live/runs/{run1_id}/recover",
            headers={"X-Recovery-Token": auth1["token"]},
            payload={
                "sandbox_name": "wrong_sandbox_name",
                "pid": auth1["pid"],
                "create_time_100ns": auth1["create_time_100ns_str"],
                "approved": True,
                "snapshot_version": auth1["snapshot_version"],
                "deadlock_cycle": auth1["deadlock_cycle"],
            },
        )
        assert c == 403

        # Supervisor PID, Critical PID 4, and outside-Job PID cannot be authorized
        for bad_pid, label in [(sup1_pid, "supervisor"), (4, "System PID 4"), (outside_proc.pid, "outside-Job process")]:
            c_bad, d_bad = _http_json(
                "POST",
                f"{base_url}/api/deadlock/live/runs/{run1_id}/recovery-authorization",
                headers={"Origin": base_url, "Sec-Fetch-Site": "same-origin"},
                payload={"victim_pid": bad_pid, "snapshot_version": ver1},
            )
            assert c_bad == 403, f"Expected 403 authorizing {label} (PID {bad_pid}), got {c_bad}: {d_bad}"

        # Wrong PID (outside-Job PID) on /recover with valid token
        c, d = _http_json(
            "POST",
            f"{base_url}/api/deadlock/live/runs/{run1_id}/recover",
            headers={"X-Recovery-Token": auth1["token"]},
            payload={
                "sandbox_name": auth1["sandbox_name"],
                "pid": outside_proc.pid,
                "create_time_100ns": str(outside_ct),
                "approved": True,
                "snapshot_version": auth1["snapshot_version"],
                "deadlock_cycle": auth1["deadlock_cycle"],
            },
        )
        assert c == 403
        assert psutil.pid_exists(outside_proc.pid), "Outside process must NEVER be terminated!"
        assert psutil.pid_exists(victim1["pid"]), "Failed requests must NEVER terminate victim!"

        # Verify live deadlock state is STILL preserved intact after all rejections
        _, snap_after_rejects = _http_json("GET", f"{base_url}/api/deadlock/live/runs/{run1_id}")
        assert snap_after_rejects["deadlock_detection"]["has_deadlock"] is True
        print("  [PASS] All process-protection guards passed & real live deadlock state preserved intact")

        # ── 9. Valid dashboard-authorized selective recovery succeeds ─────
        c_ok, rec_ok = _http_json(
            "POST",
            f"{base_url}/api/deadlock/live/runs/{run1_id}/recover",
            headers={"X-Recovery-Token": auth1["token"]},
            payload={
                "sandbox_name": auth1["sandbox_name"],
                "pid": auth1["pid"],
                "create_time_100ns": auth1["create_time_100ns_str"],
                "approved": True,
                "snapshot_version": auth1["snapshot_version"],
                "deadlock_cycle": auth1["deadlock_cycle"],
                "operator_note": "Dashboard authorized recovery test",
            },
            timeout=12.0,
        )
        assert c_ok == 200, f"Expected 200 on valid recovery, got {c_ok}: {rec_ok}"
        assert rec_ok["deadlock_resolved"] is True
        assert rec_ok["selective_recovery_succeeded"] is True
        rr = rec_ok["recovery_record"]
        assert rr["termination"]["exited_confirmed"] is True
        assert rr["termination"]["sandbox_still_active"] is True
        assert rr["post_recovery_has_deadlock"] is False
        assert rr["post_recovery_banker_safe"] is True
        print("  [PASS] Valid dashboard-authorized recovery succeeded (HTTP 200, exact 64-bit string create_time_100ns)")

        # ── 10. Single-use token replay rejected ──────────────────────────
        c_rep, d_rep = _http_json(
            "POST",
            f"{base_url}/api/deadlock/live/runs/{run1_id}/recover",
            headers={"X-Recovery-Token": auth1["token"]},
            payload={
                "sandbox_name": auth1["sandbox_name"],
                "pid": auth1["pid"],
                "create_time_100ns": auth1["create_time_100ns_str"],
                "approved": True,
                "snapshot_version": auth1["snapshot_version"],
                "deadlock_cycle": auth1["deadlock_cycle"],
            },
        )
        assert c_rep == 401 and d_rep.get("detail") == "Recovery authorization has already been used", f"Got {c_rep}: {d_rep}"
        print("  [PASS] Reused single-use recovery token rejected (HTTP 401)")

        # Confirm Run 1 supervisor exited 0 & Worker B logged WAIT_ABANDONED
        out1, err1 = p1.communicate(timeout=15)
        assert p1.returncode == 0, f"Run 1 supervisor failed: {p1.returncode}\n{out1}\n{err1}"
        assert "WAIT_ABANDONED" in out1
        assert psutil.pid_exists(outside_proc.pid), "Unrelated outside process must remain untouched!"
        print("  [PASS] Worker A terminated, Worker B handled WAIT_ABANDONED & exited 0, Supervisor exited 0, outside process untouched")

        # Clean up Run 2
        _http_json("DELETE", f"{base_url}/api/deadlock/live/runs/{run2_id}")
        try:
            p2.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p2.kill()

    finally:
        for p in (p1, p2, outside_proc, server):
            if p and p.poll() is None:
                try:
                    p.terminate()
                    p.wait(timeout=4)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass

    print("\n" + "=" * 80)
    print("  ALL RECOVERY AUTHENTICATION & SECURITY CONTRACT TESTS PASSED!")
    print("=" * 80)


if __name__ == "__main__":
    run_tests()
