/**
 * app.js — Main Application Controller & Router (Optimized)
 * 
 * Coordinates:
 *   - Navigation tab routing & active panel transitions with visibility hooks
 *   - Two Execution Modes: Normal Mode (Default/Safe) and Testing Mode (OS Labs)
 *   - Global toast notification alert system
 *   - WebSocket connection lifecycle with paced 2.0s telemetry & single connection guard
 *   - Dispatching live telemetry packets to sub-controllers only when needed
 */

const App = (() => {
    let ws = null;
    let wsReconnectAttempts = 0;
    let wsReconnectTimer = null;
    let startTime = Date.now();
    let executionMode = 'normal';
    let currentTab = 'telemetry';

    function init() {
        initTabs();
        initUptimeTimer();
        initModeControls();

        // Initialize sub-controllers
        if (typeof Telemetry !== 'undefined') Telemetry.init();
        if (typeof Isolation !== 'undefined') Isolation.init();
        if (typeof DeadlockGraph !== 'undefined') DeadlockGraph.init();
        if (typeof Simulator !== 'undefined') Simulator.init();
        if (typeof Workloads !== 'undefined') Workloads.init();

        // Query initial mode from backend
        fetchExecutionMode();

        // Connect WebSocket (single connection)
        connectWebSocket();

        // Clean WebSocket close on page exit
        window.addEventListener('beforeunload', () => {
            if (ws) {
                try {
                    ws.close();
                } catch (e) {}
            }
        });
    }

    // ── Execution Modes (Normal vs Testing) ──────────────────────────────────

    async function fetchExecutionMode() {
        try {
            const res = await fetch('/api/system/mode');
            if (res.ok) {
                const data = await res.json();
                setExecutionMode(data.mode);
            }
        } catch (e) {
            console.debug('Failed to fetch initial mode:', e);
        }
    }

    function initModeControls() {
        const btnToggle = document.getElementById('btnToggleMode');
        if (btnToggle) {
            btnToggle.addEventListener('click', toggleExecutionMode);
        }
    }

    async function toggleExecutionMode() {
        const newMode = executionMode === 'normal' ? 'testing' : 'normal';
        if (newMode === 'testing') {
            const confirmed = confirm(
                "Activate Testing Mode?\n\n" +
                "Testing Mode allows manual launching of synthetic OS workloads (CPU Hog, Memory Hog, Mutex Deadlock, Sandboxed App).\n" +
                "Workloads are strictly bound to safe laptop limits."
            );
            if (!confirmed) return;
        }

        try {
            const res = await fetch('/api/system/mode', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mode: newMode })
            });

            if (res.ok) {
                const data = await res.json();
                setExecutionMode(data.mode);
                showToast(
                    data.mode === 'normal'
                        ? 'Returned to Normal Mode (Lightweight & Safe)'
                        : 'Testing Mode Active (OS Workloads Unlocked)',
                    data.mode === 'normal' ? 'info' : 'warning'
                );
            }
        } catch (e) {
            showToast('Failed to switch mode: ' + e.message, 'error');
        }
    }

    function setExecutionMode(mode) {
        executionMode = mode;
        const badge = document.getElementById('modeBadge');
        const btnToggle = document.getElementById('btnToggleMode');
        const lockBanner = document.getElementById('workloadLockBanner');

        if (badge) {
            badge.className = `mode-badge ${mode}`;
            badge.textContent = mode === 'normal' ? 'Normal Mode' : 'Testing Mode';
        }

        if (btnToggle) {
            btnToggle.textContent = mode === 'normal' ? 'Activate Testing Mode' : 'Return to Normal Mode';
            btnToggle.className = mode === 'normal' ? 'btn btn-sm btn-outline' : 'btn btn-sm btn-primary';
        }

        if (lockBanner) {
            lockBanner.style.display = mode === 'normal' ? 'flex' : 'none';
        }
    }

    function getExecutionMode() {
        return executionMode;
    }

    // ── Tab Routing ─────────────────────────────────────────────────────────

    function initTabs() {
        const tabs = document.querySelectorAll('.nav-tab');
        tabs.forEach(tab => {
            tab.addEventListener('click', () => {
                const targetTab = tab.getAttribute('data-tab');
                switchTab(targetTab);
            });
        });
    }

    function switchTab(tabName) {
        const oldTab = currentTab;
        currentTab = tabName;

        const tabs = document.querySelectorAll('.nav-tab');
        const panels = document.querySelectorAll('.tab-panel');

        tabs.forEach(t => {
            const isActive = t.getAttribute('data-tab') === tabName;
            t.classList.toggle('active', isActive);
            t.setAttribute('aria-selected', isActive ? 'true' : 'false');
        });

        panels.forEach(p => {
            const isTarget = p.id.toLowerCase() === `panel${tabName.toLowerCase()}`;
            p.classList.toggle('active', isTarget);
        });

        // Tab lifecycle callbacks to suspend/resume background tasks
        if (oldTab === 'deadlock' && typeof DeadlockGraph !== 'undefined' && DeadlockGraph.onTabDeactivated) {
            DeadlockGraph.onTabDeactivated();
        }
        if (oldTab === 'workloads' && typeof Workloads !== 'undefined' && Workloads.onTabDeactivated) {
            Workloads.onTabDeactivated();
        }

        if (tabName === 'telemetry' && typeof Telemetry !== 'undefined' && Telemetry.onTabActivated) {
            Telemetry.onTabActivated();
        } else if (tabName === 'deadlock' && typeof DeadlockGraph !== 'undefined' && DeadlockGraph.onTabActivated) {
            requestAnimationFrame(() => {
                DeadlockGraph.onTabActivated();
            });
        } else if (tabName === 'workloads' && typeof Workloads !== 'undefined' && Workloads.onTabActivated) {
            Workloads.onTabActivated();
        }
    }

    // ── WebSocket Telemetry Stream (Paced & Protected) ───────────────────────

    function connectWebSocket() {
        // Prevent duplicate concurrent WebSocket attempts
        if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
            return;
        }

        if (wsReconnectTimer) {
            clearTimeout(wsReconnectTimer);
            wsReconnectTimer = null;
        }

        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws/telemetry`;

        const dot = document.getElementById('wsStatusDot');
        const text = document.getElementById('wsStatusText');

        if (dot) dot.className = 'status-dot offline';
        if (text) text.textContent = 'Connecting...';

        try {
            ws = new WebSocket(wsUrl);

            ws.onopen = () => {
                wsReconnectAttempts = 0;
                if (dot) dot.className = 'status-dot';
                if (text) text.textContent = 'Live Feed (~2s)';
                console.log('[WebSocket] Connected to host telemetry stream (2.0s paced)');
            };

            ws.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    if (data.type === 'telemetry') {
                        // Sync mode if provided
                        if (data.mode && data.mode !== executionMode) {
                            setExecutionMode(data.mode);
                        }

                        // Dispatch to telemetry sub-controller
                        if (typeof Telemetry !== 'undefined') {
                            Telemetry.update(data);
                        }
                        // Update active sandboxes in isolation panel if present
                        if (typeof Isolation !== 'undefined' && data.sandboxes) {
                            Isolation.updateFromTelemetry(data.sandboxes);
                        }
                        // Update live deadlock monitor if present
                        if (typeof DeadlockGraph !== 'undefined' && DeadlockGraph.updateFromTelemetry && data.live_deadlock) {
                            DeadlockGraph.updateFromTelemetry(data.live_deadlock);
                        }
                    } else if (data.type === 'live_deadlock_update') {
                        if (typeof DeadlockGraph !== 'undefined' && DeadlockGraph.handleLiveSnapshotPush) {
                            DeadlockGraph.handleLiveSnapshotPush(data);
                        }
                    }
                } catch (e) {
                    console.error('[WebSocket] Failed to parse message:', e);
                }
            };

            ws.onclose = () => {
                ws = null;
                if (dot) dot.className = 'status-dot offline';
                if (text) text.textContent = 'Disconnected';
                scheduleReconnect();
            };

            ws.onerror = (err) => {
                console.warn('[WebSocket] Connection error:', err);
                if (ws) {
                    try { ws.close(); } catch (e) {}
                    ws = null;
                }
            };

        } catch (e) {
            console.error('[WebSocket] Setup exception:', e);
            scheduleReconnect();
        }
    }

    function scheduleReconnect() {
        if (wsReconnectTimer) return;
        wsReconnectAttempts++;
        const delay = Math.min(6000, 1500 * Math.pow(1.3, wsReconnectAttempts));
        const text = document.getElementById('wsStatusText');
        if (text) text.textContent = `Reconnecting in ${(delay / 1000).toFixed(0)}s...`;

        wsReconnectTimer = setTimeout(() => {
            wsReconnectTimer = null;
            connectWebSocket();
        }, delay);
    }

    // ── Global Toast Notifications ──────────────────────────────────────────

    function showToast(message, type = 'info') {
        const container = document.getElementById('toastContainer');
        if (!container) return;

        const toast = document.createElement('div');
        toast.className = `toast ${type}`;

        const icons = {
            success: '<svg viewBox="0 0 24 24" style="width:16px;height:16px;stroke:var(--status-success-text);fill:none;stroke-width:2.2;flex-shrink:0;"><polyline points="20 6 9 17 4 12"></polyline></svg>',
            error: '<svg viewBox="0 0 24 24" style="width:16px;height:16px;stroke:var(--status-danger-text);fill:none;stroke-width:2.2;flex-shrink:0;"><circle cx="12" cy="12" r="10"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line></svg>',
            warning: '<svg viewBox="0 0 24 24" style="width:16px;height:16px;stroke:var(--status-warning-text);fill:none;stroke-width:2.2;flex-shrink:0;"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>',
            info: '<svg viewBox="0 0 24 24" style="width:16px;height:16px;stroke:var(--accent-hover);fill:none;stroke-width:2.2;flex-shrink:0;"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>'
        };

        toast.innerHTML = `<span style="display:inline-flex;align-items:center;margin-right:8px;" aria-hidden="true">${icons[type] || icons.info}</span><span>${escapeHtml(message)}</span>`;
        container.appendChild(toast);

        setTimeout(() => {
            if (toast.parentElement) {
                toast.parentElement.removeChild(toast);
            }
        }, 3000);
    }

    // ── Uptime Timer ────────────────────────────────────────────────────────

    function initUptimeTimer() {
        const el = document.getElementById('systemUptime');
        if (!el) return;

        setInterval(() => {
            const elapsedSec = Math.floor((Date.now() - startTime) / 1000);
            const hrs = Math.floor(elapsedSec / 3600);
            const mins = Math.floor((elapsedSec % 3600) / 60);
            const secs = elapsedSec % 60;

            const pad = (n) => String(n).padStart(2, '0');
            el.textContent = `Uptime: ${pad(hrs)}:${pad(mins)}:${pad(secs)}`;
        }, 1000);
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
        switchTab,
        showToast,
        getExecutionMode,
        setExecutionMode,
        toggleExecutionMode
    };
})();

// Initialize once DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    App.init();
});
