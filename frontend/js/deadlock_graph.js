/**
 * deadlock_graph.js — Wait-For Graph Canvas Renderer & Banker's Algorithm Visualizer
 * 
 * Demonstrates:
 *   - Resource Allocation Graph (RAG) / Wait-For Graph (WFG) cycle detection (Tarjan's DFS)
 *   - Interactive HTML5 Canvas rendering of process & resource nodes and directed dependencies
 *   - Deadlock cycle highlight in glowing crimson and optimal victim selection
 *   - Dijkstra's Banker's Algorithm state evaluation with dynamic matrix tables
 */

const DeadlockGraph = (() => {
    let canvas, ctx;
    let currentGraph = { nodes: [], edges: [] };
    let currentResult = null;
    let animFrameId = null;
    let pulsePhase = 0;

    // Banker's Algorithm current state
    let bankerState = null;

    function init() {
        canvas = document.getElementById('wfgCanvas');
        if (canvas) {
            ctx = canvas.getContext('2d');
            resizeCanvas();
            window.addEventListener('resize', () => {
                resizeCanvas();
                drawGraph();
            });
        }

        // WFG Controls
        const btnLoadWfg = document.getElementById('btnLoadWfgScenario');
        const btnDetect = document.getElementById('btnDetectDeadlock');
        const scenarioSelect = document.getElementById('wfgScenarioSelect');

        if (btnLoadWfg) {
            btnLoadWfg.addEventListener('click', () => {
                const scen = scenarioSelect?.value || 'circular_wait';
                loadWfgScenario(scen);
            });
        }

        if (btnDetect) {
            btnDetect.addEventListener('click', fetchWfg);
        }

        // Banker's Controls
        const btnSafeDemo = document.getElementById('btnBankerSafeDemo');
        const btnUnsafeDemo = document.getElementById('btnBankerUnsafeDemo');
        const btnCheck = document.getElementById('btnRunBankerCheck');

        if (btnSafeDemo) {
            btnSafeDemo.addEventListener('click', () => loadBankerScenario('safe'));
        }
        if (btnUnsafeDemo) {
            btnUnsafeDemo.addEventListener('click', () => loadBankerScenario('unsafe'));
        }
        if (btnCheck) {
            btnCheck.addEventListener('click', runBankerSafetyCheck);
        }

        // Load default circular wait scenario initially
        loadWfgScenario('circular_wait');
        loadBankerScenario('safe');
    }

    function isTabVisible() {
        const panel = document.getElementById('panelDeadlock');
        return panel && panel.classList.contains('active');
    }

    function onTabActivated() {
        resizeCanvas();
        layoutGraphNodes();
        drawGraph();
        if (currentResult && currentResult.has_deadlock) {
            startAnimationLoop();
        }
    }

    function onTabDeactivated() {
        stopAnimationLoop();
    }

    function resizeCanvas() {
        if (!canvas) return;
        const rect = canvas.parentElement.getBoundingClientRect();
        canvas.width = rect.width;
        canvas.height = 360;
    }

    function startAnimationLoop() {
        stopAnimationLoop();
        if (!isTabVisible() || !currentResult || !currentResult.has_deadlock) {
            return;
        }
        function loop() {
            if (!isTabVisible()) {
                animFrameId = null;
                return;
            }
            pulsePhase = (pulsePhase + 0.05) % (Math.PI * 2);
            drawGraph();
            animFrameId = requestAnimationFrame(loop);
        }
        animFrameId = requestAnimationFrame(loop);
    }

    function stopAnimationLoop() {
        if (animFrameId) {
            cancelAnimationFrame(animFrameId);
            animFrameId = null;
        }
    }

    async function loadWfgScenario(scenarioId) {
        try {
            const res = await fetch(`/api/deadlock/wfg/load/${scenarioId}`, { method: 'POST' });
            if (res.ok) {
                const data = await res.json();
                currentGraph = data.graph;
                currentResult = data.result;
                layoutGraphNodes();
                drawGraph();
                renderWfgResults(currentResult);
                App.showToast(`Loaded WFG scenario: ${scenarioId}`, 'info');
            }
        } catch (e) {
            console.error('Failed to load WFG scenario:', e);
        }
    }

    async function fetchWfg() {
        try {
            const res = await fetch('/api/deadlock/wfg');
            if (res.ok) {
                const data = await res.json();
                currentGraph = data.graph;
                currentResult = data.result;
                layoutGraphNodes();
                drawGraph();
                renderWfgResults(currentResult);
                App.showToast(currentResult.has_deadlock ? '⚠️ Deadlock cycle detected!' : '✅ No deadlock detected', currentResult.has_deadlock ? 'error' : 'success');
            }
        } catch (e) {
            console.error('Failed to fetch WFG:', e);
        }
    }

    function layoutGraphNodes() {
        if (!canvas || !currentGraph || !currentGraph.nodes) return;

        const w = canvas.width;
        const h = canvas.height;
        const centerX = w / 2;
        const centerY = h / 2;
        const radius = Math.min(centerX, centerY) * 0.72;

        const nodes = currentGraph.nodes;
        const count = nodes.length;

        for (let i = 0; i < count; i++) {
            const angle = (i / count) * (Math.PI * 2) - Math.PI / 2;
            nodes[i].x = centerX + Math.cos(angle) * radius;
            nodes[i].y = centerY + Math.sin(angle) * radius;
        }
    }

    function drawGraph() {
        if (!canvas || !ctx || !currentGraph) return;

        const w = canvas.width;
        const h = canvas.height;
        ctx.clearRect(0, 0, w, h);

        const nodeMap = new Map();
        for (const n of (currentGraph.nodes || [])) {
            nodeMap.set(n.id, n);
        }

        const involvedProcs = new Set(currentResult?.involved_processes || []);
        const involvedRes = new Set(currentResult?.involved_resources || []);
        const victimId = currentResult?.recommended_victim;

        // 1. Draw Edges
        for (const edge of (currentGraph.edges || [])) {
            const fromNode = nodeMap.get(edge.from);
            const toNode = nodeMap.get(edge.to);
            if (!fromNode || !toNode) continue;

            const isDeadlockedEdge = (involvedProcs.has(edge.from) || involvedRes.has(edge.from)) &&
                                     (involvedProcs.has(edge.to) || involvedRes.has(edge.to));

            drawArrow(
                fromNode.x, fromNode.y,
                toNode.x, toNode.y,
                edge.type === 'request' ? 'dashed' : 'solid',
                isDeadlockedEdge
            );
        }

        // 2. Draw Nodes
        for (const node of (currentGraph.nodes || [])) {
            const isProc = node.type === 'process';
            const isDeadlocked = isProc ? involvedProcs.has(node.id) : involvedRes.has(node.id);
            const isVictim = node.id === victimId;

            drawNode(node, isProc, isDeadlocked, isVictim);
        }
    }

    function drawNode(node, isProc, isDeadlocked, isVictim) {
        const x = node.x;
        const y = node.y;

        ctx.save();

        if (isVictim) {
            // Glowing amber halo for victim
            const pulse = Math.sin(pulsePhase) * 6 + 12;
            ctx.shadowColor = '#f59e0b';
            ctx.shadowBlur = pulse;
            ctx.strokeStyle = '#f59e0b';
            ctx.lineWidth = 3;
        } else if (isDeadlocked) {
            // Glowing red halo for deadlock
            const pulse = Math.sin(pulsePhase) * 6 + 10;
            ctx.shadowColor = '#f43f5e';
            ctx.shadowBlur = pulse;
            ctx.strokeStyle = '#f43f5e';
            ctx.lineWidth = 2.5;
        } else {
            ctx.shadowColor = 'transparent';
            ctx.strokeStyle = isProc ? '#6366f1' : '#06b6d4';
            ctx.lineWidth = 2;
        }

        if (isProc) {
            // Process Node: Circle
            const r = 24;
            ctx.beginPath();
            ctx.arc(x, y, r, 0, Math.PI * 2);
            ctx.fillStyle = isVictim ? 'rgba(245, 158, 11, 0.25)' : (isDeadlocked ? 'rgba(244, 63, 94, 0.25)' : 'rgba(99, 102, 241, 0.2)');
            ctx.fill();
            ctx.stroke();

            // Label
            ctx.shadowColor = 'transparent';
            ctx.fillStyle = '#f1f5f9';
            ctx.font = 'bold 11px "JetBrains Mono", monospace';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(node.label || node.id, x, y - 2);

            if (isVictim) {
                ctx.fillStyle = '#f59e0b';
                ctx.font = 'bold 8px "Inter", sans-serif';
                ctx.fillText('VICTIM', x, y + 10);
            }
        } else {
            // Resource Node: Rounded Rectangle
            const rw = 48;
            const rh = 36;
            const rx = x - rw / 2;
            const ry = y - rh / 2;

            ctx.beginPath();
            ctx.roundRect(rx, ry, rw, rh, 6);
            ctx.fillStyle = isDeadlocked ? 'rgba(244, 63, 94, 0.25)' : 'rgba(6, 182, 212, 0.2)';
            ctx.fill();
            ctx.stroke();

            // Label
            ctx.shadowColor = 'transparent';
            ctx.fillStyle = '#06b6d4';
            ctx.font = 'bold 10px "JetBrains Mono", monospace';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(node.label || node.id, x, y - 4);

            // Instances count
            ctx.fillStyle = '#94a3b8';
            ctx.font = '9px "JetBrains Mono", monospace';
            const total = node.total !== undefined ? node.total : (node.instances || 1);
            const inst = node.available !== undefined ? `${node.available}/${total}` : `${total} inst`;
            ctx.fillText(inst, x, y + 9);
        }

        ctx.restore();
    }

    function drawArrow(fromX, fromY, toX, toY, style, isDeadlocked) {
        ctx.save();

        const dx = toX - fromX;
        const dy = toY - fromY;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist === 0) return;

        const nodeRadius = 26;
        const startX = fromX + (dx / dist) * nodeRadius;
        const startY = fromY + (dy / dist) * nodeRadius;
        const endX = toX - (dx / dist) * nodeRadius;
        const endY = toY - (dy / dist) * nodeRadius;

        ctx.strokeStyle = isDeadlocked ? '#f43f5e' : (style === 'dashed' ? 'rgba(245, 158, 11, 0.7)' : 'rgba(99, 102, 241, 0.7)');
        ctx.lineWidth = isDeadlocked ? 2.5 : 1.8;

        if (style === 'dashed') {
            ctx.setLineDash([4, 4]);
        } else {
            ctx.setLineDash([]);
        }

        ctx.beginPath();
        ctx.moveTo(startX, startY);
        ctx.lineTo(endX, endY);
        ctx.stroke();

        // Arrowhead
        const angle = Math.atan2(endY - startY, endX - startX);
        const headLen = 9;

        ctx.setLineDash([]);
        ctx.fillStyle = ctx.strokeStyle;
        ctx.beginPath();
        ctx.moveTo(endX, endY);
        ctx.lineTo(endX - headLen * Math.cos(angle - Math.PI / 6), endY - headLen * Math.sin(angle - Math.PI / 6));
        ctx.lineTo(endX - headLen * Math.cos(angle + Math.PI / 6), endY - headLen * Math.sin(angle + Math.PI / 6));
        ctx.closePath();
        ctx.fill();

        ctx.restore();
    }

    function renderWfgResults(res) {
        const title = document.getElementById('wfgStatusTitle');
        const detail = document.getElementById('wfgStatusDetail');
        const box = document.getElementById('wfgResultBox');

        if (!res) return;

        if (res.has_deadlock) {
            box.style.borderColor = 'rgba(244, 63, 94, 0.5)';
            box.style.background = 'rgba(244, 63, 94, 0.08)';
            title.innerHTML = `⚠️ <span style="color: var(--accent-rose);">DEADLOCK DETECTED (${res.cycles?.length || 1} Circular Wait Cycles)</span>`;
            detail.innerHTML = `
                <div><strong>Involved Processes:</strong> ${res.involved_processes?.join(', ')}</div>
                <div><strong>Involved Resources:</strong> ${res.involved_resources?.join(', ')}</div>
                <div style="margin-top: 6px; color: var(--accent-amber);">
                    🎯 <strong>Optimal Preemption Victim:</strong> ${res.recommended_victim} 
                    <br><span style="font-size: 0.72rem; color: var(--text-tertiary);">${res.victim_reason || ''}</span>
                </div>
            `;
        } else {
            box.style.borderColor = 'rgba(16, 185, 129, 0.5)';
            box.style.background = 'rgba(16, 185, 129, 0.08)';
            title.innerHTML = `✅ <span style="color: var(--accent-emerald);">SAFE — No Circular Wait Condition Detected</span>`;
            detail.textContent = 'All requested resources can be granted or scheduled without cyclic blocking.';
        }
    }

    // ── Banker's Algorithm ──────────────────────────────────────────────────

    async function loadBankerScenario(scenarioId) {
        try {
            const res = await fetch(`/api/deadlock/banker/scenarios/${scenarioId}`);
            if (res.ok) {
                const data = await res.json();
                bankerState = data.scenario;
                renderBankerTable(data.scenario, data.result);
                App.showToast(`Loaded Banker's ${scenarioId.toUpperCase()} scenario`, 'info');
            }
        } catch (e) {
            console.error('Failed to load Banker scenario:', e);
        }
    }

    async function runBankerSafetyCheck() {
        if (!bankerState) return;

        try {
            const res = await fetch('/api/deadlock/banker/check', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    processes: bankerState.processes,
                    resources: bankerState.resources,
                    available: bankerState.available,
                    max_matrix: bankerState.max_matrix,
                    allocation_matrix: bankerState.allocation_matrix
                })
            });

            if (res.ok) {
                const result = await res.json();
                renderBankerTable(bankerState, result);
                App.showToast(result.is_safe ? 'Banker: Safe Sequence exists!' : 'Banker: UNSAFE state detected!', result.is_safe ? 'success' : 'error');
            }
        } catch (e) {
            console.error('Failed to check Banker safety:', e);
        }
    }

    function renderBankerTable(scen, result) {
        const tbody = document.getElementById('bankerTableBody');
        const availVec = document.getElementById('bankerAvailableVec');
        const resultBox = document.getElementById('bankerResultBox');

        if (!tbody || !scen) return;

        if (availVec && scen.available) {
            availVec.textContent = `[ ${scen.available.join('  ')} ]`;
        }

        const procs = scen.processes || [];
        const alloc = scen.allocation_matrix || [];
        const max = scen.max_matrix || [];
        const need = result?.need_matrix || [];

        let html = '';
        for (let i = 0; i < procs.length; i++) {
            const allocStr = alloc[i] ? alloc[i].join('  ') : '-';
            const maxStr = max[i] ? max[i].join('  ') : '-';
            const needStr = need[i] ? need[i].join('  ') : (max[i] && alloc[i] ? max[i].map((m, idx) => m - alloc[i][idx]).join('  ') : '-');

            html += `
                <tr>
                    <td style="font-weight: 700; color: var(--accent-indigo);">${procs[i]}</td>
                    <td>[ ${allocStr} ]</td>
                    <td>[ ${maxStr} ]</td>
                    <td style="color: var(--accent-cyan); font-weight: 600;">[ ${needStr} ]</td>
                </tr>
            `;
        }
        tbody.innerHTML = html;

        if (result && resultBox) {
            resultBox.style.display = 'block';
            if (result.is_safe) {
                resultBox.className = 'banker-result safe';
                resultBox.innerHTML = `
                    <div style="font-size: 0.9rem; margin-bottom: 4px;">✅ System State is <strong>SAFE</strong></div>
                    <div style="font-size: 0.78rem; font-family: var(--font-mono);">
                        Safe Execution Sequence: <strong>${result.safe_sequence ? result.safe_sequence.join(' &rarr; ') : 'None'}</strong>
                    </div>
                `;
            } else {
                resultBox.className = 'banker-result unsafe';
                resultBox.innerHTML = `
                    <div style="font-size: 0.9rem; margin-bottom: 4px;">❌ System State is <strong>UNSAFE (Deadlock Risk)</strong></div>
                    <div style="font-size: 0.78rem;">
                        No safe execution sequence exists. Available resources cannot guarantee completion without preemption.
                    </div>
                `;
            }
        }
    }

    return {
        init,
        loadWfgScenario,
        fetchWfg,
        loadBankerScenario,
        runBankerSafetyCheck,
        onTabActivated,
        onTabDeactivated
    };
})();
