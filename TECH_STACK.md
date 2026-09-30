# Tech Stack & Architecture — Smart Resource Manager & Application Isolation System

> OS Course Project | Windows NT Kernel Job Objects - Heuristic Policy Engine - Concurrency - Algorithmic Scheduling

---

## System Architecture Overview

```
+------------------------------------------------------------------------+
|                        BROWSER (Frontend)                              |
|   HTML5 + Vanilla CSS (Glassmorphism) + Vanilla JavaScript             |
|   WebSocket Client  <-->  REST API Client (fetch)                      |
+---------------------------+--------------------------------------------+
                            | HTTP/WebSocket  (port 8000)
+---------------------------v--------------------------------------------+
|                    FastAPI Backend (Python 3.x)                         |
|   REST API Routes + WebSocket Telemetry Stream (/ws/telemetry)         |
|   Pydantic Models for request/response validation                      |
|   Uvicorn ASGI server                                                  |
+---+---------------+---------------+---------------+--------------------+
    |               |               |               |
    v               v               v               v
Isolation      System          Deadlock        Simulator
Engine         Monitor         Engine          Engine
(Win32 APIs)   (psutil)        (Pure Python)   (Pure Python)
```

---

## Frontend Layer

| Component | Technology | Purpose |
|---|---|---|
| Structure | HTML5 (semantic elements) | Layout, tabs, panels, tables |
| Styling | Vanilla CSS (CSS variables, glassmorphism) | Dark mode UI, animations, gradients |
| Logic | Vanilla JavaScript (ES6+ IIFEs) | Button events, fetch calls, DOM rendering |
| Fonts | Google Fonts - Inter + JetBrains Mono | Typography |
| Real-time data | WebSocket API (new WebSocket(...)) | Live CPU/RAM telemetry every 500ms |
| HTTP calls | Fetch API (fetch('/api/...')) | REST calls to backend |
| Charts | Inline DOM rendering (div-based) | CPU/memory history graphs |

### Frontend JS Modules

| File | Responsibility |
|---|---|
| app.js | Tab routing, WebSocket lifecycle, toast notifications |
| telemetry.js | Updates CPU/RAM gauges, process table, history graphs |
| isolation.js | Job Object sandbox CRUD UI, sandbox detail cards |
| deadlock_graph.js | Canvas-based Wait-For Graph, Banker's Algorithm UI |
| simulator.js | MLFQ Gantt chart, page replacement matrix table |
| workloads.js | Launch/stop workloads, poll Active Spawned Workloads table |

---

## Backend Layer

| Component | Technology | Purpose |
|---|---|---|
| Web Framework | FastAPI (Python) | REST API routes, WebSocket endpoint |
| ASGI Server | Uvicorn | Runs FastAPI, handles async I/O |
| Data Validation | Pydantic (v1/v2) | Request body models, response schemas |
| Concurrency | asyncio + threading | Async HTTP + background daemon threads |

### Backend Python Files

| File | Responsibility |
|---|---|
| main.py | All FastAPI routes, startup/shutdown, workload launcher |
| isolation_engine.py | Windows NT Job Object creation and control |
| system_monitor.py | Real-time CPU/RAM telemetry collection |
| policy_engine.py | Autonomous resource management decisions |
| deadlock_engine.py | Cycle detection and Banker's Algorithm |
| simulator_engine.py | MLFQ scheduler and page replacement simulation |

---

## Feature 1: Job Object Sandbox (Isolation Engine)

**"How does the sandbox actually enforce limits?"**

This is the most OS-native feature. It uses raw Win32 API calls through Python's
ctypes library — no third-party library, direct kernel calls.

### Win32 APIs Called (directly via ctypes)

| Win32 API | What it does |
|---|---|
| kernel32.CreateJobObjectW() | Creates a new Windows NT Job Object (process container) |
| kernel32.SetInformationJobObject() | Sets limits: memory, CPU rate, process count |
| kernel32.AssignProcessToJobObject() | Puts a running process inside the Job Object cage |
| kernel32.QueryInformationJobObject() | Reads CPU time used, process count, memory consumed |
| kernel32.OpenProcess() | Gets a handle to a running process by PID |
| kernel32.TerminateProcess() | Force-kills a process |

### Win32 Structures Used

```
CPU Rate Control (Info class = JobObjectCpuRateControlInformation = 15):
  ControlFlags = JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | HARD_CAP  (0x1 | 0x4)
  CpuRate      = rate * 100   (e.g. 20% = 2000, in units of 0.01%)

Memory + Process Limits (Info class = JobObjectExtendedLimitInformation = 9):
  BasicLimitInformation.LimitFlags:
    JOB_OBJECT_LIMIT_JOB_MEMORY        = 0x0200  <- memory cap
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS    = 0x0008  <- max processes (fork-bomb guard)
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000  <- auto-kill on close
    JOB_OBJECT_LIMIT_PRIORITY_CLASS    = 0x0020  <- priority class
  JobMemoryLimit = limit_mb * 1024 * 1024
```

### Data Flow for Sandbox Creation

```
User clicks "Instantiate Job Object"
        |
        v
POST /api/sandboxes  ->  isolation_engine.create_sandbox()
        |
        |-- kernel32.CreateJobObjectW(NULL, "sandbox_name")
        |-- kernel32.SetInformationJobObject(job, CPU_RATE_INFO, ...)
        +-- kernel32.SetInformationJobObject(job, EXTENDED_LIMIT_INFO, ...)
        |
        v
Job Object handle stored in memory dict
        |
POST /api/workloads/launch -> subprocess.Popen(python_script)
        |
        +-- kernel32.AssignProcessToJobObject(job_handle, process_handle)
                |
                v
        Process is now inside the Job Object cage.
        Windows kernel enforces all limits automatically at hardware level.
```

### Python Libraries Used
- ctypes (Python stdlib) — calls DLLs directly, no wrapper
- ctypes.windll.kernel32 — the actual Windows kernel DLL
- psutil — reads process CPU%, memory, PID info

---

## Feature 2: Host Telemetry & Process Matrix

**"How does the live CPU/RAM graph work?"**

### Data Collection Flow

```
System Monitor daemon thread (runs every 500ms):
    |
    |-- psutil.cpu_percent(percpu=True)       <- per-core CPU usage from OS
    |-- psutil.virtual_memory()               <- RAM usage from OS
    |-- psutil.disk_io_counters()             <- disk I/O from OS
    |-- psutil.net_io_counters()              <- network I/O from OS
    +-- psutil.process_iter([...])            <- all running processes list
    |
    v
Stored in thread-safe deque (ring buffer, 120 snapshots = ~60s history)
    |
    v
WebSocket /ws/telemetry streams JSON to browser every 500ms
    |
    v
Browser telemetry.js updates CPU bar, RAM bar, process table in real time
```

### Libraries Used
- psutil — cross-platform library reading real kernel-level metrics
- asyncio — async WebSocket streaming
- threading.RLock — re-entrant lock for thread-safe data access

---

## Feature 3: Intelligent Policy Engine

**"How does the system automatically detect and throttle CPU hogs?"**

The policy engine is a background daemon thread (every 2 seconds) applying 4 rules:

### Rule 1 — CPU Hog Mitigation
```
Every 2 seconds, for each process:
  if cpu_percent > 70% for > 10 seconds:
    -> SetPriorityClass(pid, BELOW_NORMAL)   (Win32 API call)
    -> If sandbox exists: apply CPU rate cap via Job Object
    -> Log decision to audit trail
```

### Rule 2 — Memory Thrashing Guard
```
If system RAM > 85% used:
  For top memory consumers:
    -> EmptyWorkingSet(pid)    (Win32 API - trims process working set)
If system RAM > 92%:
    -> Log critical warning
```

### Rule 3 — Starvation Recovery (Aging)
```
For each process:
  if cpu_percent < 0.5% for > 15 seconds:
    -> SetPriorityClass(pid, ABOVE_NORMAL)   <- temporary priority boost
    -> Revert back after 10 seconds
```

### Rule 4 — Resource Fairness Warning
```
If any process uses > 3x average CPU vs peers:
  -> Log FAIRNESS_WARNING to decision log
```

### Libraries Used
- psutil — reads CPU/memory per process
- ctypes.windll.kernel32.SetPriorityClass() — real Win32 priority change
- threading.Thread — daemon background thread

---

## Feature 4: Deadlock & Concurrency Engine

**"Is it really detecting deadlocks or just showing fake data?"**

Real algorithms, modelled input data. Graph structure and Banker's are 100%
real implementations running on a user-configurable model.

### Sub-Feature A: Wait-For Graph + Cycle Detection

| Component | Algorithm | Implementation |
|---|---|---|
| Graph | Directed adjacency list | dict of set |
| Cycle Detection | DFS (Tarjan's style) | Recursive DFS with visited + recursion-stack sets |
| Victim Selection | Min priority + max runtime | Sorted comparison |

```python
# Real DFS cycle detection (from deadlock_engine.py)
def _dfs_cycle_detect(node, visited, rec_stack):
    visited.add(node)
    rec_stack.add(node)
    for neighbour in graph[node]:
        if neighbour not in visited:
            if _dfs_cycle_detect(neighbour, visited, rec_stack):
                return True   # cycle found!
        elif neighbour in rec_stack:
            return True       # back-edge = deadlock cycle!
    rec_stack.discard(node)
    return False
```

### Sub-Feature B: Banker's Algorithm (Safety Check)

| Step | What happens |
|---|---|
| Inputs | Available resources, Max matrix, Allocation matrix |
| Compute | Need matrix = Max - Allocation |
| Safety loop | Find process whose Need <= Work; execute it; add resources back |
| Output | is_safe: true/false + safe sequence (e.g. P1->P3->P0->P2) |

No external library — implemented from scratch in deadlock_engine.py using
Python stdlib only (collections, typing, dataclasses).

---

## Feature 5: OS Algorithm Simulator

### Sub-Feature A: MLFQ CPU Scheduler

Pure Python simulation modelling real OS scheduler behaviour.

| MLFQ Rule | Implementation |
|---|---|
| 3 priority queues Q0, Q1, Q2 | dict of collections.deque objects |
| Round-Robin in Q0 (quantum=4ms) | Pop process, run N ticks, re-queue if not done |
| Demotion on quantum expiry | process.current_queue += 1 |
| Priority Boost every 30ms | All processes moved back to Q0 |
| Gantt Chart output | List of {process_id, start_time, end_time, queue} dicts |

API endpoint: GET /api/simulator/mlfq/demo
Library: Python stdlib only (collections.deque, dataclasses)

### Sub-Feature B: Page Replacement Algorithms

| Algorithm | Data Structure Used | Eviction Policy |
|---|---|---|
| FIFO | collections.deque(maxlen=N) | popleft() removes oldest page |
| LRU | collections.OrderedDict | move_to_end() on hit; popitem(last=False) on fault |
| Clock (Second-Chance) | list + ref_bits[] + clock_hand | Scan bits; second chance if 1, evict if 0 |

API endpoint: POST /api/simulator/page-replacement
Library: Python stdlib only

---

## Feature 6: Synthetic Workload Suite

**"Are these real processes or fake?"**

100% real Python processes spawned by the operating system.

| Workload | Script | What it actually does |
|---|---|---|
| CPU Hog | workloads/cpu_hog.py | N threads running while True: pass — real CPU burn |
| Memory Hog | workloads/memory_hog.py | bytearray(chunk_mb * 1MB) in loop — real RAM allocation |
| Deadlock Demo | workloads/deadlock_demo.py | 2 threads locking mutexes in opposite order — real deadlock |
| Sandboxed App | workloads/sandboxed_app.py | Runs inside Job Object with real enforced limits |

### How Workloads Are Launched (3-step fallback)

```python
# Attempt 1: Break away from parent Job Object + open console window
subprocess.Popen(cmd, creationflags=CREATE_NEW_CONSOLE | CREATE_BREAKAWAY_FROM_JOB)

# Attempt 2: Console only (if breakaway denied — WinError 5)
subprocess.Popen(cmd, creationflags=CREATE_NEW_CONSOLE)

# Attempt 3: Silent background process
subprocess.Popen(cmd, stdout=DEVNULL, stderr=DEVNULL)

# After launch: assign to Job Object sandbox if specified
kernel32.AssignProcessToJobObject(sandbox_handle, process_handle)
```

---

## Full Dependency List

### Python (Backend)

| Package | Source | Used For |
|---|---|---|
| fastapi | pip install | Web framework, REST API, WebSocket endpoint |
| uvicorn | pip install | ASGI server to run FastAPI |
| pydantic | pip install | Request/response data validation |
| psutil | pip install | Real-time process and system metrics |
| ctypes | stdlib | Win32 API calls to kernel32.dll directly |
| threading | stdlib | Background daemon threads |
| asyncio | stdlib | Async WebSocket streaming |
| collections | stdlib | deque, OrderedDict (MLFQ, LRU, ring buffer) |
| dataclasses | stdlib | Clean data model definitions |
| subprocess | stdlib | Spawning workload processes |
| argparse | stdlib | Workload CLI argument parsing |

### JavaScript (Frontend)

| API | Source | Used For |
|---|---|---|
| WebSocket | Native browser | Live telemetry stream from backend |
| fetch() | Native browser | REST API calls |
| Canvas API | Native browser | Deadlock Wait-For Graph drawing |
| CSS Custom Properties | Native browser | Design tokens, dark mode colors |
| CSS Animations | Native browser | Micro-animations, hover effects |
| DOM API | Native browser | All rendering — no React/Vue/Angular |

NO npm, NO Node.js, NO bundler, NO JavaScript framework required.

---

## File Structure

```
d:\OS_cp\
|
|-- run_server.py              <- Entry point (starts Uvicorn on port 8000)
|-- test_endpoints.py          <- Quick API health check script
|-- TECH_STACK.md              <- This file
|
|-- backend\
|   |-- main.py                <- FastAPI app, all API routes
|   |-- isolation_engine.py    <- Win32 ctypes: Job Objects, CPU/memory limits
|   |-- system_monitor.py      <- psutil: real-time CPU/RAM/process telemetry
|   |-- policy_engine.py       <- Autonomous resource management daemon
|   |-- deadlock_engine.py     <- DFS cycle detection + Banker's Algorithm
|   +-- simulator_engine.py    <- MLFQ scheduler + FIFO/LRU/Clock algorithms
|
|-- frontend\
|   |-- index.html             <- Single-page app, all 5 tabs in one file
|   |-- css\
|   |   +-- style.css          <- Glassmorphism dark theme (1400+ lines)
|   +-- js\
|       |-- app.js             <- Tab routing, WebSocket, toast alerts
|       |-- telemetry.js       <- CPU/RAM charts, process table rendering
|       |-- isolation.js       <- Sandbox UI: create, list, destroy
|       |-- deadlock_graph.js  <- Canvas graph + Banker's UI
|       |-- simulator.js       <- MLFQ Gantt + page frame matrix
|       +-- workloads.js       <- Launch buttons + workloads status table
|
+-- workloads\
    |-- cpu_hog.py             <- Multi-threaded CPU spinner (real process)
    |-- memory_hog.py          <- Incremental heap allocator (real process)
    |-- deadlock_demo.py       <- Mutex order inversion (real OS deadlock)
    +-- sandboxed_app.py       <- Low-integrity restricted process
```

---

## OS Concepts to Implementation Mapping

| OS Concept | Where Implemented | REAL or Simulated? |
|---|---|---|
| Job Objects (like Linux cgroups) | isolation_engine.py -> kernel32.dll | REAL - Windows NT kernel enforces |
| CPU Rate Control | SetInformationJobObject() class 15 | REAL - hardware scheduler enforces |
| Memory Hard Limits | SetInformationJobObject() class 9 | REAL - Windows memory manager |
| Process Priority | SetPriorityClass() Win32 API | REAL - NT dispatcher changes scheduling |
| Working Set Trim | EmptyWorkingSet() Win32 API | REAL - Windows VMM releases RAM |
| Starvation / Aging | policy_engine.py rule loop | REAL - actual process priorities changed |
| Deadlock Detection (WFG) | deadlock_engine.py DFS | Modelled - real algorithm, modelled data |
| Banker's Algorithm | deadlock_engine.py | Modelled - real algorithm, modelled data |
| MLFQ Scheduling | simulator_engine.py | Simulated - cannot override Windows scheduler |
| Page Replacement | simulator_engine.py | Simulated - user-space model |
| Mutex Deadlock | workloads/deadlock_demo.py | REAL - actual OS threading deadlock |
| System Telemetry | system_monitor.py + psutil | REAL - reads live OS kernel data |

---

*Smart Resource Manager & Application Isolation System — OS Course Project*
