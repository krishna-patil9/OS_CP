/**
 * isolation.js — Windows NT Kernel Job Object Sandbox Controller
 * 
 * Manages:
 *   - Creating Job Objects with hard limits (CPU Rate Cap %, Private Memory MB, Working Set, Fork-Bomb Guard)
 *   - Live Sandbox Cards display & process enclosures
 *   - Kernel accounting inspection (CPU cycles, page faults, peak memory)
 *   - Process attachment & sandbox destruction
 */

const Isolation = (() => {
    let cachedSandboxes = [];

    function init() {
        const btnCreate = document.getElementById('btnCreateSandbox');
        const btnAssign = document.getElementById('btnAssignProcess');

        if (btnCreate) {
            btnCreate.addEventListener('click', handleCreateSandbox);
        }

        if (btnAssign) {
            btnAssign.addEventListener('click', handleAssignProcess);
        }

        // Initial fetch
        fetchSandboxes();
    }

    async function fetchSandboxes() {
        try {
            const res = await fetch('/api/sandboxes');
            if (res.ok) {
                const data = await res.json();
                renderSandboxes(data);
            }
        } catch (e) {
            console.error('Failed to fetch sandboxes:', e);
        }
    }

    function updateFromTelemetry(sandboxes) {
        if (sandboxes) {
            renderSandboxes(sandboxes);
        }
    }

    async function handleCreateSandbox() {
        const nameInput = document.getElementById('sbNameInput');
        const cpuInput = document.getElementById('sbCpuCapInput');
        const memInput = document.getElementById('sbMemoryCapInput');
        const procsInput = document.getElementById('sbMaxProcsInput');
        const prioSelect = document.getElementById('sbPrioritySelect');

        const name = nameInput.value.trim();
        if (!name) {
            App.showToast('Please specify a sandbox name', 'warning');
            return;
        }

        const payload = {
            name: name,
            cpu_rate_percent: parseInt(cpuInput.value) || null,
            memory_limit_mb: parseInt(memInput.value) || null,
            max_active_processes: parseInt(procsInput.value) || null,
            priority_class: prioSelect.value || null,
            kill_on_close: true
        };

        try {
            const res = await fetch('/api/sandboxes', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            if (res.ok) {
                App.showToast(`Job Sandbox "${name}" successfully created!`, 'success');
                nameInput.value = '';
                fetchSandboxes();
            } else {
                const err = await res.json();
                App.showToast(`Creation failed: ${err.detail || 'Unknown error'}`, 'error');
            }
        } catch (e) {
            App.showToast(`Error: ${e.message}`, 'error');
        }
    }

    async function handleAssignProcess() {
        const sbSelect = document.getElementById('assignSelectSandbox');
        const pidInput = document.getElementById('assignPidInput');

        const sandboxName = sbSelect.value;
        const pid = parseInt(pidInput.value);

        if (!sandboxName) {
            App.showToast('Please select an active sandbox', 'warning');
            return;
        }
        if (!pid || isNaN(pid)) {
            App.showToast('Please enter a valid process PID', 'warning');
            return;
        }

        try {
            const res = await fetch(`/api/sandboxes/${sandboxName}/assign`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ pid })
            });

            if (res.ok) {
                App.showToast(`PID ${pid} successfully enclosed in "${sandboxName}"!`, 'success');
                pidInput.value = '';
                fetchSandboxes();
            } else {
                const err = await res.json();
                App.showToast(`Assignment failed: ${err.detail || 'Unknown error'}`, 'error');
            }
        } catch (e) {
            App.showToast(`Error: ${e.message}`, 'error');
        }
    }

    async function destroySandbox(name) {
        if (!confirm(`Destroy sandbox "${name}"? All enclosed processes will be terminated by the OS kernel.`)) return;

        try {
            const res = await fetch(`/api/sandboxes/${name}`, { method: 'DELETE' });
            if (res.ok) {
                App.showToast(`Sandbox "${name}" destroyed`, 'success');
                fetchSandboxes();
            } else {
                const err = await res.json();
                App.showToast(`Destroy failed: ${err.detail || 'Unknown error'}`, 'error');
            }
        } catch (e) {
            App.showToast(`Error: ${e.message}`, 'error');
        }
    }

    async function inspectAccounting(name) {
        try {
            const res = await fetch(`/api/sandboxes/${name}/accounting`);
            if (res.ok) {
                const acc = await res.json();
                alert(
                    `Kernel Accounting for [${name}]:\n` +
                    `-----------------------------------------\n` +
                    `Total User Time: ${acc.total_user_time_sec?.toFixed(3) || 0}s\n` +
                    `Total Kernel Time: ${acc.total_kernel_time_sec?.toFixed(3) || 0}s\n` +
                    `Peak Process Memory: ${acc.peak_process_memory_mb?.toFixed(2) || 0} MB\n` +
                    `Peak Job Memory: ${acc.peak_job_memory_mb?.toFixed(2) || 0} MB\n` +
                    `Total Page Faults: ${acc.total_page_fault_count || 0}\n` +
                    `Active Processes: ${acc.active_processes || 0}\n` +
                    `Total Process Spawned: ${acc.total_processes || 0}`
                );
            }
        } catch (e) {
            App.showToast(`Accounting query failed: ${e.message}`, 'error');
        }
    }

    function renderSandboxes(sandboxes) {
        cachedSandboxes = sandboxes || [];

        const countBadge = document.getElementById('activeSandboxCount');
        if (countBadge) countBadge.textContent = cachedSandboxes.length;

        // Update dropdown selectors
        const assignSelect = document.getElementById('assignSelectSandbox');
        if (assignSelect) {
            const curVal = assignSelect.value;
            assignSelect.innerHTML = '<option value="">-- Select Active Sandbox --</option>';
            for (const sb of cachedSandboxes) {
                assignSelect.innerHTML += `<option value="${escapeHtml(sb.name)}">${escapeHtml(sb.name)}</option>`;
            }
            if (curVal) assignSelect.value = curVal;
        }

        const container = document.getElementById('sandboxListContainer');
        if (!container) return;

        if (cachedSandboxes.length === 0) {
            container.innerHTML = `
                <div class="empty-state" style="grid-column: 1 / -1;">
                    <div class="empty-state-icon">🛡️</div>
                    <div class="empty-state-text">No active Job Objects created yet. Use the form on the left or launch a sandboxed workload.</div>
                </div>
            `;
            return;
        }

        let html = '';
        for (const sb of cachedSandboxes) {
            const cfg = sb.config || sb;
            const pids = sb.assigned_pids || [];
            const pidTags = pids.length > 0
                ? pids.map(p => `<span class="sandbox-pid-tag">PID ${p}</span>`).join(' ')
                : '<span style="color: var(--text-muted); font-size: 0.75rem;">No active processes</span>';

            html += `
                <div class="sandbox-card">
                    <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: var(--space-sm);">
                        <div class="sandbox-name">🛡️ ${escapeHtml(sb.name)}</div>
                        <div class="btn-group">
                            <button class="btn btn-sm" title="Inspect Kernel Accounting" onclick="Isolation.inspectAccounting('${escapeHtml(sb.name)}')">📊</button>
                            <button class="btn btn-sm btn-danger" title="Destroy Sandbox" onclick="Isolation.destroySandbox('${escapeHtml(sb.name)}')">🗑️</button>
                        </div>
                    </div>

                    <div class="sandbox-limits">
                        <div class="sandbox-limit">
                            <span class="sandbox-limit-label">CPU Rate Hard Cap</span>
                            <span class="sandbox-limit-value">${cfg.cpu_rate_percent ? cfg.cpu_rate_percent + '%' : 'Unlimited'}</span>
                        </div>
                        <div class="sandbox-limit">
                            <span class="sandbox-limit-label">Hard RAM Limit</span>
                            <span class="sandbox-limit-value">${cfg.memory_limit_mb ? cfg.memory_limit_mb + ' MB' : 'Unlimited'}</span>
                        </div>
                        <div class="sandbox-limit">
                            <span class="sandbox-limit-label">Max Procs (Fork Cap)</span>
                            <span class="sandbox-limit-value">${cfg.max_active_processes || 'Unlimited'}</span>
                        </div>
                        <div class="sandbox-limit">
                            <span class="sandbox-limit-label">Priority Class</span>
                            <span class="sandbox-limit-value" style="font-size: 0.75rem;">${escapeHtml(cfg.priority_class || 'DEFAULT')}</span>
                        </div>
                    </div>

                    <div style="margin-top: var(--space-sm);">
                        <div class="sandbox-limit-label" style="margin-bottom: 4px;">Enclosed Processes (${pids.length})</div>
                        <div class="sandbox-pids">${pidTags}</div>
                    </div>
                </div>
            `;
        }

        container.innerHTML = html;
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
        fetchSandboxes,
        updateFromTelemetry,
        destroySandbox,
        inspectAccounting
    };
})();
