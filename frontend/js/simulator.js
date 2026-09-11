/**
 * simulator.js — OS Algorithm Interactive Simulator
 * 
 * Demonstrates:
 *   - Multi-Level Feedback Queue (MLFQ) CPU Scheduler simulation
 *   - Interactive execution Gantt timeline and turnaround/waiting time metrics
 *   - Virtual Memory Page Replacement comparison (FIFO vs LRU vs Clock)
 *   - Step-by-step frame state and hit/fault rates
 */

const Simulator = (() => {
    function init() {
        const btnRunMlfq = document.getElementById('btnRunMlfqDemo');
        const btnRunPage = document.getElementById('btnRunPageSim');

        if (btnRunMlfq) {
            btnRunMlfq.addEventListener('click', runMlfqDemo);
        }

        if (btnRunPage) {
            btnRunPage.addEventListener('click', runPageReplacementSim);
        }

        // Run initial demonstrations on page load
        runMlfqDemo();
        runPageReplacementSim();
    }

    // ── MLFQ Scheduler Simulation ───────────────────────────────────────────

    async function runMlfqDemo() {
        try {
            const res = await fetch('/api/simulator/mlfq/demo');
            if (res.ok) {
                const data = await res.json();
                renderMlfqResults(data);
                App.showToast('MLFQ simulation completed!', 'success');
            }
        } catch (e) {
            console.error('Failed to run MLFQ demo:', e);
        }
    }

    function renderMlfqResults(data) {
        if (!data) return;

        // 1. Render Queue Procs
        const procsQ0 = document.getElementById('procsQ0');
        const procsQ1 = document.getElementById('procsQ1');
        const procsQ2 = document.getElementById('procsQ2');

        const finalState = data.final_state || {};
        const processes = data.processes || [];

        // Distribute processes to queues based on final queue or events
        if (procsQ0) procsQ0.innerHTML = renderQueueChips(processes, 0, data.events);
        if (procsQ1) procsQ1.innerHTML = renderQueueChips(processes, 1, data.events);
        if (procsQ2) procsQ2.innerHTML = renderQueueChips(processes, 2, data.events);

        // 2. Render Gantt Timeline
        const ganttContainer = document.getElementById('mlfqGanttContainer');
        if (ganttContainer && data.gantt_chart) {
            renderGanttChart(ganttContainer, data.gantt_chart, processes);
        }

        // 3. Render Metrics Table
        const tbody = document.getElementById('mlfqMetricsBody');
        if (tbody && processes) {
            let html = '';
            for (const p of processes) {
                const tat = p.turnaround_time !== undefined ? p.turnaround_time : (p.completion_time - (p.arrival_time || 0));
                const wt = p.waiting_time !== undefined ? p.waiting_time : (tat - p.total_burst);

                html += `
                    <tr>
                        <td style="font-weight: 700; color: var(--accent-indigo);">${p.id}</td>
                        <td style="color: var(--text-primary); font-weight: 600;">${escapeHtml(p.name)}</td>
                        <td>${p.total_burst} ms</td>
                        <td>${p.completion_time || '-'} ms</td>
                        <td style="color: var(--accent-cyan); font-weight: 600;">${tat} ms</td>
                        <td style="color: var(--accent-amber); font-weight: 600;">${wt} ms</td>
                    </tr>
                `;
            }

            // Summary row: Averages
            if (data.metrics) {
                const m = data.metrics;
                html += `
                    <tr style="background: rgba(255, 255, 255, 0.04); font-weight: 700;">
                        <td colspan="4" style="text-align: right; color: var(--text-secondary);">Averages:</td>
                        <td style="color: var(--accent-cyan);">Avg TAT: ${(m.avg_turnaround_time || 0).toFixed(1)} ms</td>
                        <td style="color: var(--accent-amber);">Avg WT: ${(m.avg_waiting_time || 0).toFixed(1)} ms</td>
                    </tr>
                `;
            }

            tbody.innerHTML = html;
        }
    }

    function renderQueueChips(processes, queueIdx, events) {
        // Collect processes that ever entered or completed in this queue
        const colors = ['#6366f1', '#06b6d4', '#f59e0b', '#10b981', '#f43f5e'];
        let chips = '';

        for (let i = 0; i < processes.length; i++) {
            const p = processes[i];
            const color = colors[i % colors.length];
            chips += `
                <span class="mlfq-proc-chip" style="border: 1px solid ${color}; color: ${color}; background: ${color}22;">
                    ${p.id} (${p.name}) &bull; ${p.total_burst}ms
                </span>
            `;
        }

        return chips || '<span style="color: var(--text-muted); font-size: 0.75rem;">Idle</span>';
    }

    function renderGanttChart(container, gantt, processes) {
        if (!gantt || gantt.length === 0) {
            container.innerHTML = '<div style="color: var(--text-muted); font-size: 0.75rem;">No timeline events recorded.</div>';
            return;
        }

        const procColorMap = {};
        const palette = ['#6366f1', '#06b6d4', '#10b981', '#f59e0b', '#f43f5e', '#8b5cf6'];
        processes.forEach((p, idx) => {
            procColorMap[p.id] = palette[idx % palette.length];
        });

        let timelineHtml = '<div style="display: flex; gap: 2px; align-items: flex-end; padding: 10px 0; overflow-x: auto;">';

        for (const slice of gantt) {
            const procId = slice.process_id || slice.proc_id || 'IDLE';
            const duration = slice.duration || (slice.end_time - slice.start_time) || 1;
            const width = Math.max(28, duration * 10);
            const color = procColorMap[procId] || '#475569';

            timelineHtml += `
                <div style="flex: 0 0 ${width}px; text-align: center;">
                    <div style="background: ${color}33; border: 1px solid ${color}; color: ${color}; padding: 6px 2px; border-radius: 4px; font-weight: 700; font-family: var(--font-mono); font-size: 0.75rem;">
                        ${procId}
                        <div style="font-size: 0.65rem; opacity: 0.8;">Q${slice.queue !== undefined ? slice.queue : 0}</div>
                    </div>
                    <div style="font-size: 0.68rem; color: var(--text-muted); font-family: var(--font-mono); margin-top: 4px;">
                        ${slice.start_time}-${slice.end_time}ms
                    </div>
                </div>
            `;
        }

        timelineHtml += '</div>';
        container.innerHTML = timelineHtml;
    }

    // ── Page Replacement Simulation ─────────────────────────────────────────

    async function runPageReplacementSim() {
        const input = document.getElementById('pageRefStringInput');
        const rawStr = input ? input.value : "7, 0, 1, 2, 0, 3, 0, 4, 2, 3, 0, 3, 2, 1, 2, 0, 1, 7, 0, 1";

        const refString = rawStr
            .split(',')
            .map(s => parseInt(s.trim()))
            .filter(n => !isNaN(n));

        if (refString.length === 0) {
            App.showToast('Please enter a valid comma-separated reference string', 'warning');
            return;
        }

        try {
            const res = await fetch('/api/simulator/page-replacement', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    reference_string: refString,
                    num_frames: 3
                })
            });

            if (res.ok) {
                const data = await res.json();
                renderPageReplacementResults(data);
                App.showToast('Page Replacement simulation completed!', 'success');
            }
        } catch (e) {
            console.error('Failed to run page replacement:', e);
        }
    }

    function renderPageReplacementResults(data) {
        if (!data || !data.algorithms) return;

        const algos = data.algorithms;
        const fifo = algos.FIFO;
        const lru = algos.LRU;
        const clock = algos.CLOCK || algos.Clock || algos.SECOND_CHANCE;

        // Best algorithm identifier
        let bestAlgo = data.best_algorithm || 'LRU';

        // Update FIFO
        if (fifo) {
            const elFaults = document.getElementById('statFifoFaults');
            const elRate = document.getElementById('statFifoRate');
            const card = document.getElementById('cardFifo');

            if (elFaults) elFaults.textContent = fifo.fault_count || fifo.page_faults;
            if (elRate) elRate.textContent = `Fault Rate: ${(fifo.fault_rate !== undefined ? fifo.fault_rate * 100 : ((fifo.fault_count / data.total_references) * 100)).toFixed(1)}%`;
            if (card) card.classList.toggle('best', bestAlgo === 'FIFO');
        }

        // Update LRU
        if (lru) {
            const elFaults = document.getElementById('statLruFaults');
            const elRate = document.getElementById('statLruRate');
            const card = document.getElementById('cardLru');

            if (elFaults) elFaults.textContent = lru.fault_count || lru.page_faults;
            if (elRate) elRate.textContent = `Fault Rate: ${(lru.fault_rate !== undefined ? lru.fault_rate * 100 : ((lru.fault_count / data.total_references) * 100)).toFixed(1)}%`;
            if (card) card.classList.toggle('best', bestAlgo === 'LRU');
        }

        // Update Clock
        if (clock) {
            const elFaults = document.getElementById('statClockFaults');
            const elRate = document.getElementById('statClockRate');
            const card = document.getElementById('cardClock');

            if (elFaults) elFaults.textContent = clock.fault_count || clock.page_faults;
            if (elRate) elRate.textContent = `Fault Rate: ${(clock.fault_rate !== undefined ? clock.fault_rate * 100 : ((clock.fault_count / data.total_references) * 100)).toFixed(1)}%`;
            if (card) card.classList.toggle('best', bestAlgo === 'CLOCK');
        }
    }

    function escapeHtml(str) {
        if (!str) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    return {
        init,
        runMlfqDemo,
        runPageReplacementSim
    };
})();
