"""
Main Server — FastAPI REST + WebSocket Backend

Serves as the central controller connecting:
  - System Monitor (telemetry)
  - Isolation Engine (Job Objects)
  - Policy Engine (intelligent decisions)
  - Deadlock Engine (WFG & Banker's)
  - Simulator Engine (MLFQ & Page Replacement)

Also serves the static frontend files.

OS Concepts Demonstrated:
  - Concurrent request handling (async I/O + thread pool)
  - WebSocket real-time streaming (observer pattern)
  - Process control API surface
"""

import os
import sys
import time
import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Union
from urllib.parse import urlparse

from fastapi import FastAPI, Header, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.system_monitor import SystemMonitor
from backend.isolation_engine import IsolationEngine, SandboxConfig, is_critical_process
from backend.policy_engine import PolicyEngine, PolicyConfig
from backend.deadlock_engine import WaitForGraph, BankersAlgorithm, DeadlockScenarioBuilder, ProcessNode, Resource
from backend.live_deadlock_manager import (
    DEFAULT_CPU_RATE_PERCENT,
    DEFAULT_JOB_MEMORY_LIMIT_MB,
    DEFAULT_MAX_ACTIVE_PROCESSES,
    DEFAULT_MAX_RUNTIME_SECONDS,
    DEFAULT_PER_PROCESS_MEMORY_LIMIT_MB,
    DEFAULT_RECOVERY_AUTH_TTL_SECONDS,
    LiveAuthError,
    LiveConflictError,
    LiveDeadlockManager,
    LiveValidationError,
)
from backend.simulator_engine import MLFQScheduler, MLFQProcess, MLFQConfig, PageReplacementSimulator

# ──────────────────────────────────────────────────────────────────────────────
# Logging Setup
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("main")

# ──────────────────────────────────────────────────────────────────────────────
# Initialize Core Engines
# ──────────────────────────────────────────────────────────────────────────────

monitor = SystemMonitor(interval=2.0)
isolation = IsolationEngine()
policy = PolicyEngine(monitor, isolation)
bankers = BankersAlgorithm()
live_deadlock = LiveDeadlockManager(isolation, bankers)
page_sim = PageReplacementSimulator()

# Practice WFG starts empty (never pre-populated with dummy circular-wait nodes)
wfg = WaitForGraph()
main_event_loop: Optional[asyncio.AbstractEventLoop] = None

# Execution Mode: "normal" (default) or "testing"
current_execution_mode = "normal"

# ──────────────────────────────────────────────────────────────────────────────
# FastAPI App
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Smart Resource Manager & Application Isolation System",
    description="OS Course Project — Intelligent Resource Management Layer",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend static files
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

# Track spawned workload PIDs for cleanup
spawned_workloads: dict = {}

def kill_process_tree(pid: int, timeout: float = 2.0):
    """Safely terminate a process and all its children to prevent orphan processes."""
    import psutil
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        try:
            parent.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

        _, alive = psutil.wait_procs(children + [parent], timeout=timeout)
        for p in alive:
            try:
                p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

# ──────────────────────────────────────────────────────────────────────────────
# Pydantic Models
# ──────────────────────────────────────────────────────────────────────────────

class ModeRequest(BaseModel):
    mode: str  # "normal" or "testing"

class SandboxCreateRequest(BaseModel):
    name: str
    cpu_rate_percent: Optional[int] = None
    memory_limit_mb: Optional[int] = None
    per_process_memory_limit_mb: Optional[int] = None
    job_memory_limit_mb: Optional[int] = None
    max_active_processes: Optional[int] = None
    priority_class: Optional[str] = None
    kill_on_close: bool = True
    max_working_set_mb: Optional[int] = None

class SandboxUpdateRequest(BaseModel):
    cpu_rate_percent: Optional[int] = None
    memory_limit_mb: Optional[int] = None
    per_process_memory_limit_mb: Optional[int] = None
    job_memory_limit_mb: Optional[int] = None
    max_active_processes: Optional[int] = None
    priority_class: Optional[str] = None
    max_working_set_mb: Optional[int] = None

class AssignProcessRequest(BaseModel):
    pid: int

class PriorityRequest(BaseModel):
    priority: str

class AffinityRequest(BaseModel):
    core_mask: int

class PolicyConfigUpdate(BaseModel):
    cpu_hog_threshold_percent: Optional[float] = None
    cpu_hog_duration_seconds: Optional[float] = None
    cpu_hog_cap_percent: Optional[int] = None
    memory_pressure_threshold: Optional[float] = None
    memory_critical_threshold: Optional[float] = None
    starvation_cpu_min_percent: Optional[float] = None
    starvation_duration_seconds: Optional[float] = None
    evaluation_interval: Optional[float] = None

class MLFQRequest(BaseModel):
    processes: List[dict]  # [{id, name, total_burst, arrival_time}]
    num_queues: int = 3
    time_quanta: List[int] = [4, 8, 16]
    boost_interval: int = 30

class PageReplacementRequest(BaseModel):
    reference_string: List[int]
    num_frames: int = 3
    algorithm: Optional[str] = None  # FIFO, LRU, Clock, or None for all

class BankerRequest(BaseModel):
    processes: List[str]
    resources: List[str]
    available: List[int]
    max_matrix: List[List[int]]
    allocation_matrix: List[List[int]]

class BankerSimRequest(BaseModel):
    processes: List[str]
    resources: List[str]
    available: List[int]
    max_matrix: List[List[int]]
    allocation_matrix: List[List[int]]
    requesting_process: int
    request: List[int]

class WFGProcessRequest(BaseModel):
    id: str
    name: str
    priority: int = 0
    runtime_seconds: float = 0.0
    memory_mb: float = 0.0
    allocated: dict = {}
    requesting: dict = {}

class WFGResourceRequest(BaseModel):
    id: str
    name: str
    total_instances: int
    available_instances: Optional[int] = None

class WorkloadRequest(BaseModel):
    workload_type: str  # cpu_hog, memory_hog, deadlock_demo, sandboxed_app
    args: List[str] = []
    sandbox_name: Optional[str] = None

class LiveRunRegisterRequest(BaseModel):
    supervisor_pid: int
    supervisor_tid: int
    supervisor_create_time_100ns: int
    workload_name: str = "deadlock_demo"
    expected_workers: int = 2
    resources: Optional[List[dict]] = None
    cpu_rate_percent: int = DEFAULT_CPU_RATE_PERCENT
    job_memory_limit_mb: int = DEFAULT_JOB_MEMORY_LIMIT_MB
    per_process_memory_limit_mb: int = DEFAULT_PER_PROCESS_MEMORY_LIMIT_MB
    max_active_processes: int = DEFAULT_MAX_ACTIVE_PROCESSES
    max_runtime_seconds: int = DEFAULT_MAX_RUNTIME_SECONDS
    priority_class: Optional[str] = None
    kill_on_close: bool = True

class LiveWorkerRegisterRequest(BaseModel):
    worker_id: str
    name: str
    pid: int
    tid: int
    create_time_100ns: int
    recovery_priority: int
    max_claim: Dict[str, int]

class LiveSyncEventRequest(BaseModel):
    worker_id: str
    pid: int
    tid: int
    create_time_100ns: int
    event_type: str
    resource_id: Optional[str] = None
    os_mutex_name: Optional[str] = None
    seq: int = 0
    wait_result: Optional[str] = None
    wait_code: Optional[int] = None
    abandoned: bool = False
    state_validated: bool = False
    released_after_abandoned: bool = False
    exit_code: Optional[int] = None
    details: str = ""

class LiveHeartbeatRequest(BaseModel):
    actor_pid: int
    actor_role: str = "supervisor"
    worker_id: Optional[str] = None

class LiveRecoveryAuthRequest(BaseModel):
    victim_pid: Optional[int] = None
    snapshot_version: Optional[int] = None
    ttl_seconds: Optional[float] = None

class LiveRecoverRequest(BaseModel):
    sandbox_name: str
    pid: int
    create_time_100ns: Union[int, str]
    approved: bool = False
    snapshot_version: Optional[int] = None
    deadlock_cycle: Optional[List[str]] = None
    operator_note: str = ""

class LiveCompleteRequest(BaseModel):
    outcome_note: str = "Supervisor completed cleanly"

# ──────────────────────────────────────────────────────────────────────────────
# Startup / Shutdown
# ──────────────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    global main_event_loop
    main_event_loop = asyncio.get_running_loop()
    monitor.start()
    policy.start()
    logger.info(f"All engines started [Execution Mode: {current_execution_mode.upper()}]")

@app.on_event("shutdown")
async def shutdown():
    logger.info("Initiating graceful shutdown...")
    # 0. Clean up live deadlock runs & watchdogs
    try:
        live_deadlock.cleanup_all()
    except Exception as e:
        logger.warning(f"Error cleaning up live deadlock runs: {e}")

    # 1. Kill spawned workloads using tree-kill
    for wid, proc in list(spawned_workloads.items()):
        try:
            kill_process_tree(proc.pid)
        except Exception as e:
            logger.warning(f"Error stopping workload {wid}: {e}")
    spawned_workloads.clear()

    # 2. Clean up Job Object sandboxes
    try:
        isolation.cleanup_all_sandboxes()
    except Exception as e:
        logger.warning(f"Error cleaning up sandboxes: {e}")

    # 3. Stop policy engine and monitor threads cleanly
    policy.stop()
    monitor.stop()
    logger.info("All engines, sandboxes, and workloads stopped cleanly")

# ──────────────────────────────────────────────────────────────────────────────
# Frontend Route
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/")
async def serve_index():
    index_path = FRONTEND_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return {"message": "Frontend not found. Place index.html in frontend/"}

# ──────────────────────────────────────────────────────────────────────────────
# Execution Mode Control API
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/system/mode")
async def get_execution_mode():
    """Get the current system execution mode (normal or testing)."""
    return {
        "mode": current_execution_mode,
        "description": "Normal Mode (Lightweight, Safe)" if current_execution_mode == "normal" else "Testing Mode (OS Concept Workloads Unlocked)"
    }

@app.post("/api/system/mode")
async def set_execution_mode(req: ModeRequest):
    """Set the system execution mode (normal or testing)."""
    global current_execution_mode
    mode = req.mode.lower().strip()
    if mode not in ("normal", "testing"):
        raise HTTPException(400, "Mode must be 'normal' or 'testing'")
    current_execution_mode = mode
    logger.info(f"System execution mode switched to: {current_execution_mode.upper()}")
    return {
        "status": "ok",
        "mode": current_execution_mode,
        "description": "Normal Mode (Lightweight, Safe)" if current_execution_mode == "normal" else "Testing Mode (OS Concept Workloads Unlocked)"
    }

# ──────────────────────────────────────────────────────────────────────────────
# WebSocket: Real-Time Telemetry Stream
# ──────────────────────────────────────────────────────────────────────────────

connected_websockets: set = set()


async def _broadcast_live_deadlock_update(snapshot: dict):
    if not connected_websockets:
        return
    message = {
        "type": "live_deadlock_update",
        "timestamp": time.time(),
        "live_deadlock": live_deadlock.get_telemetry_summary(),
        "snapshot": snapshot,
    }
    dead = []
    for ws in list(connected_websockets):
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        connected_websockets.discard(ws)


def _on_live_deadlock_snapshot_changed(snapshot: dict):
    loop = main_event_loop
    if loop is not None and loop.is_running() and connected_websockets:
        try:
            asyncio.run_coroutine_threadsafe(_broadcast_live_deadlock_update(snapshot), loop)
        except Exception:
            pass


live_deadlock.add_update_callback(_on_live_deadlock_snapshot_changed)

@app.websocket("/ws/telemetry")
async def telemetry_websocket(websocket: WebSocket):
    """Stream system telemetry at ~2.0s intervals via WebSocket."""
    await websocket.accept()
    connected_websockets.add(websocket)
    logger.info(f"WebSocket client connected (active: {len(connected_websockets)})")

    try:
        while True:
            snapshot = monitor.get_snapshot()
            processes = monitor.get_processes()
            history = monitor.get_history()
            decisions = policy.get_decisions(limit=15)
            sandboxes = isolation.list_sandboxes()
            categories = policy.get_process_categories()

            payload = {
                "type": "telemetry",
                "timestamp": time.time(),
                "mode": current_execution_mode,
                "system": snapshot,
                "processes": processes,
                "history": history,
                "decisions": decisions,
                "sandboxes": sandboxes,
                "process_categories": categories,
                "policy_stats": policy.get_stats(),
                "live_deadlock": live_deadlock.get_telemetry_summary(),
            }

            await websocket.send_json(payload)
            await asyncio.sleep(2.0)

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.warning(f"WebSocket session closed: {e}")
    finally:
        connected_websockets.discard(websocket)

# ──────────────────────────────────────────────────────────────────────────────
# REST API: System Telemetry
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/system")
async def get_system():
    """Get current system snapshot."""
    return monitor.get_snapshot() or {}

@app.get("/api/processes")
async def get_processes():
    """Get all running processes."""
    return monitor.get_processes()

@app.get("/api/processes/{pid}")
async def get_process(pid: int):
    """Get a single process by PID."""
    proc = monitor.get_process(pid)
    if not proc:
        raise HTTPException(404, f"Process {pid} not found")
    return proc

@app.get("/api/processes/top/{sort_by}")
async def get_top_processes(sort_by: str = "cpu", limit: int = 10):
    """Get top N processes sorted by cpu or memory."""
    return monitor.get_top_processes(sort_by, limit)

@app.get("/api/history")
async def get_history():
    """Get CPU and memory usage history."""
    return monitor.get_history()

@app.get("/api/events")
async def get_events():
    """Get process lifecycle events."""
    return monitor.get_events()

# ──────────────────────────────────────────────────────────────────────────────
# REST API: Sandbox / Isolation Management
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/sandboxes")
async def list_sandboxes():
    """List all active sandboxes."""
    return isolation.list_sandboxes()

@app.post("/api/sandboxes")
async def create_sandbox(req: SandboxCreateRequest):
    """Create a new Job Object sandbox."""
    try:
        config = SandboxConfig(
            name=req.name,
            cpu_rate_percent=req.cpu_rate_percent,
            memory_limit_mb=req.memory_limit_mb,
            per_process_memory_limit_mb=req.per_process_memory_limit_mb,
            job_memory_limit_mb=req.job_memory_limit_mb,
            max_active_processes=req.max_active_processes,
            priority_class=req.priority_class,
            kill_on_close=req.kill_on_close,
            max_working_set_mb=req.max_working_set_mb,
        )
        container = isolation.create_sandbox(config)
        return {
            "status": "created",
            "name": req.name,
            "job_name": container.job_name,
            "verified_limits": container.verified_limits,
        }
    except Exception as e:
        raise HTTPException(400, str(e))

@app.put("/api/sandboxes/{name}")
async def update_sandbox(name: str, req: SandboxUpdateRequest):
    """Update sandbox limits dynamically."""
    updates = {k: v for k, v in req.dict().items() if v is not None}
    success = isolation.update_sandbox_limits(name, **updates)
    if not success:
        raise HTTPException(404, f"Sandbox '{name}' not found")
    return {"status": "updated", "name": name}

@app.delete("/api/sandboxes/{name}")
async def destroy_sandbox(name: str):
    """Destroy a sandbox and terminate its processes."""
    success = isolation.destroy_sandbox(name)
    if not success:
        raise HTTPException(404, f"Sandbox '{name}' not found")
    return {"status": "destroyed", "name": name}

@app.post("/api/sandboxes/{name}/assign")
async def assign_to_sandbox(name: str, req: AssignProcessRequest):
    """Assign a process to a sandbox."""
    try:
        isolation.assign_process(name, req.pid)
        return {"status": "assigned", "sandbox": name, "pid": req.pid}
    except Exception as e:
        raise HTTPException(400, str(e))

@app.get("/api/sandboxes/{name}/accounting")
async def get_sandbox_accounting(name: str):
    """Get kernel accounting for a sandbox."""
    result = isolation.get_job_accounting(name)
    if result is None:
        raise HTTPException(404, f"Sandbox '{name}' not found")
    return result

# ──────────────────────────────────────────────────────────────────────────────
# REST API: Process Control
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/api/processes/{pid}/priority")
async def set_priority(pid: int, req: PriorityRequest):
    """Set process priority class."""
    try:
        success = isolation.set_process_priority(pid, req.priority)
        if not success:
            raise HTTPException(400, f"Failed to set priority for PID {pid}")
        return {"status": "ok", "pid": pid, "priority": req.priority}
    except ValueError as e:
        raise HTTPException(400, str(e))

@app.get("/api/processes/{pid}/priority")
async def get_priority(pid: int):
    """Get process priority class."""
    priority = isolation.get_process_priority(pid)
    if priority is None:
        raise HTTPException(404, f"Process {pid} not found")
    return {"pid": pid, "priority": priority}

@app.post("/api/processes/{pid}/affinity")
async def set_affinity(pid: int, req: AffinityRequest):
    """Set process CPU affinity mask."""
    success = isolation.set_process_affinity(pid, req.core_mask)
    if not success:
        raise HTTPException(400, f"Failed to set affinity for PID {pid}")
    return {"status": "ok", "pid": pid, "core_mask": hex(req.core_mask)}

@app.post("/api/processes/{pid}/trim")
async def trim_process(pid: int):
    """Trim process working set."""
    success = isolation.trim_working_set(pid)
    if not success:
        raise HTTPException(400, f"Failed to trim working set for PID {pid}")
    return {"status": "trimmed", "pid": pid}

@app.post("/api/processes/{pid}/terminate")
async def terminate_process(pid: int):
    """Safely terminate a process by PID (blocks Windows system processes)."""
    if is_critical_process(pid):
        raise HTTPException(403, f"PID {pid} is a protected Windows system process and cannot be terminated.")
    import psutil
    try:
        if not psutil.pid_exists(pid):
            raise HTTPException(404, f"Process {pid} not found")
        kill_process_tree(pid)
        return {"status": "terminated", "pid": pid}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e))

# ──────────────────────────────────────────────────────────────────────────────
# REST API: Policy Engine
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/policy/config")
async def get_policy_config():
    """Get current policy configuration."""
    return policy.get_config()

@app.put("/api/policy/config")
async def update_policy_config(req: PolicyConfigUpdate):
    """Update policy configuration."""
    updates = {k: v for k, v in req.dict().items() if v is not None}
    return policy.update_config(**updates)

@app.get("/api/policy/decisions")
async def get_decisions(limit: int = 50):
    """Get recent policy decisions."""
    return policy.get_decisions(limit)

@app.get("/api/policy/stats")
async def get_policy_stats():
    """Get policy engine statistics."""
    return policy.get_stats()

@app.get("/api/isolation/log")
async def get_isolation_log(limit: int = 50):
    """Get isolation engine decision log."""
    return isolation.get_decision_log(limit)

# ──────────────────────────────────────────────────────────────────────────────
# REST API: Deadlock Engine
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/deadlock/scenarios")
async def get_deadlock_scenarios():
    """Get available pre-built deadlock scenarios."""
    return {
        "scenarios": [
            {"id": "circular_wait", "name": "Circular Wait (Deadlock)", "description": "3 processes & 3 resources in circular wait"},
            {"id": "dining_philosophers", "name": "Dining Philosophers", "description": "5 philosophers competing for 5 chopsticks"},
            {"id": "multi_resource", "name": "Multi-Resource Contention", "description": "4 processes with cross-resource dependencies"},
            {"id": "no_deadlock", "name": "Safe Resource Flow", "description": "Requests can be satisfied immediately"},
            {"id": "banker_safe", "name": "Banker's Safe State", "description": "5 processes, safe sequence exists"},
            {"id": "banker_unsafe", "name": "Banker's Unsafe State", "description": "3 processes, no safe sequence"},
        ]
    }

@app.post("/api/deadlock/wfg/load/{scenario_id}")
async def load_wfg_scenario(scenario_id: str):
    """Load a pre-built WFG scenario."""
    global wfg
    if scenario_id == "circular_wait":
        wfg = DeadlockScenarioBuilder.circular_wait_scenario()
    elif scenario_id == "dining_philosophers":
        wfg = DeadlockScenarioBuilder.dining_philosophers_scenario()
    elif scenario_id == "multi_resource":
        wfg = DeadlockScenarioBuilder.multi_resource_scenario()
    elif scenario_id == "no_deadlock":
        wfg = DeadlockScenarioBuilder.no_deadlock_scenario()
    else:
        raise HTTPException(404, f"Scenario '{scenario_id}' not found")

    result = wfg.detect_deadlock()
    return {
        "graph": wfg.get_graph_data(),
        "result": {
            "has_deadlock": result.has_deadlock,
            "cycles": result.cycles,
            "involved_processes": list(result.involved_processes),
            "involved_resources": list(result.involved_resources),
            "recommended_victim": result.recommended_victim,
            "victim_reason": result.victim_reason,
        },
    }

@app.post("/api/deadlock/wfg/custom")
async def custom_wfg(processes: List[WFGProcessRequest], resources: List[WFGResourceRequest]):
    """Analyze a custom WFG."""
    global wfg
    wfg = WaitForGraph()

    for r in resources:
        wfg.add_resource(Resource(
            r.id, r.name, r.total_instances,
            r.available_instances if r.available_instances is not None else r.total_instances
        ))

    for p in processes:
        wfg.add_process(ProcessNode(
            p.id, p.name, p.priority, p.runtime_seconds, p.memory_mb,
            dict(p.allocated), dict(p.requesting),
        ))

    result = wfg.detect_deadlock()
    return {
        "graph": wfg.get_graph_data(),
        "result": {
            "has_deadlock": result.has_deadlock,
            "cycles": result.cycles,
            "involved_processes": list(result.involved_processes),
            "involved_resources": list(result.involved_resources),
            "recommended_victim": result.recommended_victim,
            "victim_reason": result.victim_reason,
        },
    }

@app.get("/api/deadlock/wfg")
async def get_wfg():
    """Get current WFG state and detection result."""
    result = wfg.detect_deadlock()
    return {
        "graph": wfg.get_graph_data(),
        "result": {
            "has_deadlock": result.has_deadlock,
            "cycles": result.cycles,
            "involved_processes": list(result.involved_processes),
            "involved_resources": list(result.involved_resources),
            "recommended_victim": result.recommended_victim,
            "victim_reason": result.victim_reason,
        },
    }

@app.post("/api/deadlock/banker/check")
async def banker_check(req: BankerRequest):
    """Run Banker's Algorithm safety check."""
    result = bankers.check_safety(
        req.processes, req.resources, req.available,
        req.max_matrix, req.allocation_matrix,
    )
    return {
        "is_safe": result.is_safe,
        "safe_sequence": result.safe_sequence,
        "finish_flags": result.finish_flags,
        "available": result.available,
        "allocation_matrix": result.allocation_matrix,
        "need_matrix": result.need_matrix,
        "max_matrix": result.max_matrix,
        "process_names": result.process_names,
        "resource_names": result.resource_names,
    }

@app.post("/api/deadlock/banker/simulate")
async def banker_simulate(req: BankerSimRequest):
    """Simulate a resource request using Banker's Algorithm."""
    result, granted = bankers.simulate_request(
        req.processes, req.resources, req.available,
        req.max_matrix, req.allocation_matrix,
        req.requesting_process, req.request,
    )
    return {
        "would_be_granted": granted,
        "is_safe": result.is_safe,
        "safe_sequence": result.safe_sequence,
        "need_matrix": result.need_matrix,
    }

@app.get("/api/deadlock/banker/scenarios/{scenario_id}")
async def get_banker_scenario(scenario_id: str):
    """Get a pre-built Banker's Algorithm scenario."""
    if scenario_id == "safe":
        scenario = DeadlockScenarioBuilder.banker_safe_scenario()
    elif scenario_id == "unsafe":
        scenario = DeadlockScenarioBuilder.banker_unsafe_scenario()
    else:
        raise HTTPException(404, f"Scenario '{scenario_id}' not found")

    result = bankers.check_safety(
        scenario["processes"], scenario["resources"],
        scenario["available"], scenario["max_matrix"],
        scenario["allocation_matrix"],
    )
    return {
        "scenario": scenario,
        "result": {
            "is_safe": result.is_safe,
            "safe_sequence": result.safe_sequence,
            "finish_flags": result.finish_flags,
            "need_matrix": result.need_matrix,
        },
    }

# ──────────────────────────────────────────────────────────────────────────────
# REST API: Real-Time Windows-Native Live Deadlock Detection & Selective Recovery
# ──────────────────────────────────────────────────────────────────────────────

def _raise_live_http_error(exc: Exception):
    if isinstance(exc, LiveAuthError):
        raise HTTPException(401, str(exc))
    if isinstance(exc, LiveConflictError):
        raise HTTPException(409, str(exc))
    if isinstance(exc, PermissionError):
        raise HTTPException(403, str(exc))
    if isinstance(exc, KeyError):
        raise HTTPException(404, str(exc))
    if isinstance(exc, (LiveValidationError, ValueError)):
        raise HTTPException(400, str(exc))
    raise HTTPException(500, str(exc))


def _assert_same_origin_dashboard(request: Request):
    """
    Ensure recovery authorization tokens are only issued to the same-origin
    local dashboard session (blocking cross-site/cross-origin browser requests).
    """
    host = (request.headers.get("host") or "").lower().strip()
    sec_fetch_site = (request.headers.get("sec-fetch-site") or "").lower().strip()
    if sec_fetch_site and sec_fetch_site not in ("same-origin", "none"):
        raise HTTPException(403, "Cross-origin recovery authorization is forbidden")

    origin = (request.headers.get("origin") or "").strip()
    if origin:
        parsed_origin = urlparse(origin)
        origin_host = (parsed_origin.netloc or "").lower().strip()
        if not host or origin_host != host:
            raise HTTPException(403, "Cross-origin recovery authorization is forbidden")

    referer = (request.headers.get("referer") or "").strip()
    if referer and not origin:
        parsed_ref = urlparse(referer)
        ref_host = (parsed_ref.netloc or "").lower().strip()
        if host and ref_host and ref_host != host:
            raise HTTPException(403, "Cross-origin recovery authorization is forbidden")

@app.post("/api/deadlock/live/runs/register")
async def register_live_deadlock_run(req: LiveRunRegisterRequest):
    """
    Register an independent Windows workload run, create & verify a dedicated
    Windows NT Job Object sandbox, attach the supervisor PID, and return a
    unique secret per-run authentication token.
    """
    try:
        return live_deadlock.register_run(
            supervisor_pid=req.supervisor_pid,
            supervisor_tid=req.supervisor_tid,
            supervisor_create_time_100ns=req.supervisor_create_time_100ns,
            workload_name=req.workload_name,
            expected_workers=req.expected_workers,
            resources=req.resources,
            cpu_rate_percent=req.cpu_rate_percent,
            job_memory_limit_mb=req.job_memory_limit_mb,
            per_process_memory_limit_mb=req.per_process_memory_limit_mb,
            max_active_processes=req.max_active_processes,
            max_runtime_seconds=req.max_runtime_seconds,
            priority_class=req.priority_class,
            kill_on_close=req.kill_on_close,
        )
    except Exception as e:
        _raise_live_http_error(e)

@app.post("/api/deadlock/live/runs/{run_id}/workers")
async def register_live_worker(
    run_id: str,
    req: LiveWorkerRegisterRequest,
    x_run_token: Optional[str] = Header(None, alias="X-Run-Token"),
):
    """Register a child worker process and verify Windows Job Object inheritance."""
    try:
        return live_deadlock.register_worker(
            run_id=run_id,
            run_token=x_run_token,
            worker_id=req.worker_id,
            name=req.name,
            pid=req.pid,
            tid=req.tid,
            create_time_100ns=req.create_time_100ns,
            recovery_priority=req.recovery_priority,
            max_claim=req.max_claim,
        )
    except Exception as e:
        _raise_live_http_error(e)

@app.post("/api/deadlock/live/runs/{run_id}/events")
async def ingest_live_sync_event(
    run_id: str,
    req: LiveSyncEventRequest,
    x_run_token: Optional[str] = Header(None, alias="X-Run-Token"),
):
    """Ingest a real, sequenced synchronization event from an authenticated worker."""
    try:
        return live_deadlock.record_sync_event(
            run_id=run_id,
            run_token=x_run_token,
            worker_id=req.worker_id,
            pid=req.pid,
            tid=req.tid,
            create_time_100ns=req.create_time_100ns,
            event_type=req.event_type,
            resource_id=req.resource_id,
            os_mutex_name=req.os_mutex_name,
            seq=req.seq,
            wait_result=req.wait_result,
            wait_code=req.wait_code,
            abandoned=req.abandoned,
            state_validated=req.state_validated,
            released_after_abandoned=req.released_after_abandoned,
            exit_code=req.exit_code,
            details=req.details,
        )
    except Exception as e:
        _raise_live_http_error(e)

@app.post("/api/deadlock/live/runs/{run_id}/heartbeat")
async def live_run_heartbeat(
    run_id: str,
    req: LiveHeartbeatRequest,
    x_run_token: Optional[str] = Header(None, alias="X-Run-Token"),
):
    """Receive supervisor or worker heartbeat and return watchdog remaining time."""
    try:
        return live_deadlock.record_heartbeat(
            run_id=run_id,
            run_token=x_run_token,
            actor_pid=req.actor_pid,
            actor_role=req.actor_role,
            worker_id=req.worker_id,
        )
    except Exception as e:
        _raise_live_http_error(e)

@app.get("/api/deadlock/live/runs")
async def list_live_deadlock_runs():
    """List active and recent live Windows-native deadlock runs (never exposes secret tokens)."""
    return {"runs": live_deadlock.list_runs()}

@app.get("/api/deadlock/live/runs/{run_id}")
async def get_live_deadlock_run(run_id: str):
    """Get full live state (sandbox, workers, RAG/WFG, DFS, Banker's, events, watchdog)."""
    try:
        return live_deadlock.get_run(run_id)
    except Exception as e:
        _raise_live_http_error(e)

@app.post("/api/deadlock/live/runs/{run_id}/recovery-authorization")
async def request_live_recovery_authorization(
    run_id: str,
    request: Request,
    req: Optional[LiveRecoveryAuthRequest] = None,
):
    """
    Issue a short-lived, single-use, deadlock-bound recovery authorization token
    for the authorized same-origin dashboard session.
    """
    _assert_same_origin_dashboard(request)
    try:
        ttl = (
            req.ttl_seconds
            if (req is not None and req.ttl_seconds is not None)
            else DEFAULT_RECOVERY_AUTH_TTL_SECONDS
        )
        return live_deadlock.issue_recovery_authorization(
            run_id=run_id,
            victim_pid=req.victim_pid if req is not None else None,
            snapshot_version=req.snapshot_version if req is not None else None,
            ttl_seconds=ttl,
        )
    except HTTPException:
        raise
    except Exception as e:
        _raise_live_http_error(e)

@app.get("/api/deadlock/live/runs/{run_id}/recovery-challenge")
async def get_live_recovery_challenge(run_id: str, request: Request):
    """Backward-compatible alias for issuing active deadlock-bound recovery authorization."""
    _assert_same_origin_dashboard(request)
    try:
        return live_deadlock.get_recovery_challenge(run_id)
    except HTTPException:
        raise
    except Exception as e:
        _raise_live_http_error(e)

@app.post("/api/deadlock/live/runs/{run_id}/recover")
def approve_live_deadlock_recovery(
    run_id: str,
    req: LiveRecoverRequest,
    x_run_token: Optional[str] = Header(None, alias="X-Run-Token"),
    x_recovery_token: Optional[str] = Header(None, alias="X-Recovery-Token"),
):
    """
    Execute explicit user-approved selective recovery in FastAPI's thread pool
    (non-blocking so concurrent WAIT_ABANDONED / state_validated / released events
    from surviving workers are ingested immediately while recovery verifies).
    """
    try:
        return live_deadlock.approve_and_recover(
            run_id=run_id,
            sandbox_name=req.sandbox_name,
            pid=req.pid,
            create_time_100ns=req.create_time_100ns,
            approved=req.approved,
            run_token=x_run_token,
            recovery_token=x_recovery_token,
            snapshot_version=req.snapshot_version,
            deadlock_cycle=req.deadlock_cycle,
            operator_note=req.operator_note,
        )
    except Exception as e:
        _raise_live_http_error(e)

@app.post("/api/deadlock/live/runs/{run_id}/complete")
async def complete_live_deadlock_run(
    run_id: str,
    req: LiveCompleteRequest,
    x_run_token: Optional[str] = Header(None, alias="X-Run-Token"),
):
    """Complete a live deadlock run and schedule Job Object cleanup."""
    try:
        return live_deadlock.complete_run(
            run_id=run_id,
            run_token=x_run_token,
            outcome_note=req.outcome_note,
        )
    except Exception as e:
        _raise_live_http_error(e)

@app.delete("/api/deadlock/live/runs/{run_id}")
async def abort_live_deadlock_run(run_id: str):
    """Explicitly abort a live run and tear down its entire Job Object."""
    try:
        return live_deadlock.abort_run(run_id)
    except Exception as e:
        _raise_live_http_error(e)

# ──────────────────────────────────────────────────────────────────────────────
# REST API: MLFQ Scheduler Simulator
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/api/simulator/mlfq")
async def run_mlfq(req: MLFQRequest):
    """Run MLFQ scheduler simulation."""
    config = MLFQConfig(
        num_queues=req.num_queues,
        time_quanta=req.time_quanta,
        boost_interval=req.boost_interval,
    )
    scheduler = MLFQScheduler(config)

    for p in req.processes:
        scheduler.add_process(MLFQProcess(
            id=p["id"],
            name=p["name"],
            total_burst=p["total_burst"],
            arrival_time=p.get("arrival_time", 0),
        ))

    return scheduler.run_simulation()

@app.get("/api/simulator/mlfq/demo")
async def mlfq_demo():
    """Run demo MLFQ simulation."""
    scheduler = MLFQScheduler.create_demo_scenario()
    return scheduler.run_simulation()

# ──────────────────────────────────────────────────────────────────────────────
# REST API: Page Replacement Simulator
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/api/simulator/page-replacement")
async def run_page_replacement(req: PageReplacementRequest):
    """Run page replacement simulation."""
    if req.algorithm:
        algo = req.algorithm.upper()
        if algo == "FIFO":
            return page_sim.simulate_fifo(req.reference_string, req.num_frames)
        elif algo == "LRU":
            return page_sim.simulate_lru(req.reference_string, req.num_frames)
        elif algo in ("CLOCK", "SECOND_CHANCE"):
            return page_sim.simulate_clock(req.reference_string, req.num_frames)
        else:
            raise HTTPException(400, f"Unknown algorithm: {req.algorithm}")
    else:
        return page_sim.compare_algorithms(req.reference_string, req.num_frames)

@app.get("/api/simulator/page-replacement/demo")
async def page_replacement_demo():
    """Run demo page replacement comparison."""
    ref_string = PageReplacementSimulator.demo_reference_string()
    return page_sim.compare_algorithms(ref_string, num_frames=3)

# ──────────────────────────────────────────────────────────────────────────────
# REST API: Workload Launcher
# ──────────────────────────────────────────────────────────────────────────────

WORKLOAD_DIR = Path(__file__).parent.parent / "workloads"

@app.post("/api/workloads/launch")
async def launch_workload(req: WorkloadRequest):
    """Launch a synthetic workload process (Requires Testing Mode)."""
    if current_execution_mode != "testing":
        raise HTTPException(
            403,
            "Workload execution is disabled in Normal Mode. "
            "Please activate Testing Mode in the header or via POST /api/system/mode to test OS workloads."
        )

    # Restrict concurrent active workloads to protect host machine
    active_count = sum(1 for p in spawned_workloads.values() if p.poll() is None)
    if active_count >= 3:
        raise HTTPException(
            429,
            "Maximum 3 concurrent test workloads allowed to protect laptop resources. Please stop an existing workload first."
        )

    workload_map = {
        "cpu_hog": "cpu_hog.py",
        "memory_hog": "memory_hog.py",
        "deadlock_demo": "deadlock_demo.py",
        "circular_deadlock": "circular_deadlock.py",
        "dining_philosophers": "dining_philosophers.py",
        "multi_resource_deadlock": "multi_resource_deadlock.py",
        "sandboxed_app": "sandboxed_app.py",
    }

    script = workload_map.get(req.workload_type)
    if not script:
        raise HTTPException(400, f"Unknown workload: {req.workload_type}")

    script_path = WORKLOAD_DIR / script
    if not script_path.exists():
        raise HTTPException(404, f"Workload script not found: {script}")

    cmd = [sys.executable, str(script_path)] + req.args
    proc = None
    launch_note = ""

    # Try launching with escalating fallbacks to handle WinError 5 (Access Denied)
    # when the server itself is inside a Job Object that blocks child breakaway.
    try:
        # Attempt 1: Breakaway from parent Job Object + new console
        proc = subprocess.Popen(
            cmd,
            creationflags=subprocess.CREATE_NEW_CONSOLE | 0x01000000,  # CREATE_BREAKAWAY_FROM_JOB
        )
        launch_note = "breakaway+console"
    except OSError:
        pass

    if proc is None:
        try:
            # Attempt 2: New console only (no breakaway — process inherits parent Job Object)
            proc = subprocess.Popen(
                cmd,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            launch_note = "console-only (inherited job)"
        except OSError:
            pass

    if proc is None:
        try:
            # Attempt 3: Silent background process (no console window)
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            launch_note = "background (no console)"
        except Exception as e:
            raise HTTPException(500, f"All launch strategies failed: {e}")

    pid = proc.pid
    workload_id = f"{req.workload_type}_{pid}"
    spawned_workloads[workload_id] = proc
    logger.info(f"Launched workload '{req.workload_type}' PID={pid} [{launch_note}]")

    # If sandbox specified, assign to it
    if req.sandbox_name:
        try:
            isolation.assign_process(req.sandbox_name, pid)
        except Exception as e:
            logger.warning(f"Failed to assign PID {pid} to sandbox '{req.sandbox_name}': {e}")

    return {
        "status": "launched",
        "workload_id": workload_id,
        "pid": pid,
        "workload_type": req.workload_type,
        "sandbox": req.sandbox_name,
        "launch_mode": launch_note,
    }

@app.get("/api/workloads")
async def list_workloads():
    """List spawned workloads and their status."""
    result = []
    for wid, proc in list(spawned_workloads.items()):
        poll = proc.poll()
        result.append({
            "workload_id": wid,
            "pid": proc.pid,
            "running": poll is None,
            "return_code": poll,
        })
    return result

@app.delete("/api/workloads/{workload_id}")
async def stop_workload(workload_id: str):
    """Stop a spawned workload and all its descendant processes."""
    proc = spawned_workloads.get(workload_id)
    if not proc:
        raise HTTPException(404, f"Workload '{workload_id}' not found")
    try:
        kill_process_tree(proc.pid)
        del spawned_workloads[workload_id]
        return {"status": "terminated", "workload_id": workload_id}
    except Exception as e:
        raise HTTPException(500, str(e))

# ──────────────────────────────────────────────────────────────────────────────
# Main Entry Point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
