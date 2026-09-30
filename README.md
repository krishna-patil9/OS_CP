# Smart Resource Manager & Application Isolation System

An **Operating Systems Course Project** demonstrating native host OS process management, kernel-level isolation, heuristic resource management, and interactive OS algorithm visualization.

---

## 📌 Executive Summary

Modern desktop operating systems (Windows, Linux, macOS) execute millions of instructions per second across hundreds of concurrent processes. While the kernel scheduler maintains overall stability, rogue processes, memory leaks, resource hogs, and thread deadlocks routinely degrade interactive responsiveness.

This project implements an **Intelligent Resource Management Layer** that pairs **real, native host operating system control** via Windows NT Kernel Job Objects and Process APIs with an **Intelligent Decision Engine** and an **OS Algorithm Interactive Visualizer**.

### Key Highlights
- **100% Technical Honesty**: Directly leverages the Windows NT Kernel Job Object subsystem via Win32 APIs for **real hard enforcement** of CPU rate limits, memory ceilings, and process quotas.
- **Dual-Layer Architecture**: Blends native host enforcement (Layer 1) with an interactive algorithmic simulation of internal kernel mechanisms (MLFQ CPU scheduling, Banker's Algorithm, Page Replacement) (Layer 2).
- **Glassmorphism Web Dashboard**: Real-time 500ms WebSocket streaming with dynamic Canvas charts, live process matrix, interactive Wait-For Graph (WFG), and audit decision feeds.
- **Synthetic Stress Workloads**: Includes ready-to-run workloads demonstrating CPU starvation, memory thrashing, mutual exclusion deadlock inversion, and restricted-token execution.

---

## 🏛️ System Architecture

```
+-----------------------------------------------------------------------------------+
|                        Glassmorphism Web Dashboard (HTML5/CSS3/JS)                |
|      (Live Telemetry, Job Object Control, Decision Logs, WFG Canvas, MLFQ)        |
+-----------------------------------------------------------------------------------+
                                         │  (WebSocket / REST API)
                                         ▼
+-----------------------------------------------------------------------------------+
|                       FastAPI Backend Server & Controller                         |
+-----------------------------------------------------------------------------------+
             │                                                      │
             ▼                                                      ▼
┌───────────────────────────────────────┐              ┌───────────────────────────────────────┐
│     LAYER 1: NATIVE HOST CONTROL      │              │    LAYER 2: OS ALGORITHM SIMULATOR    │
│  - Windows Job Objects (CPU/RAM caps) │              │  - Multi-Level Feedback Queue (MLFQ)  │
│  - Priority Class & Affinity Control  │              │  - Dijkstra's Banker's Algorithm      │
│  - Working Set Trimming               │              │  - Page Replacement (LRU/FIFO/Clock) │
│  - Active Process Limits (Fork Guard) │              │  - Comparative Performance Metrics    │
└───────────────────────────────────────┘              └───────────────────────────────────────┘
             │                                                      │
             ▼                                                      ▼
┌───────────────────────────────────────┐              ┌───────────────────────────────────────┐
│       INTELLIGENT POLICY ENGINE       │              │       SYNTHETIC WORKLOAD SUITE        │
│  - CPU Hog Mitigation Rule            │              │  - CPU Spinner (`cpu_hog.py`)         │
│  - Memory Thrashing Guard             │              │  - Memory Allocator (`memory_hog.py`) │
│  - Starvation Recovery & Dynamic Boost│              │  - Mutex Inversion (`deadlock_demo.py`)│
│  - Foreground vs Background Isolation │              │  - Sandboxed App (`sandboxed_app.py`) │
└───────────────────────────────────────┘              └───────────────────────────────────────┘
```

---

## 🎯 Direct OS Concepts Mapping

| OS Concept | Theoretical Principle | Implementation in This Project |
| :--- | :--- | :--- |
| **Process Control & States** | Process states (Ready, Running, Blocked), PCB, Priority classes | `system_monitor.py` reads OS process table; `isolation_engine.py` adjusts Priority Class (`IDLE` to `REALTIME`) and Core Affinity masks |
| **Process Isolation & Sandboxing** | Hardware/kernel protection boundaries, resource limits | Windows NT **Job Objects** via `CreateJobObjectW`, `SetInformationJobObject`, `AssignProcessToJobObject` |
| **CPU Scheduling** | Multi-queue scheduling, dynamic priority decay, starvation prevention | `simulator_engine.py` implements a 3-tier **MLFQ** scheduler with varying time quanta (4ms, 8ms, 16ms) and periodic global boost |
| **Resource Allocation & Deadlock** | Coffman conditions, Wait-For Graph (WFG) cycles, Deadlock avoidance | `deadlock_engine.py` implements **Tarjan's DFS Cycle Detection** on directed dependency graphs and **Dijkstra's Banker's Algorithm** |
| **Virtual Memory & Page Replacement** | Demand paging, page faults, replacement policies | `simulator_engine.py` simulates and benchmarks **FIFO**, **LRU**, and **Clock (Second-Chance)** page replacement; `isolation_engine.py` invokes working set trimming (`SetProcessWorkingSetSize`) |
| **Concurrency & Synchronization** | Mutual exclusion, lock order inversion | `workloads/deadlock_demo.py` spawns worker threads acquiring locks in inverted order ($A \to B$ vs $B \to A$) |

---

## 📁 Repository Structure

```
d:/OS_cp/
├── backend/
│   ├── __init__.py
│   ├── main.py                  # FastAPI server with WebSocket streaming & REST routes
│   ├── system_monitor.py        # Host telemetry collector (psutil + win32)
│   ├── isolation_engine.py      # Win32 NT Kernel Job Objects & process control
│   ├── policy_engine.py         # Autonomous heuristic decision & mitigation engine
│   ├── deadlock_engine.py       # Wait-For Graph cycle detection & Banker's Algorithm
│   └── simulator_engine.py      # MLFQ CPU scheduler & Page replacement simulator
├── frontend/
│   ├── index.html               # Glassmorphism dashboard single-page interface
│   ├── css/
│   │   └── style.css            # Custom CSS3 design tokens, glassmorphism & responsive layout
│   └── js/
│       ├── app.js               # Application router, toast alerts, & WebSocket manager
│       ├── telemetry.js         # Canvas performance charts & process matrix controller
│       ├── isolation.js         # Job Object sandbox manager & accounting viewer
│       ├── deadlock_graph.js    # Interactive WFG Canvas renderer & Banker's visualizer
│       ├── simulator.js         # MLFQ interactive queues & Page replacement comparator
│       └── workloads.js         # Synthetic workload launch & termination controller
├── workloads/
│   ├── cpu_hog.py               # Multi-threaded CPU spinning workload
│   ├── memory_hog.py            # Progressive private heap memory allocator
│   ├── deadlock_demo.py         # Mutex order inversion deadlock generator
│   └── sandboxed_app.py         # Untrusted synthetic test binary
├── run_server.py                # Single-script launcher
└── README.md                    # Project documentation & evaluation guide
```

---

## 🚀 Quick Start & Running

### Prerequisites
- Windows 10/11 (64-bit)
- Python 3.10+ (Tested on Python 3.13)
- Required Python packages:
  ```bash
  pip install fastapi uvicorn psutil pywin32 pydantic
  ```

### Launch the Application
Run the launcher script from the workspace directory:
```bash
python run_server.py
```

Open your browser and navigate to:
```
http://127.0.0.1:8000
```

---

## 🔬 Interactive Features Walkthrough

### 1. Host Telemetry & Process Matrix (Tab 1)
- **Live Canvas Charts**: Real-time 60-second rolling CPU and Memory usage graphs rendered using smooth canvas gradients.
- **Process Filtering & Sorting**: Filter processes by category (Interactive UI vs Background Service vs Kernel) or search by PID / name.
- **Inline Process Actions**:
  - ✂️ **Trim Working Set**: Evicts unreferenced pages back to the paging file using `SetProcessWorkingSetSize(-1, -1)`.
  - ⬇️ **Set to IDLE**: Demotes thread scheduling priority class to `IDLE_PRIORITY_CLASS`.
  - ❌ **Terminate**: Terminates runaway processes.
- **Transparent Audit Feed**: Every heuristic mitigation taken by the system generates a timestamped decision entry with rationale and rule ID.

### 2. Job Object Sandbox Manager (Tab 2)
- **Create Job Object Sandbox**: Configure hard limits directly enforced by the Windows NT kernel:
  - **CPU Rate Cap (%)**: Uses `JobObjectCpuRateControlInformation` with hard clamping.
  - **Hard Memory Limit (MB)**: Enforces `JobObjectExtendedLimitInformation` (ProcessMemoryLimit).
  - **Fork-Bomb Protection**: Sets `JOB_OBJECT_LIMIT_ACTIVE_PROCESS`.
- **Enclose Running Process**: Dynamically binds existing PIDs into a Job Object context via `AssignProcessToJobObject`.
- **Kernel Accounting**: Inspect total kernel time, user time, peak memory, and page fault counters for any sandbox.

### 3. Deadlock & Concurrency Engine (Tab 3)
- **Interactive Wait-For Graph (WFG)**:**:
  - Live Allocation, Max Claim, Need, and Available matrices.
  - Executes safety algorithm to find safe sequences (e.g. $P_1 \to P_3 \to P_4 \to P_0 \to P_2$) or alert on unsafe states.

### 4. OS Algorithm Simulator (Tab 4)
- **MLFQ CPU Scheduler**:
  - 3-queue feedback scheduler: Q0 (Quantum = 4ms), Q1 (Quantum = 8ms), Q2 (Quantum = 16ms).
  - Visual process chips transition through queues as time quanta expire.
  - Execution Gantt chart visualizes time slice allocations and global boost events.
  - Directed bipartite graph with Process circles and Resource squares.
  - Detects deadlocks in $O(V + E)$ using **Tarjan's Strongly Connected Components / DFS cycle detection**.
  - Animated pulsing crimson highlights indicate active circular wait cycles.
  - Evaluates an OS cost function to identify the **Optimal Victim Process** for preemption based on Priority, Runtime, and Memory consumption.
- **Dijkstra's Banker's Algorithm
  - Detailed metrics table: Turnaround Time, Waiting Time, and CPU utilization.
- **Virtual Memory Page Replacement**:
  - Benchmarks **FIFO**, **LRU**, and **Clock (Second-Chance)** side-by-side on any reference string.
  - Highlights the most efficient algorithm with lowest page fault rate.

### 5. Synthetic Workload Suite (Tab 5)
- **CPU Hog**: Spin 2+ threads to 100% core load. Launch raw or inside a 20% Job Object cap to verify hard kernel throttling.
- **Memory Hog**: Continuously allocate private heap pages. Test against a 50MB hard Job Object cap to observe OS enforcement.
- **Deadlock Inversion**: Generates true mutual exclusion deadlock with inverted lock acquisition order.
- **Sandboxed App**: Tests process confinement and fork-bomb prevention.

---

## 🧪 Live Evaluation Demonstration Script

When presenting this project to course instructors or examiners, follow this 4-step demonstration:

### Step 1: CPU Throttling & Job Object Enforcement
1. Navigate to the **Synthetic Workload Suite** tab.
2. Click **"🚀 Launch Unrestricted CPU Hog"**.
3. Switch to **Host Telemetry & Process Matrix** tab. Observe CPU spikes to near 100% on one core.
4. Watch the **Heuristic Decision Feed**: the policy engine flags the CPU hog and demotes it.
5. Return to the Workload tab, stop the workload, and click **"🛡️ Launch Inside 20% Cap Sandbox"**.
6. Switch back to Telemetry: notice the process CPU is strictly clamped at 20% by the Windows NT Kernel Job Object.

### Step 2: Hard Memory Limit Enforcement
1. In the **Synthetic Workload Suite**, click **"🛡️ Launch Inside 50MB Hard Limit"**.
2. Switch to **Job Object Sandbox Manager** tab. View the `mem_hog_50mb_sandbox`.
3. Click the 📊 accounting button: view peak process memory and page fault counts. The process cannot breach the 50MB boundary.

### Step 3: Deadlock Detection & Victim Preemption
1. Open the **Deadlock & Concurrency Engine** tab.
2. Under WFG Cycle Detection, select **"Scenario: Circular Wait Deadlock"** and click **"Load"**.
3. Click **"🔍 Detect"**: observe the glowing red cycle and the recommended victim process with detailed cost justification.
4. Under Banker's Algorithm, click **"Safe State Demo"** -> **"Run Safety Check"** to show the computed safe execution sequence.

### Step 4: MLFQ Scheduling & Page Replacement
1. Open the **OS Algorithm Simulator** tab.
2. Click **"▶️ Run MLFQ Simulation"**: step through the Gantt chart and turnaround time comparisons.
3. Under Page Replacement, click **"Compare FIFO vs LRU vs Clock"**: observe the fault rates and LRU efficiency.

---

## 👥 Authors & Academic Context

- **Project**: Smart Resource Manager & Application Isolation System
- **Course**: Operating Systems Course Project (CS304 / Equivalent)
- **Focus**: Process Management, Kernel Job Objects, Concurrency, Virtual Memory, and Scheduling Algorithms.
