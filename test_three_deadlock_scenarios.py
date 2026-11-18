#!/usr/bin/env python3
"""
Integration Verification Suite for the Three Real-Time Windows-Native Deadlock Scenarios
and Live UI Data Integrity:

  Test 1 — Circular Deadlock       (python workloads\\circular_deadlock.py)
  Test 2 — Dining Philosophers     (python workloads\\dining_philosophers.py)
  Test 3 — Multi-Resource Deadlock (python workloads\\multi_resource_deadlock.py)
  Test 4 — UI Data Integrity, Authoritative Snapshot Versions & Aborted-Run Hygiene
"""

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil

PROJECT_ROOT = Path(__file__).resolve().parent


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


def _wait_for_new_deadlocked_run(base_url: str, seen_run_ids: set, proc: subprocess.Popen, timeout_s: float = 25.0):
    deadline = time.time() + timeout_s
    run_id = None
    while time.time() < deadline:
        code, data = _http_json("GET", f"{base_url}/api/deadlock/live/runs")
        assert code == 200
        new_runs = [r for r in data.get("runs", []) if r["run_id"] not in seen_run_ids]
        if new_runs:
            run_id = new_runs[0]["run_id"]
            c2, snap = _http_json("GET", f"{base_url}/api/deadlock/live/runs/{run_id}")
            assert c2 == 200
            if snap["deadlock_detection"]["has_deadlock"] and snap["registered_workers"] == snap["expected_workers"]:
                # Ensure every worker has entered waiting/deadlocked state
                all_req = all(sum(w["requesting"].values()) >= 1 for w in snap["workers"])
                if all_req:
                    return run_id, snap
        assert proc.poll() is None, (
            f"Scenario process exited prematurely (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout.read()}\nSTDERR:\n{proc.stderr.read()}"
        )
        time.sleep(0.2)
    raise AssertionError(f"Scenario did not reach full deadlocked state within {timeout_s}s (run_id={run_id})")


def _assert_snapshot_version_consistency(snap: dict):
    v = snap["snapshot_version"]
    assert isinstance(v, int) and v >= 1
    assert snap["graph"]["snapshot_version"] == v
    assert snap["rag"]["snapshot_version"] == v
    assert snap["wfg"]["snapshot_version"] == v
    assert snap["deadlock_result"]["snapshot_version"] == v
    assert snap["banker_result"]["snapshot_version"] == v


def _approve_and_verify_recovery(base_url: str, run_id: str, expected_victim: str, proc: subprocess.Popen):
    code_ch, ch = _http_json("GET", f"{base_url}/api/deadlock/live/runs/{run_id}/recovery-challenge")
    assert code_ch == 200, f"Recovery challenge failed: {code_ch} {ch}"
    assert ch["worker_id"] == expected_victim, f"Expected victim '{expected_victim}', got '{ch['worker_id']}'"

    code_rec, rec = _http_json(
        "POST",
        f"{base_url}/api/deadlock/live/runs/{run_id}/recover",
        headers={"X-Recovery-Token": ch["token"]},
        payload={
            "sandbox_name": ch["sandbox_name"],
            "pid": ch["pid"],
            "create_time_100ns": ch["create_time_100ns"],
            "approved": True,
            "operator_note": f"Automated approval for {expected_victim}",
        },
        timeout=12.0,
    )
    assert code_rec == 200, f"Recovery failed: {code_rec} {rec}"
    assert rec["deadlock_resolved"] is True
    assert rec["selective_recovery_succeeded"] is True
    rr = rec["recovery_record"]
    assert rr["termination"]["exited_confirmed"] is True
    assert rr["termination"]["sandbox_still_active"] is True
    assert rr["post_recovery_has_deadlock"] is False
    assert rr["post_recovery_banker_safe"] is True
    _assert_snapshot_version_consistency(rec["run"])

    stdout, stderr = proc.communicate(timeout=20)
    assert proc.returncode == 0, f"Supervisor expected exit code 0, got {proc.returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
    assert "WAIT_ABANDONED" in stdout, f"Expected WAIT_ABANDONED recovery log in stdout:\n{stdout}"
    assert not psutil.pid_exists(ch["pid"]), "Terminated victim PID still exists!"

    # Verify final events & deferred Job Object cleanup
    time.sleep(0.8)
    _, fin = _http_json("GET", f"{base_url}/api/deadlock/live/runs/{run_id}")
    _assert_snapshot_version_consistency(fin)
    evts = [e["event_type"] for e in fin["events"]]
    assert "mutex_abandoned" in evts
    assert "abandoned_acquired" in evts
    assert "state_validated" in evts
    assert "released" in evts
    assert fin["deadlock_result"]["has_deadlock"] is False
    assert fin["banker_result"]["is_safe"] is True

    cleaned = False
    for _ in range(25):
        _, sbs = _http_json("GET", f"{base_url}/api/sandboxes")
        if not any(s["name"] == ch["sandbox_name"] for s in sbs):
            cleaned = True
            break
        time.sleep(0.2)
    assert cleaned, f"Job Object '{ch['sandbox_name']}' was not cleaned up!"
    return fin


def run_all_tests():
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    print("=" * 80)
    print(f"  THREE REAL-TIME DEADLOCK SCENARIOS & UI INTEGRITY SUITE ({base_url})")
    print("=" * 80)

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

    seen_runs = set()

    try:
        # Wait for backend readiness
        ready = False
        for _ in range(50):
            try:
                c, _ = _http_json("GET", f"{base_url}/api/system/mode", timeout=1.0)
                if c == 200:
                    ready = True
                    break
            except Exception:
                time.sleep(0.2)
        assert ready, "Backend server failed to start"

        # ── PRE-CHECK: NO DUMMY WFG GRAPH AT STARTUP ──────────────────────
        code_wfg, init_wfg = _http_json("GET", f"{base_url}/api/deadlock/wfg")
        assert code_wfg == 200
        assert init_wfg["graph"]["nodes"] == [], f"Expected empty initial WFG, got {init_wfg['graph']['nodes']}"
        assert init_wfg["result"]["has_deadlock"] is False
        print("\n[PRE-CHECK PASS] Backend starts with empty WFG (0 nodes; no dummy circular_wait)")

        # ── TEST 1: CIRCULAR DEADLOCK ─────────────────────────────────────
        print("\n[TEST 1] Circular Deadlock (python workloads\\circular_deadlock.py)")
        p1 = subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "workloads" / "circular_deadlock.py"), "--server", base_url, "--timeout", "60", "--delay", "0.2"],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        run1_id, snap1 = _wait_for_new_deadlocked_run(base_url, seen_runs, p1)
        seen_runs.add(run1_id)
        _assert_snapshot_version_consistency(snap1)

        assert snap1["workload_name"] == "Circular Deadlock"
        assert len(snap1["workers"]) == 2
        assert len(snap1["resources"]) == 2
        assert snap1["sandbox"]["verified_limits"]["verified"] is True
        pids1 = {snap1["supervisor"]["pid"]} | {w["pid"] for w in snap1["workers"]}
        assert len(pids1) == 3
        assert pids1.issubset(set(snap1["sandbox"]["kernel_member_pids"]))

        rag1 = snap1["rag"]
        wfg1 = snap1["wfg"]
        assert len([e for e in rag1["edges"] if e["type"] == "allocation"]) == 2
        assert len([e for e in rag1["edges"] if e["type"] == "request"]) == 2
        assert len(wfg1["edges"]) == 2
        assert set(snap1["deadlock_result"]["involved_processes"]) == {"worker_a", "worker_b"}
        assert snap1["deadlock_result"]["recommended_victim"] == "worker_a"

        bk1 = snap1["banker_result"]
        assert bk1["data_available"] is True
        assert bk1["is_safe"] is False
        assert bk1["available"] == [0, 0]
        assert bk1["finish_flags"] == [False, False]
        assert bk1["safe_sequence"] == []

        print(f"  [PASS] Detected Circular Deadlock (run={run1_id}, v{snap1['snapshot_version']}): cycle={snap1['deadlock_result']['cycles'][0]}, Banker=UNSAFE, victim=worker_a")
        _approve_and_verify_recovery(base_url, run1_id, "worker_a", p1)
        print("  [PASS] Circular Deadlock selective recovery + WAIT_ABANDONED + SAFE post-state + Job Object cleanup verified!")

        # ── TEST 2: DINING PHILOSOPHERS ───────────────────────────────────
        print("\n[TEST 2] Dining Philosophers (python workloads\\dining_philosophers.py)")
        p2 = subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "workloads" / "dining_philosophers.py"), "--server", base_url, "--timeout", "60", "--delay", "0.2"],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        run2_id, snap2 = _wait_for_new_deadlocked_run(base_url, seen_runs, p2)
        seen_runs.add(run2_id)
        _assert_snapshot_version_consistency(snap2)

        assert snap2["workload_name"] == "Dining Philosophers"
        assert len(snap2["workers"]) == 5
        assert len(snap2["resources"]) == 5
        assert snap2["sandbox"]["verified_limits"]["verified"] is True
        pids2 = {snap2["supervisor"]["pid"]} | {w["pid"] for w in snap2["workers"]}
        assert len(pids2) == 6, f"Expected 6 distinct OS PIDs (1 supervisor + 5 philosophers), got {len(pids2)}"
        assert pids2.issubset(set(snap2["sandbox"]["kernel_member_pids"]))

        rag2 = snap2["rag"]
        wfg2 = snap2["wfg"]
        assert len([n for n in rag2["nodes"] if n["type"] == "process"]) == 5
        assert len([n for n in rag2["nodes"] if n["type"] == "resource"]) == 5
        assert len([e for e in rag2["edges"] if e["type"] == "allocation"]) == 5
        assert len([e for e in rag2["edges"] if e["type"] == "request"]) == 5
        assert len(wfg2["edges"]) == 5
        expected_phils = {"philosopher_1", "philosopher_2", "philosopher_3", "philosopher_4", "philosopher_5"}
        assert set(snap2["deadlock_result"]["involved_processes"]) == expected_phils
        assert len(snap2["deadlock_result"]["cycles"][0]) == 6  # P1->P2->P3->P4->P5->P1
        assert snap2["deadlock_result"]["recommended_victim"] == "philosopher_1"

        bk2 = snap2["banker_result"]
        assert bk2["data_available"] is True
        assert bk2["is_safe"] is False
        assert bk2["available"] == [0, 0, 0, 0, 0]
        assert bk2["finish_flags"] == [False, False, False, False, False]

        print(f"  [PASS] Detected Dining Philosophers (run={run2_id}, v{snap2['snapshot_version']}): 5 processes, 5 chopsticks, 5-node cycle={snap2['deadlock_result']['cycles'][0]}, Banker=UNSAFE, victim=philosopher_1")
        fin2 = _approve_and_verify_recovery(base_url, run2_id, "philosopher_1", p2)
        survivor_exits = {w["worker_id"]: w["exit_code"] for w in fin2["workers"] if w["worker_id"] != "philosopher_1"}
        assert all(rc == 0 for rc in survivor_exits.values()), f"Expected all 4 surviving philosophers to cascade & exit 0, got {survivor_exits}"
        print(f"  [PASS] Dining Philosophers cascading recovery verified (all 4 survivors exited 0: {survivor_exits}) & Job Object cleaned up!")

        # ── TEST 3: MULTI-RESOURCE DEADLOCK ───────────────────────────────
        print("\n[TEST 3] Multi-Resource Deadlock (python workloads\\multi_resource_deadlock.py)")
        p3 = subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "workloads" / "multi_resource_deadlock.py"), "--server", base_url, "--timeout", "60", "--delay", "0.2"],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        run3_id, snap3 = _wait_for_new_deadlocked_run(base_url, seen_runs, p3)
        seen_runs.add(run3_id)
        _assert_snapshot_version_consistency(snap3)

        assert snap3["workload_name"] == "Multi-Resource Deadlock"
        assert len(snap3["workers"]) == 4
        assert len(snap3["resources"]) == 4
        assert snap3["sandbox"]["verified_limits"]["verified"] is True
        pids3 = {snap3["supervisor"]["pid"]} | {w["pid"] for w in snap3["workers"]}
        assert len(pids3) == 5, f"Expected 5 distinct OS PIDs (1 supervisor + 4 workers), got {len(pids3)}"
        assert pids3.issubset(set(snap3["sandbox"]["kernel_member_pids"]))

        rag3 = snap3["rag"]
        wfg3 = snap3["wfg"]
        assert len([e for e in rag3["edges"] if e["type"] == "allocation"]) == 4
        assert len([e for e in rag3["edges"] if e["type"] == "request"]) == 4
        wfg3_pairs = {(e["from"], e["to"]) for e in wfg3["edges"]}
        assert ("worker_a", "worker_b") in wfg3_pairs
        assert ("worker_b", "worker_c") in wfg3_pairs
        assert ("worker_c", "worker_a") in wfg3_pairs
        assert ("worker_d", "worker_b") in wfg3_pairs, f"Worker D -> Worker B wait edge missing from WFG: {wfg3_pairs}"

        # Critical assertion: Worker D is blocked on B, NOT part of the 3-process cycle, and NOT chosen as victim despite RecPri=1!
        assert set(snap3["deadlock_result"]["involved_processes"]) == {"worker_a", "worker_b", "worker_c"}, (
            f"Worker D must NOT be classified as a cycle member! Got {snap3['deadlock_result']['involved_processes']}"
        )
        assert snap3["deadlock_result"]["recommended_victim"] == "worker_a", (
            f"Expected worker_a (RecPri=2, lowest IN CYCLE), NOT worker_d (RecPri=1, outside cycle); got {snap3['deadlock_result']['recommended_victim']}"
        )

        bk3 = snap3["banker_result"]
        assert bk3["data_available"] is True
        assert bk3["is_safe"] is False
        assert bk3["available"] == [0, 0, 0, 0]

        print(f"  [PASS] Detected Multi-Resource Deadlock (run={run3_id}, v{snap3['snapshot_version']}): 3-proc cycle={snap3['deadlock_result']['cycles'][0]}, Worker D visible in WFG (D->B) & excluded from cycle, victim=worker_a (not D), Banker=UNSAFE")
        fin3 = _approve_and_verify_recovery(base_url, run3_id, "worker_a", p3)
        survivor_exits3 = {w["worker_id"]: w["exit_code"] for w in fin3["workers"] if w["worker_id"] != "worker_a"}
        assert all(rc == 0 for rc in survivor_exits3.values()), f"Expected all 3 surviving workers (B, C, D) to complete cleanly, got {survivor_exits3}"
        print(f"  [PASS] Multi-Resource cascading recovery verified (B, C, D all exited 0: {survivor_exits3}) & Job Object cleaned up!")

        # ── TEST 4: UI DATA INTEGRITY & ABORTED-RUN HYGIENE ───────────────
        print("\n[TEST 4] UI Data Integrity, Snapshot Versioning & Aborted-Run Hygiene")

        # 4a. Static UI integrity assertions
        html = (PROJECT_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        js = (PROJECT_ROOT / "frontend" / "js" / "deadlock_graph.js").read_text(encoding="utf-8")
        assert "btnToggleLiveSync" not in html and "Sync Graphs: OFF" not in html, "Manual Sync Graphs button must be removed!"
        assert "NO ACTIVE DEADLOCK WORKLOAD" in html and "NO ACTIVE DEADLOCK WORKLOAD" in js
        assert "LIVE BANKER'S ALGORITHM" in html
        assert "loadWfgScenario('circular_wait'" not in js, "Dummy circular_wait auto-load must be removed!"
        assert "loadBankerScenario('safe'" not in js, "Static Banker auto-load must be removed!"
        assert "AUTO-SYNC LIVE" in html
        print("  [PASS] Frontend HTML/JS verified: no dummy WFG, no static Banker auto-load, no manual Sync toggle, clean empty state present")

        # 4b. Aborted-run hygiene: abort an active deadlocked run and verify stale nodes/cycles/recovery actions are cleared
        p4 = subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "workloads" / "circular_deadlock.py"), "--server", base_url, "--timeout", "60", "--delay", "0.2"],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        run4_id, snap4 = _wait_for_new_deadlocked_run(base_url, seen_runs, p4)
        seen_runs.add(run4_id)
        pre_ver = snap4["snapshot_version"]

        code_ab, aborted_snap = _http_json("DELETE", f"{base_url}/api/deadlock/live/runs/{run4_id}")
        assert code_ab == 200
        try:
            p4.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p4.kill()

        assert aborted_snap["status"] == "aborted"
        assert aborted_snap["is_active_run"] is False
        assert aborted_snap["snapshot_version"] > pre_ver
        _assert_snapshot_version_consistency(aborted_snap)
        assert aborted_snap["graph"]["nodes"] == [], "Aborted run must clear active graph nodes!"
        assert aborted_snap["deadlock_result"]["has_deadlock"] is False
        assert aborted_snap["banker_result"]["data_available"] is False
        assert aborted_snap["requires_user_approval"] is False
        assert len(aborted_snap["events"]) > 0, "Historical audit events must remain preserved after abort"

        # Verify aborted run is not presented as active in /api/deadlock/live/runs
        _, runs_after = _http_json("GET", f"{base_url}/api/deadlock/live/runs")
        ab_entry = [r for r in runs_after["runs"] if r["run_id"] == run4_id][0]
        assert ab_entry["is_active_run"] is False and ab_entry["has_deadlock"] is False
        print(f"  [PASS] Aborted run ({run4_id}) cleanly cleared live graph/Banker/recovery actions (v{pre_ver} -> v{aborted_snap['snapshot_version']}) while preserving {len(aborted_snap['events'])} historical audit events!")

    finally:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()

    print("\n" + "=" * 80)
    print("  ALL 4 INTEGRATION TESTS (3 LIVE SCENARIOS + UI INTEGRITY) PASSED!")
    print("=" * 80)


if __name__ == "__main__":
    run_all_tests()
