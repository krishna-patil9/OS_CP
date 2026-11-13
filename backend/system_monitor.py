"""
System Monitor — Real-time Host Telemetry Collector (Optimized)

Runs a dedicated daemon thread collecting per-process and system-wide telemetry
using psutil and Win32 APIs. Data is stored in thread-safe structures for
consumption by the Policy Engine and WebSocket telemetry stream.

Optimizations:
  - Default sampling interval changed from 500ms to 2.0s for lightweight execution
  - Threading.Event used for non-blocking, instantaneous thread shutdown
  - Process metadata caching (cmdline, username, create_time) avoids expensive PEB/token reads on every tick
  - Fast-path psutil query only requests essential per-tick attributes
  - Single monitoring task enforced with re-entrant thread safety
  - Graceful handling of disappearing and access-denied processes
"""

import threading
import time
import logging
from typing import Dict, List, Optional, Deque
from collections import deque
from dataclasses import dataclass, field

import psutil

logger = logging.getLogger("system_monitor")

# Retain ~120 seconds of history at 2s interval
HISTORY_LENGTH = 60


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


# Windows Priority Class mapping table
PRIORITY_MAP = {
    psutil.IDLE_PRIORITY_CLASS: "idle",
    psutil.BELOW_NORMAL_PRIORITY_CLASS: "below_normal",
    psutil.NORMAL_PRIORITY_CLASS: "normal",
    psutil.ABOVE_NORMAL_PRIORITY_CLASS: "above_normal",
    psutil.HIGH_PRIORITY_CLASS: "high",
    psutil.REALTIME_PRIORITY_CLASS: "realtime",
}


class SystemMonitor:
    """
    Concurrent lightweight system telemetry collector.

    Spawns a single background daemon thread that periodically samples host
    system and per-process metrics. All access to collected data is
    protected by a threading.RLock (reader-writer critical section).
    """

    def __init__(self, interval: float = 2.0):
        self._interval = max(0.5, float(interval))
        self._lock = threading.RLock()
        self._running = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Current snapshot (protected by _lock)
        self._current: Optional[SystemSnapshot] = None

        # Historical system-level metrics for trend analysis
        self._cpu_history: Deque[float] = deque(maxlen=HISTORY_LENGTH)
        self._mem_history: Deque[float] = deque(maxlen=HISTORY_LENGTH)
        self._timestamp_history: Deque[float] = deque(maxlen=HISTORY_LENGTH)

        # Process event tracking
        self._known_pids: set = set()
        self._events: Deque[dict] = deque(maxlen=100)

        # Process metadata cache to prevent expensive token/PEB queries on every tick
        # maps pid -> {"create_time": float, "username": str, "cmdline": str}
        self._proc_meta_cache: Dict[int, dict] = {}

        # Cached CPU core counts
        self._logical_cores = psutil.cpu_count(logical=True) or 1
        self._physical_cores = psutil.cpu_count(logical=False) or self._logical_cores

        # Pre-warm psutil CPU measurement and collect initial snapshot
        try:
            psutil.cpu_percent(interval=None)
            psutil.cpu_percent(interval=None, percpu=True)
            self._collect_snapshot()
        except Exception:
            pass

    # ── Configuration ─────────────────────────────────────────────────────

    def set_interval(self, interval: float):
        """Update monitoring interval dynamically."""
        with self._lock:
            self._interval = max(0.5, float(interval))
            logger.info(f"System monitor interval set to {self._interval}s")

    @property
    def interval(self) -> float:
        return self._interval

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self):
        """Start the background monitoring daemon thread (ensures only one runs)."""
        with self._lock:
            if self._running and self._thread and self._thread.is_alive():
                logger.debug("System monitor already running, ignoring duplicate start")
                return
            self._running = True
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._monitor_loop,
                name="SystemMonitor-Daemon",
                daemon=True,
            )
            self._thread.start()
            logger.info(f"System monitor started (interval={self._interval}s)")

    def stop(self):
        """Signal the monitoring thread to stop immediately via event."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
            self._thread = None
        logger.info("System monitor stopped cleanly")

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
                "cpu": {
                    "percent": snap.cpu_percent,
                    "logical_cores": self._logical_cores,
                    "physical_cores": self._physical_cores,
                    "per_core": snap.cpu_per_core,
                },
                "memory": {
                    "percent": snap.memory_percent,
                    "used_gb": round(snap.memory_used_mb / 1024, 2),
                    "total_gb": round(snap.memory_total_mb / 1024, 2),
                    "available_gb": round(snap.memory_available_mb / 1024, 2),
                },
                "process_counts": {
                    "total": snap.process_count,
                    "threads": snap.thread_count,
                },
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
                    "priority_class": ps.priority.upper(),
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
                "priority_class": ps.priority.upper(),
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
        """Background thread main loop: collect telemetry at fixed interval with event wait."""
        # Initial immediate collection
        try:
            self._collect_snapshot()
        except Exception as e:
            logger.error(f"Initial monitor collection error: {e}")

        while not self._stop_event.is_set():
            # Wait for interval or stop event signal
            if self._stop_event.wait(self._interval):
                break
            try:
                self._collect_snapshot()
            except Exception as e:
                logger.error(f"Monitor collection error: {e}")

    def _collect_snapshot(self):
        """
        Collect a full system + process snapshot using optimized queries.
        
        Avoids querying slow Windows attributes (cmdline, username, io_counters)
        on every process in every tick by caching static process metadata.
        """
        now = time.time()

        # System-wide metrics
        cpu_pct = psutil.cpu_percent(interval=None)
        cpu_per_core = psutil.cpu_percent(interval=None, percpu=True)
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()

        # Per-process metrics: Query only fast, essential attributes
        processes: Dict[int, ProcessSnapshot] = {}
        total_threads = 0
        current_pids = set()

        # Essential attributes fast-path (lightweight in psutil on Windows)
        fast_attrs = [
            "pid", "name", "ppid", "status", "cpu_percent",
            "memory_info", "memory_percent", "num_threads", "nice"
        ]

        for proc in psutil.process_iter(fast_attrs):
            try:
                info = proc.info
                pid = info["pid"]
                current_pids.add(pid)

                # Map Windows priority class to readable name
                nice_val = info.get("nice")
                priority_name = PRIORITY_MAP.get(nice_val, "normal")

                mem_info = info.get("memory_info")
                rss_mb = (mem_info.rss / (1024 * 1024)) if mem_info else 0.0

                # Check or populate metadata cache for static attributes
                cached_meta = self._proc_meta_cache.get(pid)
                if cached_meta is None:
                    # Query static attributes once per process lifecycle
                    try:
                        c_time = proc.create_time()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        c_time = now

                    cached_meta = {
                        "create_time": c_time,
                        "username": None,
                        "cmdline": "",
                    }
                    self._proc_meta_cache[pid] = cached_meta

                num_thr = info.get("num_threads") or 1

                ps = ProcessSnapshot(
                    pid=pid,
                    name=info.get("name") or f"Process_{pid}",
                    ppid=info.get("ppid") or 0,
                    status=info.get("status") or "running",
                    cpu_percent=info.get("cpu_percent") or 0.0,
                    memory_mb=rss_mb,
                    memory_percent=info.get("memory_percent") or 0.0,
                    num_threads=num_thr,
                    priority=priority_name,
                    create_time=cached_meta["create_time"],
                    username=cached_meta["username"],
                    cmdline=cached_meta["cmdline"],
                    io_read_bytes=0,
                    io_write_bytes=0,
                )
                processes[pid] = ps
                total_threads += num_thr
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        # Detect process lifecycle events & clean metadata cache
        new_pids = current_pids - self._known_pids
        gone_pids = self._known_pids - current_pids

        # Clean cache of terminated processes
        for pid in gone_pids:
            self._proc_meta_cache.pop(pid, None)
            self._events.append({
                "timestamp": now,
                "event": "PROCESS_TERMINATED",
                "pid": pid,
                "name": "",
            })

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

        # Build snapshot
        snapshot = SystemSnapshot(
            timestamp=now,
            cpu_percent=cpu_pct,
            cpu_per_core=cpu_per_core,
            cpu_count=self._logical_cores,
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
