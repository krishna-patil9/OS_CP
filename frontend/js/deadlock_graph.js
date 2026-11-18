/**
 * deadlock_graph.js — User-Friendly Live RAG / WFG & Banker's Algorithm Visualizer
 *
 * Features:
 *   - Plain, scannable user-facing copy (no raw internal jargon clutter)
 *   - Topology-aware RAG & WFG layouts (diamond for 2-proc, perimeter-traced ring for
 *     Dining Philosophers, separated cycle + branch for Multi-Resource)
 *   - Automatic curved opposing arcs, exact node-border arrow clipping, pill-backed
 *     edge labels, blueprint dot-grid canvas, and interactive node hover tooltips
 *   - Strict live synchronization via WebSocket push + monotonic `snapshot_version` guard
 *   - Zero dummy WFG graphs, zero static Banker's matrices in live mode
 */

const DeadlockGraph = (() => {
    let canvas, ctx, tooltipEl;
    let currentGraph = { nodes: [], edges: [], rag_edges: [], wfg_edges: [] };
    let currentResult = null;
    let animFrameId = null;
    let pulsePhase = 0;
    let currentViewMode = 'rag'; // 'rag' or 'wfg'
    let hoveredNodeId = null;

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

    // ── Friendly Formatting Helpers ──────────────────────────────────────────

    function cleanName(raw) {
        if (!raw) return '';
        return String(raw)
            .replace(/\s*\(PID\s+\d+\)/gi, '')
            .replace(/^worker_([a-z0-9]+)$/i, (_, k) => `Worker ${k.toUpperCase()}`)
            .replace(/^philosopher_(\d+)$/i, (_, n) => `Philosopher ${n}`)
            .replace(/_/g, ' ')
            .trim();
    }

    function shortCanvasName(raw) {
        return cleanName(raw).replace(/^Philosopher\s+/i, 'Phil ');
    }

    function friendlyRunStatus(status, recovered) {
        const map = {
            initializing: 'Starting',
            running: 'Running',
            deadlocked: 'Deadlocked',
            recovering: 'Recovering',
            resolved: 'Resolved',
            completed: recovered ? 'Resolved' : 'Completed',
            timed_out: 'Timed Out',
            aborted: 'Aborted',
        };
        return map[status] || status || 'Unknown';
    }

    function friendlyWorkerState(state) {
        const map = {
            registered: { label: 'Ready', cls: 'idle' },
            running: { label: 'Running', cls: 'normal' },
            waiting: { label: 'Waiting', cls: 'above_normal' },
            deadlocked: { label: 'Deadlocked', cls: 'high' },
            recovering: { label: 'Recovering', cls: 'below_normal' },
            terminated_victim: { label: 'Stopped (Victim)', cls: 'high' },
            exited: { label: 'Finished', cls: 'normal' },
            aborted: { label: 'Aborted', cls: 'idle' },
            timed_out: { label: 'Timed Out', cls: 'idle' },
        };
        return map[state] || { label: state || 'Unknown', cls: 'idle' };
    }

    function friendlyActor(actor) {
        if (!actor) return '';
        return String(actor)
            .replace(/\(PID=(\d+),\s*TID=\d+\)/g, '')
            .replace(/\(PID=\d+\)/g, '')
            .replace(/^WindowsKernel\(victim=([^,]+),\s*PID=\d+\)/, 'Windows OS')
            .replace(/^Operator\([^)]+\)/, 'User')
            .replace(/^DeadlockEngine$/, 'Detector')
            .replace(/^IsolationEngine$/, 'Sandbox')
            .trim();
    }

    function formatAuditEvent(ev) {
        const who = friendlyActor(ev.actor);
        const res = cleanName(ev.resource_id || '');
        switch (ev.event_type) {
            case 'run_registered':
                return { tag: 'Sandbox Ready', cls: 'info', text: 'Windows sandbox created and resource limits verified.' };
            case 'worker_registered':
                return { tag: 'Joined', cls: 'info', text: `${who} joined and verified inside sandbox.` };
            case 'acquired':
                return { tag: 'Acquired', cls: 'info', text: `${who} acquired ${res}.` };
            case 'wait_start':
                return { tag: 'Waiting', cls: 'warning', text: `${who} is waiting for ${res}.` };
            case 'victim_terminated':
                return { tag: 'Victim Stopped', cls: 'danger', text: `Stopped selected victim process; sandbox kept active.` };
            case 'mutex_abandoned':
                return { tag: 'Lock Abandoned', cls: 'warning', text: `${res} was marked abandoned by Windows when its owner stopped.` };
            case 'abandoned_acquired':
                return { tag: 'Lock Recovered', cls: 'warning', text: `${who} took ownership of abandoned ${res} (WAIT_ABANDONED).` };
            case 'state_validated':
                return { tag: 'Data Checked', cls: 'success', text: `${who} verified shared data while holding ${res}.` };
            case 'released':
                return { tag: 'Released', cls: 'success', text: `${who} released ${res}.` };
            case 'worker_exited':
                return { tag: 'Finished', cls: 'success', text: `${who} completed cleanly.` };
            case 'recovery_verified':
                return { tag: 'Resolved', cls: 'success', text: 'Circular wait cleared and system returned to a SAFE state.' };
            case 'run_completed':
                return { tag: 'Completed', cls: 'success', text: 'Workload finished; scheduling sandbox cleanup.' };
            case 'sandbox_cleaned_up':
                return { tag: 'Cleaned Up', cls: 'info', text: 'Windows Job Object closed and released.' };
            case 'run_aborted':
                return { tag: 'Aborted', cls: 'danger', text: 'Workload stopped by user and sandbox cleared.' };
            case 'watchdog_expired':
                return { tag: 'Timed Out', cls: 'danger', text: 'Safety timer expired — emergency sandbox cleanup ran (not selective recovery).' };
            default:
                return { tag: cleanName(ev.event_type), cls: 'info', text: ev.details || '' };
        }
    }

    // ── Initialization ───────────────────────────────────────────────────────

    function init() {
        canvas = document.getElementById('wfgCanvas');
        tooltipEl = document.getElementById('graphHoverTooltip');
        if (canvas) {
            ctx = canvas.getContext('2d');
            resizeCanvas();
            window.addEventListener('resize', () => {
                resizeCanvas();
                layoutGraphNodes();
                drawGraph();
            });
            canvas.addEventListener('mousemove', handleCanvasMouseMove);
            canvas.addEventListener('mouseleave', () => {
                if (hoveredNodeId !== null) {
                    hoveredNodeId = null;
                    hideTooltip();
                    drawGraph();
                }
            });
        }

        // Segmented RAG / WFG mode buttons
        const btnRag = document.getElementById('btnViewRag');
        const btnWfg = document.getElementById('btnViewWfg');
        if (btnRag) btnRag.addEventListener('click', () => setViewMode('rag'));
        if (btnWfg) btnWfg.addEventListener('click', () => setViewMode('wfg'));

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
                inspectingHistory = historicalRuns.some(r => r.run_id === chosen);
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

        if (btnApprove) btnApprove.addEventListener('click', approveLiveRecovery);
        if (btnAbort) btnAbort.addEventListener('click', abortLiveRun);

        // Isolated Offline Practice Section Controls
        const btnPracticeWfg = document.getElementById('btnLoadPracticeWfg');
        const btnPracticeSafe = document.getElementById('btnPracticeBankerSafe');
        const btnPracticeUnsafe = document.getElementById('btnPracticeBankerUnsafe');
        if (btnPracticeWfg) btnPracticeWfg.addEventListener('click', loadPracticeWfgScenario);
        if (btnPracticeSafe) btnPracticeSafe.addEventListener('click', () => loadPracticeBankerScenario('safe'));
        if (btnPracticeUnsafe) btnPracticeUnsafe.addEventListener('click', () => loadPracticeBankerScenario('unsafe'));

        updateViewModeChrome();
        renderEmptyLiveState();
        fetchLiveRunsInitial();
    }

    function setViewMode(mode) {
        if (mode !== 'rag' && mode !== 'wfg') return;
        currentViewMode = mode;
        hoveredNodeId = null;
        hideTooltip();
        updateViewModeChrome();
        resizeCanvas();
        layoutGraphNodes();
        drawGraph();
    }

    function toggleViewMode() {
        setViewMode(currentViewMode === 'rag' ? 'wfg' : 'rag');
    }

    function updateViewModeChrome() {
        const btnRag = document.getElementById('btnViewRag');
        const btnWfg = document.getElementById('btnViewWfg');
        const helper = document.getElementById('graphModeHelper');
        const resLegend = document.getElementById('legendResourceItem');
        const solidLegend = document.getElementById('legendSolidArrow');
        const dashedLegend = document.getElementById('legendDashedArrow');

        if (btnRag) btnRag.classList.toggle('active', currentViewMode === 'rag');
        if (btnWfg) btnWfg.classList.toggle('active', currentViewMode === 'wfg');

        if (helper) {
            helper.textContent = currentViewMode === 'rag'
                ? 'RAG view: solid arrows show which process holds a lock; dashed arrows show which lock it is waiting for. Hover any node for details.'
                : 'WFG view: each arrow shows Process A waiting directly on Process B (labeled with the contended lock). Hover any node for details.';
        }
        if (resLegend) resLegend.style.display = currentViewMode === 'rag' ? 'flex' : 'none';
        if (dashedLegend) dashedLegend.style.display = currentViewMode === 'rag' ? 'flex' : 'none';
        if (solidLegend) {
            solidLegend.innerHTML = currentViewMode === 'rag'
                ? '<span class="graph-legend-line"></span> Holds Lock'
                : '<span class="graph-legend-line"></span> Waits On Process';
        }
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
            if (currentResult && currentResult.has_deadlock) startAnimationLoop();
        });
        fetchLiveRunsInitial();
        if (!watchdogTickTimer) {
            watchdogTickTimer = setInterval(updateWatchdogCountdownUI, 500);
        }
    }

    function onTabDeactivated() {
        stopAnimationLoop();
        hideTooltip();
        if (watchdogTickTimer) {
            clearInterval(watchdogTickTimer);
            watchdogTickTimer = null;
        }
    }

    // ── Real-Time WebSocket Ingestion ────────────────────────────────────────

    function handleLiveSnapshotPush(msg) {
        if (!msg) return;
        if (msg.live_deadlock) syncRunLists(msg.live_deadlock);
        const snap = msg.snapshot;
        if (!snap) return;

        if (snap.is_active_run && (!selectedRunId || inspectingHistory || !activeRuns.some(r => r.run_id === selectedRunId))) {
            inspectingHistory = false;
            selectedRunId = snap.run_id;
            renderLiveRunSelector();
        }
        if (snap.run_id === selectedRunId) {
            applyAuthoritativeSnapshot(snap);
        }
    }

    function updateFromTelemetry(liveSummary) {
        if (!liveSummary) return;
        const prevDeadlocked = new Set(activeRuns.filter(r => r.has_deadlock).map(r => r.run_id));
        syncRunLists(liveSummary);

        for (const r of activeRuns) {
            if (r.has_deadlock && !prevDeadlocked.has(r.run_id)) {
                App.showToast(`Deadlock detected in ${r.workload_name} — open Deadlock tab to resolve.`, 'error');
            }
        }

        if (!inspectingHistory) {
            if (activeRuns.length === 0) {
                if (selectedRunId) {
                    const justEnded = historicalRuns.find(r => r.run_id === selectedRunId);
                    if (justEnded) {
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

    // ── Single Authoritative Snapshot Renderer ───────────────────────────────

    function applyAuthoritativeSnapshot(snap) {
        if (!snap || !snap.run_id) return;
        const ver = Number(snap.snapshot_version || 0);
        if (snap.run_id === lastRenderedRunId && ver < lastRenderedVersion) return;

        lastRenderedRunId = snap.run_id;
        lastRenderedVersion = ver;
        currentLiveRun = snap;

        renderLiveRunMonitor(snap);

        if (snap.status === 'aborted' || snap.status === 'timed_out') {
            clearLiveGraphAndBanker(
                snap.status === 'aborted' ? `${snap.workload_name} — Aborted` : `${snap.workload_name} — Timed Out`,
                'The live graph and Banker table have been cleared. Past events remain in the Activity Feed above.'
            );
            return;
        }

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
            'Start any scenario file in a second terminal to see the live graph and Banker analysis.'
        );
    }

    function clearLiveGraphAndBanker(titleText, detailText) {
        stopAnimationLoop();
        hideTooltip();
        currentGraph = { nodes: [], edges: [], rag_edges: [], wfg_edges: [] };
        currentResult = null;
        drawGraph();

        const overlay = document.getElementById('wfgEmptyOverlay');
        if (overlay) {
            overlay.style.display = 'flex';
            overlay.innerHTML = `
                <div style="font-weight: 700; font-size: 0.92rem; letter-spacing: 0.03em; color: var(--text-primary); margin-bottom: 6px;">
                    ${escapeHtml(titleText)}
                </div>
                <div style="font-size: 0.82rem; color: var(--text-secondary); max-width: 360px; line-height: 1.45;">
                    ${escapeHtml(detailText)}
                </div>
            `;
        }

        const wfgBadge = document.getElementById('wfgSnapshotBadge');
        const bkBadge = document.getElementById('bankerSnapshotBadge');
        if (wfgBadge) wfgBadge.textContent = 'v--';
        if (bkBadge) bkBadge.textContent = 'v--';

        const box = document.getElementById('wfgResultBox');
        const title = document.getElementById('wfgStatusTitle');
        const detail = document.getElementById('wfgStatusDetail');
        if (box) {
            box.style.borderColor = 'var(--border-default)';
            box.style.background = 'var(--bg-surface-alt)';
        }
        if (title) title.textContent = titleText;
        if (detail) detail.textContent = detailText;

        const availChips = document.getElementById('bankerAvailableChips');
        const tbody = document.getElementById('bankerTableBody');
        const availVec = document.getElementById('bankerAvailableVec');
        const safeSeq = document.getElementById('bankerSafeSeqInline');
        const bkBox = document.getElementById('bankerResultBox');

        if (availChips) availChips.innerHTML = '<span class="dl-chip dl-chip-muted">--</span>';
        if (availVec) availVec.textContent = '--';
        if (safeSeq) safeSeq.textContent = '--';
        if (bkBox) bkBox.style.display = 'none';
        if (tbody) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="5" style="text-align: center; color: var(--text-muted); padding: var(--space-md);">
                        ${escapeHtml(titleText)}
                    </td>
                </tr>
            `;
        }
    }

    function renderLiveRunSelector() {
        const sel = document.getElementById('liveRunSelect');
        if (!sel) return;

        if (activeRuns.length === 0 && historicalRuns.length === 0) {
            sel.innerHTML = '<option value="">Waiting for a live workload...</option>';
            return;
        }

        let html = '';
        if (activeRuns.length > 0) {
            html += '<optgroup label="Active Workloads">';
            for (const r of activeRuns) {
                const tag = r.status === 'deadlocked' ? 'DEADLOCKED' : 'ACTIVE';
                const selected = r.run_id === selectedRunId ? 'selected' : '';
                html += `<option value="${escapeHtml(r.run_id)}" ${selected}>${escapeHtml(r.workload_name)} — ${tag}</option>`;
            }
            html += '</optgroup>';
        } else {
            html += `<option value="" ${!selectedRunId ? 'selected' : ''}>No Active Workload</option>`;
        }

        if (historicalRuns.length > 0) {
            html += '<optgroup label="Past Runs (Read-Only)">';
            for (const r of historicalRuns) {
                const tag = friendlyRunStatus(r.status, r.selective_recovery_succeeded).toUpperCase();
                const selected = r.run_id === selectedRunId ? 'selected' : '';
                html += `<option value="${escapeHtml(r.run_id)}" ${selected}>${escapeHtml(r.workload_name)} — ${tag}</option>`;
            }
            html += '</optgroup>';
        }

        sel.innerHTML = html;
    }

    function updateWatchdogCountdownUI() {
        const el = document.getElementById('liveWatchdogCountdown');
        if (!el || !currentLiveRun) return;
        if (!currentLiveRun.is_active_run) {
            el.textContent = currentLiveRun.status === 'timed_out' ? 'Expired' : 'Stopped';
            return;
        }
        const remaining = Math.max(0, (currentLiveRun.watchdog_deadline || 0) - (Date.now() / 1000));
        el.textContent = `${Math.ceil(remaining)}s left`;
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
                histBanner.innerHTML = `Viewing past run <strong>${escapeHtml(run.workload_name)}</strong> (${escapeHtml(friendlyRunStatus(run.status, run.selective_recovery_succeeded))}). Live recovery actions are disabled.`;
            } else {
                histBanner.style.display = 'none';
            }
        }
        if (btnRet) btnRet.style.display = (!run.is_active_run || inspectingHistory) ? 'inline-flex' : 'none';

        const scenEl = document.getElementById('liveRunScenarioName');
        const statusBadge = document.getElementById('liveRunStatusBadge');
        const sbName = document.getElementById('liveRunSandboxName');
        const supInfo = document.getElementById('liveRunSupervisorInfo');
        const workersCount = document.getElementById('liveRunWorkersCount');
        const limInfo = document.getElementById('liveRunLimitsInfo');
        const verBadge = document.getElementById('liveSnapshotVersionBadge');

        if (scenEl) scenEl.textContent = run.workload_name || 'Live Workload';
        if (verBadge) verBadge.textContent = `Run ${run.run_id.slice(0, 6)} · Synced v${run.snapshot_version}`;
        if (statusBadge) {
            const label = friendlyRunStatus(run.status, run.selective_recovery_succeeded);
            statusBadge.textContent = label;
            statusBadge.className = `priority-badge ${
                run.status === 'deadlocked' || run.status === 'timed_out' || run.status === 'aborted'
                    ? 'high'
                    : (run.status === 'resolved' || run.status === 'completed' || run.status === 'running' ? 'normal' : 'idle')
            }`;
        }

        const vl = run.sandbox.verified_limits || {};
        if (sbName) {
            sbName.innerHTML = run.sandbox.is_active
                ? `Active <span class="dl-chip dl-chip-avail">${vl.verified ? 'Verified' : 'Unverified'}</span>`
                : 'Cleaned Up';
        }
        if (limInfo) {
            limInfo.textContent = `CPU ${vl.cpu_rate_percent ?? '--'}% · RAM ${vl.job_memory_limit_mb ?? '--'} MB · Max ${vl.active_process_limit ?? '--'} procs`;
        }
        if (supInfo) {
            supInfo.innerHTML = `PID ${run.supervisor.pid} <span class="dl-chip ${run.supervisor.alive ? 'dl-chip-avail' : 'dl-chip-muted'}">${run.supervisor.alive ? 'Running' : 'Exited'}</span>`;
        }
        if (workersCount) {
            const aliveWorkers = (run.workers || []).filter(w => w.alive).length;
            workersCount.textContent = `${aliveWorkers} of ${run.expected_workers} workers active`;
        }
        updateWatchdogCountdownUI();

        // Actionable Recovery Callout (Plain English)
        const recBox = document.getElementById('liveRecoveryBox');
        const recDetail = document.getElementById('liveRecoveryDetail');
        const btnApprove = document.getElementById('btnApproveLiveRecovery');
        const dl = run.deadlock_detection || {};
        const victim = dl.recommended_victim_details;

        if (recBox) {
            if (run.is_active_run && dl.has_deadlock && victim) {
                recBox.style.display = 'block';
                const holdsList = Object.entries(victim.allocated || {}).filter(([, v]) => v > 0).map(([k]) => cleanName(k));
                const holdsText = holdsList.length ? holdsList.join(', ') : 'its locks';
                const victimName = cleanName(victim.name);

                if (btnApprove) btnApprove.textContent = `Stop ${victimName} (Recommended)`;
                if (recDetail) {
                    recDetail.innerHTML = `
                        Recommended to stop <strong>${escapeHtml(victimName)}</strong> (PID ${victim.pid}, Priority ${victim.recovery_priority} — lowest in the deadlock cycle).
                        Stopping only this worker frees <strong>${escapeHtml(holdsText)}</strong> so the remaining workers can finish while the supervisor stays running.
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
                outcomeBox.innerHTML = `<strong style="color: var(--status-danger-text);">Time Limit Reached:</strong> The ${run.max_runtime_seconds}s safety timer expired and closed the entire sandbox (emergency cleanup, not selective recovery).`;
            } else if (run.status === 'aborted') {
                outcomeBox.style.display = 'block';
                outcomeBox.style.background = 'var(--bg-surface-alt)';
                outcomeBox.style.border = '1px solid var(--border-default)';
                outcomeBox.innerHTML = `<strong style="color: var(--status-danger-text);">Workload Aborted:</strong> All sandbox processes were stopped and the live graph has been cleared.`;
            } else if (run.selective_recovery_succeeded) {
                outcomeBox.style.display = 'block';
                outcomeBox.style.background = 'var(--status-success-bg)';
                outcomeBox.style.border = '1px solid var(--status-success)';
                outcomeBox.innerHTML = `<strong style="color: var(--status-success-text);">Deadlock Resolved:</strong> Only the approved worker was stopped. Surviving workers recovered the abandoned lock, verified shared data, and completed cleanly.`;
            } else {
                outcomeBox.style.display = 'none';
            }
        }

        // Clean Workers Table
        const wBody = document.getElementById('liveWorkersTableBody');
        if (wBody) {
            const workers = run.workers || [];
            if (run.status === 'aborted' || run.status === 'timed_out') {
                wBody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: var(--text-muted); padding: 14px;">Workload ${escapeHtml(friendlyRunStatus(run.status, false).toLowerCase())} — active worker rows cleared.</td></tr>`;
            } else if (workers.length === 0) {
                wBody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: var(--text-muted); padding: 14px;">Waiting for worker processes to start...</td></tr>';
            } else {
                const minPri = Math.min(...workers.map(w => w.recovery_priority));
                wBody.innerHTML = workers.map(w => {
                    const holds = Object.entries(w.allocated || {}).filter(([, v]) => v > 0).map(([k]) => `<span class="dl-chip dl-chip-hold">${escapeHtml(cleanName(k))}</span>`).join(' ') || '<span style="color:var(--text-muted);">None</span>';
                    const waits = Object.entries(w.requesting || {}).filter(([, v]) => v > 0).map(([k]) => `<span class="dl-chip dl-chip-wait">${escapeHtml(cleanName(k))}</span>`).join(' ') || '<span style="color:var(--text-muted);">None</span>';
                    const st = friendlyWorkerState(w.state);
                    const priNote = w.recovery_priority === minPri ? ' <span style="font-size:0.7rem;color:var(--text-muted);">(lowest)</span>' : '';
                    return `
                        <tr>
                            <td>
                                <div style="font-weight: 700; color: var(--text-primary);">${escapeHtml(cleanName(w.name))}</div>
                                <div style="font-size: 0.72rem; font-family: var(--font-mono); color: var(--text-muted);">PID ${w.pid}</div>
                            </td>
                            <td><strong>${w.recovery_priority}</strong>${priNote}</td>
                            <td>${holds}</td>
                            <td>${waits}</td>
                            <td><span class="priority-badge ${st.cls}">${escapeHtml(st.label)}</span></td>
                        </tr>
                    `;
                }).join('');
            }
        }

        // Clean Human-Readable Activity Feed
        const logEl = document.getElementById('liveEventLog');
        if (logEl) {
            const evts = (run.events || []).slice(-30).reverse();
            logEl.innerHTML = evts.map(ev => {
                const ts = new Date(ev.timestamp * 1000).toLocaleTimeString([], { hour12: false });
                const fmt = formatAuditEvent(ev);
                return `
                    <div class="dl-feed-item">
                        <span class="dl-feed-time">${ts}</span>
                        <span class="dl-feed-tag ${fmt.cls}">${escapeHtml(fmt.tag)}</span>
                        <span style="color: var(--text-primary);">${escapeHtml(fmt.text)}</span>
                    </div>
                `;
            }).join('');
        }
    }

    // ── Refined RAG / WFG Canvas Visualizer ──────────────────────────────────

    function renderLiveGraphFromSnapshot(snap) {
        const wfgBadge = document.getElementById('wfgSnapshotBadge');
        if (wfgBadge) wfgBadge.textContent = `v${snap.snapshot_version}`;

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
                    <div style="font-weight: 700; font-size: 0.92rem; color: var(--status-success-text); margin-bottom: 6px;">
                        ${snap.status === 'completed' || snap.status === 'resolved' ? 'All Locks Released — Workload Complete' : 'Starting Workload...'}
                    </div>
                    <div style="font-size: 0.82rem; color: var(--text-secondary);">
                        ${escapeHtml(snap.workload_name)}
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
        if (!w || w < 220) {
            const panel = document.getElementById('panelDeadlock');
            const main = document.querySelector('.main-content') || document.body;
            w = Math.floor(panel?.clientWidth || (main ? (main.clientWidth - 80) : 0) || 620);
        }

        const dpr = window.devicePixelRatio || 1;
        const parentWidth = parent ? (parent.clientWidth || w) : w;
        const cssWidth = Math.min(Math.max(300, w), parentWidth || 620);
        const cssHeight = 420;

        canvas.style.width = `${cssWidth}px`;
        canvas.style.height = `${cssHeight}px`;
        canvas.width = Math.floor(cssWidth * dpr);
        canvas.height = Math.floor(cssHeight * dpr);

        if (ctx) ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    function getCssDimensions() {
        const cssWidth = canvas && canvas.style.width ? parseFloat(canvas.style.width) : (canvas ? canvas.width : 620);
        const cssHeight = canvas && canvas.style.height ? parseFloat(canvas.style.height) : (canvas ? canvas.height : 420);
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
            pulsePhase = (pulsePhase + 0.045) % (Math.PI * 2);
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
        return currentViewMode === 'wfg'
            ? currentGraph.nodes.filter(n => n.type === 'process')
            : currentGraph.nodes;
    }

    function getActiveEdges() {
        if (!currentGraph) return [];
        return currentViewMode === 'wfg'
            ? (currentGraph.wfg_edges || [])
            : (currentGraph.rag_edges || currentGraph.edges || []);
    }

    function getNodeMetrics(nodeCount) {
        const compact = nodeCount >= 8;
        return {
            procRadius: compact ? 28 : 34,
            resW: compact ? 80 : 94,
            resH: compact ? 38 : 44,
        };
    }

    /**
     * Smart topology-aware graph layout:
     *   - 2-process WFG: crisp horizontal pair (curved opposing arrows above & below)
     *   - 2-process + 2-resource RAG: textbook clockwise diamond
     *   - Cycle + branch (e.g. Multi-Resource A->B->C->A + D->B): places main cycle in a clean
     *     ring on the left and non-cycle blocked nodes cleanly on the right (zero crossing edges)
     *   - Ring topologies (e.g. Dining Philosophers 5+5): traces edges in true cycle order so
     *     every arrow follows the perimeter with zero crossings
     */
    function layoutGraphNodes() {
        if (!canvas || !currentGraph || !currentGraph.nodes) return;
        const { w, h } = getCssDimensions();
        const nodes = getActiveNodes();
        if (nodes.length === 0) return;

        const procs = nodes.filter(n => n.type === 'process');
        const res = nodes.filter(n => n.type === 'resource');
        const nodeMap = new Map(nodes.map(n => [n.id, n]));
        const involvedProcs = new Set(currentResult?.involved_processes || []);
        const involvedRes = new Set(currentResult?.involved_resources || []);

        // 1. WFG Layout
        if (currentViewMode === 'wfg') {
            if (procs.length === 2) {
                const spread = Math.min(130, w * 0.25);
                procs[0].x = w / 2 - spread;
                procs[0].y = h / 2;
                procs[1].x = w / 2 + spread;
                procs[1].y = h / 2;
                return;
            }

            const cycleProcs = procs.filter(p => involvedProcs.has(p.id));
            const branchProcs = procs.filter(p => !involvedProcs.has(p.id));

            // Order cycle processes along actual DFS cycle if available
            const orderedCycle = orderByCycle(cycleProcs, currentResult?.cycles?.[0]);

            if (orderedCycle.length >= 2 && branchProcs.length > 0) {
                const cx = w * 0.37;
                const cy = h * 0.5;
                const r = Math.min(w * 0.23, h * 0.34);
                for (let i = 0; i < orderedCycle.length; i++) {
                    const angle = (i / orderedCycle.length) * Math.PI * 2 - Math.PI / 2;
                    orderedCycle[i].x = cx + Math.cos(angle) * r;
                    orderedCycle[i].y = cy + Math.sin(angle) * r;
                }
                const bx = w * 0.80;
                for (let i = 0; i < branchProcs.length; i++) {
                    branchProcs[i].x = bx;
                    branchProcs[i].y = branchProcs.length === 1
                        ? cy
                        : (h * 0.26 + (i / (branchProcs.length - 1)) * h * 0.48);
                }
                return;
            }

            // Regular polygon in cycle order
            const orderedAll = orderedCycle.length === procs.length ? orderedCycle : procs;
            const r = Math.min(w * 0.33, h * 0.35);
            for (let i = 0; i < orderedAll.length; i++) {
                const angle = (i / orderedAll.length) * Math.PI * 2 - Math.PI / 2;
                orderedAll[i].x = w / 2 + Math.cos(angle) * r;
                orderedAll[i].y = h / 2 + Math.sin(angle) * r;
            }
            return;
        }

        // 2. RAG Layout
        // Special textbook diamond for 2 processes + 2 resources (Circular Deadlock)
        if (procs.length === 2 && res.length === 2) {
            const dx = Math.min(165, w * 0.29);
            const dy = Math.min(125, h * 0.31);
            // Place P0 Left, P1 Right; place resource held by P0 at Top and resource held by P1 at Bottom
            const p0 = procs[0];
            const p1 = procs[1];
            const heldByP0 = Object.keys(p0.allocated || {}).find(k => (p0.allocated[k] || 0) > 0);
            const rTop = (heldByP0 && nodeMap.get(heldByP0)) || res[0];
            const rBot = res.find(r => r.id !== rTop.id) || res[1];

            p0.x = w / 2 - dx; p0.y = h / 2;
            p1.x = w / 2 + dx; p1.y = h / 2;
            rTop.x = w / 2;    rTop.y = h / 2 - dy;
            rBot.x = w / 2;    rBot.y = h / 2 + dy;
            return;
        }

        // Cycle + Branch RAG (e.g. Multi-Resource Deadlock: 3 cycle procs + 3 cycle locks + Worker D + GPU Lock)
        const cycleProcs = procs.filter(p => involvedProcs.has(p.id));
        const branchProcs = procs.filter(p => !involvedProcs.has(p.id));

        if (cycleProcs.length >= 2 && branchProcs.length > 0) {
            const orderedCycleP = orderByCycle(cycleProcs, currentResult?.cycles?.[0]);
            const placedRes = new Set();
            const ringNodes = [];

            for (const p of orderedCycleP) {
                const heldId = Object.keys(p.allocated || {}).find(k => (p.allocated[k] || 0) > 0 && nodeMap.has(k) && !placedRes.has(k));
                if (heldId) {
                    ringNodes.push(nodeMap.get(heldId));
                    placedRes.add(heldId);
                }
                ringNodes.push(p);
            }
            // Add any remaining involved resources not yet placed
            for (const r of res) {
                if (involvedRes.has(r.id) && !placedRes.has(r.id)) {
                    ringNodes.push(r);
                    placedRes.add(r.id);
                }
            }

            const cx = w * 0.35;
            const cy = h * 0.50;
            const rx = Math.min(w * 0.25, 165);
            const ry = Math.min(h * 0.37, 150);
            for (let i = 0; i < ringNodes.length; i++) {
                const angle = (i / ringNodes.length) * Math.PI * 2 - Math.PI / 2;
                ringNodes[i].x = cx + Math.cos(angle) * rx;
                ringNodes[i].y = cy + Math.sin(angle) * ry;
            }

            // Place non-cycle processes & their private locks neatly on the right column
            const bx = w * 0.81;
            const branchRes = res.filter(r => !placedRes.has(r.id));
            for (let i = 0; i < branchProcs.length; i++) {
                const bp = branchProcs[i];
                bp.x = bx;
                bp.y = branchRes.length > 0 ? h * 0.36 : h * 0.50;
                const heldPrivate = Object.keys(bp.allocated || {}).find(k => (bp.allocated[k] || 0) > 0 && !placedRes.has(k) && nodeMap.has(k));
                if (heldPrivate) {
                    const pr = nodeMap.get(heldPrivate);
                    pr.x = bx;
                    pr.y = h * 0.68;
                    placedRes.add(heldPrivate);
                }
            }
            // Fallback for any remaining unplaced resource
            let extraY = h * 0.84;
            for (const r of res) {
                if (!placedRes.has(r.id)) {
                    r.x = bx;
                    r.y = extraY;
                    extraY -= 48;
                }
            }
            return;
        }

        // General N-process / N-resource ring (e.g. Dining Philosophers): trace perimeter in true cycle order!
        const orderedProcs = orderByCycle(procs, currentResult?.cycles?.[0]);
        const placedRes = new Set();
        const ring = [];
        for (const p of orderedProcs) {
            const heldId = Object.keys(p.allocated || {}).find(k => (p.allocated[k] || 0) > 0 && nodeMap.has(k) && !placedRes.has(k));
            if (heldId) {
                ring.push(nodeMap.get(heldId));
                placedRes.add(heldId);
            }
            ring.push(p);
        }
        for (const r of res) {
            if (!placedRes.has(r.id)) ring.push(r);
        }

        const rx = Math.min(w * 0.39, 235);
        const ry = Math.min(h * 0.39, 162);
        for (let i = 0; i < ring.length; i++) {
            const angle = (i / ring.length) * Math.PI * 2 - Math.PI / 2;
            ring[i].x = w / 2 + Math.cos(angle) * rx;
            ring[i].y = h / 2 + Math.sin(angle) * ry;
        }
    }

    function orderByCycle(procs, cycleArr) {
        if (!cycleArr || cycleArr.length < 2) return procs;
        const map = new Map(procs.map(p => [p.id, p]));
        const seen = new Set();
        const ordered = [];
        for (const id of cycleArr) {
            if (map.has(id) && !seen.has(id)) {
                ordered.push(map.get(id));
                seen.add(id);
            }
        }
        for (const p of procs) {
            if (!seen.has(p.id)) ordered.push(p);
        }
        return ordered;
    }

    function drawBackgroundGrid(w, h) {
        ctx.save();
        ctx.fillStyle = '#FFFBF5';
        ctx.fillRect(0, 0, w, h);
        ctx.fillStyle = '#E6DAC9';
        const step = 22;
        for (let x = step; x < w; x += step) {
            for (let y = step; y < h; y += step) {
                ctx.fillRect(x, y, 1.5, 1.5);
            }
        }
        ctx.restore();
    }

    function clipToNodeBorder(node, targetX, targetY, metrics, margin) {
        const dx = targetX - node.x;
        const dy = targetY - node.y;
        const dist = Math.hypot(dx, dy);
        if (dist < 1e-4) return { x: node.x, y: node.y };
        const ux = dx / dist;
        const uy = dy / dist;

        if (node.type === 'process') {
            const r = metrics.procRadius + margin;
            return { x: node.x + ux * r, y: node.y + uy * r };
        }
        // Resource rounded rectangle half-extents
        const hw = metrics.resW / 2 + margin;
        const hh = metrics.resH / 2 + margin;
        const tx = Math.abs(ux) > 1e-4 ? hw / Math.abs(ux) : Infinity;
        const ty = Math.abs(uy) > 1e-4 ? hh / Math.abs(uy) : Infinity;
        const t = Math.min(tx, ty);
        return { x: node.x + ux * t, y: node.y + uy * t };
    }

    function drawGraph() {
        if (!canvas || !ctx) return;
        const { w, h } = getCssDimensions();
        drawBackgroundGrid(w, h);

        const nodes = getActiveNodes();
        const edges = getActiveEdges();
        if (nodes.length === 0) return;

        const metrics = getNodeMetrics(nodes.length);
        const nodeMap = new Map(nodes.map(n => [n.id, n]));
        const involvedProcs = new Set(currentResult?.involved_processes || []);
        const involvedRes = new Set(currentResult?.involved_resources || []);
        const victimId = currentResult?.recommended_victim;

        const cycleEdgePairs = new Set();
        for (const cyc of (currentResult?.cycles || [])) {
            for (let i = 0; i < cyc.length - 1; i++) {
                cycleEdgePairs.add(`${cyc[i]}->${cyc[i + 1]}`);
            }
        }

        // Detect opposing bidirectional pairs so we curve them symmetrically apart
        const pairSet = new Set(edges.map(e => `${e.from}->${e.to}`));

        // 1. Draw Edges
        for (const edge of edges) {
            const fromNode = nodeMap.get(edge.from);
            const toNode = nodeMap.get(edge.to);
            if (!fromNode || !toNode) continue;

            let isCycleEdge = false;
            if (currentViewMode === 'wfg') {
                isCycleEdge = cycleEdgePairs.has(`${edge.from}->${edge.to}`);
            } else {
                const procId = fromNode.type === 'process' ? fromNode.id : toNode.id;
                const resId = fromNode.type === 'resource' ? fromNode.id : toNode.id;
                isCycleEdge = involvedProcs.has(procId) && involvedRes.has(resId);
            }

            const hasReverse = pairSet.has(`${edge.to}->${edge.from}`);
            const curveAmount = hasReverse ? 42 : (currentViewMode === 'wfg' && nodes.length >= 4 ? 14 : 0);

            const edgeLabel = currentViewMode === 'wfg'
                ? cleanName(edge.resource || '')
                : (edge.type === 'request' ? 'Waits' : 'Holds');

            drawSmartArrow(
                fromNode,
                toNode,
                metrics,
                edge.type === 'request' ? 'dashed' : 'solid',
                isCycleEdge,
                edgeLabel,
                curveAmount
            );
        }

        // 2. Draw Nodes
        for (const node of nodes) {
            const isProc = node.type === 'process';
            const isDeadlocked = isProc ? involvedProcs.has(node.id) : involvedRes.has(node.id);
            const isVictim = node.id === victimId;
            const isHovered = node.id === hoveredNodeId;
            drawNode(node, metrics, isProc, isDeadlocked, isVictim, isHovered);
        }
    }

    function drawSmartArrow(fromNode, toNode, metrics, style, isCycleEdge, label, curveOffset) {
        ctx.save();
        const dx = toNode.x - fromNode.x;
        const dy = toNode.y - fromNode.y;
        const dist = Math.hypot(dx, dy);
        if (dist < 1) {
            ctx.restore();
            return;
        }

        // Normal vector for optional curve
        const nx = -dy / dist;
        const ny = dx / dist;
        const midX = (fromNode.x + toNode.x) / 2 + nx * curveOffset;
        const midY = (fromNode.y + toNode.y) / 2 + ny * curveOffset;

        // Clip start & end cleanly to exact node boundaries using tangent towards control point
        const startPt = clipToNodeBorder(fromNode, curveOffset ? midX : toNode.x, curveOffset ? midY : toNode.y, metrics, 3);
        const endPt = clipToNodeBorder(toNode, curveOffset ? midX : fromNode.x, curveOffset ? midY : fromNode.y, metrics, 5);

        const strokeColor = isCycleEdge
            ? '#C85D5D'
            : (style === 'dashed' ? '#B98532' : '#298F93');

        ctx.strokeStyle = strokeColor;
        ctx.lineWidth = isCycleEdge ? 2.4 : 1.8;
        ctx.setLineDash(style === 'dashed' ? [6, 4] : []);

        ctx.beginPath();
        ctx.moveTo(startPt.x, startPt.y);
        if (curveOffset !== 0) {
            ctx.quadraticCurveTo(midX, midY, endPt.x, endPt.y);
        } else {
            ctx.lineTo(endPt.x, endPt.y);
        }
        ctx.stroke();

        // Arrowhead aligned with exact end tangent
        const tangentX = curveOffset !== 0 ? (endPt.x - midX) : (endPt.x - startPt.x);
        const tangentY = curveOffset !== 0 ? (endPt.y - midY) : (endPt.y - startPt.y);
        const angle = Math.atan2(tangentY, tangentX);
        const headLen = 10;

        ctx.setLineDash([]);
        ctx.fillStyle = strokeColor;
        ctx.beginPath();
        ctx.moveTo(endPt.x, endPt.y);
        ctx.lineTo(endPt.x - headLen * Math.cos(angle - Math.PI / 6), endPt.y - headLen * Math.sin(angle - Math.PI / 6));
        ctx.lineTo(endPt.x - headLen * Math.cos(angle + Math.PI / 6), endPt.y - headLen * Math.sin(angle + Math.PI / 6));
        ctx.closePath();
        ctx.fill();

        // Pill-backed readable label at curve midpoint (t = 0.5)
        if (label && dist > 62) {
            const lx = curveOffset !== 0
                ? 0.25 * startPt.x + 0.5 * midX + 0.25 * endPt.x
                : (startPt.x + endPt.x) / 2;
            const ly = curveOffset !== 0
                ? 0.25 * startPt.y + 0.5 * midY + 0.25 * endPt.y
                : (startPt.y + endPt.y) / 2;

            ctx.font = '600 10px "Inter", sans-serif';
            const textW = ctx.measureText(label).width;
            const padX = 6;
            const pillW = textW + padX * 2;
            const pillH = 18;

            ctx.fillStyle = isCycleEdge ? '#FDF1F1' : '#FFFCF7';
            ctx.strokeStyle = isCycleEdge ? 'rgba(200, 93, 93, 0.45)' : 'rgba(180, 165, 150, 0.55)';
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.roundRect(lx - pillW / 2, ly - pillH / 2, pillW, pillH, 9);
            ctx.fill();
            ctx.stroke();

            ctx.fillStyle = isCycleEdge ? '#7E2B2B' : (style === 'dashed' ? '#6E490D' : '#215940');
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(label, lx, ly + 0.5);
        }

        ctx.restore();
    }

    function drawNode(node, metrics, isProc, isDeadlocked, isVictim, isHovered) {
        const x = node.x;
        const y = node.y;
        ctx.save();

        // Subtle elevation shadow
        ctx.shadowColor = isHovered ? 'rgba(50, 48, 53, 0.20)' : 'rgba(50, 48, 53, 0.10)';
        ctx.shadowBlur = isHovered ? 12 : 6;
        ctx.shadowOffsetY = 2;

        if (isVictim) {
            ctx.strokeStyle = '#B98532';
            ctx.lineWidth = Math.sin(pulsePhase) * 0.7 + 3.0;
        } else if (isDeadlocked) {
            ctx.strokeStyle = '#C85D5D';
            ctx.lineWidth = Math.sin(pulsePhase) * 0.5 + 2.5;
        } else {
            ctx.strokeStyle = isProc ? '#52748D' : '#298F93';
            ctx.lineWidth = isHovered ? 2.6 : 2.0;
        }

        if (isProc) {
            const r = metrics.procRadius;
            ctx.beginPath();
            ctx.arc(x, y, r, 0, Math.PI * 2);
            ctx.fillStyle = isVictim
                ? '#FEF5E6'
                : (isDeadlocked ? '#FCECEC' : '#F0F6FA');
            ctx.fill();
            ctx.stroke();

            ctx.shadowColor = 'transparent';
            ctx.shadowBlur = 0;
            ctx.shadowOffsetY = 0;

            // Main Name
            ctx.fillStyle = '#323035';
            ctx.font = `700 ${r >= 32 ? 11 : 10}px "Inter", sans-serif`;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(shortCanvasName(node.label || node.name || node.id), x, y - 5);

            // Second line: Priority
            ctx.fillStyle = '#625D62';
            ctx.font = '500 9px "JetBrains Mono", monospace';
            const pri = node.recovery_priority !== undefined ? `Pri ${node.recovery_priority}` : '';
            ctx.fillText(pri, x, y + 9);

            // Top Ribbon Badge for Victim or Blocked (non-cycle waiter)
            const isBlockedOnly = !isDeadlocked && Object.values(node.requesting || {}).some(v => v > 0);
            if (isVictim || isBlockedOnly) {
                const badgeText = isVictim ? 'STOP VICTIM' : 'BLOCKED';
                ctx.font = '700 8px "Inter", sans-serif';
                const bw = ctx.measureText(badgeText).width + 10;
                const bh = 15;
                const bx = x - bw / 2;
                const by = y - r - bh / 2 - 2;
                ctx.fillStyle = isVictim ? '#B98532' : '#52748D';
                ctx.beginPath();
                ctx.roundRect(bx, by, bw, bh, 7);
                ctx.fill();
                ctx.fillStyle = '#FFFFFF';
                ctx.fillText(badgeText, x, by + bh / 2 + 0.5);
            }
        } else {
            const rw = metrics.resW;
            const rh = metrics.resH;
            ctx.beginPath();
            ctx.roundRect(x - rw / 2, y - rh / 2, rw, rh, 8);
            ctx.fillStyle = isDeadlocked ? '#FDF0F0' : '#EBF7F6';
            ctx.fill();
            ctx.stroke();

            ctx.shadowColor = 'transparent';
            ctx.shadowBlur = 0;
            ctx.shadowOffsetY = 0;

            ctx.fillStyle = isDeadlocked ? '#7E2B2B' : '#195F63';
            ctx.font = `700 ${rw >= 88 ? 10.5 : 9.5}px "Inter", sans-serif`;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(cleanName(node.label || node.name || node.id), x, y - 6);

            const total = node.total !== undefined ? node.total : 1;
            const avail = node.available !== undefined ? node.available : 0;
            const statusTxt = total === 1 ? (avail > 0 ? 'Free' : 'In Use') : `${avail}/${total} free`;
            ctx.fillStyle = avail > 0 ? '#215940' : '#7E2B2B';
            ctx.font = '600 9px "Inter", sans-serif';
            ctx.fillText(statusTxt, x, y + 9);
        }

        ctx.restore();
    }

    // ── Interactive Node Hover Tooltip ───────────────────────────────────────

    function handleCanvasMouseMove(e) {
        if (!canvas) return;
        const rect = canvas.getBoundingClientRect();
        const mx = e.clientX - rect.left;
        const my = e.clientY - rect.top;
        const nodes = getActiveNodes();
        const metrics = getNodeMetrics(nodes.length);

        let hit = null;
        for (const n of nodes) {
            if (n.x === undefined || n.y === undefined) continue;
            if (n.type === 'process') {
                if (Math.hypot(mx - n.x, my - n.y) <= metrics.procRadius + 4) {
                    hit = n;
                    break;
                }
            } else {
                if (Math.abs(mx - n.x) <= metrics.resW / 2 + 4 && Math.abs(my - n.y) <= metrics.resH / 2 + 4) {
                    hit = n;
                    break;
                }
            }
        }

        if (hit) {
            canvas.style.cursor = 'pointer';
            if (hoveredNodeId !== hit.id) {
                hoveredNodeId = hit.id;
                drawGraph();
            }
            showNodeTooltip(hit, mx, my);
        } else {
            canvas.style.cursor = 'default';
            if (hoveredNodeId !== null) {
                hoveredNodeId = null;
                drawGraph();
            }
            hideTooltip();
        }
    }

    function showNodeTooltip(node, mx, my) {
        if (!tooltipEl) return;
        const involved = new Set(currentResult?.involved_processes || []);
        let html = '';
        if (node.type === 'process') {
            const holds = Object.entries(node.allocated || {}).filter(([, v]) => v > 0).map(([k]) => cleanName(k)).join(', ') || 'None';
            const waits = Object.entries(node.requesting || {}).filter(([, v]) => v > 0).map(([k]) => cleanName(k)).join(', ') || 'None';
            const roleNote = node.id === currentResult?.recommended_victim
                ? 'Recommended victim (lowest priority in cycle)'
                : (involved.has(node.id) ? 'Part of deadlock cycle' : 'Not in deadlock cycle');
            html = `
                <div style="font-weight:700;margin-bottom:3px;">${escapeHtml(cleanName(node.name))} ${node.os_pid ? `(PID ${node.os_pid})` : ''}</div>
                <div>Priority: <strong>${node.recovery_priority ?? node.priority}</strong></div>
                <div>Holding: <strong>${escapeHtml(holds)}</strong></div>
                <div>Waiting for: <strong>${escapeHtml(waits)}</strong></div>
                <div style="margin-top:3px;color:#FEF4B7;">${escapeHtml(roleNote)}</div>
            `;
        } else {
            const total = node.total ?? 1;
            const avail = node.available ?? 0;
            html = `
                <div style="font-weight:700;margin-bottom:3px;">Lock: ${escapeHtml(cleanName(node.name))}</div>
                <div>Status: <strong>${avail > 0 ? 'Free' : 'In Use'}</strong> (${avail}/${total} available)</div>
            `;
        }

        tooltipEl.innerHTML = html;
        tooltipEl.style.display = 'block';
        const { w, h } = getCssDimensions();
        const tx = mx + 14 + 220 > w ? Math.max(8, mx - 225) : mx + 14;
        const ty = my + 14 + 110 > h ? Math.max(8, my - 110) : my + 14;
        tooltipEl.style.left = `${tx}px`;
        tooltipEl.style.top = `${ty}px`;
    }

    function hideTooltip() {
        if (tooltipEl) tooltipEl.style.display = 'none';
    }

    function renderWfgResults(res, snap) {
        const title = document.getElementById('wfgStatusTitle');
        const detail = document.getElementById('wfgStatusDetail');
        const box = document.getElementById('wfgResultBox');
        if (!res || !box) return;

        if (res.has_deadlock) {
            box.style.borderColor = 'var(--status-danger)';
            box.style.background = 'var(--status-danger-bg)';
            const cyclePath = res.cycles && res.cycles[0]
                ? res.cycles[0].map(id => cleanName(id)).join(' → ')
                : '';
            const victimName = res.recommended_victim ? cleanName(res.recommended_victim) : 'None';
            const totalProcs = (snap.workers || []).length;
            const cycleCount = (res.involved_processes || []).length;
            const outsideNote = totalProcs > cycleCount
                ? ` (${totalProcs - cycleCount} other process is blocked outside the cycle and will unblock automatically once the cycle is broken.)`
                : '';

            title.innerHTML = `<span style="color: var(--status-danger-text); font-weight: 700;">Circular Wait Deadlock Found</span>`;
            detail.innerHTML = `
                ${cyclePath ? `<div style="margin-bottom: 4px; font-weight: 600; color: var(--status-danger-text);">Cycle: ${escapeHtml(cyclePath)}</div>` : ''}
                <div style="color: var(--text-primary);">
                    Suggested action: Stop <strong>${escapeHtml(victimName)}</strong> (lowest priority inside the cycle).${escapeHtml(outsideNote)}
                </div>
            `;
        } else {
            box.style.borderColor = 'var(--status-success)';
            box.style.background = 'var(--status-success-bg)';
            title.innerHTML = `<span style="color: var(--status-success-text); font-weight: 700;">No Deadlock Cycle</span>`;
            detail.textContent = snap.selective_recovery_succeeded
                ? 'The circular wait has been broken and surviving workers finished cleanly.'
                : 'No circular blocking exists right now.';
        }
    }

    // ── LIVE BANKER'S ALGORITHM Renderer ─────────────────────────────────────

    function renderLiveBankerFromSnapshot(snap) {
        const bk = snap.banker_analysis;
        const bkBadge = document.getElementById('bankerSnapshotBadge');
        const availChips = document.getElementById('bankerAvailableChips');
        const thead = document.getElementById('bankerTableHead');
        const tbody = document.getElementById('bankerTableBody');
        const availVec = document.getElementById('bankerAvailableVec');
        const safeSeqEl = document.getElementById('bankerSafeSeqInline');
        const resultBox = document.getElementById('bankerResultBox');

        if (bkBadge) bkBadge.textContent = `v${snap.snapshot_version}`;
        if (!bk || !tbody) return;

        const resources = bk.resource_names || [];
        if (thead) {
            const shortRes = resources.map(r => r.replace(/^Chopstick_/, 'C').replace(/^Mutex_/, 'M').replace(/^Lock_/, 'L').replace(/^Shared_/, '').replace(/_Lock$/, '')).join(' ');
            const hdrSuffix = shortRes ? ` <span style="font-weight:400;color:var(--text-muted);">(${escapeHtml(shortRes)})</span>` : '';
            thead.innerHTML = `
                <tr>
                    <th>Process</th>
                    <th>Holding${hdrSuffix}</th>
                    <th>Max Need${hdrSuffix}</th>
                    <th>Still Needs${hdrSuffix}</th>
                    <th>Can Finish?</th>
                </tr>
            `;
        }

        if (!bk.data_available) {
            if (availChips) availChips.innerHTML = '<span class="dl-chip dl-chip-muted">Not Available</span>';
            if (availVec) availVec.textContent = 'NOT AVAILABLE';
            if (safeSeqEl) safeSeqEl.textContent = 'Not Available';
            tbody.innerHTML = `
                <tr>
                    <td colspan="5" style="text-align: center; color: var(--text-muted); padding: var(--space-md);">
                        NOT AVAILABLE — live claim data is not available for this state.
                    </td>
                </tr>
            `;
            if (resultBox) {
                resultBox.style.display = 'block';
                resultBox.className = 'banker-result';
                resultBox.innerHTML = `<strong>Banker's Result: NOT AVAILABLE</strong>`;
            }
            return;
        }

        const availArr = bk.available || [];
        if (availChips) {
            availChips.innerHTML = resources.map((r, i) => {
                const v = availArr[i] ?? 0;
                return `<span class="dl-chip ${v > 0 ? 'dl-chip-avail' : 'dl-chip-zero'}">${escapeHtml(cleanName(r))}: ${v}</span>`;
            }).join('');
        }
        if (availVec) availVec.textContent = `[ ${availArr.join(', ')} ]`;

        const cleanSeq = (bk.safe_sequence || []).map(p => cleanName(p));
        if (safeSeqEl) {
            safeSeqEl.textContent = bk.is_safe
                ? (cleanSeq.length ? cleanSeq.join(' → ') : 'All Done')
                : 'None (Unsafe)';
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
                        ${snap.status === 'resolved' || snap.status === 'completed'
                            ? 'All remaining processes have released their locks and finished.'
                            : 'Waiting for workers to declare resource claims...'}
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
                        <td style="font-weight: 700; color: var(--text-primary); text-align: left;">${escapeHtml(cleanName(procs[i]))}</td>
                        <td>[ ${escapeHtml(aStr)} ]</td>
                        <td>[ ${escapeHtml(mStr)} ]</td>
                        <td style="color: var(--status-info-text); font-weight: 600;">[ ${escapeHtml(nStr)} ]</td>
                        <td><span class="priority-badge ${finVal ? 'normal' : 'high'}">${finVal ? 'YES' : 'NO'}</span></td>
                    </tr>
                `;
            }
            tbody.innerHTML = html;
        }

        if (resultBox) {
            resultBox.style.display = 'block';
            if (bk.is_safe) {
                resultBox.className = 'banker-result safe';
                resultBox.innerHTML = `
                    <div style="font-size: 0.9rem; font-weight: 700; margin-bottom: 2px;">SAFE — System Can Finish Cleanly</div>
                    <div style="font-size: 0.8rem;">
                        ${cleanSeq.length ? `Safe order: <strong>${escapeHtml(cleanSeq.join(' → '))}</strong>` : 'All remaining workers finished and released their locks.'}
                    </div>
                `;
            } else {
                resultBox.className = 'banker-result unsafe';
                const heading = bk.actual_deadlock_exists
                    ? 'UNSAFE — Deadlock Active'
                    : 'UNSAFE — Deadlock Risk (No Active Cycle Yet)';
                const expl = bk.actual_deadlock_exists
                    ? 'No process can satisfy its remaining lock needs with the available locks. Stop the recommended worker to recover.'
                    : 'No guaranteed safe completion sequence exists for the declared maximum needs, though no active circular wait is blocking yet.';
                resultBox.innerHTML = `
                    <div style="font-size: 0.9rem; font-weight: 700; margin-bottom: 2px;">${heading}</div>
                    <div style="font-size: 0.8rem;">${expl}</div>
                `;
            }
        }
    }

    // ── Explicit User-Approved Selective Recovery & Abort ────────────────────

    async function approveLiveRecovery() {
        if (!currentLiveRun || !currentLiveRun.is_active_run || !currentLiveRun.deadlock_detection?.recommended_victim_details) {
            App.showToast('No active deadlocked worker available to stop.', 'warning');
            return;
        }
        const runId = currentLiveRun.run_id;
        const victim = currentLiveRun.deadlock_detection.recommended_victim_details;
        const victimName = cleanName(victim.name);

        const ok = confirm(
            `Stop ${victimName} (PID ${victim.pid}) to break the deadlock?\n\n` +
            `• Scenario: ${currentLiveRun.workload_name}\n` +
            `• Priority: ${victim.recovery_priority} (lowest in cycle)\n` +
            `• Only ${victimName} will be stopped; the supervisor and sandbox remain active.`
        );
        if (!ok) return;

        try {
            const chRes = await fetch(`/api/deadlock/live/runs/${runId}/recovery-challenge`);
            if (!chRes.ok) {
                const err = await chRes.json();
                App.showToast(`Recovery check failed: ${err.detail || 'Error'}`, 'error');
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
                    operator_note: 'Approved from dashboard',
                }),
            });

            if (recRes.ok) {
                const data = await recRes.json();
                if (data.run) applyAuthoritativeSnapshot(data.run);
                App.showToast(
                    data.deadlock_resolved
                        ? `Deadlock resolved! Stopped ${victimName} and remaining workers recovered.`
                        : `Stopped ${victimName}; re-checking dependency graph.`,
                    data.deadlock_resolved ? 'success' : 'warning'
                );
            } else {
                const err = await recRes.json();
                App.showToast(`Recovery blocked: ${err.detail || 'Error'}`, 'error');
            }
        } catch (e) {
            App.showToast(`Recovery request failed: ${e.message}`, 'error');
        }
    }

    async function abortLiveRun() {
        if (!currentLiveRun || !currentLiveRun.is_active_run) return;
        const runId = currentLiveRun.run_id;
        if (!confirm(`Stop the entire ${currentLiveRun.workload_name} sandbox?`)) return;
        try {
            const res = await fetch(`/api/deadlock/live/runs/${runId}`, { method: 'DELETE' });
            if (res.ok) {
                const abortedSnap = await res.json();
                applyAuthoritativeSnapshot(abortedSnap);
                App.showToast(`Stopped ${currentLiveRun.workload_name} and cleared the live graph.`, 'info');
            }
        } catch (e) {
            App.showToast(`Abort failed: ${e.message}`, 'error');
        }
    }

    // ── Isolated Offline Practice Scenarios ──────────────────────────────────

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
            const cycleText = r.cycles && r.cycles.length
                ? r.cycles.map(c => c.map(cleanName).join(' → ')).join(' | ')
                : 'None';
            out.innerHTML = `
                <div><strong>Result:</strong> ${r.has_deadlock ? 'Deadlock Found' : 'No Deadlock'}</div>
                <div><strong>Cycle:</strong> ${escapeHtml(cycleText)}</div>
                <div><strong>Suggested Victim:</strong> ${escapeHtml(r.recommended_victim ? cleanName(r.recommended_victim) : 'None')}</div>
            `;
        } catch (e) {
            out.textContent = `Could not load example: ${e.message}`;
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
                <div><strong>State:</strong> ${r.is_safe ? 'SAFE' : 'UNSAFE'} · <strong>Available:</strong> [${s.available.join(', ')}]</div>
                <div><strong>Safe Order:</strong> ${r.safe_sequence.length ? escapeHtml(r.safe_sequence.join(' → ')) : 'None'}</div>
            `;
        } catch (e) {
            out.textContent = `Could not load example: ${e.message}`;
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
        setViewMode,
        toggleViewMode,
        updateFromTelemetry,
        handleLiveSnapshotPush,
        onTabActivated,
        onTabDeactivated
    };
})();
