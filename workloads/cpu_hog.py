"""
CPU Hog — Synthetic CPU-Intensive Workload

Spawns multiple threads that perform continuous CPU-bound computation.
Used to demonstrate:
  - CPU hog detection by the Policy Engine
  - Priority demotion and CPU throttling via Job Objects
  - CPU scheduling contention
"""

import sys
import time
import threading
import math
import os


def cpu_work():
    """Pure CPU-bound computation: calculate primes via trial division."""
    n = 2
    while True:
        is_prime = True
        for i in range(2, int(math.sqrt(n)) + 1):
            if n % i == 0:
                is_prime = False
                break
        n += 1


def main():
    num_threads = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 0  # 0 = infinite

    print(f"[CPU HOG] PID: {os.getpid()}")
    print(f"[CPU HOG] Threads: {num_threads}")
    print(f"[CPU HOG] Duration: {'infinite' if duration == 0 else f'{duration}s'}")
    print(f"[CPU HOG] Starting intensive CPU computation...")

    threads = []
    for i in range(num_threads):
        t = threading.Thread(target=cpu_work, daemon=True, name=f"CPUWorker-{i}")
        t.start()
        threads.append(t)
        print(f"  Thread {i} started")

    try:
        if duration > 0:
            time.sleep(duration)
            print(f"[CPU HOG] Duration expired. Exiting.")
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        print("\n[CPU HOG] Interrupted. Exiting.")


if __name__ == "__main__":
    main()
