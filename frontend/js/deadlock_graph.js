/**
 * deadlock_graph.js — Live Windows-Native RAG/WFG & Banker's Algorithm Visualizer
 *
 * Strictly live-driven (NO dummy WFG graph, NO static Banker's matrices in live mode,
 * NO manual Sync Graphs toggle):
 *   - Automatically selects the newest active live workload upon registration
 *   - Renders Live Monitor, RAG/WFG Canvas, and Live Banker's Algorithm from a single
 *     authoritative `snapshot_version` (rejecting out-of-order / stale updates)
 *   - Keeps educational practice scenarios isolated in a separate collapsed section
 *   - Clears aborted/timed-out live state cleanly while preserving historical inspection
 */

const DeadlockGraph = (() => {
    let canvas, ctx;
    let currentGraph = { nodes: [], edges: [], rag_edges: [], wfg_edges: [] };
    let currentResult = null;
    let animFrameId = null;
    let pulsePhase = 0;
    let currentViewMode = 'rag'; // 'rag' (Process + Resource) or 'wfg' (Process only)

    // Authoritative Live Run State
    let activeRuns = [];
    let historicalRuns = [];
    let selectedRunId = null;
    let inspectingHistory = false;
    let currentLiveRun = null;
    let lastRenderedRunId = null;
    let lastRenderedVersion = -1;
    let knownActiveIds = new Set();
    let watchdogTickTimer = null;

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

        // View mode toggle (RAG vs WFG)
        const btnToggleMode = document.getElementById('btnToggleGraphView');
        if (btnToggleMode) {
            btnToggleMode.addEventListener('click', toggleViewMode);
        }

        // Live Workload Selector & Actions
        const liveSelect = document.getElementById('liveRunSelect');
        const btnReturnToLive = document.getElementById('btnReturnToLive');
        const btnApprove = document.getElementById('btnApproveLiveRecovery');
        const btnAbort = document.getElementById('btnAbortLiveRun');

        if (liveSelect) {
            liveSelect.addEventListener('change', () => {
                const chosen = liveSelect.value || null;
                if (!chosen) {
                    inspectingHistory = false;
                    selectedRunId = null;
                    renderEmptyLiveState();
                    return;
                }
                const isHist = historicalRuns.some(r => r.run_id === chosen);
                inspectingHistory = isHist;
                selectedRunId = chosen;
                lastRenderedRunId = null;
                lastRenderedVersion = -1;
                fetchLiveRunDetail(chosen);
            });
        }

        if (btnReturnToLive) {
            btnReturnToLive.addEventListener('click', () => {
                inspectingHistory = false;
                lastRenderedRunId = null;
                lastRenderedVersion = -1;
                if (activeRuns.length > 0) {
                    selectedRunId = activeRuns[0].run_id;
                    fetchLiveRunDetail(selectedRunId);
                } else {
                    selectedRunId = null;
                    renderEmptyLiveState();
                }
                renderLiveRunSelector();
            });
        }

        if (btnApprove) {
            btnApprove.addEventListener('click', approveLiveRecovery);
        }
        if (btnAbort) {
            btnAbort.addEventListener('click', abortLiveRun);
        }

        // Isolated Offline Practice Section Controls (never touch live monitor or live canvas)
        const btnPracticeWfg = document.getElementById('btnLoadPracticeWfg');
        const btnPracticeSafe = document.getElementById('btnPracticeBankerSafe');
        const btnPracticeUnsafe = document.getElementById('btnPracticeBankerUnsafe');

        if (btnPracticeWfg) {
            btnPracticeWfg.addEventListener('click', loadPracticeWfgScenario);
        }
        if (btnPracticeSafe) {
            btnPracticeSafe.addEventListener('click', () => loadPracticeBankerScenario('safe'));
        }
        if (btnPracticeUnsafe) {
            btnPracticeUnsafe.addEventListener('click', () => loadPracticeBankerScenario('unsafe'));
        }

        // Initialize cleanly in empty live state (NO dummy circular_wait or static Banker's)
        renderEmptyLiveState();
        fetchLiveRunsInitial();
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
    }

    function isTabVisible() {
        const panel = document.getElementById('panelDeadlock');
        return panel && panel.classList.contains('active');
    }

    function onTabActivated() {
        resizeCanvas();
        layoutGraphNodes();
        drawGraph();
        requestAnimationFrame(() => {
            resizeCanvas();
            layoutGraphNodes();
            drawGraph();
            if (currentResult && currentResult.has_deadlock) {
                startAnimationLoop();
            }
        });

        // One-shot refresh on tab activation; ongoing updates arrive via WebSocket push
        fetchLiveRunsInitial();
        if (!watchdogTickTimer) {
            watchdogTickTimer = setInterval(updateWatchdogCountdownUI, 500);
        }
    }

    function onTabDeactivated() {
        stopAnimationLoop();
        if (watchdogTickTimer) {
            clearInterval(watchdogTickTimer);
            watchdogTickTimer = null;
        }
    }

    // ── Real-Time WebSocket Ingestion (Authoritative & Version-Checked) ──────

    function handleLiveSnapshotPush(msg) {
        if (!msg) return;
        if (msg.live_deadlock) {
            syncRunLists(msg.live_deadlock);
        }
        const snap = msg.snapshot;
        if (!snap) return;

        // Automatically switch to a newly registered active run
        if (snap.is_active_run && (!selectedRunId || inspectingHistory || !activeRuns.some(r => r.run_id === selectedRunId))) {
            inspectingHistory = false;
            selectedRunId = snap.run_id;
            renderLiveRunSelector();
        }

        // Apply authoritative snapshot if it matches the selected run
        if (snap.run_id === selectedRunId) {
            applyAuthoritativeSnapshot(snap);
        }
    }

    function updateFromTelemetry(liveSummary) {
        if (!liveSummary) return;
        const prevDeadlocked = new Set(activeRuns.filter(r => r.has_deadlock).map(r => r.run_id));
        syncRunLists(liveSummary);

        // Notify once when a live run enters deadlock
        for (const r of activeRuns) {
            if (r.has_deadlock && !prevDeadlocked.has(r.run_id)) {
                App.showToast(`Live Deadlock Detected: ${r.workload_name} (${r.run_id})`, 'error');
            }
        }

        // If user is NOT explicitly inspecting history, follow active runs automatically
        if (!inspectingHistory) {
            if (activeRuns.length === 0) {
                // If previously selected run just transitioned to resolved/completed/aborted, keep showing its final outcome
                // ONLY if it's the run we were actively watching and it completed/resolved cleanly; if aborted or none selected, clear live state.
                if (selectedRunId) {
                    const justEnded = historicalRuns.find(r => r.run_id === selectedRunId);
                    if (justEnded && (justEnded.status === 'aborted' || justEnded.status === 'timed_out')) {
                        fetchLiveRunDetail(selectedRunId);
                        return;
                    }
                    if (justEnded && justEnded.status === 'completed') {
                        fetchLiveRunDetail(selectedRunId);
                        return;
                    }
                }
                selectedRunId = null;
                renderEmptyLiveState();
                return;
            }

            if (!selectedRunId || !activeRuns.some(r => r.run_id === selectedRunId)) {
                selectedRunId = liveSummary.newest_active_run_id || activeRuns[0].run_id;
                renderLiveRunSelector();
            }

            if (liveSummary.active_snapshot && liveSummary.active_snapshot.run_id === selectedRunId) {
                applyAuthoritativeSnapshot(liveSummary.active_snapshot);
            } else if (selectedRunId) {
                fetchLiveRunDetail(selectedRunId);
            }
        }
    }

    function syncRunLists(summary) {
        const allRuns = summary.runs || [];
        activeRuns = summary.active_runs || allRuns.filter(r => r.is_active_run);
        historicalRuns = summary.historical_runs || allRuns.filter(r => !r.is_active_run);

        // Auto-select any brand-new active run that just registered
        for (const r of activeRuns) {
            if (!knownActiveIds.has(r.run_id)) {
                knownActiveIds.add(r.run_id);
                inspectingHistory = false;
                selectedRunId = r.run_id;
            }
        }

        renderLiveRunSelector();
    }

    async function fetchLiveRunsInitial() {
        try {
            const res = await fetch('/api/deadlock/live/runs');
            if (!res.ok) return;
            const data = await res.json();
            const runs = data.runs || [];
            const active = runs.filter(r => r.is_active_run);
            const historical = runs.filter(r => !r.is_active_run);
            syncRunLists({ runs, active_runs: active, historical_runs: historical });

            if (!inspectingHistory) {
                if (active.length > 0) {
                    if (!selectedRunId || !active.some(r => r.run_id === selectedRunId)) {
                        selectedRunId = active[0].run_id;
                    }
                    await fetchLiveRunDetail(selectedRunId);
                } else {
                    selectedRunId = null;
                    renderEmptyLiveState();
                }
            } else if (selectedRunId) {
                await fetchLiveRunDetail(selectedRunId);
            }
        } catch (e) {
            console.debug('Failed to fetch live deadlock runs:', e);
        }
    }

    async function fetchLiveRunDetail(runId) {
        if (!runId) return;
        try {
            const res = await fetch(`/api/deadlock/live/runs/${runId}`);
            if (!res.ok) return;
            const snap = await res.json();
            applyAuthoritativeSnapshot(snap);
        } catch (e) {
            console.debug('Failed to fetch live run detail:', e);
        }
    }

    // ── Single Authoritative Snapshot Renderer (Atomic WFG + Banker's Sync) ──

    function applyAuthoritativeSnapshot(snap) {
        if (!snap || !snap.run_id) return;

        // Monotonic snapshot_version guard: reject stale out-of-order updates
        const ver = Number(snap.snapshot_version || 0);
        if (snap.run_id === lastRenderedRunId && ver < lastRenderedVersion) {
            return;
        }
        lastRenderedRunId = snap.run_id;
        lastRenderedVersion = ver;
        currentLiveRun = snap;

        renderLiveRunMonitor(snap);

        const isAbortedOrTimedOut = snap.status === 'aborted' || snap.status === 'timed_out';
        if (isAbortedOrTimedOut) {
            // Never display stale deadlocked graph or stale Banker matrices for aborted runs
            clearLiveGraphAndBanker(
                snap.status === 'aborted'
                    ? `RUN ABORTED (${snap.workload_name})`
                    : `WATCHDOG EXPIRED (${snap.workload_name})`,
                'Active deadlock nodes and Banker matrices have been cleared. Historical events remain available in the audit log above.'
            );
            return;
        }

        // Render RAG/WFG and Live Banker's Algorithm strictly from THIS snapshot version
        renderLiveGraphFromSnapshot(snap);
        renderLiveBankerFromSnapshot(snap);
    }

    function renderEmptyLiveState() {
        currentLiveRun = null;
        lastRenderedRunId = null;
        lastRenderedVersion = -1;

        const empty = document.getElementById('liveRunEmptyState');
        const active = document.getElementById('liveRunActiveContent');
        const histBanner = document.getElementById('liveHistoryBanner');
        const btnRet = document.getElementById('btnReturnToLive');

        if (empty) empty.style.display = 'block';
        if (active) active.style.display = 'none';
        if (histBanner) histBanner.style.display = 'none';
        if (btnRet) btnRet.style.display = 'none';

        clearLiveGraphAndBanker(
            'NO ACTIVE DEADLOCK WORKLOAD',
            'Run one of the three Python scenario files from a separate VS Code terminal. Waiting for live workload registration...'
        );
    }

    function clearLiveGraphAndBanker(titleText, detailText) {
        stopAnimationLoop();
        currentGraph = { nodes: [], edges: [], rag_edges: [], wfg_edges: [] };
        currentResult = null;
        drawGraph();

        const overlay = document.getElementById('wfgEmptyOverlay');
        if (overlay) {
            overlay.style.display = 'flex';
            overlay.innerHTML = `
                <div style="font-weight: 700; font-size: 0.92rem; letter-spacing: 0.04em; color: var(--text-primary); margin-bottom: 6px;">
                    ${escapeHtml(titleText)}
                </div>
                <div style="font-size: 0.82rem; color: var(--text-secondary); max-width: 380px;">
                    ${escapeHtml(detailText)}
                </div>
            `;
        }

        const wfgBadge = document.getElementById('wfgSnapshotBadge');
        const bkBadge = document.getElementById('bankerSnapshotBadge');
        if (wfgBadge) wfgBadge.textContent = 'Snapshot --';
        if (bkBadge) bkBadge.textContent = 'Snapshot --';

        const box = document.getElementById('wfgResultBox');
        const title = document.getElementById('wfgStatusTitle');
        const detail = document.getElementById('wfgStatusDetail');
        if (box) {
            box.style.borderColor = 'var(--border-default)';
            box.style.background = 'var(--bg-surface-alt)';
        }
        if (title) title.textContent = titleText;
        if (detail) detail.textContent = detailText;

        const resList = document.getElementById('bankerResourcesList');
        const tbody = document.getElementById('bankerTableBody');
        const availVec = document.getElementById('bankerAvailableVec');
        const safeSeq = document.getElementById('bankerSafeSeqInline');
        const bkBox = document.getElementById('bankerResultBox');

        if (resList) resList.textContent = '--';
        if (availVec) availVec.textContent = '--';
        if (safeSeq) safeSeq.textContent = '--';
        if (bkBox) bkBox.style.display = 'none';
        if (tbody) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="5" style="text-align: center; color: var(--text-muted); padding: var(--space-md);">
                        ${escapeHtml(titleText)} — ${escapeHtml(detailText)}
                    </td>
                </tr>
            `;
        }
    }

    function renderLiveRunSelector() {
        const sel = document.getElementById('liveRunSelect');
        if (!sel) return;

        if (activeRuns.length === 0 && historicalRuns.length === 0) {
            sel.innerHTML = '<option value="">-- Waiting for Live Workload --</option>';
            return;
        }

        let html = '';
        if (activeRuns.length > 0) {
            html += '<optgroup label="Active Live Workloads">';
            for (const r of activeRuns) {
                const stateLabel = r.status === 'deadlocked' ? 'DEADLOCKED (ACTIVE)' : 'ACTIVE';
                const label = `${r.workload_name} — ${stateLabel} [${r.run_id}]`;
                const selected = r.run_id === selectedRunId ? 'selected' : '';
                html += `<option value="${escapeHtml(r.run_id)}" ${selected}>${escapeHtml(label)}</option>`;
            }
            html += '</optgroup>';
        } else {
            const noneSelected = !selectedRunId ? 'selected' : '';
            html += `<option value="" ${noneSelected}>-- No Active Workload (Live Idle) --</option>`;
        }

        if (historicalRuns.length > 0) {
            html += '<optgroup label="Historical Runs (Read-Only)">';
            for (const r of historicalRuns) {
                const tag = r.selective_recovery_succeeded ? 'RESOLVED' : (r.status || 'ended').toUpperCase();
                const label = `${r.workload_name} — ${tag} [${r.run_id}]`;
                const selected = r.run_id === selectedRunId ? 'selected' : '';
                html += `<option value="${escapeHtml(r.run_id)}" ${selected}>${escapeHtml(label)}</option>`;
            }
            html += '</optgroup>';
        }

        sel.innerHTML = html;
    }

    function updateWatchdogCountdownUI() {
        const el = document.getElementById('liveWatchdogCountdown');
        if (!el || !currentLiveRun) return;
        if (!currentLiveRun.is_active_run) {
            el.textContent = currentLiveRun.status === 'timed_out' ? 'EXPIRED (0.0s)' : 'STOPPED';
            return;
        }
        const remaining = Math.max(0, (currentLiveRun.watchdog_deadline || 0) - (Date.now() / 1000));
        el.textContent = `${remaining.toFixed(1)}s / ${currentLiveRun.max_runtime_seconds}s`;
    }

    function renderLiveRunMonitor(run) {
        const empty = document.getElementById('liveRunEmptyState');
        const active = document.getElementById('liveRunActiveContent');
        const histBanner = document.getElementById('liveHistoryBanner');
        const btnRet = document.getElementById('btnReturnToLive');

        if (empty) empty.style.display = 'none';
        if (active) active.style.display = 'block';

        if (histBanner) {
            if (!run.is_active_run) {
                histBanner.style.display = 'block';
                histBanner.innerHTML = `<strong>Historical Run (${escapeHtml(run.status.toUpperCase())}):</strong> Viewing preserved run <code>${escapeHtml(run.run_id)}</code> (${escapeHtml(run.workload_name)}). Live recovery controls are disabled.`;
            } else {
                histBanner.style.display = 'none';
            }
        }
        if (btnRet) {
            btnRet.style.display = (!run.is_active_run || inspectingHistory) ? 'inline-flex' : 'none';
        }

        const scenEl = document.getElementById('liveRunScenarioName');
        const statusBadge = document.getElementById('liveRunStatusBadge');
        const sbName = document.getElementById('liveRunSandboxName');
        const supInfo = document.getElementById('liveRunSupervisorInfo');
        const limInfo = document.getElementById('liveRunLimitsInfo');
        const verBadge = document.getElementById('liveSnapshotVersionBadge');

        if (scenEl) scenEl.textContent = run.workload_name || '--';
        if (verBadge) verBadge.textContent = `Snapshot v${run.snapshot_version}`;
        if (statusBadge) {
            const st = (run.status || 'unknown').toUpperCase();
            statusBadge.textContent = st;
            statusBadge.className = `priority-badge ${run.status === 'deadlocked' || run.status === 'timed_out' || run.status === 'aborted' ? 'high' : (run.status === 'resolved' || run.status === 'completed' ? 'normal' : 'idle')}`;
        }
        if (sbName) {
            sbName.textContent = `${run.sandbox.sandbox_name} (${run.sandbox.is_active ? 'ACTIVE' : 'CLEANED UP'})`;
        }
        if (supInfo) {
            supInfo.textContent = `PID ${run.supervisor.pid} (${run.supervisor.alive ? 'ALIVE' : 'EXITED'})`;
        }
        if (limInfo) {
            const vl = run.sandbox.verified_limits || {};
            limInfo.textContent = `CPU ${vl.cpu_rate_percent ?? '--'}% | JobRAM ${vl.job_memory_limit_mb ?? '--'}MB | ProcRAM ${vl.process_memory_limit_mb ?? '--'}MB | MaxProcs ${vl.active_process_limit ?? '--'} | Verified: ${vl.verified ? 'YES' : 'NO'}`;
        }
        updateWatchdogCountdownUI();

        // Explicit User-Approved Selective Recovery Box (STRICTLY active runs only)
        const recBox = document.getElementById('liveRecoveryBox');
        const recDetail = document.getElementById('liveRecoveryDetail');
        const dl = run.deadlock_detection || {};
        const victim = dl.recommended_victim_details;

        if (recBox) {
            if (run.is_active_run && dl.has_deadlock && victim) {
                recBox.style.display = 'block';
                const holds = Object.entries(victim.allocated || {}).filter(([, v]) => v > 0).map(([k]) => k).join(', ') || 'None';
                const waits = Object.entries(victim.requesting || {}).filter(([, v]) => v > 0).map(([k]) => k).join(', ') || 'None';
                if (recDetail) {
                    recDetail.innerHTML = `
                        <strong>Recommended Victim:</strong> ${escapeHtml(victim.name)} (OS PID <code>${victim.pid}</code>, TID <code>${victim.tid}</code>) &bull;
                        <strong>App Recovery Priority:</strong> <code>${victim.recovery_priority}</code> (1 = lowest; OS sched: <code>${escapeHtml(victim.os_scheduling_priority)}</code>) &bull;
                        <strong>Holds:</strong> ${escapeHtml(holds)} &bull; <strong>Waits For:</strong> ${escapeHtml(waits)}
                        <br><span style="font-size: 0.76rem; color: var(--text-secondary);">Approving terminates ONLY PID ${victim.pid} via TerminateProcess; Job Object '${escapeHtml(run.sandbox.sandbox_name)}' and Supervisor PID ${run.supervisor.pid} remain active.</span>
                    `;
                }
            } else {
                recBox.style.display = 'none';
            }
        }

        // Outcome Banner
        const outcomeBox = document.getElementById('liveOutcomeBox');
        if (outcomeBox) {
            if (run.status === 'timed_out') {
                outcomeBox.style.display = 'block';
                outcomeBox.style.background = 'var(--status-danger-bg)';
                outcomeBox.style.border = '1px solid var(--status-danger)';
                outcomeBox.innerHTML = `<strong style="color: var(--status-danger-text);">WATCHDOG EMERGENCY CLEANUP (NOT SELECTIVE RECOVERY):</strong> Maximum runtime (${run.max_runtime_seconds}s) expired. Whole-job <code>TerminateJobObject</code> executed.`;
            } else if (run.status === 'aborted') {
                outcomeBox.style.display = 'block';
                outcomeBox.style.background = 'var(--bg-surface-alt)';
                outcomeBox.style.border = '1px solid var(--border-default)';
                outcomeBox.innerHTML = `<strong style="color: var(--status-danger-text);">ABORTED:</strong> Run was torn down and its live graph cleared. Historical audit events are preserved below.`;
            } else if (run.selective_recovery_succeeded) {
                outcomeBox.style.display = 'block';
                outcomeBox.style.background = 'var(--status-success-bg)';
                outcomeBox.style.border = '1px solid var(--status-success)';
                outcomeBox.innerHTML = `<strong style="color: var(--status-success-text);">DEADLOCK RESOLVED (SELECTIVE RECOVERY VERIFIED):</strong> Approved victim terminated via <code>TerminateProcess</code>; OS <code>WAIT_ABANDONED</code> reconciled; remaining workers progressed and completed.`;
            } else {
                outcomeBox.style.display = 'none';
            }
        }

        // Workers Table (do not display deadlocked rows for aborted runs)
        const wBody = document.getElementById('liveWorkersTableBody');
        if (wBody) {
            const workers = run.workers || [];
            if (run.status === 'aborted' || run.status === 'timed_out') {
                wBody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--text-muted);">Run ${escapeHtml(run.status.toUpperCase())} — active worker rows cleared (see preserved audit log).</td></tr>`;
            } else if (workers.length === 0) {
                wBody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--text-muted);">Waiting for child worker processes to register...</td></tr>';
            } else {
                wBody.innerHTML = workers.map(w => {
                    const holds = Object.entries(w.allocated || {}).filter(([, v]) => v > 0).map(([k]) => k).join(', ') || '-';
                    const waits = Object.entries(w.requesting || {}).filter(([, v]) => v > 0).map(([k]) => k).join(', ') || '-';
                    return `
                        <tr>
                            <td style="font-weight: 700;">${escapeHtml(w.name)}</td>
                            <td class="pid-cell">${w.pid} / ${w.tid}</td>
                            <td><strong>RecPri ${w.recovery_priority}</strong> <span style="font-size:0.72rem;color:var(--text-secondary);">(OS: ${escapeHtml(w.os_scheduling_priority)})</span></td>
                            <td>${w.in_job_verified ? '<span style="color:var(--status-success-text);font-weight:600;">YES</span>' : 'NO'}</td>
                            <td style="font-family:var(--font-mono);font-size:0.75rem;">H: ${escapeHtml(holds)} | W: ${escapeHtml(waits)}</td>
                            <td><span class="priority-badge ${w.state === 'deadlocked' || w.state === 'terminated_victim' ? 'high' : 'normal'}">${escapeHtml(w.state.toUpperCase())}</span></td>
                        </tr>
                    `;
                }).join('');
            }
        }

        // Audit Log
        const logEl = document.getElementById('liveEventLog');
        if (logEl) {
            const evts = (run.events || []).slice().reverse();
            logEl.innerHTML = evts.map(ev => {
                const ts = new Date(ev.timestamp * 1000).toLocaleTimeString();
                const isAbandoned = ev.event_type === 'abandoned_acquired' || ev.event_type === 'mutex_abandoned';
                const color = isAbandoned
                    ? 'var(--status-warning-text)'
                    : (ev.event_type === 'watchdog_expired' || ev.event_type === 'victim_terminated' || ev.event_type === 'run_aborted'
                        ? 'var(--status-danger-text)'
                        : 'var(--text-primary)');
                return `<div style="margin-bottom: 4px; color: ${color};">[${ts}] <strong>${escapeHtml(ev.event_type)}</strong> (${escapeHtml(ev.actor)}): ${escapeHtml(ev.details)}</div>`;
            }).join('');
        }
    }

    // ── Live RAG / WFG Canvas Rendering ──────────────────────────────────────

    function renderLiveGraphFromSnapshot(snap) {
        const wfgBadge = document.getElementById('wfgSnapshotBadge');
        if (wfgBadge) wfgBadge.textContent = `Snapshot v${snap.snapshot_version}`;

        const graph = snap.graph || { nodes: [], edges: [], rag_edges: [], wfg_edges: [] };
        const dl = snap.deadlock_detection || null;

        currentGraph = graph;
        currentResult = dl;

        const overlay = document.getElementById('wfgEmptyOverlay');
        const hasNodes = graph.nodes && graph.nodes.length > 0;
        if (overlay) {
            if (!hasNodes) {
                overlay.style.display = 'flex';
                overlay.innerHTML = `
                    <div style="font-weight: 700; font-size: 0.9rem; color: var(--text-primary); margin-bottom: 6px;">
                        ${snap.status === 'completed' || snap.status === 'resolved' ? 'WORKLOAD COMPLETED — ALL RESOURCES RELEASED' : 'INITIALIZING LIVE WORKLOAD'}
                    </div>
                    <div style="font-size: 0.8rem; color: var(--text-secondary);">
                        ${escapeHtml(snap.workload_name)} (Snapshot v${snap.snapshot_version})
                    </div>
                `;
            } else {
                overlay.style.display = 'none';
            }
        }

        resizeCanvas();
        layoutGraphNodes();
        drawGraph();
        renderWfgResults(dl, snap);

        if (dl && dl.has_deadlock && snap.is_active_run) {
            startAnimationLoop();
        } else {
            stopAnimationLoop();
        }
    }

    function resizeCanvas() {
        if (!canvas) return;
        const parent = canvas.parentElement;
        let w = 0;
        if (parent) {
            const rect = parent.getBoundingClientRect();
            w = Math.floor(rect.width || parent.clientWidth || parent.offsetWidth || 0);
        }
        if (!w || w < 200) {
            const panel = document.getElementById('panelDeadlock');
            const main = document.querySelector('.main-content') || document.body;
            w = Math.floor(panel?.clientWidth || (main ? (main.clientWidth - 80) : 0) || 640);
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
        if (!isTabVisible() || !currentResult || !currentResult.has_deadlock) return;
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
            return currentGraph.wfg_edges || [];
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

        const procs = nodes.filter(n => n.type === 'process');
        const res = nodes.filter(n => n.type === 'resource');

        if (currentViewMode === 'rag' && procs.length > 0 && res.length > 0 && procs.length === res.length) {
            const ordered = [];
            for (let i = 0; i < procs.length; i++) {
                ordered.push(procs[i]);
                ordered.push(res[i]);
            }
            for (let i = 0; i < ordered.length; i++) {
                const angle = (i / ordered.length) * (Math.PI * 2) - Math.PI / 2;
                ordered[i].x = centerX + Math.cos(angle) * radius;
                ordered[i].y = centerY + Math.sin(angle) * radius;
            }
        } else {
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
        if (nodes.length === 0) return;

        const nodeMap = new Map();
        for (const n of nodes) nodeMap.set(n.id, n);

        const involvedProcs = new Set(currentResult?.involved_processes || []);
        const involvedRes = new Set(currentResult?.involved_resources || []);
        const victimId = currentResult?.recommended_victim;

        // Build exact cycle directed pairs so non-cycle blocked workers (e.g. Worker D -> Worker B)
        // are NEVER falsely colored as members of the deadlock cycle.
        const cycleEdgePairs = new Set();
        for (const cyc of (currentResult?.cycles || [])) {
            for (let i = 0; i < cyc.length - 1; i++) {
                cycleEdgePairs.add(`${cyc[i]}->${cyc[i + 1]}`);
            }
        }

        // 1. Draw Edges
        for (const edge of edges) {
            const fromNode = nodeMap.get(edge.from);
            const toNode = nodeMap.get(edge.to);
            if (!fromNode || !toNode) continue;

            let isCycleEdge = false;
            if (currentViewMode === 'wfg') {
                isCycleEdge = cycleEdgePairs.has(`${edge.from}->${edge.to}`);
            } else {
                // RAG edge: highlight ONLY if the process endpoint is in the cycle AND resource is involved
                const procId = fromNode.type === 'process' ? fromNode.id : toNode.id;
                const resId = fromNode.type === 'resource' ? fromNode.id : toNode.id;
                isCycleEdge = involvedProcs.has(procId) && involvedRes.has(resId);
            }

            drawArrow(
                fromNode.x, fromNode.y,
                toNode.x, toNode.y,
                edge.type === 'request' ? 'dashed' : 'solid',
                isCycleEdge,
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

        if (isVictim) {
            ctx.strokeStyle = '#B98532';
            ctx.lineWidth = Math.sin(pulsePhase) * 0.6 + 2.6;
        } else if (isDeadlocked) {
            ctx.strokeStyle = '#C85D5D';
            ctx.lineWidth = Math.sin(pulsePhase) * 0.5 + 2.4;
        } else {
            ctx.strokeStyle = isProc ? '#52748D' : '#298F93';
            ctx.lineWidth = 1.8;
        }

        if (isProc) {
            const r = 25;
            ctx.beginPath();
            ctx.arc(x, y, r, 0, Math.PI * 2);
            ctx.fillStyle = isVictim
                ? 'rgba(185, 133, 50, 0.16)'
                : (isDeadlocked ? 'rgba(200, 93, 93, 0.16)' : 'rgba(82, 116, 141, 0.12)');
            ctx.fill();
            ctx.stroke();

            // Compact process label (e.g. "Worker A" / "Phil 1")
            const rawLabel = (node.label || node.name || node.id).replace(/\s*\(PID\s+\d+\)/i, '');
            const shortLabel = rawLabel.replace(/^Philosopher\s+/i, 'Phil ');
            ctx.fillStyle = '#323035';
            ctx.font = 'bold 10px "JetBrains Mono", monospace';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(shortLabel, x, y - 3);

            if (isVictim) {
                ctx.fillStyle = '#6E490D';
                ctx.font = 'bold 8px "Inter", sans-serif';
                ctx.fillText('VICTIM', x, y + 10);
            } else if (node.recovery_priority !== undefined) {
                ctx.fillStyle = '#625D62';
                ctx.font = '8px "Inter", sans-serif';
                ctx.fillText(`rec:${node.recovery_priority}`, x, y + 10);
            }
        } else {
            const rw = 64;
            const rh = 34;
            ctx.beginPath();
            ctx.roundRect(x - rw / 2, y - rh / 2, rw, rh, 6);
            ctx.fillStyle = isDeadlocked ? 'rgba(200, 93, 93, 0.16)' : 'rgba(41, 143, 147, 0.12)';
            ctx.fill();
            ctx.stroke();

            ctx.fillStyle = '#21777B';
            ctx.font = 'bold 9px "JetBrains Mono", monospace';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(node.label || node.name || node.id, x, y - 4);

            ctx.fillStyle = '#625D62';
            ctx.font = '8px "JetBrains Mono", monospace';
            const total = node.total !== undefined ? node.total : 1;
            const avail = node.available !== undefined ? node.available : 0;
            ctx.fillText(`${avail}/${total} inst`, x, y + 9);
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

        const nodeRadius = 28;
        const startX = fromX + (dx / dist) * nodeRadius;
        const startY = fromY + (dy / dist) * nodeRadius;
        const endX = toX - (dx / dist) * nodeRadius;
        const endY = toY - (dy / dist) * nodeRadius;

        ctx.strokeStyle = isDeadlocked ? '#C85D5D' : (style === 'dashed' ? '#B98532' : '#52748D');
        ctx.lineWidth = isDeadlocked ? 2.2 : 1.6;
        ctx.setLineDash(style === 'dashed' ? [5, 4] : []);

        ctx.beginPath();
        ctx.moveTo(startX, startY);
        ctx.lineTo(endX, endY);
        ctx.stroke();

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

        if (label && dist > 65) {
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

    function renderWfgResults(res, snap) {
        const title = document.getElementById('wfgStatusTitle');
        const detail = document.getElementById('wfgStatusDetail');
        const box = document.getElementById('wfgResultBox');
        if (!res || !box) return;

        if (res.has_deadlock) {
            box.style.borderColor = 'var(--status-danger)';
            box.style.background = 'var(--status-danger-bg)';
            const cycleCount = res.cycles?.length || 1;
            const cyclePath = res.cycles && res.cycles[0] ? res.cycles[0].join(' &rarr; ') : '';
            title.innerHTML = `<span style="color: var(--status-danger-text); font-weight: 700;">LIVE DEADLOCK DETECTED (${cycleCount} Cycle${cycleCount > 1 ? 's' : ''}) &bull; Snapshot v${snap.snapshot_version}</span>`;
            detail.innerHTML = `
                ${cyclePath ? `<div style="margin-bottom: 6px; font-family: var(--font-mono); font-size: 0.8rem; color: var(--status-danger-text);"><strong>DFS Cycle:</strong> ${cyclePath}</div>` : ''}
                <div style="color: var(--text-primary); margin-bottom: 2px;"><strong>Cycle Members:</strong> ${escapeHtml(res.involved_processes?.join(', ') || 'None')}</div>
                <div style="color: var(--text-primary); margin-bottom: 4px;"><strong>Contended Mutexes:</strong> ${escapeHtml(res.involved_resources?.join(', ') || 'None')}</div>
                <div style="margin-top: 6px; color: var(--status-warning-text); font-weight: 600;">
                    Recommended Preemption Victim: ${escapeHtml(res.recommended_victim || 'None')}
                    <br><span style="font-size: 0.76rem; font-weight: 400; color: var(--text-secondary);">${escapeHtml(res.victim_reason || '')}</span>
                </div>
            `;
        } else {
            box.style.borderColor = 'var(--status-success)';
            box.style.background = 'var(--status-success-bg)';
            title.innerHTML = `<span style="color: var(--status-success-text); font-weight: 700;">NO DEADLOCK CYCLE — ${escapeHtml(snap.workload_name)} (Snapshot v${snap.snapshot_version})</span>`;
            detail.textContent = snap.selective_recovery_succeeded
                ? 'Selective recovery verified: circular wait eliminated and remaining workers completed cleanly.'
                : 'No circular wait detected in the live Wait-For Graph.';
        }
    }

    // ── LIVE BANKER'S ALGORITHM Renderer (Strict Live Snapshot Only) ─────────

    function renderLiveBankerFromSnapshot(snap) {
        const bk = snap.banker_analysis;
        const bkBadge = document.getElementById('bankerSnapshotBadge');
        const resList = document.getElementById('bankerResourcesList');
        const thead = document.getElementById('bankerTableHead');
        const tbody = document.getElementById('bankerTableBody');
        const availVec = document.getElementById('bankerAvailableVec');
        const safeSeqEl = document.getElementById('bankerSafeSeqInline');
        const resultBox = document.getElementById('bankerResultBox');

        if (bkBadge) bkBadge.textContent = `Snapshot v${snap.snapshot_version}`;
        if (!bk || !tbody) return;

        const resources = bk.resource_names || [];
        if (resList) {
            resList.textContent = resources.length > 0 ? resources.join(', ') : '--';
        }
        if (thead) {
            const shortRes = resources.map(r => r.replace(/^Chopstick_/, 'C').replace(/^Mutex_/, 'M').replace(/^Lock_/, 'L')).join(' ');
            const hdrSuffix = shortRes ? ` (${escapeHtml(shortRes)})` : '';
            thead.innerHTML = `
                <tr>
                    <th>Process</th>
                    <th>Allocation${hdrSuffix}</th>
                    <th>Max Claim${hdrSuffix}</th>
                    <th>Need${hdrSuffix}</th>
                    <th>Finish</th>
                </tr>
            `;
        }

        if (!bk.data_available) {
            if (availVec) availVec.textContent = 'NOT AVAILABLE';
            if (safeSeqEl) safeSeqEl.textContent = 'NOT AVAILABLE';
            tbody.innerHTML = `
                <tr>
                    <td colspan="5" style="text-align: center; color: var(--text-muted); padding: var(--space-md);">
                        NOT AVAILABLE — ${escapeHtml(bk.semantic_note || 'Required maximum claims or live state unavailable.')}
                    </td>
                </tr>
            `;
            if (resultBox) {
                resultBox.style.display = 'block';
                resultBox.className = 'banker-result';
                resultBox.innerHTML = `<strong>Banker's Status: NOT AVAILABLE</strong><div style="font-size:0.78rem;margin-top:4px;">${escapeHtml(bk.semantic_note || '')}</div>`;
            }
            return;
        }

        if (availVec) {
            availVec.textContent = `[ ${(bk.available || []).join('  ')} ]`;
        }
        if (safeSeqEl) {
            safeSeqEl.textContent = bk.is_safe
                ? ((bk.safe_sequence && bk.safe_sequence.length) ? bk.safe_sequence.join(' → ') : 'All Finished')
                : 'None (UNSAFE)';
        }

        const procs = bk.process_names || [];
        const alloc = bk.allocation_matrix || [];
        const max = bk.max_matrix || [];
        const need = bk.need_matrix || [];
        const finish = bk.finish_flags || [];

        if (procs.length === 0) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="5" style="text-align: center; color: var(--text-muted); padding: var(--space-md);">
                        ${escapeHtml(bk.semantic_note || 'No active workers holding resources.')}
                    </td>
                </tr>
            `;
        } else {
            let html = '';
            for (let i = 0; i < procs.length; i++) {
                const aStr = (alloc[i] || []).join('  ');
                const mStr = (max[i] || []).join('  ');
                const nStr = (need[i] || []).join('  ');
                const finVal = finish[i] === true;
                html += `
                    <tr>
                        <td style="font-weight: 700; color: var(--accent-hover);">${escapeHtml(procs[i])}</td>
                        <td style="font-family: var(--font-mono);">[ ${escapeHtml(aStr)} ]</td>
                        <td style="font-family: var(--font-mono);">[ ${escapeHtml(mStr)} ]</td>
                        <td style="font-family: var(--font-mono); color: var(--status-info-text); font-weight: 600;">[ ${escapeHtml(nStr)} ]</td>
                        <td><span class="priority-badge ${finVal ? 'normal' : 'high'}">${finVal ? 'TRUE' : 'FALSE'}</span></td>
                    </tr>
                `;
            }
            tbody.innerHTML = html;
        }

        if (resultBox) {
            resultBox.style.display = 'block';
            const noteHtml = bk.semantic_note
                ? `<div style="font-size: 0.76rem; margin-top: 4px; color: var(--text-secondary);">${escapeHtml(bk.semantic_note)}</div>`
                : '';
            if (bk.is_safe) {
                resultBox.className = 'banker-result safe';
                resultBox.innerHTML = `
                    <div style="font-size: 0.9rem; margin-bottom: 4px;">Live Banker's State: <strong>SAFE</strong> (Snapshot v${snap.snapshot_version})</div>
                    <div style="font-size: 0.8rem; font-family: var(--font-mono);">
                        Safe Sequence: <strong>${bk.safe_sequence && bk.safe_sequence.length ? escapeHtml(bk.safe_sequence.join(' → ')) : 'All Processes Completed'}</strong>
                    </div>
                    ${noteHtml}
                `;
            } else {
                resultBox.className = 'banker-result unsafe';
                resultBox.innerHTML = `
                    <div style="font-size: 0.9rem; margin-bottom: 4px;">Live Banker's State: <strong>UNSAFE</strong> (Snapshot v${snap.snapshot_version})</div>
                    <div style="font-size: 0.8rem; font-family: var(--font-mono);">Safe Sequence: <strong>None</strong></div>
                    ${noteHtml}
                `;
            }
        }
    }

    // ── Explicit User-Approved Selective Recovery & Abort ────────────────────

    async function approveLiveRecovery() {
        if (!currentLiveRun || !currentLiveRun.is_active_run || !currentLiveRun.deadlock_detection?.recommended_victim_details) {
            App.showToast('No active deadlocked victim available for recovery', 'warning');
            return;
        }
        const runId = currentLiveRun.run_id;
        const victim = currentLiveRun.deadlock_detection.recommended_victim_details;
        const sandboxName = currentLiveRun.sandbox.sandbox_name;

        const ok = confirm(
            `APPROVE SELECTIVE RECOVERY?\n\n` +
            `Scenario: ${currentLiveRun.workload_name} (${runId})\n` +
            `Sandbox: ${sandboxName}\n` +
            `Victim Worker: ${victim.name} (OS PID ${victim.pid})\n` +
            `Application Recovery Priority: ${victim.recovery_priority} (lowest eligible in cycle)\n\n` +
            `Only PID ${victim.pid} will be terminated via TerminateProcess.\n` +
            `The Windows Job Object and Supervisor PID ${currentLiveRun.supervisor.pid} will remain active.`
        );
        if (!ok) return;

        try {
            const chRes = await fetch(`/api/deadlock/live/runs/${runId}/recovery-challenge`);
            if (!chRes.ok) {
                const err = await chRes.json();
                App.showToast(`Recovery challenge failed: ${err.detail || 'Error'}`, 'error');
                return;
            }
            const challenge = await chRes.json();

            const recRes = await fetch(`/api/deadlock/live/runs/${runId}/recover`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Recovery-Token': challenge.token,
                },
                body: JSON.stringify({
                    sandbox_name: challenge.sandbox_name,
                    pid: challenge.pid,
                    create_time_100ns: challenge.create_time_100ns,
                    approved: true,
                    operator_note: 'Approved via Web Dashboard operator prompt',
                }),
            });

            if (recRes.ok) {
                const data = await recRes.json();
                if (data.run) applyAuthoritativeSnapshot(data.run);
                App.showToast(
                    data.deadlock_resolved
                        ? `Deadlock resolved! Victim PID ${victim.pid} terminated & WAIT_ABANDONED reconciled.`
                        : `Victim terminated; re-evaluating graph.`,
                    data.deadlock_resolved ? 'success' : 'warning'
                );
            } else {
                const err = await recRes.json();
                App.showToast(`Recovery rejected: ${err.detail || 'Error'}`, 'error');
            }
        } catch (e) {
            App.showToast(`Recovery request failed: ${e.message}`, 'error');
        }
    }

    async function abortLiveRun() {
        if (!currentLiveRun || !currentLiveRun.is_active_run) return;
        const runId = currentLiveRun.run_id;
        if (!confirm(`Abort entire sandbox '${currentLiveRun.sandbox.sandbox_name}' via TerminateJobObject?`)) {
            return;
        }
        try {
            const res = await fetch(`/api/deadlock/live/runs/${runId}`, { method: 'DELETE' });
            if (res.ok) {
                const abortedSnap = await res.json();
                applyAuthoritativeSnapshot(abortedSnap);
                App.showToast(`Run ${runId} aborted & live graph cleared`, 'info');
            }
        } catch (e) {
            App.showToast(`Abort failed: ${e.message}`, 'error');
        }
    }

    // ── Isolated Educational Practice Scenarios (Never Touch Live View) ──────

    async function loadPracticeWfgScenario() {
        const sel = document.getElementById('practiceWfgScenarioSelect');
        const out = document.getElementById('practiceWfgOutput');
        const scenarioId = sel ? sel.value : 'circular_wait';
        if (!out) return;

        try {
            const res = await fetch(`/api/deadlock/wfg/load/${scenarioId}`, { method: 'POST' });
            if (!res.ok) return;
            const data = await res.json();
            const r = data.result;
            const cycleText = r.cycles && r.cycles.length ? r.cycles.map(c => c.join(' -> ')).join(' | ') : 'None';
            out.innerHTML = `
                <div><strong>Scenario:</strong> ${escapeHtml(scenarioId)} (${data.graph.nodes.length} nodes)</div>
                <div><strong>Deadlock:</strong> ${r.has_deadlock ? 'YES' : 'NO'} &bull; <strong>Cycles:</strong> ${escapeHtml(cycleText)}</div>
                <div><strong>Recommended Victim:</strong> ${escapeHtml(r.recommended_victim || 'None')}</div>
                <div style="margin-top:4px;color:var(--text-muted);">${escapeHtml(r.victim_reason || 'No preemption needed.')}</div>
            `;
        } catch (e) {
            out.textContent = `Failed to load practice scenario: ${e.message}`;
        }
    }

    async function loadPracticeBankerScenario(scenarioId) {
        const out = document.getElementById('practiceBankerOutput');
        if (!out) return;
        try {
            const res = await fetch(`/api/deadlock/banker/scenarios/${scenarioId}`);
            if (!res.ok) return;
            const data = await res.json();
            const s = data.scenario;
            const r = data.result;
            out.innerHTML = `
                <div><strong>Example:</strong> ${escapeHtml(scenarioId.toUpperCase())} &bull; <strong>Available:</strong> [${s.available.join(', ')}]</div>
                <div><strong>Processes:</strong> ${escapeHtml(s.processes.join(', '))} &bull; <strong>Resources:</strong> ${escapeHtml(s.resources.join(', '))}</div>
                <div><strong>Result:</strong> ${r.is_safe ? 'SAFE' : 'UNSAFE'} &bull; <strong>Safe Sequence:</strong> ${r.safe_sequence.length ? escapeHtml(r.safe_sequence.join(' -> ')) : 'None'}</div>
            `;
        } catch (e) {
            out.textContent = `Failed to load practice Banker example: ${e.message}`;
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
        toggleViewMode,
        updateFromTelemetry,
        handleLiveSnapshotPush,
        onTabActivated,
        onTabDeactivated
    };
})();
