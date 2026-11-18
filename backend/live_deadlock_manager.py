"""
Live Deadlock Manager — Real-Time Windows-Native Deadlock Detection, Avoidance & Recovery

Coordinates live OS workloads executing inside dedicated Windows NT Kernel Job Objects:
  - Per-run unguessable token authentication (X-Run-Token)
  - Fail-closed Job Object creation, limit verification, and supervisor attachment
  - Worker startup-gate synchronization (workers verify Job Object inheritance & max claims
    before any mutex acquisition begins)
  - Strictly validated real synchronization event ingestion (no CPU/status inference)
  - Separated real-time RAG/WFG (DFS + multi-instance) deadlock detection and
    Banker's Algorithm safe/unsafe analysis
  - Accurate OS WAIT_ABANDONED mutex ownership, state-validation, and same-thread release tracking
  - Explicit user-approved selective victim termination (TerminateProcess only; never TerminateJobObject)
  - Configurable bounded watchdog (default 120s) with honest emergency-cleanup reporting
"""

import hmac
import logging
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import psutil

from backend.deadlock_engine import (
    BankersAlgorithm,
    DeadlockResult,
    ProcessNode,
    Resource,
    WaitForGraph,
)
from backend.isolation_engine import (
    IsolationEngine,
    SandboxConfig,
    get_process_creation_time_100ns,
    is_critical_process,
)

logger = logging.getLogger("live_deadlock_manager")

# Configurable conservative defaults
DEFAULT_CPU_RATE_PERCENT = 20
DEFAULT_JOB_MEMORY_LIMIT_MB = 512
DEFAULT_PER_PROCESS_MEMORY_LIMIT_MB = 256
DEFAULT_MAX_ACTIVE_PROCESSES = 4
DEFAULT_MAX_RUNTIME_SECONDS = 120
DEFAULT_RECOVERY_AUTH_TTL_SECONDS = 30


class LiveAuthError(PermissionError):
    """Raised when per-run or recovery token authentication fails (HTTP 401)."""


class LiveConflictError(RuntimeError):
    """Raised when live deadlock state/snapshot/worker/sandbox changed before action (HTTP 409)."""


class LiveValidationError(ValueError):
    """Raised when a registration, matrix invariant, or sync event transition is invalid (HTTP 400)."""


@dataclass
class LiveWorker:
    worker_id: str
    name: str
    pid: int
    tid: int
    create_time_100ns: int
    recovery_priority: int                  # Application recovery priority (1 = lowest/first victim)
    os_scheduling_priority: str             # Windows OS scheduling priority class (separate concept)
    max_claim: Dict[str, int]               # Declared before startup gate opens
    allocated: Dict[str, int] = field(default_factory=dict)
    requesting: Dict[str, int] = field(default_factory=dict)
    in_job_verified: bool = False
    registered_at: float = 0.0
    state: str = "registered"               # registered | running | waiting | deadlocked | recovering | exited | terminated_victim
    last_event_seq: int = 0
    sequence_gaps: int = 0
    last_heartbeat_at: float = 0.0
    exit_code: Optional[int] = None
    monitoring_disconnected: bool = False


@dataclass
class LiveResource:
    resource_id: str
    name: str
    os_mutex_name: str
    total_instances: int = 1
    owners: Dict[str, int] = field(default_factory=dict)        # worker_id -> held count
    owner_tids: Dict[str, int] = field(default_factory=dict)    # worker_id -> owning OS thread ID
    waiters: List[str] = field(default_factory=list)            # worker_ids currently waiting
    abandoned_pending: bool = False                             # True after owner died while holding mutex
    abandoned_by_worker: Optional[str] = None
    abandoned_by_pid: Optional[int] = None
    acquired_via_abandonment: bool = False                      # True while a surviving worker holds a WAIT_ABANDONED mutex
    abandoned_holder_worker: Optional[str] = None
    abandoned_state_validated: bool = False
    abandonment_history: List[dict] = field(default_factory=list)

    @property
    def allocated_total(self) -> int:
        return sum(self.owners.values())

    @property
    def available_instances(self) -> int:
        return self.total_instances - self.allocated_total


@dataclass
class LiveDeadlockRun:
    run_id: str
    workload_name: str
    sandbox_name: str
    job_name: str
    run_token: str                                  # Secret per-run token (never exposed in GET)
    supervisor_pid: int
    supervisor_tid: int
    supervisor_create_time_100ns: int
    expected_workers: int
    max_runtime_seconds: int
    created_at: float
    watchdog_deadline: float
    verified_limits: Dict[str, Any]
    nested_job_detected: bool
    resources: Dict[str, LiveResource] = field(default_factory=dict)
    workers: Dict[str, LiveWorker] = field(default_factory=dict)
    status: str = "initializing"                    # initializing | running | deadlocked | recovering | resolved | completed | timed_out | aborted
    startup_gate_ready: bool = False
    last_heartbeat_at: float = 0.0
    events: List[dict] = field(default_factory=list)
    recovery_history: List[dict] = field(default_factory=list)
    recovery_outcome: Optional[str] = None          # selective_recovery_verified | watchdog_emergency_cleanup | aborted | completed_without_deadlock
    selective_recovery_succeeded: bool = False
    approval_challenge: Optional[dict] = None       # Current active deadlock-bound recovery authorization
    recovery_authorizations: Dict[str, dict] = field(default_factory=dict)  # token -> authorization record (single-use, TTL, never logged)
    snapshot_version: int = 0
    updated_at: float = 0.0
    last_snapshot: Optional[dict] = None
    watchdog_timer: Optional[threading.Timer] = None


class LiveDeadlockManager:
    """
    Manages active and historical Windows-native real-time deadlock runs.
    """

    def __init__(self, isolation: IsolationEngine, bankers: BankersAlgorithm):
        self._isolation = isolation
        self._bankers = bankers
        self._runs: Dict[str, LiveDeadlockRun] = {}
        self._lock = threading.RLock()
        self._update_callbacks: List[Any] = []

    def add_update_callback(self, callback):
        """Register a listener invoked whenever an authoritative run snapshot changes."""
        self._update_callbacks.append(callback)

    def _emit_update(self, snapshot: dict):
        for cb in list(self._update_callbacks):
            try:
                cb(snapshot)
            except Exception as e:
                logger.debug(f"Live update callback error: {e}")

    # ── Authentication Helpers ────────────────────────────────────────────

    def _get_run_locked(self, run_id: str) -> LiveDeadlockRun:
        run = self._runs.get(run_id)
        if not run:
            raise KeyError(f"Live deadlock run '{run_id}' not found")
        return run

    def _verify_run_token_locked(self, run: LiveDeadlockRun, provided_token: Optional[str]):
        if not provided_token or not hmac.compare_digest(run.run_token, provided_token):
            raise LiveAuthError("Invalid or missing per-run authentication token (X-Run-Token)")

    def _invalidate_recovery_authorizations_locked(self, run: LiveDeadlockRun, reason: str):
        """Invalidate all unused recovery authorizations for this run (e.g. cycle/snapshot changed or run ended)."""
        run.approval_challenge = None
        for rec in run.recovery_authorizations.values():
            if not rec.get("used") and not rec.get("invalidated"):
                rec["invalidated"] = True
                rec["invalidation_reason"] = reason

    def _token_belongs_to_other_run_locked(self, current_run_id: str, token: str) -> bool:
        for other_id, other_run in self._runs.items():
            if other_id == current_run_id:
                continue
            if hmac.compare_digest(other_run.run_token, token):
                return True
            for other_tok in other_run.recovery_authorizations:
                if hmac.compare_digest(other_tok, token):
                    return True
        return False

    def _verify_recovery_auth_locked(
        self,
        run: LiveDeadlockRun,
        run_token: Optional[str],
        recovery_token: Optional[str],
        sandbox_name: str,
        pid: int,
        create_time_100ns: int,
        snapshot_version: Optional[int] = None,
        deadlock_cycle: Optional[List[str]] = None,
    ) -> Tuple[str, Optional[dict]]:
        """
        Validate recovery authorization via either:
          1. Secret per-run workload token (X-Run-Token), OR
          2. Short-lived, single-use, deadlock-bound recovery token (X-Recovery-Token)

        Enforces exact binding to run_id, sandbox_name, victim pid, exact 64-bit
        create_time_100ns, snapshot_version, deadlock_cycle, expiry, and single-use.
        Never exposes raw token values in errors or logs.
        """
        has_run_tok = bool(run_token and run_token.strip())
        has_rec_tok = bool(recovery_token and recovery_token.strip())

        if not has_run_tok and not has_rec_tok:
            raise LiveAuthError("Recovery authorization required")

        # 1. Check X-Run-Token if provided
        if has_run_tok:
            tok = run_token.strip()
            if hmac.compare_digest(run.run_token, tok):
                return "run_token", None
            if self._token_belongs_to_other_run_locked(run.run_id, tok):
                raise PermissionError("Authorization does not match this workload")
            raise LiveAuthError("Invalid recovery authorization")

        # 2. Check deadlock-bound X-Recovery-Token
        tok = recovery_token.strip()
        matched_rec: Optional[dict] = None
        for stored_tok, rec in run.recovery_authorizations.items():
            if hmac.compare_digest(stored_tok, tok):
                matched_rec = rec
                break

        if matched_rec is None:
            if self._token_belongs_to_other_run_locked(run.run_id, tok):
                raise PermissionError("Authorization does not match this workload")
            raise LiveAuthError("Invalid recovery authorization")

        if matched_rec.get("used"):
            raise LiveAuthError("Recovery authorization has already been used")

        now = time.time()
        if now > float(matched_rec.get("expires_at", 0.0)):
            raise LiveAuthError("Recovery authorization expired")

        if matched_rec.get("invalidated"):
            raise LiveConflictError("Deadlock state changed; refresh analysis")

        if matched_rec["run_id"] != run.run_id or matched_rec["sandbox_name"] != sandbox_name:
            raise PermissionError("Authorization does not match this workload")

        if matched_rec["pid"] != pid or matched_rec["create_time_100ns"] != create_time_100ns:
            raise PermissionError("Selected process is no longer eligible")

        # Validate bound snapshot_version & deadlock_cycle against current live state
        if snapshot_version is not None and int(snapshot_version) != int(matched_rec["snapshot_version"]):
            raise LiveConflictError("Deadlock state changed; refresh analysis")

        if int(matched_rec["snapshot_version"]) != int(run.snapshot_version):
            raise LiveConflictError("Deadlock state changed; refresh analysis")

        if deadlock_cycle is not None and list(deadlock_cycle) != list(matched_rec["deadlock_cycle"]):
            raise LiveConflictError("Deadlock state changed; refresh analysis")

        return "recovery_authorization_token", matched_rec

    # ── 1. Run Registration & Fail-Closed Containment ─────────────────────

    def register_run(
        self,
        supervisor_pid: int,
        supervisor_tid: int,
        supervisor_create_time_100ns: int,
        workload_name: str = "deadlock_demo",
        expected_workers: int = 2,
        resources: Optional[List[dict]] = None,
        cpu_rate_percent: int = DEFAULT_CPU_RATE_PERCENT,
        job_memory_limit_mb: int = DEFAULT_JOB_MEMORY_LIMIT_MB,
        per_process_memory_limit_mb: int = DEFAULT_PER_PROCESS_MEMORY_LIMIT_MB,
        max_active_processes: int = DEFAULT_MAX_ACTIVE_PROCESSES,
        max_runtime_seconds: int = DEFAULT_MAX_RUNTIME_SECONDS,
        priority_class: Optional[str] = None,
        kill_on_close: bool = True,
    ) -> dict:
        """
        First application-level operation for a live workload:
          1. Validate supervisor PID & exact creation time against OS.
          2. Create dedicated uniquely named Windows Job Object with strict mandatory limits.
          3. Verify all mandatory limits via QueryInformationJobObject.
          4. Attach supervisor process via AssignProcessToJobObject & confirm IsProcessInJob.
          5. Issue unguessable per-run token and start bounded watchdog timer.

        Fail-closed: if ANY containment or verification step fails, destroy the Job Object
        and raise so the workload aborts without executing.
        """
        if is_critical_process(supervisor_pid):
            raise LiveValidationError(
                f"Supervisor PID {supervisor_pid} is a protected system or server process"
            )
        if expected_workers < 2:
            raise LiveValidationError("At least 2 worker processes are required")
        if max_runtime_seconds < 5 or max_runtime_seconds > 600:
            raise LiveValidationError("max_runtime_seconds must be between 5 and 600 seconds")

        # Validate live supervisor identity before creating sandbox
        actual_ct = get_process_creation_time_100ns(supervisor_pid)
        if actual_ct != supervisor_create_time_100ns:
            raise LiveValidationError(
                f"Supervisor identity mismatch for PID {supervisor_pid}: "
                f"expected creation time {supervisor_create_time_100ns}, actual {actual_ct}"
            )

        run_id = uuid.uuid4().hex[:12]
        sandbox_name = f"dl_run_{run_id}"
        run_token = secrets.token_urlsafe(32)

        # Configure mandatory Windows Job Object limits
        sb_config = SandboxConfig(
            name=sandbox_name,
            cpu_rate_percent=cpu_rate_percent,
            per_process_memory_limit_mb=per_process_memory_limit_mb,
            memory_limit_mb=per_process_memory_limit_mb,
            job_memory_limit_mb=job_memory_limit_mb,
            max_active_processes=max_active_processes,
            priority_class=priority_class,
            kill_on_close=kill_on_close,
            strict_limits=True,
            max_runtime_seconds=max_runtime_seconds,
        )

        # Create & verify mandatory Job Object limits (raises on any failure)
        container = self._isolation.create_sandbox(sb_config)

        # Attach supervisor process; fail-closed cleanup on any error
        try:
            assign_result = self._isolation.assign_process(
                sandbox_name,
                supervisor_pid,
                expected_create_time_100ns=supervisor_create_time_100ns,
            )
            if not assign_result.get("in_job_verified"):
                raise OSError("Supervisor Job Object membership was not verified by kernel")
        except Exception:
            self._isolation.destroy_sandbox(sandbox_name, terminate_processes=False)
            raise

        # Prepare declared resources (default Lock_A, Lock_B if omitted)
        declared_res = resources or [
            {"id": "Lock_A", "name": "Lock_A", "total_instances": 1},
            {"id": "Lock_B", "name": "Lock_B", "total_instances": 1},
        ]
        res_map: Dict[str, LiveResource] = {}
        for r in declared_res:
            rid = str(r["id"])
            total_inst = int(r.get("total_instances", 1))
            if total_inst < 1:
                self._isolation.destroy_sandbox(sandbox_name, terminate_processes=False)
                raise LiveValidationError(f"Resource '{rid}' total_instances must be >= 1")
            os_name = r.get("os_mutex_name") or f"Local\\OSRM_DL_{run_id}_{rid}"
            res_map[rid] = LiveResource(
                resource_id=rid,
                name=str(r.get("name") or rid),
                os_mutex_name=os_name,
                total_instances=total_inst,
            )

        now = time.time()
        run = LiveDeadlockRun(
            run_id=run_id,
            workload_name=workload_name,
            sandbox_name=sandbox_name,
            job_name=container.job_name,
            run_token=run_token,
            supervisor_pid=supervisor_pid,
            supervisor_tid=supervisor_tid,
            supervisor_create_time_100ns=supervisor_create_time_100ns,
            expected_workers=expected_workers,
            max_runtime_seconds=max_runtime_seconds,
            created_at=now,
            watchdog_deadline=now + max_runtime_seconds,
            verified_limits=container.verified_limits,
            nested_job_detected=assign_result["nested_job_detected"],
            resources=res_map,
            status="initializing",
            last_heartbeat_at=now,
        )

        # Start bounded server-side watchdog
        timer = threading.Timer(max_runtime_seconds, self._watchdog_expire, args=(run_id,))
        timer.daemon = True
        run.watchdog_timer = timer

        with self._lock:
            self._runs[run_id] = run
            self._record_audit_locked(
                run,
                event_type="run_registered",
                actor=f"Supervisor(PID={supervisor_pid})",
                details=(
                    f"Attached to verified Job Object '{container.job_name}' "
                    f"(CPU={cpu_rate_percent}%, JobRAM={job_memory_limit_mb}MB, "
                    f"ProcRAM={per_process_memory_limit_mb}MB, MaxProcs={max_active_processes}, "
                    f"Watchdog={max_runtime_seconds}s, NestedJob={assign_result['nested_job_detected']})"
                ),
            )
            self._recompute_analysis_locked(run)
            timer.start()

        logger.info(
            f"Live run '{run_id}' registered: supervisor PID={supervisor_pid}, "
            f"sandbox='{sandbox_name}', watchdog={max_runtime_seconds}s"
        )

        return {
            "run_id": run_id,
            "run_token": run_token,  # Returned ONLY at registration
            "attached": True,
            "sandbox_name": sandbox_name,
            "job_object_name": container.job_name,
            "verified_limits": container.verified_limits,
            "nested_job_detected": assign_result["nested_job_detected"],
            "max_runtime_seconds": max_runtime_seconds,
            "watchdog_deadline": run.watchdog_deadline,
            "resources": {
                rid: {
                    "id": r.resource_id,
                    "name": r.name,
                    "os_mutex_name": r.os_mutex_name,
                    "total_instances": r.total_instances,
                }
                for rid, r in res_map.items()
            },
        }

    # ── 2. Worker Registration & Startup Gate Verification ────────────────

    def register_worker(
        self,
        run_id: str,
        run_token: Optional[str],
        worker_id: str,
        name: str,
        pid: int,
        tid: int,
        create_time_100ns: int,
        recovery_priority: int,
        max_claim: Dict[str, int],
    ) -> dict:
        """
        Register a real child worker process before the startup gate opens.
        Verifies:
          - Per-run authentication token
          - Worker PID is a real separate process (distinct from supervisor & server)
          - Exact PID + creation time match OS GetProcessTimes
          - Child worker inherited the run's Windows Job Object (IsProcessInJob == True)
          - Declared maximum claims satisfy 0 <= max_claim[r] <= total_instances[r]
        """
        with self._lock:
            run = self._get_run_locked(run_id)
            self._verify_run_token_locked(run, run_token)

            if run.status in ("completed", "timed_out", "aborted"):
                raise LiveValidationError(f"Run '{run_id}' is no longer active (status={run.status})")
            if pid == run.supervisor_pid:
                raise LiveValidationError(
                    "Worker must be a separate OS child process, not a thread inside the supervisor process"
                )
            if is_critical_process(pid):
                raise LiveValidationError(f"PID {pid} is a protected system or server process")
            if worker_id in run.workers:
                raise LiveValidationError(f"Worker '{worker_id}' is already registered")
            if recovery_priority < 1:
                raise LiveValidationError("recovery_priority must be >= 1 (1 = lowest recovery priority)")

            # Ensure distinct OS PID from other active workers
            for existing in run.workers.values():
                if existing.pid == pid:
                    raise LiveValidationError(
                        f"Worker PID {pid} is already registered under '{existing.worker_id}'. "
                        f"Each worker must be a distinct OS process."
                    )

            # Validate declared max claims before execution
            normalized_claims: Dict[str, int] = {}
            for rid, res in run.resources.items():
                claim = int(max_claim.get(rid, 0))
                if claim < 0 or claim > res.total_instances:
                    raise LiveValidationError(
                        f"Invalid max_claim for worker '{worker_id}' on resource '{rid}': "
                        f"{claim} (must be 0..{res.total_instances})"
                    )
                normalized_claims[rid] = claim
            for rid in max_claim:
                if rid not in run.resources:
                    raise LiveValidationError(f"Unknown resource '{rid}' in max_claim")

            # Verify child inherited the run's Windows Job Object and matches creation time
            verify = self._isolation.verify_process_in_sandbox(
                run.sandbox_name, pid, expected_create_time_100ns=create_time_100ns
            )
            if not verify.get("identity_match"):
                raise LiveValidationError(
                    f"Worker PID {pid} creation time mismatch: {verify.get('reason')}"
                )
            if not verify.get("in_job"):
                raise PermissionError(
                    f"Containment violation: Worker '{worker_id}' (PID {pid}) did NOT inherit "
                    f"Job Object '{run.job_name}'. Aborting registration."
                )

            os_sched_pri = self._isolation.get_process_priority(pid) or "normal"
            now = time.time()
            worker = LiveWorker(
                worker_id=worker_id,
                name=name,
                pid=pid,
                tid=tid,
                create_time_100ns=create_time_100ns,
                recovery_priority=recovery_priority,
                os_scheduling_priority=os_sched_pri,
                max_claim=normalized_claims,
                allocated={rid: 0 for rid in run.resources},
                requesting={rid: 0 for rid in run.resources},
                in_job_verified=True,
                registered_at=now,
                state="registered",
                last_heartbeat_at=now,
            )
            run.workers[worker_id] = worker
            run.last_heartbeat_at = now

            if len(run.workers) >= run.expected_workers and all(
                w.in_job_verified for w in run.workers.values()
            ):
                run.startup_gate_ready = True
                if run.status == "initializing":
                    run.status = "running"

            self._record_audit_locked(
                run,
                event_type="worker_registered",
                actor=f"{name}(PID={pid}, TID={tid})",
                details=(
                    f"Job Object inheritance verified ({run.job_name}); "
                    f"recovery_priority={recovery_priority} (OS sched='{os_sched_pri}'); "
                    f"max_claim={normalized_claims}; startup_gate_ready={run.startup_gate_ready}"
                ),
            )
            self._recompute_analysis_locked(run)

            return {
                "registered": True,
                "run_id": run_id,
                "worker_id": worker_id,
                "pid": pid,
                "tid": tid,
                "in_job_verified": True,
                "recovery_priority": recovery_priority,
                "os_scheduling_priority": os_sched_pri,
                "registered_workers": len(run.workers),
                "expected_workers": run.expected_workers,
                "startup_gate_ready": run.startup_gate_ready,
            }

    # ── 3. Heartbeat & Synchronization Event Ingestion ────────────────────

    def record_heartbeat(
        self,
        run_id: str,
        run_token: Optional[str],
        actor_pid: int,
        actor_role: str = "supervisor",
        worker_id: Optional[str] = None,
    ) -> dict:
        with self._lock:
            run = self._get_run_locked(run_id)
            self._verify_run_token_locked(run, run_token)
            now = time.time()
            run.last_heartbeat_at = now
            if worker_id and worker_id in run.workers:
                run.workers[worker_id].last_heartbeat_at = now
            remaining = max(0.0, run.watchdog_deadline - now)
            return {
                "ok": True,
                "run_id": run_id,
                "status": run.status,
                "startup_gate_ready": run.startup_gate_ready,
                "remaining_seconds": round(remaining, 2),
            }

    def record_sync_event(
        self,
        run_id: str,
        run_token: Optional[str],
        worker_id: str,
        pid: int,
        tid: int,
        create_time_100ns: int,
        event_type: str,
        resource_id: Optional[str] = None,
        os_mutex_name: Optional[str] = None,
        seq: int = 0,
        wait_result: Optional[str] = None,
        wait_code: Optional[int] = None,
        abandoned: bool = False,
        state_validated: bool = False,
        released_after_abandoned: bool = False,
        exit_code: Optional[int] = None,
        details: str = "",
    ) -> dict:
        """
        Ingest an authenticated, sequenced synchronization event from a registered worker
        and recompute RAG, WFG, DFS, multi-instance detection, and Banker's safety.
        """
        valid_events = {
            "wait_start",
            "acquired",
            "abandoned_acquired",
            "state_validated",
            "released",
            "wait_timeout",
            "worker_exited",
        }
        if event_type not in valid_events:
            raise LiveValidationError(f"Unsupported event_type '{event_type}'")

        with self._lock:
            run = self._get_run_locked(run_id)
            self._verify_run_token_locked(run, run_token)

            worker = run.workers.get(worker_id)
            if not worker:
                raise LiveValidationError(
                    f"Event rejected: worker '{worker_id}' is not registered (preventing registration race)"
                )
            if not run.startup_gate_ready and event_type != "worker_exited":
                raise LiveValidationError(
                    f"Event '{event_type}' rejected: startup gate is not yet open "
                    f"({len(run.workers)}/{run.expected_workers} workers registered)"
                )
            if worker.pid != pid or worker.create_time_100ns != create_time_100ns:
                raise LiveAuthError(
                    f"Worker identity mismatch for '{worker_id}': expected "
                    f"(PID={worker.pid}, ct={worker.create_time_100ns}), got (PID={pid}, ct={create_time_100ns})"
                )

            # Sequence tracking (monotonic per worker)
            if seq > 0:
                if worker.last_event_seq > 0 and seq != worker.last_event_seq + 1:
                    if seq > worker.last_event_seq + 1:
                        worker.sequence_gaps += (seq - worker.last_event_seq - 1)
                worker.last_event_seq = max(worker.last_event_seq, seq)

            worker.tid = tid
            now = time.time()
            worker.last_heartbeat_at = now
            run.last_heartbeat_at = now

            if event_type != "worker_exited":
                if not resource_id or resource_id not in run.resources:
                    raise LiveValidationError(f"Unknown resource_id '{resource_id}'")
                res = run.resources[resource_id]

                if event_type == "wait_start":
                    cur_alloc = worker.allocated.get(resource_id, 0)
                    cur_req = worker.requesting.get(resource_id, 0)
                    max_c = worker.max_claim.get(resource_id, 0)
                    if cur_alloc + cur_req + 1 > max_c:
                        raise LiveValidationError(
                            f"Matrix violation: worker '{worker_id}' requesting '{resource_id}' "
                            f"exceeds declared max_claim ({max_c})"
                        )
                    worker.requesting[resource_id] = cur_req + 1
                    if worker_id not in res.waiters:
                        res.waiters.append(worker_id)
                    worker.state = "waiting"
                    self._record_audit_locked(
                        run,
                        event_type="wait_start",
                        actor=f"{worker.name}(PID={pid}, TID={tid})",
                        resource_id=resource_id,
                        details=details or f"Waiting on Windows Mutex {res.os_mutex_name}",
                        extra={"seq": seq, "pid": pid, "tid": tid},
                    )

                elif event_type == "acquired":
                    if abandoned or wait_result == "WAIT_ABANDONED":
                        raise LiveValidationError(
                            "WAIT_ABANDONED acquisition must be reported via event_type='abandoned_acquired'"
                        )
                    if res.available_instances < 1:
                        raise LiveValidationError(
                            f"Matrix invariant violation: '{resource_id}' has 0 available instances "
                            f"(current owners={res.owners})"
                        )
                    cur_alloc = worker.allocated.get(resource_id, 0)
                    max_c = worker.max_claim.get(resource_id, 0)
                    if cur_alloc + 1 > max_c:
                        raise LiveValidationError(
                            f"Matrix invariant violation: allocation for '{worker_id}' on '{resource_id}' "
                            f"would exceed declared max_claim ({max_c})"
                        )
                    if worker.requesting.get(resource_id, 0) > 0:
                        worker.requesting[resource_id] -= 1
                    if worker.requesting.get(resource_id, 0) == 0 and worker_id in res.waiters:
                        res.waiters.remove(worker_id)
                    worker.allocated[resource_id] = cur_alloc + 1
                    res.owners[worker_id] = res.owners.get(worker_id, 0) + 1
                    res.owner_tids[worker_id] = tid
                    worker.state = "running"
                    self._record_audit_locked(
                        run,
                        event_type="acquired",
                        actor=f"{worker.name}(PID={pid}, TID={tid})",
                        resource_id=resource_id,
                        details=details or f"Acquired {res.os_mutex_name} (WAIT_OBJECT_0)",
                        extra={"seq": seq, "pid": pid, "tid": tid, "wait_result": "WAIT_OBJECT_0"},
                    )

                elif event_type == "abandoned_acquired":
                    # WAIT_ABANDONED (0x80): OS granted ownership to this waiting thread
                    # because the previous owning thread exited/terminated while holding the mutex.
                    if res.available_instances < 1:
                        # If the OS delivered WAIT_ABANDONED before backend finished removing a dead owner,
                        # reconcile only dead owners (never steal from an alive worker).
                        dead_owners = [
                            wid for wid in list(res.owners.keys())
                            if wid in run.workers and not psutil.pid_exists(run.workers[wid].pid)
                        ]
                        for dead_wid in dead_owners:
                            self._reconcile_dead_worker_resources_locked(run, run.workers[dead_wid], "os_exit")

                    if res.available_instances < 1:
                        raise LiveValidationError(
                            f"Matrix invariant violation on WAIT_ABANDONED for '{resource_id}': "
                            f"live owner still recorded ({res.owners})"
                        )
                    cur_alloc = worker.allocated.get(resource_id, 0)
                    max_c = worker.max_claim.get(resource_id, 0)
                    if cur_alloc + 1 > max_c:
                        raise LiveValidationError(
                            f"Matrix invariant violation: WAIT_ABANDONED allocation exceeds max_claim ({max_c})"
                        )

                    if worker.requesting.get(resource_id, 0) > 0:
                        worker.requesting[resource_id] -= 1
                    if worker.requesting.get(resource_id, 0) == 0 and worker_id in res.waiters:
                        res.waiters.remove(worker_id)

                    worker.allocated[resource_id] = cur_alloc + 1
                    res.owners[worker_id] = res.owners.get(worker_id, 0) + 1
                    res.owner_tids[worker_id] = tid
                    res.abandoned_pending = False
                    res.acquired_via_abandonment = True
                    res.abandoned_holder_worker = worker_id
                    res.abandoned_state_validated = False
                    res.abandonment_history.append({
                        "timestamp": now,
                        "phase": "abandoned_acquired",
                        "new_owner_worker": worker_id,
                        "new_owner_pid": pid,
                        "new_owner_tid": tid,
                        "wait_code": wait_code if wait_code is not None else 0x80,
                        "note": "OS delivered WAIT_ABANDONED; thread now owns mutex and must validate protected state before releasing.",
                    })
                    worker.state = "recovering"
                    self._record_audit_locked(
                        run,
                        event_type="abandoned_acquired",
                        actor=f"{worker.name}(PID={pid}, TID={tid})",
                        resource_id=resource_id,
                        details=(
                            details
                            or f"OS returned WAIT_ABANDONED (0x80) on {res.os_mutex_name}: "
                               f"ownership acquired after previous owner terminated; validating protected state"
                        ),
                        extra={
                            "seq": seq,
                            "pid": pid,
                            "tid": tid,
                            "wait_result": "WAIT_ABANDONED",
                            "wait_code": wait_code if wait_code is not None else 0x80,
                            "abandoned": True,
                        },
                    )

                elif event_type == "state_validated":
                    if worker.allocated.get(resource_id, 0) < 1:
                        raise LiveValidationError(
                            f"Worker '{worker_id}' reported state_validated without holding '{resource_id}'"
                        )
                    if res.owner_tids.get(worker_id) not in (None, tid):
                        raise LiveValidationError(
                            f"Thread mismatch on state_validated: owner TID={res.owner_tids.get(worker_id)}, got TID={tid}"
                        )
                    res.abandoned_state_validated = True
                    res.abandonment_history.append({
                        "timestamp": now,
                        "phase": "state_validated",
                        "owner_worker": worker_id,
                        "owner_pid": pid,
                        "owner_tid": tid,
                        "details": details or "Protected shared state validated while holding abandoned mutex",
                    })
                    self._record_audit_locked(
                        run,
                        event_type="state_validated",
                        actor=f"{worker.name}(PID={pid}, TID={tid})",
                        resource_id=resource_id,
                        details=details or f"Validated protected state while holding abandoned mutex {resource_id}",
                        extra={"seq": seq, "pid": pid, "tid": tid, "state_validated": True},
                    )

                elif event_type == "released":
                    cur_alloc = worker.allocated.get(resource_id, 0)
                    if cur_alloc < 1 or res.owners.get(worker_id, 0) < 1:
                        raise LiveValidationError(
                            f"Matrix violation: worker '{worker_id}' attempted to release unheld resource '{resource_id}'"
                        )
                    # Mutex release must come from the owning thread
                    if res.total_instances == 1 and res.owner_tids.get(worker_id) not in (None, tid):
                        raise LiveValidationError(
                            f"Mutex ownership violation: ReleaseMutex for '{resource_id}' called from "
                            f"TID={tid}, but owning thread is TID={res.owner_tids.get(worker_id)}"
                        )
                    worker.allocated[resource_id] = cur_alloc - 1
                    res.owners[worker_id] -= 1
                    if res.owners[worker_id] <= 0:
                        del res.owners[worker_id]
                        res.owner_tids.pop(worker_id, None)

                    was_abandoned_hold = (
                        res.acquired_via_abandonment and res.abandoned_holder_worker == worker_id
                    )
                    if was_abandoned_hold:
                        res.acquired_via_abandonment = False
                        res.abandoned_holder_worker = None
                        res.abandonment_history.append({
                            "timestamp": now,
                            "phase": "released_after_abandonment_recovery",
                            "owner_worker": worker_id,
                            "owner_pid": pid,
                            "owner_tid": tid,
                            "state_validated": res.abandoned_state_validated,
                        })

                    self._record_audit_locked(
                        run,
                        event_type="released",
                        actor=f"{worker.name}(PID={pid}, TID={tid})",
                        resource_id=resource_id,
                        details=(
                            details
                            or (
                                f"Released {res.os_mutex_name} from owning thread TID={tid} "
                                f"after WAIT_ABANDONED state validation"
                                if (was_abandoned_hold or released_after_abandoned)
                                else f"Released {res.os_mutex_name} (ReleaseMutex)"
                            )
                        ),
                        extra={
                            "seq": seq,
                            "pid": pid,
                            "tid": tid,
                            "released_after_abandoned": bool(was_abandoned_hold or released_after_abandoned),
                        },
                    )

                elif event_type == "wait_timeout":
                    if worker.requesting.get(resource_id, 0) > 0:
                        worker.requesting[resource_id] -= 1
                    if worker.requesting.get(resource_id, 0) == 0 and worker_id in res.waiters:
                        res.waiters.remove(worker_id)
                    worker.state = "running"
                    self._record_audit_locked(
                        run,
                        event_type="wait_timeout",
                        actor=f"{worker.name}(PID={pid}, TID={tid})",
                        resource_id=resource_id,
                        details=details or f"Wait timed out on {res.os_mutex_name}",
                        extra={"seq": seq, "pid": pid, "tid": tid},
                    )

            else:
                # worker_exited
                worker.exit_code = exit_code if exit_code is not None else 0
                worker.state = "exited"
                # Reconcile any resources still held at exit as abandoned (not normal release)
                self._reconcile_dead_worker_resources_locked(run, worker, reason="worker_exit")
                self._record_audit_locked(
                    run,
                    event_type="worker_exited",
                    actor=f"{worker.name}(PID={pid}, TID={tid})",
                    details=details or f"Worker exited with code {worker.exit_code}",
                    extra={"seq": seq, "pid": pid, "tid": tid, "exit_code": worker.exit_code},
                )

            snapshot = self._recompute_analysis_locked(run)
            return {
                "ok": True,
                "run_id": run_id,
                "status": run.status,
                "has_deadlock": snapshot["deadlock_detection"]["has_deadlock"],
                "banker_is_safe": snapshot["banker_analysis"]["is_safe"],
            }

    def _reconcile_dead_worker_resources_locked(
        self, run: LiveDeadlockRun, worker: LiveWorker, reason: str
    ) -> List[str]:
        """
        Reconcile resources owned or requested by a terminated/exited worker.
        Any mutex held by a terminated worker is marked OS-ABANDONED (never
        falsely logged as a normal ReleaseMutex).
        """
        abandoned_list: List[str] = []
        now = time.time()

        # Clear pending requests
        for rid, req_cnt in list(worker.requesting.items()):
            if req_cnt > 0:
                worker.requesting[rid] = 0
                res = run.resources.get(rid)
                if res and worker.worker_id in res.waiters:
                    res.waiters.remove(worker.worker_id)

        # Mark held mutexes as OS-abandoned
        for rid, alloc_cnt in list(worker.allocated.items()):
            if alloc_cnt > 0:
                worker.allocated[rid] = 0
                res = run.resources.get(rid)
                if res:
                    res.owners.pop(worker.worker_id, None)
                    res.owner_tids.pop(worker.worker_id, None)
                    res.abandoned_pending = True
                    res.abandoned_by_worker = worker.worker_id
                    res.abandoned_by_pid = worker.pid
                    res.abandonment_history.append({
                        "timestamp": now,
                        "phase": "mutex_abandoned_by_os",
                        "prior_owner_worker": worker.worker_id,
                        "prior_owner_pid": worker.pid,
                        "reason": reason,
                        "note": "Owning process terminated without ReleaseMutex; Windows NT Kernel marks mutex WAIT_ABANDONED.",
                    })
                    abandoned_list.append(rid)
                    self._record_audit_locked(
                        run,
                        event_type="mutex_abandoned",
                        actor=f"WindowsKernel(victim={worker.name}, PID={worker.pid})",
                        resource_id=rid,
                        details=(
                            f"Mutex {res.os_mutex_name} ABANDONED by OS when {worker.name} "
                            f"(PID={worker.pid}) terminated (NOT a normal ReleaseMutex)"
                        ),
                        extra={"abandoned": True, "prior_owner_pid": worker.pid},
                    )
        return abandoned_list

    # ── 4. Graph Construction, Detection & Banker's Safety Analysis ───────

    def _recompute_analysis_locked(
        self,
        run: LiveDeadlockRun,
        bump_version: bool = True,
        emit: bool = True,
    ) -> dict:
        """
        Build ONE single authoritative versioned snapshot containing:
          - RAG & WFG graphs (empty when run is aborted/timed_out)
          - DFS + Multi-Instance Deadlock Detection
          - Separated Banker's Algorithm Safety Analysis (with finish_flags)
          - Victim Recommendation & Recovery Status

        Both RAG/WFG and Banker's Algorithm render from the exact same `snapshot_version`.
        Never equates an unsafe Banker's state with an active deadlock.
        """
        now = time.time()
        if bump_version or run.snapshot_version == 0:
            run.snapshot_version += 1
            run.updated_at = now

        is_terminal_aborted = run.status in ("aborted", "timed_out")
        is_active_run = run.status not in ("completed", "timed_out", "aborted")

        wfg = WaitForGraph()
        ordered_res_ids = list(run.resources.keys())

        active_workers: List[LiveWorker] = []
        if not is_terminal_aborted:
            for rid in ordered_res_ids:
                res = run.resources[rid]
                wfg.add_resource(
                    Resource(
                        id=res.resource_id,
                        name=res.name,
                        total_instances=res.total_instances,
                        available_instances=res.available_instances,
                    )
                )

            active_workers = [
                w for w in run.workers.values()
                if w.state not in ("terminated_victim", "exited", "aborted", "timed_out")
            ]

            for w in active_workers:
                runtime_s = max(0.0, now - w.registered_at)
                mem_mb = 0.0
                try:
                    mem_mb = round(psutil.Process(w.pid).memory_info().rss / (1024 * 1024), 2)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

                wfg.add_process(
                    ProcessNode(
                        id=w.worker_id,
                        name=f"{w.name} (PID {w.pid})",
                        priority=w.recovery_priority,
                        recovery_priority=w.recovery_priority,
                        os_scheduling_priority=w.os_scheduling_priority,
                        pid=w.pid,
                        tid=w.tid,
                        create_time_100ns=w.create_time_100ns,
                        role="worker",
                        eligible_for_victim=True,
                        state=w.state,
                        runtime_seconds=runtime_s,
                        memory_mb=mem_mb,
                        allocated=dict(w.allocated),
                        requesting=dict(w.requesting),
                        max_claim=dict(w.max_claim),
                    )
                )

        dl_result: DeadlockResult = wfg.detect_deadlock()
        graph_data = wfg.get_graph_data()
        graph_data["snapshot_version"] = run.snapshot_version

        # Update worker deadlocked state markers & approval challenge ONLY for active runs
        if is_active_run and dl_result.has_deadlock:
            for wid in dl_result.involved_processes:
                if wid in run.workers and run.workers[wid].state not in ("terminated_victim", "exited", "aborted", "timed_out"):
                    run.workers[wid].state = "deadlocked"
            if run.status not in ("completed", "timed_out", "aborted", "recovering"):
                run.status = "deadlocked"

            # Issue or refresh deadlock-bound approval challenge for recommended victim
            victim_wid = dl_result.recommended_victim
            if victim_wid and victim_wid in run.workers:
                vw = run.workers[victim_wid]
                existing = run.approval_challenge
                if (
                    not existing
                    or existing.get("worker_id") != victim_wid
                    or existing.get("pid") != vw.pid
                    or existing.get("create_time_100ns") != vw.create_time_100ns
                ):
                    run.approval_challenge = {
                        "token": secrets.token_urlsafe(24),
                        "sandbox_name": run.sandbox_name,
                        "worker_id": victim_wid,
                        "pid": vw.pid,
                        "create_time_100ns": vw.create_time_100ns,
                        "issued_at": now,
                    }
        else:
            run.approval_challenge = None
            if run.status == "deadlocked":
                run.status = "resolved"

        # Separate Banker's Algorithm Safety Check (declared max_claim + live allocated)
        banker_payload = self._compute_banker_locked(
            run, active_workers, ordered_res_ids, dl_result.has_deadlock, is_terminal_aborted
        )

        # Recommended victim structured details (disabled when run is terminal/aborted)
        victim_details = None
        if is_active_run and dl_result.recommended_victim and dl_result.recommended_victim in run.workers:
            vw = run.workers[dl_result.recommended_victim]
            victim_details = {
                "worker_id": vw.worker_id,
                "name": vw.name,
                "pid": vw.pid,
                "tid": vw.tid,
                "create_time_100ns": vw.create_time_100ns,
                "recovery_priority": vw.recovery_priority,
                "os_scheduling_priority": vw.os_scheduling_priority,
                "allocated": dict(vw.allocated),
                "requesting": dict(vw.requesting),
                "reason": dl_result.victim_reason,
            }

        # Supervisor liveness check
        sup_alive = False
        try:
            sup_ct = get_process_creation_time_100ns(run.supervisor_pid)
            sup_alive = (sup_ct == run.supervisor_create_time_100ns)
        except OSError:
            sup_alive = False

        remaining_s = 0.0 if not is_active_run else max(0.0, run.watchdog_deadline - now)
        sandbox_container = self._isolation.get_sandbox(run.sandbox_name)
        sandbox_active = bool(sandbox_container and sandbox_container.is_active)

        deadlock_payload = {
            "snapshot_version": run.snapshot_version,
            "has_deadlock": dl_result.has_deadlock,
            "detection_method": dl_result.detection_method,
            "is_multi_instance_graph": dl_result.is_multi_instance_graph,
            "cycles": dl_result.cycles,
            "involved_processes": sorted(list(dl_result.involved_processes)),
            "involved_resources": sorted(list(dl_result.involved_resources)),
            "multi_instance_deadlocked": dl_result.multi_instance_deadlocked,
            "recommended_victim": dl_result.recommended_victim if is_active_run else None,
            "victim_reason": dl_result.victim_reason if is_active_run else "",
            "recommended_victim_details": victim_details,
        }

        rag_payload = {
            "snapshot_version": run.snapshot_version,
            "nodes": graph_data["nodes"],
            "edges": graph_data["rag_edges"],
        }
        wfg_payload = {
            "snapshot_version": run.snapshot_version,
            "nodes": [n for n in graph_data["nodes"] if n["type"] == "process"],
            "edges": graph_data["wfg_edges"],
        }

        recovery_status_payload = {
            "status": run.status,
            "recovery_outcome": run.recovery_outcome,
            "selective_recovery_succeeded": run.selective_recovery_succeeded,
            "requires_user_approval": bool(is_active_run and dl_result.has_deadlock and victim_details is not None),
            "history": list(run.recovery_history),
        }

        snapshot = {
            "run_id": run.run_id,
            "workload_name": run.workload_name,
            "snapshot_version": run.snapshot_version,
            "updated_at": run.updated_at,
            "is_active_run": is_active_run,
            "status": run.status,
            "recovery_outcome": run.recovery_outcome,
            "selective_recovery_succeeded": run.selective_recovery_succeeded,
            "startup_gate_ready": run.startup_gate_ready,
            "expected_workers": run.expected_workers,
            "registered_workers": len(run.workers),
            "created_at": run.created_at,
            "max_runtime_seconds": run.max_runtime_seconds,
            "watchdog_deadline": run.watchdog_deadline,
            "remaining_seconds": round(remaining_s, 2),
            "last_heartbeat_at": run.last_heartbeat_at,
            "sandbox": {
                "sandbox_name": run.sandbox_name,
                "job_name": run.job_name,
                "is_active": sandbox_active,
                "nested_job_detected": run.nested_job_detected,
                "verified_limits": run.verified_limits,
                "kernel_member_pids": self._isolation.get_job_process_ids(run.sandbox_name) if sandbox_active else [],
                "accounting": self._isolation.get_job_accounting(run.sandbox_name) if sandbox_active else None,
            },
            "supervisor": {
                "pid": run.supervisor_pid,
                "tid": run.supervisor_tid,
                "create_time_100ns": run.supervisor_create_time_100ns,
                "alive": sup_alive,
                "role": "supervisor",
                "eligible_for_victim": False,
            },
            "workers": [
                {
                    "worker_id": w.worker_id,
                    "name": w.name,
                    "pid": w.pid,
                    "tid": w.tid,
                    "create_time_100ns": w.create_time_100ns,
                    "recovery_priority": w.recovery_priority,
                    "os_scheduling_priority": w.os_scheduling_priority,
                    "in_job_verified": w.in_job_verified,
                    "state": w.state,
                    "max_claim": dict(w.max_claim),
                    "allocated": dict(w.allocated),
                    "requesting": dict(w.requesting),
                    "last_event_seq": w.last_event_seq,
                    "sequence_gaps": w.sequence_gaps,
                    "exit_code": w.exit_code,
                    "alive": psutil.pid_exists(w.pid),
                }
                for w in run.workers.values()
            ],
            "resources": [
                {
                    "id": r.resource_id,
                    "name": r.name,
                    "os_mutex_name": r.os_mutex_name,
                    "total_instances": r.total_instances,
                    "available_instances": r.available_instances,
                    "owners": dict(r.owners),
                    "owner_tids": dict(r.owner_tids),
                    "waiters": list(r.waiters),
                    "abandoned_pending": r.abandoned_pending,
                    "abandoned_by_worker": r.abandoned_by_worker,
                    "abandoned_by_pid": r.abandoned_by_pid,
                    "acquired_via_abandonment": r.acquired_via_abandonment,
                    "abandoned_holder_worker": r.abandoned_holder_worker,
                    "abandoned_state_validated": r.abandoned_state_validated,
                    "abandonment_history": list(r.abandonment_history),
                }
                for r in run.resources.values()
            ],
            "graph": graph_data,
            "rag": rag_payload,
            "wfg": wfg_payload,
            "deadlock_detection": deadlock_payload,
            "deadlock_result": deadlock_payload,
            "banker_analysis": banker_payload,
            "banker_result": banker_payload,
            "victim_recommendation": victim_details,
            "recovery_status": recovery_status_payload,
            "events": list(run.events[-100:]),
            "recovery_history": list(run.recovery_history),
            "requires_user_approval": recovery_status_payload["requires_user_approval"],
        }
        run.last_snapshot = snapshot
        if bump_version and emit:
            self._emit_update(snapshot)
        return snapshot

    def _compute_banker_locked(
        self,
        run: LiveDeadlockRun,
        active_workers: List[LiveWorker],
        ordered_res_ids: List[str],
        actual_deadlock_exists: bool,
        is_terminal_aborted: bool = False,
    ) -> dict:
        """
        Compute Banker's Algorithm safety analysis strictly from declared max_claim
        and actual live mutex allocations. Explicitly distinguishes UNSAFE state
        from active DEADLOCK, includes finish_flags, and reports NOT AVAILABLE
        when required live data or max claims are missing.
        """
        if is_terminal_aborted:
            return {
                "snapshot_version": run.snapshot_version,
                "data_available": False,
                "status_label": "NOT AVAILABLE",
                "is_safe": None,
                "safe_sequence": [],
                "finish_flags": [],
                "available": [],
                "allocation_matrix": [],
                "max_matrix": [],
                "need_matrix": [],
                "process_names": [],
                "resource_names": ordered_res_ids,
                "actual_deadlock_exists": False,
                "semantic_note": f"Run is {run.status.upper()}; live Banker's state is cleared.",
            }

        available = [run.resources[r].available_instances for r in ordered_res_ids]
        if not ordered_res_ids:
            return {
                "snapshot_version": run.snapshot_version,
                "data_available": False,
                "status_label": "NOT AVAILABLE",
                "is_safe": None,
                "safe_sequence": [],
                "finish_flags": [],
                "available": [],
                "allocation_matrix": [],
                "max_matrix": [],
                "need_matrix": [],
                "process_names": [],
                "resource_names": [],
                "actual_deadlock_exists": False,
                "semantic_note": "NOT AVAILABLE — no resources declared for this run.",
            }

        if not active_workers:
            return {
                "snapshot_version": run.snapshot_version,
                "data_available": True,
                "status_label": "SAFE",
                "is_safe": True,
                "safe_sequence": [],
                "finish_flags": [],
                "available": available,
                "allocation_matrix": [],
                "max_matrix": [],
                "need_matrix": [],
                "process_names": [],
                "resource_names": ordered_res_ids,
                "actual_deadlock_exists": actual_deadlock_exists,
                "semantic_note": (
                    "SAFE — all remaining workers have released resources and completed."
                    if run.status in ("resolved", "completed")
                    else "Waiting for worker processes to register declared maximum claims..."
                ),
            }

        # Verify declared maximum claims exist for every active worker (never fabricate)
        for w in active_workers:
            if not w.max_claim or any(r not in w.max_claim for r in ordered_res_ids):
                return {
                    "snapshot_version": run.snapshot_version,
                    "data_available": False,
                    "status_label": "NOT AVAILABLE",
                    "is_safe": None,
                    "safe_sequence": [],
                    "finish_flags": [],
                    "available": available,
                    "allocation_matrix": [],
                    "max_matrix": [],
                    "need_matrix": [],
                    "process_names": [f"{wk.name} (PID {wk.pid})" for wk in active_workers],
                    "resource_names": ordered_res_ids,
                    "actual_deadlock_exists": actual_deadlock_exists,
                    "semantic_note": "NOT AVAILABLE — declared maximum claims are incomplete.",
                }

        proc_names = [f"{w.name} (PID {w.pid})" for w in active_workers]
        max_matrix = [
            [w.max_claim.get(r, 0) for r in ordered_res_ids]
            for w in active_workers
        ]
        alloc_matrix = [
            [w.allocated.get(r, 0) for r in ordered_res_ids]
            for w in active_workers
        ]

        b_res = self._bankers.check_safety(
            processes=proc_names,
            resources=ordered_res_ids,
            available=available,
            max_matrix=max_matrix,
            allocation_matrix=alloc_matrix,
        )

        if b_res.is_safe:
            note = (
                f"SAFE state: a valid completion order exists ({' -> '.join(b_res.safe_sequence)}). "
                f"Banker's avoidance guarantees deadlock can be avoided."
            )
        elif actual_deadlock_exists:
            note = (
                "UNSAFE state AND active deadlock confirmed by WFG/RAG detection: "
                "no safe sequence exists and circular wait is currently blocking processes."
            )
        else:
            note = (
                "UNSAFE state (Deadlock Risk — NOT yet an active deadlock): "
                "no guaranteed safe completion sequence exists for declared max claims, "
                "even though WFG has not detected an active circular wait cycle."
            )

        return {
            "snapshot_version": run.snapshot_version,
            "data_available": True,
            "status_label": "SAFE" if b_res.is_safe else "UNSAFE",
            "is_safe": b_res.is_safe,
            "safe_sequence": b_res.safe_sequence,
            "finish_flags": b_res.finish_flags,
            "available": b_res.available,
            "allocation_matrix": b_res.allocation_matrix,
            "max_matrix": b_res.max_matrix,
            "need_matrix": b_res.need_matrix,
            "process_names": b_res.process_names,
            "resource_names": b_res.resource_names,
            "actual_deadlock_exists": actual_deadlock_exists,
            "semantic_note": note,
        }

    # ── 5. Explicit User-Approved Selective Victim Recovery ───────────────

    def get_recovery_challenge(self, run_id: str) -> dict:
        """
        Return the active deadlock-bound recovery challenge metadata so the local
        dashboard operator can explicitly approve terminating the exact deadlocked victim.
        Never exposes the secret per-run workload `run_token`.
        """
        with self._lock:
            run = self._get_run_locked(run_id)
            self._recompute_analysis_locked(run, bump_version=False, emit=False)
            if not run.approval_challenge:
                raise LiveValidationError(f"Run '{run_id}' does not currently have an active deadlock requiring recovery")
            return dict(run.approval_challenge)

    def approve_and_recover(
        self,
        run_id: str,
        sandbox_name: str,
        pid: int,
        create_time_100ns: int,
        approved: bool,
        run_token: Optional[str] = None,
        recovery_token: Optional[str] = None,
        operator_note: str = "",
    ) -> dict:
        """
        Perform selective recovery on explicit user approval:
          1. Validate explicit approval flag & authentication token.
          2. Validate sandbox ID matches active run.
          3. Validate PID & creation time match a registered eligible worker (not supervisor).
          4. Validate worker is still involved in an active deadlock.
          5. Terminate ONLY the approved worker via TerminateProcess (never TerminateJobObject).
          6. Confirm OS process exit.
          7. Reconcile victim's held Windows Mutexes as OS-abandoned (never normal ReleaseMutex).
          8. Verify supervisor & Job Object remain active.
          9. Briefly wait for surviving worker(s) to receive WAIT_ABANDONED, validate state, and release.
         10. Rebuild RAG & WFG and recalculate Banker's safety.
         11. Report honest verification result.
        """
        if not approved:
            raise LiveValidationError("Explicit user approval (approved=true) is required for selective recovery")

        with self._lock:
            run = self._get_run_locked(run_id)
            auth_mode = self._verify_recovery_auth_locked(
                run,
                run_token=run_token,
                recovery_token=recovery_token,
                sandbox_name=sandbox_name,
                pid=pid,
                create_time_100ns=create_time_100ns,
            )

            if run.status in ("completed", "timed_out", "aborted"):
                raise LiveValidationError(f"Cannot recover run '{run_id}' in terminal status '{run.status}'")
            if run.sandbox_name != sandbox_name:
                raise LiveValidationError(
                    f"Sandbox ID mismatch: run '{run_id}' owns '{run.sandbox_name}', got '{sandbox_name}'"
                )
            if pid == run.supervisor_pid:
                raise PermissionError("Refusing to terminate the supervisor process during selective recovery")

            target_worker: Optional[LiveWorker] = None
            for w in run.workers.values():
                if w.pid == pid:
                    target_worker = w
                    break

            if target_worker is None:
                raise LiveValidationError(f"PID {pid} is not a registered worker in run '{run_id}'")
            if target_worker.create_time_100ns != create_time_100ns:
                raise PermissionError(
                    f"PID {pid} creation time mismatch: expected {target_worker.create_time_100ns}, "
                    f"got {create_time_100ns}"
                )

            # Re-verify worker is currently involved in a detected deadlock
            pre_snapshot = self._recompute_analysis_locked(run)
            pre_dl = pre_snapshot["deadlock_detection"]
            if not pre_dl["has_deadlock"]:
                raise LiveValidationError("No active deadlock exists to recover from")
            if target_worker.worker_id not in pre_dl["involved_processes"]:
                raise LiveValidationError(
                    f"Worker '{target_worker.worker_id}' (PID {pid}) is not currently involved in the detected deadlock"
                )

            run.status = "recovering"
            run.approval_challenge = None  # Consume single-use challenge

            # Terminate ONLY this approved worker via TerminateProcess (Job Object remains active!)
            term_result = self._isolation.terminate_sandbox_process(
                sandbox_name=sandbox_name,
                pid=pid,
                expected_create_time_100ns=create_time_100ns,
            )

            target_worker.state = "terminated_victim"
            target_worker.exit_code = term_result["exit_code"]

            # Reconcile held mutexes as OS-ABANDONED (never normal release)
            abandoned_resources = self._reconcile_dead_worker_resources_locked(
                run, target_worker, reason="approved_selective_termination"
            )

            self._record_audit_locked(
                run,
                event_type="victim_terminated",
                actor=f"Operator({auth_mode})",
                details=(
                    f"Selectively terminated {target_worker.name} (PID={pid}, "
                    f"recovery_priority={target_worker.recovery_priority}); "
                    f"exited={term_result['exited_confirmed']}; "
                    f"abandoned_mutexes={abandoned_resources}; Job Object kept ACTIVE. "
                    f"{operator_note}".strip()
                ),
                extra={
                    "victim_worker_id": target_worker.worker_id,
                    "victim_pid": pid,
                    "abandoned_resources": abandoned_resources,
                    "sandbox_still_active": term_result["sandbox_still_active"],
                },
            )

            # Push intermediate recovering state immediately
            self._recompute_analysis_locked(run, bump_version=True, emit=True)

        # Release lock briefly so surviving worker(s) can process OS WAIT_ABANDONED,
        # cascade through released locks, validate state, release mutexes, and report events.
        deadline = time.time() + 2.8
        while time.time() < deadline:
            with self._lock:
                run = self._runs.get(run_id)
                if not run:
                    break
                still_pending = any(
                    r.abandoned_pending or r.acquired_via_abandonment
                    for r in run.resources.values()
                )
                all_released = all(r.allocated_total == 0 for r in run.resources.values())
                if not still_pending and all_released:
                    break
            time.sleep(0.1)

        with self._lock:
            run = self._get_run_locked(run_id)
            post_snapshot = self._recompute_analysis_locked(run, bump_version=False, emit=False)
            post_dl = post_snapshot["deadlock_detection"]
            post_banker = post_snapshot["banker_analysis"]
            resolved = not post_dl["has_deadlock"]

            if resolved and run.status not in ("completed", "timed_out", "aborted"):
                run.status = "resolved"
                run.recovery_outcome = "selective_recovery_verified"
                run.selective_recovery_succeeded = True

            recovery_record = {
                "timestamp": time.time(),
                "auth_mode": auth_mode,
                "victim_worker_id": target_worker.worker_id,
                "victim_name": target_worker.name,
                "victim_pid": pid,
                "victim_create_time_100ns": create_time_100ns,
                "victim_recovery_priority": target_worker.recovery_priority,
                "termination": term_result,
                "abandoned_resources": abandoned_resources,
                "deadlock_resolved": resolved,
                "post_recovery_has_deadlock": post_dl["has_deadlock"],
                "post_recovery_banker_safe": post_banker["is_safe"],
                "post_recovery_safe_sequence": post_banker["safe_sequence"],
                "sandbox_still_active": post_snapshot["sandbox"]["is_active"],
                "supervisor_alive": post_snapshot["supervisor"]["alive"],
            }
            run.recovery_history.append(recovery_record)

            self._record_audit_locked(
                run,
                event_type="recovery_verified",
                actor="DeadlockEngine",
                details=(
                    f"Post-recovery verification: deadlock_resolved={resolved}, "
                    f"banker_safe={post_banker['is_safe']}, "
                    f"supervisor_alive={post_snapshot['supervisor']['alive']}, "
                    f"sandbox_active={post_snapshot['sandbox']['is_active']}"
                ),
            )
            post_snapshot = self._recompute_analysis_locked(run, bump_version=True, emit=True)

            return {
                "status": "recovered" if resolved else "deadlock_persists",
                "deadlock_resolved": resolved,
                "selective_recovery_succeeded": resolved,
                "recovery_record": recovery_record,
                "run": post_snapshot,
            }

    # ── 6. Completion, Watchdog & Teardown ────────────────────────────────

    def _clear_terminal_live_state_locked(self, run: LiveDeadlockRun, terminal_state: str):
        """
        Clear active allocations, requests, and deadlocked worker markers when a run
        is aborted or timed out so stale deadlocked state never lingers in the live UI.
        Historical audit events and recovery history remain preserved.
        """
        for w in run.workers.values():
            if w.state not in ("terminated_victim", "exited"):
                w.state = terminal_state
            for rid in list(w.allocated.keys()):
                w.allocated[rid] = 0
            for rid in list(w.requesting.keys()):
                w.requesting[rid] = 0
        for r in run.resources.values():
            r.owners.clear()
            r.owner_tids.clear()
            r.waiters.clear()
            r.abandoned_pending = False
            r.acquired_via_abandonment = False
            r.abandoned_holder_worker = None

    def complete_run(
        self,
        run_id: str,
        run_token: Optional[str],
        outcome_note: str = "Supervisor completed cleanly",
    ) -> dict:
        """
        Graceful completion invoked by the supervisor after workload finishes.
        Cancels watchdog and destroys/cleans up the dedicated Job Object.
        """
        with self._lock:
            run = self._get_run_locked(run_id)
            self._verify_run_token_locked(run, run_token)

            if run.watchdog_timer:
                run.watchdog_timer.cancel()
                run.watchdog_timer = None

            if run.status not in ("timed_out", "aborted"):
                run.status = "completed"
                if run.recovery_outcome is None:
                    run.recovery_outcome = "completed_without_deadlock"

            sandbox_name = run.sandbox_name
            sup_pid = run.supervisor_pid
            self._record_audit_locked(
                run,
                event_type="run_completed",
                actor=f"Supervisor(PID={sup_pid})",
                details=f"{outcome_note}; scheduling Windows Job Object '{run.job_name}' cleanup",
            )
            snapshot = self._recompute_analysis_locked(run, bump_version=True, emit=True)

        # Clean up Job Object asynchronously once supervisor exits (or after short grace period)
        def _deferred_cleanup():
            deadline = time.time() + 3.0
            while time.time() < deadline and psutil.pid_exists(sup_pid):
                time.sleep(0.1)
            try:
                self._isolation.destroy_sandbox(sandbox_name, terminate_processes=True)
            except Exception as e:
                logger.warning(f"Deferred sandbox cleanup error for '{sandbox_name}': {e}")
            with self._lock:
                r = self._runs.get(run_id)
                if r:
                    self._record_audit_locked(
                        r,
                        event_type="sandbox_cleaned_up",
                        actor="IsolationEngine",
                        details=f"Windows Job Object '{r.job_name}' closed and released",
                    )
                    self._recompute_analysis_locked(r, bump_version=True, emit=True)

        t = threading.Thread(target=_deferred_cleanup, daemon=True)
        t.start()

        return {
            "status": "completed",
            "run_id": run_id,
            "sandbox_cleanup": "scheduled",
            "run": snapshot,
        }

    def abort_run(self, run_id: str, reason: str = "Explicit operator abort") -> dict:
        """Explicitly tear down a live run and its entire Job Object, clearing live deadlock markers."""
        with self._lock:
            run = self._get_run_locked(run_id)
            if run.watchdog_timer:
                run.watchdog_timer.cancel()
                run.watchdog_timer = None
            run.status = "aborted"
            run.recovery_outcome = "aborted"
            run.selective_recovery_succeeded = False
            run.approval_challenge = None
            self._isolation.destroy_sandbox(run.sandbox_name, terminate_processes=True)
            self._clear_terminal_live_state_locked(run, "aborted")
            self._record_audit_locked(
                run,
                event_type="run_aborted",
                actor="Operator",
                details=f"{reason}; whole-job TerminateJobObject executed for '{run.job_name}'",
            )
            return self._recompute_analysis_locked(run, bump_version=True, emit=True)

    def _watchdog_expire(self, run_id: str):
        """
        Bounded watchdog expiration handler.
        Performs emergency whole-job cleanup and honestly records timeout — NEVER
        reported as successful selective recovery.
        """
        with self._lock:
            run = self._runs.get(run_id)
            if not run or run.status in ("completed", "aborted", "timed_out"):
                return
            logger.warning(
                f"Watchdog expired for live run '{run_id}' after {run.max_runtime_seconds}s; "
                f"executing emergency Job Object cleanup"
            )
            run.status = "timed_out"
            run.recovery_outcome = "watchdog_emergency_cleanup"
            run.selective_recovery_succeeded = False
            run.approval_challenge = None
            try:
                self._isolation.destroy_sandbox(run.sandbox_name, terminate_processes=True)
            except Exception as e:
                logger.warning(f"Watchdog cleanup error for '{run.sandbox_name}': {e}")
            self._clear_terminal_live_state_locked(run, "timed_out")
            self._record_audit_locked(
                run,
                event_type="watchdog_expired",
                actor="Watchdog",
                details=(
                    f"Maximum runtime ({run.max_runtime_seconds}s) expired. Emergency whole-job "
                    f"cleanup executed via TerminateJobObject. NOT a selective recovery."
                ),
            )
            self._recompute_analysis_locked(run, bump_version=True, emit=True)

    def cleanup_all(self):
        """Cancel all watchdogs and tear down all active live runs on server shutdown."""
        with self._lock:
            for run in list(self._runs.values()):
                if run.watchdog_timer:
                    run.watchdog_timer.cancel()
                    run.watchdog_timer = None
                if run.status not in ("completed", "timed_out", "aborted"):
                    run.status = "aborted"
                    run.recovery_outcome = "server_shutdown_cleanup"
                    run.selective_recovery_succeeded = False
                    try:
                        self._isolation.destroy_sandbox(run.sandbox_name, terminate_processes=True)
                    except Exception:
                        pass
                    self._clear_terminal_live_state_locked(run, "aborted")

    # ── 7. Read-Only Queries (Never Expose Secret run_token) ──────────────

    def list_runs(self) -> List[dict]:
        with self._lock:
            items = []
            for run in sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True):
                if run.status in ("completed", "timed_out", "aborted") and run.last_snapshot is not None:
                    snap = run.last_snapshot
                else:
                    snap = self._recompute_analysis_locked(run, bump_version=False, emit=False)
                items.append({
                    "run_id": snap["run_id"],
                    "workload_name": snap["workload_name"],
                    "snapshot_version": snap["snapshot_version"],
                    "updated_at": snap["updated_at"],
                    "is_active_run": snap["is_active_run"],
                    "status": snap["status"],
                    "recovery_outcome": snap["recovery_outcome"],
                    "selective_recovery_succeeded": snap["selective_recovery_succeeded"],
                    "sandbox_name": snap["sandbox"]["sandbox_name"],
                    "sandbox_active": snap["sandbox"]["is_active"],
                    "supervisor_pid": snap["supervisor"]["pid"],
                    "supervisor_alive": snap["supervisor"]["alive"],
                    "registered_workers": snap["registered_workers"],
                    "expected_workers": snap["expected_workers"],
                    "startup_gate_ready": snap["startup_gate_ready"],
                    "has_deadlock": snap["deadlock_detection"]["has_deadlock"],
                    "banker_is_safe": snap["banker_analysis"]["is_safe"],
                    "recommended_victim": snap["deadlock_detection"]["recommended_victim"],
                    "remaining_seconds": snap["remaining_seconds"],
                    "max_runtime_seconds": snap["max_runtime_seconds"],
                    "created_at": snap["created_at"],
                })
            return items

    def get_run(self, run_id: str) -> dict:
        with self._lock:
            run = self._get_run_locked(run_id)
            return self._recompute_analysis_locked(run, bump_version=False, emit=False)

    def get_telemetry_summary(self) -> dict:
        """
        Authoritative summary + newest active run snapshot pushed over /ws/telemetry
        for automatic zero-button frontend synchronization.
        """
        with self._lock:
            runs = self.list_runs()
            active_runs = [r for r in runs if r["is_active_run"]]
            historical_runs = [r for r in runs if not r["is_active_run"]]
            deadlocked = [r for r in active_runs if r["has_deadlock"]]

            newest_active_id = active_runs[0]["run_id"] if active_runs else None
            active_snapshot = None
            if newest_active_id and newest_active_id in self._runs:
                active_snapshot = self._recompute_analysis_locked(
                    self._runs[newest_active_id], bump_version=False, emit=False
                )

            return {
                "total_runs": len(runs),
                "active_count": len(active_runs),
                "deadlocked_count": len(deadlocked),
                "newest_active_run_id": newest_active_id,
                "active_runs": active_runs[:15],
                "historical_runs": historical_runs[:15],
                "runs": runs[:20],
                "active_snapshot": active_snapshot,
            }

    # ── Audit Logging ─────────────────────────────────────────────────────

    def _record_audit_locked(
        self,
        run: LiveDeadlockRun,
        event_type: str,
        actor: str,
        details: str,
        resource_id: Optional[str] = None,
        extra: Optional[dict] = None,
    ):
        entry = {
            "timestamp": time.time(),
            "event_type": event_type,
            "actor": actor,
            "resource_id": resource_id,
            "details": details,
        }
        if extra:
            entry.update(extra)
        run.events.append(entry)
        if len(run.events) > 300:
            run.events = run.events[-300:]
