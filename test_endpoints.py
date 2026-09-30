import urllib.request
import json

print("=== Testing MLFQ demo ===")
try:
    req = urllib.request.Request('http://localhost:8000/api/simulator/mlfq/demo')
    res = urllib.request.urlopen(req, timeout=5)
    data = json.loads(res.read())
    procs = data.get('processes', [])
    gantt = data.get('gantt_chart', [])
    print("OK: {} processes, {} gantt slices".format(len(procs), len(gantt)))
    for p in procs:
        print("  {} final_queue={} burst={} completion={}".format(
            p['id'], p['final_queue'], p['total_burst'], p['completion_time']))
except Exception as e:
    print("FAIL: {}".format(e))

print()
print("=== Testing Page Replacement ===")
try:
    body = json.dumps({
        'reference_string': [7,0,1,2,0,3,0,4,2,3,0,3,2,1,2,0,1,7,0,1],
        'num_frames': 3
    }).encode()
    req = urllib.request.Request(
        'http://localhost:8000/api/simulator/page-replacement',
        data=body,
        headers={'Content-Type': 'application/json'}
    )
    res = urllib.request.urlopen(req, timeout=5)
    data = json.loads(res.read())
    algos = data.get('algorithms', {})
    print("OK: best={} algos={}".format(data.get('best_algorithm'), list(algos.keys())))
    for name, algo in algos.items():
        evts   = algo.get('events', [])
        faults = algo.get('page_faults', '?')
        print("  {}: {} faults, {} events".format(name, faults, len(evts)))
except Exception as e:
    print("FAIL: {}".format(e))
