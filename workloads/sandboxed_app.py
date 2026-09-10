"""
Sandboxed App — Synthetic Untrusted Application for Isolation Testing

A combined workload that consumes both CPU and memory, designed to
be run inside a Job Object sandbox to demonstrate:
  - CPU rate caps being enforced by the OS kernel
  - Memory hard limits preventing excessive allocation
  - Process termination when limits are exceeded
  - Controlled execution within isolation boundaries
"""

import sys
import time
import threading
import math
import os


def cpu_work():
    """Continuous CPU computation."""
    x = 1.0
    while True:
        x = math.sin(x) + math.cos(x * 1.1) + math.tan(x * 0.5 + 0.01)


def memory_work(target_mb, chunk_mb=5, interval=1.0):
    """Gradually allocate memory up to target."""
    blocks = []
    allocated = 0
    while allocated < target_mb:
        try:
            chunk = bytearray(chunk_mb * 1024 * 1024)
            for offset in range(0, len(chunk), 4096):
                chunk[offset] = 0xAA
            blocks.append(chunk)
            allocated += chunk_mb
            print(f"  [MEM] Allocated: {allocated} MB / {target_mb} MB")
        except MemoryError:
            print(f"  [MEM] MemoryError at {allocated} MB — sandbox limit reached!")
            break
        time.sleep(interval)

    print(f"  [MEM] Holding {allocated} MB...")
    while True:
        time.sleep(1)


def main():
    cpu_threads = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    target_mem_mb = int(sys.argv[2]) if len(sys.argv) > 2 else 200

    print(f"[SANDBOXED APP] PID: {os.getpid()}")
    print(f"[SANDBOXED APP] CPU threads: {cpu_threads}")
    print(f"[SANDBOXED APP] Memory target: {target_mem_mb} MB")
    print(f"[SANDBOXED APP] Starting workload (expects to be in a sandbox)...")

    # Start CPU threads
    for i in range(cpu_threads):
        t = threading.Thread(target=cpu_work, daemon=True, name=f"CPU-{i}")
        t.start()
        print(f"  CPU thread {i} started")

    # Start memory allocation in a thread
    mem_thread = threading.Thread(
        target=memory_work,
        args=(target_mem_mb,),
        daemon=True,
    )
    mem_thread.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[SANDBOXED APP] Interrupted. Exiting.")


if __name__ == "__main__":
    main()
