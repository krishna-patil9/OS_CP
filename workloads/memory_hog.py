"""
Memory Hog — Synthetic Memory Workload (Safe & Conservative)

Demonstrates OS memory management with strict safety safeguards:
  - Conservative default limit (40 MB, hard safety ceiling 100 MB)
  - Chunk allocation with safe pause intervals
  - Auto-release timeout (holds for max 15s after reaching target, never infinite)
  - Immediate signal handling for clean teardown
"""

import sys
import time
import os
import signal
import threading
import argparse

stop_event = threading.Event()


def signal_handler(signum, frame):
    """Handle termination signals immediately."""
    stop_event.set()


def main():
    parser = argparse.ArgumentParser(description="Safe Memory Hog Workload")
    parser.add_argument("pos_chunk", nargs="?", type=int, default=None)
    parser.add_argument("pos_max", nargs="?", type=int, default=None)
    parser.add_argument("pos_interval", nargs="?", type=float, default=None)
    parser.add_argument("--chunk-mb", type=int, default=None)
    parser.add_argument("--max-mb", type=int, default=None)
    parser.add_argument("--interval", type=float, default=None)
    parser.add_argument("--hold-duration", type=int, default=15, help="Seconds to hold memory before exiting")
    parsed, _ = parser.parse_known_args()

    # Enforce safe memory allocation limits (default 40 MB, max ceiling 100 MB)
    raw_max = parsed.max_mb if parsed.max_mb is not None else (
        parsed.pos_max if parsed.pos_max is not None else 40
    )
    max_mb = max(10, min(100, raw_max))

    # Chunk size (default 5 MB, max 20 MB)
    raw_chunk = parsed.chunk_mb if parsed.chunk_mb is not None else (
        parsed.pos_chunk if parsed.pos_chunk is not None else 5
    )
    chunk_size_mb = max(1, min(20, raw_chunk))

    # Interval between chunks
    raw_interval = parsed.interval if parsed.interval is not None else (
        parsed.pos_interval if parsed.pos_interval is not None else 0.5
    )
    interval = max(0.2, min(5.0, raw_interval))

    hold_duration = max(5, min(30, parsed.hold_duration))

    # Register signals for immediate cleanup
    try:
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
    except Exception:
        pass

    print(f"[SAFE MEMORY HOG] PID: {os.getpid()}")
    print(f"[SAFE MEMORY HOG] Chunk size: {chunk_size_mb} MB")
    print(f"[SAFE MEMORY HOG] Target max: {max_mb} MB (safe ceiling: 100 MB)")
    print(f"[SAFE MEMORY HOG] Hold duration: {hold_duration}s")
    print(f"[SAFE MEMORY HOG] Starting gradual allocation...")

    allocated_mb = 0
    blocks = []

    try:
        while allocated_mb < max_mb and not stop_event.is_set():
            alloc_now = min(chunk_size_mb, max_mb - allocated_mb)
            try:
                chunk = bytearray(alloc_now * 1024 * 1024)
                # Touch pages sparsely (every 64KB) to commit physical pages safely
                for offset in range(0, len(chunk), 65536):
                    chunk[offset] = 0xAA
                blocks.append(chunk)
                allocated_mb += alloc_now
                print(f"  Allocated: {allocated_mb} MB / {max_mb} MB")
            except MemoryError:
                print(f"\n[SAFE MEMORY HOG] MemoryError at {allocated_mb} MB — Job Object limit hit!")
                break

            if stop_event.wait(interval):
                break

        if not stop_event.is_set():
            print(f"[SAFE MEMORY HOG] Reached target {allocated_mb} MB. Holding for {hold_duration}s...")
            start_hold = time.time()
            while not stop_event.is_set():
                if time.time() - start_hold >= hold_duration:
                    print(f"[SAFE MEMORY HOG] Hold duration completed.")
                    break
                stop_event.wait(0.5)

    except KeyboardInterrupt:
        print(f"\n[SAFE MEMORY HOG] Interrupted via KeyboardInterrupt.")
    finally:
        print(f"[SAFE MEMORY HOG] Releasing {allocated_mb} MB memory...")
        blocks.clear()
        print(f"[SAFE MEMORY HOG] Cleanly exited.")


if __name__ == "__main__":
    main()
