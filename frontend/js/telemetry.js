/**
 * telemetry.js — Real-Time Telemetry & Process Management Module
 * 
 * Handles:
 *   - CPU & Memory Canvas Timeline charts with smooth animated gradients
 *   - Live Process Matrix filtering, sorting, and inline control actions
 *   - Intelligent Heuristic Decision Feed streaming
 *   - System status cards (CPU, RAM, Threads, Policy metrics)
 */

const Telemetry = (() => {
    // Canvas Charts State
    let cpuHistory = [];
    let memHistory = [];
    const MAX_POINTS = 60;

    let isPaused = false;
    let cachedProcesses = [];
    let cachedDecisions = [];

    // Elements
    let cpuCanvas, cpuCtx;
    let memCanvas, memCtx;

    function init() {
        cpuCanvas = document.getElementById('cpuChartCanvas');
        memCanvas = document.getElementById('memChartCanvas');

        if (cpuCanvas) {
            cpuCtx = cpuCanvas.getContext('2d');
            resizeCanvas(cpuCanvas);
        }
        if (memCanvas) {
            memCtx = memCanvas.getContext('2d');
            resizeCanvas(memCanvas);
        }

        window.addEventListener('resize', () => {
            if (cpuCanvas) resizeCanvas(cpuCanvas);
            if (memCanvas) resizeCanvas(memCanvas);
            drawChart(cpuCanvas, cpuCtx, cpuHistory, '#6366f1', 'CPU %');
            drawChart(memCanvas, memCtx, memHistory, '#06b6d4', 'RAM %');
        });

        // Search & Filter event listeners
        const searchInput = document.getElementById('searchProcessInput');
        const catSelect = document.getElementById('filterCategorySelect');
        const sortSelect = document.getElementById('sortProcessSelect');
        const btnPause = document.getElementById('btnPauseTelemetry');
        const btnRefresh = document.getElementById('btnRefreshProcesses');

        if (searchInput) searchInput.addEventListener('input', renderProcessTable);
        if (catSelect) catSelect.addEventListener('change', renderProcessTable);
        if (sortSelect) sortSelect.addEventListener('change', renderProcessTable);

        if (btnPause) {
            btnPause.addEventListener('click', () => {
                isPaused = !isPaused;
                btnPause.textContent = isPaused ? '▶️ Resume' : '⏸️ Pause';
                btnPause.classList.toggle('btn-primary', isPaused);
                App.showToast(isPaused ? 'Telemetry feed paused' : 'Telemetry feed resumed', 'info');
            });
        }

        if (btnRefresh) {
            btnRefresh.addEventListener('click', async () => {
                try {
                    const res = await fetch('/api/processes');
                    if (res.ok) {
                        cachedProcesses = await res.json();
                        renderProcessTable();
                        App.showToast('Process table refreshed', 'success');
                    }
                } catch (e) {
                    App.showToast('Failed to fetch processes: ' + e.message, 'error');
                }
            });
        }
    }

    function resizeCanvas(canvas) {
        if (!canvas || !canvas.parentElement) return;
        const rect = canvas.parentElement.getBoundingClientRect();
        const w = Math.floor(rect.width) || 300;
        const h = Math.floor(rect.height) || 180;
        if (canvas.width !== w || canvas.height !== h) {
            canvas.width = w;
            canvas.height = h;
        }
    }

    function isTabVisible() {
        const panel = document.getElementById('panelTelemetry');
        return panel && panel.classList.contains('active');
    }

    function onTabActivated() {
        if (cpuCanvas) resizeCanvas(cpuCanvas);
        if (memCanvas) resizeCanvas(memCanvas);
        drawChart(cpuCanvas, cpuCtx, cpuHistory, '#6366f1', 'CPU %');
        drawChart(memCanvas, memCtx, memHistory, '#06b6d4', 'RAM %');
        renderProcessTable();
        renderDecisionFeed();
    }

    function update(telemetryData) {
        if (isPaused) return;

        const { system, processes, history, decisions, process_categories, policy_stats } = telemetryData;

        // Seed rolling history immediately from backend if available
        if (history) {
            if (cpuHistory.length === 0 && Array.isArray(history.cpu) && history.cpu.length > 0) {
                cpuHistory = [...history.cpu];
            }
            if (memHistory.length === 0 && Array.isArray(history.memory) && history.memory.length > 0) {
                memHistory = [...history.memory];
            }
        }

        // Cache latest data
        if (processes) cachedProcesses = processes;
        if (decisions) cachedDecisions = decisions;

        const visible = isTabVisible();

        // 1. Update Stat Cards
        if (system) {
            const cpuPercent = (system.cpu && system.cpu.percent !== undefined)
                ? system.cpu.percent
                : (system.cpu_percent !== undefined ? system.cpu_percent : 0);
            const physicalCores = (system.cpu && system.cpu.physical_cores) || (system.cpu_count ? Math.max(1, Math.floor(system.cpu_count / 2)) : '-');
            const logicalCores = (system.cpu && system.cpu.logical_cores) || system.cpu_count || '-';

            const elCpu = document.getElementById('statCpuPercent');
            const barCpu = document.getElementById('barCpuPercent');
            const elCores = document.getElementById('statCpuCores');

            if (elCpu) elCpu.textContent = cpuPercent.toFixed(1) + '%';
            if (barCpu) barCpu.style.width = Math.min(100, Math.max(0, cpuPercent)) + '%';
            if (elCores) elCores.textContent = `${physicalCores} Physical | ${logicalCores} Logical Cores`;

            // Append to CPU chart history
            cpuHistory.push(cpuPercent);
            if (cpuHistory.length > MAX_POINTS) cpuHistory.shift();
            if (visible) {
                drawChart(cpuCanvas, cpuCtx, cpuHistory, '#6366f1', 'CPU %');
            }

            const mem = system.memory || {};
            const memPercent = (mem.percent !== undefined)
                ? mem.percent
                : (system.memory_percent !== undefined ? system.memory_percent : 0);
            const memUsedGb = (mem.used_gb !== undefined)
                ? mem.used_gb
                : (system.memory_used_mb ? (system.memory_used_mb / 1024) : 0);
            const memTotalGb = (mem.total_gb !== undefined)
                ? mem.total_gb
                : (system.memory_total_mb ? (system.memory_total_mb / 1024) : 0);

            const elMem = document.getElementById('statMemPercent');
            const barMem = document.getElementById('barMemPercent');
            const elDetail = document.getElementById('statMemDetail');

            if (elMem) elMem.textContent = memPercent.toFixed(1) + '%';
            if (barMem) barMem.style.width = Math.min(100, Math.max(0, memPercent)) + '%';
            if (elDetail) elDetail.textContent = `${memUsedGb.toFixed(1)} GB / ${memTotalGb.toFixed(1)} GB`;

            // Append to Memory chart history
            memHistory.push(memPercent);
            if (memHistory.length > MAX_POINTS) memHistory.shift();
            if (visible) {
                drawChart(memCanvas, memCtx, memHistory, '#06b6d4', 'RAM %');
            }

            const totalProcs = (system.process_counts && system.process_counts.total)
                || system.process_count
                || (processes ? processes.length : 0);
            const totalThreads = (system.process_counts && system.process_counts.threads)
                || system.thread_count
                || 0;

            const elProc = document.getElementById('statProcCount');
            const elThreads = document.getElementById('statThreadCount');

            if (elProc) elProc.textContent = totalProcs;
            if (elThreads) elThreads.textContent = `${totalThreads} Active Threads`;
        }

        if (policy_stats) {
            const elDecisions = document.getElementById('statDecisionsCount');
            const elDetail = document.getElementById('statPolicyDetail');
            if (elDecisions) elDecisions.textContent = policy_stats.total_decisions || decisions?.length || 0;
            if (elDetail) {
                elDetail.textContent = `${policy_stats.mitigations_applied || 0} Throttled | ${policy_stats.starvation_boosts || 0} Boosted`;
            }
        }

        // 2. Only render table & feed if tab is currently visible
        if (visible) {
            if (processes) renderProcessTable(process_categories);
            if (decisions) renderDecisionFeed();
        }
    }

    function renderProcessTable(categories) {
        const tbody = document.getElementById('processTableBody');
        if (!tbody) return;

        const search = (document.getElementById('searchProcessInput')?.value || '').toLowerCase().trim();
        const catFilter = document.getElementById('filterCategorySelect')?.value || 'all';
        const sortBy = document.getElementById('sortProcessSelect')?.value || 'cpu';

        let list = [...cachedProcesses];

        // Filter by search
        if (search) {
            list = list.filter(p => 
                p.name.toLowerCase().includes(search) || 
                String(p.pid).includes(search)
            );
        }

        // Filter by category
        if (catFilter !== 'all') {
            list = list.filter(p => (p.category || 'background').toLowerCase() === catFilter);
        }

        // Sort
        list.sort((a, b) => {
            if (sortBy === 'cpu') return (b.cpu_percent || 0) - (a.cpu_percent || 0);
            if (sortBy === 'memory') return (b.memory_mb || 0) - (a.memory_mb || 0);
            if (sortBy === 'threads') return (b.num_threads || 0) - (a.num_threads || 0);
            if (sortBy === 'pid') return a.pid - b.pid;
            return 0;
        });

        const badge = document.getElementById('procTableCountBadge');
        if (badge) badge.textContent = `${list.length} procs`;

        if (list.length === 0) {
            tbody.innerHTML = `<tr><td colspan="9" style="text-align: center; color: var(--text-muted); padding: var(--space-md);">No processes matching search criteria</td></tr>`;
            return;
        }

        // Display up to 50 top processes for UI performance
        const displayList = list.slice(0, 50);
        let html = '';

        for (const p of displayList) {
            const cpuVal = (p.cpu_percent || 0).toFixed(1);
            let cpuClass = 'low';
            if (p.cpu_percent > 30) cpuClass = 'high';
            else if (p.cpu_percent > 10) cpuClass = 'medium';

            const memVal = (p.memory_mb || 0).toFixed(1);
            let memClass = '';
            if (p.memory_mb > 500) memClass = 'high';
            else if (p.memory_mb > 200) memClass = 'medium';

            const prioClass = (p.priority_class || 'normal').toLowerCase();
            const cat = p.category || 'background';

            const sandboxTag = p.sandbox_name 
                ? `<span class="sandbox-pid-tag">${escapeHtml(p.sandbox_name)}</span>` 
                : `<span style="color: var(--text-muted); font-size: 0.72rem;">Native</span>`;

            html += `
                <tr>
                    <td class="pid-cell">${p.pid}</td>
                    <td style="font-weight: 600; color: var(--text-primary);" title="${escapeHtml(p.exe || p.name)}">
                        ${escapeHtml(p.name)}
                    </td>
                    <td><span class="category-badge ${cat}">${cat}</span></td>
                    <td><span class="priority-badge ${prioClass}">${escapeHtml(p.priority_class || 'NORMAL')}</span></td>
                    <td class="cpu-cell ${cpuClass}">${cpuVal}%</td>
                    <td class="mem-cell ${memClass}">${memVal} MB</td>
                    <td>${p.num_threads || 1}</td>
                    <td>${sandboxTag}</td>
                    <td>
                        <div class="action-cell">
                            <button class="action-btn" title="Trim Working Set (RAM)" onclick="Telemetry.trimProcess(${p.pid})">✂️</button>
                            <button class="action-btn" title="Set to IDLE Priority" onclick="Telemetry.setPriority(${p.pid}, 'IDLE_PRIORITY_CLASS')">⬇️</button>
                            <button class="action-btn danger" title="Terminate Process" onclick="Telemetry.terminateProcess(${p.pid})">❌</button>
                        </div>
                    </td>
                </tr>
            `;
        }

        tbody.innerHTML = html;
    }

    function renderDecisionFeed() {
        const container = document.getElementById('decisionFeedContainer');
        if (!container) return;

        if (!cachedDecisions || cachedDecisions.length === 0) {
            container.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon">🛡️</div>
                    <div class="empty-state-text">No policy actions triggered yet. System running smoothly.</div>
                </div>
            `;
            return;
        }

        let html = '';
        for (const d of cachedDecisions) {
            const severity = d.severity || (d.rule_id?.includes('CRIT') ? 'critical' : d.rule_id?.includes('HOG') ? 'warning' : 'info');
            const timeStr = d.timestamp ? new Date(d.timestamp * 1000).toLocaleTimeString() : '--:--:--';

            html += `
                <div class="decision-entry ${severity}">
                    <div class="decision-header">
                        <span class="decision-rule ${severity}">${escapeHtml(d.rule_id || 'POLICY_RULE')}</span>
                        <span class="decision-time">${timeStr}</span>
                    </div>
                    <div class="decision-action">${escapeHtml(d.action_taken || 'Kernel Action Enforced')}</div>
                    <div class="decision-reason">${escapeHtml(d.reason || '')}</div>
                    ${d.details ? `<div class="decision-details">${escapeHtml(JSON.stringify(d.details))}</div>` : ''}
                </div>
            `;
        }

        container.innerHTML = html;
    }

    function drawChart(canvas, ctx, data, color, label) {
        if (!canvas || !ctx) return;

        resizeCanvas(canvas);

        const w = canvas.width;
        const h = canvas.height;

        ctx.clearRect(0, 0, w, h);

        // Background subtle grid lines
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.08)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        for (let y = 0; y <= h; y += h / 4) {
            ctx.moveTo(0, y);
            ctx.lineTo(w, y);
        }
        ctx.stroke();

        if (!data || data.length === 0) {
            ctx.fillStyle = '#64748b';
            ctx.font = '12px "Inter", sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText('Awaiting telemetry stream...', w / 2, h / 2);
            return;
        }

        // If only 1 point, duplicate it so we have a valid line segment
        const points = data.length === 1 ? [data[0], data[0]] : data;

        const step = w / (MAX_POINTS - 1);
        const startX = w - (points.length - 1) * step;

        // Create gradient fill
        const grad = ctx.createLinearGradient(0, 0, 0, h);
        grad.addColorStop(0, color + '55');
        grad.addColorStop(1, color + '05');

        // Path
        ctx.beginPath();
        ctx.moveTo(startX, h - (points[0] / 100) * h);

        for (let i = 1; i < points.length; i++) {
            const x = startX + i * step;
            const y = h - (points[i] / 100) * h;
            ctx.lineTo(x, y);
        }

        // Stroke line
        ctx.strokeStyle = color;
        ctx.lineWidth = 2.5;
        ctx.lineJoin = 'round';
        ctx.stroke();

        // Fill area under curve
        ctx.lineTo(w, h);
        ctx.lineTo(startX, h);
        ctx.closePath();
        ctx.fillStyle = grad;
        ctx.fill();

        // Current value readout badge
        const lastVal = points[points.length - 1];
        ctx.fillStyle = '#f1f5f9';
        ctx.font = 'bold 11px "JetBrains Mono", monospace';
        ctx.textAlign = 'right';
        ctx.fillText(`${label}: ${lastVal.toFixed(1)}%`, w - 10, 18);
    }

    // Process Control Actions
    async function trimProcess(pid) {
        try {
            const res = await fetch(`/api/processes/${pid}/trim`, { method: 'POST' });
            if (res.ok) {
                App.showToast(`Working set trimmed for PID ${pid}`, 'success');
            } else {
                const err = await res.json();
                App.showToast(`Trim failed: ${err.detail || 'Error'}`, 'error');
            }
        } catch (e) {
            App.showToast(`Error: ${e.message}`, 'error');
        }
    }

    async function setPriority(pid, priorityClass) {
        try {
            const res = await fetch(`/api/processes/${pid}/priority`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ priority: priorityClass })
            });
            if (res.ok) {
                App.showToast(`PID ${pid} priority set to ${priorityClass}`, 'success');
            } else {
                const err = await res.json();
                App.showToast(`Priority change failed: ${err.detail || 'Error'}`, 'error');
            }
        } catch (e) {
            App.showToast(`Error: ${e.message}`, 'error');
        }
    }

    async function terminateProcess(pid) {
        if (!confirm(`Are you sure you want to terminate Process PID ${pid}?`)) return;
        try {
            const res = await fetch(`/api/processes/${pid}/terminate`, { method: 'POST' });
            if (res.ok) {
                App.showToast(`Process ${pid} terminated`, 'success');
            } else {
                const err = await res.json();
                App.showToast(`Terminate failed: ${err.detail || 'Error'}`, 'error');
            }
        } catch (e) {
            App.showToast(`Error: ${e.message}`, 'error');
        }
    }

    function escapeHtml(str) {
        if (str === null || str === undefined) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    return {
        init,
        update,
        onTabActivated,
        trimProcess,
        setPriority,
        terminateProcess
    };
})();
