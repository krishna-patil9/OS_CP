"""
Circular Wait Real-Time Deadlock Scenario (Windows-Native)

Spawns 2 actual Windows OS worker processes inside a dedicated Windows NT Job Object
contending over 2 real Windows Named Mutexes (Lock_A, Lock_B):

  Worker A (RecPri=1): Holds Lock_A -> Requests Lock_B
  Worker B (RecPri=3): Holds Lock_B -> Requests Lock_A

Expected WFG:
  Worker A -> Worker B -> Worker A

Run independently while `python run_server.py` is running:
  python workloads\\circular_deadlock.py
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
        {"id": "Lock_A", "name": "Lock_A", "total_instances": 1},
        {"id": "Lock_B", "name": "Lock_B", "total_instances": 1},
    ]

    workers = [
        WorkerSpec(
            worker_id="worker_a",
            name="Worker A",
            recovery_priority=1,  # Lowest recovery priority -> recommended victim
            first_lock="Lock_A",
            second_lock="Lock_B",
            max_claim={"Lock_A": 1, "Lock_B": 1},
        ),
        WorkerSpec(
            worker_id="worker_b",
            name="Worker B",
            recovery_priority=3,
            first_lock="Lock_B",
            second_lock="Lock_A",
            max_claim={"Lock_A": 1, "Lock_B": 1},
        ),
    ]

    return ScenarioDefinition(
        scenario_key="circular_deadlock",
        workload_name="Circular Deadlock",
        description="Classic 2-process circular wait (Worker A <-> Worker B) using Lock_A & Lock_B Named Mutexes",
        resources=resources,
        workers=workers,
        default_max_procs=4,       # 1 supervisor + 2 workers + margin
        default_cpu_cap=20,
        default_job_mem_mb=512,
        default_proc_mem_mb=256,
        default_timeout=120,
        default_delay=0.25,
    )


def main():
    run_deadlock_scenario(build_scenario(), Path(__file__))


if __name__ == "__main__":
    main()
