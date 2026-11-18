"""
Shared Windows-Native Deadlock Scenario Runner

Reuses the existing DeadlockMonitorClient, InstrumentedMutex, Win32NamedEvent,
and Windows Job Object containment lifecycle so every real-time deadlock scenario
(Circular Deadlock, Dining Philosophers, Multi-Resource Deadlock, Deadlock Demo)
runs actual OS worker processes and actual Windows Named Mutexes without
duplicating containment, startup-gate, or WAIT_ABANDONED reconciliation code.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from sdk.deadlock_sdk import (
    ContainmentError,
    DeadlockMonitorClient,
    InstrumentedMutex,
    Win32NamedEvent,
    get_current_process_identity,
)


@dataclass(frozen=True)
class WorkerSpec:
    worker_id: str
    name: str
    recovery_priority: int            # Application recovery priority (1 = lowest/first victim)
    first_lock: str
    second_lock: str
    max_claim: Dict[str, int]


@dataclass(frozen=True)
class ScenarioDefinition:
    scenario_key: str
    workload_name: str                # Displayed in Live Workload Selector
    description: str
    resources: List[dict]             # [{"id": ..., "name": ..., "total_instances": 1}, ...]
    workers: List[WorkerSpec]
    default_max_procs: int = 4
    default_cpu_cap: int = 20
    default_job_mem_mb: int = 512
    default_proc_mem_mb: int = 256
    default_timeout: int = 120
    default_delay: float = 0.25


def _resource_state_path(state_dir: Path, run_id: str, resource_id: str) -> Path:
    return state_dir / f"osrm_dl_{run_id}_{resource_id}.json"


# ──────────────────────────────────────────────────────────────────────────────
# Worker Process Execution
# ──────────────────────────────────────────────────────────────────────────────

def _run_worker_process(args):
    """Run inside a child OS worker process that inherited the run's Windows Job Object."""
    run_id = args.run_id
    run_token = os.environ.get("OSRM_RUN_TOKEN", "")
    if not run_id or not run_token:
        print("[WORKER ERROR] Missing run_id or OSRM_RUN_TOKEN; aborting.", file=sys.stderr, flush=True)
        sys.exit(2)

    ident = get_current_process_identity()
    worker_id = args.worker_id
    worker_name = args.worker_name
    recovery_priority = int(args.recovery_priority)
    first_res = args.first_lock
    second_res = args.second_lock
    first_os_name = args.first_os_mutex
    second_os_name = args.second_os_mutex
    max_claim: Dict[str, int] = json.loads(args.max_claim_json)
    state_dir = Path(args.state_dir)
    all_ready_events: List[str] = json.loads(args.all_ready_events_json)
    my_ready_event_name = args.my_ready_event
    start_gate_name = args.start_gate_event

    client = DeadlockMonitorClient(
        server_url=args.server,
        run_id=run_id,
        run_token=run_token,
        registration_timeout_sec=5.0,
        event_timeout_sec=1.5,
    )

    print(
        f"[{worker_name}] Started child OS process PID={ident['pid']} TID={ident['tid']} "
        f"(recovery_priority={recovery_priority}, max_claim={max_claim})",
        flush=True,
    )

    # Step 1: Register with backend & verify Job Object inheritance BEFORE startup gate
    try:
        reg = client.register_worker(
            worker_id=worker_id,
            name=worker_name,
            recovery_priority=recovery_priority,
            max_claim=max_claim,
        )
        print(
            f"[{worker_name}] Registered & verified in Job Object "
            f"(OS sched='{reg.get('os_scheduling_priority')}', "
            f"gate_ready={reg.get('startup_gate_ready')})",
            flush=True,
        )
    except Exception as e:
        print(
            f"[{worker_name}] ABORTING: Worker registration/Job Object verification failed: {e}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(3)

    # Step 2: Open Windows Named Events & Named Mutexes
    start_gate = Win32NamedEvent(start_gate_name, manual_reset=True, initial_state=False)
    my_ready_evt = Win32NamedEvent(my_ready_event_name, manual_reset=True, initial_state=False)
    peer_ready_evts = [
        Win32NamedEvent(ev_name, manual_reset=True, initial_state=False)
        for ev_name in all_ready_events
        if ev_name != my_ready_event_name
    ]

    first_mutex = InstrumentedMutex(client, worker_id, first_res, first_os_name)
    second_mutex = InstrumentedMutex(client, worker_id, second_res, second_os_name)

    exit_code = 0
    holding_first = False
    holding_second = False

    try:
        # Wait at startup gate until backend has registered and verified EVERY worker
        if not start_gate.wait(timeout_ms=25_000):
            print(f"[{worker_name}] Startup gate timed out; aborting cleanly.", file=sys.stderr, flush=True)
            sys.exit(4)

        # Acquire first Windows Named Mutex
        res1 = first_mutex.acquire(timeout_ms=20_000)
        if not res1.acquired:
            print(f"[{worker_name}] Failed to acquire first mutex '{first_res}' (timeout).", file=sys.stderr, flush=True)
            exit_code = 5
            return
        holding_first = True
        print(
            f"[{worker_name}] Acquired '{first_res}' ({res1.wait_result}) on TID={res1.owner_tid}",
            flush=True,
        )

        # Write protected per-resource state while exclusively holding first_mutex
        try:
            p1 = _resource_state_path(state_dir, run_id, first_res)
            p1.write_text(
                json.dumps({
                    "resource": first_res,
                    "state": f"in_progress_by_{worker_id}",
                    "owner_worker": worker_id,
                    "owner_pid": ident["pid"],
                    "owner_tid": res1.owner_tid,
                }),
                encoding="utf-8",
            )
        except Exception:
            pass

        # Deterministic barrier: signal we hold our first mutex, wait until ALL workers hold theirs
        my_ready_evt.set()
        for pe in peer_ready_evts:
            pe.wait(timeout_ms=15_000)

        if args.delay > 0:
            time.sleep(args.delay)

        # Request second Windows Named Mutex -> creates deterministic circular wait
        wait_ms = max(5_000, int(args.timeout * 1000))
        print(
            f"[{worker_name}] Requesting '{second_res}' "
            f"(blocking in Win32 WaitForSingleObject up to {args.timeout}s)...",
            flush=True,
        )
        res2 = second_mutex.acquire(timeout_ms=wait_ms)

        if res2.acquired:
            holding_second = True
            p2 = _resource_state_path(state_dir, run_id, second_res)

            if res2.abandoned:
                # WAIT_ABANDONED (0x80): This thread NOW OWNS second_mutex because the prior owner died.
                # Inspect and repair the protected resource state while holding the mutex, then release
                # from the exact same owning thread.
                print(
                    f"[{worker_name}] *** OS WAIT_ABANDONED (0x80) RECEIVED on '{second_res}' "
                    f"(TID={res2.owner_tid}) *** — Ownership acquired after victim termination!",
                    flush=True,
                )
                prior = "unknown"
                try:
                    if p2.exists():
                        prior = json.loads(p2.read_text(encoding="utf-8")).get("state", "unknown")
                    p2.write_text(
                        json.dumps({
                            "resource": second_res,
                            "state": f"reconciled_by_{worker_id}_after_abandonment",
                            "recovered_from": prior,
                            "reconciler_pid": ident["pid"],
                            "reconciler_tid": res2.owner_tid,
                        }),
                        encoding="utf-8",
                    )
                    validation_msg = (
                        f"Protected state for {second_res} inspected (prior='{prior}') and repaired "
                        f"while holding abandoned mutex on PID={ident['pid']} TID={res2.owner_tid}"
                    )
                except Exception as exc:
                    validation_msg = f"Protected state validation completed with note: {exc}"

                print(f"[{worker_name}] {validation_msg}", flush=True)
                second_mutex.report_state_validated(details=validation_msg)
            else:
                print(
                    f"[{worker_name}] Acquired '{second_res}' normally ({res2.wait_result}) on TID={res2.owner_tid}",
                    flush=True,
                )
                try:
                    p2.write_text(
                        json.dumps({
                            "resource": second_res,
                            "state": f"completed_by_{worker_id}",
                            "owner_pid": ident["pid"],
                        }),
                        encoding="utf-8",
                    )
                except Exception:
                    pass

            # Release second_mutex from the same owning thread
            second_mutex.release()
            holding_second = False
            print(f"[{worker_name}] Released '{second_res}' from owning thread.", flush=True)
        else:
            print(
                f"[{worker_name}] Wait on '{second_res}' timed out after {args.timeout}s.",
                flush=True,
            )
            exit_code = 124

        if holding_first:
            try:
                p1 = _resource_state_path(state_dir, run_id, first_res)
                p1.write_text(
                    json.dumps({
                        "resource": first_res,
                        "state": f"completed_by_{worker_id}",
                        "owner_pid": ident["pid"],
                    }),
                    encoding="utf-8",
                )
            except Exception:
                pass
            first_mutex.release()
            holding_first = False
            print(f"[{worker_name}] Released '{first_res}' from owning thread.", flush=True)

    finally:
        if holding_second:
            try:
                second_mutex.release()
            except Exception:
                pass
        if holding_first:
            try:
                first_mutex.release()
            except Exception:
                pass

        client.send_event(
            worker_id=worker_id,
            event_type="worker_exited",
            exit_code=exit_code,
            details=f"{worker_name} completed and exiting with code {exit_code}",
        )
        first_mutex.close()
        second_mutex.close()
        start_gate.close()
        my_ready_evt.close()
        for pe in peer_ready_evts:
            pe.close()

    sys.exit(exit_code)


# ──────────────────────────────────────────────────────────────────────────────
# Supervisor Process Execution
# ──────────────────────────────────────────────────────────────────────────────

def _run_supervisor(scenario: ScenarioDefinition, script_path: Path, args):
    sup_ident = get_current_process_identity()
    num_workers = len(scenario.workers)
    max_runtime = max(10, min(600, int(args.timeout)))
    max_active_procs = max(int(args.max_procs), num_workers + 1)

    print("=" * 78, flush=True)
    print(f"  WINDOWS-NATIVE REAL-TIME DEADLOCK SCENARIO: {scenario.workload_name.upper()}", flush=True)
    print("=" * 78, flush=True)
    print(f"[SUPERVISOR] Scenario : {scenario.description}", flush=True)
    print(
        f"[SUPERVISOR] PID={sup_ident['pid']} TID={sup_ident['tid']} "
        f"CreateTime={sup_ident['create_time_100ns']}",
        flush=True,
    )
    print(
        f"[SUPERVISOR] Registering with backend {args.server} for dedicated Windows Job Object...",
        flush=True,
    )

    client = DeadlockMonitorClient(
        server_url=args.server,
        registration_timeout_sec=5.0,
        event_timeout_sec=1.5,
    )

    # STEP 1: Fail-closed sandbox registration & verification BEFORE spawning workers
    try:
        reg = client.register_run(
            workload_name=scenario.workload_name,
            expected_workers=num_workers,
            resources=scenario.resources,
            cpu_rate_percent=int(args.cpu_cap),
            job_memory_limit_mb=int(args.job_mem_mb),
            per_process_memory_limit_mb=int(args.proc_mem_mb),
            max_active_processes=max_active_procs,
            max_runtime_seconds=max_runtime,
            kill_on_close=True,
        )
    except ContainmentError as e:
        print(f"\n[CONTAINMENT ABORT] {e}", file=sys.stderr, flush=True)
        print(
            "[CONTAINMENT ABORT] Refusing to execute uncontained workload. "
            "Ensure 'python run_server.py' is running.",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)

    run_id = reg["run_id"]
    sandbox_name = reg["sandbox_name"]
    job_name = reg["job_object_name"]
    vlim = reg["verified_limits"]
    res_map = reg["resources"]

    print("[SUPERVISOR] Dedicated Windows Job Object attached & verified!", flush=True)
    print(f"  Run ID              : {run_id}", flush=True)
    print(f"  Workload            : {scenario.workload_name}", flush=True)
    print(f"  Sandbox / Job Name  : {sandbox_name} ({job_name})", flush=True)
    print(f"  Nested Job Detected : {reg.get('nested_job_detected')}", flush=True)
    print(
        f"  Verified Limits     : CPU={vlim.get('cpu_rate_percent')}% hard-cap, "
        f"JobMem={vlim.get('job_memory_limit_mb')}MB, "
        f"ProcMem={vlim.get('process_memory_limit_mb')}MB, "
        f"MaxProcs={vlim.get('active_process_limit')}, "
        f"KillOnClose={vlim.get('kill_on_job_close')}",
        flush=True,
    )
    print(f"  Watchdog Timeout    : {max_runtime}s", flush=True)
    print(f"  Dashboard URL       : {args.server}  (Deadlock & Concurrency tab)", flush=True)
    print("-" * 78, flush=True)

    state_dir = Path(tempfile.gettempdir())
    created_state_files: List[Path] = []
    for r in scenario.resources:
        p = _resource_state_path(state_dir, run_id, r["id"])
        p.write_text(
            json.dumps({"resource": r["id"], "state": "clean", "initialized_by": sup_ident["pid"]}),
            encoding="utf-8",
        )
        created_state_files.append(p)

    start_gate_name = f"Local\\OSRM_DL_{run_id}_START_GATE"
    start_gate = Win32NamedEvent(start_gate_name, manual_reset=True, initial_state=False)
    ready_event_names = [f"Local\\OSRM_DL_{run_id}_HELD_{w.worker_id}" for w in scenario.workers]
    ready_events = [Win32NamedEvent(name, manual_reset=True, initial_state=False) for name in ready_event_names]

    child_env = os.environ.copy()
    child_env["OSRM_RUN_TOKEN"] = client.run_token

    children: Dict[str, subprocess.Popen] = {}
    stop_requested = False

    def _sig_handler(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    try:
        signal.signal(signal.SIGINT, _sig_handler)
        signal.signal(signal.SIGTERM, _sig_handler)
    except Exception:
        pass

    final_exit_code = 0
    outcome_note = f"{scenario.workload_name} completed cleanly"

    try:
        # STEP 2: Spawn real OS worker processes (creationflags=0 -> inherit Job Object)
        for idx, spec in enumerate(scenario.workers):
            cmd = [
                sys.executable,
                str(script_path.resolve()),
                "--worker-mode",
                "--server", args.server,
                "--run-id", run_id,
                "--worker-id", spec.worker_id,
                "--worker-name", spec.name,
                "--recovery-priority", str(spec.recovery_priority),
                "--first-lock", spec.first_lock,
                "--second-lock", spec.second_lock,
                "--first-os-mutex", res_map[spec.first_lock]["os_mutex_name"],
                "--second-os-mutex", res_map[spec.second_lock]["os_mutex_name"],
                "--max-claim-json", json.dumps(spec.max_claim),
                "--state-dir", str(state_dir),
                "--start-gate-event", start_gate_name,
                "--my-ready-event", ready_event_names[idx],
                "--all-ready-events-json", json.dumps(ready_event_names),
                "--delay", str(args.delay),
                "--timeout", str(max_runtime),
            ]
            proc = subprocess.Popen(cmd, env=child_env, creationflags=0)
            children[spec.worker_id] = proc
            print(
                f"[SUPERVISOR] Spawned {spec.name} (OS PID={proc.pid}, "
                f"recovery_priority={spec.recovery_priority}, "
                f"holds={spec.first_lock} -> requests={spec.second_lock})",
                flush=True,
            )

        # STEP 3: Hold startup gate until backend verifies ALL workers
        print(
            f"[SUPERVISOR] Holding startup gate until backend registers & verifies all {num_workers} workers...",
            flush=True,
        )
        gate_deadline = time.time() + 20.0
        gate_ready = False
        while time.time() < gate_deadline and not stop_requested:
            for wid, p in children.items():
                if p.poll() is not None:
                    raise RuntimeError(
                        f"Worker '{wid}' (PID={p.pid}) exited prematurely during registration "
                        f"(code {p.returncode})"
                    )
            hb = client.send_heartbeat(actor_role="supervisor")
            if hb and hb.get("startup_gate_ready"):
                gate_ready = True
                break
            time.sleep(0.15)

        if not gate_ready:
            raise RuntimeError("Startup gate timed out before all workers completed backend registration")

        print(
            f"[SUPERVISOR] All {num_workers} workers verified inside Job Object! Opening startup gate...",
            flush=True,
        )
        start_gate.set()

        # STEP 4: Remain operational while workers deadlock and await user-approved recovery
        print(
            f"[SUPERVISOR] Supervisor (PID={sup_ident['pid']}) active. Open {args.server} -> "
            f"Deadlock & Concurrency tab to view live RAG/WFG, Banker's matrices, and approve recovery.",
            flush=True,
        )

        start_ts = time.time()
        last_hb = 0.0
        announced_deadlock = False

        while not stop_requested:
            now = time.time()
            if now - start_ts >= max_runtime:
                print(
                    f"\n[SUPERVISOR] Watchdog reached {max_runtime}s without recovery. "
                    f"Executing emergency cleanup (NOT selective recovery).",
                    flush=True,
                )
                final_exit_code = 124
                outcome_note = f"Supervisor watchdog expired at {max_runtime}s (emergency cleanup)"
                break

            if now - last_hb >= 1.5:
                hb = client.send_heartbeat(actor_role="supervisor")
                last_hb = now
                if hb is None:
                    print(
                        "[SUPERVISOR] [MONITORING DISCONNECTED] Backend heartbeat failed; continuing locally.",
                        file=sys.stderr,
                        flush=True,
                    )
                elif hb.get("status") == "deadlocked" and not announced_deadlock:
                    announced_deadlock = True
                    print(
                        f"\n[SUPERVISOR] *** LIVE DEADLOCK CONFIRMED ({scenario.workload_name}) *** "
                        f"(remaining watchdog: {hb.get('remaining_seconds')}s)",
                        flush=True,
                    )
                    print(
                        "[SUPERVISOR] Waiting for explicit user approval to terminate the recommended victim...",
                        flush=True,
                    )

            statuses = {wid: p.poll() for wid, p in children.items()}
            if all(rc is not None for rc in statuses.values()):
                print(f"\n[SUPERVISOR] All {num_workers} worker processes exited:", flush=True)
                for wid, rc in statuses.items():
                    print(f"  - {wid} (PID={children[wid].pid}): exit_code={rc}", flush=True)

                clean_survivors = [wid for wid, rc in statuses.items() if rc == 0]
                terminated_victims = [wid for wid, rc in statuses.items() if rc != 0]
                if clean_survivors and terminated_victims:
                    outcome_note = (
                        f"Selective recovery completed ({scenario.workload_name}): "
                        f"victim(s)={terminated_victims} terminated, "
                        f"survivor(s)={clean_survivors} recovered via WAIT_ABANDONED and exited cleanly"
                    )
                    print(f"[SUPERVISOR] {outcome_note}", flush=True)
                elif len(clean_survivors) == len(statuses):
                    outcome_note = f"All workers in {scenario.workload_name} exited cleanly"
                else:
                    outcome_note = f"Workers exited with statuses: {statuses}"
                    final_exit_code = 1
                break

            time.sleep(0.2)

    except Exception as e:
        print(f"[SUPERVISOR ERROR] {e}", file=sys.stderr, flush=True)
        outcome_note = f"Supervisor error: {e}"
        final_exit_code = 1
    finally:
        for wid, p in children.items():
            if p.poll() is None:
                try:
                    p.terminate()
                    p.wait(timeout=2.0)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass

        start_gate.close()
        for ev in ready_events:
            ev.close()
        for p in created_state_files:
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass

        client.complete_run(outcome_note=outcome_note)
        print("[SUPERVISOR] Notified backend for Job Object cleanup. Exiting cleanly.", flush=True)

    sys.exit(final_exit_code)


# ──────────────────────────────────────────────────────────────────────────────
# Public Entry Helper
# ──────────────────────────────────────────────────────────────────────────────

def build_scenario_arg_parser(scenario: ScenarioDefinition) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"Windows-Native Real-Time Deadlock Scenario — {scenario.workload_name}"
    )
    parser.add_argument("--server", type=str, default="http://127.0.0.1:8000", help="Backend server URL")
    parser.add_argument("--delay", type=float, default=scenario.default_delay, help="Hold delay before requesting second lock")
    parser.add_argument("--timeout", type=int, default=scenario.default_timeout, help="Configurable watchdog timeout in seconds")
    parser.add_argument("--cpu-cap", type=int, default=scenario.default_cpu_cap, help="Job Object CPU hard cap percent")
    parser.add_argument("--job-mem-mb", type=int, default=scenario.default_job_mem_mb, help="Aggregate Job committed memory limit MB")
    parser.add_argument("--proc-mem-mb", type=int, default=scenario.default_proc_mem_mb, help="Per-process committed memory limit MB")
    parser.add_argument("--max-procs", type=int, default=scenario.default_max_procs, help="Max active processes in Job Object")

    # Internal child-worker flags
    parser.add_argument("--worker-mode", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-id", type=str, default="", help=argparse.SUPPRESS)
    parser.add_argument("--worker-id", type=str, default="", help=argparse.SUPPRESS)
    parser.add_argument("--worker-name", type=str, default="", help=argparse.SUPPRESS)
    parser.add_argument("--recovery-priority", type=int, default=1, help=argparse.SUPPRESS)
    parser.add_argument("--first-lock", type=str, default="", help=argparse.SUPPRESS)
    parser.add_argument("--second-lock", type=str, default="", help=argparse.SUPPRESS)
    parser.add_argument("--first-os-mutex", type=str, default="", help=argparse.SUPPRESS)
    parser.add_argument("--second-os-mutex", type=str, default="", help=argparse.SUPPRESS)
    parser.add_argument("--max-claim-json", type=str, default="{}", help=argparse.SUPPRESS)
    parser.add_argument("--state-dir", type=str, default="", help=argparse.SUPPRESS)
    parser.add_argument("--start-gate-event", type=str, default="", help=argparse.SUPPRESS)
    parser.add_argument("--my-ready-event", type=str, default="", help=argparse.SUPPRESS)
    parser.add_argument("--all-ready-events-json", type=str, default="[]", help=argparse.SUPPRESS)
    return parser


def run_deadlock_scenario(
    scenario: ScenarioDefinition,
    script_path: Path,
    argv: Optional[List[str]] = None,
):
    parser = build_scenario_arg_parser(scenario)
    args, _ = parser.parse_known_args(argv)
    if args.worker_mode:
        _run_worker_process(args)
    else:
        _run_supervisor(scenario, script_path, args)
