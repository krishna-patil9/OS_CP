"""
Dining Philosophers Real-Time Deadlock Scenario (Windows-Native)

Spawns 5 actual Windows OS worker processes (Philosopher 1 .. Philosopher 5)
inside a dedicated Windows NT Job Object contending over 5 real Windows Named Mutexes
(Chopstick_1 .. Chopstick_5):

  Philosopher 1 (RecPri=1): Holds Chopstick_1 -> Requests Chopstick_2
  Philosopher 2 (RecPri=2): Holds Chopstick_2 -> Requests Chopstick_3
  Philosopher 3 (RecPri=3): Holds Chopstick_3 -> Requests Chopstick_4
  Philosopher 4 (RecPri=4): Holds Chopstick_4 -> Requests Chopstick_5
  Philosopher 5 (RecPri=5): Holds Chopstick_5 -> Requests Chopstick_1

Run independently while `python run_server.py` is running:
  python workloads\\dining_philosophers.py
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
    chopsticks = [
        {"id": f"Chopstick_{i}", "name": f"Chopstick_{i}", "total_instances": 1}
        for i in range(1, 6)
    ]

    philosophers = []
    for i in range(1, 6):
        left = f"Chopstick_{i}"
        right = f"Chopstick_{(i % 5) + 1}"
        philosophers.append(
            WorkerSpec(
                worker_id=f"philosopher_{i}",
                name=f"Philosopher {i}",
                recovery_priority=i,  # Philosopher 1 has lowest recovery priority (1)
                first_lock=left,
                second_lock=right,
                max_claim={left: 1, right: 1},
            )
        )

    return ScenarioDefinition(
        scenario_key="dining_philosophers",
        workload_name="Dining Philosophers",
        description="5 OS Philosopher processes contending for 5 Windows Named Mutex chopsticks (P1->P2->P3->P4->P5->P1)",
        resources=chopsticks,
        workers=philosophers,
        default_max_procs=8,       # 1 supervisor + 5 philosophers + margin
        default_cpu_cap=20,
        default_job_mem_mb=768,    # Aggregate committed RAM for 6 Python processes
        default_proc_mem_mb=256,
        default_timeout=120,
        default_delay=0.25,
    )


def main():
    run_deadlock_scenario(build_scenario(), Path(__file__))


if __name__ == "__main__":
    main()
