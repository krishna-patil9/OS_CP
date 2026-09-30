"""
Simulator Engine — MLFQ Scheduler & Page Replacement Algorithm Visualizer

Implements interactive simulations of core OS scheduling and memory
management algorithms that cannot be directly overridden in the host kernel.

OS Concepts Demonstrated:
  - Multi-Level Feedback Queue (MLFQ) scheduling
  - Dynamic time quantum management
  - Priority demotion on quantum expiry
  - Periodic priority boosting to prevent starvation
  - Page replacement algorithms: FIFO, LRU, Clock (Second-Chance)
  - Page fault rate analysis
  - Working set model
"""

import time
import logging
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
from collections import deque, OrderedDict
from enum import Enum

logger = logging.getLogger("simulator_engine")


# ══════════════════════════════════════════════════════════════════════════════
# MLFQ SCHEDULER SIMULATOR
# ══════════════════════════════════════════════════════════════════════════════

class ProcessState(Enum):
    READY = "ready"
    RUNNING = "running"
    FINISHED = "finished"
    BLOCKED = "blocked"


@dataclass
class MLFQProcess:
    """A simulated process in the MLFQ scheduler."""
    id: str
    name: str
    total_burst: int           # Total CPU burst time required
    arrival_time: int = 0      # Time of arrival
    remaining_burst: int = 0   # Remaining CPU time
    current_queue: int = 0     # Current queue level (0 = highest priority)
    state: ProcessState = ProcessState.READY
    wait_time: int = 0
    turnaround_time: int = 0
    response_time: int = -1    # -1 = not yet scheduled
    time_in_queue: int = 0     # Time spent at current queue level
    completion_time: int = 0
    start_time: int = -1

    def __post_init__(self):
        if self.remaining_burst == 0:
            self.remaining_burst = self.total_burst


@dataclass
class MLFQConfig:
    """Configuration for the MLFQ scheduler."""
    num_queues: int = 3
    time_quanta: List[int] = field(default_factory=lambda: [4, 8, 16])
    boost_interval: int = 50   # Global priority boost every N time units
    # Queue scheduling: Q0=RR(4), Q1=RR(8), Q2=RR(16)/FCFS


@dataclass
class SchedulerEvent:
    """A single event/step in the scheduler simulation."""
    time: int
    event_type: str       # "ARRIVE", "SCHEDULE", "PREEMPT", "DEMOTE", "COMPLETE", "BOOST", "IDLE"
    process_id: Optional[str]
    queue_level: int
    details: str
    queue_state: Dict[int, List[str]]  # Snapshot of all queues at this moment


class MLFQScheduler:
    """
    Multi-Level Feedback Queue Scheduler Simulator.

    Rules (based on OSTEP MLFQ):
      1. If Priority(A) > Priority(B), A runs (A is in lower queue number).
      2. If Priority(A) == Priority(B), A & B run in Round Robin.
      3. When a job enters the system, it is placed at highest priority (Q0).
      4. Once a job uses up its time quantum at a given level, its priority
         is reduced (moved to the next lower queue).
      5. After some time period S, move all jobs to the topmost queue
         (priority boost to prevent starvation).
    """

    def __init__(self, config: Optional[MLFQConfig] = None):
        self.config = config or MLFQConfig()
        self.processes: List[MLFQProcess] = []
        self.events: List[SchedulerEvent] = []
        self.gantt_slices: List[dict] = []
        self.current_time: int = 0
        self.completed: List[MLFQProcess] = []

    def add_process(self, process: MLFQProcess):
        """Add a process to the simulation."""
        self.processes.append(process)

    def reset(self):
        """Reset simulation state."""
        self.events.clear()
        self.gantt_slices.clear()
        self.completed.clear()
        self.current_time = 0
        for p in self.processes:
            p.remaining_burst = p.total_burst
            p.current_queue = 0
            p.state = ProcessState.READY
            p.wait_time = 0
            p.turnaround_time = 0
            p.response_time = -1
            p.time_in_queue = 0
            p.completion_time = 0
            p.start_time = -1

    def run_simulation(self) -> dict:
        """
        Run the complete MLFQ simulation and return results.
        """
        self.reset()
        queues: Dict[int, deque] = {
            i: deque() for i in range(self.config.num_queues)
        }

        # Sort processes by arrival time
        all_procs = sorted(self.processes, key=lambda p: p.arrival_time)
        proc_index = 0
        running: Optional[MLFQProcess] = None
        quantum_used = 0

        max_time = sum(p.total_burst for p in all_procs) + max(
            (p.arrival_time for p in all_procs), default=0
        ) + 10

        while self.current_time < max_time:
            # 1. Check for arriving processes
            while proc_index < len(all_procs) and all_procs[proc_index].arrival_time <= self.current_time:
                p = all_procs[proc_index]
                p.state = ProcessState.READY
                p.current_queue = 0
                queues[0].append(p)
                self._record_event("ARRIVE", p.id, 0,
                                   f"Process '{p.name}' arrived (burst={p.total_burst})",
                                   queues)
                proc_index += 1

            # 2. Periodic boost (Rule 5)
            if (self.config.boost_interval > 0 and
                    self.current_time > 0 and
                    self.current_time % self.config.boost_interval == 0):
                self._priority_boost(queues, running)

            # 3. Select process to run
            if running is None:
                running, selected_queue = self._select_next(queues)
                if running:
                    quantum_used = 0
                    if running.response_time == -1:
                        running.response_time = self.current_time - running.arrival_time
                    if running.start_time == -1:
                        running.start_time = self.current_time
                    running.state = ProcessState.RUNNING
                    self._record_event("SCHEDULE", running.id, running.current_queue,
                                       f"Process '{running.name}' scheduled from Q{running.current_queue}",
                                       queues)

            # 4. Execute for 1 time unit
            if running:
                # Record into gantt timeline
                if (self.gantt_slices and
                        self.gantt_slices[-1]["process_id"] == running.id and
                        self.gantt_slices[-1]["queue"] == running.current_queue and
                        self.gantt_slices[-1]["end_time"] == self.current_time):
                    self.gantt_slices[-1]["end_time"] += 1
                    self.gantt_slices[-1]["duration"] += 1
                else:
                    self.gantt_slices.append({
                        "process_id": running.id,
                        "proc_id": running.id,
                        "name": running.name,
                        "queue": running.current_queue,
                        "start_time": self.current_time,
                        "end_time": self.current_time + 1,
                        "duration": 1,
                    })

                running.remaining_burst -= 1
                quantum_used += 1

                # Check completion
                if running.remaining_burst <= 0:
                    running.state = ProcessState.FINISHED
                    running.completion_time = self.current_time + 1
                    running.turnaround_time = running.completion_time - running.arrival_time
                    self.completed.append(running)
                    self._record_event("COMPLETE", running.id, running.current_queue,
                                       f"Process '{running.name}' completed (TAT={running.turnaround_time})",
                                       queues)
                    running = None
                    quantum_used = 0

                # Check quantum expiry (Rule 4)
                elif quantum_used >= self.config.time_quanta[min(
                        running.current_queue, len(self.config.time_quanta) - 1)]:
                    old_q = running.current_queue
                    new_q = min(old_q + 1, self.config.num_queues - 1)
                    running.current_queue = new_q
                    running.state = ProcessState.READY
                    running.time_in_queue = 0
                    queues[new_q].append(running)

                    event_type = "DEMOTE" if new_q > old_q else "PREEMPT"
                    self._record_event(event_type, running.id, new_q,
                                       f"Process '{running.name}' {'demoted to' if new_q > old_q else 'stays at'} Q{new_q} (quantum expired)",
                                       queues)
                    running = None
                    quantum_used = 0

                # Check if higher priority process arrived (preemption)
                elif running.current_queue > 0:
                    for q in range(running.current_queue):
                        if queues[q]:
                            # Higher priority process waiting — preempt
                            running.state = ProcessState.READY
                            queues[running.current_queue].appendleft(running)
                            self._record_event("PREEMPT", running.id, running.current_queue,
                                               f"Process '{running.name}' preempted by higher priority process in Q{q}",
                                               queues)
                            running = None
                            quantum_used = 0
                            break
            else:
                # CPU idle
                if proc_index >= len(all_procs) and all(len(q) == 0 for q in queues.values()):
                    break
                self._record_event("IDLE", None, -1, "CPU idle", queues)

            # Update wait times for ready processes
            for q_level, q in queues.items():
                for p in q:
                    if p.state == ProcessState.READY:
                        p.wait_time += 1

            self.current_time += 1

        return self._build_results()

    def _select_next(self, queues: Dict[int, deque]) -> Tuple[Optional[MLFQProcess], int]:
        """Select the next process from the highest non-empty queue."""
        for q_level in range(self.config.num_queues):
            if queues[q_level]:
                proc = queues[q_level].popleft()
                return proc, q_level
        return None, -1

    def _priority_boost(self, queues: Dict[int, deque], running: Optional[MLFQProcess]):
        """Move all processes to Q0 (starvation prevention via aging)."""
        boosted = []
        for q_level in range(1, self.config.num_queues):
            while queues[q_level]:
                p = queues[q_level].popleft()
                p.current_queue = 0
                p.time_in_queue = 0
                queues[0].append(p)
                boosted.append(p.name)

        if running and running.current_queue > 0:
            running.current_queue = 0
            running.time_in_queue = 0
            boosted.append(running.name)

        if boosted:
            self._record_event("BOOST", None, 0,
                               f"Priority boost: {', '.join(boosted)} moved to Q0",
                               queues)

    def _record_event(self, event_type: str, process_id: Optional[str],
                      queue_level: int, details: str,
                      queues: Dict[int, deque]):
        """Record a scheduler event with queue state snapshot."""
        queue_state = {}
        for q_level, q in queues.items():
            queue_state[q_level] = [p.id for p in q]

        self.events.append(SchedulerEvent(
            time=self.current_time,
            event_type=event_type,
            process_id=process_id,
            queue_level=queue_level,
            details=details,
            queue_state=queue_state,
        ))

    def _build_results(self) -> dict:
        """Build final simulation results with backward-compatible aliases."""
        events_data = []
        for e in self.events:
            events_data.append({
                "time": e.time,
                "event_type": e.event_type,
                "process_id": e.process_id,
                "queue_level": e.queue_level,
                "details": e.details,
                "queue_state": {str(k): v for k, v in e.queue_state.items()},
            })

        process_stats = []
        for p in self.processes:
            process_stats.append({
                "id": p.id,
                "name": p.name,
                "arrival_time": p.arrival_time,
                "total_burst": p.total_burst,
                "completion_time": p.completion_time,
                "turnaround_time": p.turnaround_time,
                "wait_time": p.wait_time,
                "waiting_time": p.wait_time,
                "response_time": p.response_time,
                "final_queue": p.current_queue,
            })

        avg_tat = 0
        avg_wait = 0
        avg_resp = 0
        n = len(self.processes)
        if n > 0:
            avg_tat = sum(p.turnaround_time for p in self.processes) / n
            avg_wait = sum(p.wait_time for p in self.processes) / n
            avg_resp = sum(p.response_time for p in self.processes if p.response_time >= 0) / max(1, n)

        summary_data = {
            "total_time": self.current_time,
            "avg_turnaround_time": round(avg_tat, 2),
            "avg_wait_time": round(avg_wait, 2),
            "avg_waiting_time": round(avg_wait, 2),
            "avg_response_time": round(avg_resp, 2),
            "completed_processes": len(self.completed),
            "total_processes": len(self.processes),
        }

        return {
            "events": events_data,
            "processes": process_stats,
            "process_stats": process_stats,
            "gantt_chart": self.gantt_slices,
            "config": {
                "num_queues": self.config.num_queues,
                "time_quanta": self.config.time_quanta,
                "boost_interval": self.config.boost_interval,
            },
            "summary": summary_data,
            "metrics": summary_data,
        }

    @staticmethod
    def create_demo_scenario() -> "MLFQScheduler":
        """Create a demonstration scenario with mixed workloads."""
        scheduler = MLFQScheduler(MLFQConfig(
            num_queues=3,
            time_quanta=[4, 8, 16],
            boost_interval=30,
        ))

        scheduler.add_process(MLFQProcess("P1", "Interactive App", total_burst=8, arrival_time=0))
        scheduler.add_process(MLFQProcess("P2", "CPU Cruncher", total_burst=24, arrival_time=0))
        scheduler.add_process(MLFQProcess("P3", "Background Job", total_burst=16, arrival_time=3))
        scheduler.add_process(MLFQProcess("P4", "Quick Task", total_burst=4, arrival_time=5))
        scheduler.add_process(MLFQProcess("P5", "Batch Process", total_burst=20, arrival_time=8))

        return scheduler


# ══════════════════════════════════════════════════════════════════════════════
# PAGE REPLACEMENT ALGORITHM SIMULATOR
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PageEvent:
    """A single step in the page replacement simulation."""
    step: int
    page_requested: int
    is_hit: bool
    page_evicted: Optional[int]
    frames_state: List[Optional[int]]
    algorithm: str
    details: str


class PageReplacementSimulator:
    """
    Simulates page replacement algorithms for a given reference string.

    Supported algorithms:
      - FIFO (First-In, First-Out)
      - LRU (Least Recently Used)
      - Clock (Second-Chance / Clock Algorithm)
    """

    def simulate_fifo(self, reference_string: List[int], num_frames: int) -> dict:
        """FIFO page replacement: evict the oldest page."""
        frames: deque = deque(maxlen=num_frames)
        events: List[PageEvent] = []
        hits = 0
        faults = 0

        for step, page in enumerate(reference_string):
            if page in frames:
                hits += 1
                events.append(PageEvent(
                    step=step, page_requested=page, is_hit=True,
                    page_evicted=None, frames_state=list(frames),
                    algorithm="FIFO", details=f"Page {page} found in frames (HIT)",
                ))
            else:
                faults += 1
                evicted = None
                if len(frames) >= num_frames:
                    evicted = frames.popleft()
                frames.append(page)
                events.append(PageEvent(
                    step=step, page_requested=page, is_hit=False,
                    page_evicted=evicted, frames_state=list(frames),
                    algorithm="FIFO",
                    details=f"Page {page} NOT in frames (FAULT)"
                            + (f", evicted page {evicted}" if evicted is not None else ""),
                ))

        return self._build_page_result("FIFO", reference_string, num_frames,
                                        events, hits, faults)

    def simulate_lru(self, reference_string: List[int], num_frames: int) -> dict:
        """LRU page replacement: evict the least recently used page."""
        # OrderedDict: key order = access recency (last = most recent)
        frames: OrderedDict = OrderedDict()
        events: List[PageEvent] = []
        hits = 0
        faults = 0

        for step, page in enumerate(reference_string):
            if page in frames:
                hits += 1
                frames.move_to_end(page)  # Mark as most recently used
                events.append(PageEvent(
                    step=step, page_requested=page, is_hit=True,
                    page_evicted=None, frames_state=list(frames.keys()),
                    algorithm="LRU", details=f"Page {page} found in frames (HIT)",
                ))
            else:
                faults += 1
                evicted = None
                if len(frames) >= num_frames:
                    evicted, _ = frames.popitem(last=False)  # Remove LRU (first)
                frames[page] = True
                events.append(PageEvent(
                    step=step, page_requested=page, is_hit=False,
                    page_evicted=evicted, frames_state=list(frames.keys()),
                    algorithm="LRU",
                    details=f"Page {page} NOT in frames (FAULT)"
                            + (f", evicted LRU page {evicted}" if evicted is not None else ""),
                ))

        return self._build_page_result("LRU", reference_string, num_frames,
                                        events, hits, faults)

    def simulate_clock(self, reference_string: List[int], num_frames: int) -> dict:
        """
        Clock (Second-Chance) algorithm.

        Each frame has a reference bit. On a fault:
          - Advance the clock hand
          - If reference bit = 1, clear it and advance (second chance)
          - If reference bit = 0, evict this page
        """
        frames: List[Optional[int]] = [None] * num_frames
        ref_bits: List[int] = [0] * num_frames
        clock_hand = 0
        events: List[PageEvent] = []
        hits = 0
        faults = 0
        used_count = 0

        for step, page in enumerate(reference_string):
            # Check if page is already in frames
            found = -1
            for i in range(num_frames):
                if frames[i] == page:
                    found = i
                    break

            if found >= 0:
                hits += 1
                ref_bits[found] = 1  # Set reference bit
                events.append(PageEvent(
                    step=step, page_requested=page, is_hit=True,
                    page_evicted=None,
                    frames_state=[f for f in frames],
                    algorithm="Clock",
                    details=f"Page {page} found at frame {found} (HIT), ref bit set",
                ))
            else:
                faults += 1
                evicted = None

                if used_count < num_frames:
                    # Still have empty frames
                    frames[used_count] = page
                    ref_bits[used_count] = 1
                    used_count += 1
                    events.append(PageEvent(
                        step=step, page_requested=page, is_hit=False,
                        page_evicted=None,
                        frames_state=[f for f in frames],
                        algorithm="Clock",
                        details=f"Page {page} loaded into empty frame (FAULT)",
                    ))
                else:
                    # Clock algorithm: find victim
                    while True:
                        if ref_bits[clock_hand] == 0:
                            evicted = frames[clock_hand]
                            frames[clock_hand] = page
                            ref_bits[clock_hand] = 1
                            clock_hand = (clock_hand + 1) % num_frames
                            break
                        else:
                            ref_bits[clock_hand] = 0  # Second chance
                            clock_hand = (clock_hand + 1) % num_frames

                    events.append(PageEvent(
                        step=step, page_requested=page, is_hit=False,
                        page_evicted=evicted,
                        frames_state=[f for f in frames],
                        algorithm="Clock",
                        details=f"Page {page} replaces page {evicted} (FAULT, clock sweep)",
                    ))

        return self._build_page_result("Clock", reference_string, num_frames,
                                        events, hits, faults)

    def compare_algorithms(self, reference_string: List[int], num_frames: int) -> dict:
        """Run all three algorithms on the same input and compare results."""
        fifo = self.simulate_fifo(reference_string, num_frames)
        lru = self.simulate_lru(reference_string, num_frames)
        clock = self.simulate_clock(reference_string, num_frames)

        best = min(
            ["FIFO", "LRU", "Clock"],
            key=lambda a: {"FIFO": fifo, "LRU": lru, "Clock": clock}[a]["summary"]["page_faults"],
        )

        return {
            "reference_string": reference_string,
            "num_frames": num_frames,
            "total_references": len(reference_string),
            "best_algorithm": best,
            "algorithms": {
                "FIFO": fifo,
                "LRU": lru,
                "Clock": clock,
            },
            "comparison": {
                "FIFO_faults": fifo["summary"]["page_faults"],
                "LRU_faults": lru["summary"]["page_faults"],
                "Clock_faults": clock["summary"]["page_faults"],
                "FIFO_hit_rate": fifo["summary"]["hit_rate"],
                "LRU_hit_rate": lru["summary"]["hit_rate"],
                "Clock_hit_rate": clock["summary"]["hit_rate"],
                "best_algorithm": best,
            },
        }

    def _build_page_result(self, algorithm: str, ref_string: List[int],
                           num_frames: int, events: List[PageEvent],
                           hits: int, faults: int) -> dict:
        """Build result dict for a page replacement simulation."""
        total = len(ref_string)
        summary = {
            "total_references": total,
            "page_hits": hits,
            "page_faults": faults,
            "hit_rate": round(hits / total * 100, 1) if total > 0 else 0,
            "fault_rate": round(faults / total * 100, 1) if total > 0 else 0,
        }
        return {
            "algorithm": algorithm,
            "reference_string": ref_string,
            "num_frames": num_frames,
            # Direct root access for frontend compatibility
            "total_references": total,
            "page_hits": hits,
            "page_faults": faults,
            "fault_count": faults,
            "hit_rate": summary["hit_rate"],
            "fault_rate": summary["fault_rate"],
            "events": [
                {
                    "step": e.step,
                    "page_requested": e.page_requested,
                    "is_hit": e.is_hit,
                    "page_evicted": e.page_evicted,
                    "frames_state": e.frames_state,
                    "details": e.details,
                }
                for e in events
            ],
            "summary": summary,
        }

    @staticmethod
    def demo_reference_string() -> List[int]:
        """Standard demo page reference string."""
        return [7, 0, 1, 2, 0, 3, 0, 4, 2, 3, 0, 3, 2, 1, 2, 0, 1, 7, 0, 1]
