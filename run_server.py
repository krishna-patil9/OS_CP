#!/usr/bin/env python3
"""
Single-Script Safe Launcher — Smart Resource Manager & Application Isolation System
OS Course Project

Starts the FastAPI server with:
  - Port collision and duplicate instance detection
  - Normal Mode activated by default (Safe, lightweight, stress workloads locked)
  - 2.0s telemetry interval to prevent CPU fan noise & laptop heating
  - Graceful Ctrl+C shutdown terminating all child processes, threads, and Job Objects
"""

import sys
import os
import signal
import socket
import logging
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

def is_port_in_use(port: int = 8000, host: str = "127.0.0.1") -> bool:
    """Check if the designated port is already occupied by another instance."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0

def find_pid_on_port(port: int = 8000):
    """Attempt to identify which PID is listening on the target port."""
    try:
        import psutil
        for conn in psutil.net_connections(kind='inet'):
            if conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                return conn.pid
    except Exception:
        pass
    return None

def print_banner(host: str = "127.0.0.1", port: int = 8000):
    banner = f"""
================================================================================
   SMART RESOURCE MANAGER & APPLICATION ISOLATION SYSTEM (OPTIMIZED)
   Operating Systems Course Project
================================================================================
   [x] Execution Mode         : NORMAL MODE (Default: Safe, Lightweight)
   [x] Telemetry Interval     : 2.0s (Paced, low CPU, metadata-cached)
   [x] Native Host OS Control : Windows NT Kernel Job Objects & Process API
   [x] Workload Protection    : Stress workloads locked; manual activation required
   [x] Deadlock & Concurrency : Wait-For Graph Cycle Detection & Banker's Algorithm
   [x] Theoretical Simulator  : MLFQ Scheduler & Page Replacement (FIFO/LRU/Clock)
================================================================================
   Dashboard URL : http://{host}:{port}
   API Docs      : http://{host}:{port}/docs
   Shutdown      : Press Ctrl+C once for graceful teardown
================================================================================
    """
    print(banner)

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Smart Resource Manager Launcher")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host interface (default: 127.0.0.1)")
    args = parser.parse_args()

    port = args.port
    host = args.host

    print_banner(host, port)

    # 1. Check for duplicate server instances
    if is_port_in_use(port, host):
        owner_pid = find_pid_on_port(port)
        pid_info = f" (PID: {owner_pid})" if owner_pid else ""
        print(f"[ERROR] Port {port} is already in use by another process{pid_info}!")
        print(f"        Please terminate the existing process or use another port.")
        sys.exit(1)

    try:
        import uvicorn
    except ImportError:
        print("[ERROR] uvicorn is not installed. Please run: pip install uvicorn")
        sys.exit(1)

    print(f"[*] Starting FastAPI Server on http://{host}:{port}...")
    print(f"[*] Telemetry updates configured to ~2.0s for lightweight laptop operation.")

    config = uvicorn.Config(
        "backend.main:app",
        host=host,
        port=port,
        log_level="info",
        reload=False,
    )
    server = uvicorn.Server(config)

    # Clean signal handling
    def handle_exit(sig, frame):
        print("\n[*] Graceful shutdown signal received. Releasing resources...")
        server.should_exit = True

    try:
        signal.signal(signal.SIGINT, handle_exit)
        signal.signal(signal.SIGTERM, handle_exit)
    except Exception:
        pass

    try:
        server.run()
    except KeyboardInterrupt:
        print("\n[*] KeyboardInterrupt detected. Shutting down...")
    finally:
        # Extra safety fallback to ensure all workloads & sandboxes are cleaned up
        try:
            from backend.main import spawned_workloads, kill_process_tree, isolation, monitor, policy, live_deadlock
            live_deadlock.cleanup_all()
            for wid, proc in list(spawned_workloads.items()):
                kill_process_tree(proc.pid)
            isolation.cleanup_all_sandboxes()
            policy.stop()
            monitor.stop()
        except Exception:
            pass
        print("[x] Server stopped. All background tasks, child processes, and Job Objects cleanly released.\n")

if __name__ == "__main__":
    main()
