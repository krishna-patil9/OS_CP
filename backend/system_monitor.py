"""
System Monitor — Real-time Host Telemetry Collector

Runs a dedicated daemon thread collecting per-process and system-wide telemetry
using psutil and Win32 APIs. Data is stored in thread-safe structures for
consumption by the Policy Engine and WebSocket telemetry stream.

OS Concepts Demonstrated:
  - Concurrent monitoring with daemon threads
  - Thread-safe data structures (Reader-Writer pattern via RLock)
  - Process lifecycle observation (creation, state, termination)
  - Parent-child process relationships (PPID tracking)
  - Per-process resource accounting (CPU, memory, threads, priority)
"""

import threading
import time
import logging
from typing import Dict, List, Optional, Deque
from collections import deque
from dataclasses import dataclass, field

import psutil

logger = logging.getLogger("system_monitor")

# Number of historical snapshots to retain for trend analysis
HISTORY_LENGTH = 120  # ~60 seconds at 500ms interval


@dataclass
class ProcessSnapshot:
    """Point-in-time snapshot of a single process."""
    pid: int
    name: str
    ppid: int
    status: str
    cpu_percent: float
    memory_mb: float          # Working set (RSS) in MB
    memory_percent: float
    num_threads: int
    priority: str
    create_time: float
    username: Optional[str] = None
    cmdline: str = ""
    io_read_bytes: int = 0
    io_write_bytes: int = 0


@dataclass
class SystemSnapshot:
    """Point-in-time snapshot of overall system state."""
    timestamp: float
    cpu_percent: float            # Overall CPU usage
    cpu_per_core: List[float]     # Per-core CPU usage
    cpu_count: int
    memory_total_mb: float
    memory_used_mb: float
    memory_percent: float
    memory_available_mb: float
    swap_total_mb: float
    swap_used_mb: float
    swap_percent: float
    process_count: int
    thread_count: int
    processes: Dict[int, ProcessSnapshot] = field(default_factory=dict)


class SystemMonitor:
    """
    Concurrent system telemetry collector.

    Spawns a background daemon thread that periodically samples host
    system and per-process metrics. All access to collected data is
    protected by a threading.RLock (reader-writer critical section).
    """

    def __init__(self, interval: float = 0.5):
        self._interval = interval
        self._lock = threading.RLock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Current snapshot (protected by _lock)
        self._current: Optional[SystemSnapshot] = None

        # Historical system-level metrics for trend analysis
        self._cpu_history: Deque[float] = deque(maxlen=HISTORY_LENGTH)
        self._mem_history: Deque[float] = deque(maxlen=HISTORY_LENGTH)
        self._timestamp_history: Deque[float] = deque(maxlen=HISTORY_LENGTH)

        # Process event tracking
        self._known_pids: set = set()
        self._events: Deque[dict] = deque(maxlen=200)

        # Pre-warm psutil CPU measurement
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self):
        """Start the background monitoring daemon thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="SystemMonitor-Daemon",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"System monitor started (interval={self._interval}s)")

    def stop(self):
        """Signal the monitoring thread to stop."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("System monitor stopped")

    # ── Data Access (thread-safe reads) ───────────────────────────────────

    def get_snapshot(self) -> Optional[dict]:
        """Return the current system snapshot as a serializable dict."""
        with self._lock:
            if not self._current:
                return None
            snap = self._current
            return {
                "timestamp": snap.timestamp,
                "cpu_percent": snap.cpu_percent,
                "cpu_per_core": snap.cpu_per_core,
                "cpu_count": snap.cpu_count,
                "memory_total_mb": round(snap.memory_total_mb, 1),
                "memory_used_mb": round(snap.memory_used_mb, 1),
                "memory_percent": snap.memory_percent,
                "memory_available_mb": round(snap.memory_available_mb, 1),
                "swap_total_mb": round(snap.swap_total_mb, 1),
                "swap_used_mb": round(snap.swap_used_mb, 1),
                "swap_percent": snap.swap_percent,
                "process_count": snap.process_count,
                "thread_count": snap.thread_count,
            }

    def get_processes(self) -> List[dict]:
        """Return the current process list as serializable dicts."""
        with self._lock:
            if not self._current:
                return []
            result = []
            for ps in self._current.processes.values():
                result.append({
                    "pid": ps.pid,
                    "name": ps.name,
                    "ppid": ps.ppid,
                    "status": ps.status,
                    "cpu_percent": round(ps.cpu_percent, 1),
                    "memory_mb": round(ps.memory_mb, 2),
                    "memory_percent": round(ps.memory_percent, 1),
                    "num_threads": ps.num_threads,
                    "priority": ps.priority,
                    "create_time": ps.create_time,
                    "username": ps.username,
                    "cmdline": ps.cmdline,
                    "io_read_bytes": ps.io_read_bytes,
                    "io_write_bytes": ps.io_write_bytes,
                })
            return result

    def get_process(self, pid: int) -> Optional[dict]:
        """Return a single process snapshot."""
        with self._lock:
            if not self._current:
                return None
            ps = self._current.processes.get(pid)
            if not ps:
                return None
            return {
                "pid": ps.pid,
                "name": ps.name,
                "ppid": ps.ppid,
                "status": ps.status,
                "cpu_percent": round(ps.cpu_percent, 1),
                "memory_mb": round(ps.memory_mb, 2),
                "memory_percent": round(ps.memory_percent, 1),
                "num_threads": ps.num_threads,
                "priority": ps.priority,
                "create_time": ps.create_time,
                "username": ps.username,
                "cmdline": ps.cmdline,
            }

    def get_history(self) -> dict:
        """Return CPU and memory usage history for chart rendering."""
        with self._lock:
            return {
                "timestamps": list(self._timestamp_history),
                "cpu": list(self._cpu_history),
                "memory": list(self._mem_history),
            }

    def get_events(self, limit: int = 50) -> List[dict]:
        """Return recent process lifecycle events."""
        with self._lock:
            return list(self._events)[-limit:]

    def get_top_processes(self, sort_by: str = "cpu", limit: int = 10) -> List[dict]:
        """Return the top N processes sorted by cpu or memory."""
        procs = self.get_processes()
        key = "cpu_percent" if sort_by == "cpu" else "memory_mb"
        procs.sort(key=lambda p: p.get(key, 0), reverse=True)
        return procs[:limit]

    # ── Internal Monitor Loop ─────────────────────────────────────────────

    def _monitor_loop(self):
        """Background thread main loop: collect telemetry at fixed interval."""
        while self._running:
            try:
                self._collect_snapshot()
            except Exception as e:
                logger.error(f"Monitor collection error: {e}")
            time.sleep(self._interval)

    def _collect_snapshot(self):
        """Collect a full system + process snapshot."""
        now = time.time()

        # System-wide metrics
        cpu_pct = psutil.cpu_percent(interval=None)
        cpu_per_core = psutil.cpu_percent(interval=None, percpu=True)
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()

        # Per-process metrics
        processes: Dict[int, ProcessSnapshot] = {}
        total_threads = 0
        current_pids = set()

        PRIORITY_MAP = {
            psutil.IDLE_PRIORITY_CLASS: "idle",
            psutil.BELOW_NORMAL_PRIORITY_CLASS: "below_normal",
            psutil.NORMAL_PRIORITY_CLASS: "normal",
            psutil.ABOVE_NORMAL_PRIORITY_CLASS: "above_normal",
            psutil.HIGH_PRIORITY_CLASS: "high",
            psutil.REALTIME_PRIORITY_CLASS: "realtime",
        }

        for proc in psutil.process_iter([
            "pid", "name", "ppid", "status", "cpu_percent",
            "memory_info", "memory_percent", "num_threads",
            "nice", "create_time", "username", "cmdline",
            "io_counters",
        ]):
            try:
                info = proc.info
                pid = info["pid"]
                current_pids.add(pid)

                # Map Windows priority class to readable name
                nice_val = info.get("nice")
                priority_name = PRIORITY_MAP.get(nice_val, f"unknown({nice_val})")

                mem_info = info.get("memory_info")
                rss_mb = (mem_info.rss / (1024 * 1024)) if mem_info else 0

                io = info.get("io_counters")
                io_read = io.read_bytes if io else 0
                io_write = io.write_bytes if io else 0

                cmdline_parts = info.get("cmdline") or []
                cmdline_str = " ".join(cmdline_parts)[:200] if cmdline_parts else ""

                ps = ProcessSnapshot(
                    pid=pid,
                    name=info.get("name", ""),
                    ppid=info.get("ppid", 0),
                    status=info.get("status", ""),
                    cpu_percent=info.get("cpu_percent", 0.0) or 0.0,
                    memory_mb=rss_mb,
                    memory_percent=info.get("memory_percent", 0.0) or 0.0,
                    num_threads=info.get("num_threads", 0) or 0,
                    priority=priority_name,
                    create_time=info.get("create_time", 0.0) or 0.0,
                    username=info.get("username"),
                    cmdline=cmdline_str,
                    io_read_bytes=io_read,
                    io_write_bytes=io_write,
                )
                processes[pid] = ps
                total_threads += ps.num_threads
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        # Detect process lifecycle events
        new_pids = current_pids - self._known_pids
        gone_pids = self._known_pids - current_pids

        for pid in new_pids:
            ps = processes.get(pid)
            if ps:
                self._events.append({
                    "timestamp": now,
                    "event": "PROCESS_CREATED",
                    "pid": pid,
                    "name": ps.name,
                    "ppid": ps.ppid,
                })

        for pid in gone_pids:
            self._events.append({
                "timestamp": now,
                "event": "PROCESS_TERMINATED",
                "pid": pid,
                "name": "",
            })

        # Build snapshot
        snapshot = SystemSnapshot(
            timestamp=now,
            cpu_percent=cpu_pct,
            cpu_per_core=cpu_per_core,
            cpu_count=psutil.cpu_count(),
            memory_total_mb=mem.total / (1024 * 1024),
            memory_used_mb=mem.used / (1024 * 1024),
            memory_percent=mem.percent,
            memory_available_mb=mem.available / (1024 * 1024),
            swap_total_mb=swap.total / (1024 * 1024),
            swap_used_mb=swap.used / (1024 * 1024),
            swap_percent=swap.percent,
            process_count=len(processes),
            thread_count=total_threads,
            processes=processes,
        )

        # Write under lock (critical section)
        with self._lock:
            self._current = snapshot
            self._cpu_history.append(cpu_pct)
            self._mem_history.append(mem.percent)
            self._timestamp_history.append(now)
            self._known_pids = current_pids
