/**
 * deadlock_graph.js — Wait-For Graph Canvas Renderer & Banker's Algorithm Visualizer
 * 
 * Demonstrates:
 *   - Resource Allocation Graph (RAG) & Wait-For Graph (WFG) cycle detection (Tarjan's DFS)
 *   - Dual View Modes: RAG (Process + Resource nodes) and WFG (Pure Process Wait-For)
 *   - Interactive HTML5 Canvas rendering with High-DPI support and instant caching
 *   - Deadlock cycle highlight in glowing crimson and optimal victim preemption selection
 *   - Dijkstra's Banker's Algorithm state evaluation with dynamic matrix tables
 */

const DeadlockGraph = (() => {
    let canvas, ctx;
    let currentGraph = { nodes: [], edges: [], rag_edges: [], wfg_edges: [] };
    let currentResult = null;
    let animFrameId = null;
    let pulsePhase = 0;
    let currentScenarioId = 'circular_wait';
    let currentViewMode = 'rag'; // 'rag' (Process + Resource) or 'wfg' (Process only)
    let isFetching = false;

    // Fast-path client caches for instantaneous switching (0ms latency)
    const scenarioCache = {};
    const bankerCache = {};

    // Banker's Algorithm current state
    let bankerState = null;

    function init() {
        canvas = document.getElementById('wfgCanvas');
        if (canvas) {
            ctx = canvas.getContext('2d');
            resizeCanvas();
            window.addEventListener('resize', () => {
                resizeCanvas();
                layoutGraphNodes();
                drawGraph();
            });
        }

        // WFG Controls
        const btnLoadWfg = document.getElementById('btnLoadWfgScenario');
        const btnDetect = document.getElementById('btnDetectDeadlock');
        const scenarioSelect = document.getElementById('wfgScenarioSelect');
        const btnToggleMode = document.getElementById('btnToggleGraphView');

        if (btnLoadWfg) {
            btnLoadWfg.addEventListener('click', () => {
                const scen = scenarioSelect?.value || 'circular_wait';
                loadWfgScenario(scen, true);
            });
        }

        if (scenarioSelect) {
            scenarioSelect.addEventListener('change', () => {
                loadWfgScenario(scenarioSelect.value, true);
            });
        }

        if (btnDetect) {
            btnDetect.addEventListener('click', () => fetchWfg(true));
        }

        if (btnToggleMode) {
            btnToggleMode.addEventListener('click', toggleViewMode);
        }

        // Banker's Controls
        const btnSafeDemo = document.getElementById('btnBankerSafeDemo');
        const btnUnsafeDemo = document.getElementById('btnBankerUnsafeDemo');
        const btnCheck = document.getElementById('btnRunBankerCheck');

        if (btnSafeDemo) {
            btnSafeDemo.addEventListener('click', () => loadBankerScenario('safe', true));
        }
        if (btnUnsafeDemo) {
            btnUnsafeDemo.addEventListener('click', () => loadBankerScenario('unsafe', true));
        }
        if (btnCheck) {
            btnCheck.addEventListener('click', runBankerSafetyCheck);
        }

        // Initial background fetch (silent, no popup toasts on initial page load)
        loadWfgScenario('circular_wait', false);
        loadBankerScenario('safe', false);
    }

    function toggleViewMode() {
        currentViewMode = currentViewMode === 'rag' ? 'wfg' : 'rag';
        const btn = document.getElementById('btnToggleGraphView');
        if (btn) {
            btn.textContent = currentViewMode === 'rag' ? 'Mode: RAG (P + R)' : 'Mode: WFG (P only)';
            btn.title = currentViewMode === 'rag'
                ? 'Showing Resource Allocation Graph (Processes + Resources)'
                : 'Showing Wait-For Graph (Direct Process Dependencies)';
        }
        resizeCanvas();
        layoutGraphNodes();
        drawGraph();
        App.showToast(
            currentViewMode === 'rag'
                ? 'Switched to Resource Allocation Graph view'
                : 'Switched to Wait-For Graph (direct wait) view',
            'info'
        );
    }

    function isTabVisible() {
        const panel = document.getElementById('panelDeadlock');
        return panel && panel.classList.contains('active');
    }

    function onTabActivated() {
        // Immediate render with fallback dimensions
        resizeCanvas();
        layoutGraphNodes();
        drawGraph();

        // Secondary pass via requestAnimationFrame once browser reflows panel geometry
        requestAnimationFrame(() => {
            resizeCanvas();
            layoutGraphNodes();
            drawGraph();
            if (currentResult && currentResult.has_deadlock) {
                startAnimationLoop();
            }
        });

        // If graph is empty (e.g. startup fetch failed or server wasn't ready), fetch now!
        if (!currentGraph || !currentGraph.nodes || currentGraph.nodes.length === 0) {
            loadWfgScenario(currentScenarioId || 'circular_wait', false);
        }

        // If banker's state is empty, load it
        if (!bankerState) {
            loadBankerScenario('safe', false);
        }
    }

    function onTabDeactivated() {
        stopAnimationLoop();
    }

    function resizeCanvas() {
        if (!canvas) return;
        const parent = canvas.parentElement;
        let w = 0;
        if (parent) {
            const rect = parent.getBoundingClientRect();
            w = Math.floor(rect.width || parent.clientWidth || parent.offsetWidth || 0);
        }

        // Robust fallback: compute from deadlock panel, main content, or window
        if (!w || w < 200) {
            const panel = document.getElementById('panelDeadlock');
            const main = document.querySelector('.main-content') || document.body;
            w = Math.floor(panel?.clientWidth || (main ? (main.clientWidth - 80) : 0) || 720);
        }

        const dpr = window.devicePixelRatio || 1;
        const parentWidth = parent ? (parent.clientWidth || w) : w;
        const cssWidth = Math.min(Math.max(280, w), parentWidth || 600);
        const cssHeight = 360;

        canvas.style.width = `${cssWidth}px`;
        canvas.style.height = `${cssHeight}px`;
        canvas.width = Math.floor(cssWidth * dpr);
        canvas.height = Math.floor(cssHeight * dpr);

        if (ctx) {
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        }
    }

    function getCssDimensions() {
        const cssWidth = canvas && canvas.style.width ? parseFloat(canvas.style.width) : (canvas ? canvas.width : 600);
        const cssHeight = canvas && canvas.style.height ? parseFloat(canvas.style.height) : (canvas ? canvas.height : 360);
        return { w: cssWidth, h: cssHeight };
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
            pulsePhase = (pulsePhase + 0.04) % (Math.PI * 2);
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

    function setLoadingState(loading, message = 'Evaluating Wait-For Graph...') {
        isFetching = loading;
        const title = document.getElementById('wfgStatusTitle');
        const btnLoad = document.getElementById('btnLoadWfgScenario');
        const btnDetect = document.getElementById('btnDetectDeadlock');

        if (btnLoad) btnLoad.disabled = loading;
        if (btnDetect) btnDetect.disabled = loading;

        if (loading && title) {
            title.innerHTML = `<span style="color: var(--text-secondary);">${message}</span>`;
        }
    }

    async function loadWfgScenario(scenarioId, isManual = false) {
        currentScenarioId = scenarioId;

        // Fast-path: Instant display from client cache (0ms latency)
        if (scenarioCache[scenarioId]) {
            applyGraphData(scenarioCache[scenarioId]);
            if (isManual) {
                App.showToast(`Loaded scenario: ${scenarioId}`, 'info');
            }
        }

        setLoadingState(true, `Loading ${scenarioId} scenario...`);

        try {
            const res = await fetch(`/api/deadlock/wfg/load/${scenarioId}`, { method: 'POST' });
            if (res.ok) {
                const data = await res.json();
                scenarioCache[scenarioId] = data;
                applyGraphData(data);
                if (isManual && !scenarioCache[scenarioId + '_notified']) {
                    App.showToast(`Loaded scenario: ${scenarioId}`, 'info');
                }
            } else {
                console.error(`Failed to load scenario ${scenarioId}: HTTP ${res.status}`);
            }
        } catch (e) {
            console.error('Failed to load WFG scenario:', e);
            if (!scenarioCache[scenarioId]) {
                const title = document.getElementById('wfgStatusTitle');
                if (title) title.innerHTML = `<span style="color: var(--status-danger-text); font-weight: 600;">Failed to load scenario: ${e.message}</span>`;
            }
        } finally {
            setLoadingState(false);
        }
    }

    async function fetchWfg(isManual = false) {
        setLoadingState(true, 'Running Tarjan\'s DFS cycle detection...');
        try {
            const res = await fetch('/api/deadlock/wfg');
            if (res.ok) {
                const data = await res.json();
                // If backend returned empty graph, fall back to default scenario
                if (!data.graph || !data.graph.nodes || data.graph.nodes.length === 0) {
                    await loadWfgScenario('circular_wait', isManual);
                    return;
                }
                applyGraphData(data);
                if (isManual) {
                    App.showToast(
                        data.result.has_deadlock ? 'Deadlock cycle detected!' : 'No deadlock detected',
                        data.result.has_deadlock ? 'error' : 'success'
                    );
                }
            }
        } catch (e) {
            console.error('Failed to fetch WFG:', e);
            if (isManual) {
                App.showToast('Failed to evaluate deadlock: ' + e.message, 'error');
            }
        } finally {
            setLoadingState(false);
        }
    }

    function applyGraphData(data) {
        currentGraph = data.graph || { nodes: [], edges: [] };
        currentResult = data.result || null;

        resizeCanvas();
        layoutGraphNodes();
        drawGraph();
        renderWfgResults(currentResult);

        if (currentResult && currentResult.has_deadlock) {
            startAnimationLoop();
        } else {
            stopAnimationLoop();
        }
    }

    function getActiveNodes() {
        if (!currentGraph || !currentGraph.nodes) return [];
        if (currentViewMode === 'wfg') {
            return currentGraph.nodes.filter(n => n.type === 'process');
        }
        return currentGraph.nodes;
    }

    function getActiveEdges() {
        if (!currentGraph) return [];
        if (currentViewMode === 'wfg') {
            return currentGraph.wfg_edges || currentGraph.edges || [];
        }
        return currentGraph.rag_edges || currentGraph.edges || [];
    }

    function layoutGraphNodes() {
        if (!canvas || !currentGraph || !currentGraph.nodes) return;

        const { w, h } = getCssDimensions();
        const centerX = w / 2;
        const centerY = h / 2;
        const radius = Math.min(centerX, centerY) * 0.72;

        const nodes = getActiveNodes();
        const count = nodes.length;
        if (count === 0) return;

        // Separate processes and resources
        const procs = nodes.filter(n => n.type === 'process');
        const res = nodes.filter(n => n.type === 'resource');

        if (currentViewMode === 'rag' && procs.length > 0 && res.length > 0 && procs.length === res.length) {
            // Interleave processes and resources along the circle for a natural dependency ring
            const ordered = [];
            for (let i = 0; i < procs.length; i++) {
                ordered.push(procs[i]);
                ordered.push(res[i]);
            }
            const total = ordered.length;
            for (let i = 0; i < total; i++) {
                const angle = (i / total) * (Math.PI * 2) - Math.PI / 2;
                ordered[i].x = centerX + Math.cos(angle) * radius;
                ordered[i].y = centerY + Math.sin(angle) * radius;
            }
        } else {
            // Equiangular circular distribution
            for (let i = 0; i < count; i++) {
                const angle = (i / count) * (Math.PI * 2) - Math.PI / 2;
                nodes[i].x = centerX + Math.cos(angle) * radius;
                nodes[i].y = centerY + Math.sin(angle) * radius;
            }
        }
    }

    function drawGraph() {
        if (!canvas || !ctx) return;

        const { w, h } = getCssDimensions();
        ctx.clearRect(0, 0, w, h);

        const nodes = getActiveNodes();
        const edges = getActiveEdges();

        if (nodes.length === 0) {
            ctx.save();
            ctx.fillStyle = '#625D62';
            ctx.font = '13px "Inter", sans-serif';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText('No graph nodes loaded. Select a scenario above.', w / 2, h / 2);
            ctx.restore();
            return;
        }

        const nodeMap = new Map();
        for (const n of nodes) {
            nodeMap.set(n.id, n);
        }

        const involvedProcs = new Set(currentResult?.involved_processes || []);
        const involvedRes = new Set(currentResult?.involved_resources || []);
        const victimId = currentResult?.recommended_victim;

        // 1. Draw Edges
        for (const edge of edges) {
            const fromNode = nodeMap.get(edge.from);
            const toNode = nodeMap.get(edge.to);
            if (!fromNode || !toNode) continue;

            const isDeadlockedEdge = (involvedProcs.has(edge.from) || involvedRes.has(edge.from)) &&
                                     (involvedProcs.has(edge.to) || involvedRes.has(edge.to));

            drawArrow(
                fromNode.x, fromNode.y,
                toNode.x, toNode.y,
                edge.type === 'request' ? 'dashed' : 'solid',
                isDeadlockedEdge,
                edge.label || (edge.type === 'request' ? 'requests' : (edge.type === 'wait' ? edge.resource : 'holds'))
            );
        }

        // 2. Draw Nodes
        for (const node of nodes) {
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

        ctx.shadowColor = 'transparent';
        ctx.shadowBlur = 0;

        if (isVictim) {
            // Subtle stroke width pulse without neon blur
            const pulse = Math.sin(pulsePhase) * 0.6 + 2.6;
            ctx.strokeStyle = '#B98532';
            ctx.lineWidth = pulse;
        } else if (isDeadlocked) {
            // Subtle stroke width pulse without neon blur
            const pulse = Math.sin(pulsePhase) * 0.5 + 2.4;
            ctx.strokeStyle = '#C85D5D';
            ctx.lineWidth = pulse;
        } else {
            ctx.strokeStyle = isProc ? '#52748D' : '#298F93';
            ctx.lineWidth = 1.8;
        }

        if (isProc) {
            // Process Node: Circle
            const r = 24;
            ctx.beginPath();
            ctx.arc(x, y, r, 0, Math.PI * 2);
            ctx.fillStyle = isVictim ? 'rgba(185, 133, 50, 0.16)' : (isDeadlocked ? 'rgba(200, 93, 93, 0.16)' : 'rgba(82, 116, 141, 0.12)');
            ctx.fill();
            ctx.stroke();

            // Label
            ctx.fillStyle = '#323035';
            ctx.font = 'bold 11px "JetBrains Mono", monospace';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(node.label || node.name || node.id, x, y - 2);

            if (isVictim) {
                ctx.fillStyle = '#6E490D';
                ctx.font = 'bold 8px "Inter", sans-serif';
                ctx.fillText('VICTIM', x, y + 10);
            } else if (node.priority !== undefined) {
                ctx.fillStyle = '#625D62';
                ctx.font = '8px "Inter", sans-serif';
                ctx.fillText(`pri: ${node.priority}`, x, y + 10);
            }
        } else {
            // Resource Node: Rounded Rectangle
            const rw = 52;
            const rh = 36;
            const rx = x - rw / 2;
            const ry = y - rh / 2;

            ctx.beginPath();
            ctx.roundRect(rx, ry, rw, rh, 6);
            ctx.fillStyle = isDeadlocked ? 'rgba(200, 93, 93, 0.16)' : 'rgba(41, 143, 147, 0.12)';
            ctx.fill();
            ctx.stroke();

            // Label
            ctx.fillStyle = '#21777B';
            ctx.font = 'bold 10px "JetBrains Mono", monospace';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(node.label || node.name || node.id, x, y - 4);

            // Instances count
            ctx.fillStyle = '#625D62';
            ctx.font = '9px "JetBrains Mono", monospace';
            const total = node.total !== undefined ? node.total : (node.instances || 1);
            const inst = node.available !== undefined ? `${node.available}/${total} inst` : `${total} inst`;
            ctx.fillText(inst, x, y + 9);
        }

        ctx.restore();
    }

    function drawArrow(fromX, fromY, toX, toY, style, isDeadlocked, label) {
        ctx.save();

        const dx = toX - fromX;
        const dy = toY - fromY;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist === 0) {
            ctx.restore();
            return;
        }

        const nodeRadius = 26;
        const startX = fromX + (dx / dist) * nodeRadius;
        const startY = fromY + (dy / dist) * nodeRadius;
        const endX = toX - (dx / dist) * nodeRadius;
        const endY = toY - (dy / dist) * nodeRadius;

        ctx.strokeStyle = isDeadlocked ? '#C85D5D' : (style === 'dashed' ? '#B98532' : '#52748D');
        ctx.lineWidth = isDeadlocked ? 2.2 : 1.6;

        if (style === 'dashed') {
            ctx.setLineDash([5, 4]);
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

        // Optional edge label
        if (label && dist > 70) {
            const midX = (startX + endX) / 2;
            const midY = (startY + endY) / 2;
            ctx.fillStyle = isDeadlocked ? '#7E2B2B' : '#625D62';
            ctx.font = '8px "JetBrains Mono", monospace';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(label, midX, midY - 6);
        }

        ctx.restore();
    }

    function renderWfgResults(res) {
        const title = document.getElementById('wfgStatusTitle');
        const detail = document.getElementById('wfgStatusDetail');
        const box = document.getElementById('wfgResultBox');

        if (!res || !box) return;

        if (res.has_deadlock) {
            box.style.borderColor = 'var(--status-danger)';
            box.style.background = 'var(--status-danger-bg)';
            const cycleCount = res.cycles?.length || 1;
            const cyclePath = res.cycles && res.cycles[0] ? res.cycles[0].join(' &rarr; ') : '';

            title.innerHTML = `<span style="color: var(--status-danger-text); font-weight: 700;">DEADLOCK DETECTED (${cycleCount} Circular Wait Cycle${cycleCount > 1 ? 's' : ''})</span>`;
            detail.innerHTML = `
                ${cyclePath ? `<div style="margin-bottom: 6px; font-family: var(--font-mono); font-size: 0.8rem; color: var(--status-danger-text);"><strong>Cycle:</strong> ${cyclePath}</div>` : ''}
                <div style="color: var(--text-primary); margin-bottom: 2px;"><strong>Involved Processes:</strong> ${res.involved_processes?.join(', ') || 'None'}</div>
                <div style="color: var(--text-primary); margin-bottom: 4px;"><strong>Contended Resources:</strong> ${res.involved_resources?.join(', ') || 'None'}</div>
                <div style="margin-top: 6px; color: var(--status-warning-text); font-weight: 600;">
                    Optimal Preemption Victim: ${res.recommended_victim || 'None'}
                    <br><span style="font-size: 0.76rem; font-weight: 400; color: var(--text-secondary);">${res.victim_reason || ''}</span>
                </div>
            `;
        } else {
            box.style.borderColor = 'var(--status-success)';
            box.style.background = 'var(--status-success-bg)';
            title.innerHTML = `<span style="color: var(--status-success-text); font-weight: 700;">SAFE — No Circular Wait Condition Detected</span>`;
            detail.textContent = 'All requested resources can be granted or scheduled without cyclic blocking.';
        }
    }

    // ── Banker's Algorithm Visualizer ────────────────────────────────────────

    async function loadBankerScenario(scenarioId, isManual = false) {
        if (bankerCache[scenarioId]) {
            bankerState = bankerCache[scenarioId].scenario;
            renderBankerTable(bankerCache[scenarioId].scenario, bankerCache[scenarioId].result);
            if (isManual) {
                App.showToast(`Loaded Banker's ${scenarioId.toUpperCase()} scenario`, 'info');
            }
        }

        try {
            const res = await fetch(`/api/deadlock/banker/scenarios/${scenarioId}`);
            if (res.ok) {
                const data = await res.json();
                bankerCache[scenarioId] = data;
                bankerState = data.scenario;
                renderBankerTable(data.scenario, data.result);
                if (isManual && !bankerCache[scenarioId + '_notified']) {
                    App.showToast(`Loaded Banker's ${scenarioId.toUpperCase()} scenario`, 'info');
                }
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
            App.showToast('Failed to run Banker safety check: ' + e.message, 'error');
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
                    <td style="font-weight: 700; color: var(--accent-hover);">${procs[i]}</td>
                    <td>[ ${allocStr} ]</td>
                    <td>[ ${maxStr} ]</td>
                    <td style="color: var(--status-info-text); font-weight: 600;">[ ${needStr} ]</td>
                </tr>
            `;
        }
        tbody.innerHTML = html;

        if (result && resultBox) {
            resultBox.style.display = 'block';
            if (result.is_safe) {
                resultBox.className = 'banker-result safe';
                resultBox.innerHTML = `
                    <div style="font-size: 0.9rem; margin-bottom: 4px;">System State is <strong>SAFE</strong></div>
                    <div style="font-size: 0.8rem; font-family: var(--font-mono);">
                        Safe Execution Sequence: <strong>${result.safe_sequence ? result.safe_sequence.join(' &rarr; ') : 'None'}</strong>
                    </div>
                `;
            } else {
                resultBox.className = 'banker-result unsafe';
                resultBox.innerHTML = `
                    <div style="font-size: 0.9rem; margin-bottom: 4px;">System State is <strong>UNSAFE (Deadlock Risk)</strong></div>
                    <div style="font-size: 0.8rem;">
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
        toggleViewMode,
        loadBankerScenario,
        runBankerSafetyCheck,
        onTabActivated,
        onTabDeactivated
    };
})();
