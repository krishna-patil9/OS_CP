"""
Deadlock Demo — Controlled Mutex Lock Order Inversion

Demonstrates deadlock principles with guaranteed self-resolution:
  - 2-3 worker threads acquiring mutexes in opposite order
  - Bounded lock acquisition timeouts so threads never freeze indefinitely
  - Overall watchdog timer ensures process exits safely after demonstration
"""

import sys
import time
import threading
import os
import signal
import argparse

stop_event = threading.Event()


def signal_handler(signum, frame):
    stop_event.set()


def main():
    parser = argparse.ArgumentParser(description="Controlled Deadlock Demo Workload")
    parser.add_argument("pos_locks", nargs="?", type=int, default=None)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--timeout", type=int, default=15, help="Total execution timeout in seconds")
    parsed, _ = parser.parse_known_args()

    num_locks = parsed.workers if parsed.workers is not None else (
        parsed.pos_locks if parsed.pos_locks is not None else 2
    )
    num_locks = max(2, min(4, num_locks))
    max_duration = max(5, min(30, parsed.timeout))

    try:
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
    except Exception:
        pass

    print(f"[SAFE DEADLOCK DEMO] PID: {os.getpid()}")
    print(f"[SAFE DEADLOCK DEMO] Creating {num_locks} mutexes for circular dependency...")
    print(f"[SAFE DEADLOCK DEMO] Maximum execution timeout: {max_duration}s")

    locks = [threading.Lock() for _ in range(num_locks)]
    lock_names = [f"Mutex-{i}" for i in range(num_locks)]
    deadlocked = threading.Event()

    def worker(thread_id, first_idx, second_idx):
        first_lock = locks[first_idx]
        second_lock = locks[second_idx]
        first_name = lock_names[first_idx]
        second_name = lock_names[second_idx]

        print(f"  Worker-{thread_id}: Requesting {first_name}...")
        first_lock.acquire()
        print(f"  Worker-{thread_id}: Acquired {first_name}. Holding for {parsed.delay}s...")
        time.sleep(parsed.delay)

        print(f"  Worker-{thread_id}: Requesting {second_name} (creates circular wait)...")
        # Use a 5-second timeout on second lock so threads can break free safely
        acquired = second_lock.acquire(timeout=5.0)

        if acquired:
            print(f"  Worker-{thread_id}: Acquired {second_name} (no deadlock)")
            second_lock.release()
        else:
            print(f"  Worker-{thread_id}: TIMEOUT waiting for {second_name} — CIRCULAR WAIT DEADLOCK!")
            deadlocked.set()

        first_lock.release()

    threads = []
    for i in range(num_locks):
        first = i
        second = (i + 1) % num_locks
        t = threading.Thread(
            target=worker,
            args=(i, first, second),
            daemon=True,
            name=f"DeadlockWorker-{i}",
        )
        threads.append(t)

    print(f"[SAFE DEADLOCK DEMO] Starting {num_locks} threads with circular lock order...")
    for t in threads:
        t.start()

    # Wait for completion or watchdog timeout
    start_time = time.time()
    for t in threads:
        remaining = max(0.1, max_duration - (time.time() - start_time))
        t.join(timeout=remaining)
        if stop_event.is_set():
            break

    if deadlocked.is_set():
        print(f"\n[SAFE DEADLOCK DEMO] *** DEADLOCK DETECTED & DEMONSTRATED ***")
        print(f"[SAFE DEADLOCK DEMO] Circular wait occurred as planned.")
    else:
        print(f"\n[SAFE DEADLOCK DEMO] Mutex sequence finished.")

    print(f"[SAFE DEADLOCK DEMO] Cleaning up and exiting cleanly.")


if __name__ == "__main__":
    main()
