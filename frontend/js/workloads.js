/**
 * workloads.js — Synthetic Workload Suite Controller
 * 
 * Manages:
 *   - Launching synthetic workloads: CPU Hog, Memory Hog, Mutex Deadlock Inversion, and Sandboxed App
 *   - Automatic sandbox creation & assignment for capped runs
 *   - Active spawned workloads monitoring table & inline termination
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

        fetchWorkloads();
        refreshTimer = setInterval(fetchWorkloads, 2000);
    }

    async function launchCpuHog(capped = false) {
        const threads = document.getElementById('cpuHogThreads')?.value || '2';
        const duration = document.getElementById('cpuHogDuration')?.value || '60';

        let sandboxName = null;
        if (capped) {
            sandboxName = 'cpu_hog_20pct_sandbox';
            // Ensure sandbox exists
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
                    args: ['--threads', String(threads), '--duration', String(duration)],
                    sandbox_name: sandboxName
                })
            });

            if (res.ok) {
                const data = await res.json();
                App.showToast(
                    `Launched CPU Hog (PID ${data.pid})${capped ? ' inside 20% Job Object cap' : ' (Uncapped)'}`,
                    capped ? 'info' : 'warning'
                );
                fetchWorkloads();
            } else {
                const err = await res.json();
                App.showToast(`Launch failed: ${err.detail || 'Error'}`, 'error');
            }
        } catch (e) {
            App.showToast(`Error: ${e.message}`, 'error');
        }
    }

    async function launchMemHog(capped = false) {
        const maxMb = document.getElementById('memHogMax')?.value || '150';
        const chunkMb = document.getElementById('memHogChunk')?.value || '10';

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
                    args: ['--max-mb', String(maxMb), '--chunk-mb', String(chunkMb)],
                    sandbox_name: sandboxName
                })
            });

            if (res.ok) {
                const data = await res.json();
                App.showToast(
                    `Launched Memory Hog (PID ${data.pid})${capped ? ' inside 50MB hard Job Object cap' : ' (Uncapped)'}`,
                    capped ? 'info' : 'warning'
                );
                fetchWorkloads();
            } else {
                const err = await res.json();
                App.showToast(`Launch failed: ${err.detail || 'Error'}`, 'error');
            }
        } catch (e) {
            App.showToast(`Error: ${e.message}`, 'error');
        }
    }

    async function launchDeadlockDemo() {
        try {
            const res = await fetch('/api/workloads/launch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    workload_type: 'deadlock_demo',
                    args: ['--workers', '2', '--delay', '0.5']
                })
            });

            if (res.ok) {
                const data = await res.json();
                App.showToast(`Launched Deadlock Demo (PID ${data.pid}) with mutex order inversion!`, 'warning');
                fetchWorkloads();
            } else {
                const err = await res.json();
                App.showToast(`Launch failed: ${err.detail || 'Error'}`, 'error');
            }
        } catch (e) {
            App.showToast(`Error: ${e.message}`, 'error');
        }
    }

    async function launchSandboxedApp() {
        const sandboxName = 'untrusted_restricted_sandbox';
        await ensureSandboxExists(sandboxName, {
            cpu_rate_percent: 15,
            memory_limit_mb: 80,
            max_active_processes: 3,
            priority_class: 'IDLE_PRIORITY_CLASS'
        });

        try {
            const res = await fetch('/api/workloads/launch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    workload_type: 'sandboxed_app',
                    args: [],
                    sandbox_name: sandboxName
                })
            });

            if (res.ok) {
                const data = await res.json();
                App.showToast(`Launched Sandboxed App (PID ${data.pid}) under Low Integrity Token & Job Object!`, 'success');
                fetchWorkloads();
            } else {
                const err = await res.json();
                App.showToast(`Launch failed: ${err.detail || 'Error'}`, 'error');
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
            // Already exists or creation logged
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
                ? '<span style="color: var(--accent-emerald); font-weight: 700;">🟢 RUNNING</span>'
                : `<span style="color: var(--text-muted);">⚪ EXITED (${w.return_code})</span>`;

            html += `
                <tr>
                    <td style="font-weight: 600; color: var(--accent-indigo);">${escapeHtml(w.workload_id)}</td>
                    <td class="pid-cell">${w.pid}</td>
                    <td>${statusBadge}</td>
                    <td style="font-family: var(--font-mono);">${w.return_code !== null ? w.return_code : '-'}</td>
                    <td>
                        <button class="btn btn-sm btn-danger" onclick="Workloads.stopWorkload('${escapeHtml(w.workload_id)}')">
                            🛑 Terminate
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
        stopWorkload
    };
})();
