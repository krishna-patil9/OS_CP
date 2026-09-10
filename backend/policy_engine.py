"""
Policy Engine — Intelligent Resource Management Decision System

Implements an autonomous policy engine that monitors system state and
applies configurable resource-management rules, producing transparent
audit logs of every decision made.

OS Concepts Demonstrated:
  - CPU scheduling policies and priority management
  - Resource allocation and fairness
  - Starvation detection and aging-based priority boosting
  - Foreground vs background process classification
  - Dynamic resource throttling under pressure
  - Working set management and memory pressure mitigation
"""

import time
import threading
import logging
from typing import List, Dict, Optional, Deque, TYPE_CHECKING
from collections import deque, defaultdict
from dataclasses import dataclass, field
from enum import Enum

if TYPE_CHECKING:
    from backend.system_monitor import SystemMonitor
    from backend.isolation_engine import IsolationEngine

logger = logging.getLogger("policy_engine")


class ProcessCategory(Enum):
    INTERACTIVE = "interactive"
    BACKGROUND = "background"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class RuleID(Enum):
    CPU_HOG = "CPU_HOG_MITIGATION"
    MEMORY_PRESSURE = "MEMORY_PRESSURE_GUARD"
    STARVATION = "STARVATION_RECOVERY"
    CLASSIFICATION = "PROCESS_CLASSIFICATION"
    RESOURCE_FAIRNESS = "RESOURCE_FAIRNESS"


@dataclass
class PolicyConfig:
    """Configurable thresholds for all policy rules."""
    # CPU Hog Rule
    cpu_hog_threshold_percent: float = 70.0      # Per-process CPU threshold
    cpu_hog_duration_seconds: float = 10.0        # How long before intervention
    cpu_hog_cap_percent: int = 20                 # CPU cap to apply to hogs

    # Memory Pressure Rule
    memory_pressure_threshold: float = 85.0       # System memory % trigger
    memory_critical_threshold: float = 92.0       # Critical memory level
    memory_per_process_max_mb: float = 1024.0     # Per-process memory alert

    # Starvation Rule
    starvation_cpu_min_percent: float = 0.5       # CPU below this = starving
    starvation_duration_seconds: float = 15.0     # Starved for this long
    starvation_boost_priority: str = "above_normal"

    # General
    evaluation_interval: float = 2.0              # Seconds between evaluations
    enabled_rules: List[str] = field(default_factory=lambda: [
        "CPU_HOG_MITIGATION",
        "MEMORY_PRESSURE_GUARD",
        "STARVATION_RECOVERY",
        "PROCESS_CLASSIFICATION",
        "RESOURCE_FAIRNESS",
    ])


@dataclass
class DecisionRecord:
    """Structured record of a policy decision."""
    timestamp: float
    rule_id: str
    pid: int
    process_name: str
    action: str
    reason: str
    details: str
    severity: str = "info"  # info, warning, critical


# Well-known interactive application names (case-insensitive match)
INTERACTIVE_APPS = {
    "explorer.exe", "chrome.exe", "firefox.exe", "msedge.exe",
    "code.exe", "notepad.exe", "cmd.exe", "powershell.exe",
    "winword.exe", "excel.exe", "devenv.exe", "idea64.exe",
    "slack.exe", "discord.exe", "teams.exe", "spotify.exe",
    "vlc.exe", "mspaint.exe", "taskmgr.exe", "terminal.exe",
    "windowsterminal.exe",
}

SYSTEM_APPS = {
    "system", "registry", "smss.exe", "csrss.exe", "wininit.exe",
    "services.exe", "lsass.exe", "svchost.exe", "dwm.exe",
    "fontdrvhost.exe", "sihost.exe", "taskhostw.exe",
    "runtimebroker.exe", "searchhost.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe", "textinputhost.exe",
}


class PolicyEngine:
    """
    Autonomous resource management policy engine.

    Periodically evaluates system state, classifies processes, detects
    resource pressure, and takes management actions with full audit logging.
    """

    def __init__(
        self,
        monitor: "SystemMonitor",
        isolation: "IsolationEngine",
        config: Optional[PolicyConfig] = None,
    ):
        self._monitor = monitor
        self._isolation = isolation
        self._config = config or PolicyConfig()
        self._lock = threading.RLock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Decision history
        self._decisions: Deque[DecisionRecord] = deque(maxlen=500)

        # Tracking state for time-based rules
        self._cpu_hog_tracker: Dict[int, float] = {}       # pid -> first_seen_time
        self._starvation_tracker: Dict[int, float] = {}    # pid -> first_starved_time
        self._throttled_pids: set = set()                  # PIDs we've already acted on
        self._boosted_pids: Dict[int, float] = {}          # pid -> boost_expiry_time
        self._process_categories: Dict[int, ProcessCategory] = {}

        # Statistics
        self._stats = {
            "total_evaluations": 0,
            "cpu_hog_mitigations": 0,
            "memory_pressure_actions": 0,
            "starvation_recoveries": 0,
            "classifications": 0,
        }

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self):
        """Start the background policy evaluation loop."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._evaluation_loop,
            name="PolicyEngine-Daemon",
            daemon=True,
        )
        self._thread.start()
        logger.info("Policy engine started")

    def stop(self):
        """Stop the policy evaluation loop."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("Policy engine stopped")

    # ── Configuration ─────────────────────────────────────────────────────

    def get_config(self) -> dict:
        """Return current policy configuration as dict."""
        c = self._config
        return {
            "cpu_hog_threshold_percent": c.cpu_hog_threshold_percent,
            "cpu_hog_duration_seconds": c.cpu_hog_duration_seconds,
            "cpu_hog_cap_percent": c.cpu_hog_cap_percent,
            "memory_pressure_threshold": c.memory_pressure_threshold,
            "memory_critical_threshold": c.memory_critical_threshold,
            "memory_per_process_max_mb": c.memory_per_process_max_mb,
            "starvation_cpu_min_percent": c.starvation_cpu_min_percent,
            "starvation_duration_seconds": c.starvation_duration_seconds,
            "starvation_boost_priority": c.starvation_boost_priority,
            "evaluation_interval": c.evaluation_interval,
            "enabled_rules": c.enabled_rules,
        }

    def update_config(self, **kwargs) -> dict:
        """Update policy configuration dynamically."""
        c = self._config
        for key, value in kwargs.items():
            if hasattr(c, key):
                setattr(c, key, value)
        return self.get_config()

    # ── Decision Log Access ───────────────────────────────────────────────

    def get_decisions(self, limit: int = 50) -> List[dict]:
        """Return recent policy decisions."""
        with self._lock:
            decisions = list(self._decisions)[-limit:]
            return [
                {
                    "timestamp": d.timestamp,
                    "rule_id": d.rule_id,
                    "pid": d.pid,
                    "process_name": d.process_name,
                    "action": d.action,
                    "reason": d.reason,
                    "details": d.details,
                    "severity": d.severity,
                }
                for d in reversed(decisions)
            ]

    def get_stats(self) -> dict:
        """Return engine statistics."""
        return dict(self._stats)

    def get_process_categories(self) -> Dict[int, str]:
        """Return current process category classifications."""
        return {pid: cat.value for pid, cat in self._process_categories.items()}

    # ── Main Evaluation Loop ──────────────────────────────────────────────

    def _evaluation_loop(self):
        """Background thread: periodically evaluate all enabled rules."""
        while self._running:
            try:
                self._evaluate()
            except Exception as e:
                logger.error(f"Policy evaluation error: {e}")
            time.sleep(self._config.evaluation_interval)

    def _evaluate(self):
        """Run all enabled policy rules against current system state."""
        self._stats["total_evaluations"] += 1
        now = time.time()

        snapshot = self._monitor.get_snapshot()
        processes = self._monitor.get_processes()

        if not snapshot or not processes:
            return

        rules = self._config.enabled_rules

        # 1. Classify processes
        if "PROCESS_CLASSIFICATION" in rules:
            self._rule_classify_processes(processes)

        # 2. CPU hog detection
        if "CPU_HOG_MITIGATION" in rules:
            self._rule_cpu_hog(processes, now)

        # 3. Memory pressure
        if "MEMORY_PRESSURE_GUARD" in rules:
            self._rule_memory_pressure(snapshot, processes, now)

        # 4. Starvation recovery
        if "STARVATION_RECOVERY" in rules:
            self._rule_starvation_recovery(processes, now)

        # 5. Resource fairness
        if "RESOURCE_FAIRNESS" in rules:
            self._rule_resource_fairness(processes, now)

        # Cleanup: expire priority boosts
        self._expire_boosts(now)

        # Cleanup: remove tracking for dead processes
        active_pids = {p["pid"] for p in processes}
        self._cpu_hog_tracker = {
            pid: t for pid, t in self._cpu_hog_tracker.items() if pid in active_pids
        }
        self._starvation_tracker = {
            pid: t for pid, t in self._starvation_tracker.items() if pid in active_pids
        }
        self._throttled_pids &= active_pids

    # ── Rule: Process Classification ──────────────────────────────────────

    def _rule_classify_processes(self, processes: List[dict]):
        """Classify processes as interactive, background, or system."""
        for p in processes:
            pid = p["pid"]
            name = (p["name"] or "").lower()

            if name in SYSTEM_APPS:
                cat = ProcessCategory.SYSTEM
            elif name in INTERACTIVE_APPS:
                cat = ProcessCategory.INTERACTIVE
            elif p.get("username") and p["cpu_percent"] > 0.1:
                cat = ProcessCategory.BACKGROUND
            else:
                cat = ProcessCategory.UNKNOWN

            old = self._process_categories.get(pid)
            if old != cat:
                self._process_categories[pid] = cat

    # ── Rule: CPU Hog Mitigation ──────────────────────────────────────────

    def _rule_cpu_hog(self, processes: List[dict], now: float):
        """
        Detect processes consuming excessive CPU for sustained periods.
        Demotes priority and optionally applies Job Object CPU cap.
        """
        threshold = self._config.cpu_hog_threshold_percent
        duration = self._config.cpu_hog_duration_seconds

        for p in processes:
            pid = p["pid"]
            name = p["name"] or ""
            cpu = p["cpu_percent"]

            # Skip system processes and already-throttled PIDs
            cat = self._process_categories.get(pid, ProcessCategory.UNKNOWN)
            if cat == ProcessCategory.SYSTEM:
                continue
            if pid in self._throttled_pids:
                continue

            if cpu >= threshold:
                if pid not in self._cpu_hog_tracker:
                    self._cpu_hog_tracker[pid] = now
                elif now - self._cpu_hog_tracker[pid] >= duration:
                    # CPU hog confirmed — take action
                    self._mitigate_cpu_hog(pid, name, cpu, now)
            else:
                # CPU dropped below threshold, reset tracker
                self._cpu_hog_tracker.pop(pid, None)

    def _mitigate_cpu_hog(self, pid: int, name: str, cpu: float, now: float):
        """Apply CPU hog mitigation: demote priority."""
        try:
            # Demote to IDLE priority
            success = self._isolation.set_process_priority(pid, "idle")
            if success:
                self._throttled_pids.add(pid)
                self._stats["cpu_hog_mitigations"] += 1
                self._record_decision(DecisionRecord(
                    timestamp=now,
                    rule_id=RuleID.CPU_HOG.value,
                    pid=pid,
                    process_name=name,
                    action="PRIORITY_DEMOTED_TO_IDLE",
                    reason=f"Process consumed {cpu:.1f}% CPU for >{self._config.cpu_hog_duration_seconds}s",
                    details=f"Priority class changed to IDLE_PRIORITY_CLASS to reduce CPU contention. "
                            f"Threshold: {self._config.cpu_hog_threshold_percent}%, "
                            f"Duration: {self._config.cpu_hog_duration_seconds}s",
                    severity="warning",
                ))
        except Exception as e:
            logger.warning(f"CPU hog mitigation failed for PID {pid}: {e}")

    # ── Rule: Memory Pressure Guard ───────────────────────────────────────

    def _rule_memory_pressure(self, snapshot: dict, processes: List[dict], now: float):
        """
        Monitor system memory pressure and trim working sets of top consumers
        when system memory usage exceeds thresholds.
        """
        mem_pct = snapshot["memory_percent"]
        threshold = self._config.memory_pressure_threshold
        critical = self._config.memory_critical_threshold

        if mem_pct < threshold:
            return

        severity = "critical" if mem_pct >= critical else "warning"

        # Sort processes by memory consumption descending
        sorted_procs = sorted(processes, key=lambda p: p["memory_mb"], reverse=True)

        # Trim working sets of top memory consumers that are not system processes
        trimmed = 0
        for p in sorted_procs[:5]:
            pid = p["pid"]
            name = p["name"] or ""
            mem_mb = p["memory_mb"]
            cat = self._process_categories.get(pid, ProcessCategory.UNKNOWN)

            if cat == ProcessCategory.SYSTEM:
                continue
            if mem_mb < 50:  # Don't bother with small processes
                continue

            try:
                success = self._isolation.trim_working_set(pid)
                if success:
                    trimmed += 1
                    self._stats["memory_pressure_actions"] += 1
                    self._record_decision(DecisionRecord(
                        timestamp=now,
                        rule_id=RuleID.MEMORY_PRESSURE.value,
                        pid=pid,
                        process_name=name,
                        action="WORKING_SET_TRIMMED",
                        reason=f"System memory at {mem_pct:.1f}% (threshold: {threshold}%)",
                        details=f"Process using {mem_mb:.1f}MB. Working set trimmed to release "
                                f"physical pages. Category: {cat.value}",
                        severity=severity,
                    ))
            except Exception as e:
                logger.warning(f"Working set trim failed for PID {pid}: {e}")

            if trimmed >= 3:
                break

    # ── Rule: Starvation Recovery ─────────────────────────────────────────

    def _rule_starvation_recovery(self, processes: List[dict], now: float):
        """
        Detect processes that are starved (receiving near-zero CPU time)
        despite being active, and temporarily boost their priority.
        This simulates the OS kernel's priority aging/boosting mechanism.
        """
        min_cpu = self._config.starvation_cpu_min_percent
        duration = self._config.starvation_duration_seconds

        for p in processes:
            pid = p["pid"]
            name = p["name"] or ""
            cpu = p["cpu_percent"]
            status = p.get("status", "")
            cat = self._process_categories.get(pid, ProcessCategory.UNKNOWN)

            # Only check non-system, running processes
            if cat == ProcessCategory.SYSTEM:
                continue
            if status not in ("running", "sleeping", "disk-sleep"):
                continue
            if pid in self._boosted_pids:
                continue

            if cpu <= min_cpu and p["num_threads"] > 0:
                if pid not in self._starvation_tracker:
                    self._starvation_tracker[pid] = now
                elif now - self._starvation_tracker[pid] >= duration:
                    # Starved — boost priority
                    self._boost_starved_process(pid, name, now)
            else:
                self._starvation_tracker.pop(pid, None)

    def _boost_starved_process(self, pid: int, name: str, now: float):
        """Temporarily boost priority of a starved process (aging mechanism)."""
        try:
            boost_prio = self._config.starvation_boost_priority
            success = self._isolation.set_process_priority(pid, boost_prio)
            if success:
                # Boost expires after 10 seconds
                self._boosted_pids[pid] = now + 10.0
                self._starvation_tracker.pop(pid, None)
                self._stats["starvation_recoveries"] += 1
                self._record_decision(DecisionRecord(
                    timestamp=now,
                    rule_id=RuleID.STARVATION.value,
                    pid=pid,
                    process_name=name,
                    action=f"PRIORITY_BOOSTED_TO_{boost_prio.upper()}",
                    reason=f"Process received <{self._config.starvation_cpu_min_percent}% CPU "
                           f"for >{self._config.starvation_duration_seconds}s (starved)",
                    details=f"Temporary priority boost applied (aging mechanism). "
                            f"Boost will expire in 10 seconds and priority will revert.",
                    severity="info",
                ))
        except Exception as e:
            logger.warning(f"Starvation boost failed for PID {pid}: {e}")

    def _expire_boosts(self, now: float):
        """Revert priority boosts that have expired."""
        expired = [pid for pid, expiry in self._boosted_pids.items() if now >= expiry]
        for pid in expired:
            try:
                self._isolation.set_process_priority(pid, "normal")
                self._record_decision(DecisionRecord(
                    timestamp=now,
                    rule_id=RuleID.STARVATION.value,
                    pid=pid,
                    process_name="",
                    action="PRIORITY_BOOST_EXPIRED",
                    reason="Temporary priority boost duration ended",
                    details="Priority reverted to NORMAL_PRIORITY_CLASS",
                    severity="info",
                ))
            except Exception:
                pass
            del self._boosted_pids[pid]

    # ── Rule: Resource Fairness ───────────────────────────────────────────

    def _rule_resource_fairness(self, processes: List[dict], now: float):
        """
        Ensure no single non-interactive process dominates resources
        while multiple other processes are competing.

        This demonstrates the OS concept of fairness in scheduling.
        """
        # Count processes that want CPU (cpu > 1%)
        competing = [p for p in processes if p["cpu_percent"] > 1.0
                     and self._process_categories.get(p["pid"]) not in
                     (ProcessCategory.SYSTEM, None)]

        if len(competing) < 3:
            return

        # Check if any single process has more than 3x the average
        total_cpu = sum(p["cpu_percent"] for p in competing)
        avg_cpu = total_cpu / len(competing)

        for p in competing:
            pid = p["pid"]
            name = p["name"] or ""
            cpu = p["cpu_percent"]
            cat = self._process_categories.get(pid, ProcessCategory.UNKNOWN)

            if cat == ProcessCategory.INTERACTIVE:
                continue
            if pid in self._throttled_pids:
                continue

            if cpu > avg_cpu * 3 and cpu > 30:
                self._record_decision(DecisionRecord(
                    timestamp=now,
                    rule_id=RuleID.RESOURCE_FAIRNESS.value,
                    pid=pid,
                    process_name=name,
                    action="FAIRNESS_WARNING",
                    reason=f"Process using {cpu:.1f}% CPU vs average {avg_cpu:.1f}% "
                           f"among {len(competing)} competing processes",
                    details=f"Process consumes {cpu/avg_cpu:.1f}x the fair share. "
                            f"Category: {cat.value}. Consider manual throttling.",
                    severity="warning",
                ))

    # ── Internal Helpers ──────────────────────────────────────────────────

    def _record_decision(self, decision: DecisionRecord):
        """Thread-safe recording of a policy decision."""
        with self._lock:
            self._decisions.append(decision)
        logger.info(
            f"[{decision.rule_id}] PID={decision.pid} ({decision.process_name}): "
            f"{decision.action} — {decision.reason}"
        )
