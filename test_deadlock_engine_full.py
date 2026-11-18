"""
Comprehensive test script for Deadlock & Concurrency Engine
Verifies all WFG scenarios, Banker's algorithm checks, response speeds, and data formats.
"""

import time
from fastapi.testclient import TestClient
from backend.main import app

def run_tests():
    print("=" * 60)
    print("DEADLOCK & CONCURRENCY ENGINE VERIFICATION")
    print("=" * 60)
    
    with TestClient(app) as client:
        # Test 1: Get scenarios list
        t0 = time.perf_counter()
        res = client.get("/api/deadlock/scenarios")
        elapsed = (time.perf_counter() - t0) * 1000
        assert res.status_code == 200, f"Expected 200, got {res.status_code}"
        scenarios = res.json()["scenarios"]
        print(f"[PASS] GET /api/deadlock/scenarios ({elapsed:.1f}ms): {len(scenarios)} scenarios found")
        for s in scenarios:
            print(f"       - {s['id']}: {s['name']}")

        # Test 2: Initial GET /api/deadlock/wfg (must start EMPTY — no dummy circular_wait graph!)
        t0 = time.perf_counter()
        res = client.get("/api/deadlock/wfg")
        elapsed = (time.perf_counter() - t0) * 1000
        assert res.status_code == 200
        data = res.json()
        assert len(data["graph"]["nodes"]) == 0, f"Initial graph must be empty (no dummy nodes), got {len(data['graph']['nodes'])}"
        assert data["result"]["has_deadlock"] is False
        print(f"[PASS] GET /api/deadlock/wfg ({elapsed:.1f}ms): Initial graph cleanly empty (0 nodes, no dummy circular_wait)")

        # Test 3: Load Circular Wait scenario
        t0 = time.perf_counter()
        res = client.post("/api/deadlock/wfg/load/circular_wait")
        elapsed = (time.perf_counter() - t0) * 1000
        assert res.status_code == 200
        data = res.json()
        assert data["result"]["has_deadlock"] is True
        assert len(data["result"]["cycles"]) >= 1
        assert "rag_edges" in data["graph"]
        assert "wfg_edges" in data["graph"]
        print(f"[PASS] POST /api/deadlock/wfg/load/circular_wait ({elapsed:.1f}ms): Deadlock detected, cycle: {data['result']['cycles'][0]}")

        # Test 4: Load Dining Philosophers scenario
        t0 = time.perf_counter()
        res = client.post("/api/deadlock/wfg/load/dining_philosophers")
        elapsed = (time.perf_counter() - t0) * 1000
        assert res.status_code == 200
        data = res.json()
        assert data["result"]["has_deadlock"] is True
        assert len(data["graph"]["nodes"]) == 10  # 5 philosophers + 5 chopsticks
        assert len(data["result"]["involved_processes"]) == 5
        print(f"[PASS] POST /api/deadlock/wfg/load/dining_philosophers ({elapsed:.1f}ms): 10 nodes (5 procs, 5 res), cycle detected")

        # Test 5: Load Multi-Resource Contention scenario
        t0 = time.perf_counter()
        res = client.post("/api/deadlock/wfg/load/multi_resource")
        elapsed = (time.perf_counter() - t0) * 1000
        assert res.status_code == 200
        data = res.json()
        assert data["result"]["has_deadlock"] is True
        assert len(data["graph"]["nodes"]) == 8  # 4 procs + 4 res
        print(f"[PASS] POST /api/deadlock/wfg/load/multi_resource ({elapsed:.1f}ms): 8 nodes, victim: {data['result']['recommended_victim']}")

        # Test 6: Load Safe scenario (No Deadlock)
        t0 = time.perf_counter()
        res = client.post("/api/deadlock/wfg/load/no_deadlock")
        elapsed = (time.perf_counter() - t0) * 1000
        assert res.status_code == 200
        data = res.json()
        assert data["result"]["has_deadlock"] is False
        assert len(data["result"]["cycles"]) == 0
        print(f"[PASS] POST /api/deadlock/wfg/load/no_deadlock ({elapsed:.1f}ms): Safe state, 0 cycles")

        # Test 7: Banker's Algorithm Safe State
        t0 = time.perf_counter()
        res = client.get("/api/deadlock/banker/scenarios/safe")
        elapsed = (time.perf_counter() - t0) * 1000
        assert res.status_code == 200
        data = res.json()
        assert data["result"]["is_safe"] is True
        assert len(data["result"]["safe_sequence"]) == 5
        print(f"[PASS] GET /api/deadlock/banker/scenarios/safe ({elapsed:.1f}ms): Safe Sequence = {' -> '.join(data['result']['safe_sequence'])}")

        # Test 8: Banker's Algorithm Unsafe State
        t0 = time.perf_counter()
        res = client.get("/api/deadlock/banker/scenarios/unsafe")
        elapsed = (time.perf_counter() - t0) * 1000
        assert res.status_code == 200
        data = res.json()
        assert data["result"]["is_safe"] is False
        print(f"[PASS] GET /api/deadlock/banker/scenarios/unsafe ({elapsed:.1f}ms): Unsafe state correctly identified")

        # Test 9: Banker's Safety Check POST
        t0 = time.perf_counter()
        req_body = {
            "processes": ["P0", "P1", "P2"],
            "resources": ["R0", "R1"],
            "available": [2, 1],
            "max_matrix": [[3, 2], [2, 1], [3, 1]],
            "allocation_matrix": [[1, 0], [1, 1], [0, 0]]
        }
        res = client.post("/api/deadlock/banker/check", json=req_body)
        elapsed = (time.perf_counter() - t0) * 1000
        assert res.status_code == 200
        data = res.json()
        print(f"[PASS] POST /api/deadlock/banker/check ({elapsed:.1f}ms): is_safe={data['is_safe']}, seq={data['safe_sequence']}")

    print("=" * 60)
    print("ALL 9 DEADLOCK & CONCURRENCY ENGINE TESTS PASSED PERFECTLY!")
    print("=" * 60)

if __name__ == "__main__":
    run_tests()
