"""
Isolation Engine — Windows NT Kernel Job Object Manager

Uses native Win32 APIs (kernel32) to enforce OS-level process grouping and
resource containment:
  - CPU Rate Hard Caps via JobObjectCpuRateControlInformation
  - Per-Process Committed Memory Limits (JOB_OBJECT_LIMIT_PROCESS_MEMORY)
  - Aggregate Job Committed Memory Limits (JOB_OBJECT_LIMIT_JOB_MEMORY)
  - Working-Set Bounds (JOB_OBJECT_LIMIT_WORKINGSET, distinct from commit limits)
  - Active Process Limits (JOB_OBJECT_LIMIT_ACTIVE_PROCESS / fork-bomb prevention)
  - Automatic Cleanup on Handle Close (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
  - Process Priority Class & CPU Affinity controls

Security Boundary Audit Note:
  Windows Job Objects enforce process grouping and kernel resource-management
  controls, NOT a security sandbox for arbitrary untrusted code. They do NOT
  independently isolate filesystem access, network access, registry access, or
  user privileges. Restricted security tokens / AppContainer / Low Integrity
  Level are NOT implemented in this module; arbitrary untrusted code requires
  an additional Windows security boundary (e.g., restricted token, AppContainer,
  or virtual machine).
"""

import ctypes
import ctypes.wintypes as wintypes
import subprocess
import os
import time
import threading
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any

import psutil

# ──────────────────────────────────────────────────────────────────────────────
# Win32 Constants
# ──────────────────────────────────────────────────────────────────────────────

# Job Object Limit Flags
JOB_OBJECT_LIMIT_WORKINGSET              = 0x00000001
JOB_OBJECT_LIMIT_PROCESS_TIME            = 0x00000002
JOB_OBJECT_LIMIT_JOB_TIME                = 0x00000004
JOB_OBJECT_LIMIT_ACTIVE_PROCESS          = 0x00000008
JOB_OBJECT_LIMIT_AFFINITY                = 0x00000010
JOB_OBJECT_LIMIT_PRIORITY_CLASS          = 0x00000020
JOB_OBJECT_LIMIT_PROCESS_MEMORY          = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY              = 0x00000200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE       = 0x00002000

# CPU Rate Control Flags
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE       = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP     = 0x4

# Job Object Info Classes
JobObjectBasicAccountingInformation      = 1
JobObjectBasicProcessIdList              = 3
JobObjectExtendedLimitInformation        = 9
JobObjectCpuRateControlInformation       = 15

# Process Creation Flags
CREATE_SUSPENDED                         = 0x00000004
CREATE_BREAKAWAY_FROM_JOB                = 0x01000000
CREATE_NEW_CONSOLE                       = 0x00000010

# Least-Privilege Process Access Rights (avoid PROCESS_ALL_ACCESS)
PROCESS_TERMINATE                        = 0x0001
PROCESS_SET_QUOTA                        = 0x0100
PROCESS_SET_INFORMATION                  = 0x0200
PROCESS_QUERY_LIMITED_INFORMATION        = 0x1000
SYNCHRONIZE                              = 0x00100000

ACCESS_ASSIGN_JOB = (
    PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION
)
ACCESS_TERMINATE_VERIFY = (
    PROCESS_TERMINATE | SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
)
ACCESS_INSPECT_ONLY = PROCESS_QUERY_LIMITED_INFORMATION
ACCESS_SET_PRIORITY_AFFINITY = (
    PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION
)
ACCESS_TRIM_WORKING_SET = (
    PROCESS_SET_QUOTA | PROCESS_QUERY_LIMITED_INFORMATION
)

# Wait Constants
WAIT_OBJECT_0                            = 0x00000000
WAIT_ABANDONED                           = 0x00000080
WAIT_TIMEOUT                             = 0x00000102
WAIT_FAILED                              = 0xFFFFFFFF
STILL_ACTIVE                             = 259

# Win32 Error Codes
ERROR_ACCESS_DENIED                      = 5
ERROR_INVALID_PARAMETER                  = 87
ERROR_ALREADY_EXISTS                     = 183
ERROR_MORE_DATA                          = 234

# Windows Scheduling Priority Classes (distinct from application recovery priority)
IDLE_PRIORITY_CLASS                      = 0x00000040
BELOW_NORMAL_PRIORITY_CLASS              = 0x00004000
NORMAL_PRIORITY_CLASS                    = 0x00000020
ABOVE_NORMAL_PRIORITY_CLASS              = 0x00008000
HIGH_PRIORITY_CLASS                      = 0x00000080
REALTIME_PRIORITY_CLASS                  = 0x00000100

PRIORITY_CLASS_MAP = {
    "idle": IDLE_PRIORITY_CLASS,
    "idle_priority_class": IDLE_PRIORITY_CLASS,
    "below_normal": BELOW_NORMAL_PRIORITY_CLASS,
    "below_normal_priority_class": BELOW_NORMAL_PRIORITY_CLASS,
    "normal": NORMAL_PRIORITY_CLASS,
    "normal_priority_class": NORMAL_PRIORITY_CLASS,
    "above_normal": ABOVE_NORMAL_PRIORITY_CLASS,
    "above_normal_priority_class": ABOVE_NORMAL_PRIORITY_CLASS,
    "high": HIGH_PRIORITY_CLASS,
    "high_priority_class": HIGH_PRIORITY_CLASS,
    "realtime": REALTIME_PRIORITY_CLASS,
    "realtime_priority_class": REALTIME_PRIORITY_CLASS,
}

PRIORITY_CLASS_NAMES = {
    IDLE_PRIORITY_CLASS: "idle",
    BELOW_NORMAL_PRIORITY_CLASS: "below_normal",
    NORMAL_PRIORITY_CLASS: "normal",
    ABOVE_NORMAL_PRIORITY_CLASS: "above_normal",
    HIGH_PRIORITY_CLASS: "high",
    REALTIME_PRIORITY_CLASS: "realtime",
}

CRITICAL_PROCESS_NAMES = {
    "system", "registry", "smss.exe", "csrss.exe", "wininit.exe",
    "services.exe", "lsass.exe", "svchost.exe", "dwm.exe",
    "fontdrvhost.exe", "sihost.exe", "taskhostw.exe",
    "runtimebroker.exe", "searchhost.exe", "explorer.exe",
    "winlogon.exe", "lsaiso.exe", "memory compression",
}


def is_critical_process(pid: int) -> bool:
    """Guard against modifying, enclosing, or terminating core Windows OS or backend processes."""
    if pid <= 4:
        return True
    if pid == os.getpid():
        return True
    try:
        proc = psutil.Process(pid)
        name = (proc.name() or "").lower()
        if name in CRITICAL_PROCESS_NAMES:
            return True
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        # If access is denied for a system-owned PID, treat conservatively when checking protection
        pass
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Win32 Structures
# ──────────────────────────────────────────────────────────────────────────────

class FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]

    def to_int(self) -> int:
        return (int(self.dwHighDateTime) << 32) | int(self.dwLowDateTime)


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
        ("Affinity", ctypes.c_size_t),  # ULONG_PTR
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
# Win32 API Bindings (use_last_error=True ensures ctypes.get_last_error() works)
# ──────────────────────────────────────────────────────────────────────────────

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

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

kernel32.IsProcessInJob.argtypes = [
    wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)
]
kernel32.IsProcessInJob.restype = wintypes.BOOL

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE

kernel32.GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
]
kernel32.GetProcessTimes.restype = wintypes.BOOL

kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateProcess.restype = wintypes.BOOL

kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD

kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateJobObject.restype = wintypes.BOOL

kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.SetPriorityClass.restype = wintypes.BOOL

kernel32.GetPriorityClass.argtypes = [wintypes.HANDLE]
kernel32.GetPriorityClass.restype = wintypes.DWORD

kernel32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
kernel32.SetProcessAffinityMask.restype = wintypes.BOOL

kernel32.SetProcessWorkingSetSizeEx.argtypes = [
    wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD
]
kernel32.SetProcessWorkingSetSizeEx.restype = wintypes.BOOL

logger = logging.getLogger("isolation_engine")


def get_process_creation_time_100ns(pid: int) -> int:
    """
    Query exact Windows FILETIME creation timestamp (100ns intervals since 1601)
    for a running process using least-privilege PROCESS_QUERY_LIMITED_INFORMATION.
    """
    handle = kernel32.OpenProcess(ACCESS_INSPECT_ONLY, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        raise OSError(err, f"OpenProcess({pid}) failed with Win32 error {err}")
    try:
        ct, et, kt, ut = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        ok = kernel32.GetProcessTimes(
            handle, ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut)
        )
        if not ok:
            err = ctypes.get_last_error()
            raise OSError(err, f"GetProcessTimes({pid}) failed with Win32 error {err}")
        return ct.to_int()
    finally:
        kernel32.CloseHandle(handle)


def _format_win32_error(err: int) -> str:
    try:
        return f"Win32 error {err} ({ctypes.FormatError(err).strip()})"
    except Exception:
        return f"Win32 error {err}"


# ──────────────────────────────────────────────────────────────────────────────
# Data Models
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SandboxConfig:
    """Configuration for a Windows NT Job Object sandbox."""
    name: str
    cpu_rate_percent: Optional[int] = None           # Hard CPU cap (1-100)
    memory_limit_mb: Optional[int] = None            # Per-process committed memory limit (legacy alias)
    per_process_memory_limit_mb: Optional[int] = None  # Explicit JOB_OBJECT_LIMIT_PROCESS_MEMORY (MB)
    job_memory_limit_mb: Optional[int] = None        # Aggregate JOB_OBJECT_LIMIT_JOB_MEMORY (MB)
    max_active_processes: Optional[int] = None       # JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    priority_class: Optional[str] = None             # Windows scheduling priority class (idle, below_normal, normal, ...)
    kill_on_close: bool = True                       # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    max_working_set_mb: Optional[int] = None         # JOB_OBJECT_LIMIT_WORKINGSET (distinct from committed memory)
    strict_limits: bool = True                       # Fail sandbox creation if any mandatory limit fails verification
    max_runtime_seconds: Optional[int] = None        # Optional metadata for bounded watchdog runs

    def effective_per_process_memory_mb(self) -> Optional[int]:
        if self.per_process_memory_limit_mb is not None:
            return self.per_process_memory_limit_mb
        return self.memory_limit_mb


@dataclass
class SandboxContainer:
    """Represents an active Windows NT Job Object sandbox with tracked processes."""
    config: SandboxConfig
    job_handle: int = 0
    job_name: str = ""
    created_at: float = 0.0
    assigned_pids: List[int] = field(default_factory=list)
    assigned_process_meta: Dict[int, int] = field(default_factory=dict)  # pid -> create_time_100ns
    verified_limits: Dict[str, Any] = field(default_factory=dict)
    nested_job_detected: bool = False
    is_active: bool = True


# ──────────────────────────────────────────────────────────────────────────────
# Isolation Engine
# ──────────────────────────────────────────────────────────────────────────────

class IsolationEngine:
    """
    Manages Windows NT Kernel Job Object sandboxes for process grouping and
    resource-limit containment.

    Note: Job Objects enforce CPU, memory, process-count, and lifetime limits;
    they do not provide filesystem, network, registry, or token privilege
    isolation.
    """

    def __init__(self):
        self._sandboxes: Dict[str, SandboxContainer] = {}
        self._lock = threading.RLock()
        self._decision_log: List[dict] = []

    # ── Job Object Lifecycle ──────────────────────────────────────────────

    def create_sandbox(self, config: SandboxConfig) -> SandboxContainer:
        """
        Create a new uniquely named Windows Job Object sandbox, apply all
        requested limits, and verify them via QueryInformationJobObject.
        If any mandatory limit fails to apply or verify, the Job Object handle
        is closed and an OSError is raised (fail-closed).
        """
        with self._lock:
            if config.name in self._sandboxes:
                raise ValueError(f"Sandbox '{config.name}' already exists")

            per_proc_mb = config.effective_per_process_memory_mb()
            job_mem_mb = config.job_memory_limit_mb

            if per_proc_mb is not None and per_proc_mb <= 0:
                raise ValueError("per_process_memory_limit_mb must be positive")
            if job_mem_mb is not None and job_mem_mb <= 0:
                raise ValueError("job_memory_limit_mb must be positive")
            if (
                per_proc_mb is not None
                and job_mem_mb is not None
                and job_mem_mb < per_proc_mb
            ):
                raise ValueError(
                    f"job_memory_limit_mb ({job_mem_mb}) must be >= per_process_memory_limit_mb ({per_proc_mb})"
                )
            if config.cpu_rate_percent is not None and not (1 <= config.cpu_rate_percent <= 100):
                raise ValueError("cpu_rate_percent must be between 1 and 100")
            if config.max_active_processes is not None and config.max_active_processes < 1:
                raise ValueError("max_active_processes must be >= 1")

            # Normalize alias field so callers inspecting config.memory_limit_mb stay consistent
            if config.per_process_memory_limit_mb is None and config.memory_limit_mb is not None:
                config.per_process_memory_limit_mb = config.memory_limit_mb
            elif config.memory_limit_mb is None and config.per_process_memory_limit_mb is not None:
                config.memory_limit_mb = config.per_process_memory_limit_mb

            job_name = f"Local\\OSRM_Sandbox_{config.name}"
            ctypes.set_last_error(0)
            job_handle = kernel32.CreateJobObjectW(None, job_name)
            last_err = ctypes.get_last_error()
            if not job_handle:
                raise OSError(
                    last_err,
                    f"CreateJobObjectW('{job_name}') failed: {_format_win32_error(last_err)}",
                )
            if last_err == ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(job_handle)
                raise ValueError(
                    f"Windows Job Object '{job_name}' already exists in kernel namespace"
                )

            container = SandboxContainer(
                config=config,
                job_handle=job_handle,
                job_name=job_name,
                created_at=time.time(),
            )

            try:
                # Apply and verify extended limits (committed memory, active processes, priority, working set, kill-on-close)
                self._apply_extended_limits(container)

                # Apply and verify CPU hard-cap rate control if specified
                if config.cpu_rate_percent is not None:
                    self._apply_cpu_rate_limit(container)

                # Read back kernel state to confirm mandatory settings actually took effect
                container.verified_limits = self.verify_sandbox_limits(container)
            except Exception:
                # Fail-closed: never leave a half-configured Job Object around
                kernel32.CloseHandle(job_handle)
                container.is_active = False
                raise

            self._sandboxes[config.name] = container
            self._log_decision(
                action="CREATE_SANDBOX",
                sandbox=config.name,
                details=(
                    f"Job Object '{job_name}' created & verified: "
                    f"CPU={config.cpu_rate_percent}%, "
                    f"ProcMem={per_proc_mb}MB, "
                    f"JobMem={job_mem_mb}MB, "
                    f"MaxProcs={config.max_active_processes}, "
                    f"WorkingSet={config.max_working_set_mb}MB, "
                    f"KillOnClose={config.kill_on_close}, "
                    f"Priority={config.priority_class}"
                ),
            )
            logger.info(f"Sandbox '{config.name}' ({job_name}) created and verified")
            return container

    def _apply_extended_limits(self, container: SandboxContainer):
        """
        Apply committed memory (per-process and/or job-wide), active-process,
        priority class, working-set, and kill-on-close limits.
        Note: Committed memory limits (JOB_OBJECT_LIMIT_PROCESS_MEMORY and
        JOB_OBJECT_LIMIT_JOB_MEMORY) are distinct from working-set limits
        (JOB_OBJECT_LIMIT_WORKINGSET).
        """
        config = container.config
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        flags = 0

        per_proc_mb = config.effective_per_process_memory_mb()
        if per_proc_mb is not None:
            info.ProcessMemoryLimit = int(per_proc_mb) * 1024 * 1024
            flags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY

        if config.job_memory_limit_mb is not None:
            info.JobMemoryLimit = int(config.job_memory_limit_mb) * 1024 * 1024
            flags |= JOB_OBJECT_LIMIT_JOB_MEMORY

        if config.max_active_processes is not None:
            info.BasicLimitInformation.ActiveProcessLimit = int(config.max_active_processes)
            flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS

        if config.priority_class is not None:
            pc = PRIORITY_CLASS_MAP.get(config.priority_class.lower())
            if pc is None:
                raise ValueError(f"Unknown Windows priority class: '{config.priority_class}'")
            info.BasicLimitInformation.PriorityClass = pc
            flags |= JOB_OBJECT_LIMIT_PRIORITY_CLASS

        if config.kill_on_close:
            flags |= JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

        if config.max_working_set_mb is not None:
            min_ws = 1024 * 1024  # 1 MB minimum working set
            max_ws = max(min_ws * 2, int(config.max_working_set_mb) * 1024 * 1024)
            info.BasicLimitInformation.MinimumWorkingSetSize = min_ws
            info.BasicLimitInformation.MaximumWorkingSetSize = max_ws
            flags |= JOB_OBJECT_LIMIT_WORKINGSET

        info.BasicLimitInformation.LimitFlags = flags

        ctypes.set_last_error(0)
        success = kernel32.SetInformationJobObject(
            container.job_handle,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not success:
            error = ctypes.get_last_error()
            msg = (
                f"SetInformationJobObject(JobObjectExtendedLimitInformation) failed "
                f"for sandbox '{config.name}': {_format_win32_error(error)}"
            )
            if config.strict_limits:
                raise OSError(error, msg)
            logger.warning(msg)

    def _apply_cpu_rate_limit(self, container: SandboxContainer):
        """Apply hard CPU rate cap via JobObjectCpuRateControlInformation."""
        config = container.config
        rate = max(1, min(100, int(config.cpu_rate_percent)))

        cpu_info = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
        cpu_info.ControlFlags = (
            JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
        )
        cpu_info.CpuRate = rate * 100  # Parts per 10,000 (20% = 2000)

        ctypes.set_last_error(0)
        success = kernel32.SetInformationJobObject(
            container.job_handle,
            JobObjectCpuRateControlInformation,
            ctypes.byref(cpu_info),
            ctypes.sizeof(cpu_info),
        )
        if not success:
            error = ctypes.get_last_error()
            msg = (
                f"SetInformationJobObject(JobObjectCpuRateControlInformation) failed "
                f"for sandbox '{config.name}': {_format_win32_error(error)}"
            )
            if config.strict_limits:
                raise OSError(error, msg)
            logger.warning(msg)

    def verify_sandbox_limits(self, container: SandboxContainer) -> dict:
        """
        Query the Windows NT Kernel directly via QueryInformationJobObject to
        verify that every mandatory requested limit is active on the Job Object.
        Raises OSError if strict_limits is enabled and any mandatory check fails.
        """
        config = container.config
        ext = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        ret_len = wintypes.DWORD(0)

        ctypes.set_last_error(0)
        ok_ext = kernel32.QueryInformationJobObject(
            container.job_handle,
            JobObjectExtendedLimitInformation,
            ctypes.byref(ext),
            ctypes.sizeof(ext),
            ctypes.byref(ret_len),
        )
        if not ok_ext:
            err = ctypes.get_last_error()
            raise OSError(
                err,
                f"QueryInformationJobObject(ExtendedLimit) failed: {_format_win32_error(err)}",
            )

        actual_flags = int(ext.BasicLimitInformation.LimitFlags)
        failures: List[str] = []

        per_proc_mb = config.effective_per_process_memory_mb()
        if per_proc_mb is not None:
            expected_bytes = int(per_proc_mb) * 1024 * 1024
            if not (actual_flags & JOB_OBJECT_LIMIT_PROCESS_MEMORY):
                failures.append("JOB_OBJECT_LIMIT_PROCESS_MEMORY flag not active")
            elif int(ext.ProcessMemoryLimit) != expected_bytes:
                failures.append(
                    f"ProcessMemoryLimit mismatch: expected {expected_bytes}, got {ext.ProcessMemoryLimit}"
                )

        if config.job_memory_limit_mb is not None:
            expected_bytes = int(config.job_memory_limit_mb) * 1024 * 1024
            if not (actual_flags & JOB_OBJECT_LIMIT_JOB_MEMORY):
                failures.append("JOB_OBJECT_LIMIT_JOB_MEMORY flag not active")
            elif int(ext.JobMemoryLimit) != expected_bytes:
                failures.append(
                    f"JobMemoryLimit mismatch: expected {expected_bytes}, got {ext.JobMemoryLimit}"
                )

        if config.max_active_processes is not None:
            if not (actual_flags & JOB_OBJECT_LIMIT_ACTIVE_PROCESS):
                failures.append("JOB_OBJECT_LIMIT_ACTIVE_PROCESS flag not active")
            elif int(ext.BasicLimitInformation.ActiveProcessLimit) != int(config.max_active_processes):
                failures.append(
                    f"ActiveProcessLimit mismatch: expected {config.max_active_processes}, "
                    f"got {ext.BasicLimitInformation.ActiveProcessLimit}"
                )

        if config.kill_on_close:
            if not (actual_flags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE):
                failures.append("JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE flag not active")

        if config.max_working_set_mb is not None:
            if not (actual_flags & JOB_OBJECT_LIMIT_WORKINGSET):
                failures.append("JOB_OBJECT_LIMIT_WORKINGSET flag not active")

        cpu_verified_pct: Optional[int] = None
        cpu_hard_cap_active = False
        if config.cpu_rate_percent is not None:
            cpu_info = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
            cpu_ret = wintypes.DWORD(0)
            ctypes.set_last_error(0)
            ok_cpu = kernel32.QueryInformationJobObject(
                container.job_handle,
                JobObjectCpuRateControlInformation,
                ctypes.byref(cpu_info),
                ctypes.sizeof(cpu_info),
                ctypes.byref(cpu_ret),
            )
            if not ok_cpu:
                err = ctypes.get_last_error()
                failures.append(
                    f"QueryInformationJobObject(CpuRateControl) failed: {_format_win32_error(err)}"
                )
            else:
                expected_flags = (
                    JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
                )
                expected_rate = max(1, min(100, int(config.cpu_rate_percent))) * 100
                if (int(cpu_info.ControlFlags) & expected_flags) != expected_flags:
                    failures.append(
                        f"CPU hard-cap flags mismatch: got 0x{cpu_info.ControlFlags:X}"
                    )
                elif int(cpu_info.CpuRate) != expected_rate:
                    failures.append(
                        f"CpuRate mismatch: expected {expected_rate}, got {cpu_info.CpuRate}"
                    )
                else:
                    cpu_verified_pct = int(cpu_info.CpuRate) // 100
                    cpu_hard_cap_active = True

        if failures and config.strict_limits:
            raise OSError(
                f"Mandatory Job Object limit verification failed for '{config.name}': "
                + "; ".join(failures)
            )

        return {
            "verified": len(failures) == 0,
            "verification_errors": failures,
            "limit_flags_hex": hex(actual_flags),
            "process_memory_limit_bytes": int(ext.ProcessMemoryLimit) if (actual_flags & JOB_OBJECT_LIMIT_PROCESS_MEMORY) else None,
            "process_memory_limit_mb": round(int(ext.ProcessMemoryLimit) / (1024 * 1024), 2) if (actual_flags & JOB_OBJECT_LIMIT_PROCESS_MEMORY) else None,
            "job_memory_limit_bytes": int(ext.JobMemoryLimit) if (actual_flags & JOB_OBJECT_LIMIT_JOB_MEMORY) else None,
            "job_memory_limit_mb": round(int(ext.JobMemoryLimit) / (1024 * 1024), 2) if (actual_flags & JOB_OBJECT_LIMIT_JOB_MEMORY) else None,
            "active_process_limit": int(ext.BasicLimitInformation.ActiveProcessLimit) if (actual_flags & JOB_OBJECT_LIMIT_ACTIVE_PROCESS) else None,
            "kill_on_job_close": bool(actual_flags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE),
            "working_set_limit_active": bool(actual_flags & JOB_OBJECT_LIMIT_WORKINGSET),
            "working_set_max_mb": round(int(ext.BasicLimitInformation.MaximumWorkingSetSize) / (1024 * 1024), 2) if (actual_flags & JOB_OBJECT_LIMIT_WORKINGSET) else None,
            "cpu_hard_cap_active": cpu_hard_cap_active,
            "cpu_rate_percent": cpu_verified_pct,
        }

    # ── Process Assignment & Identity Verification ────────────────────────

    def assign_process(
        self,
        sandbox_name: str,
        pid: int,
        expected_create_time_100ns: Optional[int] = None,
    ) -> dict:
        """
        Assign an existing process (by PID) to a named sandbox Job Object using
        least-privilege process access rights.

        Detects whether the process already belongs to another Job Object
        (nested job scenario), confirms actual membership via IsProcessInJob,
        and validates exact creation time when provided.
        """
        with self._lock:
            container = self._sandboxes.get(sandbox_name)
            if not container or not container.is_active:
                raise ValueError(f"Sandbox '{sandbox_name}' not found or inactive")

            if is_critical_process(pid):
                raise PermissionError(
                    f"Refusing to assign protected or backend process PID {pid} to sandbox '{sandbox_name}'"
                )

            ctypes.set_last_error(0)
            proc_handle = kernel32.OpenProcess(ACCESS_ASSIGN_JOB, False, pid)
            if not proc_handle:
                error = ctypes.get_last_error()
                raise OSError(
                    error,
                    f"OpenProcess({pid}) failed: {_format_win32_error(error)}",
                )

            try:
                # 1. Verify exact process creation time to prevent PID-reuse races
                ct, et, kt, ut = FILETIME(), FILETIME(), FILETIME(), FILETIME()
                if not kernel32.GetProcessTimes(
                    proc_handle, ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut)
                ):
                    error = ctypes.get_last_error()
                    raise OSError(
                        error,
                        f"GetProcessTimes({pid}) failed: {_format_win32_error(error)}",
                    )
                actual_create_time = ct.to_int()
                if (
                    expected_create_time_100ns is not None
                    and actual_create_time != expected_create_time_100ns
                ):
                    raise ValueError(
                        f"Process identity mismatch for PID {pid}: expected creation time "
                        f"{expected_create_time_100ns}, actual {actual_create_time}"
                    )

                # 2. Detect existing Job Object membership (nested job check)
                in_any_job = wintypes.BOOL(False)
                if not kernel32.IsProcessInJob(proc_handle, None, ctypes.byref(in_any_job)):
                    error = ctypes.get_last_error()
                    raise OSError(
                        error,
                        f"IsProcessInJob({pid}, NULL) failed: {_format_win32_error(error)}",
                    )
                already_in_job = bool(in_any_job.value)

                # 3. Assign to our dedicated Job Object (nested assignment on Windows 8+/10/11)
                ctypes.set_last_error(0)
                success = kernel32.AssignProcessToJobObject(
                    container.job_handle, proc_handle
                )
                if not success:
                    error = ctypes.get_last_error()
                    if already_in_job and error == ERROR_ACCESS_DENIED:
                        raise PermissionError(
                            f"PID {pid} already belongs to a parent Job Object that disallows "
                            f"nested Job Object assignment on this Windows configuration "
                            f"({_format_win32_error(error)}). Refusing to bypass existing job restrictions."
                        )
                    raise OSError(
                        error,
                        f"AssignProcessToJobObject(PID {pid}) failed: {_format_win32_error(error)}",
                    )

                # 4. Confirm the kernel actually recorded membership in THIS Job Object
                in_this_job = wintypes.BOOL(False)
                if not kernel32.IsProcessInJob(
                    proc_handle, container.job_handle, ctypes.byref(in_this_job)
                ) or not in_this_job.value:
                    raise OSError(
                        f"Post-assignment verification failed: PID {pid} is not confirmed in "
                        f"Job Object '{container.job_name}'"
                    )

                if pid not in container.assigned_pids:
                    container.assigned_pids.append(pid)
                container.assigned_process_meta[pid] = actual_create_time
                if already_in_job:
                    container.nested_job_detected = True

                self._log_decision(
                    action="ASSIGN_PROCESS",
                    sandbox=sandbox_name,
                    pid=pid,
                    details=(
                        f"Process {pid} (create_time={actual_create_time}) assigned & verified "
                        f"in sandbox '{sandbox_name}' (nested_job={already_in_job})"
                    ),
                )
                logger.info(
                    f"PID {pid} assigned & verified in sandbox '{sandbox_name}' "
                    f"(nested_job={already_in_job})"
                )
                return {
                    "assigned": True,
                    "pid": pid,
                    "create_time_100ns": actual_create_time,
                    "nested_job_detected": already_in_job,
                    "in_job_verified": True,
                    "job_name": container.job_name,
                }
            finally:
                kernel32.CloseHandle(proc_handle)

    def verify_process_in_sandbox(
        self,
        sandbox_name: str,
        pid: int,
        expected_create_time_100ns: Optional[int] = None,
    ) -> dict:
        """
        Verify via Win32 IsProcessInJob + GetProcessTimes that `pid` is currently
        a live member of `sandbox_name`'s Job Object (e.g., inherited by a child worker).
        """
        with self._lock:
            container = self._sandboxes.get(sandbox_name)
            if not container or not container.is_active:
                raise ValueError(f"Sandbox '{sandbox_name}' not found or inactive")

            ctypes.set_last_error(0)
            proc_handle = kernel32.OpenProcess(ACCESS_INSPECT_ONLY, False, pid)
            if not proc_handle:
                error = ctypes.get_last_error()
                raise OSError(
                    error,
                    f"OpenProcess({pid}) failed while verifying sandbox membership: {_format_win32_error(error)}",
                )

            try:
                ct, et, kt, ut = FILETIME(), FILETIME(), FILETIME(), FILETIME()
                if not kernel32.GetProcessTimes(
                    proc_handle, ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut)
                ):
                    error = ctypes.get_last_error()
                    raise OSError(
                        error,
                        f"GetProcessTimes({pid}) failed: {_format_win32_error(error)}",
                    )
                actual_create_time = ct.to_int()
                if (
                    expected_create_time_100ns is not None
                    and actual_create_time != expected_create_time_100ns
                ):
                    return {
                        "in_job": False,
                        "identity_match": False,
                        "pid": pid,
                        "expected_create_time_100ns": expected_create_time_100ns,
                        "actual_create_time_100ns": actual_create_time,
                        "reason": "Process creation time mismatch (possible PID reuse)",
                    }

                in_this_job = wintypes.BOOL(False)
                if not kernel32.IsProcessInJob(
                    proc_handle, container.job_handle, ctypes.byref(in_this_job)
                ):
                    error = ctypes.get_last_error()
                    raise OSError(
                        error,
                        f"IsProcessInJob({pid}) failed: {_format_win32_error(error)}",
                    )

                is_member = bool(in_this_job.value)
                if is_member:
                    if pid not in container.assigned_pids:
                        container.assigned_pids.append(pid)
                    container.assigned_process_meta[pid] = actual_create_time

                return {
                    "in_job": is_member,
                    "identity_match": True,
                    "pid": pid,
                    "create_time_100ns": actual_create_time,
                    "sandbox_name": sandbox_name,
                    "job_name": container.job_name,
                }
            finally:
                kernel32.CloseHandle(proc_handle)

    def get_job_process_ids(self, sandbox_name: str) -> List[int]:
        """Query the live list of member PIDs directly from the Windows Job Object."""
        with self._lock:
            container = self._sandboxes.get(sandbox_name)
            if not container or not container.is_active:
                return []

            capacity = 32
            for _ in range(4):
                class _PIDList(ctypes.Structure):
                    _fields_ = [
                        ("NumberOfAssignedProcesses", wintypes.DWORD),
                        ("NumberOfProcessIdsInList", wintypes.DWORD),
                        ("ProcessIdList", ctypes.c_size_t * capacity),
                    ]

                buf = _PIDList()
                ret_len = wintypes.DWORD(0)
                ctypes.set_last_error(0)
                ok = kernel32.QueryInformationJobObject(
                    container.job_handle,
                    JobObjectBasicProcessIdList,
                    ctypes.byref(buf),
                    ctypes.sizeof(buf),
                    ctypes.byref(ret_len),
                )
                if ok:
                    return [int(buf.ProcessIdList[i]) for i in range(buf.NumberOfProcessIdsInList)]
                err = ctypes.get_last_error()
                if err == ERROR_MORE_DATA:
                    capacity = max(capacity * 2, int(buf.NumberOfAssignedProcesses) + 8)
                    continue
                return []
            return []

    def terminate_sandbox_process(
        self,
        sandbox_name: str,
        pid: int,
        expected_create_time_100ns: int,
        exit_code: int = 0xDEAD,
        wait_timeout_ms: int = 5000,
    ) -> dict:
        """
        Selectively terminate ONLY ONE verified process inside an active sandbox
        Job Object using TerminateProcess (NEVER TerminateJobObject).

        Validates:
          1. Sandbox exists and is active.
          2. PID is not a critical/protected OS process or the backend itself.
          3. Exact PID + process creation time matches expected_create_time_100ns.
          4. Process is verified by the NT Kernel to belong to this Job Object.
          5. Calls TerminateProcess on the least-privilege handle and waits for exit.
          6. Leaves the Job Object active for the supervisor and surviving workers.
        """
        with self._lock:
            container = self._sandboxes.get(sandbox_name)
            if not container or not container.is_active:
                raise ValueError(f"Sandbox '{sandbox_name}' not found or inactive")

            if is_critical_process(pid):
                raise PermissionError(
                    f"Refusing to terminate protected or backend process PID {pid}"
                )

            ctypes.set_last_error(0)
            proc_handle = kernel32.OpenProcess(ACCESS_TERMINATE_VERIFY, False, pid)
            if not proc_handle:
                error = ctypes.get_last_error()
                raise OSError(
                    error,
                    f"OpenProcess({pid}) for selective termination failed: {_format_win32_error(error)}",
                )

            try:
                # Validate exact creation time
                ct, et, kt, ut = FILETIME(), FILETIME(), FILETIME(), FILETIME()
                if not kernel32.GetProcessTimes(
                    proc_handle, ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut)
                ):
                    error = ctypes.get_last_error()
                    raise OSError(
                        error,
                        f"GetProcessTimes({pid}) failed: {_format_win32_error(error)}",
                    )
                actual_create_time = ct.to_int()
                if actual_create_time != expected_create_time_100ns:
                    raise PermissionError(
                        f"Refusing selective termination: PID {pid} creation time mismatch "
                        f"(expected {expected_create_time_100ns}, actual {actual_create_time})"
                    )

                # Validate actual Job Object membership
                in_this_job = wintypes.BOOL(False)
                if not kernel32.IsProcessInJob(
                    proc_handle, container.job_handle, ctypes.byref(in_this_job)
                ) or not in_this_job.value:
                    raise PermissionError(
                        f"Refusing selective termination: PID {pid} is not a member of "
                        f"sandbox '{sandbox_name}' ({container.job_name})"
                    )

                # Terminate ONLY this single process (never TerminateJobObject)
                ctypes.set_last_error(0)
                ok_term = kernel32.TerminateProcess(proc_handle, wintypes.UINT(exit_code))
                if not ok_term:
                    error = ctypes.get_last_error()
                    # Check if already exited concurrently
                    code = wintypes.DWORD(0)
                    if kernel32.GetExitCodeProcess(proc_handle, ctypes.byref(code)) and code.value != STILL_ACTIVE:
                        pass
                    else:
                        raise OSError(
                            error,
                            f"TerminateProcess({pid}) failed: {_format_win32_error(error)}",
                        )

                # Confirm OS process exit
                wait_rc = kernel32.WaitForSingleObject(proc_handle, wintypes.DWORD(wait_timeout_ms))
                exited = wait_rc == WAIT_OBJECT_0
                actual_exit_code = wintypes.DWORD(0)
                kernel32.GetExitCodeProcess(proc_handle, ctypes.byref(actual_exit_code))

                if exited and pid in container.assigned_pids:
                    container.assigned_pids = [p for p in container.assigned_pids if p != pid]
                container.assigned_process_meta.pop(pid, None)

                self._log_decision(
                    action="SELECTIVE_TERMINATE_PROCESS",
                    sandbox=sandbox_name,
                    pid=pid,
                    details=(
                        f"Selectively terminated PID {pid} (create_time={actual_create_time}) "
                        f"via TerminateProcess; exited={exited}, exit_code={actual_exit_code.value}; "
                        f"Job Object '{container.job_name}' kept ACTIVE"
                    ),
                )
                logger.info(
                    f"Selectively terminated PID {pid} in sandbox '{sandbox_name}' "
                    f"(exited={exited}, exit_code={actual_exit_code.value}); sandbox remains active"
                )
                return {
                    "terminated": True,
                    "exited_confirmed": exited,
                    "pid": pid,
                    "create_time_100ns": actual_create_time,
                    "exit_code": int(actual_exit_code.value),
                    "sandbox_name": sandbox_name,
                    "sandbox_still_active": container.is_active,
                    "remaining_job_pids": self.get_job_process_ids(sandbox_name),
                }
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
            creationflags=CREATE_NEW_CONSOLE,
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

    # ── Process Scheduling Priority & Affinity Control ────────────────────

    def set_process_priority(self, pid: int, priority: str) -> bool:
        """Set the Windows OS scheduling priority class of a process by PID."""
        if is_critical_process(pid):
            logger.warning(f"Protected system process PID {pid} cannot have priority changed")
            return False

        pc = PRIORITY_CLASS_MAP.get(priority.lower())
        if pc is None:
            raise ValueError(f"Unknown priority class: {priority}")

        proc_handle = kernel32.OpenProcess(ACCESS_SET_PRIORITY_AFFINITY, False, pid)
        if not proc_handle:
            return False

        try:
            success = kernel32.SetPriorityClass(proc_handle, pc)
            if success:
                self._log_decision(
                    action="SET_PRIORITY",
                    pid=pid,
                    details=f"Process {pid} OS scheduling priority set to '{priority}'",
                )
            return bool(success)
        finally:
            kernel32.CloseHandle(proc_handle)

    def get_process_priority(self, pid: int) -> Optional[str]:
        """Get the Windows OS scheduling priority class name of a process by PID."""
        proc_handle = kernel32.OpenProcess(ACCESS_INSPECT_ONLY, False, pid)
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
        if is_critical_process(pid):
            logger.warning(f"Protected system process PID {pid} cannot have affinity changed")
            return False

        proc_handle = kernel32.OpenProcess(ACCESS_SET_PRIORITY_AFFINITY, False, pid)
        if not proc_handle:
            return False
        try:
            success = kernel32.SetProcessAffinityMask(proc_handle, ctypes.c_size_t(core_mask))
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
        Releases physical RAM pages back to the OS.
        """
        if is_critical_process(pid):
            return False

        proc_handle = kernel32.OpenProcess(ACCESS_TRIM_WORKING_SET, False, pid)
        if not proc_handle:
            return False
        try:
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
        """Query kernel accounting and peak memory information for a Job Object."""
        with self._lock:
            container = self._sandboxes.get(sandbox_name)
            if not container or not container.is_active:
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

            ext = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            ext_ret = wintypes.DWORD(0)
            ok_ext = kernel32.QueryInformationJobObject(
                container.job_handle,
                JobObjectExtendedLimitInformation,
                ctypes.byref(ext),
                ctypes.sizeof(ext),
                ctypes.byref(ext_ret),
            )

            peak_proc_mb = round(int(ext.PeakProcessMemoryUsed) / (1024 * 1024), 2) if ok_ext else 0.0
            peak_job_mb = round(int(ext.PeakJobMemoryUsed) / (1024 * 1024), 2) if ok_ext else 0.0

            return {
                "total_user_time_ms": info.TotalUserTime // 10000,
                "total_kernel_time_ms": info.TotalKernelTime // 10000,
                "total_user_time_sec": info.TotalUserTime / 10_000_000.0,
                "total_kernel_time_sec": info.TotalKernelTime / 10_000_000.0,
                "total_page_faults": info.TotalPageFaultCount,
                "total_page_fault_count": info.TotalPageFaultCount,
                "total_processes": info.TotalProcesses,
                "active_processes": info.ActiveProcesses,
                "total_terminated": info.TotalTerminatedProcesses,
                "peak_process_memory_mb": peak_proc_mb,
                "peak_job_memory_mb": peak_job_mb,
            }

    # ── Sandbox Teardown & Cleanup ────────────────────────────────────────

    def destroy_sandbox(self, sandbox_name: str, terminate_processes: bool = True) -> bool:
        """
        Destroy a sandbox Job Object.

        Use whole-job TerminateJobObject ONLY for explicit sandbox shutdown,
        emergency watchdog cleanup, or application teardown — NEVER for
        ordinary selective deadlock victim termination.
        """
        with self._lock:
            container = self._sandboxes.get(sandbox_name)
            if not container:
                return False

            if terminate_processes:
                kernel32.TerminateJobObject(container.job_handle, 1)
            kernel32.CloseHandle(container.job_handle)
            container.is_active = False

            del self._sandboxes[sandbox_name]
            self._log_decision(
                action="DESTROY_SANDBOX",
                sandbox=sandbox_name,
                details=(
                    f"Sandbox '{sandbox_name}' destroyed "
                    f"({'whole-job TerminateJobObject + CloseHandle' if terminate_processes else 'CloseHandle'})"
                ),
            )
            logger.info(f"Sandbox '{sandbox_name}' destroyed (terminate_processes={terminate_processes})")
            return True

    def cleanup_all_sandboxes(self):
        """Terminate and clean up all active Job Object sandboxes on shutdown."""
        with self._lock:
            names = list(self._sandboxes.keys())
            for name in names:
                try:
                    self.destroy_sandbox(name, terminate_processes=True)
                except Exception as e:
                    logger.warning(f"Error destroying sandbox '{name}' during cleanup: {e}")
            logger.info("All sandboxes cleaned up cleanly")

    def list_sandboxes(self) -> List[dict]:
        """List all active sandboxes with their configuration, verified kernel limits, and status."""
        with self._lock:
            result = []
            for name, container in self._sandboxes.items():
                accounting = self.get_job_accounting(name)
                kernel_pids = self.get_job_process_ids(name)

                # Merge kernel-reported member PIDs with tracked alive PIDs
                alive_set = set(kernel_pids)
                for pid in container.assigned_pids:
                    if psutil.pid_exists(pid):
                        alive_set.add(pid)
                alive_pids = sorted(alive_set)
                container.assigned_pids = alive_pids

                per_proc_mb = container.config.effective_per_process_memory_mb()
                result.append({
                    "name": name,
                    "job_name": container.job_name,
                    "is_active": container.is_active,
                    "created_at": container.created_at,
                    "cpu_rate_percent": container.config.cpu_rate_percent,
                    "memory_limit_mb": per_proc_mb,
                    "per_process_memory_limit_mb": per_proc_mb,
                    "job_memory_limit_mb": container.config.job_memory_limit_mb,
                    "max_active_processes": container.config.max_active_processes,
                    "priority_class": container.config.priority_class,
                    "kill_on_close": container.config.kill_on_close,
                    "max_working_set_mb": container.config.max_working_set_mb,
                    "nested_job_detected": container.nested_job_detected,
                    "verified_limits": container.verified_limits,
                    "security_boundary_note": (
                        "Job Object resource containment only (CPU, memory, active processes, kill-on-close); "
                        "not a filesystem/network/token security sandbox."
                    ),
                    "config": {
                        "cpu_rate_percent": container.config.cpu_rate_percent,
                        "memory_limit_mb": per_proc_mb,
                        "per_process_memory_limit_mb": per_proc_mb,
                        "job_memory_limit_mb": container.config.job_memory_limit_mb,
                        "max_active_processes": container.config.max_active_processes,
                        "priority_class": container.config.priority_class,
                        "kill_on_close": container.config.kill_on_close,
                        "max_working_set_mb": container.config.max_working_set_mb,
                    },
                    "assigned_pids": alive_pids,
                    "kernel_member_pids": kernel_pids,
                    "accounting": accounting,
                })
            return result

    def update_sandbox_limits(self, sandbox_name: str, **kwargs) -> bool:
        """
        Dynamically update limits on an active sandbox and re-verify via kernel query.
        """
        with self._lock:
            container = self._sandboxes.get(sandbox_name)
            if not container or not container.is_active:
                return False

            config = container.config
            if "cpu_rate_percent" in kwargs:
                config.cpu_rate_percent = kwargs["cpu_rate_percent"]
                if config.cpu_rate_percent is not None:
                    self._apply_cpu_rate_limit(container)
            if "per_process_memory_limit_mb" in kwargs:
                config.per_process_memory_limit_mb = kwargs["per_process_memory_limit_mb"]
                config.memory_limit_mb = kwargs["per_process_memory_limit_mb"]
            elif "memory_limit_mb" in kwargs:
                config.memory_limit_mb = kwargs["memory_limit_mb"]
                config.per_process_memory_limit_mb = kwargs["memory_limit_mb"]
            if "job_memory_limit_mb" in kwargs:
                config.job_memory_limit_mb = kwargs["job_memory_limit_mb"]
            if "max_active_processes" in kwargs:
                config.max_active_processes = kwargs["max_active_processes"]
            if "priority_class" in kwargs:
                config.priority_class = kwargs["priority_class"]
            if "max_working_set_mb" in kwargs:
                config.max_working_set_mb = kwargs["max_working_set_mb"]

            self._apply_extended_limits(container)
            container.verified_limits = self.verify_sandbox_limits(container)
            self._log_decision(
                action="UPDATE_SANDBOX",
                sandbox=sandbox_name,
                details=f"Limits updated & verified: {kwargs}",
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
        if len(self._decision_log) > 500:
            self._decision_log = self._decision_log[-500:]

    def get_decision_log(self, limit: int = 50) -> List[dict]:
        """Return the most recent decision log entries."""
        return list(reversed(self._decision_log[-limit:]))

    def get_sandbox(self, sandbox_name: str) -> Optional[SandboxContainer]:
        """Return a sandbox container by name."""
        with self._lock:
            return self._sandboxes.get(sandbox_name)
