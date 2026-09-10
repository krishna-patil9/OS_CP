"""
Deadlock Demo — Synthetic Mutex Lock Order Inversion

Spawns threads that acquire locks in opposite order to create
an actual deadlock. Used to demonstrate:
  - Deadlock occurrence due to circular wait
  - Lock order inversion as a deadlock cause
  - How the system can detect deadlocked threads
"""

import sys
import time
import threading
import os


def main():
    num_locks = int(sys.argv[1]) if len(sys.argv) > 1 else 3

    print(f"[DEADLOCK DEMO] PID: {os.getpid()}")
    print(f"[DEADLOCK DEMO] Creating {num_locks} locks for circular wait...")

    locks = [threading.Lock() for _ in range(num_locks)]
    lock_names = [f"Lock-{i}" for i in range(num_locks)]
    deadlocked = threading.Event()

    def worker(thread_id, first_lock, second_lock, first_name, second_name):
        """Acquire locks in specific order to cause deadlock."""
        print(f"  Thread-{thread_id}: Acquiring {first_name}...")
        first_lock.acquire()
        print(f"  Thread-{thread_id}: Acquired {first_name}. Waiting 1s...")
        time.sleep(1)  # Give other threads time to acquire their first lock

        print(f"  Thread-{thread_id}: Trying to acquire {second_name}...")
        acquired = second_lock.acquire(timeout=10)

        if acquired:
            print(f"  Thread-{thread_id}: Acquired {second_name} (no deadlock)")
            second_lock.release()
        else:
            print(f"  Thread-{thread_id}: TIMEOUT acquiring {second_name} — DEADLOCK!")
            deadlocked.set()

        first_lock.release()

    threads = []
    for i in range(num_locks):
        first = i
        second = (i + 1) % num_locks
        t = threading.Thread(
            target=worker,
            args=(i, locks[first], locks[second],
                  lock_names[first], lock_names[second]),
            daemon=True,
        )
        threads.append(t)

    print(f"[DEADLOCK DEMO] Starting {num_locks} threads with circular lock order...")
    for t in threads:
        t.start()

    # Wait for deadlock detection or completion
    for t in threads:
        t.join(timeout=15)

    if deadlocked.is_set():
        print(f"\n[DEADLOCK DEMO] *** DEADLOCK DETECTED ***")
        print(f"[DEADLOCK DEMO] Circular wait among {num_locks} threads.")
        print(f"[DEADLOCK DEMO] Each thread holds one lock and waits for the next.")
        alive = sum(1 for t in threads if t.is_alive())
        print(f"[DEADLOCK DEMO] Threads still blocked: {alive}")
    else:
        print(f"\n[DEADLOCK DEMO] All threads completed (no deadlock occurred).")

    print(f"[DEADLOCK DEMO] Exiting.")


if __name__ == "__main__":
    main()
