/**
 * simulator.js — OS Algorithm Interactive Simulator
 *
 * Demonstrates:
 *   - Multi-Level Feedback Queue (MLFQ) CPU Scheduler simulation
 *   - Interactive execution Gantt timeline and turnaround/waiting time metrics
 *   - Virtual Memory Page Replacement comparison (FIFO vs LRU vs Clock)
 *   - Step-by-step frame state and hit/fault allocation matrix
 */

const Simulator = (() => {
    let lastPageData = null;
    let selectedAlgo = 'FIFO';

    // ── Initialization ──────────────────────────────────────────────────────

    function init() {
        const btnRunMlfq = document.getElementById('btnRunMlfqDemo');
        const btnRunPage = document.getElementById('btnRunPageSim');

        if (btnRunMlfq) {
            btnRunMlfq.addEventListener('click', () => {
                setButtonLoading(btnRunMlfq, true, 'Run MLFQ Simulation');
                runMlfqDemo().finally(() => setButtonLoading(btnRunMlfq, false, 'Run MLFQ Simulation'));
            });
        }

        if (btnRunPage) {
            btnRunPage.addEventListener('click', () => {
                setButtonLoading(btnRunPage, true, 'Compare All Algorithms');
                runPageReplacementSim().finally(() => setButtonLoading(btnRunPage, false, 'Compare All Algorithms'));
            });
        }

        // Algo trace tab buttons for page replacement matrix
        const algoTabs = document.querySelectorAll('#pageAlgoTabs .algo-tab-btn');
        algoTabs.forEach(tab => {
            tab.addEventListener('click', () => {
                algoTabs.forEach(t => t.classList.remove('active'));
                tab.classList.add('active');
                selectedAlgo = tab.getAttribute('data-algo') || 'FIFO';
                if (lastPageData) {
                    renderPageTraceTable(lastPageData, selectedAlgo);
                }
            });
        });

        // Auto-run on page load (non-blocking)
        runMlfqDemo();
        runPageReplacementSim();
    }

    function setButtonLoading(btn, loading, originalText) {
        if (!btn) return;
        if (loading) {
            btn.disabled = true;
            btn.innerHTML = '<span style="opacity:0.75">Running...</span>';
        } else {
            btn.disabled = false;
            btn.innerHTML = originalText;
        }
    }

    // ── Sub-Tab Switcher ────────────────────────────────────────────────────

    function switchSubTab(tab) {
        const cardMlfq = document.getElementById('cardMlfq');
        const cardPage = document.getElementById('cardPageReplacement');
        const btnMlfq  = document.getElementById('btnSubTabMlfq');
        const btnPage  = document.getElementById('btnSubTabPage');
        const btnBoth  = document.getElementById('btnSubTabBoth');

        // Update button active styles
        [btnMlfq, btnPage, btnBoth].forEach(b => {
            if (b) b.className = 'btn btn-sm';
        });

        if (tab === 'mlfq') {
            if (btnMlfq) btnMlfq.className = 'btn btn-sm btn-primary';
            if (cardMlfq) cardMlfq.style.display = 'block';
            if (cardPage) cardPage.style.display = 'none';
            if (cardMlfq) cardMlfq.scrollIntoView({ behavior: 'smooth', block: 'start' });
        } else if (tab === 'page') {
            if (btnPage) btnPage.className = 'btn btn-sm btn-primary';
            if (cardMlfq) cardMlfq.style.display = 'none';
            if (cardPage) cardPage.style.display = 'block';
            if (cardPage) cardPage.scrollIntoView({ behavior: 'smooth', block: 'start' });
        } else {
            if (btnBoth) btnBoth.className = 'btn btn-sm btn-primary';
            if (cardMlfq) cardMlfq.style.display = 'block';
            if (cardPage) cardPage.style.display = 'block';
        }
    }

    // ── MLFQ Scheduler Simulation ───────────────────────────────────────────

    async function runMlfqDemo() {
        const ganttContainer = document.getElementById('mlfqGanttContainer');
        if (ganttContainer) {
            ganttContainer.innerHTML = '<div style="color: var(--text-muted); font-size:0.78rem; padding: var(--space-sm);">Running MLFQ simulation...</div>';
        }

        try {
            const res = await fetch('/api/simulator/mlfq/demo');
            if (!res.ok) {
                const errText = await res.text().catch(() => `HTTP ${res.status}`);
                showSimError(ganttContainer, `Server error ${res.status}: ${errText}`);
                if (typeof App !== 'undefined') App.showToast(`MLFQ error: HTTP ${res.status}`, 'error');
                return;
            }

            const data = await res.json();
            renderMlfqResults(data);
            if (typeof App !== 'undefined') App.showToast('MLFQ simulation completed', 'success');

        } catch (e) {
            console.error('[Simulator] MLFQ fetch failed:', e);
            showSimError(ganttContainer, 'Cannot reach server. Is python run_server.py running?');
            if (typeof App !== 'undefined') App.showToast('Cannot reach server — restart run_server.py', 'error');
        }
    }

    function showSimError(container, message) {
        if (!container) return;
        container.innerHTML = `
            <div style="color: var(--status-danger-text); font-size:0.82rem; padding: var(--space-md); background: var(--status-danger-bg); border: 1px solid var(--status-danger); border-radius: 6px; font-family: var(--font-mono);">
                ${escapeHtml(message)}
            </div>`;
    }

    function renderMlfqResults(data) {
        if (!data) return;

        const processes = data.processes || data.process_stats || [];
        const events    = data.events || [];
        const gantt     = data.gantt_chart || [];
        const metrics   = data.metrics || data.summary || {};

        // 1. Render Queue Process Chips
        const procsQ0 = document.getElementById('procsQ0');
        const procsQ1 = document.getElementById('procsQ1');
        const procsQ2 = document.getElementById('procsQ2');

        if (procsQ0) procsQ0.innerHTML = renderQueueChips(processes, 0);
        if (procsQ1) procsQ1.innerHTML = renderQueueChips(processes, 1);
        if (procsQ2) procsQ2.innerHTML = renderQueueChips(processes, 2);

        // 2. Render Gantt Timeline
        const ganttContainer = document.getElementById('mlfqGanttContainer');
        if (ganttContainer) {
            if (gantt.length > 0) {
                renderGanttChart(ganttContainer, gantt, processes);
            } else {
                ganttContainer.innerHTML = '<div style="color:var(--text-muted);font-size:0.75rem;padding:var(--space-sm)">No timeline events recorded.</div>';
            }
        }

        // 3. Render Metrics Table
        const tbody = document.getElementById('mlfqMetricsBody');
        if (tbody && processes.length > 0) {
            let html = '';
            for (const p of processes) {
                const tat = p.turnaround_time !== undefined
                    ? p.turnaround_time
                    : (p.completion_time - (p.arrival_time || 0));
                const wt = p.wait_time !== undefined ? p.wait_time
                         : p.waiting_time !== undefined ? p.waiting_time
                         : (tat - (p.total_burst || 0));

                html += `
                    <tr>
                        <td style="font-weight:700;color:var(--accent-hover)">${escapeHtml(p.id)}</td>
                        <td style="color:var(--text-primary);font-weight:600">${escapeHtml(p.name)}</td>
                        <td>${p.total_burst} ms</td>
                        <td>${p.completion_time !== undefined ? p.completion_time : '-'} ms</td>
                        <td style="color:var(--status-info-text);font-weight:600">${tat} ms</td>
                        <td style="color:var(--status-warning-text);font-weight:600">${wt} ms</td>
                    </tr>`;
            }

            const avgTat = metrics.avg_turnaround_time || 0;
            const avgWt  = metrics.avg_wait_time || metrics.avg_waiting_time || 0;

            html += `
                <tr style="background:var(--bg-surface-alt);font-weight:700;border-top:1px solid var(--border-default)">
                    <td colspan="4" style="text-align:right;color:var(--text-secondary)">Averages:</td>
                    <td style="color:var(--status-info-text)">Avg TAT: ${Number(avgTat).toFixed(1)} ms</td>
                    <td style="color:var(--status-warning-text)">Avg WT: ${Number(avgWt).toFixed(1)} ms</td>
                </tr>`;

            tbody.innerHTML = html;
        }
    }

    function renderQueueChips(processes, queueIdx) {
        const colors = {
            'P1': '#21777B',
            'P2': '#2C485C',
            'P3': '#215940',
            'P4': '#6E490D',
            'P5': '#7E2B2B'
        };

        // Show processes whose final resting queue matches this level
        const inQueue = processes.filter(p => {
            const q = p.final_queue !== undefined ? p.final_queue : p.queue;
            return q === queueIdx;
        });

        if (inQueue.length === 0) {
            return '<span style="color:var(--text-muted);font-size:0.78rem;font-style:italic">No processes at this level</span>';
        }

        return inQueue.map(p => {
            const color = colors[p.id] || '#21777B';
            const done  = p.completion_time > 0 ? 'Completed' : 'Active';
            const doneColor = p.completion_time > 0 ? 'var(--status-success-text)' : 'var(--status-warning-text)';
            return `
                <span class="mlfq-proc-chip" style="border:1px solid ${color}66;color:${color};background:${color}14;display:inline-flex;align-items:center;gap:6px">
                    <strong>${escapeHtml(p.id)}</strong>
                    <span>${escapeHtml(p.name)}</span>
                    <span style="font-size:0.7rem;background:${color}22;padding:1px 5px;border-radius:4px">${p.total_burst}ms</span>
                    <span style="font-size:0.7rem;color:${doneColor};font-weight:600">${done}</span>
                </span>`;
        }).join('\n');
    }

    function renderGanttChart(container, gantt, processes) {
        const procColorMap = {
            'P1': '#21777B', 'P2': '#2C485C', 'P3': '#215940',
            'P4': '#6E490D', 'P5': '#7E2B2B'
        };
        const palette = ['#21777B', '#2C485C', '#215940', '#6E490D', '#7E2B2B', '#5B2C8C'];
        processes.forEach((p, idx) => {
            if (!procColorMap[p.id]) procColorMap[p.id] = palette[idx % palette.length];
        });

        let html = '<div style="display:flex;gap:3px;align-items:flex-end;padding:12px 6px;overflow-x:auto;min-height:80px">';

        for (const slice of gantt) {
            const procId   = slice.process_id || slice.proc_id || 'IDLE';
            const duration = slice.duration || ((slice.end_time || 0) - (slice.start_time || 0)) || 1;
            const width    = Math.max(40, duration * 14);
            const color    = procColorMap[procId] || '#475569';
            const q        = slice.queue !== undefined ? slice.queue : 0;

            html += `
                <div style="flex:0 0 ${width}px;text-align:center"
                     title="${escapeHtml(procId)} (Q${q}): ${slice.start_time}–${slice.end_time}ms (${duration}ms)">
                    <div style="background:${color}28;border:1px solid ${color};color:${color};padding:6px 2px;
                                border-radius:6px;font-weight:700;font-family:var(--font-mono);font-size:0.75rem;
                                transition:transform 0.15s ease;cursor:default"
                         onmouseenter="this.style.transform='scale(1.08)'"
                         onmouseleave="this.style.transform='scale(1)'">
                        ${escapeHtml(procId)}
                        <div style="font-size:0.65rem;opacity:0.85">Q${q}</div>
                    </div>
                    <div style="font-size:0.68rem;color:var(--text-muted);font-family:var(--font-mono);margin-top:4px">
                        ${slice.start_time}–${slice.end_time}
                    </div>
                </div>`;
        }

        html += '</div>';
        container.innerHTML = html;
    }

    // ── Page Replacement Simulation ─────────────────────────────────────────

    async function runPageReplacementSim() {
        const input       = document.getElementById('pageRefStringInput');
        const framesInput = document.getElementById('pageFramesInput');
        const gridEl      = document.getElementById('pageComparisonGrid');
        const matrixEl    = document.getElementById('pageMatrixContainer');

        const rawStr   = input ? input.value : '7,0,1,2,0,3,0,4,2,3,0,3,2,1,2,0,1,7,0,1';
        const numFrames = framesInput ? (parseInt(framesInput.value) || 3) : 3;
        const refString = rawStr.split(',').map(s => parseInt(s.trim())).filter(n => !isNaN(n));

        if (refString.length === 0) {
            if (typeof App !== 'undefined') App.showToast('Enter a valid comma-separated reference string', 'warning');
            return;
        }

        if (matrixEl) {
            matrixEl.innerHTML = '<div style="color:var(--text-muted);font-size:0.78rem;padding:var(--space-md);text-align:center">Running comparison...</div>';
        }

        try {
            const res = await fetch('/api/simulator/page-replacement', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ reference_string: refString, num_frames: numFrames })
            });

            if (!res.ok) {
                const errText = await res.text().catch(() => `HTTP ${res.status}`);
                showSimError(matrixEl, `Server error ${res.status}: ${errText}`);
                if (typeof App !== 'undefined') App.showToast(`Page replacement error: HTTP ${res.status}`, 'error');
                return;
            }

            const data = await res.json();
            lastPageData = data;
            renderPageReplacementResults(data);
            renderPageTraceTable(data, selectedAlgo);
            if (typeof App !== 'undefined') App.showToast('Page replacement comparison complete', 'success');

            // Make the card visible and scroll to it
            const card = document.getElementById('cardPageReplacement');
            if (card) {
                card.style.display = 'block';
                setTimeout(() => card.scrollIntoView({ behavior: 'smooth', block: 'start' }), 100);
            }

        } catch (e) {
            console.error('[Simulator] Page replacement fetch failed:', e);
            showSimError(matrixEl, 'Cannot reach server. Is python run_server.py running?');
            if (typeof App !== 'undefined') App.showToast('Cannot reach server — restart run_server.py', 'error');
        }
    }

    function renderPageReplacementResults(data) {
        if (!data || !data.algorithms) {
            console.warn('[Simulator] Page replacement data missing algorithms field', data);
            return;
        }

        const algos  = data.algorithms;
        const fifo   = algos.FIFO;
        const lru    = algos.LRU;
        const clock  = algos.Clock || algos.CLOCK || algos.SECOND_CHANCE;

        const bestAlgo  = (data.best_algorithm || '').toUpperCase();
        const totalRefs = data.total_references || (data.reference_string ? data.reference_string.length : 1);

        function getStats(algoObj) {
            if (!algoObj) return { faults: '--', rate: '--' };
            const faults = algoObj.page_faults !== undefined ? algoObj.page_faults
                         : (algoObj.summary && algoObj.summary.page_faults !== undefined) ? algoObj.summary.page_faults
                         : (algoObj.fault_count || 0);
            const rate   = algoObj.fault_rate !== undefined ? algoObj.fault_rate
                         : (algoObj.summary && algoObj.summary.fault_rate !== undefined) ? algoObj.summary.fault_rate
                         : ((faults / totalRefs) * 100);
            return { faults, rate: Number(rate).toFixed(1) };
        }

        // Update FIFO card
        if (fifo) {
            const st = getStats(fifo);
            const el1 = document.getElementById('statFifoFaults');
            const el2 = document.getElementById('statFifoRate');
            const card = document.getElementById('cardFifo');
            if (el1) el1.textContent = st.faults;
            if (el2) el2.textContent = `Fault Rate: ${st.rate}%`;
            if (card) {
                card.classList.toggle('best', bestAlgo === 'FIFO');
                if (bestAlgo === 'FIFO') card.classList.add('best');
            }
        }

        // Update LRU card
        if (lru) {
            const st = getStats(lru);
            const el1 = document.getElementById('statLruFaults');
            const el2 = document.getElementById('statLruRate');
            const card = document.getElementById('cardLru');
            if (el1) el1.textContent = st.faults;
            if (el2) el2.textContent = `Fault Rate: ${st.rate}%`;
            if (card) {
                card.classList.remove('best');
                if (bestAlgo === 'LRU') card.classList.add('best');
            }
        }

        // Update Clock card
        if (clock) {
            const st = getStats(clock);
            const el1 = document.getElementById('statClockFaults');
            const el2 = document.getElementById('statClockRate');
            const card = document.getElementById('cardClock');
            if (el1) el1.textContent = st.faults;
            if (el2) el2.textContent = `Fault Rate: ${st.rate}%`;
            if (card) {
                card.classList.remove('best');
                if (bestAlgo === 'CLOCK') card.classList.add('best');
            }
        }
    }

    function renderPageTraceTable(data, algoName) {
        const container = document.getElementById('pageMatrixContainer');
        if (!container || !data || !data.algorithms) return;

        // Find the algorithm data — try exact name, then case-insensitive
        let algoData = data.algorithms[algoName];
        if (!algoData) {
            for (const k of Object.keys(data.algorithms)) {
                if (k.toLowerCase() === algoName.toLowerCase()) {
                    algoData = data.algorithms[k];
                    break;
                }
            }
        }

        if (!algoData || !algoData.events || algoData.events.length === 0) {
            container.innerHTML = `<div style="color:var(--text-muted);font-size:0.75rem;padding:var(--space-md);text-align:center">
                No event data for ${escapeHtml(algoName)}. Click "Compare All Algorithms" first.</div>`;
            return;
        }

        const events    = algoData.events;
        const numFrames = data.num_frames || (events[0] && events[0].frames_state ? events[0].frames_state.length : 3);

        // Build table HTML
        let html = '<table class="page-matrix-table"><thead><tr>';
        html += '<th class="step-header" style="text-align:left;position:sticky;left:0;z-index:3;background:var(--bg-card)">Frame / Step</th>';

        for (let i = 0; i < events.length; i++) {
            html += `<th class="step-header">#${i + 1}</th>`;
        }
        html += '</tr>';

        // "Page Requested" row
        html += '<tr><td class="row-label">Page Ref</td>';
        for (const ev of events) {
            html += `<td class="cell-page">${ev.page_requested}</td>`;
        }
        html += '</tr></thead><tbody>';

        // Frame rows
        for (let f = 0; f < numFrames; f++) {
            html += `<tr><td class="row-label">Frame ${f}</td>`;
            for (const ev of events) {
                const fs  = ev.frames_state || [];
                const val = (fs[f] !== undefined && fs[f] !== null) ? fs[f] : '-';
                const hi  = (!ev.is_hit && val === ev.page_requested) ? 'cell-frame highlight' : 'cell-frame';
                html += `<td class="${hi}">${val}</td>`;
            }
            html += '</tr>';
        }

        // Result row (HIT / FAULT)
        html += '<tr><td class="row-label">Result</td>';
        for (const ev of events) {
            if (ev.is_hit) {
                html += '<td class="cell-hit">HIT ✓</td>';
            } else {
                html += '<td class="cell-fault">FAULT ✗</td>';
            }
        }
        html += '</tr>';

        // Evicted row
        html += '<tr><td class="row-label">Evicted</td>';
        for (const ev of events) {
            const ev_page = (ev.page_evicted !== null && ev.page_evicted !== undefined);
            html += `<td style="font-size:0.74rem;${ev_page ? 'color:var(--status-danger-text);font-weight:600' : 'color:var(--text-muted)'}">
                ${ev_page ? `Pg ${ev.page_evicted}` : '–'}</td>`;
        }
        html += '</tr>';

        html += '</tbody></table>';

        // Summary strip
        const total     = events.length;
        const faults    = events.filter(e => !e.is_hit).length;
        const hits      = total - faults;
        const hitRate   = total > 0 ? ((hits / total) * 100).toFixed(1) : '0.0';
        const faultRate = total > 0 ? ((faults / total) * 100).toFixed(1) : '0.0';

        html += `
            <div style="display:flex;gap:var(--space-lg);justify-content:flex-end;align-items:center;
                        margin-top:var(--space-sm);padding:var(--space-xs) var(--space-sm);
                        font-family:var(--font-mono);font-size:0.78rem">
                <span style="color:var(--text-secondary)">Total Refs: <strong>${total}</strong></span>
                <span style="color:var(--status-success-text)">Hits: <strong>${hits}</strong> (${hitRate}%)</span>
                <span style="color:var(--status-danger-text)">Faults: <strong>${faults}</strong> (${faultRate}%)</span>
            </div>`;

        container.innerHTML = html;
    }

    // ── Utilities ────────────────────────────────────────────────────────────

    function escapeHtml(str) {
        if (str === null || str === undefined) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    // ── Public API ───────────────────────────────────────────────────────────

    return {
        init,
        switchSubTab,
        runMlfqDemo,
        runPageReplacementSim
    };
})();
