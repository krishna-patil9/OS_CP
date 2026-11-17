/**
 * workloads.js — Synthetic Workload Suite Controller (Safe & Controlled)
 * 
 * Manages:
 *   - Controlled launching of synthetic workloads: CPU Hog, Memory Hog, Mutex Deadlock Inversion, and Sandboxed App
 *   - Enforcing execution mode check (Testing Mode required)
 *   - Enforcing safe default arguments to prevent personal laptop overheating
 *   - Safe polling only when the Workloads tab is active
 */

const Workloads = (() => {
    let refreshTimer = null;

    function init() {
        // Button bindings
        const btnCpu = document.getElementById('btnLaunchCpuHog');
        const btnCpuCapped = document.getElementById('btnLaunchCpuHogCapped');
        const btnMem = document.getElementById('btnLaunchMemHog');
        const btnMemCapped = document.getElementById('btnLaunchMemHogCapped');
        const btnDeadlock = document.getElementById('btnLaunchDeadlockDemo');
        const btnSandbox = document.getElementById('btnLaunchSandboxedApp');
        const btnRefresh = document.getElementById('btnRefreshWorkloads');

        if (btnCpu) btnCpu.addEventListener('click', () => launchCpuHog(false));
        if (btnCpuCapped) btnCpuCapped.addEventListener('click', () => launchCpuHog(true));
        if (btnMem) btnMem.addEventListener('click', () => launchMemHog(false));
        if (btnMemCapped) btnMemCapped.addEventListener('click', () => launchMemHog(true));
        if (btnDeadlock) btnDeadlock.addEventListener('click', launchDeadlockDemo);
        if (btnSandbox) btnSandbox.addEventListener('click', launchSandboxedApp);
        if (btnRefresh) btnRefresh.addEventListener('click', fetchWorkloads);
    }

    function isTabVisible() {
        const panel = document.getElementById('panelWorkloads');
        return panel && panel.classList.contains('active');
    }

    function onTabActivated() {
        fetchWorkloads();
        if (!refreshTimer) {
            refreshTimer = setInterval(fetchWorkloads, 3000);
        }
    }

    function onTabDeactivated() {
        if (refreshTimer) {
            clearInterval(refreshTimer);
            refreshTimer = null;
        }
    }

    function checkTestingMode() {
        if (typeof App !== 'undefined' && App.getExecutionMode && App.getExecutionMode() !== 'testing') {
            App.showToast('Normal Mode Active: Stress tests are locked. Click "Testing Mode" in the header to unlock.', 'warning');
            return false;
        }
        return true;
    }

    async function launchCpuHog(capped = true) {
        if (!checkTestingMode()) return;

        const rawThreads = parseInt(document.getElementById('cpuHogThreads')?.value) || 1;
        const rawDuration = parseInt(document.getElementById('cpuHogDuration')?.value) || 15;

        // Enforce safe laptop bounds
        const threads = Math.min(2, Math.max(1, rawThreads));
        const duration = Math.min(30, Math.max(5, rawDuration));

        const modeDesc = capped ? 'inside a 20% CPU Rate Cap Job Object' : 'controlled (1-2 threads, duty-cycled)';
        if (!confirm(`Launch safe CPU Hog (${threads} thread(s), ${duration}s max duration, ${modeDesc})?`)) {
            return;
        }

        let sandboxName = null;
        if (capped) {
            sandboxName = 'cpu_hog_20pct_sandbox';
            await ensureSandboxExists(sandboxName, {
                cpu_rate_percent: 20,
                priority_class: 'BELOW_NORMAL_PRIORITY_CLASS'
            });
        }

        try {
            const res = await fetch('/api/workloads/launch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    workload_type: 'cpu_hog',
                    args: ['--threads', String(threads), '--duration', String(duration), '--intensity', capped ? '30' : '50'],
                    sandbox_name: sandboxName
                })
            });

            if (res.ok) {
                const data = await res.json();
                App.showToast(
                    `Launched CPU Hog (PID ${data.pid})${capped ? ' inside 20% Job Object cap' : ' (Controlled)'}`,
                    'info'
                );
                fetchWorkloads();
            } else {
                const err = await res.json();
                App.showToast(`Launch blocked: ${err.detail || 'Error'}`, 'error');
            }
        } catch (e) {
            App.showToast(`Error: ${e.message}`, 'error');
        }
    }

    async function launchMemHog(capped = true) {
        if (!checkTestingMode()) return;

        const rawMax = parseInt(document.getElementById('memHogMax')?.value) || 40;
        const rawChunk = parseInt(document.getElementById('memHogChunk')?.value) || 5;

        // Safe laptop bounds
        const maxMb = Math.min(60, Math.max(10, rawMax));
        const chunkMb = Math.min(10, Math.max(1, rawChunk));

        const modeDesc = capped ? 'inside a 50MB hard Job Object cap' : 'controlled memory test';
        if (!confirm(`Launch safe Memory Hog (Target: ${maxMb} MB, ${chunkMb} MB chunks, ${modeDesc})?`)) {
            return;
        }

        let sandboxName = null;
        if (capped) {
            sandboxName = 'mem_hog_50mb_sandbox';
            await ensureSandboxExists(sandboxName, {
                memory_limit_mb: 50,
                priority_class: 'NORMAL_PRIORITY_CLASS'
            });
        }

        try {
            const res = await fetch('/api/workloads/launch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    workload_type: 'memory_hog',
                    args: ['--max-mb', String(maxMb), '--chunk-mb', String(chunkMb), '--hold-duration', '15'],
                    sandbox_name: sandboxName
                })
            });

            if (res.ok) {
                const data = await res.json();
                App.showToast(
                    `Launched Memory Hog (PID ${data.pid})${capped ? ' inside 50MB hard Job Object cap' : ' (Controlled)'}`,
                    'info'
                );
                fetchWorkloads();
            } else {
                const err = await res.json();
                App.showToast(`Launch blocked: ${err.detail || 'Error'}`, 'error');
            }
        } catch (e) {
            App.showToast(`Error: ${e.message}`, 'error');
        }
    }

    async function launchDeadlockDemo() {
        if (!checkTestingMode()) return;

        if (!confirm('Launch synthetic Mutex Deadlock demonstration (2 threads, circular wait, 15s watchdog timeout)?')) {
            return;
        }

        try {
            const res = await fetch('/api/workloads/launch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    workload_type: 'deadlock_demo',
                    args: ['--workers', '2', '--delay', '0.5', '--timeout', '15']
                })
            });

            if (res.ok) {
                const data = await res.json();
                App.showToast(`Launched Deadlock Demo (PID ${data.pid}) with mutex order inversion!`, 'warning');
                fetchWorkloads();
            } else {
                const err = await res.json();
                App.showToast(`Launch blocked: ${err.detail || 'Error'}`, 'error');
            }
        } catch (e) {
            App.showToast(`Error: ${e.message}`, 'error');
        }
    }

    async function launchSandboxedApp() {
        if (!checkTestingMode()) return;

        if (!confirm('Launch Untrusted Sandboxed App inside a Windows NT Job Object (15% CPU cap, 50MB RAM limit, max 3 processes)?')) {
            return;
        }

        const sandboxName = 'untrusted_restricted_sandbox';
        await ensureSandboxExists(sandboxName, {
            cpu_rate_percent: 15,
            memory_limit_mb: 50,
            max_active_processes: 3,
            priority_class: 'IDLE_PRIORITY_CLASS'
        });

        try {
            const res = await fetch('/api/workloads/launch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    workload_type: 'sandboxed_app',
                    args: ['--threads', '1', '--memory-mb', '30', '--duration', '20'],
                    sandbox_name: sandboxName
                })
            });

            if (res.ok) {
                const data = await res.json();
                App.showToast(`Launched Sandboxed App (PID ${data.pid}) under Job Object isolation!`, 'success');
                fetchWorkloads();
            } else {
                const err = await res.json();
                App.showToast(`Launch blocked: ${err.detail || 'Error'}`, 'error');
            }
        } catch (e) {
            App.showToast(`Error: ${e.message}`, 'error');
        }
    }

    async function ensureSandboxExists(name, limits) {
        try {
            await fetch('/api/sandboxes', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: name,
                    ...limits,
                    kill_on_close: true
                })
            });
        } catch (e) {
            // Already exists or handled
        }
    }

    async function fetchWorkloads() {
        try {
            const res = await fetch('/api/workloads');
            if (res.ok) {
                const data = await res.json();
                renderWorkloadTable(data);
            }
        } catch (e) {
            // Silent error handling for background polling
        }
    }

    async function stopWorkload(workloadId) {
        try {
            const res = await fetch(`/api/workloads/${workloadId}`, { method: 'DELETE' });
            if (res.ok) {
                App.showToast(`Terminated workload ${workloadId}`, 'info');
                fetchWorkloads();
            } else {
                const err = await res.json();
                App.showToast(`Failed to stop workload: ${err.detail || 'Error'}`, 'error');
            }
        } catch (e) {
            App.showToast(`Error: ${e.message}`, 'error');
        }
    }

    function renderWorkloadTable(workloads) {
        const tbody = document.getElementById('workloadTableBody');
        if (!tbody) return;

        if (!workloads || workloads.length === 0) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="5" style="text-align: center; color: var(--text-muted); padding: var(--space-md);">
                        No synthetic workloads spawned yet. Click any launch button above.
                    </td>
                </tr>
            `;
            return;
        }

        let html = '';
        for (const w of workloads) {
            const isRunning = w.running;
            const statusBadge = isRunning
                ? '<span class="priority-badge normal">RUNNING</span>'
                : `<span class="priority-badge idle">EXITED (${w.return_code})</span>`;

            html += `
                <tr>
                    <td style="font-weight: 600; color: var(--accent-primary-hover);">${escapeHtml(w.workload_id)}</td>
                    <td class="pid-cell">${w.pid}</td>
                    <td>${statusBadge}</td>
                    <td style="font-family: var(--font-mono);">${w.return_code !== null ? w.return_code : '-'}</td>
                    <td>
                        <button class="btn btn-sm btn-danger" onclick="Workloads.stopWorkload('${escapeHtml(w.workload_id)}')">
                            Stop
                        </button>
                    </td>
                </tr>
            `;
        }

        tbody.innerHTML = html;
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
        fetchWorkloads,
        stopWorkload,
        onTabActivated,
        onTabDeactivated
    };
})();
