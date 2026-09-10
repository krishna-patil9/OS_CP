"""
Memory Hog — Synthetic Memory-Intensive Workload

Continuously allocates memory in configurable chunks to demonstrate:
  - Memory pressure detection by the Policy Engine
  - Working set trimming
  - Hard memory limits via Job Objects
  - OS behavior when process exceeds memory cap
"""

import sys
import time
import os


def main():
    chunk_size_mb = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    max_mb = int(sys.argv[2]) if len(sys.argv) > 2 else 500
    interval = float(sys.argv[3]) if len(sys.argv) > 3 else 0.5

    print(f"[MEMORY HOG] PID: {os.getpid()}")
    print(f"[MEMORY HOG] Chunk size: {chunk_size_mb} MB")
    print(f"[MEMORY HOG] Max allocation: {max_mb} MB")
    print(f"[MEMORY HOG] Interval: {interval}s between allocations")
    print(f"[MEMORY HOG] Starting memory allocation...")

    allocated_mb = 0
    blocks = []  # Keep references so GC doesn't free them

    try:
        while allocated_mb < max_mb:
            # Allocate chunk_size_mb of memory
            chunk = bytearray(chunk_size_mb * 1024 * 1024)
            # Touch every page to ensure physical allocation
            for offset in range(0, len(chunk), 4096):
                chunk[offset] = 0xFF
            blocks.append(chunk)
            allocated_mb += chunk_size_mb
            print(f"  Allocated: {allocated_mb} MB / {max_mb} MB")
            time.sleep(interval)

        print(f"[MEMORY HOG] Reached target {max_mb} MB. Holding memory...")

        while True:
            time.sleep(1)

    except MemoryError:
        print(f"\n[MEMORY HOG] MemoryError at {allocated_mb} MB — OS limit reached!")
        print(f"[MEMORY HOG] This demonstrates Job Object hard memory caps.")
        time.sleep(5)
    except KeyboardInterrupt:
        print(f"\n[MEMORY HOG] Interrupted at {allocated_mb} MB. Exiting.")


if __name__ == "__main__":
    main()
