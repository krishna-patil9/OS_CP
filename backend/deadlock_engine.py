"""
Deadlock Engine — Wait-For Graph Cycle Detection & Banker's Algorithm

Provides two core deadlock-related OS capabilities:
  1. Wait-For Graph (WFG) construction and cycle detection using DFS
  2. Banker's Algorithm for safe-state evaluation

OS Concepts Demonstrated:
  - Deadlock detection via cycle detection in resource allocation graphs
  - Deadlock avoidance via Banker's Algorithm (safe/unsafe state)
  - Deadlock prevention strategies
  - Resource contention modeling
  - Victim selection for deadlock resolution
"""

import time
import logging
from typing import List, Dict, Optional, Tuple, Set
from dataclasses import dataclass, field
from collections import defaultdict

logger = logging.getLogger("deadlock_engine")


# ──────────────────────────────────────────────────────────────────────────────
# Data Models
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Resource:
    """A system resource that processes compete for."""
    id: str
    name: str
    total_instances: int
    available_instances: Optional[int] = None

    def __post_init__(self):
        if self.available_instances is None:
            self.available_instances = self.total_instances


@dataclass
class ProcessNode:
    """A process in the resource allocation graph."""
    id: str
    name: str
    priority: int = 0
    runtime_seconds: float = 0.0
    memory_mb: float = 0.0
    allocated: Dict[str, int] = field(default_factory=dict)   # resource_id -> count
    requesting: Dict[str, int] = field(default_factory=dict)  # resource_id -> count
    max_claim: Dict[str, int] = field(default_factory=dict)   # resource_id -> max count
    recovery_priority: Optional[int] = None                   # Application recovery priority (1 = lowest/first victim)
    os_scheduling_priority: Optional[str] = None              # Windows OS scheduling priority class (separate concept)
    pid: Optional[int] = None
    tid: Optional[int] = None
    create_time_100ns: Optional[int] = None
    role: str = "worker"                                      # "worker" or "supervisor"
    eligible_for_victim: bool = True
    state: str = "running"


@dataclass
class WFGEdge:
    """An edge in the Wait-For Graph."""
    from_process: str
    to_process: str
    resource: str
    weight: int = 1


@dataclass
class DeadlockResult:
    """Result of a deadlock detection analysis."""
    has_deadlock: bool
    cycles: List[List[str]]              # List of cycles (each cycle is list of process IDs)
    involved_processes: Set[str]
    involved_resources: Set[str]
    recommended_victim: Optional[str]
    victim_reason: str = ""
    timestamp: float = 0.0
    detection_method: str = "dfs_wfg"
    is_multi_instance_graph: bool = False
    multi_instance_deadlocked: List[str] = field(default_factory=list)


@dataclass
class BankerResult:
    """Result of a Banker's Algorithm safety check."""
    is_safe: bool
    safe_sequence: List[str]             # Safe execution order (empty if unsafe)
    available: List[int]
    allocation_matrix: List[List[int]]
    need_matrix: List[List[int]]
    max_matrix: List[List[int]]
    process_names: List[str]
    resource_names: List[str]
    timestamp: float = 0.0
    finish_flags: List[bool] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Wait-For Graph Engine
# ──────────────────────────────────────────────────────────────────────────────

class WaitForGraph:
    """
    Constructs and analyzes a Wait-For Graph for deadlock detection.

    In the WFG, nodes are processes and a directed edge P1 -> P2 means
    P1 is waiting for a resource currently held by P2. A cycle in this
    graph indicates a deadlock.
    """

    def __init__(self):
        self.processes: Dict[str, ProcessNode] = {}
        self.resources: Dict[str, Resource] = {}
        self.edges: List[WFGEdge] = []
        self._adjacency: Dict[str, List[str]] = defaultdict(list)

    def reset(self):
        """Clear all state."""
        self.processes.clear()
        self.resources.clear()
        self.edges.clear()
        self._adjacency.clear()

    def add_process(self, process: ProcessNode):
        """Add a process node to the graph."""
        self.processes[process.id] = process

    def add_resource(self, resource: Resource):
        """Add a resource to the graph."""
        self.resources[resource.id] = resource

    def build_edges(self):
        """
        Build Wait-For edges from allocation and request state.

        For each process P that is requesting resource R:
          Find all processes Q that hold instances of R.
          Add edge P -> Q (P waits for Q to release R).
        """
        self.edges.clear()
        self._adjacency.clear()

        # Build map: resource_id -> set of holders
        resource_holders: Dict[str, Set[str]] = defaultdict(set)
        for pid, proc in self.processes.items():
            for res_id, count in proc.allocated.items():
                if count > 0:
                    resource_holders[res_id].add(pid)

        # For each process requesting a resource, add edges to holders
        for pid, proc in self.processes.items():
            for res_id, req_count in proc.requesting.items():
                if req_count <= 0:
                    continue
                res = self.resources.get(res_id)
                if res and res.available_instances >= req_count:
                    continue  # Request can be satisfied, no wait

                holders = resource_holders.get(res_id, set())
                for holder_id in holders:
                    if holder_id != pid:
                        edge = WFGEdge(
                            from_process=pid,
                            to_process=holder_id,
                            resource=res_id,
                        )
                        self.edges.append(edge)
                        self._adjacency[pid].append(holder_id)

    def detect_cycles(self) -> List[List[str]]:
        """
        Detect all cycles in the Wait-For Graph using DFS.

        Uses a color-based DFS approach:
          - WHITE (0): unvisited
          - GRAY (1): in current DFS path (on stack)
          - BLACK (2): fully processed

        A back edge (to a GRAY node) indicates a cycle.
        """
        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {pid: WHITE for pid in self.processes}
        parent: Dict[str, Optional[str]] = {pid: None for pid in self.processes}
        cycles: List[List[str]] = []

        def dfs(node: str, path: List[str]):
            color[node] = GRAY
            path.append(node)

            for neighbor in self._adjacency.get(node, []):
                if neighbor not in color:
                    continue
                if color[neighbor] == GRAY:
                    # Found a cycle — extract it
                    cycle_start = path.index(neighbor)
                    cycle = path[cycle_start:] + [neighbor]
                    cycles.append(cycle)
                elif color[neighbor] == WHITE:
                    parent[neighbor] = node
                    dfs(neighbor, path)

            path.pop()
            color[node] = BLACK

        for pid in self.processes:
            if color.get(pid) == WHITE:
                dfs(pid, [])

        return cycles

    def detect_multi_instance_deadlock(self) -> List[str]:
        """
        Multi-instance deadlock detection via matrix reduction (Silberschatz / Coffman).

        Uses current Available, Allocation, and Request vectors (NOT Max claims —
        Banker's Algorithm is separate). Iteratively marks processes whose current
        Request[i] <= Work as finishable and releases their Allocation[i] into Work.
        Any remaining unfinished processes are genuinely deadlocked.
        """
        proc_ids = list(self.processes.keys())
        res_ids = list(self.resources.keys())
        if not proc_ids or not res_ids:
            return []

        work = [int(self.resources[r].available_instances or 0) for r in res_ids]
        alloc = [
            [int(self.processes[p].allocated.get(r, 0)) for r in res_ids]
            for p in proc_ids
        ]
        req = [
            [int(self.processes[p].requesting.get(r, 0)) for r in res_ids]
            for p in proc_ids
        ]

        n = len(proc_ids)
        m = len(res_ids)
        finish = [
            all(alloc[i][j] == 0 for j in range(m)) and all(req[i][j] == 0 for j in range(m))
            for i in range(n)
        ]

        while True:
            progressed = False
            for i in range(n):
                if finish[i]:
                    continue
                if all(req[i][j] <= work[j] for j in range(m)):
                    for j in range(m):
                        work[j] += alloc[i][j]
                    finish[i] = True
                    progressed = True
            if not progressed:
                break

        return [proc_ids[i] for i in range(n) if not finish[i]]

    def detect_deadlock(self) -> DeadlockResult:
        """
        Full deadlock detection:
          - Builds RAG/WFG edges and runs DFS cycle detection.
          - Runs multiple-instance matrix reduction when resources have >1 instances
            (since a cycle is necessary but not sufficient for multi-instance deadlock).
          - Recommends the optimal eligible victim using declared recovery priority.
        """
        self.build_edges()
        cycles = self.detect_cycles()
        is_multi_instance = any(r.total_instances > 1 for r in self.resources.values())
        multi_deadlocked = self.detect_multi_instance_deadlock()

        involved_procs: Set[str] = set()
        involved_res: Set[str] = set()

        if is_multi_instance:
            # Multi-instance reduction is definitive when any resource has >1 instances
            involved_procs.update(multi_deadlocked)
            has_deadlock = len(multi_deadlocked) > 0
            detection_method = "multi_instance_reduction+dfs"
            # Filter cycles to those whose members are actually deadlocked
            cycles = [c for c in cycles if all(pid in involved_procs for pid in c)]
        else:
            for cycle in cycles:
                for pid in cycle:
                    involved_procs.add(pid)
            has_deadlock = len(cycles) > 0
            detection_method = "dfs_wfg"

        for pid in involved_procs:
            proc = self.processes.get(pid)
            if proc:
                for res_id, cnt in proc.requesting.items():
                    if cnt > 0:
                        involved_res.add(res_id)
                for res_id, cnt in proc.allocated.items():
                    if cnt > 0:
                        involved_res.add(res_id)

        victim = None
        victim_reason = ""
        if has_deadlock and involved_procs:
            victim, victim_reason = self._select_victim(involved_procs)

        return DeadlockResult(
            has_deadlock=has_deadlock,
            cycles=cycles,
            involved_processes=involved_procs,
            involved_resources=involved_res,
            recommended_victim=victim,
            victim_reason=victim_reason,
            timestamp=time.time(),
            detection_method=detection_method,
            is_multi_instance_graph=is_multi_instance,
            multi_instance_deadlocked=multi_deadlocked,
        )

    def _select_victim(self, involved: Set[str]) -> Tuple[Optional[str], str]:
        """
        Select optimal eligible victim for deadlock resolution.

        When nodes declare explicit application `recovery_priority` (1 = lowest,
        distinct from Windows OS scheduling priority), strictly select the eligible
        worker with the lowest recovery_priority (ties broken by shorter runtime,
        then lower memory). Otherwise fall back to the legacy weighted cost formula
        for static educational scenarios.
        """
        eligible = [
            self.processes[pid]
            for pid in involved
            if pid in self.processes and self.processes[pid].eligible_for_victim
        ]
        if not eligible:
            return None, "No eligible worker victim found in deadlock cycle."

        uses_recovery_priority = any(p.recovery_priority is not None for p in eligible)

        if uses_recovery_priority:
            eligible.sort(
                key=lambda p: (
                    p.recovery_priority if p.recovery_priority is not None else p.priority,
                    p.runtime_seconds,
                    p.memory_mb,
                    p.id,
                )
            )
            chosen = eligible[0]
            rec_pri = chosen.recovery_priority if chosen.recovery_priority is not None else chosen.priority
            pid_display = f"OS PID {chosen.pid}" if chosen.pid is not None else chosen.id
            reason = (
                f"Selected '{chosen.name}' ({pid_display}) as lowest recovery-priority eligible victim: "
                f"recovery_priority={rec_pri} (1=lowest, distinct from OS scheduling priority "
                f"'{chosen.os_scheduling_priority or 'normal'}'), "
                f"runtime={chosen.runtime_seconds:.1f}s, memory={chosen.memory_mb:.1f}MB"
            )
            return chosen.id, reason

        best_victim = None
        best_cost = float("inf")
        reason = ""

        for proc in eligible:
            cost = (proc.priority * 10) + proc.runtime_seconds + (proc.memory_mb / 100)
            if cost < best_cost:
                best_cost = cost
                best_victim = proc.id
                reason = (
                    f"Selected '{proc.name}' (PID {proc.id}) as victim: "
                    f"priority={proc.priority}, runtime={proc.runtime_seconds:.1f}s, "
                    f"memory={proc.memory_mb:.1f}MB, cost={cost:.2f}"
                )

        return best_victim, reason

    def get_graph_data(self) -> dict:
        """Return graph data for visualization."""
        nodes = []
        for pid, proc in self.processes.items():
            rec_pri = proc.recovery_priority if proc.recovery_priority is not None else proc.priority
            nodes.append({
                "id": pid,
                "name": proc.name,
                "label": proc.name,
                "type": "process",
                "priority": rec_pri,
                "recovery_priority": rec_pri,
                "os_scheduling_priority": proc.os_scheduling_priority,
                "os_pid": proc.pid,
                "tid": proc.tid,
                "role": proc.role,
                "state": proc.state,
                "eligible_for_victim": proc.eligible_for_victim,
                "runtime_seconds": proc.runtime_seconds,
                "memory_mb": proc.memory_mb,
                "allocated": proc.allocated,
                "requesting": proc.requesting,
                "max_claim": proc.max_claim,
            })

        for rid, res in self.resources.items():
            nodes.append({
                "id": rid,
                "name": res.name,
                "label": res.name,
                "type": "resource",
                "total": res.total_instances,
                "instances": res.total_instances,
                "available": res.available_instances,
            })

        # 1. Resource Allocation Graph (RAG) edges connecting Processes and Resources
        rag_edges = []
        for pid, proc in self.processes.items():
            # Allocation edges: Resource -> Process (held by process)
            for rid, count in proc.allocated.items():
                if count > 0 and rid in self.resources:
                    rag_edges.append({
                        "from": rid,
                        "to": pid,
                        "type": "allocation",
                        "resource": rid,
                        "label": f"holds ({count})" if count > 1 else "holds",
                        "weight": count,
                    })
            # Request edges: Process -> Resource (requested by process)
            for rid, count in proc.requesting.items():
                if count > 0 and rid in self.resources:
                    rag_edges.append({
                        "from": pid,
                        "to": rid,
                        "type": "request",
                        "resource": rid,
                        "label": f"requests ({count})" if count > 1 else "requests",
                        "weight": count,
                    })

        # 2. Pure Wait-For Graph (WFG) edges: Process -> Process
        wfg_edges = []
        for e in self.edges:
            wfg_edges.append({
                "from": e.from_process,
                "to": e.to_process,
                "type": "wait",
                "resource": e.resource,
                "label": f"waits for {e.resource}",
            })

        # By default, provide RAG edges in 'edges' so that both Process and Resource nodes are connected
        return {
            "nodes": nodes,
            "edges": rag_edges,
            "rag_edges": rag_edges,
            "wfg_edges": wfg_edges,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Banker's Algorithm Engine
# ──────────────────────────────────────────────────────────────────────────────

class BankersAlgorithm:
    """
    Implementation of Dijkstra's Banker's Algorithm for deadlock avoidance.

    Given:
      - n processes, m resource types
      - Available vector A[m]: currently available instances of each resource
      - Max matrix MAX[n][m]: maximum claim of each process
      - Allocation matrix ALLOC[n][m]: currently allocated to each process

    Computes:
      - Need matrix NEED[n][m] = MAX - ALLOC
      - Safety check: finds a safe sequence or declares state UNSAFE
    """

    def check_safety(
        self,
        processes: List[str],
        resources: List[str],
        available: List[int],
        max_matrix: List[List[int]],
        allocation_matrix: List[List[int]],
    ) -> BankerResult:
        """
        Run the Banker's Algorithm safety check.

        Returns BankerResult with is_safe flag and safe sequence if one exists.
        """
        n = len(processes)
        m = len(resources)

        # Validate dimensions and matrix invariants
        if len(available) != m:
            raise ValueError(f"Available vector length {len(available)} != {m}")
        if len(max_matrix) != n:
            raise ValueError(f"Max matrix rows {len(max_matrix)} != {n}")
        if len(allocation_matrix) != n:
            raise ValueError(f"Allocation matrix rows {len(allocation_matrix)} != {n}")
        if any(a < 0 for a in available):
            raise ValueError(f"Available vector cannot contain negative values: {available}")

        # Compute Need matrix: NEED[i][j] = MAX[i][j] - ALLOC[i][j]
        need = []
        for i in range(n):
            if len(max_matrix[i]) != m or len(allocation_matrix[i]) != m:
                raise ValueError(f"Row {i} length mismatch: expected {m} resource columns")
            row = []
            for j in range(m):
                if max_matrix[i][j] < 0 or allocation_matrix[i][j] < 0:
                    raise ValueError(
                        f"Negative matrix entry at process={processes[i]}, resource={resources[j]}"
                    )
                if allocation_matrix[i][j] > max_matrix[i][j]:
                    raise ValueError(
                        f"Allocation ({allocation_matrix[i][j]}) exceeds declared Max claim "
                        f"({max_matrix[i][j]}) for process={processes[i]}, resource={resources[j]}"
                    )
                row.append(max_matrix[i][j] - allocation_matrix[i][j])
            need.append(row)

        # Safety Algorithm
        work = list(available)                 # Work vector = copy of Available
        finish = [False] * n                   # Finish flags
        safe_sequence: List[str] = []

        while True:
            found = False
            for i in range(n):
                if finish[i]:
                    continue
                # Check if Need[i] <= Work (component-wise)
                can_finish = all(need[i][j] <= work[j] for j in range(m))
                if can_finish:
                    # Process i can finish: release its resources
                    for j in range(m):
                        work[j] += allocation_matrix[i][j]
                    finish[i] = True
                    safe_sequence.append(processes[i])
                    found = True

            if not found:
                break

        is_safe = all(finish)

        return BankerResult(
            is_safe=is_safe,
            safe_sequence=safe_sequence if is_safe else [],
            available=available,
            allocation_matrix=allocation_matrix,
            need_matrix=need,
            max_matrix=max_matrix,
            process_names=processes,
            resource_names=resources,
            timestamp=time.time(),
            finish_flags=list(finish),
        )

    def simulate_request(
        self,
        processes: List[str],
        resources: List[str],
        available: List[int],
        max_matrix: List[List[int]],
        allocation_matrix: List[List[int]],
        requesting_process: int,
        request: List[int],
    ) -> Tuple[BankerResult, bool]:
        """
        Simulate a resource request and check if granting it keeps state safe.

        Returns (result_after_grant, would_be_granted).
        """
        n = len(processes)
        m = len(resources)
        i = requesting_process

        # Check: Request <= Need
        need_i = [max_matrix[i][j] - allocation_matrix[i][j] for j in range(m)]
        if any(request[j] > need_i[j] for j in range(m)):
            # Request exceeds maximum claim — error
            result = self.check_safety(processes, resources, available,
                                       max_matrix, allocation_matrix)
            return result, False

        # Check: Request <= Available
        if any(request[j] > available[j] for j in range(m)):
            # Not enough resources — must wait
            result = self.check_safety(processes, resources, available,
                                       max_matrix, allocation_matrix)
            return result, False

        # Tentatively allocate
        new_available = [available[j] - request[j] for j in range(m)]
        new_alloc = [row[:] for row in allocation_matrix]
        for j in range(m):
            new_alloc[i][j] += request[j]

        result = self.check_safety(processes, resources, new_available,
                                   max_matrix, new_alloc)

        return result, result.is_safe


# ──────────────────────────────────────────────────────────────────────────────
# Deadlock Demo Scenario Builder
# ──────────────────────────────────────────────────────────────────────────────

class DeadlockScenarioBuilder:
    """
    Pre-built deadlock scenarios for demonstration purposes.
    """

    @staticmethod
    def circular_wait_scenario() -> WaitForGraph:
        """
        Classic circular wait: P1 -> R1 -> P2 -> R2 -> P3 -> R3 -> P1
        """
        wfg = WaitForGraph()

        # Resources (each with 1 instance)
        wfg.add_resource(Resource("R1", "Printer", 1, 0))
        wfg.add_resource(Resource("R2", "Scanner", 1, 0))
        wfg.add_resource(Resource("R3", "Disk", 1, 0))

        # Processes
        wfg.add_process(ProcessNode(
            "P1", "Process A", priority=2, runtime_seconds=5.0, memory_mb=100,
            allocated={"R1": 1}, requesting={"R2": 1},
        ))
        wfg.add_process(ProcessNode(
            "P2", "Process B", priority=1, runtime_seconds=10.0, memory_mb=200,
            allocated={"R2": 1}, requesting={"R3": 1},
        ))
        wfg.add_process(ProcessNode(
            "P3", "Process C", priority=3, runtime_seconds=2.0, memory_mb=50,
            allocated={"R3": 1}, requesting={"R1": 1},
        ))

        return wfg

    @staticmethod
    def no_deadlock_scenario() -> WaitForGraph:
        """
        No deadlock: requests can be satisfied.
        """
        wfg = WaitForGraph()

        wfg.add_resource(Resource("R1", "CPU", 2, 1))
        wfg.add_resource(Resource("R2", "Memory", 3, 2))

        wfg.add_process(ProcessNode(
            "P1", "Worker A", priority=1,
            allocated={"R1": 1, "R2": 1}, requesting={},
        ))
        wfg.add_process(ProcessNode(
            "P2", "Worker B", priority=2,
            allocated={"R2": 0}, requesting={"R2": 1},
        ))

        return wfg

    @staticmethod
    def dining_philosophers_scenario() -> WaitForGraph:
        """
        Classic Dijkstra Dining Philosophers problem (5 philosophers, 5 chopsticks).
        Every philosopher holds left chopstick and waits for right chopstick -> cyclic deadlock.
        """
        wfg = WaitForGraph()
        n = 5
        for i in range(n):
            wfg.add_resource(Resource(f"C{i}", f"Chopstick {i}", 1, 0))

        for i in range(n):
            left_chop = f"C{i}"
            right_chop = f"C{(i + 1) % n}"
            wfg.add_process(ProcessNode(
                f"Phil_{i}", f"Philosopher {i}",
                priority=2 if i % 2 == 0 else 1,
                runtime_seconds=float(i * 3 + 2),
                memory_mb=40.0 + i * 10,
                allocated={left_chop: 1},
                requesting={right_chop: 1},
            ))

        return wfg

    @staticmethod
    def multi_resource_scenario() -> WaitForGraph:
        """
        Multi-resource contention scenario:
        P1, P2, P3 form a circular deadlock over Mutex A, Mutex B, and Shared DB.
        P4 is blocked on Mutex B (waiting on P2), demonstrating dependency chains.
        """
        wfg = WaitForGraph()

        wfg.add_resource(Resource("MA", "Mutex A", 1, 0))
        wfg.add_resource(Resource("MB", "Mutex B", 1, 0))
        wfg.add_resource(Resource("DB", "Shared DB", 1, 0))
        wfg.add_resource(Resource("GPU", "GPU Lock", 1, 0))

        wfg.add_process(ProcessNode(
            "P1", "Service A", priority=2, runtime_seconds=8.0, memory_mb=120,
            allocated={"MA": 1}, requesting={"MB": 1},
        ))
        wfg.add_process(ProcessNode(
            "P2", "Service B", priority=1, runtime_seconds=15.0, memory_mb=250,
            allocated={"MB": 1}, requesting={"DB": 1},
        ))
        wfg.add_process(ProcessNode(
            "P3", "Service C", priority=3, runtime_seconds=4.0, memory_mb=80,
            allocated={"DB": 1}, requesting={"MA": 1},
        ))
        wfg.add_process(ProcessNode(
            "P4", "Analytics", priority=1, runtime_seconds=2.0, memory_mb=60,
            allocated={"GPU": 1}, requesting={"MB": 1},
        ))

        return wfg

    @staticmethod
    def banker_safe_scenario() -> dict:
        """Classic Banker's Algorithm safe state example."""
        return {
            "processes": ["P0", "P1", "P2", "P3", "P4"],
            "resources": ["A", "B", "C"],
            "available": [3, 3, 2],
            "max_matrix": [
                [7, 5, 3],
                [3, 2, 2],
                [9, 0, 2],
                [2, 2, 2],
                [4, 3, 3],
            ],
            "allocation_matrix": [
                [0, 1, 0],
                [2, 0, 0],
                [3, 0, 2],
                [2, 1, 1],
                [0, 0, 2],
            ],
        }

    @staticmethod
    def banker_unsafe_scenario() -> dict:
        """Banker's Algorithm unsafe state example."""
        return {
            "processes": ["P0", "P1", "P2"],
            "resources": ["A", "B"],
            "available": [1, 1],
            "max_matrix": [
                [4, 3],
                [3, 4],
                [3, 3],
            ],
            "allocation_matrix": [
                [2, 1],
                [1, 2],
                [1, 1],
            ],
        }
