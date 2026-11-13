"""
CPU Hog — Synthetic CPU-Intensive Workload (Safe & Controlled)

Spawns controlled worker thread(s) that perform CPU computation with:
  - Strict thread limits (default 1 thread, capped at 2 max)
  - Enforced maximum duration (default 15s, hard capped at 60s)
  - Threading.Event and signal handlers for immediate termination
  - Micro-pause duty cycling to prevent laptop overheating
"""

import sys
import time
import threading
import math
import os
import signal
import argparse

stop_event = threading.Event()


def signal_handler(signum, frame):
    """Handle termination signals immediately."""
    stop_event.set()


def cpu_work(worker_id: int, intensity: int = 50):
    """
    CPU-bound computation: calculate primes with duty-cycle pauses
    to prevent thermal throttling and hardware stress.
    """
    n = 2
    work_batch = max(50, intensity * 20)
    count = 0

    while not stop_event.is_set():
        limit = int(math.isqrt(n)) + 1
        for i in range(2, limit):
            if n % i == 0:
                break
        n += 1
        count += 1

        if count >= work_batch:
            count = 0
            # Micro-sleep allows scheduler yields and cools the core
            time.sleep(0.005)


def main():
    parser = argparse.ArgumentParser(description="Safe CPU Hog Workload")
    parser.add_argument("pos_threads", nargs="?", type=int, default=None)
    parser.add_argument("pos_duration", nargs="?", type=int, default=None)
    parser.add_argument("--threads", type=int, default=None)
    parser.add_argument("--duration", type=int, default=None)
    parser.add_argument("--intensity", type=int, default=50, help="CPU intensity percentage (10-100)")
    parsed, _ = parser.parse_known_args()

    # Enforce safe thread bounds (default 1, max 2)
    raw_threads = parsed.threads if parsed.threads is not None else (
        parsed.pos_threads if parsed.pos_threads is not None else 1
    )
    num_threads = max(1, min(2, raw_threads))

    # Enforce safe duration (default 15s, max 60s, never infinite)
    raw_duration = parsed.duration if parsed.duration is not None else (
        parsed.pos_duration if parsed.pos_duration is not None else 15
    )
    if raw_duration <= 0 or raw_duration > 60:
        duration = 15
    else:
        duration = raw_duration

    intensity = max(10, min(100, parsed.intensity))

    # Register signals for immediate cleanup
    try:
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
    except Exception:
        pass

    print(f"[SAFE CPU HOG] PID: {os.getpid()}")
    print(f"[SAFE CPU HOG] Worker Threads: {num_threads} (safe cap: 2)")
    print(f"[SAFE CPU HOG] Duration: {duration}s (hard timeout)")
    print(f"[SAFE CPU HOG] Intensity: {intensity}%")
    print(f"[SAFE CPU HOG] Starting controlled computation...")

    threads = []
    for i in range(num_threads):
        t = threading.Thread(
            target=cpu_work,
            args=(i, intensity),
            daemon=True,
            name=f"SafeCPUWorker-{i}",
        )
        t.start()
        threads.append(t)
        print(f"  Worker {i} active")

    # Run for specified duration or until stop_event is set
    start_time = time.time()
    try:
        while not stop_event.is_set():
            elapsed = time.time() - start_time
            if elapsed >= duration:
                print(f"[SAFE CPU HOG] Duration {duration}s reached. Stopping cleanly.")
                break
            stop_event.wait(0.5)
    except KeyboardInterrupt:
        print("\n[SAFE CPU HOG] Interrupted via KeyboardInterrupt.")
    finally:
        stop_event.set()
        for t in threads:
            t.join(timeout=1.0)
        print(f"[SAFE CPU HOG] All worker threads terminated cleanly. Exiting.")


if __name__ == "__main__":
    main()
