#!/usr/bin/env python3
"""
Single-Script Launcher — Smart Resource Manager & Application Isolation System
OS Course Project

Starts the FastAPI server, initializes the host telemetry collector,
and launches the intelligent policy engine.
"""

import sys
import os
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

def print_banner():
    banner = """
================================================================================
   SMART RESOURCE MANAGER & APPLICATION ISOLATION SYSTEM
   Operating Systems Course Project
================================================================================
   [x] Native Host OS Control  : Windows NT Kernel Job Objects & Process API
   [x] Real Hard Enforcement  : CPU Rate Caps, Memory Quotas, Fork-Bomb Guard
   [x] Intelligent Policy      : CPU Hog Mitigation, Memory Thrashing Guard,
                                 Starvation Dynamic Boost, Interactive vs Bg
   [x] Deadlock & Concurrency  : Wait-For Graph Cycle Detection (Tarjan's DFS)
                                 & Dijkstra's Banker's Algorithm
   [x] OS Theoretical Sim      : Multi-Level Feedback Queue (MLFQ) Scheduler
                                 & Page Replacement (FIFO / LRU / Clock)
   [x] Web Dashboard           : Glassmorphism Dark Mode UI (WebSocket 500ms)
================================================================================
   Dashboard URL: http://127.0.0.1:8000
   API Docs     : http://127.0.0.1:8000/docs
================================================================================
    """
    print(banner)

def main():
    print_banner()

    try:
        import uvicorn
    except ImportError:
        print("[ERROR] uvicorn is not installed. Please run: pip install uvicorn")
        sys.exit(1)

    print("[*] Starting FastAPI Backend & Real-Time Telemetry...")
    uvicorn.run(
        "backend.main:app",
        host="127.0.0.1",
        port=8000,
        log_level="info",
        reload=False
    )

if __name__ == "__main__":
    main()
