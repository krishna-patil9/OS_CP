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


import argparse


def main():
    parser = argparse.ArgumentParser(description="Memory Hog Workload")
    parser.add_argument("pos_chunk", nargs="?", type=int, default=None)
    parser.add_argument("pos_max", nargs="?", type=int, default=None)
    parser.add_argument("pos_interval", nargs="?", type=float, default=None)
    parser.add_argument("--chunk-mb", type=int, default=None)
    parser.add_argument("--max-mb", type=int, default=None)
    parser.add_argument("--interval", type=float, default=None)
    parsed, _ = parser.parse_known_args()

    chunk_size_mb = parsed.chunk_mb if parsed.chunk_mb is not None else (parsed.pos_chunk if parsed.pos_chunk is not None else 10)
    max_mb = parsed.max_mb if parsed.max_mb is not None else (parsed.pos_max if parsed.pos_max is not None else 500)
    interval = parsed.interval if parsed.interval is not None else (parsed.pos_interval if parsed.pos_interval is not None else 0.5)

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
