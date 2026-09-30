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
from typing import List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.system_monitor import SystemMonitor
from backend.isolation_engine import IsolationEngine, SandboxConfig
from backend.policy_engine import PolicyEngine, PolicyConfig
from backend.deadlock_engine import WaitForGraph, BankersAlgorithm, DeadlockScenarioBuilder, ProcessNode, Resource
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

monitor = SystemMonitor(interval=0.5)
isolation = IsolationEngine()
policy = PolicyEngine(monitor, isolation)
bankers = BankersAlgorithm()
page_sim = PageReplacementSimulator()

# Deadlock WFG (stateful for interactive graph editing)
wfg = WaitForGraph()

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

# ──────────────────────────────────────────────────────────────────────────────
# Pydantic Models
# ──────────────────────────────────────────────────────────────────────────────

class SandboxCreateRequest(BaseModel):
    name: str
    cpu_rate_percent: Optional[int] = None
    memory_limit_mb: Optional[int] = None
    max_active_processes: Optional[int] = None
    priority_class: Optional[str] = None
    kill_on_close: bool = True
    max_working_set_mb: Optional[int] = None

class SandboxUpdateRequest(BaseModel):
    cpu_rate_percent: Optional[int] = None
    memory_limit_mb: Optional[int] = None
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

# ──────────────────────────────────────────────────────────────────────────────
# Startup / Shutdown
# ──────────────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    monitor.start()
    policy.start()
    logger.info("All engines started")

@app.on_event("shutdown")
async def shutdown():
    policy.stop()
    monitor.stop()
    # Kill spawned workloads
    for name, proc in spawned_workloads.items():
        try:
            proc.terminate()
        except Exception:
            pass
    logger.info("All engines stopped")

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
# WebSocket: Real-Time Telemetry Stream
# ──────────────────────────────────────────────────────────────────────────────

@app.websocket("/ws/telemetry")
async def telemetry_websocket(websocket: WebSocket):
    """Stream system telemetry at 500ms intervals via WebSocket."""
    await websocket.accept()
    logger.info("WebSocket client connected")

    try:
        while True:
            snapshot = monitor.get_snapshot()
            processes = monitor.get_processes()
            history = monitor.get_history()
            decisions = policy.get_decisions(limit=20)
            sandboxes = isolation.list_sandboxes()
            categories = policy.get_process_categories()

            payload = {
                "type": "telemetry",
                "timestamp": time.time(),
                "system": snapshot,
                "processes": processes,
                "history": history,
                "decisions": decisions,
                "sandboxes": sandboxes,
                "process_categories": categories,
                "policy_stats": policy.get_stats(),
            }

            await websocket.send_json(payload)
            await asyncio.sleep(0.5)

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")

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
            max_active_processes=req.max_active_processes,
            priority_class=req.priority_class,
            kill_on_close=req.kill_on_close,
            max_working_set_mb=req.max_working_set_mb,
        )
        isolation.create_sandbox(config)
        return {"status": "created", "name": req.name}
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
    """Terminate a process by PID."""
    import psutil
    try:
        proc = psutil.Process(pid)
        proc.terminate()
        return {"status": "terminated", "pid": pid}
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
            {"id": "circular_wait", "name": "Circular Wait (Deadlock)", "description": "3 processes in circular wait"},
            {"id": "no_deadlock", "name": "No Deadlock", "description": "Requests can be satisfied"},
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
            "need_matrix": result.need_matrix,
        },
    }

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
    """Launch a synthetic workload process."""
    workload_map = {
        "cpu_hog": "cpu_hog.py",
        "memory_hog": "memory_hog.py",
        "deadlock_demo": "deadlock_demo.py",
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
    """Stop a spawned workload."""
    proc = spawned_workloads.get(workload_id)
    if not proc:
        raise HTTPException(404, f"Workload '{workload_id}' not found")
    try:
        proc.terminate()
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
