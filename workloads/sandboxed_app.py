"""
Sandboxed App — Synthetic Untrusted Application for Isolation Testing (Safe Bounds)

Combined CPU & memory workload designed for testing inside a Job Object sandbox:
  - Default 1 CPU thread (max 2) with duty-cycle micro-pauses
  - Conservative memory target (35 MB, max 70 MB)
  - Hard execution duration limit (default 20s, max 45s)
  - Clean event-driven shutdown
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
    stop_event.set()


def cpu_work():
    """Paced CPU computation."""
    x = 1.0
    count = 0
    while not stop_event.is_set():
        x = math.sin(x) + math.cos(x * 1.1) + math.tan(x * 0.5 + 0.01)
        count += 1
        if count >= 1000:
            count = 0
            time.sleep(0.005)  # Duty-cycle pause


def memory_work(target_mb: int, chunk_mb: int = 5, interval: float = 0.5):
    """Gradually allocate memory up to safe target."""
    blocks = []
    allocated = 0
    while allocated < target_mb and not stop_event.is_set():
        alloc_now = min(chunk_mb, target_mb - allocated)
        try:
            chunk = bytearray(alloc_now * 1024 * 1024)
            for offset in range(0, len(chunk), 65536):
                chunk[offset] = 0xAA
            blocks.append(chunk)
            allocated += alloc_now
            print(f"  [SANDBOX MEM] Allocated: {allocated} MB / {target_mb} MB")
        except MemoryError:
            print(f"  [SANDBOX MEM] MemoryError at {allocated} MB — Job Object memory cap enforced!")
            break

        if stop_event.wait(interval):
            break

    print(f"  [SANDBOX MEM] Allocation cycle finished ({allocated} MB).")


def main():
    parser = argparse.ArgumentParser(description="Safe Sandboxed App Workload")
    parser.add_argument("pos_threads", nargs="?", type=int, default=None)
    parser.add_argument("pos_mem", nargs="?", type=int, default=None)
    parser.add_argument("--threads", type=int, default=None)
    parser.add_argument("--memory-mb", "--memory", type=int, default=None)
    parser.add_argument("--duration", type=int, default=20, help="Max run duration in seconds")
    parsed, _ = parser.parse_known_args()

    # Enforce safe thread count (default 1, max 2)
    raw_threads = parsed.threads if parsed.threads is not None else (
        parsed.pos_threads if parsed.pos_threads is not None else 1
    )
    cpu_threads = max(1, min(2, raw_threads))

    # Enforce safe memory limit (default 35 MB, max 70 MB)
    raw_mem = parsed.memory_mb if parsed.memory_mb is not None else (
        parsed.pos_mem if parsed.pos_mem is not None else 35
    )
    target_mem_mb = max(10, min(70, raw_mem))

    duration = max(5, min(45, parsed.duration))

    try:
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
    except Exception:
        pass

    print(f"[SAFE SANDBOXED APP] PID: {os.getpid()}")
    print(f"[SAFE SANDBOXED APP] CPU threads: {cpu_threads} (safe cap: 2)")
    print(f"[SAFE SANDBOXED APP] Memory target: {target_mem_mb} MB (safe cap: 70 MB)")
    print(f"[SAFE SANDBOXED APP] Maximum duration: {duration}s")
    print(f"[SAFE SANDBOXED APP] Running inside Job Object sandbox...")

    threads = []
    for i in range(cpu_threads):
        t = threading.Thread(target=cpu_work, daemon=True, name=f"SandboxCPU-{i}")
        t.start()
        threads.append(t)

    mem_thread = threading.Thread(
        target=memory_work,
        args=(target_mem_mb,),
        daemon=True,
        name="SandboxMEM",
    )
    mem_thread.start()
    threads.append(mem_thread)

    start_time = time.time()
    try:
        while not stop_event.is_set():
            if time.time() - start_time >= duration:
                print(f"[SAFE SANDBOXED APP] Duration {duration}s completed.")
                break
            stop_event.wait(0.5)
    except KeyboardInterrupt:
        print("\n[SAFE SANDBOXED APP] Interrupted.")
    finally:
        stop_event.set()
        for t in threads:
            t.join(timeout=1.0)
        print(f"[SAFE SANDBOXED APP] Exiting cleanly.")


if __name__ == "__main__":
    main()
