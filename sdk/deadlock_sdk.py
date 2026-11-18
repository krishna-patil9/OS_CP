"""
Windows-Native Real-Time Deadlock Monitoring SDK

Provides:
  - Exact Win32 process & thread identity (PID, TID, 64-bit FILETIME creation time)
  - Fail-closed workload registration & Windows Job Object containment verification
  - Per-run secret token authentication (X-Run-Token)
  - Win32 Named Mutex wrapper (CreateMutexW / WaitForSingleObject / ReleaseMutex)
    with accurate WAIT_ABANDONED (0x00000080) ownership, state-validation, and
    same-thread release enforcement
  - Win32 Named Event wrapper (CreateEventW / SetEvent / WaitForSingleObject)
    for worker startup-gate synchronization
  - Bounded-timeout, monotonically sequenced HTTP event transport that never
    indefinitely blocks OS synchronization calls and reports disconnection honestly
"""

import ctypes
import ctypes.wintypes as wintypes
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# ──────────────────────────────────────────────────────────────────────────────
# Win32 Constants & Bindings
# ──────────────────────────────────────────────────────────────────────────────

WAIT_OBJECT_0  = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT   = 0x00000102
WAIT_FAILED    = 0xFFFFFFFF
INFINITE       = 0xFFFFFFFF


class FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]

    def to_int(self) -> int:
        return (int(self.dwHighDateTime) << 32) | int(self.dwLowDateTime)


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

kernel32.GetCurrentProcess.argtypes = []
kernel32.GetCurrentProcess.restype = wintypes.HANDLE

kernel32.GetCurrentProcessId.argtypes = []
kernel32.GetCurrentProcessId.restype = wintypes.DWORD

kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

kernel32.GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
]
kernel32.GetProcessTimes.restype = wintypes.BOOL

kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE

kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
kernel32.ReleaseMutex.restype = wintypes.BOOL

kernel32.CreateEventW.argtypes = [
    wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR
]
kernel32.CreateEventW.restype = wintypes.HANDLE

kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.SetEvent.restype = wintypes.BOOL

kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
kernel32.ResetEvent.restype = wintypes.BOOL

kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


class ContainmentError(RuntimeError):
    """Raised when mandatory Windows Job Object containment or verification fails."""


@dataclass
class AcquireResult:
    acquired: bool
    abandoned: bool
    timed_out: bool
    wait_code: int
    wait_result: str
    owner_tid: Optional[int] = None


def get_current_process_identity() -> dict:
    """Return exact Win32 PID, TID, and 64-bit FILETIME creation timestamp."""
    proc_handle = kernel32.GetCurrentProcess()
    pid = int(kernel32.GetCurrentProcessId())
    tid = int(kernel32.GetCurrentThreadId())
    ct, et, kt, ut = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    ctypes.set_last_error(0)
    ok = kernel32.GetProcessTimes(
        proc_handle, ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut)
    )
    if not ok:
        err = ctypes.get_last_error()
        raise OSError(err, f"GetProcessTimes failed with Win32 error {err}")
    return {
        "pid": pid,
        "tid": tid,
        "create_time_100ns": ct.to_int(),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Native Windows Synchronization Primitives
# ──────────────────────────────────────────────────────────────────────────────

class Win32NamedEvent:
    """Manual-reset Windows Named Event for deterministic cross-process startup gates."""

    def __init__(self, name: str, manual_reset: bool = True, initial_state: bool = False):
        self.name = name
        ctypes.set_last_error(0)
        self.handle = kernel32.CreateEventW(None, bool(manual_reset), bool(initial_state), name)
        if not self.handle:
            err = ctypes.get_last_error()
            raise OSError(err, f"CreateEventW('{name}') failed with Win32 error {err}")

    def set(self):
        ctypes.set_last_error(0)
        if not kernel32.SetEvent(self.handle):
            err = ctypes.get_last_error()
            raise OSError(err, f"SetEvent('{self.name}') failed with Win32 error {err}")

    def wait(self, timeout_ms: int = INFINITE) -> bool:
        rc = kernel32.WaitForSingleObject(self.handle, wintypes.DWORD(timeout_ms))
        if rc == WAIT_OBJECT_0:
            return True
        if rc == WAIT_TIMEOUT:
            return False
        err = ctypes.get_last_error()
        raise OSError(err, f"WaitForSingleObject(Event '{self.name}') failed: rc={rc}, err={err}")

    def close(self):
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None


class Win32NamedMutex:
    """
    Native Windows Named Mutex wrapper using kernel32 CreateMutexW,
    WaitForSingleObject, ReleaseMutex, and CloseHandle.
    """

    def __init__(self, os_mutex_name: str):
        self.os_mutex_name = os_mutex_name
        ctypes.set_last_error(0)
        self.handle = kernel32.CreateMutexW(None, False, os_mutex_name)
        if not self.handle:
            err = ctypes.get_last_error()
            raise OSError(err, f"CreateMutexW('{os_mutex_name}') failed with Win32 error {err}")
        self._owner_tid: Optional[int] = None
        self._acquired_abandoned: bool = False

    def acquire(self, timeout_ms: int = INFINITE) -> AcquireResult:
        ctypes.set_last_error(0)
        rc = int(kernel32.WaitForSingleObject(self.handle, wintypes.DWORD(timeout_ms)))
        tid = int(kernel32.GetCurrentThreadId())

        if rc == WAIT_OBJECT_0:
            self._owner_tid = tid
            self._acquired_abandoned = False
            return AcquireResult(
                acquired=True,
                abandoned=False,
                timed_out=False,
                wait_code=rc,
                wait_result="WAIT_OBJECT_0",
                owner_tid=tid,
            )
        if rc == WAIT_ABANDONED:
            # Critical Windows semantics: WAIT_ABANDONED means THIS thread NOW OWNS the mutex
            # because the prior owning thread terminated without calling ReleaseMutex.
            self._owner_tid = tid
            self._acquired_abandoned = True
            return AcquireResult(
                acquired=True,
                abandoned=True,
                timed_out=False,
                wait_code=rc,
                wait_result="WAIT_ABANDONED",
                owner_tid=tid,
            )
        if rc == WAIT_TIMEOUT:
            return AcquireResult(
                acquired=False,
                abandoned=False,
                timed_out=True,
                wait_code=rc,
                wait_result="WAIT_TIMEOUT",
                owner_tid=None,
            )
        err = ctypes.get_last_error()
        raise OSError(
            err,
            f"WaitForSingleObject('{self.os_mutex_name}') failed: rc=0x{rc:X}, err={err}",
        )

    def release(self) -> bool:
        """
        Release the Windows Named Mutex from the SAME thread that acquired it.
        Returns True if the released hold originated from WAIT_ABANDONED.
        """
        current_tid = int(kernel32.GetCurrentThreadId())
        if self._owner_tid is not None and current_tid != self._owner_tid:
            raise RuntimeError(
                f"Mutex '{self.os_mutex_name}' must be released by owning thread "
                f"TID={self._owner_tid}, not TID={current_tid}"
            )
        was_abandoned = self._acquired_abandoned
        ctypes.set_last_error(0)
        ok = kernel32.ReleaseMutex(self.handle)
        if not ok:
            err = ctypes.get_last_error()
            raise OSError(err, f"ReleaseMutex('{self.os_mutex_name}') failed with Win32 error {err}")
        self._owner_tid = None
        self._acquired_abandoned = False
        return was_abandoned

    def close(self):
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None


# ──────────────────────────────────────────────────────────────────────────────
# Backend Monitoring Client
# ──────────────────────────────────────────────────────────────────────────────

class DeadlockMonitorClient:
    """
    HTTP client for real-time deadlock registration, Job Object verification,
    and bounded-timeout sequenced synchronization event reporting.
    """

    def __init__(
        self,
        server_url: str = "http://127.0.0.1:8000",
        run_id: Optional[str] = None,
        run_token: Optional[str] = None,
        registration_timeout_sec: float = 5.0,
        event_timeout_sec: float = 1.5,
    ):
        self.server_url = server_url.rstrip("/")
        self.run_id = run_id
        self.run_token = run_token
        self.registration_timeout_sec = registration_timeout_sec
        self.event_timeout_sec = event_timeout_sec

        self._seq_lock = threading.Lock()
        self._seq = 0
        self.monitoring_connected = True
        self.dropped_events = 0
        self.last_transport_error: Optional[str] = None

    def _next_seq(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Optional[dict] = None,
        timeout: float = 2.0,
        include_token: bool = True,
    ) -> dict:
        url = f"{self.server_url}{path}"
        headers = {"Content-Type": "application/json"}
        if include_token and self.run_token:
            headers["X-Run-Token"] = self.run_token
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise RuntimeError(f"HTTP {e.code} from {method} {path}: {body or e.reason}") from e
        except urllib.error.URLError as e:
            raise ConnectionError(f"Cannot reach backend {url}: {e.reason}") from e
        except TimeoutError as e:
            raise TimeoutError(f"Timed out after {timeout}s calling {method} {path}") from e

    # ── Fail-Closed Containment Registration ──────────────────────────────

    def register_run(
        self,
        workload_name: str,
        expected_workers: int,
        resources: List[dict],
        cpu_rate_percent: int = 20,
        job_memory_limit_mb: int = 512,
        per_process_memory_limit_mb: int = 256,
        max_active_processes: int = 4,
        max_runtime_seconds: int = 120,
        priority_class: Optional[str] = None,
        kill_on_close: bool = True,
    ) -> dict:
        """
        FIRST application-level call before creating workers.
        Raises ContainmentError if backend cannot create, verify, and attach the Job Object.
        """
        ident = get_current_process_identity()
        payload = {
            "supervisor_pid": ident["pid"],
            "supervisor_tid": ident["tid"],
            "supervisor_create_time_100ns": ident["create_time_100ns"],
            "workload_name": workload_name,
            "expected_workers": expected_workers,
            "resources": resources,
            "cpu_rate_percent": cpu_rate_percent,
            "job_memory_limit_mb": job_memory_limit_mb,
            "per_process_memory_limit_mb": per_process_memory_limit_mb,
            "max_active_processes": max_active_processes,
            "max_runtime_seconds": max_runtime_seconds,
            "priority_class": priority_class,
            "kill_on_close": kill_on_close,
        }
        try:
            resp = self._request_json(
                "POST",
                "/api/deadlock/live/runs/register",
                payload=payload,
                timeout=self.registration_timeout_sec,
                include_token=False,
            )
        except Exception as e:
            raise ContainmentError(
                f"Mandatory sandbox registration failed (aborting to avoid uncontained execution): {e}"
            ) from e

        # Verify mandatory acknowledgement fields strictly
        verified = resp.get("verified_limits") or {}
        if not resp.get("attached"):
            raise ContainmentError("Backend did not confirm Job Object attachment (attached != True)")
        if not resp.get("run_id") or not resp.get("run_token"):
            raise ContainmentError("Backend registration missing run_id or per-run authentication token")
        if not verified.get("verified"):
            raise ContainmentError(
                f"Mandatory Windows Job Object limits failed verification: {verified.get('verification_errors')}"
            )
        if not verified.get("cpu_hard_cap_active"):
            raise ContainmentError("CPU hard-cap control was not verified on the Windows Job Object")
        if verified.get("job_memory_limit_mb") is None or verified.get("process_memory_limit_mb") is None:
            raise ContainmentError("Per-process and aggregate job memory limits were not verified")
        if verified.get("active_process_limit") is None or not verified.get("kill_on_job_close"):
            raise ContainmentError("Active-process limit or kill-on-job-close was not verified")

        self.run_id = resp["run_id"]
        self.run_token = resp["run_token"]
        return resp

    def register_worker(
        self,
        worker_id: str,
        name: str,
        recovery_priority: int,
        max_claim: Dict[str, int],
    ) -> dict:
        """
        Register this child worker process BEFORE opening/passing the startup gate.
        Fail-closed if backend cannot verify Job Object inheritance.
        """
        if not self.run_id or not self.run_token:
            raise ContainmentError("Cannot register worker without active run_id and run_token")
        ident = get_current_process_identity()
        payload = {
            "worker_id": worker_id,
            "name": name,
            "pid": ident["pid"],
            "tid": ident["tid"],
            "create_time_100ns": ident["create_time_100ns"],
            "recovery_priority": recovery_priority,
            "max_claim": max_claim,
        }
        resp = self._request_json(
            "POST",
            f"/api/deadlock/live/runs/{self.run_id}/workers",
            payload=payload,
            timeout=self.registration_timeout_sec,
            include_token=True,
        )
        if not resp.get("registered") or not resp.get("in_job_verified"):
            raise ContainmentError(
                f"Backend refused worker '{worker_id}' registration or failed Job Object verification: {resp}"
            )
        return resp

    # ── Bounded-Timeout Event & Heartbeat Transport ───────────────────────

    def send_event(
        self,
        worker_id: str,
        event_type: str,
        resource_id: Optional[str] = None,
        os_mutex_name: Optional[str] = None,
        wait_result: Optional[str] = None,
        wait_code: Optional[int] = None,
        abandoned: bool = False,
        state_validated: bool = False,
        released_after_abandoned: bool = False,
        exit_code: Optional[int] = None,
        details: str = "",
    ) -> Optional[dict]:
        """
        Send a sequenced synchronization event with a strict bounded timeout so
        OS synchronization calls are never indefinitely blocked by HTTP issues.
        Reports monitoring disconnection honestly if backend is unreachable.
        """
        if not self.run_id or not self.run_token:
            return None

        ident = get_current_process_identity()
        seq = self._next_seq()
        payload = {
            "worker_id": worker_id,
            "pid": ident["pid"],
            "tid": ident["tid"],
            "create_time_100ns": ident["create_time_100ns"],
            "event_type": event_type,
            "resource_id": resource_id,
            "os_mutex_name": os_mutex_name,
            "seq": seq,
            "wait_result": wait_result,
            "wait_code": wait_code,
            "abandoned": abandoned,
            "state_validated": state_validated,
            "released_after_abandoned": released_after_abandoned,
            "exit_code": exit_code,
            "details": details,
        }
        try:
            resp = self._request_json(
                "POST",
                f"/api/deadlock/live/runs/{self.run_id}/events",
                payload=payload,
                timeout=self.event_timeout_sec,
                include_token=True,
            )
            if not self.monitoring_connected:
                self.monitoring_connected = True
                print(
                    f"[MONITORING RECONNECTED] Worker {worker_id} resumed backend reporting (seq={seq})",
                    flush=True,
                )
            return resp
        except Exception as e:
            self.monitoring_connected = False
            self.dropped_events += 1
            self.last_transport_error = str(e)
            print(
                f"[MONITORING DISCONNECTED] Worker {worker_id} failed to report '{event_type}' "
                f"(seq={seq}, timeout={self.event_timeout_sec}s): {e}",
                file=sys.stderr,
                flush=True,
            )
            return None

    def send_heartbeat(self, actor_role: str = "supervisor", worker_id: Optional[str] = None) -> Optional[dict]:
        if not self.run_id or not self.run_token:
            return None
        ident = get_current_process_identity()
        try:
            resp = self._request_json(
                "POST",
                f"/api/deadlock/live/runs/{self.run_id}/heartbeat",
                payload={
                    "actor_pid": ident["pid"],
                    "actor_role": actor_role,
                    "worker_id": worker_id,
                },
                timeout=self.event_timeout_sec,
                include_token=True,
            )
            self.monitoring_connected = True
            return resp
        except Exception as e:
            self.monitoring_connected = False
            self.last_transport_error = str(e)
            return None

    def get_run_status(self) -> Optional[dict]:
        if not self.run_id:
            return None
        try:
            return self._request_json(
                "GET",
                f"/api/deadlock/live/runs/{self.run_id}",
                timeout=self.event_timeout_sec,
                include_token=False,
            )
        except Exception:
            return None

    def complete_run(self, outcome_note: str = "Supervisor completed cleanly") -> Optional[dict]:
        if not self.run_id or not self.run_token:
            return None
        try:
            return self._request_json(
                "POST",
                f"/api/deadlock/live/runs/{self.run_id}/complete",
                payload={"outcome_note": outcome_note},
                timeout=self.registration_timeout_sec,
                include_token=True,
            )
        except Exception as e:
            print(f"[MONITORING WARNING] Failed to notify backend of run completion: {e}", file=sys.stderr, flush=True)
            return None


# ──────────────────────────────────────────────────────────────────────────────
# Instrumented Windows Named Mutex
# ──────────────────────────────────────────────────────────────────────────────

class InstrumentedMutex:
    """
    Combines a real Windows Named Mutex (Win32NamedMutex) with sequenced
    monitoring SDK event reporting (wait_start, acquired, abandoned_acquired,
    state_validated, released).
    """

    def __init__(
        self,
        client: DeadlockMonitorClient,
        worker_id: str,
        resource_id: str,
        os_mutex_name: str,
    ):
        self.client = client
        self.worker_id = worker_id
        self.resource_id = resource_id
        self.os_mutex_name = os_mutex_name
        self.mutex = Win32NamedMutex(os_mutex_name)

    def acquire(self, timeout_ms: int = INFINITE) -> AcquireResult:
        self.client.send_event(
            worker_id=self.worker_id,
            event_type="wait_start",
            resource_id=self.resource_id,
            os_mutex_name=self.os_mutex_name,
            details=f"Calling WaitForSingleObject on {self.os_mutex_name}",
        )

        result = self.mutex.acquire(timeout_ms=timeout_ms)

        if result.acquired and not result.abandoned:
            self.client.send_event(
                worker_id=self.worker_id,
                event_type="acquired",
                resource_id=self.resource_id,
                os_mutex_name=self.os_mutex_name,
                wait_result=result.wait_result,
                wait_code=result.wait_code,
                abandoned=False,
                details=f"Acquired {self.os_mutex_name} via WAIT_OBJECT_0",
            )
        elif result.acquired and result.abandoned:
            # Distinct WAIT_ABANDONED event: this thread now holds ownership of the abandoned mutex!
            self.client.send_event(
                worker_id=self.worker_id,
                event_type="abandoned_acquired",
                resource_id=self.resource_id,
                os_mutex_name=self.os_mutex_name,
                wait_result="WAIT_ABANDONED",
                wait_code=WAIT_ABANDONED,
                abandoned=True,
                details=(
                    f"Acquired ABANDONED mutex {self.os_mutex_name} (WAIT_ABANDONED=0x80); "
                    f"previous owner died without ReleaseMutex. Validating protected state before release."
                ),
            )
        elif result.timed_out:
            self.client.send_event(
                worker_id=self.worker_id,
                event_type="wait_timeout",
                resource_id=self.resource_id,
                os_mutex_name=self.os_mutex_name,
                wait_result="WAIT_TIMEOUT",
                wait_code=WAIT_TIMEOUT,
                details=f"WaitForSingleObject timed out on {self.os_mutex_name}",
            )

        return result

    def report_state_validated(self, details: str = "Protected shared state verified consistent"):
        self.client.send_event(
            worker_id=self.worker_id,
            event_type="state_validated",
            resource_id=self.resource_id,
            os_mutex_name=self.os_mutex_name,
            state_validated=True,
            details=details,
        )

    def release(self, details: str = ""):
        was_abandoned = self.mutex.release()
        self.client.send_event(
            worker_id=self.worker_id,
            event_type="released",
            resource_id=self.resource_id,
            os_mutex_name=self.os_mutex_name,
            released_after_abandoned=was_abandoned,
            details=details
            or (
                f"Released {self.os_mutex_name} from owning thread after WAIT_ABANDONED state validation"
                if was_abandoned
                else f"Released {self.os_mutex_name} via ReleaseMutex"
            ),
        )

    def close(self):
        self.mutex.close()
