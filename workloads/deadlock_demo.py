"""
Real-Time Windows-Native Deadlock Demo (Backward-Compatible Circular Deadlock Entrypoint)

Preserves backward compatibility for:
  python workloads\\deadlock_demo.py
as well as CLI options (--server, --workers, --delay, --timeout, --cpu-cap,
--job-mem-mb, --proc-mem-mb, --max-procs).

Delegates to the shared Windows-native scenario runner so Job Object containment,
startup gate synchronization, real Windows Named Mutexes, and WAIT_ABANDONED
reconciliation are identical across all scenario files.
"""

import argparse
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


def build_scenario(num_workers: int = 2) -> ScenarioDefinition:
    n = max(2, min(5, int(num_workers)))
    lock_ids = ["Lock_A", "Lock_B"] if n == 2 else [f"Lock_{chr(65 + i)}" for i in range(n)]
    resources = [{"id": lid, "name": lid, "total_instances": 1} for lid in lock_ids]
    priorities = [1, 3, 2, 4, 5]

    workers = []
    for i in range(n):
        letter = chr(65 + i)
        first_lid = lock_ids[i]
        second_lid = lock_ids[(i + 1) % n]
        workers.append(
            WorkerSpec(
                worker_id=f"worker_{letter.lower()}",
                name=f"Worker {letter}",
                recovery_priority=priorities[i % len(priorities)],
                first_lock=first_lid,
                second_lock=second_lid,
                max_claim={first_lid: 1, second_lid: 1},
            )
        )

    return ScenarioDefinition(
        scenario_key="deadlock_demo",
        workload_name="Circular Deadlock",
        description=f"{n}-process circular wait using Windows Named Mutexes (backward-compatible deadlock_demo.py)",
        resources=resources,
        workers=workers,
        default_max_procs=max(4, n + 2),
        default_cpu_cap=20,
        default_job_mem_mb=512,
        default_proc_mem_mb=256,
        default_timeout=120,
        default_delay=0.35,
    )


def main():
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--workers", type=int, default=2)
    pre_args, _ = pre.parse_known_args()
    run_deadlock_scenario(build_scenario(pre_args.workers), Path(__file__))


if __name__ == "__main__":
    main()
