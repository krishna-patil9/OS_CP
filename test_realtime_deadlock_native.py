#!/usr/bin/env python3
"""
Comprehensive Windows-Native Real-Time Deadlock Detection & Recovery Test Suite

Verifies ALL architectural, security, synchronization, and recovery criteria
directly on the host Windows OS (zero Docker / WSL2):
  1. Windows Job Object mandatory limit enforcement & QueryInformationJobObject verification
     (CPU hard cap, aggregate job memory, per-process memory, active-process, kill-on-close,
      distinct working-set limits).
  2. Security boundaries: protected process rejection, server PID protection,
     PID + creation-time validation, per-run token auth.
  3. Multi-instance deadlock detection & Banker's Algorithm semantic separation.
  4. Fail-closed containment when backend is unreachable.
  5. Full two-terminal live run (`uvicorn` server + `python workloads\\deadlock_demo.py`):
     - Dedicated Job Object attachment before worker creation
     - Child worker Job Object inheritance & startup gate
     - Real cross-process Windows Named Mutex deadlock
     - DFS cycle detection + live Banker's safety analysis + lowest recovery_priority victim
     - Explicit user-approved selective TerminateProcess (Supervisor & Job Object remain active)
     - Actual OS WAIT_ABANDONED (0x80) ownership, state validation, and same-thread ReleaseMutex
     - Post-recovery verification & automatic Job Object cleanup
  6. Bounded watchdog emergency cleanup honesty (never reported as selective recovery).
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

from backend.deadlock_engine import BankersAlgorithm, ProcessNode, Resource, WaitForGraph
from backend.isolation_engine import (
    IsolationEngine,
    SandboxConfig,
    get_process_creation_time_100ns,
)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http_json(method: str, url: str, payload=None, headers=None, timeout=5.0):
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


def test_1_job_object_limits_and_verification():
    print("\n[TEST 1] Windows Job Object Dual Committed Memory + CPU Hard Cap + Verification")
    iso = IsolationEngine()
    try:
        cfg = SandboxConfig(
            name="unit_verify_limits",
            cpu_rate_percent=20,
            per_process_memory_limit_mb=256,
            job_memory_limit_mb=512,
            max_active_processes=4,
            kill_on_close=True,
            strict_limits=True,
        )
        c = iso.create_sandbox(cfg)
        vl = c.verified_limits
        assert vl["verified"] is True, f"Limits failed verification: {vl}"
        assert vl["cpu_hard_cap_active"] is True and vl["cpu_rate_percent"] == 20
        assert vl["process_memory_limit_mb"] == 256.0
        assert vl["job_memory_limit_mb"] == 512.0
        assert vl["active_process_limit"] == 4
        assert vl["kill_on_job_close"] is True
        assert vl["working_set_limit_active"] is False, "Working set must stay distinct from committed memory!"
        print(f"  [PASS] Verified kernel limits via QueryInformationJobObject: {vl}")

        # Verify invalid memory hierarchy (job < per-process) fails closed
        try:
            iso.create_sandbox(
                SandboxConfig(
                    name="invalid_mem_hierarchy",
                    per_process_memory_limit_mb=512,
                    job_memory_limit_mb=256,
                )
            )
            raise AssertionError("Expected ValueError for job_memory < per_process_memory")
        except ValueError:
            print("  [PASS] Invalid memory configuration rejected fail-closed")

        # Verify protected process & server self-assignment rejected
        try:
            iso.assign_process("unit_verify_limits", 4)
            raise AssertionError("Expected PermissionError for System PID 4")
        except PermissionError:
            print("  [PASS] Critical OS process PID 4 protected from assignment")

        try:
            iso.assign_process("unit_verify_limits", os.getpid())
            raise AssertionError("Expected PermissionError for current server PID")
        except PermissionError:
            print("  [PASS] Current backend process PID protected from assignment")
    finally:
        iso.cleanup_all_sandboxes()


def test_2_multi_instance_and_banker_semantics():
    print("\n[TEST 2] Multi-Instance Deadlock Detection & Banker's Semantics Separation")

    # 2a. Multi-instance SAFE cycle: R1 has 2 instances (held by P1 & P3), P2 requests R1 -> P3 finishes -> no deadlock
    wfg_safe = WaitForGraph()
    wfg_safe.add_resource(Resource("R1", "TapeDrive", 2, 0))
    wfg_safe.add_resource(Resource("R2", "Printer", 1, 0))
    wfg_safe.add_process(ProcessNode("P1", "P1", recovery_priority=1, allocated={"R1": 1}, requesting={"R2": 1}))
    wfg_safe.add_process(ProcessNode("P2", "P2", recovery_priority=2, allocated={"R2": 1}, requesting={"R1": 1}))
    wfg_safe.add_process(ProcessNode("P3", "P3", recovery_priority=3, allocated={"R1": 1}, requesting={}))
    res_safe = wfg_safe.detect_deadlock()
    assert res_safe.is_multi_instance_graph is True
    assert res_safe.has_deadlock is False, f"Multi-instance non-deadlock falsely flagged: {res_safe}"
    print("  [PASS] Multi-instance cycle with finishable holder correctly classified as NO DEADLOCK")

    # 2b. Multi-instance GENUINE deadlock: P1, P2, P3 all blocked on exhausted resources
    wfg_dead = WaitForGraph()
    wfg_dead.add_resource(Resource("R1", "TapeDrive", 2, 0))
    wfg_dead.add_resource(Resource("R2", "Printer", 1, 0))
    wfg_dead.add_process(ProcessNode("P1", "P1", recovery_priority=3, allocated={"R1": 1}, requesting={"R2": 1}))
    wfg_dead.add_process(ProcessNode("P2", "P2", recovery_priority=1, allocated={"R2": 1}, requesting={"R1": 1}))
    wfg_dead.add_process(ProcessNode("P3", "P3", recovery_priority=2, allocated={"R1": 1}, requesting={"R2": 1}))
    res_dead = wfg_dead.detect_deadlock()
    assert res_dead.has_deadlock is True
    assert set(res_dead.multi_instance_deadlocked) == {"P1", "P2", "P3"}
    assert res_dead.recommended_victim == "P2", f"Expected lowest recovery_priority P2, got {res_dead.recommended_victim}"
    print(f"  [PASS] Multi-instance deadlock detected; lowest recovery_priority victim={res_dead.recommended_victim}")

    # 2c. Banker's matrix validation & UNSAFE != DEADLOCK distinction
    banker = BankersAlgorithm()
    try:
        banker.check_safety(["P0"], ["R0"], [0], [[1]], [[2]])
        raise AssertionError("Expected ValueError when allocation > max_claim")
    except ValueError:
        print("  [PASS] Banker's Algorithm rejected invalid matrix (allocation > max_claim)")

    # Two processes each holding 1 lock (max_claim=[1,1]), neither requesting yet:
    # Banker's = UNSAFE, WFG = NO DEADLOCK
    b_unsafe = banker.check_safety(["A", "B"], ["Lock_A", "Lock_B"], [0, 0], [[1, 1], [1, 1]], [[1, 0], [0, 1]])
    assert b_unsafe.is_safe is False
    wfg_no_req = WaitForGraph()
    wfg_no_req.add_resource(Resource("Lock_A", "Lock_A", 1, 0))
    wfg_no_req.add_resource(Resource("Lock_B", "Lock_B", 1, 0))
    wfg_no_req.add_process(ProcessNode("A", "A", allocated={"Lock_A": 1}, requesting={}))
    wfg_no_req.add_process(ProcessNode("B", "B", allocated={"Lock_B": 1}, requesting={}))
    assert wfg_no_req.detect_deadlock().has_deadlock is False
    print("  [PASS] Separated Banker's UNSAFE state from active WFG DEADLOCK")


def test_3_fail_closed_when_backend_unreachable():
    print("\n[TEST 3] Fail-Closed Containment When Backend Is Unreachable")
    dead_port = _find_free_port()
    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "workloads" / "deadlock_demo.py"),
            "--server",
            f"http://127.0.0.1:{dead_port}",
            "--timeout",
            "15",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode != 0, "Workload MUST abort with non-zero exit when sandbox registration fails!"
    combined = proc.stdout + "\n" + proc.stderr
    assert "CONTAINMENT ABORT" in combined, f"Expected containment abort message, got:\n{combined}"
    print(f"  [PASS] Unreachable backend caused immediate fail-closed abort (rc={proc.returncode})")


def test_4_and_5_live_end_to_end_and_watchdog():
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    print(f"\n[TEST 4] Live End-to-End Two-Terminal Workflow (Server on {base_url})")

    # Start real FastAPI backend server (Terminal 1 equivalent)
    server_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        # Wait for server readiness
        ready = False
        for _ in range(50):
            try:
                code, _ = _http_json("GET", f"{base_url}/api/system/mode", timeout=1.0)
                if code == 200:
                    ready = True
                    break
            except Exception:
                time.sleep(0.2)
        assert ready, "Backend uvicorn server failed to start in time"
        print(f"  [PASS] Terminal 1 backend server ready at {base_url} (Normal Mode default)")

        # Launch independent workload (Terminal 2 exact workflow)
        demo_proc = subprocess.Popen(
            [
                sys.executable,
                str(PROJECT_ROOT / "workloads" / "deadlock_demo.py"),
                "--server",
                base_url,
                "--timeout",
                "45",
                "--delay",
                "0.3",
            ],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Wait for live deadlock detection
        run_id = None
        live_snap = None
        deadline = time.time() + 20.0
        while time.time() < deadline:
            code, data = _http_json("GET", f"{base_url}/api/deadlock/live/runs")
            assert code == 200
            runs = data.get("runs", [])
            if runs:
                run_id = runs[0]["run_id"]
                assert "run_token" not in runs[0], "Secret run_token must NEVER be leaked in GET /runs!"
                c2, snap = _http_json("GET", f"{base_url}/api/deadlock/live/runs/{run_id}")
                assert c2 == 200
                assert "run_token" not in snap, "Secret run_token must NEVER be leaked in GET /runs/{id}!"
                if snap["deadlock_detection"]["has_deadlock"]:
                    live_snap = snap
                    break
            assert demo_proc.poll() is None, (
                f"deadlock_demo.py exited prematurely (rc={demo_proc.returncode}):\n"
                f"{demo_proc.stdout.read()}\n{demo_proc.stderr.read()}"
            )
            time.sleep(0.25)

        assert live_snap is not None, "Live cross-process deadlock was not detected within 20s!"
        sb = live_snap["sandbox"]
        vl = sb["verified_limits"]
        sup = live_snap["supervisor"]
        workers = {w["worker_id"]: w for w in live_snap["workers"]}
        dl = live_snap["deadlock_detection"]
        bk = live_snap["banker_analysis"]

        # Verify containment & Job Object inheritance
        assert sb["is_active"] is True
        assert vl["verified"] is True
        assert vl["cpu_rate_percent"] == 20
        assert vl["job_memory_limit_mb"] == 512.0
        assert vl["process_memory_limit_mb"] == 256.0
        assert vl["active_process_limit"] == 4
        assert vl["kill_on_job_close"] is True
        assert live_snap["startup_gate_ready"] is True
        assert sup["alive"] is True
        assert "worker_a" in workers and "worker_b" in workers
        wa, wb = workers["worker_a"], workers["worker_b"]
        assert len({sup["pid"], wa["pid"], wb["pid"]}) == 3, "Supervisor, Worker A, Worker B must be 3 distinct OS PIDs!"
        assert wa["in_job_verified"] is True and wb["in_job_verified"] is True
        assert {sup["pid"], wa["pid"], wb["pid"]}.issubset(set(sb["kernel_member_pids"])), (
            f"All 3 PIDs must appear in Windows JobObjectBasicProcessIdList: {sb['kernel_member_pids']}"
        )
        assert 0.0 < live_snap["remaining_seconds"] <= 45.0

        print(
            f"  [PASS] Containment verified: Job={sb['job_name']}, "
            f"Supervisor PID={sup['pid']}, Worker A PID={wa['pid']} (RecPri={wa['recovery_priority']}), "
            f"Worker B PID={wb['pid']} (RecPri={wb['recovery_priority']}), "
            f"Kernel Member PIDs={sb['kernel_member_pids']}"
        )

        # Verify DFS cycle, Banker's live safety, and lowest recovery_priority victim
        assert dl["has_deadlock"] is True
        assert len(dl["cycles"]) >= 1
        assert set(dl["involved_processes"]) == {"worker_a", "worker_b"}
        assert dl["recommended_victim"] == "worker_a", f"Expected worker_a (RecPri=1), got {dl['recommended_victim']}"
        assert bk["is_safe"] is False
        assert bk["available"] == [0, 0]
        print(
            f"  [PASS] Live Deadlock confirmed: cycle={dl['cycles'][0]}, "
            f"Banker safe={bk['is_safe']}, recommended_victim={dl['recommended_victim']} (RecPri=1)"
        )

        # Security / Auth negative tests
        # 1) Unauthenticated event submission must be rejected (HTTP 401)
        code_unauth_ev, _ = _http_json(
            "POST",
            f"{base_url}/api/deadlock/live/runs/{run_id}/events",
            payload={
                "worker_id": "worker_a",
                "pid": wa["pid"],
                "tid": wa["tid"],
                "create_time_100ns": wa["create_time_100ns"],
                "event_type": "released",
                "resource_id": "Lock_A",
            },
        )
        assert code_unauth_ev == 401, f"Expected 401 for unauthenticated event, got {code_unauth_ev}"
        print("  [PASS] Unauthenticated sync event rejected with HTTP 401")

        # 2) Unauthenticated recovery must be rejected (HTTP 401)
        code_unauth_rec, _ = _http_json(
            "POST",
            f"{base_url}/api/deadlock/live/runs/{run_id}/recover",
            payload={
                "sandbox_name": sb["sandbox_name"],
                "pid": wa["pid"],
                "create_time_100ns": wa["create_time_100ns"],
                "approved": True,
            },
        )
        assert code_unauth_rec == 401, f"Expected 401 for unauthenticated recovery, got {code_unauth_rec}"
        print("  [PASS] Unauthenticated recovery rejected with HTTP 401")

        # Fetch deadlock-bound recovery challenge token
        code_ch, challenge = _http_json("GET", f"{base_url}/api/deadlock/live/runs/{run_id}/recovery-challenge")
        assert code_ch == 200 and challenge["pid"] == wa["pid"]

        # 3) Recovery with approved=False must be rejected (HTTP 400)
        code_no_app, _ = _http_json(
            "POST",
            f"{base_url}/api/deadlock/live/runs/{run_id}/recover",
            headers={"X-Recovery-Token": challenge["token"]},
            payload={
                "sandbox_name": challenge["sandbox_name"],
                "pid": challenge["pid"],
                "create_time_100ns": challenge["create_time_100ns"],
                "approved": False,
            },
        )
        assert code_no_app == 400, f"Expected 400 when approved=False, got {code_no_app}"
        print("  [PASS] Unapproved recovery (approved=False) rejected with HTTP 400")

        # 4) Recovery with wrong creation time must be rejected (HTTP 401/403)
        code_bad_ct, _ = _http_json(
            "POST",
            f"{base_url}/api/deadlock/live/runs/{run_id}/recover",
            headers={"X-Recovery-Token": challenge["token"]},
            payload={
                "sandbox_name": challenge["sandbox_name"],
                "pid": challenge["pid"],
                "create_time_100ns": challenge["create_time_100ns"] + 999,
                "approved": True,
            },
        )
        assert code_bad_ct in (401, 403), f"Expected 401/403 for creation time mismatch, got {code_bad_ct}"
        print(f"  [PASS] Creation-time mismatch rejected with HTTP {code_bad_ct}")

        # 5) Explicit User-Approved Selective Recovery of Worker A
        code_rec, rec_data = _http_json(
            "POST",
            f"{base_url}/api/deadlock/live/runs/{run_id}/recover",
            headers={"X-Recovery-Token": challenge["token"]},
            payload={
                "sandbox_name": challenge["sandbox_name"],
                "pid": challenge["pid"],
                "create_time_100ns": challenge["create_time_100ns"],
                "approved": True,
                "operator_note": "Automated test explicit approval",
            },
            timeout=10.0,
        )
        assert code_rec == 200, f"Expected 200 on approved recovery, got {code_rec}: {rec_data}"
        rec_record = rec_data["recovery_record"]
        assert rec_data["deadlock_resolved"] is True
        assert rec_data["selective_recovery_succeeded"] is True
        assert rec_record["termination"]["exited_confirmed"] is True
        assert rec_record["termination"]["sandbox_still_active"] is True
        assert rec_record["post_recovery_has_deadlock"] is False
        assert rec_record["post_recovery_banker_safe"] is True
        assert "Lock_A" in rec_record["abandoned_resources"]

        # Wait for supervisor (deadlock_demo.py) to observe Worker B clean completion and exit
        stdout, stderr = demo_proc.communicate(timeout=15)
        assert demo_proc.returncode == 0, f"Supervisor expected exit code 0, got {demo_proc.returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
        assert "WAIT_ABANDONED" in stdout, f"Worker B must log real OS WAIT_ABANDONED!\nSTDOUT:\n{stdout}"
        assert not psutil.pid_exists(wa["pid"]), "Victim Worker A PID must be terminated"

        # Fetch final run snapshot & verify WAIT_ABANDONED audit trail + Job Object cleanup
        time.sleep(1.0)
        code_fin, fin_snap = _http_json("GET", f"{base_url}/api/deadlock/live/runs/{run_id}")
        assert code_fin == 200
        evt_types = [e["event_type"] for e in fin_snap["events"]]
        assert "mutex_abandoned" in evt_types, f"Expected mutex_abandoned event, got {evt_types}"
        assert "abandoned_acquired" in evt_types, f"Expected abandoned_acquired (WAIT_ABANDONED) event, got {evt_types}"
        assert "state_validated" in evt_types, f"Expected state_validated event while holding abandoned mutex, got {evt_types}"
        assert "released" in evt_types, f"Expected same-thread ReleaseMutex event after validation, got {evt_types}"
        assert fin_snap["status"] == "completed"
        assert fin_snap["selective_recovery_succeeded"] is True

        # Wait up to 4s for deferred Job Object cleanup
        cleaned = False
        for _ in range(20):
            _, sbs = _http_json("GET", f"{base_url}/api/sandboxes")
            if not any(s["name"] == sb["sandbox_name"] for s in sbs):
                cleaned = True
                break
            time.sleep(0.2)
        assert cleaned, f"Job Object '{sb['sandbox_name']}' was not cleaned up after run completion!"
        print("  [PASS] Selective recovery verified: ONLY Worker A terminated; Worker B received WAIT_ABANDONED, validated state, released mutexes, exited 0; Supervisor exited 0; Job Object cleaned up!")

        # ── TEST 5: WATCHDOG EMERGENCY CLEANUP HONESTY TEST ───────────────
        print("\n[TEST 5] Watchdog Expiration Emergency Cleanup Honesty (Never Reported as Selective Recovery)")
        wd_proc = subprocess.Popen(
            [
                sys.executable,
                str(PROJECT_ROOT / "workloads" / "deadlock_demo.py"),
                "--server",
                base_url,
                "--timeout",
                "10",
                "--delay",
                "0.2",
            ],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        wd_run_id = None
        t_start = time.time()
        while time.time() - t_start < 15.0:
            _, data = _http_json("GET", f"{base_url}/api/deadlock/live/runs")
            new_runs = [r for r in data.get("runs", []) if r["run_id"] != run_id]
            if new_runs:
                wd_run_id = new_runs[0]["run_id"]
                if new_runs[0]["has_deadlock"]:
                    break
            time.sleep(0.25)
        assert wd_run_id is not None, "Watchdog test run did not register"
        print(f"  [INFO] Watchdog test run '{wd_run_id}' deadlocked; waiting for 10s server watchdog expiry...")

        # Do NOT approve recovery; wait for watchdog to fire
        wd_expired = False
        wd_snap = None
        for _ in range(40):
            _, snap = _http_json("GET", f"{base_url}/api/deadlock/live/runs/{wd_run_id}")
            if snap["status"] == "timed_out":
                wd_expired = True
                wd_snap = snap
                break
            time.sleep(0.4)

        try:
            wd_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            wd_proc.kill()

        assert wd_expired, f"Watchdog did not expire as expected; last snapshot status={wd_snap['status'] if wd_snap else None}"
        assert wd_snap["recovery_outcome"] == "watchdog_emergency_cleanup"
        assert wd_snap["selective_recovery_succeeded"] is False, "Watchdog expiry must NEVER be reported as selective recovery!"
        assert wd_snap["sandbox"]["is_active"] is False
        assert not psutil.pid_exists(wd_snap["supervisor"]["pid"])
        for w in wd_snap["workers"]:
            assert not psutil.pid_exists(w["pid"]), f"Watchdog must clean up worker PID {w['pid']}"
        print(
            f"  [PASS] Watchdog expired honestly: status={wd_snap['status']}, "
            f"outcome={wd_snap['recovery_outcome']}, selective_recovery_succeeded={wd_snap['selective_recovery_succeeded']}, "
            f"sandbox cleaned up & all enclosed processes terminated"
        )

    finally:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()


if __name__ == "__main__":
    print("=" * 80)
    print("  WINDOWS-NATIVE REAL-TIME DEADLOCK DETECTION & RECOVERY VERIFICATION SUITE")
    print("=" * 80)
    test_1_job_object_limits_and_verification()
    test_2_multi_instance_and_banker_semantics()
    test_3_fail_closed_when_backend_unreachable()
    test_4_and_5_live_end_to_end_and_watchdog()
    print("\n" + "=" * 80)
    print("  ALL WINDOWS-NATIVE REAL-TIME DEADLOCK TESTS PASSED!")
    print("=" * 80)
