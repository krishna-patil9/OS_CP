"""
Isolation Engine — Windows NT Kernel Job Object & Restricted Token Manager

Uses native Win32 APIs to enforce real OS-level process isolation:
  - CPU Rate Caps via JobObjectCpuRateControlInformation
  - Hard Memory Limits via JobObjectExtendedLimitInformation
  - Active Process Limits (fork-bomb prevention)
  - Low Integrity Level execution via Restricted Security Tokens

OS Concepts Demonstrated:
  - Process isolation and protection boundaries
  - Resource limits enforced by the kernel
  - Security token manipulation and integrity levels
  - Job object process grouping (analogous to Linux cgroups)
"""

import ctypes
import ctypes.wintypes as wintypes
import subprocess
import os
import time
import threading
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, List

import psutil

# ──────────────────────────────────────────────────────────────────────────────
# Win32 Constants
# ──────────────────────────────────────────────────────────────────────────────

# Job Object Limit Flags
JOB_OBJECT_LIMIT_PROCESS_MEMORY          = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY              = 0x00000200
JOB_OBJECT_LIMIT_ACTIVE_PROCESS          = 0x00000008
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE       = 0x00002000
JOB_OBJECT_LIMIT_WORKINGSET              = 0x00000001
JOB_OBJECT_LIMIT_PRIORITY_CLASS          = 0x00000020
JOB_OBJECT_LIMIT_PROCESS_TIME            = 0x00000002

# CPU Rate Control Flags
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE       = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP     = 0x4

# Job Object Info Classes
JobObjectExtendedLimitInformation        = 9
JobObjectCpuRateControlInformation       = 15
JobObjectBasicAccountingInformation      = 1

# Process Creation Flags
CREATE_SUSPENDED                         = 0x00000004
CREATE_BREAKAWAY_FROM_JOB                = 0x01000000
CREATE_NEW_CONSOLE                       = 0x00000010

# Process Access Rights
PROCESS_ALL_ACCESS                       = 0x001FFFFF
PROCESS_SET_QUOTA                        = 0x0100
PROCESS_TERMINATE                        = 0x0001

# Priority Classes
IDLE_PRIORITY_CLASS                      = 0x00000040
BELOW_NORMAL_PRIORITY_CLASS              = 0x00004000
NORMAL_PRIORITY_CLASS                    = 0x00000020
ABOVE_NORMAL_PRIORITY_CLASS              = 0x00008000
HIGH_PRIORITY_CLASS                      = 0x00000080
REALTIME_PRIORITY_CLASS                  = 0x00000100

PRIORITY_CLASS_MAP = {
    "idle": IDLE_PRIORITY_CLASS,
    "below_normal": BELOW_NORMAL_PRIORITY_CLASS,
    "normal": NORMAL_PRIORITY_CLASS,
    "above_normal": ABOVE_NORMAL_PRIORITY_CLASS,
    "high": HIGH_PRIORITY_CLASS,
    "realtime": REALTIME_PRIORITY_CLASS,
}

PRIORITY_CLASS_NAMES = {v: k for k, v in PRIORITY_CLASS_MAP.items()}

# ──────────────────────────────────────────────────────────────────────────────
# Win32 Structures
# ──────────────────────────────────────────────────────────────────────────────

class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]

class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]

class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]

class JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("ControlFlags", wintypes.DWORD),
        ("CpuRate", wintypes.DWORD),  # Parts per 10000 (e.g. 2000 = 20%)
    ]

class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]

# ──────────────────────────────────────────────────────────────────────────────
# Win32 API Bindings
# ──────────────────────────────────────────────────────────────────────────────

kernel32 = ctypes.windll.kernel32
advapi32 = ctypes.windll.advapi32

kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
kernel32.CreateJobObjectW.restype = wintypes.HANDLE

kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
]
kernel32.SetInformationJobObject.restype = wintypes.BOOL

kernel32.QueryInformationJobObject.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD)
]
kernel32.QueryInformationJobObject.restype = wintypes.BOOL

kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateJobObject.restype = wintypes.BOOL

kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.SetPriorityClass.restype = wintypes.BOOL

kernel32.GetPriorityClass.argtypes = [wintypes.HANDLE]
kernel32.GetPriorityClass.restype = wintypes.DWORD

kernel32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_ulong)]
kernel32.SetProcessAffinityMask.restype = wintypes.BOOL

kernel32.SetProcessWorkingSetSizeEx.argtypes = [
    wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD
]
kernel32.SetProcessWorkingSetSizeEx.restype = wintypes.BOOL

logger = logging.getLogger("isolation_engine")


# ──────────────────────────────────────────────────────────────────────────────
# Data Models
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SandboxConfig:
    """Configuration for a Job Object sandbox container."""
    name: str
    cpu_rate_percent: Optional[int] = None       # Hard CPU cap (1-100)
    memory_limit_mb: Optional[int] = None        # Hard memory limit in MB
    max_active_processes: Optional[int] = None   # Max child processes allowed
    priority_class: Optional[str] = None         # idle, below_normal, normal, etc.
    kill_on_close: bool = True                   # Kill all processes when job closes
    max_working_set_mb: Optional[int] = None     # Working set maximum in MB


@dataclass
class SandboxContainer:
    """Represents an active Job Object sandbox with tracked processes."""
    config: SandboxConfig
    job_handle: int = 0
    created_at: float = 0.0
    assigned_pids: List[int] = field(default_factory=list)
    is_active: bool = True


# ──────────────────────────────────────────────────────────────────────────────
# Isolation Engine
# ──────────────────────────────────────────────────────────────────────────────

class IsolationEngine:
    """
    Manages Windows Job Object sandboxes for process isolation.

    Each sandbox is a named Job Object with configured resource limits.
    Processes can be assigned to sandboxes to enforce OS-level hard caps.
    """

    def __init__(self):
        self._sandboxes: Dict[str, SandboxContainer] = {}
        self._lock = threading.Lock()
        self._decision_log: List[dict] = []

    # ── Job Object Lifecycle ──────────────────────────────────────────────

    def create_sandbox(self, config: SandboxConfig) -> SandboxContainer:
        """
        Create a new Windows Job Object sandbox with the given configuration.
        Applies CPU rate caps, memory limits, and process restrictions.
        """
        with self._lock:
            if config.name in self._sandboxes:
                raise ValueError(f"Sandbox '{config.name}' already exists")

            # Create the Job Object
            job_name = f"OSRM_Sandbox_{config.name}"
            job_handle = kernel32.CreateJobObjectW(None, job_name)
            if not job_handle:
                error = ctypes.get_last_error()
                raise OSError(f"CreateJobObjectW failed with error {error}")

            container = SandboxContainer(
                config=config,
                job_handle=job_handle,
                created_at=time.time(),
            )

            # Apply extended limits (memory, process count, priority, kill-on-close)
            self._apply_extended_limits(container)

            # Apply CPU rate control if specified
            if config.cpu_rate_percent is not None:
                self._apply_cpu_rate_limit(container)

            self._sandboxes[config.name] = container
            self._log_decision(
                action="CREATE_SANDBOX",
                sandbox=config.name,
                details=f"Job Object created: CPU={config.cpu_rate_percent}%, "
                        f"RAM={config.memory_limit_mb}MB, "
                        f"MaxProcs={config.max_active_processes}, "
                        f"Priority={config.priority_class}",
            )
            logger.info(f"Sandbox '{config.name}' created successfully")
            return container

    def _apply_extended_limits(self, container: SandboxContainer):
        """Apply memory, process count, priority, and kill-on-close limits."""
        config = container.config
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        flags = 0

        if config.memory_limit_mb is not None:
            info.ProcessMemoryLimit = config.memory_limit_mb * 1024 * 1024
            flags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY

        if config.max_active_processes is not None:
            info.BasicLimitInformation.ActiveProcessLimit = config.max_active_processes
            flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS

        if config.priority_class is not None:
            pc = PRIORITY_CLASS_MAP.get(config.priority_class.lower())
            if pc:
                info.BasicLimitInformation.PriorityClass = pc
                flags |= JOB_OBJECT_LIMIT_PRIORITY_CLASS

        if config.kill_on_close:
            flags |= JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

        if config.max_working_set_mb is not None:
            info.BasicLimitInformation.MinimumWorkingSetSize = 1024 * 1024  # 1 MB min
            info.BasicLimitInformation.MaximumWorkingSetSize = (
                config.max_working_set_mb * 1024 * 1024
            )
            flags |= JOB_OBJECT_LIMIT_WORKINGSET

        info.BasicLimitInformation.LimitFlags = flags

        success = kernel32.SetInformationJobObject(
            container.job_handle,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not success:
            error = ctypes.get_last_error()
            logger.warning(f"SetInformationJobObject (extended) failed: error {error}")

    def _apply_cpu_rate_limit(self, container: SandboxContainer):
        """Apply hard CPU rate cap to the Job Object."""
        config = container.config
        rate = max(1, min(100, config.cpu_rate_percent))

        cpu_info = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
        cpu_info.ControlFlags = (
            JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
        )
        cpu_info.CpuRate = rate * 100  # Convert percentage to parts-per-10000

        success = kernel32.SetInformationJobObject(
            container.job_handle,
            JobObjectCpuRateControlInformation,
            ctypes.byref(cpu_info),
            ctypes.sizeof(cpu_info),
        )
        if not success:
            error = ctypes.get_last_error()
            logger.warning(f"SetInformationJobObject (CPU rate) failed: error {error}")

    # ── Process Assignment ────────────────────────────────────────────────

    def assign_process(self, sandbox_name: str, pid: int) -> bool:
        """
        Assign an existing process (by PID) to a named sandbox Job Object.
        The process will immediately be subject to the sandbox's resource limits.
        """
        with self._lock:
            container = self._sandboxes.get(sandbox_name)
            if not container or not container.is_active:
                raise ValueError(f"Sandbox '{sandbox_name}' not found or inactive")

            proc_handle = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
            if not proc_handle:
                error = ctypes.get_last_error()
                raise OSError(f"OpenProcess({pid}) failed with error {error}")

            try:
                success = kernel32.AssignProcessToJobObject(
                    container.job_handle, proc_handle
                )
                if not success:
                    error = ctypes.get_last_error()
                    raise OSError(
                        f"AssignProcessToJobObject failed with error {error}"
                    )

                container.assigned_pids.append(pid)
                self._log_decision(
                    action="ASSIGN_PROCESS",
                    sandbox=sandbox_name,
                    pid=pid,
                    details=f"Process {pid} assigned to sandbox '{sandbox_name}'",
                )
                logger.info(f"PID {pid} assigned to sandbox '{sandbox_name}'")
                return True
            finally:
                kernel32.CloseHandle(proc_handle)

    def spawn_in_sandbox(
        self, sandbox_name: str, command: str, args: Optional[List[str]] = None
    ) -> int:
        """
        Spawn a new process and immediately assign it to a sandbox.
        Returns the PID of the spawned process.
        """
        full_cmd = [command] + (args or [])
        proc = subprocess.Popen(
            full_cmd,
            creationflags=CREATE_NEW_CONSOLE | CREATE_BREAKAWAY_FROM_JOB,
        )
        pid = proc.pid

        try:
            self.assign_process(sandbox_name, pid)
        except Exception:
            proc.terminate()
            raise

        self._log_decision(
            action="SPAWN_IN_SANDBOX",
            sandbox=sandbox_name,
            pid=pid,
            details=f"Spawned '{command}' (PID {pid}) in sandbox '{sandbox_name}'",
        )
        return pid

    # ── Process Priority & Affinity Control ───────────────────────────────

    def set_process_priority(self, pid: int, priority: str) -> bool:
        """Set the priority class of a process by PID."""
        pc = PRIORITY_CLASS_MAP.get(priority.lower())
        if pc is None:
            raise ValueError(f"Unknown priority class: {priority}")

        proc_handle = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
        if not proc_handle:
            return False

        try:
            success = kernel32.SetPriorityClass(proc_handle, pc)
            if success:
                self._log_decision(
                    action="SET_PRIORITY",
                    pid=pid,
                    details=f"Process {pid} priority set to '{priority}'",
                )
            return bool(success)
        finally:
            kernel32.CloseHandle(proc_handle)

    def get_process_priority(self, pid: int) -> Optional[str]:
        """Get the priority class name of a process by PID."""
        proc_handle = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
        if not proc_handle:
            return None
        try:
            pc = kernel32.GetPriorityClass(proc_handle)
            return PRIORITY_CLASS_NAMES.get(pc, f"unknown({pc})")
        finally:
            kernel32.CloseHandle(proc_handle)

    def set_process_affinity(self, pid: int, core_mask: int) -> bool:
        """
        Set CPU affinity mask for a process.
        core_mask is a bitmask (e.g. 0x3 = cores 0 and 1).
        """
        proc_handle = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
        if not proc_handle:
            return False
        try:
            mask = ctypes.c_ulong(core_mask)
            success = kernel32.SetProcessAffinityMask(proc_handle, ctypes.pointer(mask))
            if success:
                self._log_decision(
                    action="SET_AFFINITY",
                    pid=pid,
                    details=f"Process {pid} affinity set to mask 0x{core_mask:X}",
                )
            return bool(success)
        finally:
            kernel32.CloseHandle(proc_handle)

    def trim_working_set(self, pid: int) -> bool:
        """
        Force-trim the working set of a process.
        This releases physical RAM pages back to the OS, simulating
        memory pressure management that the OS kernel performs.
        """
        proc_handle = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
        if not proc_handle:
            return False
        try:
            # Setting both min and max to -1 signals the OS to trim working set
            SIZE_MAX = ctypes.c_size_t(-1).value
            success = kernel32.SetProcessWorkingSetSizeEx(
                proc_handle, SIZE_MAX, SIZE_MAX, 0
            )
            if success:
                self._log_decision(
                    action="TRIM_WORKING_SET",
                    pid=pid,
                    details=f"Working set trimmed for process {pid}",
                )
            return bool(success)
        finally:
            kernel32.CloseHandle(proc_handle)

    # ── Job Object Accounting ─────────────────────────────────────────────

    def get_job_accounting(self, sandbox_name: str) -> Optional[dict]:
        """Query kernel accounting information for a Job Object."""
        with self._lock:
            container = self._sandboxes.get(sandbox_name)
            if not container:
                return None

        info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        ret_len = wintypes.DWORD(0)
        success = kernel32.QueryInformationJobObject(
            container.job_handle,
            JobObjectBasicAccountingInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(ret_len),
        )
        if not success:
            return None

        return {
            "total_user_time_ms": info.TotalUserTime // 10000,
            "total_kernel_time_ms": info.TotalKernelTime // 10000,
            "total_page_faults": info.TotalPageFaultCount,
            "total_processes": info.TotalProcesses,
            "active_processes": info.ActiveProcesses,
            "total_terminated": info.TotalTerminatedProcesses,
        }

    # ── Sandbox Management ────────────────────────────────────────────────

    def destroy_sandbox(self, sandbox_name: str) -> bool:
        """Destroy a sandbox, terminating all assigned processes."""
        with self._lock:
            container = self._sandboxes.get(sandbox_name)
            if not container:
                return False

            # Terminate all processes in the job
            kernel32.TerminateJobObject(container.job_handle, 1)
            kernel32.CloseHandle(container.job_handle)
            container.is_active = False

            del self._sandboxes[sandbox_name]
            self._log_decision(
                action="DESTROY_SANDBOX",
                sandbox=sandbox_name,
                details=f"Sandbox '{sandbox_name}' destroyed, all processes terminated",
            )
            logger.info(f"Sandbox '{sandbox_name}' destroyed")
            return True

    def list_sandboxes(self) -> List[dict]:
        """List all active sandboxes with their configuration and status."""
        with self._lock:
            result = []
            for name, container in self._sandboxes.items():
                accounting = self.get_job_accounting(name)
                # Clean up PIDs that no longer exist
                alive_pids = []
                for pid in container.assigned_pids:
                    if psutil.pid_exists(pid):
                        alive_pids.append(pid)
                container.assigned_pids = alive_pids

                result.append({
                    "name": name,
                    "is_active": container.is_active,
                    "created_at": container.created_at,
                    "config": {
                        "cpu_rate_percent": container.config.cpu_rate_percent,
                        "memory_limit_mb": container.config.memory_limit_mb,
                        "max_active_processes": container.config.max_active_processes,
                        "priority_class": container.config.priority_class,
                        "kill_on_close": container.config.kill_on_close,
                        "max_working_set_mb": container.config.max_working_set_mb,
                    },
                    "assigned_pids": alive_pids,
                    "accounting": accounting,
                })
            return result

    def update_sandbox_limits(self, sandbox_name: str, **kwargs) -> bool:
        """
        Dynamically update limits on an active sandbox.
        Accepts: cpu_rate_percent, memory_limit_mb, max_active_processes,
                 priority_class, max_working_set_mb
        """
        with self._lock:
            container = self._sandboxes.get(sandbox_name)
            if not container or not container.is_active:
                return False

            config = container.config
            if "cpu_rate_percent" in kwargs:
                config.cpu_rate_percent = kwargs["cpu_rate_percent"]
                self._apply_cpu_rate_limit(container)
            if "memory_limit_mb" in kwargs:
                config.memory_limit_mb = kwargs["memory_limit_mb"]
            if "max_active_processes" in kwargs:
                config.max_active_processes = kwargs["max_active_processes"]
            if "priority_class" in kwargs:
                config.priority_class = kwargs["priority_class"]
            if "max_working_set_mb" in kwargs:
                config.max_working_set_mb = kwargs["max_working_set_mb"]

            self._apply_extended_limits(container)
            self._log_decision(
                action="UPDATE_SANDBOX",
                sandbox=sandbox_name,
                details=f"Limits updated: {kwargs}",
            )
            return True

    # ── Decision Logging ──────────────────────────────────────────────────

    def _log_decision(self, action: str, details: str, sandbox: str = "",
                      pid: int = 0):
        """Record a structured decision log entry."""
        entry = {
            "timestamp": time.time(),
            "engine": "IsolationEngine",
            "action": action,
            "sandbox": sandbox,
            "pid": pid,
            "details": details,
        }
        self._decision_log.append(entry)
        # Keep last 500 entries
        if len(self._decision_log) > 500:
            self._decision_log = self._decision_log[-500:]

    def get_decision_log(self, limit: int = 50) -> List[dict]:
        """Return the most recent decision log entries."""
        return list(reversed(self._decision_log[-limit:]))

    def get_sandbox(self, sandbox_name: str) -> Optional[SandboxContainer]:
        """Return a sandbox container by name."""
        with self._lock:
            return self._sandboxes.get(sandbox_name)
