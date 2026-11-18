"""
Multi-Resource Real-Time Deadlock Scenario (Windows-Native)

Spawns 4 actual Windows OS worker processes inside a dedicated Windows NT Job Object
contending over 4 real Windows Named Mutexes (Mutex_A, Mutex_B, Shared_DB, GPU_Lock):

  Worker A (RecPri=2): Holds Mutex_A   -> Requests Mutex_B   (IN CYCLE)
  Worker B (RecPri=3): Holds Mutex_B   -> Requests Shared_DB (IN CYCLE)
  Worker C (RecPri=4): Holds Shared_DB -> Requests Mutex_A   (IN CYCLE)
  Worker D (RecPri=1): Holds GPU_Lock  -> Requests Mutex_B   (BLOCKED BY B, NOT IN CYCLE)

Expected WFG:
  Worker A -> Worker B -> Worker C -> Worker A   (3-process deadlock cycle)
  Worker D -> Worker B                           (blocked dependency, NOT in cycle)

Note: Worker D intentionally has recovery_priority=1 (lower than cycle members) to
prove that victim selection ONLY considers eligible workers inside the actual cycle
(recommending Worker A with recovery_priority=2, never Worker D).

Run independently while `python run_server.py` is running:
  python workloads\\multi_resource_deadlock.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sdk.deadlock_sdk import (  # noqa: F401
    DeadlockMonitorClient,
    InstrumentedMutex,
    Win32NamedEvent,
    get_current_process_identity,
)
from sdk.scenario_runner import ScenarioDefinition, WorkerSpec, run_deadlock_scenario


def build_scenario() -> ScenarioDefinition:
    resources = [
        {"id": "Mutex_A", "name": "Mutex_A", "total_instances": 1},
        {"id": "Mutex_B", "name": "Mutex_B", "total_instances": 1},
        {"id": "Shared_DB", "name": "Shared_DB", "total_instances": 1},
        {"id": "GPU_Lock", "name": "GPU_Lock", "total_instances": 1},
    ]

    workers = [
        WorkerSpec(
            worker_id="worker_a",
            name="Worker A",
            recovery_priority=2,  # Lowest recovery priority INSIDE the 3-process cycle
            first_lock="Mutex_A",
            second_lock="Mutex_B",
            max_claim={"Mutex_A": 1, "Mutex_B": 1},
        ),
        WorkerSpec(
            worker_id="worker_b",
            name="Worker B",
            recovery_priority=3,
            first_lock="Mutex_B",
            second_lock="Shared_DB",
            max_claim={"Mutex_B": 1, "Shared_DB": 1},
        ),
        WorkerSpec(
            worker_id="worker_c",
            name="Worker C",
            recovery_priority=4,
            first_lock="Shared_DB",
            second_lock="Mutex_A",
            max_claim={"Shared_DB": 1, "Mutex_A": 1},
        ),
        WorkerSpec(
            worker_id="worker_d",
            name="Worker D",
            recovery_priority=1,  # Blocked by Worker B, NOT in cycle — must NOT be picked as victim
            first_lock="GPU_Lock",
            second_lock="Mutex_B",
            max_claim={"GPU_Lock": 1, "Mutex_B": 1},
        ),
    ]

    return ScenarioDefinition(
        scenario_key="multi_resource_deadlock",
        workload_name="Multi-Resource Deadlock",
        description="4 OS workers & 4 Windows Named Mutexes: A->B->C->A cycle plus D blocked on B (non-cycle)",
        resources=resources,
        workers=workers,
        default_max_procs=6,       # 1 supervisor + 4 workers + margin
        default_cpu_cap=20,
        default_job_mem_mb=640,
        default_proc_mem_mb=256,
        default_timeout=120,
        default_delay=0.25,
    )


def main():
    run_deadlock_scenario(build_scenario(), Path(__file__))


if __name__ == "__main__":
    main()
